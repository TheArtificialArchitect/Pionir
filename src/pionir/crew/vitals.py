"""Vitals: does each worker and leader actually produce anything?

The most expensive failure in this estate is the thing that boots green, logs plausibly
and does nothing. The rule is to watch the OUTPUT, not the verdict log - so every check
here reads the runs table (store.py), which records every attempt, and compares what a
worker produced against the chances it has had:

- **never succeeded**: tried ``NEVER_AFTER`` times and not once succeeded. A placeholder
  that honestly says "not wired yet" is listed separately (``not_wired``), and a worker
  missing its secret is called out as NOT CONFIGURED - both loud, neither faked.
- **silent**: succeeded ``SILENT_AFTER`` times in a row and produced nothing. It reads as
  green everywhere else; this is the only place it is said aloud.
- **stale**: once succeeded, but not within ``STALE_AFTER_CADENCES`` of its cadence.

Every check is edge-triggered and CLEARS when its condition passes, so none of this is a
latch: the first row a silent worker writes retires its warning for good.
"""
from __future__ import annotations

from collections.abc import Callable

from .log import log

NEVER_AFTER = 3
SILENT_AFTER = 3
CHECK_EVERY_SECONDS = 300


def _complaints(h) -> dict:
    """check id -> the sentence, for every check this health row currently fails."""
    out = {}
    if h.not_wired:
        out["not_wired"] = "is a placeholder: not wired yet, so it produces nothing"
    elif h.not_configured:
        out["not_configured"] = f"is NOT CONFIGURED and cannot run: {h.last_error}"
    elif h.has_never_succeeded and h.attempts >= NEVER_AFTER:
        out["never"] = (f"has been tried {h.attempts} times and has never once succeeded "
                        f"(last: {h.last_error_kind}: {h.last_error})")
    elif h.is_stale and not h.has_never_succeeded:
        out["stale"] = f"is stale: {h.consecutive_failures} failures since its last success"
    if h.silent_streak >= SILENT_AFTER:
        out["silent"] = (f"has succeeded {h.silent_streak} times in a row and produced "
                         "nothing")
    return out


class Vitals:
    """Edge-triggered, so a standing problem is said once and a fixed one is said once."""

    def __init__(self, store, cadences: Callable[[], dict],
                 clock: Callable[[], float]) -> None:
        self.store = store
        self.cadences = cadences          # -> {worker or leader id: cadence seconds}
        self._clock = clock
        self.open: dict = {}              # (id, check) -> sentence
        self.raised = 0
        self.cleared = 0
        self.last_t = -10.0 ** 12

    def check(self, force: bool = False) -> list:
        now = self._clock()
        if not force and now - self.last_t < CHECK_EVERY_SECONDS:
            return self.report()
        self.last_t = now
        seen: dict = {}
        for h in self.store.health(self.cadences(), now):
            for cid, sentence in _complaints(h).items():
                seen[(h.worker_id, cid)] = sentence
        for key, sentence in seen.items():
            if key not in self.open:
                self.raised += 1
                warn = log.info if key[1] == "not_wired" else log.warning
                warn("VITALS: %s %s", key[0], sentence)
        for key in list(self.open):
            if key not in seen:
                self.cleared += 1
                log.info("VITALS cleared: %s (%s)", key[0], key[1])
        self.open = seen
        return self.report()

    def report(self) -> list:
        return [{"who": wid, "check": cid, "says": s}
                for (wid, cid), s in sorted(self.open.items())]

    def to_dict(self) -> dict:
        return {"open": self.report(), "raised": self.raised, "cleared": self.cleared}
