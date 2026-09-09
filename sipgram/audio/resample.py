from __future__ import annotations

import numpy as np


def resample(pcm16le: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Linear resampling between the SIP codec rate and the Telegram/Opus rate."""
    if src_rate == dst_rate or not pcm16le:
        return pcm16le
    x = np.frombuffer(pcm16le[: len(pcm16le) // 2 * 2], dtype="<i2").astype(np.float64)
    n_out = int(len(x) * dst_rate / src_rate)
    if n_out <= 0:
        return b""
    src_t = np.arange(len(x)) / src_rate
    dst_t = np.arange(n_out) / dst_rate
    return np.interp(dst_t, src_t, x).astype("<i2").tobytes()


def mix(a: bytes, b: bytes) -> bytes:
    """Sums two PCM streams of the same length with clipping."""
    n = min(len(a), len(b)) // 2 * 2
    x = np.frombuffer(a[:n], dtype="<i2").astype(np.int32)
    y = np.frombuffer(b[:n], dtype="<i2").astype(np.int32)
    return np.clip(x + y, -32768, 32767).astype("<i2").tobytes()
