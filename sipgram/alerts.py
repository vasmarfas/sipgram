"""Operational alerts to the administrator's Telegram chat.

Problems are reported only after they have lasted `down_after` seconds, and each state change is
reported once, so a flapping registration cannot turn into a stream of messages.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from .config import NotificationsConfig
from .messages import default_language, t

log = logging.getLogger("sipgram.alerts")


class Alerts:
    def __init__(self, cfg: NotificationsConfig, account_of: Callable[[int], object], notifier):
        self.cfg = cfg
        self.notifier = notifier
        self._account_of = account_of
        self.admin_id: int | None = None
        self._down_since: dict[str, float] = {}
        self._reported: set[str] = set()
        self._last: dict[str, float] = {}
        self._timers: dict[str, asyncio.Task] = {}
        self.lang = default_language()

    async def resolve(self, resolver: Callable[[str], Awaitable[object]]) -> None:
        """Turns the configured admin (id / @username / +phone) into a user id."""
        if not self.cfg.enabled:
            return
        try:
            entity = await resolver(self.cfg.admin)
            self.admin_id = int(getattr(entity, "user_id", 0)) or None
        except Exception as e:
            log.error("notifications: cannot resolve admin %s: %s", self.cfg.admin, e)

    async def _send(self, key: str, text: str, throttle: float = 30.0) -> None:
        if self.admin_id is None:
            return
        now = time.time()
        if now - self._last.get(key, 0.0) < throttle:
            return
        self._last[key] = now
        try:
            await self.notifier.send(self.admin_id, text)
        except Exception as e:
            log.warning("could not deliver the alert: %s", e)

    # ---- SIP registrations ----

    def registration(self, account: str, ok: bool, detail: str) -> None:
        """Called from the SIP thread of control; schedules the alert on the loop."""
        if not (self.cfg.enabled and self.cfg.registration):
            return
        key = f"reg:{account}"
        if ok:
            self._down_since.pop(key, None)
            timer = self._timers.pop(key, None)
            if timer:
                timer.cancel()
            if key in self._reported:
                self._reported.discard(key)
                asyncio.ensure_future(self._send(key, t("alert_reg_ok", self.lang, account=account), throttle=0))
            return
        if key in self._down_since:
            return
        self._down_since[key] = time.time()
        self._timers[key] = asyncio.ensure_future(self._report_down(key, account, detail))

    async def _report_down(self, key: str, account: str, detail: str) -> None:
        try:
            await asyncio.sleep(self.cfg.down_after)
        except asyncio.CancelledError:
            return
        if key not in self._down_since:
            return
        self._reported.add(key)
        await self._send(key, t("alert_reg_down", self.lang, account=account,
                                seconds=self.cfg.down_after, reason=detail), throttle=0)

    # ---- Telegram side ----

    async def telegram_problem(self, what: str, detail: str) -> None:
        if self.cfg.enabled and self.cfg.telegram:
            await self._send(f"tg:{what}", t("alert_telegram", self.lang, what=what, reason=detail))

    async def call_failed(self, user: str, peer: str, reason: str) -> None:
        if self.cfg.enabled and self.cfg.calls:
            await self._send("call", t("alert_call_failed", self.lang, user=user, peer=peer, reason=reason), throttle=0)

    async def started(self, users: int, accounts: int) -> None:
        if self.cfg.enabled and self.cfg.startup:
            await self._send("start", t("alert_started", self.lang, users=users, accounts=accounts), throttle=0)

    async def stopping(self) -> None:
        if self.cfg.enabled and self.cfg.startup:
            await self._send("stop", t("alert_stopped", self.lang), throttle=0)

    def cancel(self) -> None:
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
