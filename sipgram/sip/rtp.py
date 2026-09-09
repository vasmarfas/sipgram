"""RTP session (RFC 3550) with RFC 4733 DTMF for a single audio stream."""
from __future__ import annotations

import asyncio
import logging
import random
import socket
import struct
from collections.abc import Callable

from .rtcp import RtcpSession

log = logging.getLogger("sipgram.sip.rtp")

DTMF_CHARS = "0123456789*#ABCD"


class RtpPortPool:
    def __init__(self, start: int, end: int):
        self.start = start if start % 2 == 0 else start + 1
        self.end = end
        self._next = self.start

    def bind(self, ip: str) -> socket.socket:
        tried = 0
        total = max(1, (self.end - self.start) // 2 + 1)
        while tried < total:
            port = self._next
            self._next += 2
            if self._next > self.end:
                self._next = self.start
            tried += 1
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.bind((ip, port))
                sock.setblocking(False)
                return sock
            except OSError:
                sock.close()
        raise OSError(f"no free RTP port in {self.start}-{self.end}")


class RtpSession(asyncio.DatagramProtocol):
    """One RTP flow. Payload callback receives (payload, marker, timestamp, seq)."""

    def __init__(self, sock: socket.socket, clock_rate: int):
        self.sock = sock
        self.local_ip, self.local_port = sock.getsockname()[:2]
        self.clock_rate = clock_rate
        self.remote: tuple[str, int] | None = None
        self.payload_type = 0
        self.dtmf_pt: int | None = None
        self.ssrc = random.getrandbits(32)
        self.seq = random.getrandbits(16)
        self.timestamp = random.getrandbits(32)
        self.on_payload: Callable[[bytes, bool, int, int], None] | None = None
        self.on_dtmf: Callable[[str], None] | None = None
        self.symmetric = True
        self._transport: asyncio.DatagramTransport | None = None
        self._dtmf_task: asyncio.Task | None = None
        self._last_event: tuple[int, int] | None = None
        self._marker_pending = True
        self.rx_packets = 0
        self.tx_packets = 0
        self.tx_octets = 0
        self.rx_ssrc: int | None = None
        self.rtcp: RtcpSession | None = None

    async def start(self, rtcp: bool = True) -> None:
        loop = asyncio.get_event_loop()
        transport, _ = await loop.create_datagram_endpoint(lambda: self, sock=self.sock)
        self._transport = transport
        if rtcp:
            await self._start_rtcp()

    async def _start_rtcp(self) -> None:
        """RTCP lives on the odd port right after the RTP one (RFC 3550)."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((self.local_ip, self.local_port + 1))
            sock.setblocking(False)
        except OSError as e:
            sock.close()
            log.debug("no RTCP port %d: %s", self.local_port + 1, e)
            return
        session = RtcpSession(sock, self)
        try:
            await session.start()
        except Exception as e:
            log.debug("RTCP could not start: %s", e)
            return
        self.rtcp = session

    def close(self) -> None:
        if self.rtcp:
            self.rtcp.close()
            self.rtcp = None
        if self._dtmf_task:
            self._dtmf_task.cancel()
            self._dtmf_task = None
        if self._transport:
            self._transport.close()
            self._transport = None

    def set_remote(self, ip: str, port: int) -> None:
        self.remote = (ip, port)
        if self.rtcp:
            self.rtcp.set_remote(ip, port + 1)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(data) < 12:
            return
        b0, b1, seq, ts, ssrc = struct.unpack("!BBHII", data[:12])
        if b0 >> 6 != 2:
            return
        cc = b0 & 0x0F
        ext = b0 & 0x10
        padding = b0 & 0x20
        marker = bool(b1 & 0x80)
        pt = b1 & 0x7F
        offset = 12 + cc * 4
        if ext:
            if len(data) < offset + 4:
                return
            ext_len = struct.unpack("!H", data[offset + 2 : offset + 4])[0]
            offset += 4 + ext_len * 4
        end = len(data)
        if padding and end > offset:
            end -= data[-1]
        payload = data[offset:end]
        if self.symmetric and addr != self.remote:
            log.debug("RTP source %s differs from SDP %s, switching (symmetric RTP)", addr, self.remote)
            self.remote = addr
        self.rx_packets += 1
        self.rx_ssrc = ssrc
        if self.rtcp is not None:
            self.rtcp.stats.on_packet(seq, ts, ssrc)
        if self.dtmf_pt is not None and pt == self.dtmf_pt:
            self._handle_dtmf(payload, ts)
            return
        if pt == self.payload_type and self.on_payload:
            self.on_payload(payload, marker, ts, seq)

    def _handle_dtmf(self, payload: bytes, ts: int) -> None:
        if len(payload) < 4:
            return
        event = payload[0]
        end = bool(payload[1] & 0x80)
        if end and event < len(DTMF_CHARS):
            key = (ts, event)
            if self._last_event != key:
                self._last_event = key
                if self.on_dtmf:
                    self.on_dtmf(DTMF_CHARS[event])

    def send(self, payload: bytes, samples: int, marker: bool = False) -> None:
        if not self.remote or not self._transport:
            return
        if self._marker_pending:
            marker = True
            self._marker_pending = False
        header = struct.pack(
            "!BBHII", 0x80, (0x80 if marker else 0) | self.payload_type,
            self.seq, self.timestamp & 0xFFFFFFFF, self.ssrc,
        )
        try:
            self._transport.sendto(header + payload, self.remote)
        except Exception as e:
            log.debug("RTP send failed: %s", e)
        self.seq = (self.seq + 1) & 0xFFFF
        self.timestamp = (self.timestamp + samples) & 0xFFFFFFFF
        self.tx_packets += 1
        self.tx_octets += len(payload)

    def advance(self, samples: int) -> None:
        """Advance the clock without sending (silence suppression)."""
        self.timestamp = (self.timestamp + samples) & 0xFFFFFFFF
        self._marker_pending = True

    def send_dtmf(self, digits: str, duration_ms: int = 100, gap_ms: int = 60) -> asyncio.Task | None:
        if self.dtmf_pt is None:
            return None
        prev = self._dtmf_task
        self._dtmf_task = asyncio.get_event_loop().create_task(self._dtmf_worker(prev, digits, duration_ms, gap_ms))
        return self._dtmf_task

    async def _dtmf_worker(self, prev: asyncio.Task | None, digits: str, duration_ms: int, gap_ms: int) -> None:
        if prev and not prev.done():
            try:
                await prev
            except Exception:
                pass
        step = self.clock_rate * 20 // 1000
        for ch in digits:
            if ch.upper() not in DTMF_CHARS:
                continue
            event = DTMF_CHARS.index(ch.upper())
            start_ts = self.timestamp & 0xFFFFFFFF
            total = self.clock_rate * duration_ms // 1000
            elapsed = 0
            first = True
            while elapsed < total:
                elapsed = min(elapsed + step, total)
                self._send_event(event, start_ts, elapsed, end=False, marker=first)
                first = False
                await asyncio.sleep(0.02)
            for _ in range(3):
                self._send_event(event, start_ts, total, end=True, marker=False)
            self.timestamp = (start_ts + total) & 0xFFFFFFFF
            self._marker_pending = True
            await asyncio.sleep(gap_ms / 1000)

    def _send_event(self, event: int, ts: int, duration: int, end: bool, marker: bool) -> None:
        if not self.remote or not self._transport or self.dtmf_pt is None:
            return
        payload = struct.pack("!BBH", event, (0x80 if end else 0) | 10, min(duration, 0xFFFF))
        header = struct.pack("!BBHII", 0x80, (0x80 if marker else 0) | self.dtmf_pt, self.seq, ts, self.ssrc)
        try:
            self._transport.sendto(header + payload, self.remote)
        except Exception as e:
            log.debug("RTP DTMF send failed: %s", e)
        self.seq = (self.seq + 1) & 0xFFFF
