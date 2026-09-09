"""In-memory call recording: both directions mixed, encoded to Ogg/Opus, sent to the chat.

Nothing is written to disk. Encoding happens while the call runs (a minute of speech is about
150 KB of Opus instead of 5.7 MB of raw PCM), so a long call stays cheap in memory.
"""
from __future__ import annotations

import logging

from ..sip import opus
from .ogg import OggOpusWriter
from .resample import mix, resample

log = logging.getLogger("sipgram.record")

FRAME_SAMPLES = 960          # 20 ms at 48 kHz


class RecordingUnavailable(RuntimeError):
    pass


class CallRecorder:
    def __init__(self, max_seconds: int = 3600, bitrate: int = 20000):
        if not opus.available():
            raise RecordingUnavailable(opus.load_error())
        self.encoder = opus.Encoder(48000, 1, bitrate)
        self.writer = OggOpusWriter(48000, 1)
        self.max_samples = max(1, max_seconds) * 48000
        self.samples = 0
        self.truncated = False
        self._pcm = bytearray()

    def add(self, far: bytes, near: bytes, rate: int) -> None:
        """One tick of the bridge: what the PBX side said and what the Telegram side said."""
        if self.samples >= self.max_samples:
            self.truncated = True
            return
        chunk = mix(far, near) if near else far
        if rate != 48000:
            chunk = resample(chunk, rate, 48000)
        self._pcm.extend(chunk)
        step = FRAME_SAMPLES * 2
        while len(self._pcm) >= step:
            frame = bytes(self._pcm[:step])
            del self._pcm[:step]
            try:
                self.writer.write(self.encoder.encode(frame), FRAME_SAMPLES)
            except Exception as e:
                log.warning("recording stopped, encoder failed: %s", e)
                self.samples = self.max_samples
                return
            self.samples += FRAME_SAMPLES

    def finish(self) -> tuple[bytes, int]:
        """Returns the .ogg voice message and its duration in seconds."""
        data = self.writer.finish()
        self.encoder.close()
        return data, int(self.samples / 48000)

    @property
    def duration(self) -> float:
        return self.samples / 48000.0
