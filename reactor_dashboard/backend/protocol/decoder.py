"""
Byte stream -> TelemetryFrame.

A UART delivers an unstructured byte stream. A packet may arrive split across
a dozen reads, several packets may arrive in one read, and after a cable
glitch the stream may resume mid-packet. PacketDecoder absorbs all of that and
emits only whole, version-checked, CRC-valid packets.

It is deliberately transport-agnostic: it holds no serial port, no thread and
no timer, and is driven purely by feed(). That is what lets the simulator and
the real UART share it, and what makes it testable without hardware.

Responsibility boundary
-----------------------
This class decodes faithfully. It does NOT judge plausibility -- it will
happily emit a frame reporting 900 kPa if that is genuinely what arrived over
a CRC-valid packet. Deciding that a value is implausible, stale or physically
impossible is analytics' job, and keeping that out of here means the decoder
can never silently alter what the reactor actually said.

Recovery policy
---------------
On any failure the decoder discards ONE byte and hunts for the next sync word,
rather than skipping the whole suspect packet. A false 0xAA 0x55 occurring
inside float payload data is entirely possible, and skipping forward by a
claimed length would swallow the real packet that follows it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.protocol.crc import crc16
from backend.protocol.spec import (
    CRC_SPAN_START,
    CRC_STRUCT,
    HEADER_LENGTH,
    HEADER_STRUCT,
    HISTORY_BEGIN_PAYLOAD_LENGTH,
    MAX_PAYLOAD_LENGTH,
    PROTOCOL_VERSION,
    RECORD_COUNT_STRUCT,
    SAMPLE_PACKET_TYPES,
    SAMPLE_PAYLOAD_LENGTH,
    SAMPLE_STRUCT,
    SYNC_0,
    SYNC_WORD,
    PacketType,
    ReactorState,
)
from backend.telemetry_frame import TelemetryFrame

_SYNC_0_BYTE = bytes((SYNC_0,))

#: Sentinel: the buffer holds no complete packet, so stop and wait for bytes.
_NEED_MORE = object()

#: Expected payload size per packet type. Checked AFTER the CRC passes, so a
#: CRC-valid packet of unexpected size is reported as firmware/protocol skew
#: rather than misfiled as line corruption.
_EXPECTED_PAYLOAD_LENGTH = {
    PacketType.FRAME: SAMPLE_PAYLOAD_LENGTH,
    PacketType.HISTORY_RECORD: SAMPLE_PAYLOAD_LENGTH,
    PacketType.HISTORY_BEGIN: HISTORY_BEGIN_PAYLOAD_LENGTH,
    PacketType.HISTORY_END: 0,
}


@dataclass(frozen=True, slots=True)
class DecodedPacket:
    """
    One successfully decoded packet.

    `frame` is populated for FRAME and HISTORY_RECORD; `record_count` for
    HISTORY_BEGIN. HISTORY_END carries neither.
    """

    type: PacketType
    frame: TelemetryFrame | None = None
    record_count: int | None = None


@dataclass(slots=True)
class DecoderStats:
    """
    Link health counters, surfaced in the dashboard's diagnostics strip.

    An operator must never have to open a menu to find out whether the number
    in front of them is live, so every one of these is displayed.
    """

    bytes_received: int = 0
    packets_decoded: int = 0
    frames_decoded: int = 0

    #: Packet failed the Modbus CRC. The usual symptom of a marginal cable.
    crc_errors: int = 0

    #: protocolVersion was not 1. Means the Arduino is running firmware this
    #: build does not understand -- a loud, specific failure, not a parse error.
    version_rejects: int = 0

    #: payloadLength was impossible, or disagreed with the packet type.
    length_errors: int = 0

    #: packetType was not a known value.
    type_errors: int = 0

    #: state byte outside the ReactorState enum. Packet dropped.
    state_errors: int = 0

    #: Transmitted state disagreed with the firmware's own fusion policy
    #: applied to the transmitted flags. Frame is still delivered (the wire
    #: value is authoritative) but the disagreement is reported.
    state_mismatches: int = 0

    #: Times the decoder had to hunt for a new sync word.
    resyncs: int = 0

    #: Bytes thrown away while resynchronising.
    discarded_bytes: int = 0

    @property
    def error_count(self) -> int:
        """Total malformed-packet events. The headline number for the HMI."""
        return (
            self.crc_errors
            + self.version_rejects
            + self.length_errors
            + self.type_errors
            + self.state_errors
        )


class PacketDecoder:
    """Stateful, incremental decoder for the v1 telemetry protocol."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._stats = DecoderStats()

    @property
    def stats(self) -> DecoderStats:
        return self._stats

    @property
    def pending_bytes(self) -> int:
        """Bytes buffered awaiting more data. Useful when diagnosing a stall."""
        return len(self._buffer)

    def reset(self) -> None:
        """
        Drop buffered bytes without clearing counters.

        Called when the link is reopened: whatever was mid-flight belongs to
        the previous connection and must not be stitched onto the new one.
        """
        self._buffer.clear()

    def feed(self, data: bytes) -> list[DecodedPacket]:
        """
        Absorb newly received bytes and return every packet now complete.

        Returns an empty list when the data did not complete a packet. Never
        raises on malformed input -- malformed input is expected, counted in
        `stats`, and recovered from.
        """
        self._stats.bytes_received += len(data)
        self._buffer += data

        packets: list[DecodedPacket] = []

        while True:
            result = self._extract_one()

            if result is _NEED_MORE:
                break

            if result is not None:
                packets.append(result)

        return packets

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _extract_one(self):
        """
        Attempt to consume one packet from the head of the buffer.

        Returns a DecodedPacket on success, None when a malformed packet was
        discarded (caller should retry -- progress was made), or _NEED_MORE
        when the buffer is exhausted.
        """
        buffer = self._buffer

        # -- 1. Acquire frame alignment ---------------------------------

        index = buffer.find(SYNC_WORD)

        if index == -1:
            # Retain a trailing 0xAA: it may be the first half of a sync word
            # whose second half is still in flight. Dropping it would lose the
            # packet that follows.
            keep = 1 if buffer.endswith(_SYNC_0_BYTE) else 0

            self._discard(len(buffer) - keep)

            return _NEED_MORE

        if index > 0:
            self._discard(index)

        # -- 2. Header --------------------------------------------------

        if len(buffer) < HEADER_LENGTH:
            return _NEED_MORE

        _, _, version, type_value, payload_length = HEADER_STRUCT.unpack_from(
            buffer, 0
        )

        if version != PROTOCOL_VERSION:
            self._stats.version_rejects += 1
            self._resync()
            return None

        try:
            packet_type = PacketType(type_value)
        except ValueError:
            self._stats.type_errors += 1
            self._resync()
            return None

        # Bound the wait before it starts. A corrupt length field is the
        # classic way to stall a decoder forever on bytes that never come.
        if payload_length > MAX_PAYLOAD_LENGTH:
            self._stats.length_errors += 1
            self._resync()
            return None

        # -- 3. Whole packet present? -----------------------------------

        total_length = HEADER_LENGTH + payload_length + CRC_STRUCT.size

        if len(buffer) < total_length:
            return _NEED_MORE

        # -- 4. Integrity -----------------------------------------------

        payload_end = HEADER_LENGTH + payload_length

        (received_crc,) = CRC_STRUCT.unpack_from(buffer, payload_end)

        # Sync bytes excluded -- see spec.CRC_SPAN_START.
        calculated_crc = crc16(memoryview(buffer)[CRC_SPAN_START:payload_end])

        if received_crc != calculated_crc:
            self._stats.crc_errors += 1
            self._resync()
            return None

        # The packet is authentic from here on. Anything still wrong with it
        # is a firmware/protocol disagreement, not line noise.

        if payload_length != _EXPECTED_PAYLOAD_LENGTH[packet_type]:
            self._stats.length_errors += 1
            self._consume(total_length)
            return None

        payload = bytes(buffer[HEADER_LENGTH:payload_end])

        self._consume(total_length)

        # -- 5. Interpret -----------------------------------------------

        packet = self._build_packet(packet_type, payload)

        if packet is not None:
            self._stats.packets_decoded += 1

            if packet.frame is not None:
                self._stats.frames_decoded += 1

        return packet

    def _build_packet(
        self, packet_type: PacketType, payload: bytes
    ) -> DecodedPacket | None:
        if packet_type in SAMPLE_PACKET_TYPES:
            frame = self._parse_sample(payload)

            if frame is None:
                return None

            return DecodedPacket(type=packet_type, frame=frame)

        if packet_type is PacketType.HISTORY_BEGIN:
            (record_count,) = RECORD_COUNT_STRUCT.unpack(payload)

            return DecodedPacket(type=packet_type, record_count=record_count)

        return DecodedPacket(type=packet_type)

    def _parse_sample(self, payload: bytes) -> TelemetryFrame | None:
        """Decode the 48-byte payload shared by FRAME and HISTORY_RECORD."""
        (
            device_uptime_ms,
            pressure,
            pressure_rate,
            temperature,
            temperature_rate,
            co,
            co2,
            ch4,
            gas_ratio,
            gas_concentration,
            anomaly_score,
            warning,
            emergency,
            anomaly,
            state_value,
        ) = SAMPLE_STRUCT.unpack(payload)

        try:
            state = ReactorState(state_value)
        except ValueError:
            self._stats.state_errors += 1
            return None

        warning = bool(warning)
        emergency = bool(emergency)
        anomaly = bool(anomaly)

        # Self-check: `state` is fully derivable from the three flags via the
        # firmware's fusion policy. A disagreement means the mirrored logic has
        # drifted from firmware, or a corruption slipped past the CRC. The wire
        # value wins; the disagreement is counted and shown.
        if ReactorState.derive(warning, emergency, anomaly) is not state:
            self._stats.state_mismatches += 1

        return TelemetryFrame(
            device_uptime_ms=device_uptime_ms,
            pressure_kpa=pressure,
            pressure_rate_kpa_s=pressure_rate,
            temperature_c=temperature,
            temperature_rate_c_s=temperature_rate,
            co=co,
            co2=co2,
            ch4=ch4,
            gas_ratio=gas_ratio,
            gas_concentration=gas_concentration,
            anomaly_score=anomaly_score,
            warning=warning,
            emergency=emergency,
            anomaly=anomaly,
            state=state,
        )

    def _consume(self, count: int) -> None:
        """Remove a successfully handled packet from the head of the buffer."""
        del self._buffer[:count]

    def _discard(self, count: int) -> None:
        """Throw away `count` unusable leading bytes."""
        if count <= 0:
            return

        del self._buffer[:count]

        self._stats.discarded_bytes += count
        self._stats.resyncs += 1

    def _resync(self) -> None:
        """
        Step past a rejected sync word.

        Exactly one byte, so that a real packet immediately following a false
        sync match is still found.
        """
        self._discard(1)
