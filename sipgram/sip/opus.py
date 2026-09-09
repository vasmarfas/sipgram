"""Opus encoder/decoder through ctypes bindings to libopus.

libopus is a small C library (`apt install libopus0`); binding it directly keeps the wheel
list short and works the same in the container and on a developer machine that has the library.
When it is missing, :func:`available` returns False and the codec is simply not offered.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import logging

log = logging.getLogger("sipgram.sip.opus")

APPLICATION_VOIP = 2048
_SET_BITRATE = 4002
_SET_INBAND_FEC = 4012
_SET_PACKET_LOSS_PERC = 4014
_SET_SIGNAL = 4024
_SIGNAL_VOICE = 3001
_MAX_PACKET = 1500
_MAX_FRAME_SAMPLES = 5760          # 120 ms at 48 kHz, the largest Opus frame

_LIB: ctypes.CDLL | None = None
_LOAD_ERROR = ""


def _load() -> ctypes.CDLL | None:
    global _LIB, _LOAD_ERROR
    if _LIB is not None or _LOAD_ERROR:
        return _LIB
    names = [ctypes.util.find_library("opus"), "libopus.so.0", "libopus.so", "opus.dll", "libopus-0.dll"]
    for name in [n for n in names if n]:
        try:
            lib = ctypes.CDLL(name)
        except OSError:
            continue
        lib.opus_encoder_create.restype = ctypes.c_void_p
        lib.opus_encoder_create.argtypes = [ctypes.c_int32, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        lib.opus_encode.restype = ctypes.c_int32
        lib.opus_encode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16), ctypes.c_int,
                                    ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int32]
        lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
        lib.opus_decoder_create.restype = ctypes.c_void_p
        lib.opus_decoder_create.argtypes = [ctypes.c_int32, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        lib.opus_decode.restype = ctypes.c_int32
        lib.opus_decode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int32,
                                    ctypes.POINTER(ctypes.c_int16), ctypes.c_int, ctypes.c_int]
        lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
        lib.opus_get_version_string.restype = ctypes.c_char_p
        _LIB = lib
        log.info("using %s", lib.opus_get_version_string().decode(errors="replace"))
        return _LIB
    _LOAD_ERROR = "libopus not found (install libopus0)"
    return None


def available() -> bool:
    return _load() is not None


def load_error() -> str:
    _load()
    return _LOAD_ERROR


class OpusError(RuntimeError):
    pass


class Encoder:
    def __init__(self, rate: int = 48000, channels: int = 1, bitrate: int = 24000, loss_percent: int = 5):
        lib = _load()
        if lib is None:
            raise OpusError(_LOAD_ERROR)
        err = ctypes.c_int()
        self._st = lib.opus_encoder_create(rate, channels, APPLICATION_VOIP, ctypes.byref(err))
        if err.value != 0 or not self._st:
            raise OpusError(f"opus_encoder_create failed: {err.value}")
        self._lib = lib
        self.rate = rate
        self.channels = channels
        for request, value in ((_SET_BITRATE, bitrate), (_SET_INBAND_FEC, 1),
                               (_SET_PACKET_LOSS_PERC, loss_percent), (_SET_SIGNAL, _SIGNAL_VOICE)):
            lib.opus_encoder_ctl(ctypes.c_void_p(self._st), ctypes.c_int(request), ctypes.c_int32(value))

    def encode(self, pcm16le: bytes) -> bytes:
        samples = len(pcm16le) // 2 // self.channels
        if samples <= 0:
            return b""
        buf = (ctypes.c_ubyte * _MAX_PACKET)()
        pcm = (ctypes.c_int16 * (samples * self.channels)).from_buffer_copy(pcm16le[: samples * self.channels * 2])
        n = self._lib.opus_encode(ctypes.c_void_p(self._st), pcm, samples, buf, _MAX_PACKET)
        if n < 0:
            raise OpusError(f"opus_encode failed: {n}")
        return bytes(buf[:n])

    def close(self) -> None:
        if getattr(self, "_st", None):
            self._lib.opus_encoder_destroy(ctypes.c_void_p(self._st))
            self._st = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


class Decoder:
    def __init__(self, rate: int = 48000, channels: int = 1):
        lib = _load()
        if lib is None:
            raise OpusError(_LOAD_ERROR)
        err = ctypes.c_int()
        self._st = lib.opus_decoder_create(rate, channels, ctypes.byref(err))
        if err.value != 0 or not self._st:
            raise OpusError(f"opus_decoder_create failed: {err.value}")
        self._lib = lib
        self.rate = rate
        self.channels = channels

    def decode(self, payload: bytes, lost: bool = False) -> bytes:
        pcm = (ctypes.c_int16 * (_MAX_FRAME_SAMPLES * self.channels))()
        if lost or not payload:
            n = self._lib.opus_decode(ctypes.c_void_p(self._st), None, 0, pcm, _MAX_FRAME_SAMPLES, 0)
        else:
            data = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
            n = self._lib.opus_decode(ctypes.c_void_p(self._st), data, len(payload), pcm, _MAX_FRAME_SAMPLES, 0)
        if n < 0:
            raise OpusError(f"opus_decode failed: {n}")
        return bytes(memoryview(pcm).cast("B")[: n * self.channels * 2])

    def close(self) -> None:
        if getattr(self, "_st", None):
            self._lib.opus_decoder_destroy(ctypes.c_void_p(self._st))
            self._st = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
