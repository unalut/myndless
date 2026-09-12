"""CRC-8 implementation matching the MYND firmware's Actionslink transport.

Parameters (reverse engineered from
reference/mynd-firmware/.../actionslink_bt_ll.c):
    poly   = 0x07
    init   = 0x00
    refin  = False
    refout = False
    xorout = 0x00
"""

from __future__ import annotations

_POLY = 0x07


def _build_table() -> list[int]:
    table = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) & 0xFF) ^ _POLY
            else:
                crc = (crc << 1) & 0xFF
        table.append(crc)
    return table


# NOTE: the naive bit-by-bit construction above only matches the firmware's
# lookup table for a non-reflected CRC-8 with poly 0x07. Verified below
# against the literal table dumped from actionslink_bt_ll.c.
_TABLE = _build_table()

_REFERENCE_TABLE_PREFIX = [
    0x00, 0x07, 0x0E, 0x09, 0x1C, 0x1B, 0x12, 0x15,
    0x38, 0x3F, 0x36, 0x31, 0x24, 0x23, 0x2A, 0x2D,
]

assert _TABLE[:16] == _REFERENCE_TABLE_PREFIX, "CRC-8 table does not match firmware reference"


def crc8(data: bytes, crc: int = 0x00) -> int:
    """Compute the Actionslink CRC-8 over ``data``, continuing from ``crc``."""
    for byte in data:
        crc = _TABLE[(crc ^ byte) & 0xFF]
    return crc
