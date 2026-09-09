"""HTTP API: click-to-call and call control for CRM systems and scripts.

Off by default. When enabled it wants a bearer token of at least 16 characters, binds to
127.0.0.1 unless told otherwise, and can be limited to a list of addresses.
"""
from __future__ import annotations

import hmac
import ipaddress
import logging
import time
from collections import deque
from typing import Any

from aiohttp import web

from .config import ApiConfig
from .messages import fmt_duration
from .sip.account import CallState

log = logging.getLogger("sipgram.api")


class HttpApi:
    def __init__(self, cfg: ApiConfig, manager):
        self.cfg = cfg
        self.manager = manager
        self._hits: dict[str, deque[float]] = {}
        self._runner: web.AppRunner | None = None
        self.app = web.Application(middlewares=[self._guard])
        self.app.add_routes([
            web.get("/api/health", self.health),
            web.get("/api/status", self.status),
            web.get("/api/users", self.users),
            web.get("/api/history", self.history),
            web.post("/api/calls", self.place_call),
            web.post("/api/calls/{user}/hangup", self.hangup),
            web.post("/api/calls/{user}/dtmf", self.dtmf),
            web.post("/api/calls/{user}/transfer", self.transfer),
            web.post("/api/messages", self.message),
        ])

    # ---- lifecycle ----

    async def start(self) -> None:
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.cfg.bind, self.cfg.port)
        await site.start()
        log.info("HTTP API on http://%s:%d (token auth%s)", self.cfg.bind, self.cfg.port,
                 f", allowlist {', '.join(self.cfg.allow)}" if self.cfg.allow else "")

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ---- security ----

    def _allowed_ip(self, peer: str) -> bool:
        if not self.cfg.allow:
            return True
        try:
            addr = ipaddress.ip_address(peer)
        except ValueError:
            return False
        for entry in self.cfg.allow:
            try:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            except ValueError:
                log.warning("api.allow: %s is not an address or network", entry)
        return False

    def _rate_ok(self, peer: str) -> bool:
        if self.cfg.rate_limit <= 0:
            return True
        now = time.monotonic()
        hits = self._hits.setdefault(peer, deque())
        while hits and now - hits[0] > 60:
            hits.popleft()
        if len(hits) >= self.cfg.rate_limit:
            return False
        hits.append(now)
        return True

    @staticmethod
    def _token_of(request: web.Request) -> str:
        header = request.headers.get("Authorization", "")
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        return request.headers.get("X-Api-Token", "")

    @web.middleware
    async def _guard(self, request: web.Request, handler):
        peer = request.remote or ""
        if request.path == "/api/health":
            return await handler(request)
        if not self._allowed_ip(peer):
            log.warning("api: rejected %s %s from %s (not in api.allow)", request.method, request.path, peer)
            raise web.HTTPForbidden(reason="address not allowed")
        if not hmac.compare_digest(self._token_of(request), self.cfg.token):
            log.warning("api: bad token for %s %s from %s", request.method, request.path, peer)
            raise web.HTTPUnauthorized(reason="bad token")
        if not self._rate_ok(peer):
            raise web.HTTPTooManyRequests(reason="rate limit")
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except Exception as e:
            log.exception("api: %s %s failed", request.method, request.path)
            return web.json_response({"error": str(e)}, status=500)

    # ---- helpers ----

    def _user(self, spec: str):
        spec = str(spec).strip()
        if spec.lstrip("-").isdigit():
            u = self.manager.users.get(int(spec))
            if u is not None:
                return u
        for u in self.manager.users.values():
            if u.cfg.id == spec or u.name == spec:
                return u
        raise web.HTTPNotFound(reason=f"no such user: {spec}")

    @staticmethod
    async def _json(request: web.Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(reason="body must be JSON") from None
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(reason="body must be a JSON object")
        return body

    # ---- handlers ----

    async def health(self, request: web.Request) -> web.Response:
        accounts = {rt.name: rt.sip.registered for rt in self.manager.accounts}
        ok = all(accounts.values()) if accounts else False
        return web.json_response({"ok": ok, "accounts": accounts}, status=200 if ok else 503)

    async def status(self, request: web.Request) -> web.Response:
        m = self.manager
        return web.json_response({
            "gateways": {gw.name: {"session": gw.cfg.session, "calls": len(gw.engine.calls),
                                   "max_calls": gw.cfg.max_calls} for gw in m.gateways.values()},
            "accounts": [{"name": rt.name, "extension": rt.cfg.username, "registered": rt.sip.registered,
                          "owners": [u.id for u in rt.owners], "shared": rt.cfg.shared} for rt in m.accounts],
            "calls": [self._call_info(u) for u in m.users.values() if u.legs() or u.tg],
        })

    @staticmethod
    def _call_info(u) -> dict[str, Any]:
        def leg(x):
            if x is None:
                return None
            return {"peer": x.peer, "number": x.number, "direction": x.direction,
                    "account": x.account.name, "state": x.call.state.value,
                    "duration": int(time.time() - x.call.connected_at) if x.call.connected_at else 0}
        return {"user": u.id, "name": u.name, "dnd": u.dnd, "recording": bool(u.bridge and u.bridge.recording),
                "active": leg(u.active), "held": leg(u.held), "waiting": leg(u.waiting)}

    async def users(self, request: web.Request) -> web.Response:
        return web.json_response([
            {"id": u.id, "name": u.name, "language": u.lang, "dnd": u.dnd, "can_call": u.cfg.can_call,
             "gateway": u.gw.name if u.gw else None,
             "accounts": [rt.name for rt in u.accounts]} for u in self.manager.users.values()])

    async def history(self, request: web.Request) -> web.Response:
        u = self._user(request.query.get("user", ""))
        limit = min(int(request.query.get("limit", 20)), 200)
        items = self.manager.history.last(u.id, limit)
        return web.json_response([{"ts": r.ts, "direction": r.direction, "peer": r.peer, "account": r.account,
                                   "duration": r.duration, "result": r.result} for r in items])

    async def place_call(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        u = self._user(body.get("user", ""))
        number = str(body.get("number", "")).strip()
        if not number:
            raise web.HTTPBadRequest(reason="number is required")
        account = body.get("account")
        rt = None
        if account:
            rt = next((a for a in u.accounts if a.name == account), None)
            if rt is None:
                raise web.HTTPBadRequest(reason=f"user has no account named {account}")
        if u.busy:
            raise web.HTTPConflict(reason="user is already in a call")
        log.info("api: call %s -> %s", u.id, number)
        self.manager.loop.create_task(self.manager.dial_from_api(u, number, rt))
        return web.json_response({"ok": True, "user": u.id, "number": number})

    async def hangup(self, request: web.Request) -> web.Response:
        u = self._user(request.match_info["user"])
        await self.manager.hangup_user(u)
        return web.json_response({"ok": True})

    async def dtmf(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        u = self._user(request.match_info["user"])
        digits = str(body.get("digits", ""))
        if not digits:
            raise web.HTTPBadRequest(reason="digits are required")
        if not (u.active and u.active.call.state == CallState.CONNECTED):
            raise web.HTTPConflict(reason="no active call")
        await u.active.call.send_dtmf(digits)
        return web.json_response({"ok": True, "digits": digits})

    async def transfer(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        u = self._user(request.match_info["user"])
        number = str(body.get("number", "")).strip()
        if not (u.active and u.active.call.state == CallState.CONNECTED):
            raise web.HTTPConflict(reason="no active call")
        if not number:
            raise web.HTTPBadRequest(reason="number is required")
        ok, detail = await u.active.call.refer(self.manager.apply_dial_rules(number))
        return web.json_response({"ok": ok, "detail": detail}, status=200 if ok else 409)

    async def message(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        u = self._user(body.get("user", ""))
        text = str(body.get("text", "")).strip()
        if not text:
            raise web.HTTPBadRequest(reason="text is required")
        await self.manager.notifier.send(u.id, text)
        return web.json_response({"ok": True})


def duration_text(seconds: float) -> str:
    return fmt_duration(seconds)
