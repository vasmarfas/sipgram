from __future__ import annotations

import logging
import os
import socket
import sys
from pathlib import Path

log = logging.getLogger("sipgram")


def detect_local_ip(remote_host: str, remote_port: int = 5060) -> str:
    """Source address the OS would use to reach the PBX."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((remote_host, remote_port))
        return s.getsockname()[0]
    except OSError:
        return "0.0.0.0"
    finally:
        s.close()


def raise_timer_resolution() -> None:
    """Windows timers default to ~15 ms granularity; RTP pacing wants 1 ms."""
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass


def hard_exit(code: int = 0) -> None:
    """Terminate without running C++ static destructors (ntgcalls threads block normal exit)."""
    sys.stdout.flush()
    sys.stderr.flush()
    if sys.platform == "win32":
        try:
            import ctypes

            k32 = ctypes.windll.kernel32
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            k32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            k32.TerminateProcess(k32.GetCurrentProcess(), code)
        except Exception:
            pass
    os._exit(code)


def setup_logging(level: str = "INFO", file: str = "", base_dir: Path | None = None) -> None:
    fmt = "%(asctime)s %(levelname)-5s %(name)s: %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if file:
        path = Path(file)
        if not path.is_absolute() and base_dir is not None:
            path = base_dir / path
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(path, encoding="utf-8"))
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format=fmt, handlers=handlers, force=True)
    for noisy in ("telethon", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
