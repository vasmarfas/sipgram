"""Minimal Ogg/Opus writer: wraps Opus packets in the container Telegram wants for voice messages.

Only what a voice note needs: an OpusHead page, an OpusTags page, then audio pages. Everything is
built in memory, since recordings go straight into the chat and never touch the disk.
"""
from __future__ import annotations

import os
import struct

PRE_SKIP = 312          # libopus lookahead at 48 kHz
_CRC_TABLE: list[int] = []


def _crc_table() -> list[int]:
    if not _CRC_TABLE:
        for i in range(256):
            r = i << 24
            for _ in range(8):
                r = ((r << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if r & 0x80000000 else (r << 1) & 0xFFFFFFFF
            _CRC_TABLE.append(r)
    return _CRC_TABLE


def crc32(data: bytes) -> int:
    """The Ogg flavour of CRC-32: polynomial 0x04C11DB7, no reflection, no final xor."""
    table = _crc_table()
    crc = 0
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ table[((crc >> 24) & 0xFF) ^ byte]
    return crc


class OggOpusWriter:
    """Opus packets in, an .ogg voice message out. One packet per page keeps the muxing trivial."""

    def __init__(self, sample_rate: int = 48000, channels: int = 1, serial: int | None = None):
        self.rate = sample_rate
        self.channels = channels
        self.serial = serial if serial is not None else int.from_bytes(os.urandom(4), "little")
        self._page = 0
        self._granule = 0
        self._out = bytearray()
        self._pending: tuple[bytes, int] | None = None
        self._write_headers()

    # ---- container ----

    def _page_bytes(self, payload: bytes, header_type: int, granule: int) -> bytes:
        segments: list[int] = []
        remaining = len(payload)
        while remaining >= 255:
            segments.append(255)
            remaining -= 255
        segments.append(remaining)
        header = struct.pack("<4sBBqIIIB", b"OggS", 0, header_type, granule, self.serial, self._page, 0, len(segments))
        page = bytearray(header + bytes(segments) + payload)
        page[22:26] = struct.pack("<I", crc32(bytes(page)))
        self._page += 1
        return bytes(page)

    def _write_headers(self) -> None:
        head = b"OpusHead" + struct.pack("<BBHIhB", 1, self.channels, PRE_SKIP, self.rate, 0, 0)
        self._out += self._page_bytes(head, 2, 0)          # begin of stream
        vendor = b"sipgram"
        tags = b"OpusTags" + struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", 0)
        self._out += self._page_bytes(tags, 0, 0)

    def _flush_pending(self, last: bool = False) -> None:
        if self._pending is None:
            return
        packet, samples = self._pending
        self._pending = None
        self._granule += samples
        self._out += self._page_bytes(packet, 4 if last else 0, self._granule + PRE_SKIP)

    def write(self, packet: bytes, samples: int) -> None:
        """Adds one Opus packet covering `samples` samples at 48 kHz."""
        if not packet:
            return
        self._flush_pending()
        self._pending = (packet, samples)

    def finish(self) -> bytes:
        """Closes the stream (end-of-stream flag on the last page) and returns the file."""
        self._flush_pending(last=True)
        return bytes(self._out)

    @property
    def duration(self) -> float:
        pending = self._pending[1] if self._pending else 0
        return (self._granule + pending) / 48000.0

    @property
    def size(self) -> int:
        return len(self._out)
