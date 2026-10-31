"""
The TelemetrySource seam.

There is nothing to test about a Protocol at runtime -- it has no behaviour.
What these tests pin down is the *architectural claim* the Protocol makes:

    An implementation satisfies TelemetrySource by having read(), without
    importing it, inheriting from it, or registering with it.

and the claim the whole two-day plan rests on:

    Whatever produces the bytes, they reach PacketDecoder identically.

Both are properties of the design rather than of any one class, which is why
they are tested here with throwaway sources rather than being left implicit
until SimulationSource and UARTSource exist.
"""

from backend.protocol.decoder import PacketDecoder
from backend.protocol.encoder import encode_frame
from backend.protocol.spec import ReactorState
from backend.telemetry_frame import TelemetryFrame

# NOTE: this module deliberately does NOT import TelemetrySource.
# That absence is the point -- see test_source_needs_no_import_to_conform.


def make_frame(uptime_ms: int) -> TelemetryFrame:
    return TelemetryFrame(
        device_uptime_ms=uptime_ms,
        pressure_kpa=101.3,
        pressure_rate_kpa_s=0.0,
        temperature_c=0.0,
        temperature_rate_c_s=0.0,
        co=400.0,
        co2=3000.0,
        ch4=1800.0,
        gas_ratio=0.6,
        gas_concentration=5200.0,
        anomaly_score=0.05,
        warning=False,
        emergency=False,
        anomaly=False,
        state=ReactorState.NORMAL,
    )


class ScriptedSource:
    """
    A telemetry source that replays a fixed list of byte chunks.

    Inherits from nothing and imports nothing from the seam. It is a valid
    TelemetrySource purely because it has read().
    """

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def read(self) -> bytes:
        if not self._chunks:
            return b""

        return self._chunks.pop(0)


class SilentSource:
    """A source that never produces data -- an unplugged cable."""

    def read(self) -> bytes:
        return b""


def drain(source, decoder: PacketDecoder, polls: int) -> list:
    """
    A minimal driver: poll the source, feed the decoder, collect packets.

    This is the shape of the real pump that lands with the backend module. It
    is written here in five lines to show that driving the seam requires
    nothing more than that -- no lifecycle, no threading, no callbacks.
    """
    packets = []

    for _ in range(polls):
        packets.extend(decoder.feed(source.read()))

    return packets


# --------------------------------------------------------------------------
# Structural conformance
# --------------------------------------------------------------------------


def test_source_needs_no_import_to_conform():
    """
    ScriptedSource satisfies the seam without any coupling to it.

    If TelemetrySource were an ABC, this module would have to import it and
    ScriptedSource would have to inherit from it. The fact that neither is
    true is the entire argument for structural typing here.
    """
    source = ScriptedSource([b"abc"])

    assert callable(source.read)
    assert source.read() == b"abc"


def test_read_returns_empty_bytes_when_idle():
    """b"" is the normal idle case, not an error condition."""
    assert SilentSource().read() == b""

    exhausted = ScriptedSource([b"one"])
    exhausted.read()

    assert exhausted.read() == b""


def test_silent_source_produces_no_packets_and_no_errors():
    """
    An unplugged cable must not look like corruption.

    Link health is judged on frame arrival elsewhere; the decoder itself
    should simply report nothing wrong, because nothing is wrong with the
    bytes it was given -- there were none.
    """
    decoder = PacketDecoder()

    assert drain(SilentSource(), decoder, polls=100) == []
    assert decoder.stats.error_count == 0
    assert decoder.stats.bytes_received == 0


# --------------------------------------------------------------------------
# Bytes reach the decoder identically regardless of source
# --------------------------------------------------------------------------


def test_whole_packet_per_read():
    """The shape a well-behaved UART read tends to produce at 1 Hz."""
    frames = [make_frame(index * 1000) for index in range(3)]

    source = ScriptedSource([encode_frame(frame) for frame in frames])
    decoder = PacketDecoder()

    packets = drain(source, decoder, polls=3)

    assert [packet.frame.device_uptime_ms for packet in packets] == [0, 1000, 2000]
    assert decoder.stats.error_count == 0


def test_packets_split_across_reads():
    """
    The shape a real UART actually produces: reads land wherever they land.

    The seam returns bytes, not packets, precisely so this is the decoder's
    problem and not every source's problem.
    """
    stream = b"".join(encode_frame(make_frame(index * 1000)) for index in range(3))

    chunks = [stream[offset : offset + 13] for offset in range(0, len(stream), 13)]

    source = ScriptedSource(chunks)
    decoder = PacketDecoder()

    packets = drain(source, decoder, polls=len(chunks))

    assert len(packets) == 3
    assert decoder.stats.error_count == 0


def test_several_packets_in_one_read():
    """A burst -- history replay, or a backlog after a slow repaint."""
    stream = b"".join(encode_frame(make_frame(index * 1000)) for index in range(5))

    decoder = PacketDecoder()

    packets = drain(ScriptedSource([stream]), decoder, polls=1)

    assert len(packets) == 5


def test_idle_polls_between_packets_are_harmless():
    """
    Realistic timing: a driver polling at 20 Hz sees ~19 empty reads between
    frames arriving at 1 Hz. That must cost nothing and corrupt nothing.
    """
    packet = encode_frame(make_frame(5000))

    chunks = [b""] * 19 + [packet] + [b""] * 19

    decoder = PacketDecoder()

    packets = drain(ScriptedSource(chunks), decoder, polls=len(chunks))

    assert len(packets) == 1
    assert decoder.stats.error_count == 0


def test_two_different_sources_yield_identical_frames():
    """
    The claim the whole simulation-first plan depends on.

    One source hands over a whole packet, the other dribbles it out a byte at
    a time. The decoded frame must be indistinguishable -- otherwise the
    dashboard would behave differently on hardware than in simulation, and two
    days of development against the simulator would prove nothing.
    """
    frame = make_frame(4242)
    packet = encode_frame(frame)

    bulk = ScriptedSource([packet])
    dribble = ScriptedSource([packet[i : i + 1] for i in range(len(packet))])

    from_bulk = drain(bulk, PacketDecoder(), polls=1)
    from_dribble = drain(dribble, PacketDecoder(), polls=len(packet))

    assert len(from_bulk) == len(from_dribble) == 1

    left = from_bulk[0].frame
    right = from_dribble[0].frame

    assert left.as_wire_tuple() == right.as_wire_tuple()
