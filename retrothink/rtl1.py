"""RTL1, the RT4K's framed binary transfer format. Pure: no I/O.

A frame is: A5 5A | nonce lo hi | length lo hi | type | seq | payload | crc16 lo hi,
with CRC-16/CCITT-FALSE over nonce..payload.
"""

from __future__ import annotations

import binascii

CRC_INIT = 0xFFFF


def crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF).

    Args:
        data: The bytes to checksum.

    Returns:
        The 16-bit checksum.
    """
    return binascii.crc_hqx(data, CRC_INIT)
