"""RTCP (RFC 3550): sender/receiver reports on the odd port next to the RTP one.

Two reasons to bother: some PBXes and session border controllers drop a call whose media has no
RTCP, and the reports are the only place where packet loss and jitter of a live call are visible.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import struct
import time

log = logging.getLogger("sipgram.sip.rtcp")

SR = 200
RR = 201
SDES = 202
BYE = 203
NTP_EPOCH = 2208988800          # seconds between 1900 and 1970
INTERVAL = 5.0


def ntp_now() -> tuple[int, int]:
    now = time.time() + NTP_EPOCH
    seconds = int(now)
    fraction = int((now - seconds) * (1 << 32))
    return seconds, fraction


class InboundStats:
    """Everything a receiver report needs about the stream coming in."""

    def __init__(self, clock_rate: int):
        self.clock_rate = clock_rate
        self.ssrc: int | None = None
        self.base_seq = 0
        self.max_seq = 0
        self.cycles = 0
        self.received = 0
        self.expected_prior = 0
        self.received_prior = 0
        self.jitter = 0.0
        self._transit = 0.0
        self.last_sr_middle = 0
        self.last_sr_at = 0.0

    def on_packet(self, seq: int, timestamp: int, ssrc: int) -> None:
        if self.ssrc is None:
            self.ssrc = ssrc
            self.base_seq = seq
            self.max_seq = seq
        elif ssrc != self.ssrc:                       # the far end restarted its stream
            self.ssrc, self.base_seq, self.max_seq = ssrc, seq, seq
            self.cycles = self.received = 0
            self.expected_prior = self.received_prior = 0
        else:
            if seq < self.max_seq and self.max_seq - seq > 32768:
                self.cycles += 1 << 16
            if (seq + self.cycles) > (self.max_seq + self.cycles):
                self.max_seq = seq
        self.received += 1
        arrival = time.monotonic() * self.clock_rate
        transit = arrival - timestamp
        if self._transit:
            d = abs(transit - self._transit)
            self.jitter += (d - self.jitter) / 16.0
        self._transit = transit

    @property
    def expected(self) -> int:
        return (self.cycles + self.max_seq) - self.base_seq + 1

    def report_block(self) -> bytes | None:
        if self.ssrc is None:
            return None
        expected = self.expected
        lost = max(0, expected - self.received)
        expected_interval = expected - self.expected_prior
        received_interval = self.received - self.received_prior
        self.expected_prior, self.received_prior = expected, self.received
        lost_interval = expected_interval - received_interval
        fraction = 0 if expected_interval <= 0 or lost_interval <= 0 else (lost_interval << 8) // expected_interval
        dlsr = int((time.monotonic() - self.last_sr_at) * 65536) if self.last_sr_at else 0
        return struct.pack("!IBBHIIII", self.ssrc, min(fraction, 255), (lost >> 16) & 0xFF, lost & 0xFFFF,
                           self.cycles + self.max_seq, int(self.jitter), self.last_sr_middle, dlsr)

    @property
    def loss_percent(self) -> float:
        expected = self.expected
        return 0.0 if expected <= 0 else max(0.0, (expected - self.received) * 100.0 / expected)


class RtcpSession(asyncio.DatagramProtocol):
    """Sends a report every five seconds and remembers what the far end reports back."""

    def __init__(self, sock: socket.socket, rtp, cname: str = "sipgram"):
        self.sock = sock
        self.rtp = rtp
        self.cname = cname.encode()[:255]
        self.remote: tuple[str, int] | None = None
        self.stats = InboundStats(rtp.clock_rate)
        self.remote_loss_percent = 0.0
        self.remote_jitter = 0
        self.reports_in = 0
        self.reports_out = 0
        self._transport: asyncio.DatagramTransport | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        loop = asyncio.get_event_loop()
        transport, _ = await loop.create_datagram_endpoint(lambda: self, sock=self.sock)
        self._transport = transport
        self._task = loop.create_task(self._loop())

    def close(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        if self._transport:
            if self.remote:
                try:
                    self._transport.sendto(self._compound(bye=True), self.remote)
                except Exception:
                    pass
            self._transport.close()
            self._transport = None

    def set_remote(self, ip: str, port: int) -> None:
        self.remote = (ip, port)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(INTERVAL)
            if self.remote and self._transport:
                try:
                    self._transport.sendto(self._compound(), self.remote)
                    self.reports_out += 1
                except Exception as e:
                    log.debug("rtcp send failed: %s", e)

    # ---- packets ----

    def _compound(self, bye: bool = False) -> bytes:
        block = self.stats.report_block()
        blocks = block or b""
        count = 1 if block else 0
        if self.rtp.tx_packets:
            seconds, fraction = ntp_now()
            body = struct.pack("!IIIII", self.rtp.ssrc, seconds, fraction, self.rtp.timestamp & 0xFFFFFFFF,
                               self.rtp.tx_packets) + struct.pack("!I", self.rtp.tx_octets)
            packet = self._header(SR, count, len(body) + len(blocks)) + body + blocks
        else:
            body = struct.pack("!I", self.rtp.ssrc)
            packet = self._header(RR, count, len(body) + len(blocks)) + body + blocks
        packet += self._sdes()
        if bye:
            packet += self._header(BYE, 1, 4) + struct.pack("!I", self.rtp.ssrc)
        return packet

    @staticmethod
    def _header(pt: int, count: int, body_len: int) -> bytes:
        length = (4 + body_len) // 4 - 1
        return struct.pack("!BBH", 0x80 | (count & 0x1F), pt, length)

    def _sdes(self) -> bytes:
        item = bytes([1, len(self.cname)]) + self.cname
        chunk = struct.pack("!I", self.rtp.ssrc) + item + b"\x00"
        while len(chunk) % 4:
            chunk += b"\x00"
        return self._header(SDES, 1, len(chunk)) + chunk

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        offset = 0
        while offset + 4 <= len(data):
            first, pt, words = struct.unpack_from("!BBH", data, offset)
            size = (words + 1) * 4
            body = data[offset + 4: offset + size]
            if pt == SR and len(body) >= 24:
                ntp_sec, ntp_frac = struct.unpack_from("!II", body, 4)
                self.stats.last_sr_middle = ((ntp_sec & 0xFFFF) << 16) | (ntp_frac >> 16)
                self.stats.last_sr_at = time.monotonic()
                self._read_blocks(body[24:], first & 0x1F)
            elif pt == RR and len(body) >= 4:
                self._read_blocks(body[4:], first & 0x1F)
            offset += size
        self.reports_in += 1

    def _read_blocks(self, data: bytes, count: int) -> None:
        for i in range(count):
            block = data[i * 24: (i + 1) * 24]
            if len(block) < 24:
                return
            _, fraction, hi, lo, _, jitter, _, _ = struct.unpack("!IBBHIIII", block)
            self.remote_loss_percent = fraction * 100.0 / 256.0
            self.remote_jitter = jitter

    def report(self) -> dict:
        return {
            "rx_packets": self.rtp.rx_packets,
            "tx_packets": self.rtp.tx_packets,
            "loss_percent_in": round(self.stats.loss_percent, 2),
            "jitter_ms_in": round(self.stats.jitter * 1000.0 / max(1, self.stats.clock_rate), 1),
            "loss_percent_out": round(self.remote_loss_percent, 2),
            "jitter_ms_out": round(self.remote_jitter * 1000.0 / max(1, self.stats.clock_rate), 1),
            "reports_in": self.reports_in,
            "reports_out": self.reports_out,
        }
