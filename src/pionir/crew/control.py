"""When a worker runs, beyond its cadence: parking, retries, and the controls Moss and leaders hold.

The dispatcher (pool.py) used to know one rule: a worker is due once its cadence (scaled by
its fixed jitter) has passed since its last attempt. That rule ran products.api_builder
1,471 times in a row into the same ``not_configured`` wall, every 300 s, and ignored
``WorkerError.retryable`` entirely: a timeout waited a whole cadence, the same as a missing
secret. ``DispatchControl`` adds four things on top of the cadence, and nothing else:

**Parking.** A run that fails ``NOT_CONFIGURED`` and not retryable PARKS the worker: it is
not run at its cadence any more, only probed on an exponential backoff (its cadence x2,
x4, ... capped at ``PARK_CAP_SECONDS``). The log says so ONCE, when it parks. The way back
is automatic: while parked, the worker's own ``readiness()`` is re-checked every
``READINESS_RECHECK_SECONDS`` (on the worker pool, never the loop thread - the API
builder's readiness runs icacls and PowerShell), and the moment it turns from "not ready"
to ready the worker is un-parked and run at once. A probe that succeeds, or that fails any
other way, un-parks it too; so does a run-now. Every probe that runs is still recorded as
an attempt, so the runs table, the vitals and the leaders see exactly what they did.

**Retries.** A retryable failure is retried on a short backoff (``RETRY_DELAYS``: 1 min,
4 min, 16 min), only where the delay is at most half the worker's cadence (otherwise the
cadence comes first anyway), at most ``len(RETRY_DELAYS)`` times per failure streak. Then
it falls back to its normal cadence until it succeeds again; a success resets everything.
A non-retryable failure is never retried early.

**Controls** (Moss, through Pionir's ``crew.*`` capabilities, and leaders): run a worker
NOW, pause and resume it, and scale its cadence by a bounded multiplier
(``MIN_MULTIPLIER``..``MAX_MULTIPLIER``). Compute only - none of them spends or approves
anything; a worker's real-world actions still go through Pionir's gates. A pause and a
multiplier always EXPIRE (at most ``MAX_HOURS``), so neither can latch: what un-sets them
is ``resume`` / a multiplier of 1, or simply their expiry. A leader may ask for a run-now of
its OWN division's workers, at most ``LEADER_RUNS_PER_DAY`` a day per division.

**Visibility.** ``status`` is what /api/divisions shows per worker: its state, why, when it
is next due, its consecutive failures (from the runs table) and every control on it.

State lives in the crew store's ``meta`` table (``STATE_KEY``), so a park's backoff, a
pause and a multiplier survive a restart; a pending run-now does not (it is a "now").
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime

from .log import lesion, log
from .worker import ErrorKind

STATE_KEY = "dispatch_control"

PARK_CAP_SECONDS = 6 * 3600
PARK_MIN_SECONDS = 300
READINESS_RECHECK_SECONDS = 120
RETRY_DELAYS = (60, 240, 960)
MIN_MULTIPLIER = 0.25
MAX_MULTIPLIER = 4.0
MAX_HOURS = 7 * 24
DEFAULT_HOURS = 24
LEADER_RUNS_PER_DAY = 3
RUN_NOW_MIN_SPACING = 60
MAX_REASON_CHARS = 200

_NOT_READY = object()       # readiness could not be read: neither ready nor not


def local_day(t: float) -> str:
    return datetime.fromtimestamp(t).astimezone().strftime("%Y-%m-%d")


def parse_hours(raw, default: float = DEFAULT_HOURS) -> float:
    """How long a pause or a multiplier lasts: more than 0, at most MAX_HOURS."""
    if raw is None:
        return float(default)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"hours is a number from 0 to {MAX_HOURS}, not {raw!r}")  # noqa: TRY004
    if not 0 < raw <= MAX_HOURS:
        raise ValueError(f"hours is more than 0 and at most {MAX_HOURS} (7 days), not {raw}")
    return float(raw)


def parse_multiplier(raw) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"multiplier is a number from {MIN_MULTIPLIER} to {MAX_MULTIPLIER}, "  # noqa: TRY004
                         f"not {raw!r}")
    if not MIN_MULTIPLIER <= raw <= MAX_MULTIPLIER:
        raise ValueError(f"multiplier is from {MIN_MULTIPLIER} (4x as often) to "
                         f"{MAX_MULTIPLIER} (a quarter as often), not {raw}")
    return float(raw)


def parse_reason(raw) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str) or not raw.isprintable():
        raise ValueError("reason is one line of printable text")
    if len(raw.strip()) > MAX_REASON_CHARS:
        raise ValueError(f"reason is at most {MAX_REASON_CHARS} characters")
    return raw.strip()


def _clip(text, n: int = 200) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 3] + "..."


class DispatchControl:
    """Parking, retries and the run-now / pause / cadence controls. Thread-safe: the loop
    thread asks ``due``, pool threads report ``after_run`` and ``recheck``, the API thread
    sets controls."""

    def __init__(self, store, registry, *, secrets_dir=None,
                 clock: Callable[[], float] = time.time,
                 jitter: Callable[[str], float] = lambda _w: 1.0) -> None:
        self.store = store
        self.registry = registry
        self.secrets_dir = secrets_dir
        self._clock = clock
        self._jitter = jitter
        self._lock = threading.RLock()
        self.paused: dict = {}          # wid -> {until, by, reason, since}
        self.multipliers: dict = {}     # wid -> {factor, until, by, since}
        self.parked: dict = {}          # wid -> {since, reason, probes, next_probe_at, gated, ...}
        self.retries: dict = {}         # wid -> {attempt, at, kind, exhausted}
        self.leader_runs: dict = {"day": None, "counts": {}}
        self.run_now: dict = {}         # wid -> {by, at}; in memory: a "now" does not outlive us
        self.last_run_now: dict = {}    # wid -> when it was last asked for
        self.parks_logged = 0
        self._load()

    # ---- persistence -----------------------------------------------------------------
    def _load(self) -> None:
        try:
            doc = self.store.get(STATE_KEY) or {}
        except Exception as exc:  # noqa: BLE001 - start clean, and say so
            lesion("control.load", exc)
            doc = {}
        known = set(self.registry.ids())
        for name in ("paused", "multipliers", "parked", "retries"):
            got = doc.get(name) if isinstance(doc, dict) else None
            if isinstance(got, dict):
                setattr(self, name, {k: dict(v) for k, v in got.items()
                                     if k in known and isinstance(v, dict)})
        lr = doc.get("leader_runs") if isinstance(doc, dict) else None
        if isinstance(lr, dict) and isinstance(lr.get("counts"), dict):
            self.leader_runs = {"day": lr.get("day"), "counts": dict(lr["counts"])}
        for park in self.parked.values():
            park["next_check_at"] = 0.0         # re-check readiness at once after a restart

    def _save(self) -> None:
        doc = {"paused": self.paused, "multipliers": self.multipliers, "parked": self.parked,
               "retries": self.retries, "leader_runs": self.leader_runs}
        try:
            self.store.set(STATE_KEY, doc)
        except Exception as exc:  # noqa: BLE001 - in-memory state still holds; say so
            lesion("control.save", exc)

    # ---- cadence ---------------------------------------------------------------------
    def multiplier(self, worker_id: str, now: float) -> float:
        m = self.multipliers.get(worker_id)
        return float(m["factor"]) if m and now < m["until"] else 1.0

    def cadence(self, worker, now: float) -> float:
        """The worker's cadence as it stands: jitter and any live multiplier applied."""
        return (float(worker.cadence_seconds) * self._jitter(worker.worker_id)
                * self.multiplier(worker.worker_id, now))

    def _expire(self, now: float) -> None:
        changed = False
        for table, what in ((self.paused, "pause"), (self.multipliers, "cadence multiplier")):
            for wid in [w for w, v in table.items() if now >= float(v.get("until", 0))]:
                log.info("crew control: %s's %s expired (set by %s)", wid, what,
                         table[wid].get("by"))
                del table[wid]
                changed = True
        if changed:
            self._save()

    # ---- the dispatcher's questions --------------------------------------------------
    def is_due(self, worker, prev: float | None, now: float) -> bool:
        """Is this worker to run now? Run-now first, then pause, park, retry, cadence."""
        wid = worker.worker_id
        with self._lock:
            self._expire(now)
            if wid in self.paused:
                return False
            if wid in self.run_now:
                return True
            park = self.parked.get(wid)
            if park is not None:
                return now >= float(park["next_probe_at"])
            retry = self.retries.get(wid)
            if retry and not retry.get("exhausted") and retry.get("at") is not None \
                    and now >= float(retry["at"]):
                return True
        return prev is None or now - prev >= self.cadence(worker, now)

    def rechecks_due(self, now: float) -> list:
        """Parked worker ids whose readiness is due another look."""
        with self._lock:
            return [wid for wid, p in self.parked.items()
                    if wid not in self.paused and now >= float(p.get("next_check_at", 0))]

    def recheck(self, worker) -> bool:
        """Read a parked worker's readiness (on a pool thread). True when it turned ready and
        was un-parked - it is then queued to run at once."""
        wid = worker.worker_id
        ready = self._readiness(worker)
        now = self._clock()
        with self._lock:
            park = self.parked.get(wid)
            if park is None:
                return False
            park["next_check_at"] = now + READINESS_RECHECK_SECONDS
            park["checks"] = int(park.get("checks", 0)) + 1
            if ready is _NOT_READY:
                return False
            if ready is not None:
                park["gated"] = True
                park["readiness"] = _clip(ready)
                return False
            if not park.get("gated"):
                return False        # readiness never knew why: only a probe can tell
            self._unpark(wid, "its readiness turned ok", now)
            self.run_now[wid] = {"by": "readiness", "at": now}
            return True

    def _readiness(self, worker):
        fn = getattr(worker, "readiness", None)
        if not callable(fn) or not getattr(worker, "live", True):
            return _NOT_READY
        try:
            return fn(self.secrets_dir)
        except Exception as exc:  # noqa: BLE001 - unknown, not ready and not ok
            lesion("control.readiness", exc)
            return _NOT_READY

    def started(self, worker_id: str) -> None:
        """A run of this worker is starting: it answers any run-now asked before now."""
        with self._lock:
            self.run_now.pop(worker_id, None)

    def after_run(self, worker, error, now: float) -> None:
        """Fold one finished run into the park and retry state."""
        wid = worker.worker_id
        with self._lock:
            before = (wid in self.parked, dict(self.retries.get(wid) or {}))
            if error is None:
                if wid in self.parked:
                    self._unpark(wid, "it ran ok", now)
                self.retries.pop(wid, None)
            elif error.kind == ErrorKind.NOT_CONFIGURED and not error.retryable:
                self.retries.pop(wid, None)
                self._park(worker, error, now)
            else:
                if wid in self.parked:
                    self._unpark(wid, f"it is configured now (it failed {error.kind} "
                                      "instead)", now)
                if error.retryable:
                    self._schedule_retry(worker, error, now)
                else:
                    self.retries.pop(wid, None)
            after = (wid in self.parked, dict(self.retries.get(wid) or {}))
            if after != before or wid in self.parked:
                self._save()
        if wid in self.parked and self.parked[wid].get("gated") is None:
            # does its readiness know why? (read outside the lock: it may be slow)
            ready = self._readiness(worker)
            with self._lock:
                park = self.parked.get(wid)
                if park is not None and park.get("gated") is None:
                    park["gated"] = ready is not None and ready is not _NOT_READY
                    if park["gated"]:
                        park["readiness"] = _clip(ready)
                    self._save()

    def _park(self, worker, error, now: float) -> None:
        wid = worker.worker_id
        park = self.parked.get(wid)
        probes = 0 if park is None else int(park.get("probes", 0)) + 1
        base = max(float(worker.cadence_seconds), PARK_MIN_SECONDS)
        delay = min(PARK_CAP_SECONDS, base * 2 ** (probes + 1))
        if park is None:
            park = {"since": now, "gated": None, "checks": 0}
            self.parks_logged += 1
            log.warning("crew control: %s PARKED - not configured: %s. It is no longer run "
                        "every %d s; it is probed after %d s (backing off to %d h) and "
                        "run again at once when its readiness turns ok.", wid,
                        _clip(error.message), int(worker.cadence_seconds), int(delay),
                        PARK_CAP_SECONDS // 3600)
        park.update(reason=_clip(error.message), probes=probes, last_probe_at=now,
                    next_probe_at=now + delay,
                    next_check_at=now + READINESS_RECHECK_SECONDS)
        self.parked[wid] = park

    def _unpark(self, wid: str, why: str, now: float) -> None:
        park = self.parked.pop(wid, None)
        if park is not None:
            log.info("crew control: %s un-parked after %.0f min: %s", wid,
                     (now - float(park.get("since", now))) / 60, why)

    def _schedule_retry(self, worker, error, now: float) -> None:
        wid = worker.worker_id
        prev = self.retries.get(wid)
        if prev and prev.get("exhausted"):
            return                      # this failure streak already had its retries
        attempt = 1 if prev is None else int(prev.get("attempt", 0)) + 1
        cadence = self.cadence(worker, now)
        delay = RETRY_DELAYS[attempt - 1] if attempt <= len(RETRY_DELAYS) else None
        if delay is None or delay > cadence / 2:
            self.retries[wid] = {"attempt": attempt - 1, "at": None, "kind": str(error.kind),
                                 "exhausted": True}
            return
        self.retries[wid] = {"attempt": attempt, "at": now + delay, "kind": str(error.kind),
                             "exhausted": False}

    # ---- the controls ----------------------------------------------------------------
    def _worker(self, worker_id):
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker names one of the crew's workers")
        try:
            return self.registry.require(worker_id.strip())
        except KeyError as exc:
            raise ValueError(str(exc).strip("'\"")) from exc

    def request_run(self, worker_id: str, *, by: str, division: str | None = None,
                    in_flight: bool = False) -> dict:
        """Run a worker at the next tick. ``division`` is set when a LEADER asks: then only
        its own division's workers, at most LEADER_RUNS_PER_DAY a day."""
        w = self._worker(worker_id)
        now = self._clock()
        with self._lock:
            self._expire(now)
            if w.worker_id in self.paused:
                p = self.paused[w.worker_id]
                raise ValueError(f"{w.worker_id} is paused until {_when(p['until'])} "
                                 f"(by {p.get('by')}); resume it first")
            if division is not None:
                if w.division != division:
                    raise ValueError(f"{w.worker_id} is not in the {division} division")
                day = local_day(now)
                if self.leader_runs.get("day") != day:
                    self.leader_runs = {"day": day, "counts": {}}
                used = int(self.leader_runs["counts"].get(division, 0))
                if used >= LEADER_RUNS_PER_DAY:
                    raise ValueError(f"the {division} leader has used its "
                                     f"{LEADER_RUNS_PER_DAY} run-now requests for today")
            if w.worker_id in self.run_now:
                return self._answer(w, now, queued=True, note="already queued")
            last = self.last_run_now.get(w.worker_id)
            if last is not None and now - last < RUN_NOW_MIN_SPACING:
                raise ValueError(f"{w.worker_id} was asked to run {now - last:.0f} s ago; at "
                                 f"most once every {RUN_NOW_MIN_SPACING} s")
            if division is not None:
                self.leader_runs["counts"][division] = used + 1
                self._save()
            self.run_now[w.worker_id] = {"by": by, "at": now}
            self.last_run_now[w.worker_id] = now
        log.info("crew control: %s asked to run now by %s", w.worker_id, by)
        return self._answer(w, now, queued=True,
                            note="running now; it will run again when this run ends"
                            if in_flight else "")

    def pause(self, worker_id: str, *, by: str, hours=None, reason=None) -> dict:
        w = self._worker(worker_id)
        hours = parse_hours(hours)
        reason = parse_reason(reason)
        now = self._clock()
        with self._lock:
            self.paused[w.worker_id] = {"since": now, "until": now + hours * 3600, "by": by,
                                        "reason": reason}
            self.run_now.pop(w.worker_id, None)
            self._save()
        log.info("crew control: %s paused for %g h by %s%s", w.worker_id, hours, by,
                 f" ({reason})" if reason else "")
        return self._answer(w, now)

    def resume(self, worker_id: str, *, by: str) -> dict:
        w = self._worker(worker_id)
        now = self._clock()
        with self._lock:
            was = self.paused.pop(w.worker_id, None)
            if was is not None:
                self._save()
        if was is not None:
            log.info("crew control: %s resumed by %s", w.worker_id, by)
        return self._answer(w, now, note="" if was is not None else "it was not paused")

    def set_cadence(self, worker_id: str, multiplier, *, by: str, hours=None) -> dict:
        w = self._worker(worker_id)
        factor = parse_multiplier(multiplier)
        hours = parse_hours(hours)
        now = self._clock()
        with self._lock:
            if factor == 1.0:
                self.multipliers.pop(w.worker_id, None)
            else:
                self.multipliers[w.worker_id] = {"factor": factor, "since": now,
                                                 "until": now + hours * 3600, "by": by}
            self._save()
        log.info("crew control: %s cadence x%g for %g h by %s", w.worker_id, factor, hours, by)
        return self._answer(w, now)

    def _answer(self, w, now: float, **extra) -> dict:
        out = {"worker": w.worker_id, "control": self.status(w, now)}
        out.update({k: v for k, v in extra.items() if v != ""})
        return out

    # ---- what a viewer sees ----------------------------------------------------------
    def status(self, worker, now: float, *, health=None, prev: float | None = None,
               in_flight: bool = False) -> dict:
        """One worker's dispatch state for /api/divisions. ``health`` (store.Health) gives
        the consecutive failures; ``prev`` its last attempt."""
        wid = worker.worker_id
        with self._lock:
            paused = dict(self.paused.get(wid) or {}) or None
            if paused and now >= paused["until"]:
                paused = None
            mult = dict(self.multipliers.get(wid) or {}) or None
            if mult and now >= mult["until"]:
                mult = None
            park = dict(self.parked.get(wid) or {}) or None
            retry = dict(self.retries.get(wid) or {}) or None
            asked = dict(self.run_now.get(wid) or {}) or None
        cadence = self.cadence(worker, now)
        if paused:
            state, next_at = "paused", None
        elif in_flight:
            state, next_at = "running", None
        elif asked:
            state, next_at = "run_requested", now
        elif park:
            state, next_at = "parked", park.get("next_probe_at")
        elif retry and not retry.get("exhausted") and retry.get("at") is not None:
            state, next_at = "retrying", retry["at"]
        else:
            state = "scheduled"
            next_at = None if prev is None else prev + cadence
        out = {"state": state, "next_due_at": next_at,
               "cadence_seconds": round(cadence, 1),
               "consecutive_failures": (health.consecutive_failures
                                        if health is not None else None)}
        if paused:
            out["paused"] = {"until": paused["until"], "by": paused.get("by"),
                             "reason": paused.get("reason") or None}
        if mult:
            out["cadence_multiplier"] = {"factor": mult["factor"], "until": mult["until"],
                                         "by": mult.get("by")}
        if park:
            out["parked"] = {"since": park.get("since"), "reason": park.get("reason"),
                             "probes": park.get("probes", 0),
                             "next_probe_at": park.get("next_probe_at"),
                             "readiness_gated": bool(park.get("gated")),
                             "readiness": park.get("readiness")}
        if retry:
            out["retry"] = {"attempt": retry.get("attempt"), "at": retry.get("at"),
                            "kind": retry.get("kind"),
                            "exhausted": bool(retry.get("exhausted"))}
        if asked:
            out["run_requested_by"] = asked.get("by")
        return out

    def summary(self, now: float) -> dict:
        """{parked: [...], paused: [...], retrying: [...], multiplied: [...]} by worker id."""
        with self._lock:
            self._expire(now)
            return {"parked": sorted(self.parked), "paused": sorted(self.paused),
                    "retrying": sorted(w for w, r in self.retries.items()
                                       if not r.get("exhausted") and r.get("at") is not None),
                    "multiplied": sorted(self.multipliers)}


def _when(t: float) -> str:
    return datetime.fromtimestamp(float(t)).astimezone().strftime("%Y-%m-%d %H:%M")
