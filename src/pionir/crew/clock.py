"""The wall clock.

Hearth ran a game clock: integer game seconds, advanced one per tick, two game
hours to the real hour. The crew does real work for real people, so the only
honest time is the time on the wall. ``t`` is whole seconds since the epoch;
the hour and day are local, because "the morning" means Ian's morning. Nothing
advances this clock - it is read, not ticked - and the time source is
injectable so tests never wait on it.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime

DAY = 86400


class WallClock:
    def __init__(self, now: Callable[[], float] = time.time) -> None:
        self._now = now

    @property
    def now(self) -> float:
        return self._now()

    @property
    def t(self) -> int:
        return int(self._now())

    def local(self) -> datetime:
        return datetime.fromtimestamp(self._now()).astimezone()

    @property
    def hour(self) -> int:
        return self.local().hour

    @property
    def hour_float(self) -> float:
        d = self.local()
        return d.hour + d.minute / 60.0 + d.second / 3600.0

    def hhmm(self) -> str:
        return self.local().strftime("%H:%M")

    def day_start_t(self) -> int:
        """Local midnight today, as epoch seconds."""
        d = self.local().replace(hour=0, minute=0, second=0, microsecond=0)
        return int(d.timestamp())

    def in_window(self, start_h: float, end_h: float) -> bool:
        """True when the local time of day lies in [start, end), wrapping midnight."""
        h = self.hour_float
        if start_h <= end_h:
            return start_h <= h < end_h
        return h >= start_h or h < end_h

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.local().strftime("%Y-%m-%d %H:%M:%S")
