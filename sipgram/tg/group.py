"""Telegram group calls (voice chats).

`/conf` builds a conference on the PBX, which is the right place for internal numbers. A voice chat
is the other direction: it lets people who have no extension, and are only reachable in Telegram,
join the same conversation. The gateway joins the voice chat of one configured group and mixes the
SIP legs into it.

Flow: create_call(chat_id) -> payload, set_stream_sources, phone.joinGroupCall(params=payload),
UpdateGroupCallConnection.params -> connect(chat_id, params, False).
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable

import ntgcalls
from telethon import utils
from telethon.errors import RPCError
from telethon.tl import functions, types

log = logging.getLogger("sipgram.tg.group")

GROUP_RATE = 48000


async def input_group_call(account, chat: str, create: bool = True, title: str = "") -> tuple[types.InputGroupCall, object, str]:
    """Returns the voice chat of `chat`, starting one when the group has none."""
    entity = await account.client.get_entity(chat)
    peer = utils.get_input_peer(entity)
    name = getattr(entity, "title", None) or str(chat)
    if isinstance(peer, types.InputPeerUser):
        raise ValueError(f"{chat} is a user, not a group: a voice chat needs a group or a channel")
    call = await _existing_call(account, entity)
    if call is not None:
        return call, peer, name
    if not create:
        raise ValueError(f"{name} has no active voice chat")
    result = await account.invoke(functions.phone.CreateGroupCallRequest(
        peer=peer, random_id=random.randint(1, 0x7FFFFFFE), title=title or None,
    ))
    for update in getattr(result, "updates", []):
        if isinstance(update, types.UpdateGroupCall) and isinstance(update.call, types.GroupCall):
            log.info("started a voice chat in %s", name)
            return types.InputGroupCall(id=update.call.id, access_hash=update.call.access_hash), peer, name
    raise ValueError(f"could not start a voice chat in {name}")


async def _existing_call(account, entity) -> types.InputGroupCall | None:
    if isinstance(entity, types.Channel):
        full = await account.invoke(functions.channels.GetFullChannelRequest(channel=entity))
    else:
        full = await account.invoke(functions.messages.GetFullChatRequest(chat_id=entity.id))
    call = getattr(full.full_chat, "call", None)
    return call if isinstance(call, types.InputGroupCall) else None


class TgGroupCall:
    """One voice chat the gateway takes part in. Audio is exchanged as 10 ms PCM16 at 48 kHz."""

    def __init__(self, engine, key: int, call: types.InputGroupCall, peer, title: str):
        self.engine = engine
        self.key = key
        self.call = call
        self.peer = peer
        self.title = title
        self.sample_rate = GROUP_RATE
        self.joined = False
        self.created = time.time()
        self.ended: asyncio.Future = engine.loop.create_future()
        self.connected: asyncio.Future = engine.loop.create_future()
        self.on_audio: Callable[[int, bytes], None] | None = None
        self.participants = 0
        self.frames_in = 0
        self.frames_out = 0

    @property
    def active(self) -> bool:
        return not self.ended.done()

    def frame_bytes(self) -> int:
        return self.sample_rate * 2 // 100

    def send_audio(self, pcm10ms: bytes) -> None:
        if not self.joined or not self.active:
            return
        try:
            fut = self.engine.ntg.send_external_frame(
                self.key, ntgcalls.StreamDevice.MICROPHONE, pcm10ms, ntgcalls.FrameData(0, 0, 0, 0))
            fut.add_done_callback(lambda f: f.cancelled() or f.exception())
            self.frames_out += 1
        except Exception as e:
            log.debug("group send_external_frame failed: %s", e)

    async def invite(self, users: list[types.InputUser]) -> list[str]:
        """Rings the voice chat on those users' phones. Returns the ones Telegram refused."""
        failed: list[str] = []
        for user in users:
            try:
                await self.engine.account.invoke(functions.phone.InviteToGroupCallRequest(call=self.call, users=[user]))
            except RPCError as e:
                log.info("cannot invite %s into the voice chat: %s", user.user_id, e)
                failed.append(str(user.user_id))
        return failed

    async def leave(self, reason: str = "hangup") -> None:
        await self.engine.leave_group(self, reason)
