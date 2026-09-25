"""The wall clock.

The crew does real work for real people, so the only honest time is the time on the
wall. ``t`` is whole seconds since the epoch. Nothing advances this clock - it is read,
not ticked - and the time source is injectable so tests never wait on it.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime


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

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.local().strftime("%Y-%m-%d %H:%M:%S")
