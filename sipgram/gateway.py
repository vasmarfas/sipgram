from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import time

from .alerts import Alerts
from .api import HttpApi
from .config import Config
from .history import CallHistory
from .manager import CallManager, GatewayRuntime
from .messages import default_language, set_language
from .prefs import UserPrefs
from .tg.account import TgAccount
from .tg.bot import BotUi
from .tg.calls import TgCallEngine
from .tg.notify import Notifier
from .util import raise_timer_resolution

log = logging.getLogger("sipgram.gateway")
STATE_INTERVAL = 30


class Gateway:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        set_language(cfg.telegram.language)
        self.accounts: dict[str, TgAccount] = {
            g.name: TgAccount(cfg.telegram, g.session) for g in cfg.telegram.gateways
        }
        self.bot: BotUi | None = BotUi(cfg.telegram, cfg.state_dir) if cfg.telegram.bot_token else None
        self.prefs = UserPrefs(cfg.state_dir / "prefs.json")
        self.notifier = Notifier(self._account_of, self.bot, lang_of=self._lang_of)
        self.history = CallHistory(cfg.state_dir / "history.json", cfg.calls.history_size)
        self.alerts = Alerts(cfg.notifications, self._account_of, self.notifier)
        self.manager: CallManager | None = None
        self.api: HttpApi | None = None
        self.stop_event = asyncio.Event()

    async def run(self) -> int:
        raise_timer_resolution()
        loop = asyncio.get_event_loop()
        if sys.platform != "win32":
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self.stop_event.set)
        for note in self.cfg.notes:
            log.warning("%s", note)
        try:
            gateways: list[GatewayRuntime] = []
            for gcfg in self.cfg.telegram.gateways:
                account = self.accounts[gcfg.name]
                await account.connect()
                gateways.append(GatewayRuntime(gcfg, account, TgCallEngine(account)))
            self.manager = CallManager(self.cfg, gateways, self.notifier, self.history, self.prefs)
            self.manager.alerts = self.alerts
            rate = self.manager.default_rate()
            for gw in gateways:
                gw.engine.default_rate = rate
            if self.bot is not None:
                self.bot.on_command = self.manager.handle_text
                self.bot.card_provider = self.manager.card_for
                self.bot.lang_of = self._lang_of
                await self.bot.start()
            await self.manager.start()
            if len(self.manager.users) == len(self.cfg.users):
                self.history.prune(self.manager.users)
                self.prefs.prune(self.manager.users)
            self.alerts.lang = self.cfg.telegram.language
            await self.alerts.resolve(gateways[0].account.resolve_user)
            if self.cfg.api.enabled:
                self.api = HttpApi(self.cfg.api, self.manager)
                await self.api.start()
        except Exception as e:
            log.error("gateway failed to start: %s", e)
            await self._shutdown()
            return 1
        log.info("gateway running: %d telegram account(s), %d user(s), %d SIP account(s); press Ctrl+C to stop",
                 len(gateways), len(self.manager.users), len(self.manager.accounts))
        await self.alerts.started(len(self.manager.users), len(self.manager.accounts))
        state_task = asyncio.get_event_loop().create_task(self._state_writer())
        try:
            await self.stop_event.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            state_task.cancel()
            log.info("shutting down")
            await self.alerts.stopping()
            await self._shutdown()
        return 0

    def _account_of(self, user_id: int) -> TgAccount:
        if self.manager is not None:
            return self.manager.account_of(user_id)
        return next(iter(self.accounts.values()))

    def _lang_of(self, user_id: int) -> str:
        return self.manager.lang_of(user_id) if self.manager else default_language()

    async def _shutdown(self) -> None:
        if self.api is not None:
            await self.api.stop()
        if self.manager is not None:
            try:
                await self.manager.stop()
            except Exception:
                log.exception("error stopping call manager")
        if self.bot is not None:
            await self.bot.stop()
        for account in self.accounts.values():
            await account.disconnect()

    async def _state_writer(self) -> None:
        path = self.cfg.state_dir / "state.json"
        while True:
            try:
                snap = self.manager.snapshot() if self.manager else {}
                snap["ts"] = time.time()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(snap), encoding="utf-8")
            except Exception as e:
                log.debug("state file: %s", e)
            await asyncio.sleep(STATE_INTERVAL)
