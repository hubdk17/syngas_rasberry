"""
The Arduino telemetry protocol, version 1.

This package is the contract with the firmware at D:\\Desktop\\syngas_arduino.
It is the only part of the application permitted to know a byte offset, a sync
word or a CRC polynomial; everything above it speaks in TelemetryFrame.

See docs/PROTOCOL.md for the derivation and its firmware citations.
"""

from backend.protocol.crc import crc16
from backend.protocol.decoder import DecodedPacket, DecoderStats, PacketDecoder
from backend.protocol.encoder import (
    encode_frame,
    encode_history_begin,
    encode_history_end,
    encode_history_record,
    encode_packet,
)
from backend.protocol.spec import PROTOCOL_VERSION, PacketType, ReactorState

__all__ = [
    "PROTOCOL_VERSION",
    "DecodedPacket",
    "DecoderStats",
    "PacketDecoder",
    "PacketType",
    "ReactorState",
    "crc16",
    "encode_frame",
    "encode_history_begin",
    "encode_history_end",
    "encode_history_record",
    "encode_packet",
]
