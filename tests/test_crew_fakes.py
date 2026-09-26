"""Fakes for the crew-divisions tests. No tests live here.

Nothing built by these helpers reaches a real network, model, Pionir or Claude: HTTP is
``FakeHttp``, the model is ``FakeOllama``, Pionir is ``FakePionir``, Claude is
``FakeClaude``, the GPU lock probe always says free, and state lives in a temp dir.
"""
from __future__ import annotations

import json
import threading
import time
from typing import ClassVar

from crew_support import FakeOllama, settings, temp_dir

from pionir.crew.gpu import CardWatch
from pionir.crew.net import HttpResponse, HttpUnreachable
from pionir.crew.registry import build_registry
from pionir.crew.result import Err, Ok
from pionir.crew.runtime import Crew
from pionir.crew.worker import ErrorKind, WorkerError, make_output, never_raises
from pionir.crew.workers import IMPLS

__all__ = ["FakeClaude", "FakeHttp", "FakeOllama", "FakePionir", "ScriptedWorker",
           "catalogue", "make_crew", "settings", "temp_dir"]


# Pionir's real response shapes (server.py run_task / _execute_task)
def done(result, task_id="t-done") -> dict:
    return {"ok": True, "agent_id": "worker", "result": result, "evidence": [],
            "task_id": task_id}


PENDING = {"ok": False, "status": "pending_approval", "approval_id": "ap-1",
           "summary": "outreach . outreach.send", "task_id": "t-pending",
           "note": "held for Ian's approval - it has not run"}
FAILED = {"ok": False, "agent_id": "worker", "task_id": "t-failed",
          "error": {"type": "PionirError", "message": "the mail relay refused the login"}}


class FakePionir:
    """Answers ``run_task`` from a script keyed by capability."""

    def __init__(self, answers: dict | None = None) -> None:
        self.answers = dict(answers or {})
        self.calls: list = []

    def run_task(self, capability, payload, *, permissions=(), wait=30.0):
        self.calls.append((capability, dict(payload), tuple(permissions)))
        ans = self.answers[capability]
        if isinstance(ans, Exception):
            raise ans
        return dict(ans)

    def task(self, task_id, *, wait=0.0):
        raise AssertionError("no test job keeps running")


class FakeHttp:
    """``routes`` maps a URL to (status, body-object-or-bytes) or an exception to raise.
    Every call is recorded with its headers and the monotonic time it started."""

    def __init__(self, routes: dict | None = None, delay: float = 0.0) -> None:
        self.routes = dict(routes or {})
        self.delay = delay
        self.calls: list = []
        self._lock = threading.Lock()

    def get(self, url, *, headers=None, timeout=20.0):
        with self._lock:
            self.calls.append((url, dict(headers or {}), time.monotonic()))
        if self.delay:
            time.sleep(self.delay)
        ans = self.routes.get(url)
        if ans is None:
            raise HttpUnreachable(f"no route to {url}")
        if isinstance(ans, Exception):
            raise ans
        status, body = ans
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        return HttpResponse(status, body, 42.0)


class FakeClaude:
    """A Claude runner that never reaches Claude."""

    def __init__(self, answer: str = "Hold steady.", error: Exception | None = None) -> None:
        self.answer = answer
        self.error = error
        self.prompts: list = []

    def __call__(self, prompt: str, timeout: float) -> str:
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        return self.answer


class ScriptedWorker:
    """A worker whose behaviour is a catalogue param: ``mode`` is one of
    ok / silent / err / partial / raise (decorated) / raw_raise (undecorated) / slow.
    Records the monotonic time each run started."""

    live = True
    starts: ClassVar[list] = []     # (worker_id, monotonic) across all instances
    _lock = threading.Lock()

    def __init__(self, spec, *, mode: str = "ok", value: int = 1200, sleep: float = 0.0,
                 unit: str = "usd_cents", measures: str = "revenue") -> None:
        self.worker_id, self.division, self.kind = spec.worker_id, spec.division, spec.kind
        self.cadence_seconds, self.provider, self.stage = (spec.cadence_seconds, spec.provider,
                                                           spec.stage)
        self.entities = spec.entities
        self.mode, self.value, self.sleep = mode, value, sleep
        self.unit, self.measures = unit, measures
        self.runs = 0

    def readiness(self, secrets_dir):
        return None

    def run(self, ctx):
        if self.mode == "raw_raise":
            raise RuntimeError("a KeyError three frames deep")
        return self._run(ctx)

    @never_raises()
    def _run(self, ctx):
        with ScriptedWorker._lock:
            ScriptedWorker.starts.append((self.worker_id, time.monotonic()))
        self.runs += 1
        if self.sleep:
            time.sleep(self.sleep)
        if self.mode == "raise":
            raise KeyError("price")
        if self.mode == "err":
            return Err(WorkerError(self.worker_id, ErrorKind.HTTP_ERROR, "HTTP 500"))
        if self.mode == "silent":
            return Ok(())
        if self.mode == "partial":          # one source failed, another answered
            return Err(WorkerError(self.worker_id, ErrorKind.HTTP_ERROR, "HTTP 500", partial=(
                make_output(self, valid_at=ctx.now, observed_at=ctx.now,
                            payload={"from": "the other source"}, entities=self.entities,
                            provenance={"source": "real"}),)))
        return Ok((make_output(self, valid_at=ctx.now + self.runs, observed_at=ctx.now,
                               payload={"run": self.runs},
                               figures=[{"value": self.value, "unit": self.unit,
                                         "measures": self.measures}],
                               entities=self.entities,
                               provenance={"source": "real"}),))


TEST_IMPLS = {**IMPLS, "scripted": ScriptedWorker}


def catalogue(divisions: dict | None = None, providers: dict | None = None,
              known=("Moss",)) -> dict:
    """``divisions``: {division_id: [worker entry, ...]}; a worker entry is a dict whose
    defaults are a scripted ``ok`` worker on provider ``p``."""
    divisions = divisions if divisions is not None else {"alpha": [{"name": "a1"}]}
    out = []
    for did, workers in divisions.items():
        ws = []
        for w in workers:
            entry = {"impl": "scripted", "kind": "revenue", "cadence_seconds": 60,
                     "provider": "p", **w}
            ws.append(entry)
        out.append({"id": did, "title": did.capitalize(), "leader_cadence_seconds": 600,
                    "workers": ws})
    return {"providers": providers or {"p": 0.0, "q": 0.0, "none": 0.0},
            "known_names": list(known), "divisions": out}


def make_crew(root, *, cat: dict | None = None, http=None, post=None, claude=None,
              registry=None, now=time.time, research=None, **cfg):
    """A whole, unstarted Crew on fakes. Caller must ``crew.stop()``."""
    cfg.setdefault("secrets_dir", __import__("pathlib").Path(root) / "secrets")
    cfg.setdefault("deliveries_dir", __import__("pathlib").Path(root) / "deliveries")
    cfg.setdefault("products_dir", __import__("pathlib").Path(root) / "products")
    s = settings(root, **cfg)
    reg = registry or build_registry(cat or catalogue(), TEST_IMPLS)
    return Crew(s, reg, http=http or FakeHttp(), post=post or FakeOllama(),
                client=FakePionir(),
                card=CardWatch(s.gpu_lock_path, probe=lambda: None, poll_seconds=0.01),
                claude_runner=claude if claude is not None else FakeClaude(),
                # never the real claude_research_runner from a test
                research_runner=research if research is not None else FakeClaude(), now=now)
