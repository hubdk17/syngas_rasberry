"""
Modbus CRC-16, as used by the reactor telemetry link.

The firmware reuses the CRC already present on the RS485 sensor bus rather
than carrying a second implementation (UARTTelemetry.cpp:160-161), so this is
a transcription of ModbusCRC::calculate (ModbusCRC.cpp:12-35).

    Width         16 bits
    Polynomial    0xA001  (reflected form of 0x8005)
    Initial       0xFFFF
    Final XOR     0x0000
    Wire order    little endian (low byte first)

Known answer: crc16(b"123456789") == 0x4B37
"""

from __future__ import annotations

POLYNOMIAL = 0xA001
INITIAL_VALUE = 0xFFFF


def _build_table() -> tuple[int, ...]:
    """
    Precompute the byte-wise CRC table.

    Generated with the identical bit-by-bit loop the firmware runs, so the
    table is provably equivalent to the C++ implementation rather than a
    separately-sourced constant that has to be trusted.
    """
    table = []

    for value in range(256):
        crc = value

        for _ in range(8):
            crc = (crc >> 1) ^ POLYNOMIAL if crc & 0x0001 else crc >> 1

        table.append(crc)

    return tuple(table)


_TABLE = _build_table()


def crc16(data: bytes | bytearray | memoryview) -> int:
    """Return the Modbus CRC-16 of `data` as an int in [0, 0xFFFF]."""
    crc = INITIAL_VALUE

    for byte in data:
        crc = (crc >> 8) ^ _TABLE[(crc ^ byte) & 0xFF]

    return crc
