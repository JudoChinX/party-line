"""Tests for RTL1 framing."""

from __future__ import annotations

from party_line import rtl1


def test_crc16_check_value() -> None:
    """Test the CRC matches the CRC-16/CCITT-FALSE check value."""
    assert rtl1.crc16(b'123456789') == 0x29B1


def test_crc16_of_nothing_is_the_initial_value() -> None:
    """Test an empty input returns 0xFFFF."""
    assert rtl1.crc16(b'') == 0xFFFF
