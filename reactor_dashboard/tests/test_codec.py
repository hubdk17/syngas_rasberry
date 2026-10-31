"""
Encoder/decoder conformance against the firmware wire format.

These tests are the reason the encoder exists. The Raspberry Pi never
transmits telemetry, so encode_frame() is here purely so the decoder can be
driven with known-good bytes -- and so the simulator can drive it with the
same bytes the reactor will.

The tests fall into three groups:

  * Layout      -- byte offsets and sizes match docs/PROTOCOL.md exactly.
  * Round trip  -- every field survives encode -> decode unchanged.
  * Recovery    -- the decoder survives fragmentation, garbage, corruption
                   and false sync words without losing the next good packet.

The recovery group matters most. A decoder that works on clean input is easy;
the reactor will not always provide clean input.
"""

import struct

import pytest

from backend.protocol.crc import crc16
from backend.protocol.decoder import PacketDecoder
from backend.protocol.encoder import (
    encode_frame,
    encode_history_begin,
    encode_history_end,
    encode_history_record,
    encode_packet,
)
from backend.protocol.spec import (
    CRC_SPAN_START,
    HEADER_LENGTH,
    MAX_PACKET_LENGTH,
    PROTOCOL_VERSION,
    SAMPLE_PAYLOAD_LENGTH,
    SYNC_0,
    SYNC_1,
    PacketType,
    ReactorState,
)
from backend.telemetry_frame import TelemetryFrame

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def make_frame(**overrides) -> TelemetryFrame:
    """
    A representative frame.

    Defaults mirror what the current firmware actually emits: an atmospheric
    pressure reading, hard-zero temperature (no sensor exists), and a
    suppressed anomaly verdict. Tests that need other values override them.
    """
    values = dict(
        device_uptime_ms=123_456,
        pressure_kpa=101.25,
        pressure_rate_kpa_s=-0.5,
        temperature_c=0.0,
        temperature_rate_c_s=0.0,
        co=420.5,
        co2=3200.0,
        ch4=1875.25,
        gas_ratio=0.5860156,
        gas_concentration=5495.75,
        anomaly_score=0.125,
        warning=False,
        emergency=False,
        anomaly=False,
        state=ReactorState.NORMAL,
    )
    values.update(overrides)

    return TelemetryFrame(**values)


def assert_frames_equal(actual: TelemetryFrame, expected: TelemetryFrame) -> None:
    """
    Compare wire fields with a float tolerance.

    Field-by-field rather than `==` because the payload is float32 on the wire
    and float64 in Python, so a value that survived the round trip perfectly
    can still differ in the last bit.
    """
    assert actual.device_uptime_ms == expected.device_uptime_ms

    assert actual.pressure_kpa == pytest.approx(expected.pressure_kpa, rel=1e-6)
    assert actual.pressure_rate_kpa_s == pytest.approx(
        expected.pressure_rate_kpa_s, rel=1e-6
    )
    assert actual.temperature_c == pytest.approx(expected.temperature_c, abs=1e-6)
    assert actual.temperature_rate_c_s == pytest.approx(
        expected.temperature_rate_c_s, abs=1e-6
    )
    assert actual.co == pytest.approx(expected.co, rel=1e-6)
    assert actual.co2 == pytest.approx(expected.co2, rel=1e-6)
    assert actual.ch4 == pytest.approx(expected.ch4, rel=1e-6)
    assert actual.gas_ratio == pytest.approx(expected.gas_ratio, rel=1e-6)
    assert actual.gas_concentration == pytest.approx(
        expected.gas_concentration, rel=1e-6
    )
    assert actual.anomaly_score == pytest.approx(expected.anomaly_score, rel=1e-6)

    assert actual.warning == expected.warning
    assert actual.emergency == expected.emergency
    assert actual.anomaly == expected.anomaly
    assert actual.state == expected.state


# --------------------------------------------------------------------------
# Layout -- pins the wire format against docs/PROTOCOL.md
# --------------------------------------------------------------------------


def test_frame_packet_is_56_bytes():
    """6 byte header + 48 byte payload + 2 byte CRC. UARTTelemetry.h:84-91"""
    packet = encode_frame(make_frame())

    assert len(packet) == 56
    assert len(packet) == MAX_PACKET_LENGTH


def test_header_layout():
    packet = encode_frame(make_frame())

    assert packet[0] == SYNC_0 == 0xAA
    assert packet[1] == SYNC_1 == 0x55
    assert packet[2] == PROTOCOL_VERSION == 1
    assert packet[3] == PacketType.FRAME == 0x01

    payload_length = struct.unpack_from("<H", packet, 4)[0]
    assert payload_length == SAMPLE_PAYLOAD_LENGTH == 48


def test_payload_field_offsets_are_independently_verifiable():
    """
    Read each field at its documented absolute offset without going through
    SAMPLE_STRUCT.

    If this test only used the same format string the encoder uses, it would
    prove nothing about the offsets -- both would drift together. The literal
    offsets here come from docs/PROTOCOL.md section 4.
    """
    frame = make_frame(
        device_uptime_ms=0x11223344,
        pressure_kpa=101.25,
        pressure_rate_kpa_s=-0.5,
        temperature_c=1.5,
        temperature_rate_c_s=-2.25,
        co=420.5,
        co2=3200.0,
        ch4=1875.25,
        gas_ratio=0.75,
        gas_concentration=5495.75,
        anomaly_score=0.125,
        warning=True,
        emergency=False,
        anomaly=True,
        state=ReactorState.WARNING,
    )

    packet = encode_frame(frame)
    body = packet[HEADER_LENGTH:]

    assert struct.unpack_from("<I", body, 0)[0] == 0x11223344
    assert struct.unpack_from("<f", body, 4)[0] == pytest.approx(101.25)
    assert struct.unpack_from("<f", body, 8)[0] == pytest.approx(-0.5)
    assert struct.unpack_from("<f", body, 12)[0] == pytest.approx(1.5)
    assert struct.unpack_from("<f", body, 16)[0] == pytest.approx(-2.25)
    assert struct.unpack_from("<f", body, 20)[0] == pytest.approx(420.5)
    assert struct.unpack_from("<f", body, 24)[0] == pytest.approx(3200.0)
    assert struct.unpack_from("<f", body, 28)[0] == pytest.approx(1875.25)
    assert struct.unpack_from("<f", body, 32)[0] == pytest.approx(0.75)
    assert struct.unpack_from("<f", body, 36)[0] == pytest.approx(5495.75)
    assert struct.unpack_from("<f", body, 40)[0] == pytest.approx(0.125)

    assert body[44] == 1  # warning
    assert body[45] == 0  # emergency
    assert body[46] == 1  # anomaly
    assert body[47] == ReactorState.WARNING == 1


def test_crc_covers_version_through_payload_and_excludes_sync():
    """
    The single most important layout test.

    CRC over the whole packet, or over the payload alone, both produce
    plausible 16-bit numbers that would pass a naive round-trip test against
    our own encoder -- and then fail against real firmware. This pins the span
    to [2, 6+N) explicitly.
    """
    packet = encode_frame(make_frame())

    payload_end = HEADER_LENGTH + SAMPLE_PAYLOAD_LENGTH
    transmitted = struct.unpack_from("<H", packet, payload_end)[0]

    assert transmitted == crc16(packet[CRC_SPAN_START:payload_end])

    # And is NOT any of the plausible-but-wrong spans.
    assert transmitted != crc16(packet[:payload_end])
    assert transmitted != crc16(packet[HEADER_LENGTH:payload_end])


def test_crc_is_transmitted_little_endian():
    packet = encode_frame(make_frame())

    payload_end = HEADER_LENGTH + SAMPLE_PAYLOAD_LENGTH
    expected = crc16(packet[CRC_SPAN_START:payload_end])

    assert packet[payload_end] == expected & 0xFF
    assert packet[payload_end + 1] == (expected >> 8) & 0xFF


def test_history_packet_sizes():
    assert len(encode_history_begin(60)) == 10
    assert len(encode_history_record(make_frame())) == 56
    assert len(encode_history_end()) == 8


def test_oversized_payload_is_refused():
    with pytest.raises(ValueError):
        encode_packet(PacketType.FRAME, b"\x00" * 49)


# --------------------------------------------------------------------------
# Round trip
# --------------------------------------------------------------------------


def test_round_trip_preserves_every_field():
    frame = make_frame()

    packets = PacketDecoder().feed(encode_frame(frame))

    assert len(packets) == 1
    assert packets[0].type is PacketType.FRAME
    assert_frames_equal(packets[0].frame, frame)


@pytest.mark.parametrize(
    "state,warning,emergency,anomaly",
    [
        (ReactorState.NORMAL, False, False, False),
        (ReactorState.WARNING, True, False, False),
        (ReactorState.EMERGENCY, True, True, False),
        (ReactorState.EMERGENCY, False, True, False),
        (ReactorState.ANOMALY, False, False, True),
    ],
)
def test_round_trip_every_reactor_state(state, warning, emergency, anomaly):
    """
    Includes ANOMALY, which the current firmware can never emit.

    It is tested anyway because the flag starts arriving with no protocol
    change on the day ENABLE_XGBOOST_INFERENCE is switched on, and that must
    not be the day we discover the Pi mishandles it.
    """
    frame = make_frame(
        state=state, warning=warning, emergency=emergency, anomaly=anomaly
    )

    decoder = PacketDecoder()
    packets = decoder.feed(encode_frame(frame))

    assert len(packets) == 1
    assert packets[0].frame.state is state
    assert decoder.stats.state_mismatches == 0


def test_round_trip_extreme_values():
    """Emergency-range values and a full-scale device clock."""
    frame = make_frame(
        device_uptime_ms=0xFFFF_FFFF,
        pressure_kpa=185.75,
        pressure_rate_kpa_s=-12.5,
        temperature_c=675.0,
        temperature_rate_c_s=15.25,
        co=1500.0,
        co2=9000.0,
        ch4=6000.0,
        gas_ratio=0.6666667,
        gas_concentration=16500.0,
        anomaly_score=1.0,
        warning=True,
        emergency=True,
        anomaly=False,
        state=ReactorState.EMERGENCY,
    )

    packets = PacketDecoder().feed(encode_frame(frame))

    assert_frames_equal(packets[0].frame, frame)


def test_round_trip_zeroed_frame():
    """The firmware's default-constructed state, and the boot frame."""
    frame = make_frame(
        device_uptime_ms=0,
        pressure_kpa=0.0,
        pressure_rate_kpa_s=0.0,
        co=0.0,
        co2=0.0,
        ch4=0.0,
        gas_ratio=0.0,
        gas_concentration=0.0,
        anomaly_score=0.0,
    )

    packets = PacketDecoder().feed(encode_frame(frame))

    assert_frames_equal(packets[0].frame, frame)


def test_history_burst_round_trip():
    """A full HISTORY_BEGIN / RECORD x N / HISTORY_END burst."""
    frames = [make_frame(device_uptime_ms=1000 * index) for index in range(60)]

    stream = encode_history_begin(len(frames))
    for frame in frames:
        stream += encode_history_record(frame)
    stream += encode_history_end()

    packets = PacketDecoder().feed(stream)

    assert len(packets) == 62

    assert packets[0].type is PacketType.HISTORY_BEGIN
    assert packets[0].record_count == 60

    assert packets[-1].type is PacketType.HISTORY_END
    assert packets[-1].frame is None

    for index, packet in enumerate(packets[1:-1]):
        assert packet.type is PacketType.HISTORY_RECORD
        assert_frames_equal(packet.frame, frames[index])


def test_decoded_frames_carry_only_wire_data():
    """
    The decoder invents nothing.

    TelemetryFrame carried a host-stamped `received_at` until build step 4;
    it was removed so that a frame is exactly the wire payload. Arrival time
    now lives in TransportMetadata, assigned by the store.

    The consequence worth locking down: two frames decoded from identical
    bytes are equal. Without that, round-trip, replay and regression
    comparisons all silently compare timestamps instead of telemetry.
    """
    packet = encode_frame(make_frame())

    first = PacketDecoder().feed(packet)[0].frame
    second = PacketDecoder().feed(packet)[0].frame

    assert first == second
    assert not hasattr(first, "received_at")


# --------------------------------------------------------------------------
# Recovery -- the group that earns its keep
# --------------------------------------------------------------------------


def test_packet_split_across_single_byte_reads():
    """The worst realistic fragmentation: one byte per read."""
    frame = make_frame()
    packet = encode_frame(frame)

    decoder = PacketDecoder()
    received = []

    for index in range(len(packet)):
        received.extend(decoder.feed(packet[index : index + 1]))

        # Nothing may be emitted until the very last byte arrives.
        if index < len(packet) - 1:
            assert received == []

    assert len(received) == 1
    assert_frames_equal(received[0].frame, frame)


def test_multiple_packets_in_one_read():
    frames = [make_frame(device_uptime_ms=index * 1000) for index in range(5)]

    stream = b"".join(encode_frame(frame) for frame in frames)

    packets = PacketDecoder().feed(stream)

    assert len(packets) == 5
    for index, packet in enumerate(packets):
        assert packet.frame.device_uptime_ms == index * 1000


def test_arbitrary_chunk_boundaries():
    """Every chunk size from 1 to 64 across a multi-packet stream."""
    frames = [make_frame(device_uptime_ms=index * 1000) for index in range(4)]
    stream = b"".join(encode_frame(frame) for frame in frames)

    for chunk_size in range(1, 65):
        decoder = PacketDecoder()
        received = []

        for offset in range(0, len(stream), chunk_size):
            received.extend(decoder.feed(stream[offset : offset + chunk_size]))

        assert len(received) == 4, f"chunk_size={chunk_size}"
        assert decoder.stats.error_count == 0
        assert decoder.pending_bytes == 0


def test_leading_garbage_is_skipped():
    """A receiver starting mid-stream must find the next packet."""
    frame = make_frame()

    decoder = PacketDecoder()
    packets = decoder.feed(b"\x01\x02\x03garbage\xff" + encode_frame(frame))

    assert len(packets) == 1
    assert_frames_equal(packets[0].frame, frame)
    assert decoder.stats.discarded_bytes == 11


def test_corrupted_crc_is_rejected_and_next_packet_survives():
    good = encode_frame(make_frame(device_uptime_ms=1000))

    corrupted = bytearray(encode_frame(make_frame(device_uptime_ms=2000)))
    corrupted[20] ^= 0xFF  # flip a bit inside the payload

    decoder = PacketDecoder()
    packets = decoder.feed(bytes(corrupted) + good)

    assert decoder.stats.crc_errors == 1
    assert len(packets) == 1
    assert packets[0].frame.device_uptime_ms == 1000


def test_corruption_anywhere_in_packet_is_caught():
    """Sweep a bit flip across every byte and confirm none decodes silently."""
    original = encode_frame(make_frame())

    for index in range(len(original)):
        corrupted = bytearray(original)
        corrupted[index] ^= 0x01

        decoder = PacketDecoder()
        packets = decoder.feed(bytes(corrupted))

        if packets:
            # Only acceptable outcome: the byte that flipped was a status
            # flag or another field the CRC still validates -- impossible,
            # since the CRC covers offsets 2..53. Offsets 0/1 are sync bytes.
            assert index < CRC_SPAN_START, (
                f"byte {index} was corrupted but the packet still decoded"
            )


def test_unknown_protocol_version_is_rejected():
    packet = bytearray(encode_frame(make_frame()))
    packet[2] = 99

    decoder = PacketDecoder()
    packets = decoder.feed(bytes(packet))

    assert packets == []
    assert decoder.stats.version_rejects == 1


def test_version_reject_does_not_swallow_following_packet():
    """A firmware-skew packet must not take a good packet down with it."""
    skewed = bytearray(encode_frame(make_frame(device_uptime_ms=1000)))
    skewed[2] = 2  # a future protocol version

    good = encode_frame(make_frame(device_uptime_ms=2000))

    decoder = PacketDecoder()
    packets = decoder.feed(bytes(skewed) + good)

    assert decoder.stats.version_rejects == 1
    assert len(packets) == 1
    assert packets[0].frame.device_uptime_ms == 2000


def test_unknown_packet_type_is_rejected():
    packet = bytearray(encode_frame(make_frame()))
    packet[3] = 0x7F

    decoder = PacketDecoder()

    assert decoder.feed(bytes(packet)) == []
    assert decoder.stats.type_errors == 1


def test_impossible_payload_length_does_not_stall_the_decoder():
    """
    A corrupt length field is the classic denial-of-progress bug: the decoder
    sits forever waiting for bytes that will never arrive.
    """
    packet = bytearray(encode_frame(make_frame()))
    struct.pack_into("<H", packet, 4, 60000)

    good = encode_frame(make_frame(device_uptime_ms=7777))

    decoder = PacketDecoder()
    packets = decoder.feed(bytes(packet) + good)

    assert decoder.stats.length_errors == 1
    assert len(packets) == 1
    assert packets[0].frame.device_uptime_ms == 7777


def test_crc_valid_packet_with_wrong_payload_length_is_reported():
    """
    A well-formed packet whose payload size does not match its type means the
    firmware layout changed without a version bump. Distinct from corruption,
    and counted as such.
    """
    decoder = PacketDecoder()

    assert decoder.feed(encode_packet(PacketType.FRAME, b"\x00" * 10)) == []
    assert decoder.stats.length_errors == 1
    assert decoder.stats.crc_errors == 0


def test_false_sync_word_does_not_consume_the_real_packet():
    """
    0xAA 0x55 can occur inside float payload data by chance. Skipping forward
    by a claimed length -- rather than by one byte -- would swallow the
    genuine packet that follows.
    """
    frame = make_frame(device_uptime_ms=4242)

    decoder = PacketDecoder()
    packets = decoder.feed(b"\xaa\x55\xff\xff\xff\xff" + encode_frame(frame))

    assert len(packets) == 1
    assert packets[0].frame.device_uptime_ms == 4242


def test_bare_sync_word_before_real_packet():
    frame = make_frame(device_uptime_ms=999)

    decoder = PacketDecoder()
    packets = decoder.feed(b"\xaa\x55" + encode_frame(frame))

    assert len(packets) == 1
    assert packets[0].frame.device_uptime_ms == 999


def test_sync_word_split_across_reads_is_not_lost():
    """A trailing 0xAA must be retained -- its 0x55 may be in the next read."""
    packet = encode_frame(make_frame())

    decoder = PacketDecoder()

    assert decoder.feed(b"\x00\x01" + packet[:1]) == []
    assert decoder.pending_bytes == 1

    packets = decoder.feed(packet[1:])

    assert len(packets) == 1


def test_truncated_packet_leaves_decoder_waiting_not_broken():
    packet = encode_frame(make_frame())

    decoder = PacketDecoder()

    assert decoder.feed(packet[:30]) == []
    assert decoder.stats.error_count == 0
    assert decoder.pending_bytes == 30

    assert len(decoder.feed(packet[30:])) == 1
    assert decoder.pending_bytes == 0


def test_pure_garbage_does_not_grow_the_buffer_without_bound():
    """
    A disconnected or mis-wired line delivers noise indefinitely. The decoder
    must not accumulate it.
    """
    decoder = PacketDecoder()

    for _ in range(200):
        decoder.feed(bytes(range(256)))

    assert decoder.pending_bytes <= MAX_PACKET_LENGTH


def test_reset_drops_buffered_bytes_but_keeps_counters():
    """On reconnect, mid-flight bytes belong to the previous link."""
    packet = encode_frame(make_frame())

    decoder = PacketDecoder()
    decoder.feed(packet[:30])

    assert decoder.pending_bytes == 30

    decoder.reset()

    assert decoder.pending_bytes == 0
    assert decoder.stats.bytes_received == 30

    # The stale tail must not be stitched onto the new connection.
    assert decoder.feed(packet[30:]) == []


# --------------------------------------------------------------------------
# Semantic validation
# --------------------------------------------------------------------------


def test_invalid_state_byte_is_rejected():
    packet = bytearray(encode_frame(make_frame()))
    packet[HEADER_LENGTH + 47] = 9  # outside ReactorState

    # Recompute the CRC so this tests state validation, not corruption.
    payload_end = HEADER_LENGTH + SAMPLE_PAYLOAD_LENGTH
    struct.pack_into("<H", packet, payload_end, crc16(packet[CRC_SPAN_START:payload_end]))

    decoder = PacketDecoder()

    assert decoder.feed(bytes(packet)) == []
    assert decoder.stats.state_errors == 1
    assert decoder.stats.crc_errors == 0


def test_state_inconsistent_with_flags_is_counted_but_frame_still_delivered():
    """
    The transmitted state is authoritative -- the Pi does not overrule the
    reactor. But a disagreement with the firmware's own fusion policy means
    something is wrong, and is surfaced rather than hidden.
    """
    frame = make_frame(warning=True, emergency=False, state=ReactorState.NORMAL)

    decoder = PacketDecoder()
    packets = decoder.feed(encode_frame(frame))

    assert len(packets) == 1
    assert packets[0].frame.state is ReactorState.NORMAL
    assert decoder.stats.state_mismatches == 1


def test_reactor_state_derivation_matches_firmware_fusion_policy():
    """DecisionEngine.cpp:60-87 -- deterministic limits outrank the model."""
    derive = ReactorState.derive

    assert derive(False, False, False) is ReactorState.NORMAL
    assert derive(True, False, False) is ReactorState.WARNING
    assert derive(False, True, False) is ReactorState.EMERGENCY
    assert derive(False, False, True) is ReactorState.ANOMALY

    # Emergency outranks everything.
    assert derive(True, True, True) is ReactorState.EMERGENCY

    # Warning outranks the model.
    assert derive(True, False, True) is ReactorState.WARNING


# --------------------------------------------------------------------------
# Counters -- these drive the operator's diagnostics strip
# --------------------------------------------------------------------------


def test_counters_track_a_realistic_mixed_stream():
    good_a = encode_frame(make_frame(device_uptime_ms=1000))
    good_b = encode_frame(make_frame(device_uptime_ms=2000))

    bad_crc = bytearray(encode_frame(make_frame()))
    bad_crc[25] ^= 0xFF

    bad_version = bytearray(encode_frame(make_frame()))
    bad_version[2] = 42

    stream = good_a + bytes(bad_crc) + b"\x00\x01\x02" + bytes(bad_version) + good_b

    decoder = PacketDecoder()
    packets = decoder.feed(stream)

    assert len(packets) == 2
    assert decoder.stats.frames_decoded == 2
    assert decoder.stats.packets_decoded == 2
    assert decoder.stats.crc_errors == 1
    assert decoder.stats.version_rejects == 1
    assert decoder.stats.error_count == 2
    assert decoder.stats.bytes_received == len(stream)


def test_non_frame_packets_count_as_packets_but_not_frames():
    decoder = PacketDecoder()
    decoder.feed(encode_history_begin(5) + encode_history_end())

    assert decoder.stats.packets_decoded == 2
    assert decoder.stats.frames_decoded == 0
