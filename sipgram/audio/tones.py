"""Call-progress tone generators producing 16-bit PCM frames."""
from __future__ import annotations

import math

import numpy as np

# (frequencies, on_ms, off_ms) per regional standard
RINGBACK = {
    "ru": ([425.0], 1000, 4000),
    "eu": ([425.0], 1000, 4000),
    "us": ([440.0, 480.0], 2000, 4000),
    "uk": ([400.0, 450.0], 400, 200),
}
BUSY = {
    "ru": ([425.0], 400, 400),
    "eu": ([425.0], 500, 500),
    "us": ([480.0, 620.0], 500, 500),
    "uk": ([400.0], 375, 375),
}


class ToneGenerator:
    """Yields successive frames of a cadenced tone at the given sample rate."""

    def __init__(self, freqs: list[float], on_ms: int, off_ms: int, rate: int, amplitude: float = 0.2):
        self.rate = rate
        self.freqs = freqs
        self.on = on_ms * rate // 1000
        self.off = off_ms * rate // 1000
        self.amp = amplitude * 32767 / max(1, len(freqs))
        self.pos = 0

    def frame(self, samples: int) -> bytes:
        idx = np.arange(self.pos, self.pos + samples)
        t = idx / self.rate
        signal = np.zeros(samples, dtype=np.float64)
        for f in self.freqs:
            signal += np.sin(2 * math.pi * f * t)
        cycle = self.on + self.off
        if cycle > 0:
            gate = (idx % cycle) < self.on
            signal = signal * gate
        self.pos += samples
        return (signal * self.amp).astype("<i2").tobytes()


def ringback(region: str, rate: int) -> ToneGenerator | None:
    if region in ("", "none", "off"):
        return None
    spec = RINGBACK.get(region, RINGBACK["ru"])
    return ToneGenerator(spec[0], spec[1], spec[2], rate)


def busy(region: str, rate: int) -> ToneGenerator:
    spec = BUSY.get(region, BUSY["ru"])
    return ToneGenerator(spec[0], spec[1], spec[2], rate)


DTMF_FREQS = {
    "1": (697, 1209), "2": (697, 1336), "3": (697, 1477), "A": (697, 1633),
    "4": (770, 1209), "5": (770, 1336), "6": (770, 1477), "B": (770, 1633),
    "7": (852, 1209), "8": (852, 1336), "9": (852, 1477), "C": (852, 1633),
    "*": (941, 1209), "0": (941, 1336), "#": (941, 1477), "D": (941, 1633),
}


def dtmf_pcm(digits: str, rate: int, tone_ms: int = 120, gap_ms: int = 60, amplitude: float = 0.25) -> bytes:
    """Sidetone for the keys the user pressed: RFC 4733 carries them out of band,
    so without this the caller hears nothing while dialling."""
    out = bytearray()
    tone_n = rate * tone_ms // 1000
    gap = b"\x00\x00" * (rate * gap_ms // 1000)
    fade = max(1, rate // 500)
    for ch in digits.upper():
        pair = DTMF_FREQS.get(ch)
        if pair is None:
            continue
        t = np.arange(tone_n) / rate
        signal = np.sin(2 * math.pi * pair[0] * t) + np.sin(2 * math.pi * pair[1] * t)
        envelope = np.ones(tone_n)
        envelope[:fade] = np.linspace(0.0, 1.0, fade)
        envelope[-fade:] = np.linspace(1.0, 0.0, fade)
        out += (signal * envelope * (amplitude * 32767 / 2)).astype("<i2").tobytes()
        out += gap
    return bytes(out)


def beep(rate: int, freq: float = 1000.0, ms: int = 200) -> bytes:
    return ToneGenerator([freq], ms, 0, rate, amplitude=0.3).frame(rate * ms // 1000)
