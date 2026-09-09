"""SIP transports: UDP datagram and TCP/TLS stream, both feeding parsed messages to a callback."""
from __future__ import annotations

import asyncio
import logging
import ssl
from collections.abc import Awaitable, Callable

from .message import SipMessage, message_length

log = logging.getLogger("sipgram.sip.transport")

MessageHandler = Callable[[SipMessage, tuple[str, int]], Awaitable[None] | None]


class SipTransport:
    kind = "UDP"

    def __init__(self, local_ip: str, local_port: int, on_message: MessageHandler):
        self.local_ip = local_ip
        self.local_port = local_port
        self.on_message = on_message
        self.loop = asyncio.get_event_loop()
        self.on_disconnect: Callable[[], None] | None = None

    async def start(self) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        raise NotImplementedError

    async def send(self, data: bytes, addr: tuple[str, int]) -> None:
        raise NotImplementedError

    async def send_keepalive(self, addr: tuple[str, int]) -> None:
        await self.send(b"\r\n\r\n", addr)

    def _dispatch(self, data: bytes, addr: tuple[str, int]) -> None:
        if not data.strip():
            return
        try:
            msg = SipMessage.parse(data)
        except Exception as e:
            log.debug("dropping unparsable packet from %s: %s", addr, e)
            return
        result = self.on_message(msg, addr)
        if asyncio.iscoroutine(result):
            self.loop.create_task(result)


class UdpTransport(SipTransport, asyncio.DatagramProtocol):
    kind = "UDP"

    def __init__(self, local_ip: str, local_port: int, on_message: MessageHandler):
        SipTransport.__init__(self, local_ip, local_port, on_message)
        self._transport: asyncio.DatagramTransport | None = None

    async def start(self) -> None:
        transport, _ = await self.loop.create_datagram_endpoint(
            lambda: self, local_addr=(self.local_ip, self.local_port)
        )
        self._transport = transport
        self.local_port = transport.get_extra_info("sockname")[1]
        log.info("SIP UDP listening on %s:%d", self.local_ip, self.local_port)

    async def stop(self) -> None:
        if self._transport:
            self._transport.close()
            self._transport = None

    async def send(self, data: bytes, addr: tuple[str, int]) -> None:
        if self._transport:
            self._transport.sendto(data, addr)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._dispatch(data, addr)

    def error_received(self, exc: Exception) -> None:
        log.warning("UDP send/receive error: %s", exc)


class TcpTransport(SipTransport):
    """Single persistent client connection to the registrar (TCP or TLS)."""

    def __init__(
        self,
        local_ip: str,
        local_port: int,
        on_message: MessageHandler,
        remote: tuple[str, int],
        tls: bool = False,
        tls_verify: bool = True,
    ):
        super().__init__(local_ip, local_port, on_message)
        self.kind = "TLS" if tls else "TCP"
        self.remote = remote
        self.tls = tls
        self.tls_verify = tls_verify
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        await self._connect()

    async def _connect(self) -> None:
        async with self._lock:
            if self._writer and not self._writer.is_closing():
                return
            ctx = None
            if self.tls:
                ctx = ssl.create_default_context()
                if not self.tls_verify:
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
            local_addr = (self.local_ip, self.local_port) if self.local_port else None
            self._reader, self._writer = await asyncio.open_connection(
                self.remote[0], self.remote[1], ssl=ctx, local_addr=local_addr,
                server_hostname=self.remote[0] if self.tls else None,
            )
            sock = self._writer.get_extra_info("sockname")
            if sock:
                self.local_port = sock[1]
            log.info("SIP %s connected to %s:%d from port %d", self.kind, self.remote[0], self.remote[1], self.local_port)
            self._reader_task = self.loop.create_task(self._read_loop(self._reader))

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        buf = b""
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                buf += chunk
                while True:
                    stripped = buf.lstrip(b"\r\n")
                    if len(stripped) != len(buf):
                        buf = stripped
                    n = message_length(buf)
                    if n is None:
                        break
                    packet, buf = buf[:n], buf[n:]
                    self._dispatch(packet, self.remote)
        except (asyncio.CancelledError, ConnectionError, ssl.SSLError):
            pass
        except Exception as e:
            log.warning("%s read loop error: %s", self.kind, e)
        finally:
            log.warning("SIP %s connection to %s:%d closed", self.kind, *self.remote)
            self._writer = None
            self._reader = None
            if self.on_disconnect:
                self.on_disconnect()

    async def stop(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
        if self._writer:
            self._writer.close()
            self._writer = None

    async def send(self, data: bytes, addr: tuple[str, int]) -> None:
        if not self._writer or self._writer.is_closing():
            await self._connect()
        assert self._writer is not None
        self._writer.write(data)
        await self._writer.drain()
