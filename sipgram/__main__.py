from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

from . import __version__
from .config import Config, ConfigError, load_config
from .util import hard_exit, setup_logging

log = logging.getLogger("sipgram")


def _load(args) -> Config:
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        raise SystemExit(2) from None
    if getattr(args, "verbose", False):
        cfg.logging.level = "DEBUG"
    if getattr(args, "sip_trace", False):
        cfg.logging.sip_trace = True
    setup_logging(cfg.logging.level, cfg.logging.file, Path(args.config).resolve().parent)
    if cfg.logging.sip_trace:
        logging.getLogger("sipgram.sip.trace").setLevel(logging.DEBUG)
    for note in cfg.notes:
        print(f"note: {note}", file=sys.stderr)
    return cfg


async def _cmd_run(args) -> int:
    from .gateway import Gateway

    cfg = _load(args)
    return await Gateway(cfg).run()


async def _cmd_login(args) -> int:
    from .tg.account import TgAccount

    cfg = _load(args)
    targets = [g for g in cfg.telegram.gateways if not args.gateway or g.name == args.gateway]
    if not targets:
        print(f"no telegram gateway named {args.gateway}", file=sys.stderr)
        return 2
    for gcfg in targets:
        print(f"== gateway {gcfg.name}: logging in the Telegram account (session {gcfg.session})")
        acc = TgAccount(cfg.telegram, gcfg.session)
        await acc.login_interactive()
        await acc.disconnect()
    if cfg.telegram.bot_token and not args.gateway:
        from .tg.bot import bot_login_check

        name = await bot_login_check(cfg.telegram)
        print(f"== bot @{name} is reachable; users must press Start in it to get buttons")
    return 0


async def _cmd_whoami(args) -> int:
    from .tg.account import NotAuthorized, TgAccount

    cfg = _load(args)
    rc = 0
    for gcfg in cfg.telegram.gateways:
        acc = TgAccount(cfg.telegram, gcfg.session)
        try:
            await acc.connect()
        except NotAuthorized as e:
            print(f"gateway {gcfg.name}: NOT LOGGED IN ({e})")
            rc = 1
            continue
        me = acc.me
        assert me is not None
        print(f"gateway {gcfg.name}: id={me.id} name={acc._name(me)} phone={me.phone} max_calls={gcfg.max_calls}")
        for u in cfg.users:
            if (u.gateway or cfg.telegram.gateways[0].name) != gcfg.name:
                continue
            try:
                iu = await acc.resolve_user(u.id)
                owned = [a.name for a in cfg.accounts if u.id in a.users]
                print(f"  user {u.id} -> id={iu.user_id} ({await acc.user_display(iu.user_id)}) lines={owned or 'shared only'}")
            except Exception as e:
                print(f"  user {u.id}: cannot resolve: {e}")
                rc = 1
        await acc.disconnect()
    if cfg.telegram.bot_token:
        from .tg.bot import bot_login_check

        try:
            print(f"bot: @{await bot_login_check(cfg.telegram)}")
        except Exception as e:
            print(f"bot: FAILED ({e})")
            rc = 1
    return rc


async def _cmd_check(args) -> int:
    """Registers each SIP account once (no Telegram involved) and reports the result."""
    from .sip.account import CallError, SipAccount
    from .util import detect_local_ip

    cfg = _load(args)
    rc = 0
    for ac in cfg.accounts:
        if args.account and ac.name != args.account:
            continue
        local_ip = ac.local_ip or detect_local_ip(ac.server, ac.port)
        ac.register = False
        ac.keepalive = 0
        acc = SipAccount(ac, local_ip)
        try:
            await acc.start()
            expires = await acc._register_once(ac.expires)
            print(f"account {ac.name}: SIP registration OK ({ac.username}@{ac.domain} via {ac.server}:{ac.port}, "
                  f"local {local_ip}:{acc.transport.local_port}, codecs {', '.join(ac.codecs)}, expires {expires}s)")
            await acc._register_once(0)
        except CallError as e:
            print(f"account {ac.name}: SIP registration FAILED: {e}")
            rc = 1
        except Exception as e:
            print(f"account {ac.name}: SIP error: {e}")
            rc = 1
        finally:
            await acc.transport.stop()
    return rc


async def _cmd_doctor(args) -> int:
    """Checks config, ports, DNS, SIP registration, Telegram sessions and libopus in one go."""
    from . import doctor

    return await doctor.run(_load(args), offline=args.offline)


async def _cmd_status(args) -> int:
    """Reads the state file written by a running gateway (used by the Docker healthcheck)."""
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    path = cfg.state_dir / "state.json"
    if not path.exists():
        print("not running (no state file)")
        return 1
    snap = json.loads(path.read_text(encoding="utf-8"))
    age = time.time() - snap.get("ts", 0)
    accounts = snap.get("accounts", {})
    bad = [n for n, ok in accounts.items() if not ok]
    calls = snap.get("telegram_calls", {})
    print(f"state age {age:.0f}s; accounts: " + ", ".join(f"{n}={'OK' if ok else 'FAIL'}" for n, ok in accounts.items())
          + f"; sip legs: {snap.get('calls', 0)}; telegram calls: {calls}")
    return 1 if age > 90 or bad else 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="sipgram", description="SIP <-> Telegram voice gateway")
    parser.add_argument("-c", "--config", default="config.yaml", help="path to config.yaml")
    parser.add_argument("--version", action="version", version=f"sipgram {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="log at DEBUG level")
    parser.add_argument("--sip-trace", action="store_true", help="log every SIP message")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="start the gateway")
    p_login = sub.add_parser("login", help="interactive Telegram login for the gateway accounts")
    p_login.add_argument("gateway", nargs="?", help="gateway name (default: all of them)")
    sub.add_parser("whoami", help="show the gateway accounts and resolve configured users")
    p_check = sub.add_parser("check", help="test SIP registration of the accounts")
    p_check.add_argument("account", nargs="?", help="account name (default: all)")
    sub.add_parser("status", help="health of a running gateway (exit 0 = healthy)")
    p_doctor = sub.add_parser("doctor", help="check everything the gateway needs (exit 0 = all good)")
    p_doctor.add_argument("--offline", action="store_true", help="skip DNS, SIP registration and Telegram checks")
    args = parser.parse_args(argv)
    handler = {"run": _cmd_run, "login": _cmd_login, "whoami": _cmd_whoami, "check": _cmd_check,
               "status": _cmd_status, "doctor": _cmd_doctor}[args.command]
    try:
        rc = asyncio.run(handler(args))
    except KeyboardInterrupt:
        rc = 0
    if args.command in ("run", "whoami", "login", "doctor"):
        hard_exit(rc)
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
