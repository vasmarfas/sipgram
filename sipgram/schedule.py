"""Time windows and incoming-call screening.

Everything here is empty by default, so a config without a `schedule` section behaves exactly as
before. Times are the local time of the process; in Docker set TZ in docker-compose.yml.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time

DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
ALIASES = {"all": "mon-sun", "daily": "mon-sun", "weekdays": "mon-fri", "weekend": "sat-sun",
           "пн": "mon", "вт": "tue", "ср": "wed", "чт": "thu", "пт": "fri", "сб": "sat", "вс": "sun"}
_TIME = re.compile(r"^(\d{1,2}):(\d{2})$")


class ScheduleError(ValueError):
    pass


def _parse_days(spec: str) -> set[int]:
    text = str(spec or "all").strip().lower()
    text = ALIASES.get(text, text)
    days: set[int] = set()
    for part in text.replace(" ", "").split(","):
        part = ALIASES.get(part, part)
        if "-" in part:
            a, _, b = part.partition("-")
            if a not in DAYS or b not in DAYS:
                raise ScheduleError(f"unknown day range: {part}")
            first, last = DAYS[a], DAYS[b]
            days.update((first + i) % 7 for i in range((last - first) % 7 + 1))
        elif part in DAYS:
            days.add(DAYS[part])
        else:
            raise ScheduleError(f"unknown day: {part}")
    return days


def _parse_time(spec, field: str) -> time:
    m = _TIME.match(str(spec).strip())
    if not m:
        raise ScheduleError(f"{field} must look like 09:00, got {spec!r}")
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour == 24 and minute == 0:
        return time(23, 59, 59)
    if hour > 23 or minute > 59:
        raise ScheduleError(f"{field} is out of range: {spec}")
    return time(hour, minute)


@dataclass(frozen=True)
class Window:
    days: frozenset[int]
    start: time
    end: time

    def contains(self, moment: datetime) -> bool:
        now, weekday = moment.time(), moment.weekday()
        if self.start <= self.end:
            return weekday in self.days and self.start <= now <= self.end
        if weekday in self.days and now >= self.start:
            return True
        return (weekday - 1) % 7 in self.days and now <= self.end

    def __str__(self) -> str:
        names = [name for name, index in sorted(DAYS.items(), key=lambda kv: kv[1]) if index in self.days]
        return f"{','.join(names)} {self.start.strftime('%H:%M')}-{self.end.strftime('%H:%M')}"


class Schedule:
    """A list of day/time windows. `mon-fri 09:00-18:00` or {days: mon-fri, from: '09:00', to: '18:00'}."""

    def __init__(self, windows: list[Window] | None = None):
        self.windows = windows or []

    @classmethod
    def parse(cls, spec, where: str = "schedule") -> Schedule:
        if not spec:
            return cls([])
        if isinstance(spec, (str, dict)):
            spec = [spec]
        if not isinstance(spec, list):
            raise ScheduleError(f"{where}: expected a list of windows")
        windows: list[Window] = []
        for i, item in enumerate(spec):
            at = f"{where}[{i}]"
            if isinstance(item, str):
                parts = item.split()
                if len(parts) != 2 or "-" not in parts[1]:
                    raise ScheduleError(f"{at}: expected 'mon-fri 09:00-18:00', got {item!r}")
                days_spec, hours = parts
                start, _, end = hours.partition("-")
            elif isinstance(item, dict):
                unknown = set(item) - {"days", "from", "to"}
                if unknown:
                    raise ScheduleError(f"{at}: unknown keys {sorted(unknown)}")
                days_spec = item.get("days", "all")
                start, end = item.get("from", "00:00"), item.get("to", "23:59")
            else:
                raise ScheduleError(f"{at}: expected a string or a mapping")
            windows.append(Window(frozenset(_parse_days(days_spec)),
                                  _parse_time(start, f"{at}.from"), _parse_time(end, f"{at}.to")))
        return cls(windows)

    @property
    def empty(self) -> bool:
        return not self.windows

    def matches(self, moment: datetime | None = None) -> bool:
        return any(w.contains(moment or datetime.now()) for w in self.windows)

    def __str__(self) -> str:
        return "; ".join(str(w) for w in self.windows)


def match_number(pattern: str, number: str, name: str = "") -> bool:
    """`+7999*` (wildcards), `~^7\\d{10}$` (regex) or a plain number; `anonymous` matches a hidden caller."""
    pattern = str(pattern).strip()
    if not pattern:
        return False
    digits = re.sub(r"[^\d*#]", "", (number or "").lstrip("+"))
    if pattern.lower() in ("anonymous", "unknown", "аноним"):
        return not digits
    if pattern.startswith("~"):
        try:
            rx = re.compile(pattern[1:])
        except re.error as e:
            raise ScheduleError(f"bad regex in the list: {pattern[1:]} ({e})") from e
        return bool(rx.search(number or "") or (name and rx.search(name)))
    clean = re.sub(r"[^\d*#?]", "", pattern.lstrip("+"))
    rx = re.compile("^" + re.escape(clean).replace(r"\*", ".*").replace(r"\?", ".") + "$")
    return bool(rx.match(digits))


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""        # blacklist | whitelist | quiet | off_hours


ALLOWED = Decision(True)


def screen(rules, number: str, name: str = "", moment: datetime | None = None) -> Decision:
    """Applies a ScheduleConfig to one incoming call. The whitelist wins over the clock."""
    if rules is None or rules.empty:
        return ALLOWED
    if any(match_number(p, number, name) for p in rules.blacklist):
        return Decision(False, "blacklist")
    if rules.whitelist:
        if any(match_number(p, number, name) for p in rules.whitelist):
            return ALLOWED
        return Decision(False, "whitelist")
    closed = time_closed(rules, moment)
    return Decision(False, closed) if closed else ALLOWED


def time_closed(rules, moment: datetime | None = None) -> str:
    """The clock part alone: '' when calls may ring right now, otherwise the reason."""
    if rules is None:
        return ""
    now = moment or datetime.now()
    if not rules.work.empty and not rules.work.matches(now):
        return "off_hours"
    return "quiet" if rules.quiet.matches(now) else ""
