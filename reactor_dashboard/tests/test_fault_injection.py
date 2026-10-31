"""
Link fault injection.

These tests check the injector produces conditions the dashboard's
communication handling must survive, and -- just as importantly -- that the
decoder actually *notices* them. An injector that drops packets the decoder
never misses would be testing nothing.

The pairing matters: FaultStats counts faults caused, DecoderStats counts
faults observed. Several tests below assert the two agree, which is what makes
this a test of the whole communication path rather than of the injector alone.
"""

import random

import pytest

from backend.protocol.decoder import PacketDecoder
from backend.protocol.encoder import encode_frame
from backend.protocol.spec import ReactorState
from backend.telemetry_frame import TelemetryFrame
from config.settings import LinkFaultConfig, load_config
from simulation.factory import create_simulation_source
from simulation.fault_injection import FaultInjectingSource, LinkFault

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

PRISTINE = LinkFaultConfig(
    enabled=True,
    packet_loss_probability=0.0,
    corruption_probability=0.0,
    delay_probability=0.0,
    delay_seconds=2.0,
    disconnect_probability=0.0,
    disconnect_duration_s=20.0,
)


def with_rates(**overrides) -> LinkFaultConfig:
    import dataclasses

    return dataclasses.replace(PRISTINE, **overrides)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class PacketSource:
    """Emits one whole encoded frame per read. Stands in for the simulator."""

    def __init__(self) -> None:
        self.uptime_ms = 0

    def read(self) -> bytes:
        self.uptime_ms += 1000

        return encode_frame(
            TelemetryFrame(
                device_uptime_ms=self.uptime_ms,
                pressure_kpa=105.0,
                pressure_rate_kpa_s=0.0,
                temperature_c=0.0,
                temperature_rate_c_s=0.0,
                co=420.0,
                co2=3200.0,
                ch4=1850.0,
                gas_ratio=0.578,
                gas_concentration=5470.0,
                anomaly_score=0.05,
                warning=False,
                emergency=False,
                anomaly=False,
                state=ReactorState.NORMAL,
            )
        )


def build(config: LinkFaultConfig, seed: int = 1):
    clock = FakeClock()

    injector = FaultInjectingSource(
        source=PacketSource(), config=config, rng=random.Random(seed), clock=clock
    )

    return injector, clock


def drain(injector, clock, reads: int, decoder=None):
    decoder = decoder or PacketDecoder()
    packets = []

    for _ in range(reads):
        clock.advance(1.0)
        packets.extend(decoder.feed(injector.read()))

    return packets, decoder


# --------------------------------------------------------------------------
# Pass-through
# --------------------------------------------------------------------------


def test_injector_with_no_faults_is_transparent():
    """
    The default must be a pristine link. A developer building a gauge should
    not be fighting injected CRC errors.
    """
    injector, clock = build(PRISTINE)

    packets, decoder = drain(injector, clock, 200)

    assert len(packets) == 200
    assert decoder.stats.error_count == 0
    assert injector.stats.reads_passed == 200


def test_disabled_link_config_means_no_injector_in_the_chain():
    """Nothing wraps the source unless fault injection is switched on."""
    import dataclasses

    simulation = load_config().simulation

    assert simulation.link.enabled is False

    clock = FakeClock()
    plain = create_simulation_source(simulation, clock=clock)

    assert not isinstance(plain, FaultInjectingSource)

    enabled = dataclasses.replace(
        simulation, link=dataclasses.replace(simulation.link, enabled=True)
    )

    wrapped = create_simulation_source(enabled, clock=clock)

    assert isinstance(wrapped, FaultInjectingSource)


def test_wrapped_source_is_reachable_explicitly():
    """
    `injector.source` rather than attribute forwarding.

    A debug panel writing `injector.source.scenarios` says plainly that it is
    reaching past the injector; forwarding would hide a whole layer.
    """
    import dataclasses

    simulation = load_config().simulation
    enabled = dataclasses.replace(
        simulation, link=dataclasses.replace(simulation.link, enabled=True)
    )

    wrapped = create_simulation_source(enabled, clock=FakeClock())

    assert wrapped.source.scenarios.available


# --------------------------------------------------------------------------
# Packet loss
# --------------------------------------------------------------------------


def test_packet_loss_removes_frames_without_corrupting_the_rest():
    """
    A lost packet must leave a gap, not a mess. The surviving frames have to
    decode cleanly, or the dashboard would see loss as corruption.
    """
    injector, clock = build(with_rates(packet_loss_probability=0.25), seed=3)

    packets, decoder = drain(injector, clock, 400)

    assert injector.stats.packets_dropped > 50
    assert len(packets) == 400 - injector.stats.packets_dropped
    assert decoder.stats.error_count == 0


def test_triggered_packet_loss_drops_exactly_one_frame():
    """One-shot: an operator wants one lost packet, not a broken link."""
    injector, clock = build(PRISTINE)

    drain(injector, clock, 5)

    injector.trigger(LinkFault.PACKET_LOSS)

    packets, _ = drain(injector, clock, 3)

    assert len(packets) == 2
    assert injector.stats.packets_dropped == 1


def test_lost_frames_leave_a_gap_in_device_uptime():
    """
    What the dashboard actually sees. The device clock keeps advancing while
    frames go missing, so a gap is detectable from the telemetry itself
    without needing the injector to say so.
    """
    injector, clock = build(PRISTINE)

    drain(injector, clock, 3)
    injector.trigger(LinkFault.PACKET_LOSS)
    packets, _ = drain(injector, clock, 3)

    uptimes = [packet.frame.device_uptime_ms for packet in packets]

    assert uptimes == [5000, 6000]  # 4000 was dropped


# --------------------------------------------------------------------------
# Corruption
# --------------------------------------------------------------------------


def test_corruption_is_detected_by_the_crc():
    """
    The whole reason the protocol carries a checksum. A corrupted frame must
    never reach the dashboard as data.
    """
    injector, clock = build(with_rates(corruption_probability=1.0), seed=5)

    packets, decoder = drain(injector, clock, 60)

    assert injector.stats.packets_corrupted == 60
    assert packets == []

    # Every corruption is caught -- as a CRC error when the flipped bit lands
    # in the payload, or as a resync when it lands in the sync word. Both are
    # real line faults; neither delivers bad data.
    assert decoder.stats.crc_errors > 0
    assert decoder.stats.crc_errors + decoder.stats.resyncs >= 60


def test_corruption_never_delivers_a_bad_frame():
    """
    The claim that matters. Across many seeds, not one corrupted packet may
    be decoded as a valid frame.
    """
    for seed in range(40):
        injector, clock = build(with_rates(corruption_probability=1.0), seed=seed)

        packets, _ = drain(injector, clock, 25)

        assert packets == [], f"corrupted frame accepted with seed {seed}"


def test_triggered_corruption_affects_exactly_one_frame():
    injector, clock = build(PRISTINE)

    drain(injector, clock, 3)

    injector.trigger(LinkFault.CORRUPTION)

    packets, decoder = drain(injector, clock, 3)

    assert injector.stats.packets_corrupted == 1

    # Exactly one frame is lost; the two good ones either side survive.
    assert len(packets) == 2

    # The decoder noticed. Note it reports MORE than one recovery event for a
    # single corrupt packet, and that is correct: rejecting the bad CRC is one
    # resync, and discarding the remainder of the ruined packet to find the
    # next sync word is another. Asserting a count of exactly one here would
    # be asserting a misunderstanding of how resynchronisation works.
    assert decoder.stats.error_count + decoder.stats.resyncs > 0
    assert decoder.stats.discarded_bytes > 0


def test_decoder_recovers_immediately_after_a_corrupt_frame():
    """
    Recovery is the point. One bad packet must not desynchronise the stream
    and take the following good ones with it.
    """
    injector, clock = build(with_rates(corruption_probability=0.3), seed=9)

    packets, decoder = drain(injector, clock, 500)

    delivered = len(packets)
    corrupted = injector.stats.packets_corrupted

    assert corrupted > 50
    assert delivered == 500 - corrupted


# --------------------------------------------------------------------------
# Delay
# --------------------------------------------------------------------------


def test_delayed_frame_arrives_late_rather_than_never():
    injector, clock = build(PRISTINE)

    drain(injector, clock, 3)

    injector.trigger(LinkFault.DELAY)

    immediate, decoder = drain(injector, clock, 1)

    # The delayed frame is withheld; nothing else was due this read.
    assert immediate == []
    assert injector.stats.packets_delayed == 1

    later, _ = drain(injector, clock, 3, decoder)

    uptimes = [packet.frame.device_uptime_ms for packet in later]

    # The held frame (4000) is released alongside the frames that follow it,
    # so it arrives out of step with the cadence -- which is exactly the
    # condition the dashboard's ordering logic has to cope with.
    assert 4000 in uptimes


def test_delayed_frames_are_not_lost_over_a_long_run():
    """Delay must be delay, not silent loss."""
    injector, clock = build(with_rates(delay_probability=0.2), seed=4)

    packets, decoder = drain(injector, clock, 400)

    # Everything eventually arrives except what is still in flight.
    assert len(packets) >= 400 - 2
    assert decoder.stats.error_count == 0


def test_delayed_frames_still_decode_cleanly():
    """A late packet is intact; only its timing is wrong."""
    injector, clock = build(with_rates(delay_probability=0.5), seed=6)

    packets, decoder = drain(injector, clock, 200)

    assert decoder.stats.error_count == 0
    assert all(packet.frame is not None for packet in packets)


# --------------------------------------------------------------------------
# Disconnect and reconnect
# --------------------------------------------------------------------------


def test_disconnect_produces_silence_not_errors():
    """
    An unplugged cable must not look like corruption. The decoder should
    report nothing wrong, because nothing is wrong with the bytes it was
    given -- there were none.
    """
    injector, clock = build(PRISTINE)

    injector.disconnect(duration_s=10.0)

    packets, decoder = drain(injector, clock, 8)

    assert packets == []
    assert decoder.stats.error_count == 0
    assert injector.is_disconnected


def test_link_recovers_after_the_disconnect_expires():
    injector, clock = build(PRISTINE)

    injector.disconnect(duration_s=5.0)

    drain(injector, clock, 4)
    assert injector.is_disconnected

    packets, decoder = drain(injector, clock, 5)

    assert len(packets) > 0
    assert decoder.stats.error_count == 0
    assert not injector.is_disconnected


def test_frames_produced_during_a_disconnect_are_lost_not_buffered():
    """
    A real unplugged cable does not hold bytes for later. Buffering them would
    produce an unrealistic flood on reconnect and hide the outage.
    """
    injector, clock = build(PRISTINE)

    drain(injector, clock, 2)

    injector.disconnect(duration_s=5.0)
    drain(injector, clock, 4)

    after, _ = drain(injector, clock, 3)

    uptimes = [packet.frame.device_uptime_ms for packet in after]

    # Frames 3000..6000 fell in the outage and are simply gone.
    assert min(uptimes) >= 7000
    assert injector.stats.bytes_suppressed > 0


def test_manual_reconnect_ends_an_outage_early():
    injector, clock = build(PRISTINE)

    injector.disconnect(duration_s=3600.0)

    drain(injector, clock, 3)
    assert injector.is_disconnected

    injector.reconnect()

    packets, _ = drain(injector, clock, 2)

    assert len(packets) == 2


def test_triggering_disconnect_uses_the_configured_duration():
    injector, clock = build(with_rates(disconnect_duration_s=4.0))

    injector.trigger(LinkFault.DISCONNECT)

    assert injector.is_disconnected

    clock.advance(5.0)

    assert not injector.is_disconnected


# --------------------------------------------------------------------------
# Cross-cutting
# --------------------------------------------------------------------------


def test_every_fault_type_can_be_triggered():
    """Guards against a fault being defined but unreachable."""
    for fault in LinkFault:
        injector, clock = build(PRISTINE)

        drain(injector, clock, 2)
        injector.trigger(fault)
        drain(injector, clock, 4)

        stats = injector.stats

        caused = (
            stats.packets_dropped
            + stats.packets_corrupted
            + stats.packets_delayed
            + stats.disconnects
        )

        assert caused >= 1, f"{fault} had no effect"


def test_at_most_one_fault_per_frame():
    """
    Two faults on one packet -- corrupted AND delayed -- would make the
    decoder's behaviour ambiguous to interpret. The injector's job is to
    produce conditions whose expected outcome is clear.
    """
    aggressive = with_rates(
        packet_loss_probability=0.5,
        corruption_probability=0.5,
        delay_probability=0.5,
    )

    injector, clock = build(aggressive, seed=2)

    reads = 300
    drain(injector, clock, reads)

    stats = injector.stats

    total = (
        stats.reads_passed
        + stats.packets_dropped
        + stats.packets_corrupted
        + stats.packets_delayed
        + stats.disconnects
    )

    assert total == reads


def test_fault_runs_are_reproducible_from_a_seed():
    def run() -> list[int]:
        injector, clock = build(with_rates(
            packet_loss_probability=0.2, corruption_probability=0.2
        ), seed=77)

        packets, _ = drain(injector, clock, 200)

        return [packet.frame.device_uptime_ms for packet in packets]

    assert run() == run()


def test_injector_wraps_the_real_simulator_end_to_end():
    """
    The composed system: reactor, encoder, link faults, decoder.

    Frames still decode to sane reactor values, and the faults show up in the
    decoder's own counters -- which is what the HMI's diagnostics strip will
    be reading.
    """
    import dataclasses

    simulation = load_config().simulation

    link = dataclasses.replace(
        simulation.link,
        enabled=True,
        packet_loss_probability=0.05,
        corruption_probability=0.05,
    )

    clock = FakeClock()
    source = create_simulation_source(
        dataclasses.replace(simulation, link=link), clock=clock
    )

    decoder = PacketDecoder()
    frames = []

    for _ in range(2000):
        clock.advance(1.0)
        frames.extend(p.frame for p in decoder.feed(source.read()) if p.frame)

    assert len(frames) > 1500
    assert decoder.stats.crc_errors + decoder.stats.resyncs > 0
    assert decoder.stats.state_mismatches == 0

    for frame in frames:
        assert 0.0 <= frame.pressure_kpa < 250.0
        assert 0.0 <= frame.anomaly_score <= 1.0


@pytest.mark.parametrize(
    "field",
    [
        "packet_loss_probability",
        "corruption_probability",
        "delay_probability",
        "disconnect_probability",
    ],
)
def test_probabilities_outside_zero_to_one_are_rejected(tmp_path, field):
    """
    A "probability" of 1.5 is always a mistake, and one that would otherwise
    silently mean "always".
    """
    from config.settings import ConfigError
    from tests.test_settings import VALID, write_config

    text = VALID.replace(f"{field} = 0.0", f"{field} = 1.5")

    assert text != VALID, f"{field} not present in the fixture"

    with pytest.raises(ConfigError) as error:
        load_config(write_config(tmp_path, text))

    assert field in str(error.value)
