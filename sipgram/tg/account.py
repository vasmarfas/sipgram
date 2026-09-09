"""Telethon client wrapper for one gateway account."""
from __future__ import annotations

import asyncio
import io
import logging
import platform
import random
from collections.abc import Awaitable, Callable

from telethon import TelegramClient, events
from telethon.errors import RPCError
from telethon.tl import functions, types

from ..config import TelegramAppConfig

log = logging.getLogger("sipgram.tg")


def _voice_file(ogg: bytes) -> io.BytesIO:
    buf = io.BytesIO(ogg)
    buf.name = "call.ogg"
    return buf


class NotAuthorized(Exception):
    pass


class TgAccount:
    def __init__(self, app: TelegramAppConfig, session: str, phone: str = ""):
        app.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.session_path = app.sessions_dir / session
        self.phone = phone
        self.client = TelegramClient(
            str(self.session_path), app.api_id, app.api_hash,
            device_model=app.device_model, app_version=app.app_version,
            system_version=f"{platform.system()} {platform.release()}",
        )
        self.me: types.User | None = None
        self._raw_handlers: list[Callable[[object], Awaitable[None] | None]] = []
        self._message_handlers: list[Callable[[int, str, object], Awaitable[None] | None]] = []
        self._entity_cache: dict[str, types.InputUser] = {}

    async def connect(self) -> None:
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise NotAuthorized(f"session {self.session_path} is not logged in; run: sipgram login")
        self.me = await self.client.get_me()
        assert self.me is not None
        log.info("telegram: logged in as %s (id=%s, phone=%s)", self._name(self.me), self.me.id, self.me.phone)
        self.client.add_event_handler(self._on_raw, events.Raw())
        self.client.add_event_handler(self._on_message, events.NewMessage(incoming=True))

    async def login_interactive(self) -> None:
        await self.client.start(phone=lambda: self.phone or input("Gateway account phone (+7...): "))
        self.me = await self.client.get_me()
        assert self.me is not None
        print(f"Logged in as {self._name(self.me)} (id={self.me.id}), session saved to {self.session_path}.session")

    async def disconnect(self) -> None:
        try:
            await self.client.disconnect()
        except Exception:
            pass

    @staticmethod
    def _name(user: types.User) -> str:
        parts = [user.first_name or "", user.last_name or ""]
        name = " ".join(p for p in parts if p).strip()
        if user.username:
            name += f" @{user.username}"
        return name or str(user.id)

    def add_raw_handler(self, handler: Callable[[object], Awaitable[None] | None]) -> None:
        self._raw_handlers.append(handler)

    def add_message_handler(self, handler: Callable[[int, str, object], Awaitable[None] | None]) -> None:
        self._message_handlers.append(handler)

    async def _on_raw(self, update) -> None:
        for h in self._raw_handlers:
            try:
                r = h(update)
                if asyncio.iscoroutine(r):
                    await r
            except Exception:
                log.exception("raw update handler failed")

    async def _on_message(self, event) -> None:
        if not event.is_private:
            return
        sender_id = event.sender_id
        if sender_id is None:
            return
        text = event.raw_text or ""
        for h in self._message_handlers:
            try:
                r = h(sender_id, text, event)
                if asyncio.iscoroutine(r):
                    await r
            except Exception:
                log.exception("message handler failed")

    async def invoke(self, request):
        return await self.client(request)

    async def send_text(self, user_id: int, text: str) -> None:
        try:
            await self.client.send_message(user_id, text)
        except RPCError as e:
            log.warning("send_message to %s failed: %s", user_id, e)

    async def send_voice(self, user_id: int, ogg: bytes, duration: int, caption: str = "") -> bool:
        """Sends an in-memory Ogg/Opus buffer as a Telegram voice message."""
        try:
            await self.client.send_file(
                user_id, _voice_file(ogg), caption=caption or None, voice_note=True,
                attributes=[types.DocumentAttributeAudio(duration=duration, voice=True)],
            )
            return True
        except Exception as e:
            log.warning("could not send the voice message to %s: %s", user_id, e)
            return False

    async def resolve_user(self, spec: str) -> types.InputUser:
        """Accepts a numeric id, @username or +phone. Phone numbers are imported as contacts."""
        spec = str(spec).strip()
        if spec in self._entity_cache:
            return self._entity_cache[spec]
        if spec.startswith("+"):
            entity = await self._import_phone(spec)
        elif spec.startswith("@"):
            entity = await self.client.get_input_entity(spec)
        elif spec.lstrip("-").isdigit():
            try:
                entity = await self.client.get_input_entity(int(spec))
            except ValueError:
                await self.client.get_dialogs(limit=200)
                entity = await self.client.get_input_entity(int(spec))
        else:
            entity = await self.client.get_input_entity(spec)
        if isinstance(entity, types.InputPeerUser):
            entity = types.InputUser(user_id=entity.user_id, access_hash=entity.access_hash)
        if not isinstance(entity, types.InputUser):
            raise ValueError(f"{spec} is not a Telegram user")
        self._entity_cache[spec] = entity
        return entity

    async def _import_phone(self, phone: str) -> types.InputUser:
        result = await self.client(functions.contacts.ImportContactsRequest(contacts=[
            types.InputPhoneContact(client_id=random.getrandbits(63), phone=phone, first_name=phone, last_name="")
        ]))
        for u in result.users:
            return types.InputUser(user_id=u.id, access_hash=u.access_hash)
        raise ValueError(f"{phone} is not registered in Telegram or hides the number")

    async def ensure_contact(self, user: types.InputUser, name: str = "") -> None:
        """Adds the user to the gateway's contacts so their calls are accepted with default privacy."""
        try:
            entity = await self.client.get_entity(user)
        except Exception:
            entity = None
        if isinstance(entity, types.User) and entity.contact:
            return
        first = name or (entity.first_name if isinstance(entity, types.User) and entity.first_name else str(user.user_id))
        try:
            await self.client(functions.contacts.AddContactRequest(
                id=user, first_name=first, last_name="", phone="", add_phone_privacy_exception=False,
            ))
            log.info("telegram: added %s to contacts", first)
        except RPCError as e:
            log.warning("telegram: could not add %s to contacts: %s", user.user_id, e)

    async def user_display(self, user_id: int) -> str:
        try:
            entity = await self.client.get_entity(user_id)
            if isinstance(entity, types.User):
                return self._name(entity)
        except Exception:
            pass
        return str(user_id)
