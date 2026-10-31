"""
TelemetryFrame -> bytes.

The Raspberry Pi never transmits telemetry, so this module exists for two
reasons, both of them about confidence in the decoder:

1. The simulator emits real protocol bytes through the real decoder. The
   component most likely to hide an off-by-one is therefore exercised
   continuously from day one, instead of being tested for the first time when
   the reactor is live.

2. Round-trip and corruption tests need a way to produce known-good packets.

It is a transcription of UARTTelemetry::transmit and
UARTTelemetry::writeSamplePayload (UARTTelemetry.cpp:91-177).
"""

from __future__ import annotations

from backend.protocol.crc import crc16
from backend.protocol.spec import (
    CRC_SPAN_START,
    CRC_STRUCT,
    HEADER_STRUCT,
    MAX_PAYLOAD_LENGTH,
    PROTOCOL_VERSION,
    RECORD_COUNT_STRUCT,
    SAMPLE_STRUCT,
    SYNC_0,
    SYNC_1,
    PacketType,
)
from backend.telemetry_frame import TelemetryFrame


def encode_packet(packet_type: PacketType, payload: bytes = b"") -> bytes:
    """
    Wrap `payload` in the v1 framing: header, payload, CRC.

    The CRC covers everything from CRC_SPAN_START to the end of the payload --
    the sync bytes are excluded. Getting this span wrong is the single most
    likely protocol bug, which is why it is expressed once, here, and once in
    the decoder, and why the tests check it against firmware-shaped packets.
    """
    if len(payload) > MAX_PAYLOAD_LENGTH:
        raise ValueError(
            f"payload of {len(payload)} bytes exceeds the "
            f"{MAX_PAYLOAD_LENGTH}-byte protocol maximum"
        )

    header = HEADER_STRUCT.pack(
        SYNC_0,
        SYNC_1,
        PROTOCOL_VERSION,
        int(packet_type),
        len(payload),
    )

    packet = bytearray(header)
    packet += payload

    checksum = crc16(packet[CRC_SPAN_START:])
    packet += CRC_STRUCT.pack(checksum)

    return bytes(packet)


def encode_sample_payload(frame: TelemetryFrame) -> bytes:
    """Serialise the 48-byte sample payload shared by FRAME and HISTORY_RECORD."""
    return SAMPLE_STRUCT.pack(*frame.as_wire_tuple())


def encode_frame(frame: TelemetryFrame) -> bytes:
    """One live sample, as the firmware sends it every 1000 ms."""
    return encode_packet(PacketType.FRAME, encode_sample_payload(frame))


def encode_history_record(frame: TelemetryFrame) -> bytes:
    """One archived sample. Identical payload to a FRAME, different type."""
    return encode_packet(PacketType.HISTORY_RECORD, encode_sample_payload(frame))


def encode_history_begin(record_count: int) -> bytes:
    """Opens a history burst, announcing how many records follow."""
    return encode_packet(
        PacketType.HISTORY_BEGIN, RECORD_COUNT_STRUCT.pack(record_count)
    )


def encode_history_end() -> bytes:
    """Closes a history burst. No payload."""
    return encode_packet(PacketType.HISTORY_END)
