"""A loopback HTTP server and dashboard that make Pionir visible and launchable.

Pionir has been a CLI: correct, inspectable, and invisible. This is the window
onto it - a local page where the routing can be watched deciding, the roster's
health and the card's VRAM seen at a glance, the audit ledger read as it grows,
and any specialist driven directly, privileged ones behind a confirm.

The endpoint logic lives in ``PionirApp`` as plain dict-returning methods so it
is testable without a socket; the HTTP handler is a thin shell over it. The
server binds loopback only - it is an admin surface for the person at the
machine, the same trust boundary as the specialists it front-ends.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
import webbrowser
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .approvals import ApprovalQueue
from .bootstrap import PionirRuntime
from .cli import _capabilities, _doctor, _jsonable
from .contracts import RiskLevel, Task, outcome_ok
from .errors import PionirError, RoutingAmbiguous
from .router import Candidate, IntentRouter, RoutingDecision
from .runtime import AuditEvent
from .scheduler import observed_free_vram_mb

_log = logging.getLogger(__name__)

MAX_REQUEST_BYTES = 1_000_000
_UI_PATH = Path(__file__).parent / "web" / "dashboard.html"

# Jobs: how long a POST waits for its work before answering 202 and letting the
# job run on. The voice gives up at 180s, so the default is under that; the max
# covers the longest inner specialist call (Daedalus, up to 600s).
DEFAULT_WAIT_SECONDS = 150.0
MAX_WAIT_SECONDS = 600.0
JOBS_KEEP = 500
_TASK_ID = re.compile(r"[0-9a-f]{32}")
JOB_STATUSES = ("running", "done", "error", "pending_approval", "unclear", "self")

# Loopback admin surface: a browser tab on any origin can still POST here
# (a `text/plain` "simple request" needs no CORS preflight), so a POST must look
# like it came from our own page or a local client, or it is refused.
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class Jobs:
    """Durable record of every intent/task/approval run, one JSON file each.

    Written BEFORE the request is answered and again from the worker thread
    when the work finishes, so a result is never lost to a client that gave up
    (the voice at 180s, the phone proxy at 6s): the file holds it, and
    ``GET /api/task/<id>`` hands it over later. Atomic writes (temp + replace);
    the directory is bounded to the newest ``JOBS_KEEP`` files.
    """

    def __init__(self, root: Path, *, keep: int = JOBS_KEEP) -> None:
        self.root = root
        self.keep = keep
        self._lock = threading.Lock()
        self._done: dict[str, threading.Event] = {}
        self.root.mkdir(parents=True, exist_ok=True)
        self._recover_interrupted()

    def _path(self, task_id: str) -> Path:
        return self.root / f"{task_id}.json"

    def _write(self, record: dict[str, Any]) -> None:
        fd, tmp = tempfile.mkstemp(dir=str(self.root), prefix=".job-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record, handle, ensure_ascii=False, default=str)
            os.replace(tmp, self._path(record["task_id"]))
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError as error:
                    _log.warning("job temp file %s not removed: %s", tmp, error)

    def _files(self) -> list[Path]:
        """Job files, newest first. A file that vanishes mid-listing sorts last."""
        def mtime(path: Path) -> int:
            try:
                return path.stat().st_mtime_ns
            except OSError:
                return 0
        return sorted(
            (p for p in self.root.glob("*.json") if _TASK_ID.fullmatch(p.stem)),
            key=mtime, reverse=True,
        )

    def _prune(self) -> None:
        files = self._files()
        for stale in files[self.keep:]:
            try:
                stale.unlink()
            except OSError as error:
                _log.warning("job file %s not pruned: %s", stale, error)

    def _recover_interrupted(self) -> None:
        """Mark jobs orphaned by a previous process as interrupted."""

        for path in self._files():
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(record, dict) or record.get("status") != "running":
                continue
            record["status"] = "error"
            record["finished_at"] = datetime.now(UTC).isoformat()
            detail = {
                "type": "Interrupted",
                "message": "Pionir restarted before this job finished",
            }
            record["result"] = {
                "status": "error",
                "error": detail,
                "task_id": record.get("task_id"),
            }
            record["error"] = detail
            try:
                self._write(record)
            except OSError as error:
                _log.warning("interrupted job %s could not be recovered: %s", path, error)

    def create(self, kind: str, request: dict[str, Any], *, task_id: str | None = None) -> dict[str, Any]:
        task_id = task_id or uuid.uuid4().hex
        if not _TASK_ID.fullmatch(task_id):
            raise ValueError("task_id must be a 32-character lowercase hex id")
        record = {
            "task_id": task_id,
            "kind": kind,
            "status": "running",
            "created_at": datetime.now(UTC).isoformat(),
            "finished_at": None,
            "request": request,
            "result": None,
            "error": None,
        }
        with self._lock:
            if self._path(task_id).exists():
                raise ValueError(f"job {task_id} already exists")
            self._done[task_id] = threading.Event()
            self._write(record)
            self._prune()
        return record

    def finish(self, task_id: str, status: str, *, result: dict[str, Any] | None = None,
               error: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if status not in JOB_STATUSES:
            raise ValueError(f"unknown job status {status!r}")
        with self._lock:
            record = self.get(task_id)
            if record is None:
                return None
            record["status"] = status
            record["finished_at"] = datetime.now(UTC).isoformat()
            record["result"] = result
            record["error"] = error
            self._write(record)
            event = self._done.get(task_id)
            if event is not None:
                event.set()
                self._done.pop(task_id, None)
        return record

    def get(self, task_id: str) -> dict[str, Any] | None:
        if not _TASK_ID.fullmatch(task_id or ""):
            return None
        try:
            document = json.loads(self._path(task_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return document if isinstance(document, dict) else None

    def wait(self, task_id: str, timeout: float) -> bool:
        """True once the job has finished (or was never running here)."""
        event = self._done.get(task_id)
        if event is None:
            record = self.get(task_id)
            return record is not None and record["status"] != "running"
        return event.wait(max(0.0, timeout))

    def recent(self, n: int = 20) -> list[dict[str, Any]]:
        """The newest ``n`` jobs without their ``result`` bodies (fetch one by id)."""
        out: list[dict[str, Any]] = []
        for path in self._files()[: max(0, n)]:
            record = self.get(path.stem)
            if record is not None:
                out.append({k: v for k, v in record.items() if k != "result"})
        return out


def _job_status(result: dict[str, Any]) -> str:
    """Map a handler's response dict onto the job's terminal status."""
    status = result.get("status")
    if status in ("pending_approval", "unclear", "self", "error"):
        return str(status)
    if status in ("done", "planned"):
        return "done"
    if "ok" in result:
        return "done" if result["ok"] else "error"
    return "done"


# Whether a specialist's own output reports success. An adapter can return
# normally and still carry a failing verdict; reporting that as ``ok: true``
# recorded a failed action as a success (both approved rows in queue.json were
# actually failures). This is the same function the executive uses to choose
# task.failed over task.completed in the audit ledger - one rule, so the
# response and the ledger can never disagree.
_outcome_ok = outcome_ok


def _clamp_wait(wait: Any) -> float:
    try:
        value = float(DEFAULT_WAIT_SECONDS if wait is None else wait)
    except (TypeError, ValueError):
        value = DEFAULT_WAIT_SECONDS
    return min(max(value, 0.0), MAX_WAIT_SECONDS)

# The only thing the voice runs on her own through /api/intent: asking Atani to
# think (no doer, no side effect). Everything else is a doer's job, which only
# Atani tasks - see PionirApp.intent.
_VOICE_REASONING = frozenset({"reasoning.atani_answer"})

# When she names a bot (or clearly means security work) but phrases it loosely,
# the classifier can come back unsure. That is exactly Atani's job to sort out, so
# such a request goes to the manager rather than dead-ending as "unclear".
_NAMES_A_DOER = re.compile(
    r"\b(nyx|voodoo|daedalus|melete|atani|recon|reconnaissance|pentest)\b", re.I
)


def _decision_json(decision: RoutingDecision) -> dict[str, Any]:
    return {
        "capability": decision.capability,
        "confidence": decision.confidence,
        "reason": decision.reason,
        "resolved": decision.resolved,
        "runner_up": decision.runner_up,
        "candidates": [_candidate_json(item) for item in decision.candidates],
    }


def _candidate_json(candidate: Candidate) -> dict[str, Any]:
    return {
        "capability": candidate.capability,
        "agent_id": candidate.agent_id,
        "description": candidate.description,
        "score": candidate.score,
        "matched": list(candidate.matched),
    }


class PionirApp:
    """Every dashboard endpoint, as plain data - no HTTP, so it is unit-testable."""

    def __init__(self, runtime: PionirRuntime) -> None:
        self.runtime = runtime
        self.router = IntentRouter(runtime.executive)
        self.approvals = ApprovalQueue(runtime.settings.state_root / "approvals" / "queue.json")
        self.jobs = Jobs(runtime.settings.state_root / "tasks")

    # ---- jobs: work that outlives the request ---------------------------
    def _submit(
        self,
        kind: str,
        request: dict[str, Any],
        work: Callable[[], dict[str, Any]],
        wait: float,
        *,
        known: dict[str, Any] | None = None,
        task_id: str | None = None,
        on_finish: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Run ``work`` on a worker thread, wait up to ``wait`` seconds.

        The job file is written before this returns and rewritten by the worker
        when the work ends, whichever the client did in between. Finished in
        time: the work's own response plus ``task_id``. Not yet: ``status:
        running`` plus ``task_id`` and whatever is already ``known`` (the routing
        decision), for the client to poll ``GET /api/task/<task_id>``.
        """

        record = self.jobs.create(kind, request, task_id=task_id)
        tid = record["task_id"]
        box: dict[str, Any] = {}

        def worker() -> None:
            result: dict[str, Any] | None = None
            detail: dict[str, Any] | None = None
            try:
                result = work()
                status = _job_status(result)
                response = {**result, "task_id": tid}
            except Exception as error:  # noqa: BLE001 - the worker must record, never vanish
                _log.exception("job %s (%s) failed", tid, kind)
                detail = {"type": type(error).__name__, "message": str(error)}
                status = "error"
                response = {"status": "error", "error": detail, "task_id": tid}
            box["response"] = response
            # on_finish (the approval record) runs BEFORE the job is marked done,
            # so anyone who waited on the job sees the approval already settled.
            if on_finish is not None:
                try:
                    on_finish(response)
                except Exception:  # noqa: BLE001
                    _log.exception("job %s (%s) on_finish failed", tid, kind)
            # Polling should return the same response as a client that stayed
            # connected, including the durable task id.
            self.jobs.finish(tid, status, result=response, error=detail)

        threading.Thread(target=worker, name=f"pionir-job-{tid[:8]}", daemon=True).start()
        if self.jobs.wait(tid, wait) and "response" in box:
            return box["response"]
        # A still-running job carries an explicit ``running: True`` alongside the
        # status and task id. A client that gave up before ``wait`` and only
        # checked for ``ok`` used to read its absence as failure ("Daedalus
        # couldn't: no reason given") while the job ran on; this flag is the
        # unambiguous signal it cannot misread. galatea/hands.py still keys on
        # status=="running" + task_id, so it is untouched.
        return {**(known or {}), "status": "running", "running": True, "task_id": tid}

    def job(self, task_id: str) -> dict[str, Any] | None:
        return self.jobs.get(task_id)

    def jobs_view(self, n: int = 20) -> dict[str, Any]:
        return {"tasks": self.jobs.recent(n)}

    def _record_routed(self, decision: RoutingDecision, *, note: str) -> None:
        """A classification that never reached the executive is still a routing
        decision, and the ledger's ask-rate is meaningless without it."""
        self.runtime.executive.audit_sink.record(
            AuditEvent(
                event_type="task.routed",
                task_id=uuid.uuid4(),
                agent_id="unrouted",
                occurred_at=datetime.now(UTC),
                detail=f"{decision.audit_detail()} {note}",
            )
        )

    # ---- read-only views -------------------------------------------------
    def roster(self) -> list[dict[str, Any]]:
        """The registered specialists, from the registry, without probing them.

        Deliberately does not reach out to each service - that is doctor's job
        and it is slow. The dashboard paints this instantly and fills health in
        behind it.
        """

        return _capabilities(self.runtime)

    def gpu(self) -> dict[str, Any]:
        budget = self.runtime.settings.resource_budget
        resident: list[dict[str, Any]] = []
        try:
            from .benchmark import read_loaded_models

            resident = [
                {"name": item.name, "fully_on_gpu": bool(item.fully_on_gpu)}
                for item in read_loaded_models()
            ]
        except Exception:  # noqa: BLE001 - a display must never take the server down
            resident = []
        total = used = None
        try:
            from .benchmark import read_gpu_memory

            memory = read_gpu_memory()
            total, used = memory.total_mb, memory.used_mb
        except Exception:  # noqa: BLE001
            total = used = None
        return {
            "observed_free_mb": observed_free_vram_mb(),
            "total_mb": total,
            "used_mb": used,
            "budget": {
                "total_mb": budget.total_vram_mb,
                "reserved_mb": budget.reserved_vram_mb,
                "usable_mb": budget.usable_vram_mb,
                "max_gpu_leases": budget.max_gpu_leases,
            },
            "resident": resident,
        }

    def state(self) -> dict[str, Any]:
        return {
            "roster": self.roster(),
            "gpu": self.gpu(),
            # The voice's own URL, so the dashboard can embed her glass as its
            # Voice view rather than opening her in a separate tab. None when no
            # voice is configured, and the dashboard hides the view.
            "voice_url": self.runtime.settings.galatea_url,
            # The spine's vital sign: Bryo's felt pressure. Read from the reader's
            # cache - never a subprocess per poll. None when Bryo isn't wired in.
            "bryo": self.body(),
            "generated_at": datetime.now(UTC).isoformat(),
        }

    def body(self) -> dict[str, Any] | None:
        reading = self.runtime.executive.body_reading()
        if reading is None:
            return None
        return {
            "alive": bool(getattr(reading, "alive", False)),
            "pressure": float(getattr(reading, "pressure", 0.0)),
            "defer_heavy_work": bool(getattr(reading, "defer_heavy_work", False)),
            "note": str(getattr(reading, "note", "")),
            "source": str(getattr(reading, "source", "")),
        }

    def doctor(self) -> dict[str, Any]:
        return _doctor(self.runtime)

    def audit(self, limit: int = 60) -> dict[str, Any]:
        sink = self.runtime.executive.audit_sink
        events = sink.recent(limit) if hasattr(sink, "recent") else []
        integrity = "verified"
        head = None
        try:
            count, head = sink.verify()  # type: ignore[attr-defined]
        except PionirError as error:
            integrity = f"broken: {error}"
            count = None
        return {
            "integrity": integrity,
            "events_total": count,
            "head_sha256": head,
            "events": events,
        }

    # ---- driving the brain ----------------------------------------------
    def classify(self, request: str) -> dict[str, Any]:
        return {"decision": _decision_json(self.router.classify(request))}

    def route(
        self,
        request: str,
        *,
        permissions: list[str] | None = None,
        execute: bool = True,
    ) -> dict[str, Any]:
        """Classify a request, and unless told otherwise run it.

        With ``execute`` false this is the 'explain' path: it shows where the
        request would go and how sure the router is, and touches no model. With
        it true, an ambiguous request comes back as a question rather than a
        guess, and any specialist or gate error is returned as data - the
        dashboard shows the failure, it does not crash on it.
        """

        decision = self.router.classify(request)
        if not execute:
            self._record_routed(decision, note="executed=false")
            return {"executed": False, "decision": _decision_json(decision)}
        if not decision.resolved:
            return {
                "executed": False,
                "decision": _decision_json(decision),
                "question": decision.question(),
            }
        try:
            _, result = self.router.route(
                request, granted_permissions=permissions or ()
            )
        except RoutingAmbiguous as question:
            return {
                "executed": False,
                "decision": _decision_json(decision),
                "question": str(question),
            }
        except Exception as error:  # noqa: BLE001 - surfaced to the page as data
            return {
                "executed": False,
                "decision": _decision_json(decision),
                "error": {"type": type(error).__name__, "message": str(error)},
            }
        return {
            "executed": True,
            "decision": _decision_json(decision),
            "result": _jsonable(result.output),
            "agent_id": result.agent_id,
            "evidence": list(result.evidence),
        }

    def intent(self, request: str, *, wait: float = DEFAULT_WAIT_SECONDS) -> dict[str, Any]:
        """The voice's one seam for getting something done - and it reaches only
        Atani, never a doer. Runs as a job (see ``_submit``): answered in full if
        it finishes within ``wait`` seconds, else ``status: running`` + task_id.

        The design (Ian, 2026-09-11): the voice does not control the organs
        directly. She views and reads, and she may ask Atani for a specific bot,
        but only Atani tasks the doers. So this hands her intent to Atani and
        nothing else. Atani reasoning answers her outright. A request that needs
        a doer (Daedalus, Melete, Nyx, Voodoo) is Atani's to dispatch, and it now
        does: `atani manage` picks the bot, POSTs the task to Pionir's /api/task,
        polls for the outcome, and parks on pending_approval - so the voice gets
        back what actually happened (completed / needs_approval / failed), not a
        promise. Anti-confabulation still holds: what she reports is what actually
        came back.
        """

        decision = self.router.classify(request)
        base = {"decision": _decision_json(decision)}
        body = {"request": request}

        def settle(response: dict[str, Any]) -> dict[str, Any]:
            # No work to run: the answer is the decision itself. Still a job, so
            # the record of what the voice asked and what she was told persists.
            record = self.jobs.create("intent", body)
            self.jobs.finish(record["task_id"], _job_status(response), result=response)
            return {**response, "task_id": record["task_id"]}

        def run(capability: str, permissions: frozenset[str]) -> dict[str, Any]:
            return self._submit(
                "intent", body,
                lambda: self._run_intent(capability, {"content": request}, permissions, decision),
                wait, known=base,
            )

        if not decision.resolved:
            if _NAMES_A_DOER.search(request):
                # she named a bot; the classifier just wasn't sure. Hand it to
                # Atani, who decides which organ and tasks it, rather than asking.
                return run("manager.atani_manage", frozenset({"atani.manage"}))
            self._record_routed(decision, note="handled=voice_unclear")
            return settle({**base, "status": "unclear", "question": decision.question()})
        cap = decision.capability
        if cap == "conversation.galatea_reply":
            self._record_routed(decision, note="handled=voice_self")
            return settle({**base, "status": "self",
                           "note": "that routes back to the voice - answer it yourself"})
        if cap in _VOICE_REASONING:
            # Asking Atani to think: Atani answers her directly, no doer involved.
            return run(cap, frozenset({"atani.chat"}))
        if self._capability_risk(cap) is RiskLevel.READ_ONLY:
            # She may view, read and look for herself (Bryo's vitals, a status
            # snapshot); that is not tasking a doer, so it runs directly.
            return run(cap, frozenset())
        # Everything else names a doer's job. The voice does not task doers - she
        # asks Atani, and Atani tasks the right bot through Pionir. Hand the whole
        # request to Atani the manager; it decides, tasks, waits, and returns what
        # actually happened. manager.atani_manage holds no GPU lease of its own,
        # so the doer's task can take the single lease.
        return run("manager.atani_manage", frozenset({"atani.manage"}))

    def _capability_risk(self, name: str) -> RiskLevel | None:
        for manifest in self.runtime.executive.registry.manifests():
            for capability in manifest.capabilities:
                if capability.name == name:
                    return capability.risk
        return None

    def _run_intent(self, capability, payload, permissions, decision, *, planned=False):
        try:
            # routing_detail: the ledger's task.routed event carries the router's
            # confidence and runner-up, as it does for `pionir route`.
            result = self.runtime.executive.execute(
                Task(capability, payload, permissions),
                routing_detail=decision.audit_detail(),
            )
        except Exception as error:  # noqa: BLE001 - returned to the voice as data
            return {
                "decision": _decision_json(decision),
                "status": "error",
                "error": {"type": type(error).__name__, "message": str(error)},
            }
        return {
            "decision": _decision_json(decision),
            "status": "planned" if planned else "done",
            "planned": planned,
            "agent_id": result.agent_id,
            "result": _jsonable(result.output),
            "evidence": list(result.evidence),
        }

    def _cap_and_agent(self, name: str):
        for manifest in self.runtime.executive.registry.manifests():
            for capability in manifest.capabilities:
                if capability.name == name:
                    return manifest.agent_id, capability
        return None, None

    def _validation_error(
        self, capability: str, payload: dict[str, Any], granted: list[str]
    ) -> dict[str, Any] | None:
        """The adapter's own argument check, run before queuing an approval.

        Returns an error detail if the request is malformed (and so must not be
        parked for Ian), or None if it is well-formed or the adapter has no
        pre-execution validator. Only structural validation - permissions and
        execution still happen later, unchanged.
        """

        agent_id, _cap = self._cap_and_agent(capability)
        if agent_id is None:
            return None
        adapter = self.runtime.adapters.get(agent_id)
        validate = getattr(adapter, "validate", None)
        if validate is None:
            return None
        try:
            validate(Task(capability, payload, frozenset(granted)))
        except PionirError as error:
            return {"type": type(error).__name__, "message": str(error)}
        return None

    def _needs_approval(self, capability: str, granted: list[str]) -> bool:
        """A privileged action arriving without the permission it needs is held
        for Ian, not refused. Anything read-only or already-permitted just runs."""
        _agent, cap = self._cap_and_agent(capability)
        if cap is None:
            return False   # unknown capability: let execute() report it as it always has
        return (cap.risk is RiskLevel.PRIVILEGED
                and not set(cap.required_permissions).issubset(set(granted)))

    def _summarize(self, capability: str, payload: dict[str, Any]) -> str:
        agent, _cap = self._cap_and_agent(capability)
        who = agent or capability.split(".")[0]
        action = payload.get("action")
        if isinstance(action, str) and action.strip():
            # a tool action: show the whole command so Ian decides on the real thing
            args = payload.get("args")
            gist = " ".join([action, *[str(a) for a in args]]) if isinstance(args, list) else action
        else:
            gist = ""
            for key in ("content", "task", "goal", "command", "target", "request", "intent"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    gist = value.strip()
                    break
                if isinstance(value, (dict, list)):
                    gist = json.dumps(value)[:160]
                    break
        return f"{who} · {capability}" + (f" — {gist[:200]}" if gist else "")

    def run_task(
        self,
        capability: str,
        payload: dict[str, Any],
        *,
        permissions: list[str] | None = None,
        deferrable: bool = False,
        wait: float = DEFAULT_WAIT_SECONDS,
    ) -> dict[str, Any]:
        """Invoke one named capability directly. A privileged action without its
        permission is parked in the approval queue and does NOT run - it waits for
        Ian's yes; everything else runs as a job (``_submit``): answered in full
        within ``wait`` seconds, else ``status: running`` + task_id to poll.

        ``deferrable`` lets a background caller say its work can wait: if Bryo is
        alive and stressed, GPU work is held back and returned as a BodyDeferred
        error rather than run. Default False - nothing interactive is ever held."""

        granted = list(permissions or ())
        body = {"capability": capability, "payload": payload, "permissions": granted,
                "deferrable": deferrable}
        # Validate the action's arguments BEFORE anything is parked for approval.
        # Argument validation used to live only in the adapter's execute(), which
        # runs after Ian approves - so he could be asked to approve a request that
        # can never run (a bad host, a disallowed flag). Check it now instead.
        invalid = self._validation_error(capability, payload, granted)
        if invalid is not None:
            response = {"ok": False, "status": "error", "error": invalid}
            record = self.jobs.create("task", body)
            self.jobs.finish(record["task_id"], "error", result=response)
            return {**response, "task_id": record["task_id"]}
        if self._needs_approval(capability, granted):
            _agent, cap = self._cap_and_agent(capability)
            summary = self._summarize(capability, payload)
            approval_id = self.approvals.enqueue(
                capability, payload, sorted(cap.required_permissions), summary=summary
            )
            response = {
                "ok": False,
                "status": "pending_approval",
                "approval_id": approval_id,
                "summary": summary,
                "note": "held for Ian's approval - it has not run",
            }
            record = self.jobs.create("task", body)
            self.jobs.finish(record["task_id"], "pending_approval", result=response)
            return {**response, "task_id": record["task_id"]}
        return self._submit(
            "task", body,
            lambda: self._execute_task(capability, payload, granted, deferrable=deferrable),
            wait,
        )

    def _execute_task(
        self, capability: str, payload: dict[str, Any], granted: list[str],
        *, deferrable: bool = False,
    ) -> dict[str, Any]:
        """The synchronous core: run it now, on this thread, and report as data."""
        self._recall_lessons(capability, payload)
        try:
            result = self.runtime.executive.execute(
                Task(capability, payload, frozenset(granted)), deferrable=deferrable
            )
        except Exception as error:  # noqa: BLE001 - returned as data
            response = {"ok": False, "error": {"type": type(error).__name__, "message": str(error)}}
            self._learn_from_failure(capability, payload, response)
            return response
        # The adapter can return normally and still carry a failing verdict; the
        # outer ok reflects that, so a refused solve or a non-zero action is not
        # recorded as a success (item 3). approve()'s finish reads this ok to mark
        # the row approved vs approved_failed.
        ok = _outcome_ok(result.output)
        response = {
            "ok": ok,
            "agent_id": result.agent_id,
            "result": _jsonable(result.output),
            "evidence": list(result.evidence),
        }
        if not ok:
            self._learn_from_failure(capability, payload, response)
        return response

    def _recall_lessons(self, capability: str, payload: dict[str, Any]) -> None:
        """Consult the shared lessons namespace before tasking a doer, and note
        what was recalled in the ledger, so a mistake learned once is in front of
        the next similar action (the recall-before-act hook, previously only wired
        into the CLI)."""

        cortex = getattr(self.runtime, "cortex", None)
        if cortex is None:
            return
        context = self._summarize(capability, payload)
        try:
            lessons = cortex.lessons_for(context)
        except Exception:  # noqa: BLE001 - memory must never fail a task
            return
        if not lessons:
            return
        try:
            self.runtime.executive.audit_sink.record(
                AuditEvent(
                    event_type="task.recalled_lessons",
                    task_id=uuid.uuid4(),
                    agent_id=capability.split(".")[0],
                    occurred_at=datetime.now(UTC),
                    detail=f"capability={capability} lessons={len(lessons)}",
                )
            )
        except Exception:  # noqa: BLE001
            _log.warning("recording recalled-lessons event failed", exc_info=True)

    def _learn_from_failure(
        self, capability: str, payload: dict[str, Any], response: dict[str, Any]
    ) -> None:
        """Record a failed task or approval as a lesson in the shared namespace,
        so the circuit-breaker path is not the only thing that ever writes one and
        a repeated mistake surfaces on the next similar request."""

        cortex = getattr(self.runtime, "cortex", None)
        if cortex is None:
            return
        error = response.get("error")
        why = ""
        if isinstance(error, Mapping):
            why = str(error.get("message") or error.get("type") or "")
        elif isinstance(response.get("result"), Mapping):
            why = str(response["result"].get("error") or "")
        summary = self._summarize(capability, payload)
        text = f"{summary} failed" + (f": {why[:300]}" if why else "")
        try:
            cortex.record_lesson(text, slug=f"failure:{capability}")
        except Exception:  # noqa: BLE001 - a lesson write must never fail a task
            _log.warning("recording failure lesson failed", exc_info=True)

    # ---- approvals: Ian's yes/no on a parked privileged action ----------
    def approvals_view(self) -> dict[str, Any]:
        return {"pending": self.approvals.pending(), "recent": self.approvals.recent(20)}

    def approve(self, approval_id: str, *, wait: float = 0.0) -> dict[str, Any]:
        """Claim first, then run as a job. The claim is an atomic pending->running
        move in the queue, so two taps (or a proxy retry after its own timeout)
        can never run the action twice: the second gets AlreadyResolved. The
        response comes back at once with the job's task_id; when the job ends the
        approval record is marked approved (or approved_failed) with the result."""

        task_id = uuid.uuid4().hex
        record = self.approvals.claim(approval_id, task_id=task_id)
        if record is None:
            current = self.approvals.get(approval_id)
            if current is None:
                return {"ok": False, "error": {"type": "NotFound", "message": "no such approval"}}
            return {"ok": False, "error": {"type": "AlreadyResolved",
                                           "message": f"already {current['status']}"}}

        def finish(response: dict[str, Any]) -> None:
            status = "approved" if response.get("ok") is True else "approved_failed"
            if not self.approvals.resolve(approval_id, status, response, from_status="running"):
                _log.warning("approval %s was not running when its job finished", approval_id)

        # run it with exactly the permission it needed - never wider
        response = self._submit(
            "approval",
            {"approval_id": approval_id, "capability": record["capability"],
             "payload": record["payload"], "permissions": record["permissions"]},
            lambda: self._execute_task(record["capability"], record["payload"],
                                       list(record["permissions"])),
            wait, task_id=task_id, on_finish=finish,
        )
        if response.get("status") == "running":
            return {"ok": True, "status": "running", "approval_id": approval_id,
                    "task_id": task_id}
        # finished inside the wait (tests, or a caller that asked to wait)
        return {"ok": True, "status": self.approvals.get(approval_id)["status"],
                "approval_id": approval_id, "task_id": task_id, "result": response}

    def deny(self, approval_id: str) -> dict[str, Any]:
        if self.approvals.resolve(approval_id, "denied"):
            return {"ok": True, "status": "denied", "approval_id": approval_id}
        return {"ok": False, "error": {"type": "AlreadyResolved", "message": "not pending"}}


def _ui_bytes() -> bytes:
    try:
        return _UI_PATH.read_bytes()
    except OSError:
        return b"<h1>Pionir</h1><p>dashboard.html is missing.</p>"


def _hostname(header: str | None) -> str | None:
    """The host part of a Host/Origin-style header, without port or brackets."""
    if not header:
        return None
    try:
        return urlparse("//" + header.strip()).hostname
    except ValueError:
        return None


def _post_allowed(headers: Any, bind_host: str) -> str | None:
    """None if the POST may proceed, else why it may not.

    Three checks, all cheap: the body must be declared JSON (a cross-site form
    or `text/plain` fetch cannot say that without a preflight, which loopback
    never grants); the Host must be us; and if the browser sent an Origin it
    must be our own page. A local client with no Origin (curl, the voice,
    Atani) passes.
    """

    content_type = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if content_type != "application/json":
        return "content-type must be application/json"
    host_header = headers.get("Host")
    host = _hostname(host_header)
    if host is None or host not in (_LOCAL_HOSTS | {bind_host}):
        return "host is not local"
    origin = headers.get("Origin")
    if origin is not None and origin.strip().lower() != f"http://{host_header.strip()}".lower():
        return "origin does not match host"
    return None


def _make_handler(app: PionirApp, *, bind_host: str = "127.0.0.1"):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Pionir/0.1"
        timeout = 30  # a stalled client releases its thread; a job outlives it anyway

        def log_message(self, *_args: Any) -> None:  # keep the console quiet
            return

        def _send(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_job(self, payload: dict[str, Any]) -> None:
            """A job response: 202 while it is still running, 200 once answered."""
            self._send(payload, 202 if payload.get("status") == "running" else 200)

        def _body(self) -> dict[str, Any] | None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if length <= 0:
                return {}
            if length > MAX_REQUEST_BYTES:
                return None
            try:
                document = json.loads(self.rfile.read(length).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return None
            return document if isinstance(document, dict) else None

        def do_GET(self) -> None:
            route = urlparse(self.path)
            if route.path in ("/", "/index.html"):
                body = _ui_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            try:
                if route.path == "/api/state":
                    self._send(app.state())
                elif route.path == "/api/doctor":
                    self._send(app.doctor())
                elif route.path == "/api/capabilities":
                    self._send({"capabilities": app.roster()})
                elif route.path == "/api/audit":
                    n = int(parse_qs(route.query).get("n", ["60"])[0] or 60)
                    self._send(app.audit(min(max(n, 1), 500)))
                elif route.path == "/api/approvals":
                    self._send(app.approvals_view())
                elif route.path == "/api/tasks":
                    n = int(parse_qs(route.query).get("n", ["20"])[0] or 20)
                    self._send(app.jobs_view(min(max(n, 1), JOBS_KEEP)))
                elif route.path.startswith("/api/task/"):
                    task_id = route.path[len("/api/task/"):].strip("/")
                    query = parse_qs(route.query)
                    if "wait" in query:
                        # optional long-poll: hold up to `wait` seconds for it to end
                        app.jobs.wait(task_id, _clamp_wait(query["wait"][0]))
                    record = app.job(task_id)
                    if record is None:
                        self._send({"error": "not found", "task_id": task_id}, 404)
                    else:
                        self._send(record)
                else:
                    self._send({"error": "not found"}, 404)
            except Exception as error:  # noqa: BLE001
                self._send({"error": type(error).__name__, "message": str(error)}, 500)

        def do_POST(self) -> None:
            route = urlparse(self.path)
            refused = _post_allowed(self.headers, bind_host)
            if refused is not None:
                self._send({"error": "forbidden", "reason": refused}, 403)
                return
            body = self._body()
            if body is None:
                self._send({"error": "bad json"}, 400)
                return
            try:
                if route.path == "/api/route":
                    request = str(body.get("request", "")).strip()
                    if not request:
                        self._send({"error": "request is required"}, 400)
                        return
                    self._send(
                        app.route(
                            request,
                            permissions=[str(p) for p in body.get("permissions", [])],
                            execute=bool(body.get("execute", True)),
                        )
                    )
                elif route.path == "/api/intent":
                    request = str(body.get("request", "") or body.get("intent", "")).strip()
                    if not request:
                        self._send({"error": "intent is required"}, 400)
                        return
                    self._send_job(app.intent(request, wait=_clamp_wait(body.get("wait"))))
                elif route.path == "/api/task":
                    capability = str(body.get("capability", "")).strip()
                    if not capability:
                        self._send({"error": "capability is required"}, 400)
                        return
                    payload = body.get("payload")
                    self._send_job(
                        app.run_task(
                            capability,
                            payload if isinstance(payload, dict) else {},
                            permissions=[str(p) for p in body.get("permissions", [])],
                            deferrable=body.get("deferrable") is True,
                            wait=_clamp_wait(body.get("wait")),
                        )
                    )
                elif route.path == "/api/approvals/approve":
                    aid = str(body.get("id", "")).strip()
                    if not aid:
                        self._send({"error": "id required"}, 400)
                        return
                    self._send_job(app.approve(aid))
                elif route.path == "/api/approvals/deny":
                    aid = str(body.get("id", "")).strip()
                    if not aid:
                        self._send({"error": "id required"}, 400)
                        return
                    self._send(app.deny(aid))
                else:
                    self._send({"error": "not found"}, 404)
            except Exception as error:  # noqa: BLE001
                self._send({"error": type(error).__name__, "message": str(error)}, 500)

    return Handler


class _RateLimitedErrors:
    """Log recurring errors at most once per interval, counting the suppressed ones.

    The pulse thread runs every second or two; a persistent fault (a bad poll,
    the state dir vanishing) would flood the log if every failure logged. It used
    to swallow them all with ``except Exception: pass`` - which hid the fault
    entirely. This logs the first, then one line per interval with how many were
    suppressed, so a real problem is visible without drowning the log."""

    def __init__(
        self, logger: logging.Logger, *, interval: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._log = logger
        self._interval = interval
        self._clock = clock
        self._last: float | None = None
        self._suppressed = 0

    def note(self, error: BaseException, message: str) -> None:
        now = self._clock()
        if self._last is None or now - self._last >= self._interval:
            extra = f" ({self._suppressed} similar suppressed)" if self._suppressed else ""
            self._log.warning("%s: %s: %s%s", message, type(error).__name__, error, extra)
            self._last = now
            self._suppressed = 0
        else:
            self._suppressed += 1


def _start_pulse(app: PionirApp) -> tuple[threading.Thread | None, threading.Event]:
    """Bryo's pulse: while the server is up, write Pionir's live state where his
    sensors read it. A daemon thread, so it lives exactly as long as the server
    and needs no separate process (estate rule 3). Only runs when the cache's
    directory already exists - it never creates a stray terrarium tree - and can
    be turned off with PIONIR_BRYO_FEED=off."""
    from . import bryofeed

    stop = threading.Event()
    if os.environ.get("PIONIR_BRYO_FEED", "").strip().lower() in {"off", "none", "false", "0"}:
        return None, stop
    path = Path(os.environ.get("PIONIR_BRYO_FEED_PATH", bryofeed.DEFAULT_OUT))
    if not path.parent.exists():
        # No terrarium here: nothing to feed, and we don't invent his state dir.
        return None, stop
    try:
        interval = max(1.0, float(os.environ.get("PIONIR_BRYO_FEED_INTERVAL", bryofeed.DEFAULT_INTERVAL)))
    except ValueError:
        interval = bryofeed.DEFAULT_INTERVAL

    errors = _RateLimitedErrors(_log)

    def pump() -> None:
        prev: int | None = None
        while not stop.is_set():
            try:
                audit = app.audit(1)
                snap, prev = bryofeed.shape(app.state(), audit.get("events_total"), prev)
                bryofeed.write(path, snap)
            except Exception as error:  # noqa: BLE001 - a bad poll must never take the server down
                errors.note(error, "Bryo pulse poll failed")
            stop.wait(interval)

    thread = threading.Thread(target=pump, name="pionir-bryo-pulse", daemon=True)
    thread.start()
    return thread, stop


def serve(
    runtime: PionirRuntime,
    *,
    port: int = 8780,
    open_browser: bool = True,
) -> int:
    """Run the dashboard in the foreground until interrupted.

    Foreground and loopback by design: it is awake while this window is open,
    nothing starts it on its own, and closing the window stops it - the same
    contract as every other visible process in the estate.
    """

    app = PionirApp(runtime)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(app, bind_host="127.0.0.1"))
    url = f"http://127.0.0.1:{port}/"
    pulse_thread, pulse_stop = _start_pulse(app)
    print("  PIONIR")
    print(f"  the brain is visible at {url}")
    if pulse_thread is not None:
        print("  Bryo's pulse is beating (Pionir's state written where he can feel it).")
    print("  awake while this window is open; Ctrl+C stops it.")
    if open_browser:
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping.")
    finally:
        pulse_stop.set()
        httpd.shutdown()
        httpd.server_close()
    return 0
