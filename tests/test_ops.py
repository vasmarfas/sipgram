"""Housekeeping: history rotation, forgetting users who left, and `sipgram doctor`."""
import json

import pytest

from sipgram import doctor
from sipgram.config import load_config
from sipgram.history import CallHistory, record
from sipgram.prefs import UserPrefs

CONFIG = """
telegram: {api_id: 1, api_hash: h, sessions_dir: sessions}
users: [111, 222]
sip:
  server: 127.0.0.1
  local_port: 45070
  rtp_port_min: 45100
  rtp_port_max: 45110
  accounts:
    - {username: "491", password: x, users: [111, 222]}
"""


def test_history_keeps_only_the_last_calls_per_user(tmp_path):
    path = tmp_path / "history.json"
    history = CallHistory(path, size=5)
    for i in range(20):
        history.add(111, record("in", f"10{i}", "491", 0.0, "missed"))
    assert len(json.loads(path.read_text(encoding="utf-8"))["111"]) == 5
    assert [r.peer for r in history.last(111, 2)] == ["1019", "1018"]
    assert len(CallHistory(path, size=3).last(111, 10)) == 3, "an oversized file is trimmed on load"


def test_history_rotates_when_the_file_grows_too_large(tmp_path):
    path = tmp_path / "history.json"
    history = CallHistory(path, size=200, rotate_bytes=2000)
    for i in range(200):
        history.add(111, record("in", f"7999123456{i}", "491", 0.0, "answered"))
    assert path.with_name("history.json.1").exists(), "the old file is kept next to the new one"
    assert path.stat().st_size < 2000
    assert history.last(111, 1)[0].peer.endswith("199"), "the newest calls survive"


def test_prune_forgets_users_who_left_the_config(tmp_path):
    history = CallHistory(tmp_path / "history.json", size=5)
    prefs = UserPrefs(tmp_path / "prefs.json")
    for uid in (111, 222):
        history.add(uid, record("in", "101", "491", 0.0, "missed"))
        prefs.set(uid, "lang", "en")
    assert history.prune([111]) == 1 and prefs.prune([111]) == 1
    assert history.last(222) == [] and prefs.get(222, "lang") is None
    assert prefs.get(111, "lang") == "en" and history.last(111)
    assert "222" not in json.loads((tmp_path / "prefs.json").read_text(encoding="utf-8"))
    assert history.prune([111]) == 0, "nothing left to remove"


@pytest.mark.asyncio
async def test_doctor_checks_the_local_side_without_network(tmp_path, capsys):
    (tmp_path / "config.yaml").write_text(CONFIG, encoding="utf-8")
    cfg = load_config(tmp_path / "config.yaml")
    rc = await doctor.run(cfg, offline=True)
    out = capsys.readouterr().out
    for check in ("config", "version", "ntgcalls", "state dir", "sip port", "rtp ports", "schedule", "running"):
        assert check in out, check
    assert "[fail]" not in out and rc == 0
    assert "45100-45110" in out


@pytest.mark.asyncio
async def test_doctor_reports_a_port_that_is_already_taken(tmp_path, capsys):
    import socket

    (tmp_path / "config.yaml").write_text(CONFIG, encoding="utf-8")
    cfg = load_config(tmp_path / "config.yaml")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", cfg.accounts[0].local_port))
    try:
        rc = await doctor.run(cfg, offline=True)
    finally:
        sock.close()
    assert rc == 1 and "[fail] sip port" in capsys.readouterr().out
