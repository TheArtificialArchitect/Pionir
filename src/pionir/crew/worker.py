"""The contract every worker implements, and the record it emits.

A worker is a **job, not an agent** - Peter's specialist (``C:\\src\\The-Web``, ADR-0001:
"the 300th specialist is a config line and costs nothing per cycle"). It does exactly
one narrow thing - read the ledger, check the site is up - and returns typed records.
It has no temperament, no mood, no memory of its own and no model. The owner's rule is
that personality in workers costs GPU and memory for nothing; the one mind is Moss.

If a job genuinely needs words (drafting a post), the worker asks for them through
``WorkContext.words``, which goes to the crew's one shared brain with JSON-schema
structured output, charged to the worker's division. That is the ONLY way a worker
touches a model, and an output built from such words is marked ``derived`` so its
numbers can never back a report (grounding.py). Real-world actions go through
``WorkContext.job`` -> Pionir's ``/api/task`` (hands.py), where privileged actions are
parked for the owner's approval.

**A worker answers with a Result, never with an exception** (``never_raises``). A dead
endpoint, a 429 or a renamed JSON field must become an ``Err`` the dispatcher records,
not a traceback that takes the cycle down - and not an empty tuple, which would read
exactly like "nothing to report".

**Two timestamps, always both.** ``valid_at`` is when the fact was true at the source;
``observed_at`` is when the crew saw it. A dying feed shows the first standing still
while the second moves.

**The id is a hash of the fact, not the sighting**, so re-reading an unchanged fact does
not mint a second row.
"""
from __future__ import annotations

import functools
import hashlib
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .figures import Figure
from .result import Err, Result

log = logging.getLogger("pionir.crew")

#: A worker that has not succeeded in this many of its own cadences is STALE.
STALE_AFTER_CADENCES = 3


class ErrorKind(StrEnum):
    TIMEOUT = "timeout"
    HTTP_ERROR = "http_error"
    MALFORMED = "malformed"
    RATE_LIMITED = "rate_limited"
    AUTH = "auth"
    UNAVAILABLE = "unavailable"
    NOT_CONFIGURED = "not_configured"   # a secret or setting it needs is missing
    NOT_WIRED = "not_wired"             # a placeholder: this job has not been built yet
    NO_WORDS = "no_words"               # the shared brain refused or failed
    UNGROUNDED = "ungrounded"           # a leader's report stated what nothing recorded


@dataclass(frozen=True, slots=True)
class WorkerError:
    worker_id: str
    kind: ErrorKind
    message: str
    retryable: bool = True

    def __str__(self) -> str:
        return f"[{self.worker_id}/{self.kind}] {self.message}"


@dataclass(frozen=True, slots=True)
class Output:
    """One fact from one worker. Built only through ``make_output``."""

    output_id: str
    worker_id: str
    division: str
    kind: str
    valid_at: float          # epoch seconds: when it was true at the source
    observed_at: float       # epoch seconds: when the crew saw it
    payload: dict
    figures: tuple = ()      # of Figure
    entities: tuple = ()     # names this fact actually carries
    provenance: dict = field(default_factory=dict)

    @property
    def derived(self) -> bool:
        """True when a model produced any of it - never a source of backing figures."""
        return bool(self.provenance.get("derived"))


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def make_output(worker, *, valid_at: float, observed_at: float, payload: dict,
                figures: Sequence[Figure] = (), entities: Sequence[str] = (),
                provenance: dict | None = None, kind: str | None = None) -> Output:
    """The only constructor a worker should use: it types the figures and derives the id."""
    figs = tuple(f if isinstance(f, Figure) else Figure.from_dict(f) for f in figures)
    kind = kind or worker.kind
    seed = "\x1f".join((worker.worker_id, kind, repr(float(valid_at)), _canonical(payload),
                        _canonical([f.to_dict() for f in figs])))
    return Output(
        output_id=hashlib.sha256(seed.encode()).hexdigest()[:32],
        worker_id=worker.worker_id, division=worker.division, kind=kind,
        valid_at=float(valid_at), observed_at=float(observed_at), payload=dict(payload),
        figures=figs, entities=tuple(str(e) for e in entities if str(e).strip()),
        provenance=dict(provenance or {}),
    )


@dataclass
class WorkContext:
    """What a worker is handed for one run. Everything outside it is injected, so a test
    worker never reaches a network, a model or Pionir."""

    now: float
    http: Any                              # net.Http
    secrets_dir: Path
    # (purpose, system, user, schema) -> Result[dict, WorkerError]; the only model path
    words: Callable[..., Result] | None = None
    # (hands.Job) -> hands.JobOutcome; the only way to act in the world
    job: Callable[[Any], Any] | None = None
    # (approval_id) -> Pionir's approval record for a job it parked (hands.Hands.approval)
    approval: Callable[[str], dict] | None = None
    # this worker's division's goal, as Moss set it (direction.py), or None
    goal: str | None = None
    # where a worker that must remember what it did keeps its own small record
    state_dir: Path | None = None
    # where the owner drops each order's finished work (config.CrewSettings.deliveries_dir)
    deliveries_dir: Path | None = None


@runtime_checkable
class Worker(Protocol):
    """What the dispatcher needs from a worker, and nothing more."""

    worker_id: str
    division: str
    kind: str
    cadence_seconds: int
    provider: str
    stage: int
    live: bool          # False for a placeholder that honestly reports "not wired yet"

    def run(self, ctx: WorkContext) -> Result: ...


def never_raises(kind: ErrorKind = ErrorKind.UNAVAILABLE):
    """Convert any escaping exception into an ``Err``. ``BaseException`` is deliberately
    not caught - Ctrl+C and SystemExit must still stop the process."""

    def decorate(fn: Callable[..., Result]) -> Callable[..., Result]:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs) -> Result:
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - this IS the boundary
                who = getattr(args[0], "worker_id", "unknown") if args else "unknown"
                log.warning("worker %s raised %s: %s", who, type(exc).__name__, exc)
                return Err(WorkerError(str(who), kind, f"{type(exc).__name__}: {exc}"))

        return wrapper

    return decorate
