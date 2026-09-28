"""The overnight GPU window: when a Daedalus build may run.

Daedalus's 30B coder takes the whole card and evicts the model Moss and the leaders speak
with, so builds run only at night: by default 01:00-07:00 local time (configurable; a window
may cross midnight, e.g. 23:00-05:00). A window is named by the local date it STARTS on - the
"night" - and at most one product is started per night.

A job may start only when it can finish inside the window: ``now + budget <= end``. Its
``not_after`` (the wall-clock time Pionir's adapter cancels it at, whatever else) is
``min(now + budget, end)``, so nothing Daedalus does on the GPU outlives the window.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta

_HHMM = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")


def parse_hhmm(text: str) -> time:
    m = _HHMM.fullmatch(str(text or "").strip())
    if m is None:
        raise ValueError(f"{text!r} is not a time of day like 01:00")
    return time(int(m.group(1)), int(m.group(2)))


@dataclass(frozen=True)
class Night:
    """One occurrence of the window: its name (the local date it starts on) and its bounds
    in epoch seconds."""

    key: str
    start: float
    end: float

    def contains(self, t: float) -> bool:
        return self.start <= t < self.end


@dataclass(frozen=True)
class Window:
    start: time
    end: time

    @classmethod
    def parse(cls, spec: str) -> Window:
        """``"01:00-07:00"``."""
        parts = str(spec or "").split("-")
        if len(parts) != 2:
            raise ValueError(f"{spec!r} is not a window like 01:00-07:00")
        start, end = parse_hhmm(parts[0]), parse_hhmm(parts[1])
        if start == end:
            raise ValueError("a window cannot start and end at the same time")
        return cls(start, end)

    def _night_starting(self, day) -> Night:
        start = datetime.combine(day, self.start)
        end_day = day if self.end > self.start else day + timedelta(days=1)
        end = datetime.combine(end_day, self.end)
        # naive local datetimes: .timestamp() reads them as local time
        return Night(day.isoformat(), start.timestamp(), end.timestamp())

    def current(self, now: float) -> Night | None:
        """The night whose window contains ``now``, or None outside every window."""
        today = datetime.fromtimestamp(now).date()
        for day in (today, today - timedelta(days=1)):
            night = self._night_starting(day)
            if night.contains(now):
                return night
        return None

    def last_ended(self, now: float) -> Night:
        """The most recent night whose window has ended at ``now``."""
        today = datetime.fromtimestamp(now).date()
        for back in range(3):
            night = self._night_starting(today - timedelta(days=back))
            if night.end <= now:
                return night
        raise AssertionError("a window always ended within the last three days")

    def next_start(self, now: float) -> float:
        """When the next window opens (``now`` itself if one is open)."""
        if self.current(now) is not None:
            return now
        today = datetime.fromtimestamp(now).date()
        for ahead in range(3):
            night = self._night_starting(today + timedelta(days=ahead))
            if night.start > now:
                return night.start
        raise AssertionError("a window always opens within three days")

    def describe(self) -> str:
        return f"{self.start:%H:%M}-{self.end:%H:%M} local time"


def can_start(night: Night | None, now: float, budget_seconds: float) -> bool:
    """A job of this budget may start now: inside a window, and able to finish in it."""
    return night is not None and night.contains(now) and now + budget_seconds <= night.end


def not_after(night: Night, now: float, budget_seconds: float) -> float:
    """When Pionir cancels the job, whatever else: its budget, and never past the window."""
    return min(now + budget_seconds, night.end)
