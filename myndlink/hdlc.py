"""HDLC-style framing used by the Actionslink transport over UART.

Reverse engineered from
reference/mynd-firmware/Projects/Mynd/external/teufel/libs/actionslink/src/transport/actionslink_bt_ll.c

Frame layout (before byte-stuffing), 8-byte header + payload:

    [0] start magic byte      = 0x55
    [1] (value << 3) | packet_type
    [2] transaction_id
    [3] payload_length (LSB)
    [4] payload_length (MSB)
    [5] payload CRC-8
    [6] reserved              = 0x00
    [7] header CRC-8 (over bytes 0..6)
    [8:] payload bytes (protobuf-encoded), CRC-8'd above

The whole thing (header + payload) is then byte-stuffed HDLC-style and
wrapped between 0x7E frame delimiters:
    - 0x7E and 0x7D in the data are escaped as 0x7D followed by (byte ^ 0x20)
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from .crc8 import crc8

FRAME_DELIMITER = 0x7E
ESCAPE_CHARACTER = 0x7D
ESCAPE_MASK = 0x20

HEADER_SIZE = 8
START_MAGIC_BYTE = 0x55

NACK_REASON_BAD_PACKET = 1
NACK_REASON_BAD_CRC = 2
NACK_REASON_INVALID_LENGTH = 3
NACK_REASON_BUSY = 4


class PacketType(enum.IntEnum):
    ACK = 0x00
    PROTOBUF = 0x01


class FrameError(Exception):
    """Raised when a received frame fails validation (bad CRC, bad magic, ...)."""


@dataclass
class Frame:
    packet_type: PacketType
    value: int  # NACK reason for ACK packets with an error, 0 for a plain ACK
    transaction_id: int
    payload: bytes


def _needs_escape(byte: int) -> bool:
    return byte in (FRAME_DELIMITER, ESCAPE_CHARACTER)


def _stuff(data: bytes) -> bytes:
    out = bytearray()
    for byte in data:
        if _needs_escape(byte):
            out.append(ESCAPE_CHARACTER)
            out.append(byte ^ ESCAPE_MASK)
        else:
            out.append(byte)
    return bytes(out)


def encode_frame(packet_type: PacketType, value: int, transaction_id: int, payload: bytes = b"") -> bytes:
    """Build a complete, byte-stuffed HDLC frame ready to write to the UART."""
    header = bytearray(HEADER_SIZE)
    header[0] = START_MAGIC_BYTE
    header[1] = ((value & 0x1F) << 3) | (packet_type & 0x07)
    header[2] = transaction_id & 0xFF
    header[3] = len(payload) & 0xFF
    header[4] = (len(payload) >> 8) & 0xFF
    header[5] = crc8(payload) if payload else 0x00
    header[6] = 0x00
    header[7] = crc8(bytes(header[:7]))

    body = bytes(header) + payload
    return bytes([FRAME_DELIMITER]) + _stuff(body) + bytes([FRAME_DELIMITER])


class FrameParser:
    """Incremental HDLC frame parser fed one buffer of bytes at a time.

    Mirrors ``actionslink_bt_ll.c``'s byte-at-a-time state machine: bytes are
    buffered between two ``0x7E`` delimiters, unescaped as they arrive, and
    validated (header CRC, payload CRC, magic byte) once a delimiter closes
    the frame.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._escaping = False
        self._in_frame = False

    def feed(self, data: bytes) -> list[Frame | FrameError]:
        """Feed raw bytes; returns a list of parsed Frames and/or FrameErrors."""
        results: list[Frame | FrameError] = []
        for byte in data:
            result = self._feed_byte(byte)
            if result is not None:
                results.append(result)
        return results

    def _reset(self) -> None:
        self._buffer.clear()
        self._escaping = False
        self._in_frame = False

    def _feed_byte(self, byte: int) -> Frame | FrameError | None:
        if byte == FRAME_DELIMITER:
            if not self._in_frame:
                # Opening delimiter (or a stray one between frames).
                self._in_frame = True
                self._buffer.clear()
                self._escaping = False
                return None

            # Closing delimiter.
            data = bytes(self._buffer)
            self._reset()
            if not data:
                # Two delimiters back-to-back; ignore, wait for real data.
                self._in_frame = True
                return None
            return self._validate(data)

        if not self._in_frame:
            # Noise before the first delimiter; ignore.
            return None

        if byte == ESCAPE_CHARACTER:
            self._escaping = True
            return None

        if self._escaping:
            byte ^= ESCAPE_MASK
            self._escaping = False

        self._buffer.append(byte)
        return None

    @staticmethod
    def _validate(data: bytes) -> Frame | FrameError:
        if len(data) < HEADER_SIZE:
            return FrameError(f"frame too short: {len(data)} bytes")

        header = data[:HEADER_SIZE]
        payload = data[HEADER_SIZE:]

        expected_header_crc = crc8(header[:7])
        if expected_header_crc != header[7]:
            return FrameError(f"bad header crc (exp {expected_header_crc:#04x}, got {header[7]:#04x})")

        if header[0] != START_MAGIC_BYTE:
            return FrameError(f"bad start magic byte: {header[0]:#04x}")

        packet_type_raw = header[1] & 0x07
        try:
            packet_type = PacketType(packet_type_raw)
        except ValueError:
            return FrameError(f"invalid packet type: {packet_type_raw}")

        value = header[1] >> 3
        transaction_id = header[2]
        payload_length = header[3] | (header[4] << 8)

        if payload_length != len(payload):
            return FrameError(f"payload length mismatch (header says {payload_length}, got {len(payload)})")

        expected_payload_crc = crc8(payload) if payload else 0x00
        if expected_payload_crc != header[5]:
            return FrameError(f"bad payload crc (exp {expected_payload_crc:#04x}, got {header[5]:#04x})")

        if packet_type == PacketType.PROTOBUF and payload_length == 0:
            return FrameError("protobuf packet with empty payload")
        if packet_type == PacketType.ACK and payload_length != 0:
            return FrameError("ack/nack packet with non-empty payload")

        return Frame(packet_type=packet_type, value=value, transaction_id=transaction_id, payload=payload)
