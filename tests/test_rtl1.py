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


def test_decoder_round_trip_across_split_reads() -> None:
    """Test frames split at every byte boundary still decode."""
    blob = rtl1.encode_frame(7, rtl1.TYPE_DATA, 0, b'hello') + rtl1.encode_frame(7, rtl1.TYPE_RESPONSE, 1, b'x')
    decoder = rtl1.Decoder()
    frames = []
    for index in range(len(blob)):
        frames += decoder.feed(blob[index : index + 1])
    assert [(frame.kind, frame.seq, frame.payload, frame.crc_ok) for frame in frames] == [
        (rtl1.TYPE_DATA, 0, b'hello', True),
        (rtl1.TYPE_RESPONSE, 1, b'x', True),
    ]


def test_decoder_skips_noise_and_flags_bad_crc() -> None:
    """Test leading garbage is skipped and a corrupted CRC is reported, not hidden."""
    frame = bytearray(rtl1.encode_frame(1, rtl1.TYPE_DATA, 0, b'data'))
    frame[-1] ^= 0xFF
    frames = rtl1.Decoder().feed(b'garbage\xa5' + bytes(frame))
    assert len(frames) == 1
    assert frames[0].crc_ok is False


def test_decoder_resyncs_past_impossible_length() -> None:
    """Test a header claiming more than 2048 bytes is treated as noise."""
    bogus = rtl1.MAGIC + b'\x00\x00\xff\xff\x03\x00'
    frames = rtl1.Decoder().feed(bogus + rtl1.encode_frame(2, rtl1.TYPE_DATA, 0, b'ok'))
    assert [frame.payload for frame in frames] == [b'ok']


def test_decoder_keeps_a_trailing_half_magic() -> None:
    """Test a buffer ending in 0xA5 keeps it for the next read."""
    decoder = rtl1.Decoder()
    frame = rtl1.encode_frame(3, rtl1.TYPE_DATA, 0, b'z')
    assert not decoder.feed(b'noise' + frame[:1])
    assert [found.payload for found in decoder.feed(frame[1:])] == [b'z']
