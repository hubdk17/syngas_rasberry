"""
Wire-format constants for the reactor telemetry protocol, version 1.

This module is a transcription of the Arduino firmware. Nothing here is a
design decision made on the Raspberry Pi side -- every value has a firmware
source, cited inline. See docs/PROTOCOL.md for the full contract.

Source of truth: D:\\Desktop\\syngas_arduino

This is the ONLY module in the project permitted to know a byte offset.
"""

from __future__ import annotations

import struct
from enum import IntEnum

# --------------------------------------------------------------------------
# Protocol identity
#
# The version travels in every packet so the Pi can reject firmware it does
# not understand rather than silently misparsing it.
#
# config/communication_config.h:54-57
# --------------------------------------------------------------------------

SYNC_0 = 0xAA
SYNC_1 = 0x55
SYNC_WORD = bytes((SYNC_0, SYNC_1))

PROTOCOL_VERSION = 1


# --------------------------------------------------------------------------
# Physical link
#
# config/communication_config.h:23, config/hardware_config.h:29
# --------------------------------------------------------------------------

BAUD_RATE = 115200

#: Live frame cadence. config/timing_config.h:63
TELEMETRY_PERIOD_MS = 1000

#: Arduino-side history depth: 60 samples at 1 Hz. CircularBuffer.h:69-71
HISTORY_CAPACITY = 60


# --------------------------------------------------------------------------
# Framing geometry
#
# UARTTelemetry.h:84-91
# --------------------------------------------------------------------------

HEADER_LENGTH = 6
CRC_LENGTH = 2
SAMPLE_PAYLOAD_LENGTH = 48

#: Largest payload the firmware can emit. Used to reject a corrupt length
#: field before it can stall the decoder waiting for bytes that never come.
MAX_PAYLOAD_LENGTH = SAMPLE_PAYLOAD_LENGTH

MAX_PACKET_LENGTH = HEADER_LENGTH + MAX_PAYLOAD_LENGTH + CRC_LENGTH

#: The CRC covers everything after the sync bytes -- version, type, length and
#: payload -- so a receiver re-synchronising mid-stream can validate a
#: candidate packet without rescanning the sync bytes it just consumed.
#:
#: UARTTelemetry.cpp:20, UARTTelemetry.cpp:164-168
CRC_SPAN_START = 2


class PacketType(IntEnum):
    """Wire values. Do not renumber. UARTTelemetry.h:72-78"""

    FRAME = 0x01
    HISTORY_BEGIN = 0x02
    HISTORY_RECORD = 0x03
    HISTORY_END = 0x04


#: Packet types whose payload is a 48-byte sample. FRAME and HISTORY_RECORD
#: share an identical layout, so one parser covers both.
#: UARTTelemetry.h:51-52
SAMPLE_PACKET_TYPES = frozenset({PacketType.FRAME, PacketType.HISTORY_RECORD})

#: HISTORY_BEGIN carries a uint16 record count. UARTTelemetry.cpp:194-195
HISTORY_BEGIN_PAYLOAD_LENGTH = 2


# --------------------------------------------------------------------------
# Struct layouts
#
# The firmware writes every field byte by byte rather than copying a struct,
# so the layout depends on neither compiler padding nor target alignment
# (UARTTelemetry.h:26-28). The '<' prefix therefore maps 1:1 onto the wire:
# little endian, no padding.
# --------------------------------------------------------------------------

#: sync0, sync1, version, packet type, payload length
HEADER_STRUCT = struct.Struct("<BBBBH")

#: uint32 timestamp, 10 float32 features, 4 uint8 status bytes.
#: Field order fixed by UARTTelemetry::writeSamplePayload (UARTTelemetry.cpp:91-122).
SAMPLE_STRUCT = struct.Struct("<I10f4B")

CRC_STRUCT = struct.Struct("<H")

RECORD_COUNT_STRUCT = struct.Struct("<H")

# These are contract violations, not style issues: if the firmware layout ever
# drifts from this transcription, fail at import rather than at 3 a.m.
assert HEADER_STRUCT.size == HEADER_LENGTH
assert SAMPLE_STRUCT.size == SAMPLE_PAYLOAD_LENGTH
assert CRC_STRUCT.size == CRC_LENGTH


# --------------------------------------------------------------------------
# Reactor state
#
# DecisionEngine.h:42-48
# --------------------------------------------------------------------------


class ReactorState(IntEnum):
    """
    The reactor's own verdict, as decided by the Arduino DecisionEngine.

    NORMAL     No limit breached, no anomaly.
    WARNING    At least one warning threshold breached.
    EMERGENCY  Pressure or temperature past its emergency limit.
    ANOMALY    Model flagged an anomaly with no fixed limit breached.

    ANOMALY is currently unreachable: ENABLE_XGBOOST_INFERENCE is disabled in
    the firmware while the forest is a placeholder, so DecisionEngine forces
    the anomaly flag false (DecisionEngine.cpp:50-56). It is handled here
    regardless, because the flag starts arriving with no protocol change on
    the day that toggle is enabled.
    """

    NORMAL = 0
    WARNING = 1
    EMERGENCY = 2
    ANOMALY = 3

    @property
    def label(self) -> str:
        return self.name

    @staticmethod
    def derive(warning: bool, emergency: bool, anomaly: bool) -> "ReactorState":
        """
        Reproduce the firmware's fusion policy (DecisionEngine.cpp:60-87).

        Deterministic safety limits outrank the model: a hard limit breach is
        auditable and always actionable, whereas the model is probabilistic.
        ANOMALY therefore only surfaces when no fixed limit is breached.

        Used as a decoder self-check. The transmitted state is authoritative;
        a disagreement means either a corrupt-but-CRC-valid frame or a
        firmware/protocol skew, and is reported rather than hidden.
        """
        if emergency:
            return ReactorState.EMERGENCY
        if warning:
            return ReactorState.WARNING
        if anomaly:
            return ReactorState.ANOMALY
        return ReactorState.NORMAL


# --------------------------------------------------------------------------
# Device clock
# --------------------------------------------------------------------------

#: The firmware timestamp is millis() since boot, not wall clock, and wraps
#: at 2**32 ms (~49.7 days). PhysicsEngine.cpp:109
DEVICE_CLOCK_MODULUS = 1 << 32
