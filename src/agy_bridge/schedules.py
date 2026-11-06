"""Scheduled prompts: /every 1h <prompt>, /at 09:00 <prompt> (daily)."""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

EVERY_RE = re.compile(r"^(\d+)\s*(m|min|mins|h|hr|hrs|d|day|days)$", re.IGNORECASE)
AT_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
UNITS = {"m": 60, "min": 60, "mins": 60, "h": 3600, "hr": 3600, "hrs": 3600, "d": 86400, "day": 86400, "days": 86400}


@dataclass
class Schedule:
    id: str
    chat_key: str
    chat_id: int
    thread_id: int | None
    prompt: str
    every_seconds: int = 0  # interval schedules
    at: str = ""  # "HH:MM" daily schedules (local time)
    cwd: str = ""
    next_run: float = 0.0
    last_run: float = 0.0
    created: float = field(default_factory=time.time)

    def describe(self) -> str:
        if self.at:
            when = f"daily at {self.at}"
        else:
            s = self.every_seconds
            when = f"every {s // 86400}d" if s % 86400 == 0 else f"every {s // 3600}h" if s % 3600 == 0 else f"every {s // 60}m"
        nxt = datetime.fromtimestamp(self.next_run).strftime("%a %H:%M") if self.next_run else "?"
        return f"{when} (next {nxt})"

    def compute_next(self, now: float | None = None) -> float:
        now = now or time.time()
        if self.at:
            h, m = map(int, self.at.split(":"))
            dt = datetime.fromtimestamp(now).replace(hour=h, minute=m, second=0, microsecond=0)
            if dt.timestamp() <= now:
                dt += timedelta(days=1)
            return dt.timestamp()
        return now + self.every_seconds

    def to_dict(self) -> dict:
        return asdict(self)


def parse_interval(text: str) -> int:
    m = EVERY_RE.match(text.strip())
    if not m:
        raise ValueError("Use an interval like 30m, 2h or 1d")
    secs = int(m.group(1)) * UNITS[m.group(2).lower()]
    if secs < 300:
        raise ValueError("The shortest interval is 5m")
    return secs


def parse_at(text: str) -> str:
    m = AT_RE.match(text.strip())
    if not m:
        raise ValueError("Use a 24-hour time like 09:00 or 18:30")
    return f"{int(m.group(1)):02d}:{m.group(2)}"
