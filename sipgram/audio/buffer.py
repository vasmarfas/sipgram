"""Thread-safe PCM FIFO used to smooth Telegram -> RTP audio."""
from __future__ import annotations

import threading


class PcmBuffer:
    def __init__(self, max_bytes: int):
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._max = max_bytes
        self.pushed = 0
        self.dropped = 0
        self.underruns = 0

    def push(self, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            self._buf.extend(data)
            self.pushed += len(data)
            overflow = len(self._buf) - self._max
            if overflow > 0:
                del self._buf[:overflow]
                self.dropped += overflow

    def pull(self, n: int, pad: bool = True) -> bytes | None:
        """Return exactly n bytes (zero padded on underrun) or None when empty and pad is False."""
        with self._lock:
            have = len(self._buf)
            if have >= n:
                out = bytes(self._buf[:n])
                del self._buf[:n]
                return out
            if have == 0 and not pad:
                return None
            self.underruns += 1
            out = bytes(self._buf) + b"\x00" * (n - have)
            self._buf.clear()
            return out

    def __len__(self) -> int:
        with self._lock:
            return len(self._buf)

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()
