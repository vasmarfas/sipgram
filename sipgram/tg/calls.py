"""Telegram private (P2P) calls on top of NTgCalls + Telethon raw API.

Flow (outgoing):  create_p2p_call -> init_exchange -> phone.requestCall -> phoneCallAccepted(g_b)
                  -> exchange_keys -> phone.confirmCall -> connect_p2p -> CONNECTED
Flow (incoming):  phoneCallRequested(g_a_hash) -> phone.receivedCall -> [accept] create_p2p_call
                  -> init_exchange(g_a_hash) -> phone.acceptCall(g_b) -> phoneCall(g_a, fingerprint)
                  -> exchange_keys -> connect_p2p -> CONNECTED
Audio is exchanged as 10 ms PCM16 frames at `sample_rate` (NTgCalls resamples to Opus internally).
"""
from __future__ import annotations

import asyncio
import enum
import logging
import random
import time
from collections.abc import Awaitable, Callable

import ntgcalls
from telethon import utils
from telethon.errors import RPCError
from telethon.tl import functions, types

from .account import TgAccount
from .group import GROUP_RATE, TgGroupCall, input_group_call

log = logging.getLogger("sipgram.tg.calls")

CONNECT_TIMEOUT = 20.0


class TgCallError(Exception):
    def __init__(self, reason: str, sip_code: int = 480):
        super().__init__(reason)
        self.reason = reason
        self.sip_code = sip_code


class TgCallState(enum.Enum):
    NEW = "new"
    REQUESTING = "requesting"
    RINGING = "ringing"
    INCOMING = "incoming"
    ACCEPTING = "accepting"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ENDED = "ended"


def _map_rpc_error(e: RPCError) -> TgCallError:
    msg = str(e)
    name = getattr(e, "message", "") or msg
    if "PRIVACY" in name:
        return TgCallError("user's privacy settings do not allow calls from the gateway account", 403)
    if "BLOCKED" in name:
        return TgCallError("the user has blocked the gateway account", 403)
    if "FLOOD" in name:
        return TgCallError(f"telegram flood wait: {msg}", 503)
    if "OUTDATED" in name or "LAYER" in name:
        return TgCallError(f"telegram client/protocol mismatch: {msg}", 480)
    return TgCallError(f"telegram error: {msg}", 480)


class TgCall:
    def __init__(self, engine: TgCallEngine, user_id: int, outgoing: bool, sample_rate: int):
        self.engine = engine
        self.user_id = user_id
        self.outgoing = outgoing
        self.sample_rate = sample_rate
        self.state = TgCallState.NEW
        self.call_id: int | None = None
        self.access_hash: int | None = None
        self.g_a_hash: bytes | None = None
        self.video = False
        self.created = time.time()
        self.connected_at = 0.0
        self.end_reason = ""
        self.ended: asyncio.Future = engine.loop.create_future()
        self.connected: asyncio.Future = engine.loop.create_future()
        self._accepted: asyncio.Future = engine.loop.create_future()
        self._confirmed: asyncio.Future = engine.loop.create_future()
        self._media_ready = False
        self._sig_in: list[bytes] = []
        self._sig_out: asyncio.Queue = asyncio.Queue()
        self._sig_task: asyncio.Task | None = None
        self.on_state: Callable[[TgCall, TgCallState], None] | None = None
        self.on_audio: Callable[[bytes], None] | None = None
        self.frames_in = 0
        self.frames_out = 0
        self.library_version = ""

    @property
    def active(self) -> bool:
        return self.state != TgCallState.ENDED

    @property
    def peer(self) -> types.InputPhoneCall:
        assert self.call_id is not None and self.access_hash is not None
        return types.InputPhoneCall(id=self.call_id, access_hash=self.access_hash)

    def _set_state(self, state: TgCallState) -> None:
        if self.state == state or self.state == TgCallState.ENDED:
            return
        self.state = state
        if state == TgCallState.CONNECTED:
            self.connected_at = time.time()
        log.info("tg call with %s: %s", self.user_id, state.value)
        if self.on_state:
            try:
                self.on_state(self, state)
            except Exception:
                log.exception("tg on_state failed")

    def frame_bytes(self) -> int:
        return self.sample_rate * 2 // 100

    def send_audio(self, pcm10ms: bytes) -> None:
        """Push one 10 ms PCM16 frame toward Telegram (call from the event loop thread)."""
        if not self._media_ready or self.state == TgCallState.ENDED:
            return
        try:
            fut = self.engine.ntg.send_external_frame(
                self.user_id, ntgcalls.StreamDevice.MICROPHONE, pcm10ms, ntgcalls.FrameData(0, 0, 0, 0)
            )
            fut.add_done_callback(self._frame_done)
            self.frames_out += 1
        except Exception as e:
            log.debug("send_external_frame failed: %s", e)

    @staticmethod
    def _frame_done(fut: asyncio.Future) -> None:
        if not fut.cancelled() and fut.exception() is not None:
            log.debug("external frame error: %s", fut.exception())

    async def set_sample_rate(self, rate: int) -> None:
        if rate == self.sample_rate:
            return
        self.sample_rate = rate
        if self._media_ready or self.state in (TgCallState.CONNECTING, TgCallState.CONNECTED):
            await self.engine._configure_media(self)

    async def hangup(self, reason: str = "hangup") -> None:
        await self.engine._end_call(self, reason, local=True)

    async def accept(self) -> None:
        await self.engine._accept(self)


class TgCallEngine:
    def __init__(self, account: TgAccount, default_rate: int = 8000):
        self.account = account
        self.loop = asyncio.get_event_loop()
        self.default_rate = default_rate
        self.ntg = ntgcalls.NTgCalls()
        self.calls: dict[int, TgCall] = {}
        self.groups: dict[int, TgGroupCall] = {}
        self.on_incoming: Callable[[TgCall], Awaitable[None] | None] | None = None
        self._protocol = ntgcalls.NTgCalls.get_protocol()
        self.ntg.on_connection_change(self._on_connection_change)
        self.ntg.on_frames(self._on_frames)
        self.ntg.on_signaling(self._on_signaling)
        account.add_raw_handler(self._on_raw_update)

    @property
    def library_versions(self) -> list[str]:
        return list(self._protocol.library_versions)

    def tl_protocol(self) -> types.PhoneCallProtocol:
        return types.PhoneCallProtocol(
            min_layer=self._protocol.min_layer, max_layer=self._protocol.max_layer,
            udp_p2p=self._protocol.udp_p2p, udp_reflector=self._protocol.udp_reflector,
            library_versions=self.library_versions,
        )

    def call_for(self, user_id: int) -> TgCall | None:
        return self.calls.get(user_id)

    def _by_call_id(self, call_id: int) -> TgCall | None:
        for c in self.calls.values():
            if c.call_id == call_id:
                return c
        return None

    # ---- media ----

    async def _configure_media(self, call: TgCall) -> None:
        desc = ntgcalls.AudioDescription(ntgcalls.MediaSource.EXTERNAL, call.sample_rate, 1, "", False)
        await self.ntg.set_stream_sources(call.user_id, ntgcalls.StreamMode.CAPTURE, ntgcalls.MediaDescription(microphone=desc))
        await self.ntg.set_stream_sources(call.user_id, ntgcalls.StreamMode.PLAYBACK, ntgcalls.MediaDescription(microphone=desc))
        call._media_ready = True

    # ---- group calls (voice chats) ----

    async def join_group(self, chat: str, title: str = "") -> TgGroupCall:
        """Joins (or starts) the voice chat of `chat` and returns once media is connected."""
        call, peer, name = await input_group_call(self.account, chat, title=title)
        key = utils.get_peer_id(peer)
        existing = self.groups.get(key)
        if existing is not None and existing.active:
            return existing
        group = TgGroupCall(self, key, call, peer, name)
        self.groups[key] = group
        try:
            payload = await self.ntg.create_call(key)
            desc = ntgcalls.AudioDescription(ntgcalls.MediaSource.EXTERNAL, GROUP_RATE, 1, "", False)
            media = ntgcalls.MediaDescription(microphone=desc)
            await self.ntg.set_stream_sources(key, ntgcalls.StreamMode.CAPTURE, media)
            await self.ntg.set_stream_sources(key, ntgcalls.StreamMode.PLAYBACK, media)
            try:
                result = await self.account.invoke(functions.phone.JoinGroupCallRequest(
                    call=call, params=types.DataJSON(data=payload), muted=False,
                    video_stopped=True, join_as=types.InputPeerSelf(),
                ))
            except RPCError as e:
                raise _map_rpc_error(e) from e
            params = self._join_params(result)
            if params is None:
                raise TgCallError("telegram did not return the voice chat connection parameters", 480)
            await self.ntg.connect(key, params, False)
            group.joined = True
            try:
                await asyncio.wait_for(asyncio.shield(group.connected), CONNECT_TIMEOUT)
            except asyncio.TimeoutError:
                raise TgCallError("voice chat media did not connect", 480) from None
            log.info("joined the voice chat of %s (%d)", name, key)
            return group
        except TgCallError:
            await self.leave_group(group, "failed")
            raise
        except Exception as e:
            log.exception("joining the voice chat failed")
            await self.leave_group(group, "failed")
            raise TgCallError(f"voice chat: {e}", 480) from e

    @staticmethod
    def _join_params(result) -> str | None:
        for update in getattr(result, "updates", []):
            if isinstance(update, types.UpdateGroupCallConnection):
                return update.params.data
        return None

    async def leave_group(self, group: TgGroupCall, reason: str = "hangup") -> None:
        if not group.active:
            return
        if group.joined:
            try:
                await self.account.invoke(functions.phone.LeaveGroupCallRequest(call=group.call, source=0))
            except RPCError as e:
                log.debug("leaveGroupCall failed: %s", e)
        group.joined = False
        try:
            await self.ntg.stop(group.key)
        except Exception:
            pass
        if not group.connected.done():
            group.connected.set_exception(TgCallError(reason))
            group.connected.exception()
        if not group.ended.done():
            group.ended.set_result(reason)
        if self.groups.get(group.key) is group:
            self.groups.pop(group.key, None)
        log.info("left the voice chat of %s (%s)", group.title, reason)

    async def _dh_config(self) -> ntgcalls.DhConfig:
        dh = await self.account.invoke(functions.messages.GetDhConfigRequest(version=0, random_length=256))
        if not isinstance(dh, types.messages.DhConfig):
            raise TgCallError("unexpected DH config response", 500)
        return ntgcalls.DhConfig(dh.g, dh.p, dh.random)

    @staticmethod
    def _servers(connections) -> list[ntgcalls.RTCServer]:
        out = []
        for c in connections:
            if isinstance(c, types.PhoneConnectionWebrtc):
                out.append(ntgcalls.RTCServer(c.id, c.ip, c.ipv6, c.port, c.username, c.password,
                                              bool(c.turn), bool(c.stun), False, None))
            elif isinstance(c, types.PhoneConnection):
                out.append(ntgcalls.RTCServer(c.id, c.ip, c.ipv6, c.port, None, None, True, False, bool(c.tcp), c.peer_tag))
        return out

    async def _connect_media(self, call: TgCall, pc: types.PhoneCall) -> None:
        versions = list(pc.protocol.library_versions)
        call.library_version = max(versions, key=lambda v: [int(x) for x in v.split(".")]) if versions else "?"
        call._set_state(TgCallState.CONNECTING)
        await self.ntg.connect_p2p(call.user_id, self._servers(pc.connections), versions, bool(pc.p2p_allowed))
        call._sig_task = self.loop.create_task(self._signaling_pump(call))
        for data in call._sig_in:
            try:
                await self.ntg.send_signaling(call.user_id, data)
            except Exception as e:
                log.debug("replaying signaling failed: %s", e)
        call._sig_in.clear()
        try:
            await asyncio.wait_for(asyncio.shield(call.connected), CONNECT_TIMEOUT)
        except asyncio.TimeoutError:
            raise TgCallError("telegram media did not connect (network/relay problem)", 480) from None

    async def _signaling_pump(self, call: TgCall) -> None:
        while call.active:
            data = await call._sig_out.get()
            if data is None:
                return
            if call.call_id is None:
                continue
            try:
                await self.account.invoke(functions.phone.SendSignalingDataRequest(peer=call.peer, data=data))
            except RPCError as e:
                log.debug("sendSignalingData failed: %s", e)
            except Exception as e:
                log.debug("sendSignalingData error: %s", e)

    # ---- outgoing ----

    async def call(self, input_user: types.InputUser, sample_rate: int = 0, ring_timeout: float = 45.0) -> TgCall:
        """Calls a Telegram user and returns once media is connected. Raises TgCallError otherwise."""
        uid = input_user.user_id
        if uid in self.calls and self.calls[uid].active:
            raise TgCallError("already in a call with this user", 486)
        call = TgCall(self, uid, outgoing=True, sample_rate=sample_rate or self.default_rate)
        self.calls[uid] = call
        try:
            await self.ntg.create_p2p_call(uid)
            await self._configure_media(call)
            g_a_hash = await self.ntg.init_exchange(uid, await self._dh_config(), None)
            call._set_state(TgCallState.REQUESTING)
            try:
                result = await self.account.invoke(functions.phone.RequestCallRequest(
                    user_id=input_user, g_a_hash=g_a_hash, protocol=self.tl_protocol(),
                    video=False, random_id=random.randint(1, 0x7FFFFFFE),
                ))
            except RPCError as e:
                raise _map_rpc_error(e) from e
            pc = result.phone_call
            call.call_id = pc.id
            call.access_hash = pc.access_hash
            try:
                g_b = await asyncio.wait_for(asyncio.shield(call._accepted), ring_timeout)
            except asyncio.TimeoutError:
                raise TgCallError("no answer", 480) from None
            call._set_state(TgCallState.ACCEPTING)
            auth = await self.ntg.exchange_keys(uid, g_b, 0)
            try:
                confirmed = await self.account.invoke(functions.phone.ConfirmCallRequest(
                    peer=call.peer, g_a=auth.g_a_or_b, key_fingerprint=auth.key_fingerprint, protocol=self.tl_protocol(),
                ))
            except RPCError as e:
                raise _map_rpc_error(e) from e
            pc2 = confirmed.phone_call
            if not isinstance(pc2, types.PhoneCall):
                raise TgCallError(f"unexpected confirmCall result {type(pc2).__name__}", 480)
            await self._connect_media(call, pc2)
            return call
        except TgCallError as e:
            if call.active:
                await self._end_call(call, e.reason, local=True)
            raise
        except Exception as e:
            log.exception("outgoing telegram call failed")
            if call.active:
                await self._end_call(call, "failed", local=True)
            raise TgCallError(f"call setup failed: {e}", 480) from e

    # ---- incoming ----

    async def _accept(self, call: TgCall) -> None:
        if call.state != TgCallState.INCOMING:
            raise TgCallError("call is not in incoming state", 480)
        uid = call.user_id
        try:
            call._set_state(TgCallState.ACCEPTING)
            await self.ntg.create_p2p_call(uid)
            await self._configure_media(call)
            g_b = await self.ntg.init_exchange(uid, await self._dh_config(), call.g_a_hash)
            try:
                await self.account.invoke(functions.phone.AcceptCallRequest(peer=call.peer, g_b=g_b, protocol=self.tl_protocol()))
            except RPCError as e:
                raise _map_rpc_error(e) from e
            try:
                pc = await asyncio.wait_for(asyncio.shield(call._confirmed), CONNECT_TIMEOUT)
            except asyncio.TimeoutError:
                raise TgCallError("caller did not confirm the call", 480) from None
            await self.ntg.exchange_keys(uid, pc.g_a_or_b, pc.key_fingerprint)
            await self._connect_media(call, pc)
        except TgCallError as e:
            if call.active:
                await self._end_call(call, e.reason, local=True)
            raise
        except Exception as e:
            log.exception("accepting telegram call failed")
            if call.active:
                await self._end_call(call, "failed", local=True)
            raise TgCallError(f"accept failed: {e}", 480) from e

    async def cancel(self, user_id: int, reason: str = "missed") -> None:
        call = self.calls.get(user_id)
        if call is not None and call.active:
            await self._end_call(call, reason, local=True)

    # ---- teardown ----

    async def _discard(self, call: TgCall, reason) -> None:
        if call.call_id is None:
            return
        duration = int(time.time() - call.connected_at) if call.connected_at else 0
        try:
            await self.account.invoke(functions.phone.DiscardCallRequest(
                peer=call.peer, duration=duration, reason=reason, connection_id=0, video=False,
            ))
        except RPCError as e:
            log.debug("discardCall failed: %s", e)

    async def _end_call(self, call: TgCall, reason: str, local: bool, notify_peer: bool = True) -> None:
        if call.state == TgCallState.ENDED:
            return
        call.end_reason = reason
        call._media_ready = False
        if local and notify_peer:
            tl_reason = {
                "busy": types.PhoneCallDiscardReasonBusy(),
                "missed": types.PhoneCallDiscardReasonMissed(),
                "no answer": types.PhoneCallDiscardReasonMissed(),
                "disconnect": types.PhoneCallDiscardReasonDisconnect(),
            }.get(reason, types.PhoneCallDiscardReasonHangup())
            await self._discard(call, tl_reason)
        try:
            await self.ntg.stop(call.user_id)
        except Exception:
            pass
        if call._sig_task:
            call._sig_out.put_nowait(None)
            call._sig_task = None
        for fut in (call._accepted, call._confirmed, call.connected):
            if not fut.done():
                fut.set_exception(TgCallError(reason))
                fut.exception()
        if not call.ended.done():
            call.ended.set_result(reason)
        if self.calls.get(call.user_id) is call:
            self.calls.pop(call.user_id, None)
        call._set_state(TgCallState.ENDED)

    # ---- NTgCalls callbacks (worker threads) ----

    def _on_connection_change(self, user_id: int, info) -> None:
        state = info.state
        name = getattr(state, "name", str(state))
        self.loop.call_soon_threadsafe(self._connection_changed, int(user_id), name)

    def _connection_changed(self, user_id: int, name: str) -> None:
        group = self.groups.get(user_id)
        if group is not None:
            log.info("voice chat %s: media %s", group.title, name)
            if name == "CONNECTED" and not group.connected.done():
                group.connected.set_result(True)
            elif name in ("FAILED", "TIMEOUT", "CLOSED"):
                self.loop.create_task(self.leave_group(group, "disconnect" if name != "CLOSED" else "hangup"))
            return
        call = self.calls.get(user_id)
        if call is None:
            return
        log.info("tg call with %s: media %s", user_id, name)
        if name == "CONNECTED":
            if not call.connected.done():
                call.connected.set_result(True)
            call._set_state(TgCallState.CONNECTED)
        elif name in ("FAILED", "TIMEOUT", "CLOSED"):
            if call.state == TgCallState.ENDED:
                return
            self.loop.create_task(self._end_call(call, "disconnect" if name != "CLOSED" else "hangup", local=True))

    def _on_frames(self, user_id: int, mode, device, frames) -> None:
        if mode != ntgcalls.StreamMode.PLAYBACK:
            return
        group = self.groups.get(int(user_id))
        if group is not None:
            if group.on_audio is None:
                return
            for f in frames:
                data = f.data
                if data:
                    group.frames_in += 1
                    group.on_audio(int(getattr(f, "ssrc", 0)), bytes(data))
            return
        call = self.calls.get(int(user_id))
        if call is None or call.on_audio is None:
            return
        for f in frames:
            data = f.data
            if data:
                call.frames_in += 1
                call.on_audio(bytes(data))

    def _on_signaling(self, user_id: int, data: bytes) -> None:
        self.loop.call_soon_threadsafe(self._queue_signaling, int(user_id), bytes(data))

    def _queue_signaling(self, user_id: int, data: bytes) -> None:
        call = self.calls.get(user_id)
        if call is not None and call.active:
            call._sig_out.put_nowait(data)

    # ---- MTProto updates ----

    async def _on_raw_update(self, update) -> None:
        if isinstance(update, types.UpdatePhoneCallSignalingData):
            call = self._by_call_id(update.phone_call_id)
            if call is None:
                return
            if call.state in (TgCallState.CONNECTING, TgCallState.CONNECTED) and call._sig_task is not None:
                try:
                    await self.ntg.send_signaling(call.user_id, update.data)
                except Exception as e:
                    log.debug("send_signaling failed: %s", e)
            else:
                call._sig_in.append(bytes(update.data))
            return
        if isinstance(update, types.UpdateGroupCall):
            call_id = getattr(update.call, "id", None)
            group = next((g for g in self.groups.values() if g.call.id == call_id), None)
            if group is not None and isinstance(update.call, types.GroupCallDiscarded):
                await self.leave_group(group, "hangup")
            return
        if not isinstance(update, types.UpdatePhoneCall):
            return
        pc = update.phone_call
        if isinstance(pc, types.PhoneCallRequested):
            await self._incoming_requested(pc)
        elif isinstance(pc, types.PhoneCallWaiting):
            call = self._by_call_id(pc.id)
            if call and call.outgoing and pc.receive_date and call.state == TgCallState.REQUESTING:
                call._set_state(TgCallState.RINGING)
        elif isinstance(pc, types.PhoneCallAccepted):
            call = self._by_call_id(pc.id)
            if call and not call._accepted.done():
                call._accepted.set_result(pc.g_b)
        elif isinstance(pc, types.PhoneCall):
            call = self._by_call_id(pc.id)
            if call and not call._confirmed.done():
                call._confirmed.set_result(pc)
        elif isinstance(pc, types.PhoneCallDiscarded):
            call = self._by_call_id(pc.id)
            if call is None:
                return
            reason = type(pc.reason).__name__.replace("PhoneCallDiscardReason", "").lower() if pc.reason else "hangup"
            log.info("tg call with %s discarded by peer: %s", call.user_id, reason)
            await self._end_call(call, reason, local=False)

    async def _incoming_requested(self, pc: types.PhoneCallRequested) -> None:
        uid = pc.admin_id
        existing = self.calls.get(uid)
        if existing and existing.active:
            if existing.call_id == pc.id:
                return
            log.info("second call from %s while one is active; declining busy", uid)
            try:
                await self.account.invoke(functions.phone.DiscardCallRequest(
                    peer=types.InputPhoneCall(id=pc.id, access_hash=pc.access_hash), duration=0,
                    reason=types.PhoneCallDiscardReasonBusy(), connection_id=0, video=False))
            except RPCError:
                pass
            return
        call = TgCall(self, uid, outgoing=False, sample_rate=self.default_rate)
        call.call_id = pc.id
        call.access_hash = pc.access_hash
        call.g_a_hash = pc.g_a_hash
        call.video = bool(pc.video)
        self.calls[uid] = call
        call._set_state(TgCallState.INCOMING)
        try:
            await self.account.invoke(functions.phone.ReceivedCallRequest(peer=call.peer))
        except RPCError as e:
            log.debug("receivedCall failed: %s", e)
        if self.on_incoming:
            r = self.on_incoming(call)
            if asyncio.iscoroutine(r):
                self.loop.create_task(r)
        else:
            await self._end_call(call, "busy", local=True)
