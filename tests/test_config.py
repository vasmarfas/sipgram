import textwrap

import pytest

from sipgram.config import ConfigError, load_config


def _write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(text), encoding="utf-8")
    return p


def test_v02_layout(tmp_path, monkeypatch):
    monkeypatch.setenv("SIP_PW", "s3cret")
    cfg = load_config(_write(tmp_path, """
        telegram: {api_id: 1, api_hash: "h", session: gw, bot_token: "123:abc", max_calls: 4}
        users:
          - {id: 111, name: John}
          - "@petya"
        sip:
          server: pbx.local
          codecs: [pcmu]
          accounts:
            - {username: "491", password: "${SIP_PW}", user: 111}
            - {username: "492", password: x, users: ["@petya", 111], ring_all: true, transport: tcp}
            - {username: "trunk", password: x, shared: true, local_port: 6000}
        calls: {outgoing_mode: direct, call_waiting: false}
    """))
    assert cfg.telegram.session == "gw" and cfg.telegram.bot_token == "123:abc" and cfg.telegram.max_calls == 4
    assert [u.id for u in cfg.users] == ["111", "@petya"]
    a, b, c = cfg.accounts
    assert a.password == "s3cret" and a.users == ["111"] and a.domain == "pbx.local" and a.codecs == ["pcmu"]
    assert a.local_port == 5070 and b.local_port == 5071 and c.local_port == 6000
    assert b.users == ["@petya", "111"] and b.ring_all and b.transport == "tcp"
    assert c.shared and c.users == []
    assert cfg.calls.outgoing_mode == "direct" and cfg.calls.call_waiting is False
    assert cfg.state_dir == cfg.telegram.sessions_dir == (tmp_path / "sessions").resolve()


def test_account_users_are_added_implicitly(tmp_path):
    cfg = load_config(_write(tmp_path, """
        telegram: {api_id: 1, api_hash: h}
        sip:
          server: pbx
          accounts:
            - {username: "1", password: x, users: [555]}
    """))
    assert [u.id for u in cfg.users] == ["555"]


def test_migration_from_lines(tmp_path):
    cfg = load_config(_write(tmp_path, """
        telegram: {api_id: 1, api_hash: h}
        sip_defaults: {transport: tcp}
        lines:
          - name: line1
            telegram: {session: line1}
            sip: {server: pbx, username: "391", password: x, local_port: 5070}
            users: [{id: 111, name: John}, 222]
            incoming: {ring_to: 222, ring_timeout: 30}
            outgoing: {mode: direct, default_destination: "100", dial_rules: [["x", "y"]]}
            audio: {jitter_ms: 60}
          - name: line2
            telegram: {session: line1}
            sip: {server: pbx, username: "392", password: x, local_port: 5071}
            users: [333]
    """))
    assert cfg.notes and "v0.1" in cfg.notes[0]
    assert cfg.telegram.session == "line1"
    assert [u.id for u in cfg.users] == ["111", "222", "333"]
    assert cfg.user("111").name == "John"
    a, b = cfg.accounts
    assert a.name == "line1" and a.users == ["222", "111"] and a.ring_timeout == 30 and a.default_destination == "100"
    assert a.transport == "tcp" and b.name == "line2" and b.users == ["333"]
    assert cfg.calls.outgoing_mode == "direct" and cfg.calls.dial_rules == [["x", "y"]] and cfg.calls.jitter_ms == 60


def test_migration_rejects_several_sessions(tmp_path):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, """
            telegram: {api_id: 1, api_hash: h}
            lines:
              - {name: a, telegram: {session: s1}, sip: {server: pbx, username: "1", password: x}, users: [1]}
              - {name: b, telegram: {session: s2}, sip: {server: pbx, username: "2", password: x}, users: [2]}
        """))


def test_errors(tmp_path):
    base = "telegram: {api_id: 1, api_hash: h}\n"
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, base + "sip: {server: pbx, accounts: []}\n"))
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, base + "sip: {server: pbx, accounts: [{username: '1', password: x}]}\n"))
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, base + "sip: {server: pbx, transport: sctp, accounts: [{username: '1', password: x, users: [1]}]}\n"))
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, base + "sip: {server: pbx, accounts: [{username: '1', password: '${DEFINITELY_MISSING_VAR}', users: [1]}]}\n"))
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, base + "sip: {server: pbx, accounts: [{username: '1', password: x, users: [1], local_port: 5070}, {username: '2', password: x, users: [2], local_port: 5070}]}\n"))
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, base + "sip: {server: pbx, codecs: [g729], accounts: [{username: '1', password: x, users: [1]}]}\n"))
