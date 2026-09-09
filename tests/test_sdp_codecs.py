import math

import numpy as np
import pytest

from sipgram.sip import opus
from sipgram.sip.codecs import L16Codec, PcmaCodec, PcmuCodec, build_codec, codec_list
from sipgram.sip.sdp import Sdp, SdpCodec, answer_for, local_offer, match_answer

ASTERISK_OFFER = """v=0
o=- 1234 1234 IN IP4 10.0.0.1
s=Asterisk
c=IN IP4 10.0.0.1
t=0 0
m=audio 10002 RTP/AVP 8 0 101
a=rtpmap:8 PCMA/8000
a=rtpmap:0 PCMU/8000
a=rtpmap:101 telephone-event/8000
a=fmtp:101 0-16
a=ptime:20
a=maxptime:150
a=sendrecv
"""


def test_parse_offer():
    sdp = Sdp.parse(ASTERISK_OFFER)
    assert sdp.conn_ip == "10.0.0.1" and sdp.port == 10002
    assert [c.name for c in sdp.codecs] == ["PCMA", "PCMU", "telephone-event"]
    assert sdp.dtmf_pt == 101 and sdp.ptime == 20 and sdp.direction == "sendrecv"


def test_answer_prefers_offer_order():
    offer = Sdp.parse(ASTERISK_OFFER)
    supported = codec_list(["pcmu", "pcma"])
    ans, chosen = answer_for(offer, "10.0.0.5", 40000, supported)
    assert chosen.name == "PCMA" and chosen.pt == 8
    text = ans.build()
    assert "m=audio 40000 RTP/AVP 8 101" in text
    assert "a=rtpmap:101 telephone-event/8000" in text
    assert "c=IN IP4 10.0.0.5" in text
    assert Sdp.parse(text).dtmf_pt == 101


def test_answer_none_when_no_common():
    offer = Sdp.parse(ASTERISK_OFFER.replace("m=audio 10002 RTP/AVP 8 0 101", "m=audio 10002 RTP/AVP 18"))
    assert answer_for(offer, "10.0.0.5", 40000, codec_list(["pcma"])) is None


def test_offer_and_match():
    offer = local_offer("10.0.0.5", 40000, codec_list(["pcma", "l16/16000"]))
    # one telephone-event per clock rate: RFC 4733 events run at the audio codec's rate
    assert [(c.name, c.rate, c.pt) for c in offer.codecs] == [
        ("PCMA", 8000, 8), ("L16", 16000, 96), ("telephone-event", 8000, 101), ("telephone-event", 16000, 102)]
    assert offer.dtmf_pt_for(16000) == 102 and offer.dtmf_pt_for(8000) == 101 and offer.dtmf_pt_for(None) == 101
    answer = Sdp.parse("v=0\r\no=- 1 1 IN IP4 10.0.0.1\r\ns=-\r\nc=IN IP4 10.0.0.1\r\nt=0 0\r\n"
                       "m=audio 5000 RTP/AVP 96 101\r\na=rtpmap:96 L16/16000\r\na=rtpmap:101 telephone-event/8000\r\n")
    chosen = match_answer(offer, answer)
    assert chosen and chosen.name == "L16" and chosen.rate == 16000 and chosen.pt == 96


def test_hold_direction():
    offer = Sdp.parse(ASTERISK_OFFER.replace("a=sendrecv", "a=sendonly"))
    ans, _ = answer_for(offer, "10.0.0.5", 40000, codec_list(["pcma"]))
    assert ans.direction == "recvonly"


def _tone(rate: int, seconds: float = 0.1) -> bytes:
    t = np.arange(int(rate * seconds)) / rate
    return (np.sin(2 * math.pi * 440 * t) * 12000).astype("<i2").tobytes()


def test_g711_roundtrip_quality():
    for codec in (PcmuCodec(0), PcmaCodec(8)):
        pcm = _tone(8000)
        payload = codec.encode(pcm)
        assert len(payload) == len(pcm) // 2
        back = np.frombuffer(codec.decode(payload), dtype="<i2").astype(np.float64)
        orig = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
        snr = 10 * np.log10(np.sum(orig ** 2) / np.sum((orig - back) ** 2))
        assert snr > 30, f"{codec.name} SNR {snr:.1f} dB"


def test_g711_reference_values():
    # canonical samples from the ITU G.711 tables via g711.c reference implementation
    u = PcmuCodec(0)
    a = PcmaCodec(8)
    assert u.encode(b"\x00\x00") == b"\xff"
    assert a.encode(b"\x00\x00") == b"\xd5"
    assert u.decode(b"\xff") == b"\x00\x00"
    assert a.decode(b"\xd5") == b"\x08\x00"
    assert u.encode((32767).to_bytes(2, "little", signed=True)) == b"\x80"
    assert u.encode((-32768).to_bytes(2, "little", signed=True)) == b"\x00"
    assert a.encode((32767).to_bytes(2, "little", signed=True)) == b"\xaa"
    assert a.encode((-32768).to_bytes(2, "little", signed=True)) == b"\x2a"


def test_l16_big_endian():
    c = L16Codec(96, 16000)
    assert c.encode(b"\x34\x12") == b"\x12\x34"
    assert c.decode(b"\x12\x34") == b"\x34\x12"


def test_build_codec_and_list():
    assert build_codec(SdpCodec(0, "PCMU", 8000)).name == "PCMU"
    assert build_codec(SdpCodec(18, "G729", 8000)) is None
    lst = codec_list(["ulaw", "alaw", "slin16"])
    assert [(c.name, c.rate) for c in lst] == [("PCMU", 8000), ("PCMA", 8000), ("L16", 16000)]


def test_opus_offer_shape():
    """RFC 7587: opus is always advertised as 48000/2 even when the stream is mono."""
    if not opus.available():
        pytest.skip("libopus is not installed")
    offer = local_offer("10.0.0.5", 40000, codec_list(["opus", "pcma"]))
    text = offer.build()
    assert "a=rtpmap:96 opus/48000/2" in text
    assert "a=fmtp:96 useinbandfec=1" in text
    assert "a=rtpmap:101 telephone-event/48000" in text and "a=rtpmap:102 telephone-event/8000" in text
    c = build_codec(offer.codecs[0])
    assert c.name == "opus" and c.rate == 48000


def test_opus_roundtrip_quality():
    if not opus.available():
        pytest.skip("libopus is not installed")
    codec = build_codec(SdpCodec(96, "opus", 48000, 2))
    frame = _tone(48000, 0.02)                     # 20 ms at 48 kHz
    for _ in range(10):                            # let the encoder settle
        payload = codec.encode(frame)
        back = codec.decode(payload)
    assert 0 < len(payload) < 400, f"20 ms of speech should be a small packet, got {len(payload)}"
    assert len(back) == len(frame)
    x = np.frombuffer(back, dtype="<i2").astype(np.float64)
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    peak = float(np.fft.rfftfreq(len(x), 1 / 48000)[np.argmax(spec[1:]) + 1])
    assert abs(peak - 440) < 60, f"440 Hz tone came back as {peak:.0f} Hz"
