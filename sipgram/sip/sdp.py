"""Minimal SDP (RFC 4566) offer/answer for a single audio stream."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

STATIC_PAYLOADS = {
    0: ("PCMU", 8000, 1),
    3: ("GSM", 8000, 1),
    8: ("PCMA", 8000, 1),
    9: ("G722", 8000, 1),
    10: ("L16", 44100, 2),
    11: ("L16", 44100, 1),
    13: ("CN", 8000, 1),
    18: ("G729", 8000, 1),
}


@dataclass
class SdpCodec:
    pt: int
    name: str
    rate: int
    channels: int = 1
    fmtp: str = ""

    def __str__(self) -> str:
        return f"{self.name}/{self.rate}" + (f"/{self.channels}" if self.channels > 1 else "")


@dataclass
class Sdp:
    origin_ip: str = "0.0.0.0"
    conn_ip: str = "0.0.0.0"
    port: int = 0
    codecs: list[SdpCodec] = field(default_factory=list)
    direction: str = "sendrecv"
    ptime: int | None = None
    session_id: int = 0
    session_version: int = 0
    session_name: str = "sipgram"
    proto: str = "RTP/AVP"

    def codec(self, name: str, rate: int | None = None) -> SdpCodec | None:
        for c in self.codecs:
            if c.name.upper() == name.upper() and (rate is None or c.rate == rate):
                return c
        return None

    @property
    def dtmf_pt(self) -> int | None:
        c = self.codec("telephone-event")
        return c.pt if c else None

    def dtmf_pt_for(self, rate: int | None) -> int | None:
        """RFC 4733 events must run at the clock rate of the chosen audio codec."""
        if rate is not None:
            c = self.codec("telephone-event", rate)
            if c:
                return c.pt
        return self.dtmf_pt

    @classmethod
    def parse(cls, text: str | bytes) -> Sdp:
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        sdp = cls()
        session_conn = ""
        media_conn = ""
        in_audio = False
        seen_audio = False
        pt_order: list[int] = []
        rtpmap: dict[int, tuple[str, int, int]] = {}
        fmtp: dict[int, str] = {}
        for raw in text.replace("\r\n", "\n").split("\n"):
            line = raw.strip()
            if len(line) < 2 or line[1] != "=":
                continue
            key, value = line[0], line[2:]
            if key == "o":
                parts = value.split()
                if len(parts) >= 6:
                    sdp.session_id = int(parts[1]) if parts[1].isdigit() else 0
                    sdp.session_version = int(parts[2]) if parts[2].isdigit() else 0
                    sdp.origin_ip = parts[5]
            elif key == "c":
                parts = value.split()
                ip = parts[2] if len(parts) >= 3 else ""
                if in_audio:
                    media_conn = ip
                elif not seen_audio:
                    session_conn = ip
            elif key == "m":
                parts = value.split()
                if parts[0] == "audio" and not seen_audio:
                    in_audio = True
                    seen_audio = True
                    sdp.port = int(parts[1])
                    sdp.proto = parts[2]
                    pt_order = [int(p) for p in parts[3:] if p.isdigit()]
                else:
                    in_audio = False
            elif key == "a":
                name, _, arg = value.partition(":")
                if name in ("sendrecv", "sendonly", "recvonly", "inactive"):
                    if in_audio or not seen_audio:
                        sdp.direction = name
                elif in_audio and name == "rtpmap":
                    pt_s, _, desc = arg.partition(" ")
                    if pt_s.isdigit():
                        d = desc.split("/")
                        rate = int(d[1]) if len(d) > 1 and d[1].isdigit() else 8000
                        ch = int(d[2]) if len(d) > 2 and d[2].isdigit() else 1
                        rtpmap[int(pt_s)] = (d[0], rate, ch)
                elif in_audio and name == "fmtp":
                    pt_s, _, params = arg.partition(" ")
                    if pt_s.isdigit():
                        fmtp[int(pt_s)] = params.strip()
                elif in_audio and name == "ptime" and arg.strip().isdigit():
                    sdp.ptime = int(arg)
        sdp.conn_ip = media_conn or session_conn or sdp.origin_ip
        for pt in pt_order:
            if pt in rtpmap:
                cname, rate, ch = rtpmap[pt]
            elif pt in STATIC_PAYLOADS:
                cname, rate, ch = STATIC_PAYLOADS[pt]
            else:
                continue
            sdp.codecs.append(SdpCodec(pt, cname, rate, ch, fmtp.get(pt, "")))
        return sdp

    def build(self) -> str:
        if not self.session_id:
            self.session_id = int(time.time())
        if not self.session_version:
            self.session_version = self.session_id
        lines = [
            "v=0",
            f"o=sipgram {self.session_id} {self.session_version} IN IP4 {self.origin_ip}",
            f"s={self.session_name}",
            f"c=IN IP4 {self.conn_ip}",
            "t=0 0",
            f"m=audio {self.port} {self.proto} " + " ".join(str(c.pt) for c in self.codecs),
        ]
        for c in self.codecs:
            lines.append(f"a=rtpmap:{c.pt} {c}")
            if c.fmtp:
                lines.append(f"a=fmtp:{c.pt} {c.fmtp}")
        if self.ptime:
            lines.append(f"a=ptime:{self.ptime}")
        lines.append(f"a={self.direction}")
        return "\r\n".join(lines) + "\r\n"

    def encode(self) -> bytes:
        return self.build().encode("utf-8")


def local_offer(ip: str, port: int, codecs: list[SdpCodec], dtmf_pt: int | None = 101) -> Sdp:
    """Offer the codecs plus one telephone-event per distinct clock rate among them."""
    sdp = Sdp(origin_ip=ip, conn_ip=ip, port=port, codecs=list(codecs), ptime=20)
    if dtmf_pt is None:
        return sdp
    used = {c.pt for c in sdp.codecs}
    pt = dtmf_pt
    for rate in dict.fromkeys(c.rate for c in codecs):
        if sdp.codec("telephone-event", rate):
            continue
        while pt in used:
            pt += 1
        used.add(pt)
        sdp.codecs.append(SdpCodec(pt, "telephone-event", rate, 1, "0-16"))
    return sdp


def answer_for(offer: Sdp, ip: str, port: int, supported: list[SdpCodec]) -> tuple[Sdp, SdpCodec] | None:
    """Pick the first offered codec we support (offerer preference wins)."""
    chosen: SdpCodec | None = None
    for oc in offer.codecs:
        for sc in supported:
            if oc.name.upper() == sc.name.upper() and oc.rate == sc.rate and oc.channels == sc.channels:
                chosen = SdpCodec(oc.pt, sc.name, sc.rate, sc.channels, sc.fmtp or oc.fmtp)
                break
        if chosen:
            break
    if not chosen:
        return None
    codecs = [chosen]
    te = offer.codec("telephone-event", chosen.rate) or offer.codec("telephone-event")
    if te:
        codecs.append(SdpCodec(te.pt, "telephone-event", te.rate, 1, "0-16"))
    direction = {"sendonly": "recvonly", "recvonly": "sendonly", "inactive": "inactive"}.get(offer.direction, "sendrecv")
    ans = Sdp(origin_ip=ip, conn_ip=ip, port=port, codecs=codecs, direction=direction, ptime=20)
    return ans, chosen


def match_answer(offer: Sdp, answer: Sdp) -> SdpCodec | None:
    """Codec selected by the far end from our offer."""
    for ac in answer.codecs:
        if ac.name.lower() == "telephone-event":
            continue
        for oc in offer.codecs:
            if oc.name.upper() == ac.name.upper() and oc.rate == ac.rate:
                return SdpCodec(ac.pt, oc.name, oc.rate, oc.channels, oc.fmtp)
    return None
