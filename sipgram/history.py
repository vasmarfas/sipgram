"""Per-user call log, persisted as JSON (best effort)."""
from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger("sipgram.history")

ROTATE_BYTES = 1 << 20      # a file this large means an old config or a very busy month


@dataclass
class CallRecord:
    ts: float
    direction: str
    peer: str
    account: str
    duration: int
    result: str


class CallHistory:
    def __init__(self, path: Path | None, size: int = 50, rotate_bytes: int = ROTATE_BYTES):
        self.path = path
        self.size = max(1, size)
        self.rotate_bytes = rotate_bytes
        self._data: dict[str, list[dict]] = {}
        self._lock = threading.Lock()
        if path and path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                self._data = {str(k): v[-self.size:] for k, v in loaded.items() if isinstance(v, list)}
            except Exception as e:
                log.warning("history file %s unreadable, starting empty: %s", path, e)

    def prune(self, known: Iterable[int]) -> int:
        """Drops the log of users who are no longer in the config."""
        keep = {str(u) for u in known}
        with self._lock:
            gone = [uid for uid in self._data if uid not in keep]
            for uid in gone:
                del self._data[uid]
            if gone:
                self._save()
                log.info("history: removed %d user(s) that left the config", len(gone))
        return len(gone)

    def add(self, user_id: int, rec: CallRecord) -> None:
        with self._lock:
            items = self._data.setdefault(str(user_id), [])
            items.append(asdict(rec))
            del items[: -self.size]
            self._save()

    def last(self, user_id: int, n: int = 10) -> list[CallRecord]:
        with self._lock:
            items = self._data.get(str(user_id), [])[-n:]
        return [CallRecord(**i) for i in reversed(items)]

    def last_caller(self, user_id: int) -> str:
        for r in self.last(user_id, self.size):
            if r.direction == "in" and r.peer:
                return r.peer
        return ""

    def last_dialed(self, user_id: int) -> str:
        for r in self.last(user_id, self.size):
            if r.direction == "out" and r.peer:
                return r.peer
        return ""

    def _save(self) -> None:
        if not self.path:
            return
        data = json.dumps(self._data, ensure_ascii=False)
        if len(data) > self.rotate_bytes:
            self._rotate()
            data = json.dumps(self._data, ensure_ascii=False)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(data, encoding="utf-8")
            tmp.replace(self.path)
        except Exception as e:
            log.debug("history save failed: %s", e)

    def _rotate(self) -> None:
        """The old file is kept as history.json.1 and the live one starts small again."""
        try:
            if self.path.exists():
                self.path.replace(self.path.with_name(self.path.name + ".1"))
        except OSError as e:
            log.debug("history rotation failed: %s", e)
        keep = max(5, self.size // 4)
        while True:
            for items in self._data.values():
                del items[:-keep]
            if keep <= 1 or len(json.dumps(self._data, ensure_ascii=False)) <= self.rotate_bytes:
                break
            keep //= 2
        log.info("history rotated: kept the last %d call(s) per user", keep)


def record(direction: str, peer: str, account: str, connected_at: float, result: str) -> CallRecord:
    duration = int(time.time() - connected_at) if connected_at else 0
    return CallRecord(ts=time.time(), direction=direction, peer=peer, account=account, duration=duration, result=result)
