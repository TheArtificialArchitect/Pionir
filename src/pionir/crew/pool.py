"""Running workers: which are due, several at once, politely, every attempt recorded.

Peter runs its specialists one after another. The crew runs several at once in a
BOUNDED pool (``max_workers``), because a worker spends its time waiting on a network,
and that waiting should overlap. Concurrency brings three rules with it:

**Politeness per provider survives concurrency.** ``ProviderGate`` hands out start
slots per provider under one lock: a slot is reserved (``next_free += interval``) before
the thread sleeps to it, so two threads can never both take "now" for the same host.
A 429 costs far more than the pause, and reads like a dead feed until someone looks.

**Fixed per-id jitter** (``jitter``): each worker's cadence is scaled by a stable
multiplier from its id, so workers that failed together do not come due together for
ever after. Deterministic, so a staleness reading is reproducible across restarts.

**One writer.** Workers never touch SQLite themselves: the dispatcher records each
attempt through ``CrewStore.record_attempt``, which hands the write to the store's one
writer thread (store.py). ``record_attempt`` is called on EVERY path out of a run -
success, ``Err``, an exception that escaped ``never_raises``, a malformed ``Ok``.

A worker cannot stop the pool: ``run`` is contractually incapable of raising, and is
still called inside a guard, because "contractually" describes the decorated workers and
not a future one that forgets the decorator.
"""
from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as wait_all
from dataclasses import dataclass, field

from .log import lesion, log
from .result import Err, Ok
from .worker import ErrorKind, Output, WorkerError

JITTER_SPREAD = 0.30
DEFAULT_MIN_INTERVAL = 0.3


def jitter(worker_id: str) -> float:
    """A stable multiplier in [0.85, 1.15] for one worker, derived from its id."""
    h = int(hashlib.sha256(worker_id.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return 1.0 - JITTER_SPREAD / 2 + h * JITTER_SPREAD


def interleave(workers: Sequence) -> list:
    """Round-robin across providers, so one host's workers are not fired back to back."""
    lanes: dict = {}
    for w in workers:
        lanes.setdefault(w.provider, []).append(w)
    out: list = []
    while lanes:
        for p in list(lanes):
            out.append(lanes[p].pop(0))
            if not lanes[p]:
                del lanes[p]
    return out


class ProviderGate:
    """Minimum seconds between two starts against the same provider, across threads."""

    def __init__(self, intervals: dict, *, default: float = DEFAULT_MIN_INTERVAL,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.intervals = dict(intervals)
        self.default = default
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_free: dict = {}
        self.waited_seconds = 0.0

    def wait_turn(self, provider: str) -> float:
        """Block until this provider may be called again; returns how long it waited."""
        interval = float(self.intervals.get(provider, self.default))
        with self._lock:                        # reserve the slot BEFORE sleeping to it
            now = self._clock()
            slot = max(now, self._next_free.get(provider, now))
            self._next_free[provider] = slot + interval
            self.waited_seconds += slot - now
        if slot > now:
            self._sleep(slot - now)
        return slot - now


@dataclass
class DispatchReport:
    """What one dispatch produced."""

    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    written: int = 0
    skipped: int = 0              # not due yet
    busy: int = 0                 # due, but its previous run is still going
    errors: list = field(default_factory=list)

    @property
    def silent_success(self) -> bool:
        """Every worker reported success and not one new row appeared. Expected on a
        quiet re-read; also exactly what a frozen source looks like."""
        return self.attempted > 0 and self.failed == 0 and self.written == 0


class Dispatcher:
    def __init__(self, registry, store, *, context: Callable, gate: ProviderGate,
                 max_workers: int = 4, clock: Callable[[], float] = time.time) -> None:
        if max_workers <= 0:
            raise ValueError("max_workers must be positive")
        self.registry = registry
        self.store = store
        self._context = context                 # worker -> WorkContext
        self.gate = gate
        self._clock = clock
        self._pool = ThreadPoolExecutor(max_workers=max_workers,
                                        thread_name_prefix="pionir-crew-worker")
        self._lock = threading.Lock()
        self._in_flight: set = set()
        self.max_workers = max_workers

    def due(self, now: float | None = None, only: Sequence[str] | None = None) -> tuple:
        """(due workers in run order, how many were skipped as not due). ``only`` names
        workers to run regardless of cadence; an unknown name is an error, not an empty
        pass, because it is a caller bug and must not read as nothing to report."""
        now = self._clock() if now is None else now
        if only is not None:
            return [self.registry.require(w) for w in only], 0
        last = self.store.last_attempts()
        due, skipped = [], 0
        for w in self.registry.all():
            prev = last.get(w.worker_id)
            if prev is not None and now - prev < w.cadence_seconds * jitter(w.worker_id):
                skipped += 1
                continue
            due.append(w)
        by_stage: dict = {}
        for w in due:
            by_stage.setdefault(w.stage, []).append(w)
        return [w for s in sorted(by_stage) for w in interleave(by_stage[s])], skipped

    def dispatch(self, now: float | None = None, *, only: Sequence[str] | None = None,
                 wait: bool = False) -> DispatchReport:
        """Submit every due worker to the pool. With ``wait`` the report includes their
        outcomes; without it, it counts what was submitted."""
        workers, skipped = self.due(now, only)
        report = DispatchReport(skipped=skipped)
        futures: list = []
        for w in workers:
            with self._lock:
                if w.worker_id in self._in_flight:
                    report.busy += 1
                    continue
                self._in_flight.add(w.worker_id)
            try:
                futures.append(self._pool.submit(self.run_one, w))
            except RuntimeError:                 # the pool is shutting down
                with self._lock:
                    self._in_flight.discard(w.worker_id)
                break
        report.attempted = len(futures)
        if wait:
            wait_all(futures)
            for f in futures:
                error, written = f.result()
                report.written += written
                if error is None:
                    report.succeeded += 1
                else:
                    report.failed += 1
                    report.errors.append(error)
        return report

    def run_one(self, worker) -> tuple:
        """Run one worker and record the attempt, whatever happens. -> (error, written)."""
        error: WorkerError | None = None
        outputs: list = []
        started = self._clock()
        try:
            self.gate.wait_turn(worker.provider)
            started = self._clock()
            result = worker.run(self._context(worker))
            if isinstance(result, Err):
                error = result.error
                value = getattr(error, "partial", ())
            elif isinstance(result, Ok):
                value = result.value
                if not isinstance(value, (tuple, list)):
                    raise TypeError(f"{worker.worker_id} returned Ok({type(value).__name__}); "
                                    "a single Output is not a one-element tuple")
            else:
                raise TypeError(f"{worker.worker_id} returned {type(result).__name__}, "
                                "not Ok or Err")
            for o in value:
                if not isinstance(o, Output) or o.worker_id != worker.worker_id:
                    raise TypeError(f"{worker.worker_id} returned {o!r}, not its own Output")
            outputs = list(value)
        except Exception as exc:  # noqa: BLE001 - recorded as the run's error, below
            error = WorkerError(worker.worker_id, ErrorKind.UNAVAILABLE,
                                f"escaped the contract: {type(exc).__name__}: {exc}")
            log.warning("worker %s raised past never_raises: %s", worker.worker_id, exc)
            outputs = []
        written = 0
        try:
            written = self.store.record_attempt(
                worker_id=worker.worker_id, division=worker.division, started_at=started,
                finished_at=self._clock(), error=error, outputs=outputs)
        except Exception as exc:  # noqa: BLE001 - a store failure must be loud, not fatal
            lesion("pool.record_attempt", exc)
        finally:
            with self._lock:
                self._in_flight.discard(worker.worker_id)
        if error is not None:
            log.warning("worker %s: %s", worker.worker_id, error)
        return error, written

    def in_flight(self) -> list:
        with self._lock:
            return sorted(self._in_flight)

    def shutdown(self, wait: bool = True) -> None:
        self._pool.shutdown(wait=wait, cancel_futures=True)
