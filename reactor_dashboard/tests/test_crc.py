"""
CRC-16 conformance.

The CRC is where a protocol bug hides longest, because a wrong implementation
still produces plausible-looking 16-bit numbers. These tests pin it against
the published Modbus check value and against an independent transcription of
the firmware's bit-by-bit loop.
"""

from backend.protocol.crc import INITIAL_VALUE, POLYNOMIAL, crc16


def reference_crc16(data: bytes) -> int:
    """
    Line-for-line transcription of ModbusCRC::calculate (ModbusCRC.cpp:20-34).

    Deliberately unoptimised and written independently of crc.py's table, so
    that agreement between the two means something.
    """
    crc = 0xFFFF

    for byte in data:
        crc ^= byte

        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc = crc >> 1

    return crc


def test_modbus_check_value():
    """The standard CRC-16/MODBUS check value for b"123456789"."""
    assert crc16(b"123456789") == 0x4B37


def test_empty_input_returns_initial_value():
    assert crc16(b"") == INITIAL_VALUE


def test_parameters_match_firmware():
    assert POLYNOMIAL == 0xA001
    assert INITIAL_VALUE == 0xFFFF


def test_table_matches_bitwise_reference():
    """Table-driven and bit-by-bit must agree across the whole byte range."""
    for value in range(256):
        payload = bytes((value,))
        assert crc16(payload) == reference_crc16(payload)


def test_matches_reference_on_realistic_payloads():
    for length in (1, 2, 6, 47, 48, 54, 56, 255):
        payload = bytes((index * 7 + 13) & 0xFF for index in range(length))
        assert crc16(payload) == reference_crc16(payload)


def test_result_is_always_16_bit():
    for length in range(0, 64):
        payload = bytes(range(length))
        assert 0 <= crc16(payload) <= 0xFFFF


def test_single_bit_flip_changes_crc():
    """A CRC that misses single-bit errors would be worse than none at all."""
    original = bytes(range(48))

    baseline = crc16(original)

    for index in range(len(original)):
        for bit in range(8):
            corrupted = bytearray(original)
            corrupted[index] ^= 1 << bit

            assert crc16(corrupted) != baseline
