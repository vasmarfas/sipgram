"""Per-user preferences that must survive a restart (language, do-not-disturb)."""
from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

log = logging.getLogger("sipgram.prefs")


class UserPrefs:
    def __init__(self, path: Path | None):
        self.path = path
        self._data: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        if path and path.exists():
            try:
                self._data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as e:
                log.warning("preferences file %s unreadable, starting empty: %s", path, e)

    def get(self, user_id: int, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(str(user_id), {}).get(key, default)

    def set(self, user_id: int, key: str, value: Any) -> None:
        with self._lock:
            self._data.setdefault(str(user_id), {})[key] = value
            self._save()

    def prune(self, known: Iterable[int]) -> int:
        """Forgets the settings of users who are no longer in the config."""
        keep = {str(u) for u in known}
        with self._lock:
            gone = [uid for uid in self._data if uid not in keep]
            for uid in gone:
                del self._data[uid]
            if gone:
                self._save()
                log.info("preferences: removed %d user(s) that left the config", len(gone))
        return len(gone)

    def _save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except Exception as e:
            log.debug("preferences save failed: %s", e)
