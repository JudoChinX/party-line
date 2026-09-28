"""RTL1, the RT4K's framed binary transfer format. Pure: no I/O.

A frame is: A5 5A | nonce lo hi | length lo hi | type | seq | payload | crc16 lo hi,
with CRC-16/CCITT-FALSE over nonce..payload.
"""

from __future__ import annotations

import binascii

CRC_INIT = 0xFFFF
CRC_LENGTH = 2
HEADER_LENGTH = 8
MAGIC = b'\xa5\x5a'
MAX_PAYLOAD = 2048
TYPE_ABORT = 6
TYPE_DATA = 3
TYPE_NAK = 5
TYPE_RESPONSE = 2


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
