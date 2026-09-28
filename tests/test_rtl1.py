"""Tests for RTL1 framing."""

from __future__ import annotations

import pytest

from party_line import rtl1


def test_crc16_check_value() -> None:
    """Test the CRC matches the CRC-16/CCITT-FALSE check value."""
    assert rtl1.crc16(b'123456789') == 0x29B1


def test_crc16_of_nothing_is_the_initial_value() -> None:
    """Test an empty input returns 0xFFFF."""
    assert rtl1.crc16(b'') == 0xFFFF


def test_encode_frame_layout() -> None:
    """Test the frame is magic, little-endian header, payload, little-endian CRC."""
    frame = rtl1.encode_frame(0x1234, rtl1.TYPE_ABORT, 0x105, b'ab')
    assert frame[:8] == bytes([0xA5, 0x5A, 0x34, 0x12, 0x02, 0x00, 0x06, 0x05])
    assert frame[8:10] == b'ab'
    assert int.from_bytes(frame[10:], 'little') == rtl1.crc16(frame[2:10])


def test_encode_frame_refuses_oversize_payload() -> None:
    """Test a payload over 2048 bytes is refused."""
    with pytest.raises(ValueError):
        rtl1.encode_frame(1, rtl1.TYPE_DATA, 0, bytes(rtl1.MAX_PAYLOAD + 1))
