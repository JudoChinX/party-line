"""RTL1, the RT4K's framed binary transfer format. Pure: no I/O.

A frame is: A5 5A | nonce lo hi | length lo hi | type | seq | payload | crc16 lo hi,
with CRC-16/CCITT-FALSE over nonce..payload.
"""

from __future__ import annotations

import binascii
from dataclasses import dataclass
from typing import Optional

CRC_INIT = 0xFFFF
CRC_LENGTH = 2
HEADER_LENGTH = 8
MAGIC = b'\xa5\x5a'
MAX_PAYLOAD = 2048
TYPE_ABORT = 6
TYPE_DATA = 3
TYPE_NAK = 5
TYPE_RESPONSE = 2


@dataclass(frozen=True)
class Frame:
    """One decoded frame.

    Attributes:
        nonce: The transfer's session number.
        kind: The frame type (TYPE_*).
        seq: Sequence number, modulo 256.
        payload: The frame's bytes.
        crc_ok: Whether the checksum matched.
    """

    nonce: int
    kind: int
    seq: int
    payload: bytes
    crc_ok: bool


def crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF).

    Args:
        data: The bytes to checksum.

    Returns:
        The 16-bit checksum.
    """
    return binascii.crc_hqx(data, CRC_INIT)


def encode_frame(nonce: int, kind: int, seq: int, payload: bytes = b'') -> bytes:
    """Build one frame.

    Args:
        nonce: The transfer's session number.
        kind: The frame type (TYPE_*).
        seq: Sequence number; only the low byte is sent.
        payload: At most MAX_PAYLOAD bytes.

    Returns:
        The frame, ready to write.

    Raises:
        ValueError: If the payload is too long.
    """
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f'payload exceeds {MAX_PAYLOAD} bytes')
    body = nonce.to_bytes(2, 'little') + len(payload).to_bytes(2, 'little') + bytes([kind, seq & 0xFF]) + payload
    return MAGIC + body + crc16(body).to_bytes(CRC_LENGTH, 'little')


class Decoder:
    """Turns a byte stream into frames, tolerating noise between them."""

    def __init__(self) -> None:
        """Start with an empty buffer."""
        self._buffer = b''

    def _next(self) -> Optional[Frame]:
        """Pop one complete frame off the buffer, or None if there is none yet."""
        frame = None
        while frame is None and self._sync():
            length = int.from_bytes(self._buffer[4:6], 'little')
            end = HEADER_LENGTH + length + CRC_LENGTH
            if len(self._buffer) < end:
                break
            body = self._buffer[2 : HEADER_LENGTH + length]
            frame = Frame(
                nonce=int.from_bytes(self._buffer[2:4], 'little'),
                kind=self._buffer[6],
                seq=self._buffer[7],
                payload=self._buffer[HEADER_LENGTH : HEADER_LENGTH + length],
                crc_ok=int.from_bytes(self._buffer[end - CRC_LENGTH : end], 'little') == crc16(body),
            )
            self._buffer = self._buffer[end:]
        return frame

    def _sync(self) -> bool:
        """Drop bytes before the next magic; True if a whole header is buffered."""
        start = self._buffer.find(MAGIC)
        if start < 0:
            self._buffer = self._buffer[-1:] if self._buffer.endswith(MAGIC[:1]) else b''
            return False
        self._buffer = self._buffer[start:]
        return len(self._buffer) >= HEADER_LENGTH

    def feed(self, data: bytes) -> list[Frame]:
        """Add bytes and return every frame they complete.

        Args:
            data: Bytes as read from the port.

        Returns:
            Complete frames, in order.
        """
        self._buffer += data
        frames = []
        frame = self._next()
        while frame is not None:
            frames.append(frame)
            frame = self._next()
        return frames
