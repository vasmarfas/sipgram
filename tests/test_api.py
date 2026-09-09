"""HTTP API: authentication, the allowlist and the endpoints, against a fake call manager."""
from __future__ import annotations

import asyncio

import aiohttp
import pytest

from sipgram.api import HttpApi
from sipgram.config import ApiConfig
from sipgram.history import CallHistory, record
from sipgram.sip.account import CallState

TOKEN = "0123456789abcdef-token"


class FakeCall:
    def __init__(self):
        self.state = CallState.CONNECTED
        self.connected_at = 0.0
        self.dtmf: list[str] = []
        self.refers: list[str] = []

    async def send_dtmf(self, digits):
        self.dtmf.append(digits)

    async def refer(self, target, timeout=15.0):
        self.refers.append(target)
        return True, "202"


class FakeLeg:
    def __init__(self, account):
        self.call = FakeCall()
        self.peer = "+7 999"
        self.number = "79990000000"
        self.direction = "out"
        self.account = account


class FakeUser:
    def __init__(self, uid, accounts):
        self.id = uid
        self.name = "Иван"
        self.lang = "ru"
        self.dnd = False
        self.bridge = None
        self.gw = type("GW", (), {"name": "main"})()
        self.accounts = accounts
        self.cfg = type("Cfg", (), {"id": str(uid), "can_call": True})()
        self.active = None
        self.held = None
        self.waiting = None
        self.busy = False
        self.tg = None

    def legs(self):
        return [x for x in (self.active, self.held, self.waiting) if x]


class FakeManager:
    def __init__(self, loop):
        self.loop = loop
        account = type("Acc", (), {"name": "491", "cfg": type("C", (), {"username": "491", "shared": False})(),
                                   "sip": type("S", (), {"registered": True})(), "owners": []})()
        self.accounts = [account]
        self.users = {111: FakeUser(111, [account])}
        self.gateways = {"main": type("GW", (), {
            "name": "main", "cfg": type("C", (), {"session": "s", "max_calls": 3})(),
            "engine": type("E", (), {"calls": {}})()})()}
        self.history = CallHistory(None, 10)
        self.history.add(111, record("in", "79990000000", "491", 0.0, "missed"))
        self.notifier = type("N", (), {"messages": [], "send": self._send})()
        self.dialed: list[tuple[int, str, object]] = []
        self.hung_up: list[int] = []

    async def _send(self, uid, text, buttons=None):
        self.notifier.messages.append((uid, text))

    async def dial_from_api(self, user, number, rt=None):
        self.dialed.append((user.id, number, rt))

    async def hangup_user(self, user):
        self.hung_up.append(user.id)

    @staticmethod
    def apply_dial_rules(number: str) -> str:
        return number.replace(" ", "")


@pytest.fixture
async def api():
    loop = asyncio.get_running_loop()
    manager = FakeManager(loop)
    cfg = ApiConfig(enabled=True, bind="127.0.0.1", port=0, token=TOKEN)
    service = HttpApi(cfg, manager)
    await service.start()
    port = service._runner.addresses[0][1]
    yield service, manager, f"http://127.0.0.1:{port}"
    await service.stop()


async def call_api(url, method="GET", token=TOKEN, **kw):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with aiohttp.ClientSession() as session, session.request(method, url, headers=headers, **kw) as resp:
        body = await resp.json() if resp.content_type == "application/json" else await resp.text()
        return resp.status, body


@pytest.mark.asyncio
async def test_health_needs_no_token(api):
    _, _, base = api
    status, body = await call_api(f"{base}/api/health", token="")
    assert status == 200 and body["ok"] is True and body["accounts"] == {"491": True}


@pytest.mark.asyncio
async def test_token_is_required_and_compared_fully(api):
    _, _, base = api
    assert (await call_api(f"{base}/api/status", token=""))[0] == 401
    assert (await call_api(f"{base}/api/status", token="wrong"))[0] == 401
    assert (await call_api(f"{base}/api/status", token=TOKEN[:-1]))[0] == 401
    assert (await call_api(f"{base}/api/status"))[0] == 200


@pytest.mark.asyncio
async def test_status_users_and_history(api):
    _, _, base = api
    status, body = await call_api(f"{base}/api/status")
    assert status == 200 and body["gateways"]["main"]["max_calls"] == 3
    assert body["accounts"][0]["extension"] == "491"
    status, body = await call_api(f"{base}/api/users")
    assert body[0]["id"] == 111 and body[0]["gateway"] == "main"
    status, body = await call_api(f"{base}/api/history?user=111")
    assert body[0]["peer"] == "79990000000" and body[0]["result"] == "missed"
    assert (await call_api(f"{base}/api/history?user=999"))[0] == 404


@pytest.mark.asyncio
async def test_click_to_call(api):
    _, manager, base = api
    status, body = await call_api(f"{base}/api/calls", "POST", json={"user": 111, "number": "+7 999 000 00 00"})
    assert status == 200 and body["ok"] is True
    await asyncio.sleep(0.05)
    assert manager.dialed == [(111, "+7 999 000 00 00", None)]
    assert (await call_api(f"{base}/api/calls", "POST", json={"user": 111}))[0] == 400
    assert (await call_api(f"{base}/api/calls", "POST", json={"user": 42, "number": "1"}))[0] == 404
    assert (await call_api(f"{base}/api/calls", "POST", data="not json"))[0] == 400


@pytest.mark.asyncio
async def test_dtmf_transfer_hangup_and_message(api):
    _, manager, base = api
    user = manager.users[111]
    assert (await call_api(f"{base}/api/calls/111/dtmf", "POST", json={"digits": "12"}))[0] == 409
    user.active = FakeLeg(manager.accounts[0])
    status, _ = await call_api(f"{base}/api/calls/111/dtmf", "POST", json={"digits": "12"})
    assert status == 200 and user.active.call.dtmf == ["12"]
    status, body = await call_api(f"{base}/api/calls/111/transfer", "POST", json={"number": "10 1"})
    assert status == 200 and body["ok"] and user.active.call.refers == ["101"]
    assert (await call_api(f"{base}/api/calls/111/hangup", "POST"))[0] == 200
    assert manager.hung_up == [111]
    assert (await call_api(f"{base}/api/messages", "POST", json={"user": 111, "text": "hi"}))[0] == 200
    assert manager.notifier.messages == [(111, "hi")]


@pytest.mark.asyncio
async def test_allowlist_blocks_other_addresses(api):
    service, _, base = api
    service.cfg.allow = ["10.1.2.0/24"]
    assert (await call_api(f"{base}/api/status"))[0] == 403
    service.cfg.allow = ["127.0.0.1"]
    assert (await call_api(f"{base}/api/status"))[0] == 200


@pytest.mark.asyncio
async def test_rate_limit(api):
    service, _, base = api
    service.cfg.rate_limit = 3
    service._hits.clear()
    codes = [(await call_api(f"{base}/api/status"))[0] for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3:] == [429, 429]
