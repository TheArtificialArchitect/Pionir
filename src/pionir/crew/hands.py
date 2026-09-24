"""The crew's hands: asking Pionir to do something real, and hearing what happened.

The crew runs in its own process and reaches Pionir over HTTP exactly as Moss does
(``C:\\src\\Galatea\\galatea\\hands.py``): ``POST /api/task`` with a capability and a
payload, then ``GET /api/task/<id>`` while it is still running. Pionir, not this file,
decides what is safe: a privileged capability arriving without its permission is parked
for Ian and comes back ``pending_approval`` - it has NOT run.

Every answer is normalised into a ``JobOutcome`` whose ``status`` is one of:

    done              Pionir ran it and its own verdict was ok
    pending_approval  parked for Ian's yes; it has not run
    running           still going when we stopped following it; no result yet
    failed            Pionir answered, and it did not work (refused, invalid, errored)
    unreachable       no usable answer came back at all; we do not know that it ran

Only ``done`` is ever recorded as something the agent did (agent.py). Anything Pionir
said that is not plainly one of the others is ``failed``, never assumed done.

A call never runs inside the tick: the tick holds the crew's lock, and waiting on HTTP
there would freeze every agent. ``Hands`` is one worker thread behind a queue, like the
brain; the outcome comes back through a callback under the crew's lock, between ticks.
The client is injected, so no test ever reaches a network.
"""
from __future__ import annotations

import collections
import http.client
import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import quote, urlparse

from .grounding import values_in_data
from .log import lesion, log

_MAX_BYTES = 2_000_000
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
STATUSES = ("done", "pending_approval", "running", "failed", "unreachable")


class PionirUnreachable(RuntimeError):
    pass


@dataclass
class JobOutcome:
    status: str
    capability: str = ""
    task_id: str = ""
    approval_id: str = ""
    agent_id: str = ""
    answer: str = ""
    error: str = ""
    figures: list = field(default_factory=list)
    evidence: list = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"unknown job status {self.status!r}")

    @property
    def ran(self) -> bool:
        return self.status == "done"


def _answer_of(result) -> str:
    """The human-facing text out of a specialist's result, whatever shape it is."""
    if isinstance(result, str):
        return result.strip()[:2000]
    if not isinstance(result, dict):
        return ""
    for key in ("reply", "answer", "summary", "result", "message"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:2000]
    try:
        return json.dumps(result, ensure_ascii=False, default=str)[:1000]
    except (TypeError, ValueError) as exc:
        log.warning("hands: a result could not be rendered: %s", exc)
        return ""


def _error_of(doc: dict) -> str:
    err = doc.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err.get("type") or "error")
    return str(err or doc.get("reason") or "")


def outcome_of(capability: str, doc) -> JobOutcome:
    """One of Pionir's answers -> a JobOutcome. Never assumes success."""
    if not isinstance(doc, dict):
        return JobOutcome("failed", capability, error="Pionir answered with something that is "
                                                       "not an object; not counted as done")
    task_id = str(doc.get("task_id") or "")
    status = doc.get("status")
    if status == "pending_approval":
        return JobOutcome("pending_approval", capability, task_id=task_id,
                          approval_id=str(doc.get("approval_id") or ""),
                          answer=str(doc.get("summary") or ""),
                          error=str(doc.get("note") or "held for Ian's approval - it has not run"))
    if status == "running":
        return JobOutcome("running", capability, task_id=task_id)
    if status == "error" or doc.get("ok") is False:
        return JobOutcome("failed", capability, task_id=task_id,
                          agent_id=str(doc.get("agent_id") or ""),
                          error=_error_of(doc) or "Pionir reported it did not work")
    if doc.get("ok") is True:
        result = doc.get("result")
        return JobOutcome("done", capability, task_id=task_id,
                          agent_id=str(doc.get("agent_id") or ""),
                          answer=_answer_of(result),
                          figures=sorted(values_in_data(result)),
                          evidence=[str(e) for e in (doc.get("evidence") or [])][:10])
    return JobOutcome("failed", capability, task_id=task_id,
                      error=_error_of(doc) or "Pionir's answer carried no outcome; "
                                              "not counted as done")


class PionirClient:
    """Loopback only: the crew talks to the local Pionir and nothing else."""

    def __init__(self, url: str = "http://127.0.0.1:8780", *, timeout: float = 60.0) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "http" or parsed.hostname not in LOOPBACK:
            raise ValueError(f"the crew reaches Pionir over loopback only, not {url!r}")
        self.url = url.rstrip("/")
        self.timeout = timeout
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _call(self, method: str, path: str, body: dict | None, timeout: float) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.url + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
        try:
            with self._opener.open(request, timeout=timeout) as response:
                raw = response.read(_MAX_BYTES)
        except urllib.error.HTTPError as exc:
            try:
                doc = json.loads(exc.read(_MAX_BYTES).decode("utf-8"))
            except (ValueError, OSError) as inner:
                raise PionirUnreachable(f"HTTP {exc.code} ({inner})") from exc
            if isinstance(doc, dict):
                doc.setdefault("ok", False)
                doc.setdefault("error", f"HTTP {exc.code}")
                return doc
            raise PionirUnreachable(f"HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            raise PionirUnreachable(f"{type(exc).__name__}: {exc}") from exc
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise PionirUnreachable(f"unreadable answer: {exc}") from exc
        if not isinstance(doc, dict):
            raise PionirUnreachable("Pionir returned a non-object response")
        return doc

    def run_task(self, capability: str, payload: dict, *, permissions: tuple = (),
                 wait: float = 30.0) -> dict:
        body = {"capability": capability, "payload": payload,
                "permissions": list(permissions), "wait": wait}
        return self._call("POST", "/api/task", body, self.timeout + wait)

    def task(self, task_id: str, *, wait: float = 0.0) -> dict:
        return self._call("GET", f"/api/task/{quote(task_id)}?wait={wait:g}", None,
                          self.timeout + wait)


class _Req:
    __slots__ = ("agent_id", "callback", "id", "job", "queued_at")

    def __init__(self, rid, agent_id, job, callback, queued_at) -> None:
        self.id = rid
        self.agent_id = agent_id
        self.job = job
        self.callback = callback
        self.queued_at = queued_at


class Hands:
    """One worker, one job at a time, outcome back under the crew's lock."""

    def __init__(self, cfg, sim, client, *, now: Callable[[], float] = time.monotonic) -> None:
        self.cfg = cfg
        self.sim = sim
        self.client = client
        self._now = now
        self.follow_seconds = float(getattr(cfg, "job_follow_seconds", 600.0))
        self.poll_seconds = float(getattr(cfg, "job_poll_seconds", 20.0))
        self._q: collections.deque = collections.deque()
        self._cv = threading.Condition()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="pionir-crew-hands", daemon=True)
        self._next_id = 1
        self.busy_with: _Req | None = None
        self.counts: collections.Counter = collections.Counter()
        self.held_for_pause = 0
        self.last_error: str | None = None

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    def submit(self, agent_id: str, job, callback) -> int:
        with self._cv:
            rid = self._next_id
            self._next_id += 1
            self._q.append(_Req(rid, agent_id, job, callback, self._now()))
            self._cv.notify()
        self.counts["submitted"] += 1
        return rid

    def waiting(self) -> list:
        with self._cv:
            return [{"id": r.id, "agent": r.agent_id, "capability": r.job.capability}
                    for r in self._q]

    # ---- the worker -----------------------------------------------------
    def step(self) -> bool:
        """Wait for a job, run it, deliver the outcome. False when stopping. The thread is
        only this in a loop, so tests drive it directly."""
        with self._cv:
            while not self._q and not self._stop.is_set():
                self._cv.wait(0.5)
            if self._stop.is_set():
                return False
        if getattr(self.sim, "paused_reason", None) is not None:
            # a paused crew starts nothing in the world; the job keeps its place
            self.held_for_pause += 1
            self._stop.wait(0.25)
            return True
        with self._cv:
            if not self._q:
                return True
            req = self._q.popleft()
        self._serve(req)
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if not self.step():
                    return
            except Exception as exc:  # noqa: BLE001 - the worker must outlive one bad job
                lesion("hands.step", exc)
                self._stop.wait(1.0)

    def _serve(self, req: _Req) -> None:
        self.busy_with = req
        job = req.job
        try:
            out = self._run_job(job)
        except PionirUnreachable as exc:
            out = JobOutcome("unreachable", job.capability, error=str(exc))
        except Exception as exc:  # noqa: BLE001 - counted and logged; the job is NOT done
            lesion("hands.run", exc)
            out = JobOutcome("unreachable", job.capability,
                             error=f"the crew could not read Pionir's answer: "
                                   f"{type(exc).__name__}: {exc}")
        self.counts[out.status] += 1
        if out.status in ("failed", "unreachable"):
            self.last_error = out.error
            log.warning("hands: %s for %s -> %s: %s", job.capability, req.agent_id,
                        out.status, out.error)
        else:
            log.info("hands: %s for %s -> %s", job.capability, req.agent_id, out.status)
        try:
            with self.sim.lock:
                req.callback(out)
        except Exception as exc:  # noqa: BLE001
            lesion("hands.callback", exc)
        self.busy_with = None

    def _run_job(self, job) -> JobOutcome:
        doc = self.client.run_task(job.capability, dict(job.payload),
                                   permissions=tuple(job.permissions), wait=job.wait)
        out = outcome_of(job.capability, doc)
        deadline = self._now() + self.follow_seconds
        while out.status == "running" and out.task_id and not self._stop.is_set():
            left = deadline - self._now()
            if left <= 0:
                break
            record = self.client.task(out.task_id, wait=min(self.poll_seconds, left))
            if not isinstance(record, dict):
                return JobOutcome("failed", job.capability, task_id=out.task_id,
                                  error="Pionir's task record is not an object")
            if record.get("status") == "running":
                continue
            inner = record.get("result")
            out = outcome_of(job.capability, inner if isinstance(inner, dict) else record)
            out.task_id = out.task_id or str(record.get("task_id") or "")
        return out

    def snapshot(self) -> dict:
        return {"queue": self.waiting(),
                "busy": ({"agent": self.busy_with.agent_id,
                          "capability": self.busy_with.job.capability}
                         if self.busy_with else None),
                "counts": dict(self.counts), "held_for_pause": self.held_for_pause,
                "last_error": self.last_error}
