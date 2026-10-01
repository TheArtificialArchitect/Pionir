"""The API builder's Daedalus jobs: submit one, follow it, settle it, never lose it.

``products.api_builder`` hands the WRITING of a product to Daedalus (local model, contained
sandbox user, free) exactly the way the Builds division does: one ``coding.daedalus_build``
job at a time, in the product's own sandbox repo, inside the overnight window, saved BEFORE
it is sent so a crash can never start a second one. This mixin is that lifecycle, mirrored
from ``builds/worker.py`` (which it does not touch). The host class (apibuild/worker.py) owns
the record, the checks, Claude's review and the owner's gate; it must provide ``window``,
``budget``, ``max_attempts``, ``_setup``, ``_event`` and ``_shelve``.

A job that was not started (Daedalus down, the circuit open, the GPU busy) is WAITING, never a
failed attempt. Only a job that ran and failed its own gate, ran out of time, or was lost
counts, and a product gets ``max_attempts`` of those before it is shelved.
"""
from __future__ import annotations

import re
import secrets
from pathlib import Path

from pionir import build_sandbox

from ..blog import _clip
from ..builds import sandbox
from ..builds.window import not_after
from ..builds.worker import BUILD, CANCEL, COUNTED, LOST_AFTER, PERMISSION, RETRY_AFTER, _hm
from ..delivery import _PASSING, PASSING_TYPES
from ..hands import Job
from ..log import log
from ..orders import _NOT_SET_UP
from .prompts import intent as intent_text

# vitest keeps to two workers: the sandbox's job object also holds the node child processes
VITEST_ARGS = "run --watch=false --coverage.enabled=false --maxWorkers=2"


def _q(text) -> str:
    """A PowerShell single-quoted string."""
    return "'" + str(text).replace("'", "''") + "'"


def verify_command(setup) -> str:
    """Daedalus's gate for a TypeScript product, a PowerShell command. Daedalus's own PATH has
    no node, so node and the tools' .js files are named by absolute path, from the setup
    record; tsc must exit 0, then vitest must."""
    tools = Path(setup.node_tools)
    tsc = tools.joinpath(*build_sandbox.TSC_JS)
    vitest = tools.joinpath(*build_sandbox.VITEST_MJS)
    return (f"& {_q(setup.node)} {_q(tsc)} --noEmit --pretty false; "
            f"if ($LASTEXITCODE -ne 0) {{ exit 1 }}; "
            f"& {_q(setup.node)} {_q(vitest)} {VITEST_ARGS}; exit $LASTEXITCODE")


class DaedalusJobs:
    """The job lifecycle, for ``ApiBuilder``. Products are keyed by their backlog ``id``."""

    def _git(self) -> dict:
        return {} if getattr(self, "git_run", None) is None else {"run": self.git_run}

    # ---- following the one job in flight ---------------------------------------------------
    def _follow(self, ctx, rec: dict, events: list) -> None:
        act = rec.get("active")
        if not act:
            return
        p = rec["products"].get(act.get("pid"))
        if p is None:
            rec["active"] = None
            return
        if not act.get("task_id"):
            self._orphan(ctx, rec, p, act, events)
            return
        if ctx.task is None:
            return
        out = ctx.task(BUILD, act["task_id"])
        if out.status in ("running", "unreachable"):
            if ctx.now > float(act["not_after"]) + LOST_AFTER:
                self._lost(ctx, rec, p, events, f"no outcome {LOST_AFTER / 60:.0f} minutes after "
                           f"its deadline (Pionir said {out.status})")
            return
        if out.status == "failed" and not out.error_type and re.search(
                r"(?i)\b404\b|not found|unknown task", out.error or ""):
            if ctx.now > float(act["not_after"]) + LOST_AFTER:
                self._lost(ctx, rec, p, events, "Pionir no longer knows the task")
            return
        self._job_settle(ctx, rec, p, out, events)

    def _job_settle(self, ctx, rec: dict, p: dict, out, events: list) -> None:
        """What one job's outcome means for its product."""
        rec["active"] = None
        a = p["attempts"][-1]
        a["finished_at"] = ctx.now
        a["pionir_status"] = out.status
        output = out.result if isinstance(out.result, dict) else {}
        if out.status == "done" and output.get("not_configured"):
            self._not_started(ctx, p, a, str(output.get("refused") or build_sandbox.SETUP_HINT),
                              events, retry=True)
        elif out.status == "done" and output.get("refused") and output.get("started") is False:
            self._not_started(ctx, p, a, "the window closed before the GPU was free; nothing "
                              "was started", events)
        elif out.status == "done" and output.get("ok") is True \
                and output.get("passed") is not False:
            self._landed(ctx, rec, p, a, output, events)
        elif out.status == "done":
            why = (output.get("gate_reason") or output.get("error")
                   or (f"its gate failed at {output.get('stage_failed')}"
                       if output.get("stage_failed") else "Daedalus reported it did not work"))
            a.update(outcome="gate_failed", stage_failed=output.get("stage_failed"),
                     why=_clip(why, 300), job_id=output.get("job_id"))
            self._failed_attempt(ctx, rec, p, f"Daedalus's own gate did not pass: "
                                 f"{_clip(why, 300)}", events)
        elif out.status == "failed" and out.error_type == "AdapterTimeout":
            a.update(outcome="timed_out", why=_clip(out.error, 300))
            self._failed_attempt(ctx, rec, p, f"it did not finish in its "
                                 f"{self.budget // 60}-minute budget and was stopped", events)
        elif out.status == "failed" and (out.error_type in PASSING_TYPES or (
                not out.error_type and _PASSING.search(out.error or ""))):
            # lost touch WHILE Daedalus ran it: it may still be running and commit, so wait
            # out its deadline before sending the job again (a commit is found meanwhile)
            running = "while running job" in (out.error or "")
            self._not_started(ctx, p, a, f"Daedalus or the GPU was not available "
                              f"({_clip(out.error, 160)})", events, retry=True,
                              hold=(float(a["not_after"]) - ctx.now + 300.0) if running
                              else 0.0)
        elif out.status == "failed" and out.error_type == "AdapterProtocolError":
            a.update(outcome="refused", why=_clip(out.error, 300))
            self._shelve(ctx, rec, p, f"Pionir refused the build: {_clip(out.error, 240)}",
                         events)
        elif out.status == "pending_approval":
            # never expected: the build capability runs with its own grant
            a.update(outcome="parked", approval_id=out.approval_id)
            p.update(state="queued", waiting="Pionir parked the build for approval instead "
                     "of running it; no build runs until that is fixed")
            rec["blocked_until"] = ctx.now + 86400
            events.append(self._event(ctx, "api.waiting", {
                "id": p["id"], "why": "Pionir parked the Daedalus build for approval instead "
                                      "of running it; denied builds never run"}))
        elif out.status == "unreachable":
            # we do not know that it did not start: it is cancelled by its id before anything
            # else is sent (``_orphan``), and until then it is still the one job in flight
            rec["active"] = {"pid": p["id"], "task_id": None, "build_id": a.get("build_id"),
                             "attempt": a["n"], "not_after": a["not_after"]}
            a.pop("finished_at", None)
            self._orphan(ctx, rec, p, rec["active"], events)
        else:
            a.update(outcome="failed", why=_clip(out.error, 300))
            self._failed_attempt(ctx, rec, p, f"the job failed ({_clip(out.error, 240)})",
                                 events)
        self._save(ctx, rec)

    def _not_started(self, ctx, p, a, why, events, *, retry=False, hold=0.0) -> None:
        a.update(outcome="not_started", why=_clip(why, 300))
        # not an attempt: a repair stays a repair, with its reasons
        p.update(state="repair" if a.get("kind") == "repair" else "queued",
                 waiting=_clip(why, 300),
                 retry_after=ctx.now + max(RETRY_AFTER if retry else 0.0, hold))
        events.append(self._event(ctx, "api.waiting", {"id": p["id"],
                                                       "why": _clip(why, 200)},
                                  names=(p.get("name"),)))

    def _landed(self, ctx, rec, p, a, output, events) -> None:
        """Daedalus says its gate passed: the commit must be in the sandbox's own branch."""
        try:
            head = sandbox.head(p["repo"], **self._git())
            commit = str(output.get("commit") or "")
            if head == p["base"] and commit:
                head = sandbox.built_commit(p["repo"], p["base"], commit, **self._git())
        except sandbox.SandboxError as exc:
            a.update(outcome="failed", why=_clip(exc, 300))
            self._failed_attempt(ctx, rec, p, f"its commit could not be found in the sandbox "
                                 f"({_clip(exc, 200)})", events)
            return
        if head == p["base"] or head == p.get("reviewed_head"):
            a.update(outcome="gate_failed", why="Daedalus reported a pass but committed nothing")
            self._failed_attempt(ctx, rec, p, "Daedalus reported a pass but committed nothing "
                                 "new", events)
            return
        a.update(outcome="built", commit=head, job_id=output.get("job_id"),
                 files=list(output.get("files") or [])[:40])
        p.update(state="built", head=head, waiting=None, review_failures=0, review_after=0.0)
        events.append(self._event(ctx, "api.built", {
            "id": p["id"], "attempt": a["n"], "commit": head[:12]}, names=(p.get("name"),)))

    def _lost(self, ctx, rec, p, events, why) -> None:
        a = p["attempts"][-1]
        rec["active"] = None
        a["finished_at"] = ctx.now
        try:
            head = sandbox.head(p["repo"], **self._git())
        except sandbox.SandboxError:
            head = p["base"]
        if head not in (p["base"], p.get("reviewed_head")):
            # the outcome never reached us, but a commit landed: it is checked like any build
            self._landed(ctx, rec, p, a, {"ok": True, "commit": head}, events)
            a["note"] = f"Pionir lost track of the job ({why}), but a commit landed"
        else:
            a.update(outcome="lost", why=_clip(why, 300))
            self._failed_attempt(ctx, rec, p, f"the job was lost: {why}", events)
        self._save(ctx, rec)

    @staticmethod
    def _counted(p: dict) -> int:
        return sum(1 for a in p["attempts"] if a.get("outcome") in COUNTED)

    def _failed_attempt(self, ctx, rec, p, why: str, events: list) -> None:
        if self._counted(p) < self.max_attempts:
            p.update(state="repair", repair_reasons=[_clip(why, 400)], waiting=None,
                     retry_after=0.0)
            events.append(self._event(ctx, "api.attempt_failed", {
                "id": p["id"], "why": _clip(why, 200), "repair_next": True},
                names=(p.get("name"),)))
        else:
            self._shelve(ctx, rec, p, why, events)

    # ---- an unconfirmed or abandoned job -----------------------------------------------------
    def _recover_orphans(self, ctx, rec, events) -> None:
        """A product left ``building`` with no job in flight (a crash between two saves) is
        never stuck: its last attempt becomes the job in flight again."""
        act = rec.get("active")
        for p in rec["products"].values():
            if p.get("state") != "building" or (act and act.get("pid") == p["id"]):
                continue
            a = (p.get("attempts") or [{}])[-1]
            rec["active"] = act = {"pid": p["id"], "task_id": a.get("task_id"),
                                   "build_id": a.get("build_id"), "attempt": a.get("n"),
                                   "not_after": float(a.get("not_after") or ctx.now)}
            log.warning("%s: %s was left building with no job in flight; following it",
                        self.worker_id, p["id"])
            break

    def _orphan(self, ctx, rec, p, act, events) -> None:
        """A job Pionir may have started but never told us about: cancel it by its build id
        before anything else is sent. Until that is confirmed it stays the one job in
        flight; past its deadline with no confirmation it is lost."""
        build_id = act.get("build_id")
        if build_id:
            out = ctx.job(Job(CANCEL, {"build_id": build_id},
                              what=f"cancel the unconfirmed build of {p['id']}"))
            if out.status == "done":
                rec["active"] = None
                a = p["attempts"][-1]
                a.update(finished_at=ctx.now, outcome="not_started",
                         why="its start was never confirmed; it was cancelled")
                self._not_started(ctx, p, a, "its start was never confirmed; it was cancelled",
                                  events, retry=True)
                self._save(ctx, rec)
                return
        if ctx.now > float(act.get("not_after") or 0) + LOST_AFTER:
            self._lost(ctx, rec, p, events, "its start was never confirmed and it could not "
                                             "be cancelled")

    def _landed_meanwhile(self, ctx, rec, p, events) -> bool:
        """Before a retry: a job whose outcome never reached us may still have committed."""
        if not p.get("attempts") or not p.get("repo") or not p.get("base"):
            return False
        try:
            head = sandbox.head(p["repo"], **self._git())
        except sandbox.SandboxError:
            return False
        if head in (p["base"], p.get("reviewed_head"), p.get("head")):
            return False
        self._landed(ctx, rec, p, p["attempts"][-1], {"ok": True, "commit": head}, events)
        self._save(ctx, rec)
        return True

    # ---- sending the job ---------------------------------------------------------------------
    def _job_start(self, ctx, rec: dict, p: dict, entry: dict, night, events: list) -> None:
        kind = "repair" if p.get("state") == "repair" else "build"
        try:
            build_sandbox.link_node_modules(p["repo"], self._setup, **self._link())
        except (OSError, build_sandbox.SandboxError) as exc:
            p.update(waiting=f"the tools could not be linked into its repo ({_clip(exc, 160)})",
                     retry_after=ctx.now + RETRY_AFTER)
            events.append(self._event(ctx, "api.waiting", {
                "id": p["id"], "why": p["waiting"]}, names=(p.get("name"),)))
            return
        verify = verify_command(self._setup)
        deadline = not_after(night, ctx.now, self.budget)
        n = len(p["attempts"]) + 1
        build_id = f"{p['slug']}-{n}-{secrets.token_hex(4)}"
        payload = {"intent": intent_text(entry, p["repo"], verify,
                                         (p.get("repair_reasons") or []) if kind == "repair"
                                         else ()),
                   "repo": p["repo"], "verify": verify, "budget_seconds": self.budget,
                   "not_after": deadline, "build_id": build_id}
        a = {"n": n, "kind": kind, "submitted_at": ctx.now, "not_after": deadline,
             "night": night.key, "build_id": build_id}
        p["attempts"].append(a)
        rec["nights"][night.key]["started"] = rec["nights"][night.key].get("started") \
            or p["id"]
        p.update(state="building", waiting=None)
        # the latch, saved BEFORE the job can start: whatever happens next, this job is the
        # one in flight until it is settled, cancelled or lost - never a second one
        rec["active"] = {"pid": p["id"], "task_id": None, "build_id": build_id,
                         "attempt": n, "not_after": deadline}
        self._save(ctx, rec)
        out = ctx.job(Job(BUILD, payload, what=f"write the API product {p['id']} with Daedalus "
                                              f"in its sandbox ({kind})",
                          permissions=(PERMISSION,), wait=5.0, follow=0))
        a["task_id"] = out.task_id
        if out.status == "running" and out.task_id:
            rec["active"]["task_id"] = out.task_id
            events.append(self._event(ctx, "api.started", {
                "id": p["id"], "kind": kind, "attempt": n,
                "budget_minutes": self.budget // 60, "not_after": _hm(deadline)},
                names=(p.get("name"),)))
            self._save(ctx, rec)
            return
        if out.status == "failed" and (out.error_type == "CapabilityNotFound"
                                       or _NOT_SET_UP.search(out.error or "")):
            rec["active"] = None
            p["attempts"].pop()
            p.update(state=kind if kind == "repair" else "queued",
                     waiting=f"{BUILD} is not set up in Pionir ({_clip(out.error, 120)})",
                     retry_after=ctx.now + RETRY_AFTER)
            events.append(self._event(ctx, "api.not_set_up", {
                "id": p["id"], "why": _clip(out.error, 160)}, names=(p.get("name"),)))
            self._save(ctx, rec)
            return
        self._job_settle(ctx, rec, p, out, events)

    def _link(self) -> dict:
        make = getattr(self, "make_link", None)
        return {} if make is None else {"make_link": make}
