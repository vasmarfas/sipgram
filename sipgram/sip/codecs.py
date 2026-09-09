"""Audio codecs for the SIP leg: G.711 (PCMU/PCMA) via lookup tables and L16."""
from __future__ import annotations

import logging

import numpy as np

from . import opus
from .sdp import SdpCodec

log = logging.getLogger("sipgram.sip.codecs")

_BIAS = 0x84
_CLIP = 8159
_SEG_UEND = np.array([0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF], dtype=np.int32)
_SEG_AEND = np.array([0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF], dtype=np.int32)


def _ulaw_encode_table() -> np.ndarray:
    pcm = np.arange(-32768, 32768, dtype=np.int32) >> 2
    mask = np.where(pcm < 0, 0x7F, 0xFF)
    mag = np.minimum(np.abs(pcm), _CLIP) + (_BIAS >> 2)
    seg = np.searchsorted(_SEG_UEND, mag, side="left")
    seg_c = np.minimum(seg, 7)
    out = np.where(seg >= 8, 0x7F, (seg_c << 4) | ((mag >> (seg_c + 1)) & 0xF))
    return (out ^ mask).astype(np.uint8)


def _alaw_encode_table() -> np.ndarray:
    pcm = np.arange(-32768, 32768, dtype=np.int32) >> 3
    mask = np.where(pcm >= 0, 0xD5, 0x55)
    mag = np.where(pcm >= 0, pcm, -pcm - 1)
    seg = np.searchsorted(_SEG_AEND, mag, side="left")
    seg_c = np.minimum(seg, 7)
    quant = np.where(seg_c < 2, (mag >> 1) & 0xF, (mag >> seg_c) & 0xF)
    out = np.where(seg >= 8, 0x7F, (seg_c << 4) | quant)
    return (out ^ mask).astype(np.uint8)


def _ulaw_decode_table() -> np.ndarray:
    out = np.zeros(256, dtype=np.int16)
    for u in range(256):
        v = ~u & 0xFF
        t = ((v & 0xF) << 3) + _BIAS
        t <<= (v & 0x70) >> 4
        out[u] = (_BIAS - t) if (v & 0x80) else (t - _BIAS)
    return out


def _alaw_decode_table() -> np.ndarray:
    out = np.zeros(256, dtype=np.int16)
    for a in range(256):
        v = a ^ 0x55
        t = (v & 0xF) << 4
        seg = (v & 0x70) >> 4
        if seg == 0:
            t += 8
        elif seg == 1:
            t += 0x108
        else:
            t += 0x108
            t <<= seg - 1
        out[a] = t if (v & 0x80) else -t
    return out


ULAW_ENC = _ulaw_encode_table()
ALAW_ENC = _alaw_encode_table()
ULAW_DEC = _ulaw_decode_table()
ALAW_DEC = _alaw_decode_table()


def _pcm_index(pcm16le: bytes) -> np.ndarray:
    return np.frombuffer(pcm16le[: len(pcm16le) // 2 * 2], dtype="<i2").astype(np.int32) + 32768


class AudioCodec:
    """Converts between 16-bit little-endian PCM (host side) and RTP payload."""

    name = "RAW"
    rate = 8000
    channels = 1

    def __init__(self, pt: int):
        self.pt = pt

    def encode(self, pcm16le: bytes) -> bytes:
        raise NotImplementedError

    def decode(self, payload: bytes) -> bytes:
        raise NotImplementedError

    def silence(self, samples: int) -> bytes:
        return self.encode(b"\x00\x00" * samples)

    def samples(self, ms: int) -> int:
        return self.rate * ms // 1000

    def to_sdp(self) -> SdpCodec:
        return SdpCodec(self.pt, self.name, self.rate, self.channels)


class PcmuCodec(AudioCodec):
    name = "PCMU"

    def encode(self, pcm16le: bytes) -> bytes:
        return ULAW_ENC[_pcm_index(pcm16le)].tobytes()

    def decode(self, payload: bytes) -> bytes:
        return ULAW_DEC[np.frombuffer(payload, dtype=np.uint8)].tobytes()

    def silence(self, samples: int) -> bytes:
        return b"\xff" * samples


class PcmaCodec(AudioCodec):
    name = "PCMA"

    def encode(self, pcm16le: bytes) -> bytes:
        return ALAW_ENC[_pcm_index(pcm16le)].tobytes()

    def decode(self, payload: bytes) -> bytes:
        return ALAW_DEC[np.frombuffer(payload, dtype=np.uint8)].tobytes()

    def silence(self, samples: int) -> bytes:
        return b"\xd5" * samples


class L16Codec(AudioCodec):
    name = "L16"

    def __init__(self, pt: int, rate: int):
        super().__init__(pt)
        self.rate = rate

    def encode(self, pcm16le: bytes) -> bytes:
        return np.frombuffer(pcm16le[: len(pcm16le) // 2 * 2], dtype="<i2").astype(">i2").tobytes()

    def decode(self, payload: bytes) -> bytes:
        return np.frombuffer(payload[: len(payload) // 2 * 2], dtype=">i2").astype("<i2").tobytes()


class OpusCodec(AudioCodec):
    """Opus at 48 kHz mono. SDP always advertises opus/48000/2 (RFC 7587) even for a mono stream."""

    name = "opus"
    rate = 48000

    def __init__(self, pt: int, bitrate: int = 24000):
        super().__init__(pt)
        self._encoder = opus.Encoder(self.rate, 1, bitrate)
        self._decoder = opus.Decoder(self.rate, 1)

    def encode(self, pcm16le: bytes) -> bytes:
        return self._encoder.encode(pcm16le)

    def decode(self, payload: bytes) -> bytes:
        return self._decoder.decode(payload)

    def silence(self, samples: int) -> bytes:
        return self._encoder.encode(b"\x00\x00" * samples)

    def to_sdp(self) -> SdpCodec:
        return SdpCodec(self.pt, "opus", 48000, 2, OPUS_FMTP)


OPUS_FMTP = "useinbandfec=1;usedtx=0;maxplaybackrate=48000;stereo=0;sprop-stereo=0"


def build_codec(sdp_codec: SdpCodec) -> AudioCodec | None:
    n = sdp_codec.name.upper()
    if n == "PCMU" and sdp_codec.rate == 8000:
        return PcmuCodec(sdp_codec.pt)
    if n == "PCMA" and sdp_codec.rate == 8000:
        return PcmaCodec(sdp_codec.pt)
    if n == "OPUS" and sdp_codec.rate == 48000:
        return OpusCodec(sdp_codec.pt) if opus.available() else None
    if n == "L16" and sdp_codec.channels == 1 and sdp_codec.rate in (8000, 16000, 24000, 32000, 44100, 48000):
        return L16Codec(sdp_codec.pt, sdp_codec.rate)
    return None


def codec_list(names: list[str]) -> list[SdpCodec]:
    """Config names -> SDP codec descriptors with payload types."""
    out: list[SdpCodec] = []
    dyn = 96
    for raw in names:
        n = raw.strip().lower()
        if n in ("pcmu", "ulaw", "g711u", "mulaw"):
            out.append(SdpCodec(0, "PCMU", 8000))
        elif n in ("pcma", "alaw", "g711a"):
            out.append(SdpCodec(8, "PCMA", 8000))
        elif n.startswith("opus"):
            if not opus.available():
                log.warning("codec opus is configured but %s; continuing without it", opus.load_error())
                continue
            out.append(SdpCodec(dyn, "opus", 48000, 2, OPUS_FMTP))
            dyn += 1
        elif n.startswith("l16") or n.startswith("slin"):
            rate = 8000
            if n == "slin16":
                rate = 16000
            for sep in ("/", "-", ":"):
                if sep in n:
                    rate = int(n.split(sep, 1)[1])
            if rate == 44100:
                out.append(SdpCodec(11, "L16", 44100))
            else:
                out.append(SdpCodec(dyn, "L16", rate))
                dyn += 1
        else:
            raise ValueError(f"unsupported codec in config: {raw}")
    if not out:
        raise ValueError("no usable codec left: " + (opus.load_error() or "check the codecs list"))
    return out
