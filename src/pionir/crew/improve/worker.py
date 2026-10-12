"""``builds.improve``: Pionir's self-improvement - a personality-free worker, one pass a night.

It finds a failure that keeps recurring identically in Pionir's own records, has Daedalus write
a fix with a regression test in the build sandbox, checks and verifies it there, has Claude
review it once, and asks Ian. It NEVER applies anything itself: Pionir's ``improve.apply`` does,
on his yes only (selfimprove/apply.py). docs/SELF_IMPROVEMENT_DESIGN.md has the design; in
order, each run:

1. **What Ian decided** (always, even with the sandbox gone): a parked card that was denied ->
   ``denied``; approved -> ``applied`` / ``stale`` / ``undone`` from Pionir's ``outcome.json``;
   ``undone.json`` (``python -m pionir improve undo``) -> ``undone``.
2. **NOT SET UP** unless the build sandbox's contained user is set up (the Python part of
   ``tools\\setup-build-sandbox.ps1``; node is never needed here) and ``PIONIR_AUTH_COMPAT`` is
   off: nothing is cloned, written or run.
3. **Follow** the one Daedalus job in flight (``coding.daedalus_build``, followed with
   ``ctx.task``, never two at once): landed -> ``verifying``; gate failed, timed out, lost,
   or "a pass but committed nothing new" -> the ONE repair, then ``shelved``; Daedalus or the
   GPU busy -> waiting, never an attempt.
4. **Verify** (one step a run, so no run holds a pool thread for hours): the hard refusals
   (selfimprove/guard.py, fail closed, before any Claude call); the regression test FAILS on the
   base with only the test files changed and PASSES with the change; the full suite of the base
   (cached per base commit) and of the change, contained - no failure the base does not have.
5. **Review**: one Claude call (``ctx.review``: no tools, the daily cap AND the night cap, the
   cheaper model - the crew's ``NIGHT_IMPLS``). Budget spent -> waits, never approved by
   default. Rejected -> the one repair (if not used), then ``shelved``.
6. **Ask Ian**: the patch and its card to ``<improve_dir>/<id>/``, then ``improve.apply``
   ``{id, patch_sha256}`` - parked by Pionir on every call -> ``awaiting_owner``.
7. **Detect** once per night in its window (default 07:00-11:00, after the build window), and
   **start** at most ``per_night`` (2) candidates a night: never while the build window is open
   or ``builds.daedalus`` has a job in flight, and ``rest_minutes`` after its own last job (the
   card is Moss's again in between) - Pionir takes the GPU lease for the job and hands the card
   back to Moss when it ends.

Every candidate and every state it passed through is in its record (``builds.improve.json``,
the ledger ``python -m pionir improve status`` reads). Its pulse goes amber when it detected
clusters for ``amber_nights`` nights running and proposed nothing: wired but inert is said aloud.
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime
from pathlib import Path

from pionir import build_sandbox
from pionir.auth import compat_from_environment
from pionir.selfimprove import guard, store
from pionir.selfimprove.runner import SuiteResult, module_name

from ..blog import _clip, _Unreadable, read_record, record_path, save_record
from ..builds import sandbox as bsandbox
from ..builds.window import Window, can_start, not_after
from ..builds.worker import BUILD, CANCEL, LOST_AFTER, PERMISSION, RETRY_AFTER, _hm
from ..delivery import _PASSING, PASSING_TYPES
from ..figures import Figure
from ..hands import Job, outcome_of
from ..log import log
from ..orders import _NOT_SET_UP
from ..result import Err, Ok, Result
from ..worker import ErrorKind, WorkContext, make_output, never_raises
from ..workers import _Base
from . import detect, prompts
from . import sandbox as isandbox

APPLY = "improve.apply"
BUILDS_WORKER = "builds.daedalus"
SANDBOX_NEIGHBOURS = (BUILDS_WORKER, "products.api_builder")
DAY = 86400.0
DENIED_COOLDOWN = 30 * DAY
SHELVED_COOLDOWN = 14 * DAY
REVIEW_ATTEMPTS = 3
REVIEW_RETRY = 1800.0
SUBMIT_RETRY = 900.0
KEEP_NIGHTS = 30
KEEP_BASELINES = 3
TERMINAL = frozenset({"applied", "denied", "shelved", "stale", "undone"})


def _overlaps(a: Window, b: Window) -> bool:
    """Whether two daily windows share any minute (each may cross midnight)."""
    def minutes(w: Window) -> set:
        s, e = w.start.hour * 60 + w.start.minute, w.end.hour * 60 + w.end.minute
        return set(range(s, e)) if e > s else set(range(s, 1440)) | set(range(0, e))
    return bool(minutes(a) & minutes(b))


class SelfImprover(_Base):
    """``builds.improve``: see the module docstring."""

    record_what = "the self-improvement worker's ledger of every candidate it proposed"

    def __init__(self, spec, *, window: str = "07:00-11:00", builds_window: str = "01:00-07:00",
                 budget_minutes: int = 60, per_night: int = 2, threshold: int = 5,
                 lookback_days: int = 7, fresh_hours: int = 48, amber_nights: int = 3,
                 rest_minutes: int = 10,
                 suite_timeout_minutes: int = 40, targeted_timeout_minutes: int = 5,
                 review_timeout_seconds: float = 600.0) -> None:
        super().__init__(spec)
        self.window = Window.parse(window)
        self.builds_window = Window.parse(builds_window)
        if _overlaps(self.window, self.builds_window):
            raise ValueError("window must not overlap builds_window: the night builds and the "
                             "self-improvement pass never share the GPU")
        if not 10 <= int(budget_minutes) <= 180:
            raise ValueError("budget_minutes must be 10 to 180")
        self.budget = int(budget_minutes) * 60
        if not 1 <= int(per_night) <= 2:
            raise ValueError("per_night must be 1 or 2 (the owner's cap: at most 2 a night)")
        self.per_night = int(per_night)
        if not 2 <= int(threshold) <= 100:
            raise ValueError("threshold must be 2 to 100")
        self.threshold = int(threshold)
        if not 1 <= int(lookback_days) <= 30:
            raise ValueError("lookback_days must be 1 to 30")
        self.lookback_days = int(lookback_days)
        if not 1 <= int(amber_nights) <= 14:
            raise ValueError("amber_nights must be 1 to 14")
        self.amber_nights = int(amber_nights)
        # still recurring: a cluster not seen for this long may already be fixed by hand
        if not 6 <= int(fresh_hours) <= 24 * int(lookback_days):
            raise ValueError("fresh_hours must be 6 to the lookback")
        self.fresh = int(fresh_hours) * 3600.0
        if not 0 <= int(rest_minutes) <= 60:
            raise ValueError("rest_minutes must be 0 to 60")
        self.rest = int(rest_minutes) * 60
        if not 5 <= int(suite_timeout_minutes) <= 120:
            raise ValueError("suite_timeout_minutes must be 5 to 120")
        self.suite_timeout = int(suite_timeout_minutes) * 60.0
        if not 1 <= int(targeted_timeout_minutes) <= 30:
            raise ValueError("targeted_timeout_minutes must be 1 to 30")
        self.targeted_timeout = int(targeted_timeout_minutes) * 60.0
        self.review_timeout = float(review_timeout_seconds)
        # the outside world, injected (tests replace these)
        self.load_sandbox = lambda root: build_sandbox.load_setup(root)
        self.auth_compat = compat_from_environment
        self.run_suite = isandbox.run_suite
        self.git_run = None                     # None: subprocess.run
        self._setup = None

    # ---- readiness ---------------------------------------------------------------------------
    def readiness(self, secrets_dir) -> str | None:
        """NOT SET UP while the contained sandbox user is not (Python only: node is not
        needed), or while auth compat is on."""
        import os
        root = os.environ.get("PIONIR_DAEDALUS_SANDBOX", "").strip() or \
            build_sandbox.default_sandbox_root()
        _setup, why = self.load_sandbox(root)
        return (f"NOT SET UP: {why}" if why else None) or self._compat_refusal()

    def _compat_refusal(self) -> str | None:
        if self.auth_compat():
            return ("NOT SET UP: PIONIR_AUTH_COMPAT is on, so a process with no token "
                    "(generated code on loopback included) is still served; set "
                    "PIONIR_AUTH_COMPAT=off once every client sends its token")
        return None

    # ---- the record --------------------------------------------------------------------------
    @staticmethod
    def _blank() -> dict:
        return {"candidates": {}, "active": None, "nights": {}, "baselines": {}, "counts": {},
                "rest_until": 0.0, "amber": None}

    def load(self, state_dir) -> dict:
        doc = read_record(state_dir, self.worker_id)
        return self._blank() if doc is None else {**self._blank(), **doc}

    def _save(self, ctx: WorkContext, rec: dict) -> None:
        for key in sorted(rec["nights"])[:-KEEP_NIGHTS]:
            rec["nights"].pop(key, None)
        for key in list(rec["baselines"])[:-KEEP_BASELINES]:
            rec["baselines"].pop(key, None)
        save_record(record_path(ctx.state_dir, self.worker_id), rec)

    def _git(self) -> dict:
        return {} if self.git_run is None else {"run": self.git_run}

    def _count(self, rec: dict, name: str) -> None:
        rec["counts"][name] = int(rec["counts"].get(name) or 0) + 1

    def _to(self, ctx, rec: dict, c: dict, state: str, why: str = "", events=None) -> None:
        """One state change, with its reason, in the candidate's own history."""
        c["state"] = state
        c["why"] = _clip(why, 500) if why else c.get("why")
        c.setdefault("history", []).append({"at": ctx.now, "state": state,
                                            "why": _clip(why, 300)})
        if state in TERMINAL:
            c["ended_at"] = ctx.now
            self._count(rec, state)
        if events is not None:
            events.append(self._event(ctx, f"improve.{state}", {
                "id": c["id"], "worker": c["evidence"]["worker"],
                "reason": _clip(c["evidence"]["reason"], 160), "why": _clip(why, 200)}))

    # ---- one run -----------------------------------------------------------------------------
    @never_raises()
    def run(self, ctx: WorkContext) -> Result:
        missing = [n for n, v in (("state_dir", ctx.state_dir),
                                  ("builds_sandbox", ctx.builds_sandbox),
                                  ("improve_dir", ctx.improve_dir),
                                  ("pionir_repo", ctx.pionir_repo)) if v is None]
        if missing:
            return self._err(ErrorKind.NOT_CONFIGURED, f"NOT SET UP: no {', '.join(missing)}",
                             retryable=False)
        if ctx.job is None:
            return self._err(ErrorKind.NOT_CONFIGURED, "NOT SET UP: no hands - it acts only "
                             "through Pionir", retryable=False)
        try:
            rec = self.load(ctx.state_dir)
        except _Unreadable as exc:
            return self._err(ErrorKind.MALFORMED, f"its ledger is unreadable ({exc}); refusing "
                             "to act, since it could start a second job", retryable=False)
        events: list = []
        self._owner(ctx, rec, events)
        setup, why = self.load_sandbox(ctx.builds_sandbox)
        if setup is None:
            self._amber(ctx, rec)
            self._save(ctx, rec)
            return self._err(ErrorKind.NOT_CONFIGURED, f"NOT SET UP: {why or build_sandbox.SETUP_HINT}",
                             retryable=False)
        compat = self._compat_refusal()
        if compat:
            self._save(ctx, rec)
            return self._err(ErrorKind.NOT_CONFIGURED, compat, retryable=False)
        self._setup = setup
        self._follow(ctx, rec, events)
        self._verify_step(ctx, rec, events)
        self._review_step(ctx, rec, events)
        self._detect(ctx, rec, events)
        self._maybe_start(ctx, rec, events)
        self._amber(ctx, rec)
        self._save(ctx, rec)
        return Ok((*events, self._tally(ctx, rec)))

    # ---- 1. what Ian decided -------------------------------------------------------------------
    def _owner(self, ctx: WorkContext, rec: dict, events: list) -> None:
        for c in rec["candidates"].values():
            where = store.folder(ctx.improve_dir, c["id"])
            if c.get("state") == "applied":
                undone = store.read_json(where / store.UNDONE)
                if undone and undone.get("undone"):
                    self._to(ctx, rec, c, "undone", f"Ian undid it (reverted by "
                             f"{str(undone.get('revert_commit') or '')[:10]})", events)
                continue
            if c.get("state") != "awaiting_owner":
                continue
            outcome = store.read_json(where / store.OUTCOME)
            if outcome and outcome.get("patch_sha256") == c.get("patch_sha256"):
                self._settle_outcome(ctx, rec, c, outcome, events)
                continue
            if ctx.approval is None or not c.get("approval_id"):
                continue
            got = ctx.approval(c["approval_id"]) or {}
            state = got.get("status")
            if state == "denied":
                reason = str(got.get("reason") or "denied")
                if reason == "expired":
                    c["expired"] = True
                    self._to(ctx, rec, c, "denied", "the card expired unanswered", events)
                else:
                    self._to(ctx, rec, c, "denied", f"Ian denied it ({_clip(reason, 200)})",
                             events)
            elif state in ("approved", "approved_failed"):
                out = outcome_of(APPLY, got.get("result"))
                result = out.result if isinstance(out.result, dict) else {}
                if result.get("applied") is not None or result.get("stale") is not None:
                    self._settle_outcome(ctx, rec, c, result, events)
                elif state == "approved_failed":
                    self._to(ctx, rec, c, "stale", f"approved, but it could not be applied "
                             f"({_clip(out.error, 200)}); will re-propose", events)

    def _settle_outcome(self, ctx, rec, c: dict, outcome: dict, events: list) -> None:
        if outcome.get("applied") and outcome.get("reverted"):
            c["applied_commit"] = outcome.get("commit")
            self._count(rec, "applied")
            self._to(ctx, rec, c, "undone", str(outcome.get("why") or "reverted at once: its "
                                                "tests failed on main"), events)
        elif outcome.get("applied"):
            c["applied_commit"] = outcome.get("commit")
            c["applied_at"] = ctx.now
            self._to(ctx, rec, c, "applied", f"commit {str(outcome.get('commit'))[:10]} on main; "
                     f"undo: python -m pionir improve undo {c['id']}", events)
        elif outcome.get("stale"):
            self._to(ctx, rec, c, "stale", str(outcome.get("why") or "main moved; will "
                                               "re-propose"), events)
        else:
            self._to(ctx, rec, c, "stale", f"approved, but it could not be applied "
                     f"({_clip(outcome.get('error'), 200)}); will re-propose", events)

    # ---- 3. the Daedalus job in flight ---------------------------------------------------------
    def _follow(self, ctx: WorkContext, rec: dict, events: list) -> None:
        act = rec.get("active")
        if not act:
            return
        c = rec["candidates"].get(act.get("id"))
        if c is None:
            rec["active"] = None
            return
        if not act.get("task_id"):
            self._orphan(ctx, rec, c, act, events)
            return
        if ctx.task is None:
            return
        out = ctx.task(BUILD, act["task_id"])
        if out.status in ("running", "unreachable"):
            if ctx.now > float(act["not_after"]) + LOST_AFTER:
                self._lost(ctx, rec, c, events, f"no outcome {LOST_AFTER / 60:.0f} minutes after "
                           f"its deadline (Pionir said {out.status})")
            return
        if out.status == "failed" and not out.error_type and re.search(
                r"(?i)\b404\b|not found|unknown task", out.error or ""):
            if ctx.now > float(act["not_after"]) + LOST_AFTER:
                self._lost(ctx, rec, c, events, "Pionir no longer knows the task")
            return
        self._settle_job(ctx, rec, c, out, events)

    def _settle_job(self, ctx, rec: dict, c: dict, out, events: list) -> None:
        rec["active"] = None
        rec["rest_until"] = ctx.now + self.rest         # Moss has the card back for a while
        a = c["attempts"][-1]
        a.update(finished_at=ctx.now, pionir_status=out.status)
        output = out.result if isinstance(out.result, dict) else {}
        if out.status == "done" and output.get("not_configured"):
            self._wait(ctx, c, a, str(output.get("refused") or build_sandbox.SETUP_HINT),
                       events, retry=True)
        elif out.status == "done" and output.get("refused") and output.get("started") is False:
            self._wait(ctx, c, a, "the window closed before the GPU was free; nothing was "
                                  "started", events)
        elif out.status == "done" and output.get("ok") is True \
                and output.get("passed") is not False:
            self._landed(ctx, rec, c, a, output, events)
        elif out.status == "done":
            why = (output.get("gate_reason") or output.get("error")
                   or "Daedalus reported it did not work")
            a.update(outcome="gate_failed", why=_clip(why, 300))
            self._failed(ctx, rec, c, [f"Daedalus's own gate did not pass: {_clip(why, 300)}"],
                         events)
        elif out.status == "failed" and out.error_type == "AdapterTimeout":
            a.update(outcome="timed_out", why=_clip(out.error, 300))
            self._failed(ctx, rec, c, [f"it did not finish in its {self.budget // 60}-minute "
                                       "budget and was stopped"], events)
        elif out.status == "failed" and (out.error_type in PASSING_TYPES or (
                not out.error_type and _PASSING.search(out.error or ""))):
            running = "while running job" in (out.error or "")
            self._wait(ctx, c, a, f"Daedalus or the GPU was not available "
                       f"({_clip(out.error, 160)})", events, retry=True,
                       hold=(float(a["not_after"]) - ctx.now + 300.0) if running else 0.0)
        elif out.status == "failed" and out.error_type == "AdapterProtocolError":
            a.update(outcome="refused", why=_clip(out.error, 300))
            self._to(ctx, rec, c, "shelved", f"Pionir refused the job: {_clip(out.error, 240)}",
                     events)
        elif out.status == "pending_approval":
            a.update(outcome="parked", approval_id=out.approval_id)
            self._to(ctx, rec, c, "shelved", "Pionir parked the Daedalus job for approval "
                     "instead of running it with its sandbox grant; nothing is written until "
                     "that is fixed", events)
        elif out.status == "unreachable":
            rec["active"] = {"id": c["id"], "task_id": None, "build_id": a.get("build_id"),
                             "not_after": a["not_after"]}
            a.pop("finished_at", None)
            self._orphan(ctx, rec, c, rec["active"], events)
        else:
            a.update(outcome="failed", why=_clip(out.error, 300))
            self._failed(ctx, rec, c, [f"the job failed ({_clip(out.error, 240)})"], events)
        self._save(ctx, rec)

    def _wait(self, ctx, c, a, why, events, *, retry=False, hold=0.0) -> None:
        """Not an attempt: the job did not run (Daedalus, the GPU or the window)."""
        a.update(outcome="not_started", why=_clip(why, 300))
        c.update(waiting=_clip(why, 300),
                 retry_after=ctx.now + max(RETRY_AFTER if retry else 0.0, hold))
        events.append(self._event(ctx, "improve.waiting", {"id": c["id"],
                                                          "why": _clip(why, 200)}))

    def _landed(self, ctx, rec, c, a, output, events) -> None:
        """Daedalus says its gate passed: a NEW commit must be in the sandbox repo."""
        prior = c.get("head") or c["base"]
        try:
            head = bsandbox.head(c["repo"], **self._git())
            commit = str(output.get("commit") or "")
            if head == prior and commit:
                head = bsandbox.built_commit(c["repo"], c["base"], commit, **self._git())
        except bsandbox.SandboxError as exc:
            a.update(outcome="failed", why=_clip(exc, 300))
            self._failed(ctx, rec, c, [f"its commit could not be found in the sandbox "
                                       f"({_clip(exc, 200)})"], events)
            return
        if head == prior:
            a.update(outcome="gate_failed", why="Daedalus reported a pass but committed nothing")
            self._failed(ctx, rec, c, ["Daedalus reported a pass but committed nothing new"],
                         events)
            return
        a.update(outcome="built", commit=head)
        c.update(head=head, waiting=None, verify={}, verdict=None)
        self._to(ctx, rec, c, "verifying", f"Daedalus committed {head[:10]}", events)

    def _lost(self, ctx, rec, c, events, why) -> None:
        a = c["attempts"][-1]
        rec["active"] = None
        rec["rest_until"] = ctx.now + self.rest
        a["finished_at"] = ctx.now
        try:
            head = bsandbox.head(c["repo"], **self._git())
        except bsandbox.SandboxError:
            head = c.get("head") or c["base"]
        if head not in (c["base"], c.get("head")):
            self._landed(ctx, rec, c, a, {"ok": True, "commit": head}, events)
            a["note"] = f"Pionir lost track of the job ({why}), but a commit landed"
        else:
            a.update(outcome="lost", why=_clip(why, 300))
            self._failed(ctx, rec, c, [f"the job was lost: {why}"], events)
        self._save(ctx, rec)

    def _orphan(self, ctx, rec, c, act, events) -> None:
        """A job Pionir may have started but never confirmed: cancelled by its build id before
        anything else is sent; lost past its deadline."""
        if act.get("build_id"):
            out = ctx.job(Job(CANCEL, {"build_id": act["build_id"]},
                              what=f"cancel the unconfirmed self-improvement job {c['id']}"))
            if out.status == "done":
                rec["active"] = None
                a = c["attempts"][-1]
                a["finished_at"] = ctx.now
                self._wait(ctx, c, a, "its start was never confirmed; it was cancelled", events,
                           retry=True)
                self._save(ctx, rec)
                return
        if ctx.now > float(act.get("not_after") or 0) + LOST_AFTER:
            self._lost(ctx, rec, c, events, "its start was never confirmed and it could not be "
                                            "cancelled")

    def _failed(self, ctx, rec, c: dict, reasons: list, events: list, *,
                hard: bool = False) -> None:
        """A failed attempt, verification or review: the ONE repair with the reasons, or
        shelved. A hard refusal (the protected list) is shelved at once."""
        text = "; ".join(_clip(r, 200) for r in reasons[:6])
        if not hard and int(c.get("repairs") or 0) < 1:
            c["repairs"] = 1
            c.update(start_next="repair", repair_reasons=[_clip(r, 400) for r in reasons[:10]],
                     retry_after=0.0, waiting=None)
            self._to(ctx, rec, c, "writing", f"one repair follows: {text}", events)
        else:
            self._to(ctx, rec, c, "shelved", ("refused by the protected list: " if hard else
                                              "after its one repair: ") + text, events)

    # ---- 4. verification, one step a run -----------------------------------------------------
    def _verify_step(self, ctx: WorkContext, rec: dict, events: list) -> None:
        if rec.get("active") or self._builds_busy(ctx):
            return          # a run reaps every process of the sandbox user: never mid-job
        c = next((c for c in sorted(rec["candidates"].values(),
                                    key=lambda q: float(q.get("created_at") or 0))
                  if c.get("state") == "verifying"), None)
        if c is None:
            return
        v = c.setdefault("verify", {})
        try:
            base = isandbox.export(c["repo"], c["base"], **self._git())
            head = isandbox.export(c["repo"], c["head"], **self._git())
            patch = isandbox.diff(c["repo"], c["base"], c["head"], **self._git())
        except bsandbox.SandboxError as exc:
            self._to(ctx, rec, c, "shelved", f"its sandbox repo could not be read "
                     f"({_clip(exc, 200)})", events)
            return
        if not v.get("checked"):
            paths = guard.changed_paths(patch)
            local = {Path(p).stem for p in base if p.startswith("tests/") and p.count("/") == 1}
            hard = guard.patch_problems(patch, local_modules=local) + \
                guard.tree_problems(paths, base, head)
            if hard:
                c["refusals"] = [_clip(r, 300) for r in hard[:12]]
                self._failed(ctx, rec, c, hard, events, hard=True)
                return
            want = prompts.regression_file(c["id"])
            if want not in head or base.get(want) is not None:
                self._failed(ctx, rec, c, [f"it did not add the regression test {want}"],
                             events)
                return
            v.update(checked=True, tests=sorted(p for p in paths if module_name(p)),
                     lines=sum(f.lines for f in guard.parse_patch(patch)), files=len(paths))
            self._save(ctx, rec)
            return
        if "with" not in v:
            mods = [module_name(p) for p in v["tests"]]
            with_change = self.run_suite(head, setup=self._setup, timeout=self.targeted_timeout,
                                         modules=mods)
            without = dict(base)
            for p in v["tests"]:
                without[p] = head[p]            # the new tests on the OLD code
            before = self.run_suite(without, setup=self._setup, timeout=self.targeted_timeout,
                                    modules=mods)
            v.update({"with": with_change.to_dict(), "without": before.to_dict()})
            problems = []
            if not with_change.ok or with_change.ran == 0:
                problems.append(f"its regression test does not pass with the change "
                                f"({_clip(with_change.tail[-300:], 300)})")
            if before.ok or (before.finished and not before.failed):
                problems.append("its regression test PASSES without the change: it does not "
                                "reproduce the failure")
            if problems:
                self._failed(ctx, rec, c, problems, events)
                return
            self._save(ctx, rec)
            return
        baseline = SuiteResult.from_dict(rec["baselines"].get(c["base"]))
        if baseline is None:
            got = self.run_suite(base, setup=self._setup, timeout=self.suite_timeout)
            if not got.finished:
                self._to(ctx, rec, c, "shelved", f"the full suite of main could not be run in "
                         f"the sandbox ({_clip(got.tail[-200:], 200)}); nothing to compare "
                         "against", events)
                return
            rec["baselines"][c["base"]] = got.to_dict() | {"failed": got.failed}
            self._save(ctx, rec)
            return
        got = self.run_suite(head, setup=self._setup, timeout=self.suite_timeout)
        v["suite"] = got.to_dict()
        if not got.finished:
            self._failed(ctx, rec, c, [f"the full suite did not finish with the change "
                                       f"({_clip(got.tail[-200:], 200)})"], events)
            return
        new = sorted(set(got.failed) - set(baseline.failed))
        if new or got.ran < baseline.ran:
            why = (f"the full suite has {len(new)} failure(s) main does not have: "
                   f"{', '.join(new[:5])}" if new else
                   f"the full suite ran {got.ran} tests, fewer than main's {baseline.ran}")
            self._failed(ctx, rec, c, [why], events)
            return
        c["verification"] = (
            f"the regression test failed without the change and passed with it; the full suite "
            f"ran {got.ran} tests with the change and has no failure main does not have"
            + (f" (main itself fails {len(baseline.failed)} in the sandbox: "
               f"{', '.join(baseline.failed[:3])})" if baseline.failed else " (all green)")
            + f"; {v.get('files')} file(s), {v.get('lines')} changed line(s)")
        self._to(ctx, rec, c, "reviewing", "verified in the sandbox", events)

    # ---- 5./6. Claude's review, then Ian ----------------------------------------------------
    def _review_step(self, ctx: WorkContext, rec: dict, events: list) -> None:
        c = next((c for c in sorted(rec["candidates"].values(),
                                    key=lambda q: float(q.get("created_at") or 0))
                  if c.get("state") == "reviewing"
                  and float(c.get("review_after") or 0) <= ctx.now), None)
        if c is None:
            return
        try:
            patch = isandbox.diff(c["repo"], c["base"], c["head"], **self._git())
        except bsandbox.SandboxError as exc:
            self._to(ctx, rec, c, "shelved", f"its sandbox repo could not be read "
                     f"({_clip(exc, 200)})", events)
            return
        verdict = c.get("verdict")
        if not (isinstance(verdict, dict) and verdict.get("head") == c["head"]):
            verdict = self._ask_claude(ctx, rec, c, patch, events)
            if verdict is None:
                return
        if not verdict["approved"]:
            c["reviewed_head"] = c["head"]
            self._failed(ctx, rec, c, [f"Claude: {r}" for r in verdict["reasons"]], events)
            return
        self._submit(ctx, rec, c, patch, events)

    def _ask_claude(self, ctx, rec, c, patch: str, events) -> dict | None:
        prompt, why = prompts.review_prompt(c, patch, c.get("verification") or "")
        if prompt is None:
            self._failed(ctx, rec, c, [why], events)
            return None
        if ctx.review is None:
            c.update(waiting="no Claude review path is configured; nothing reaches Ian without "
                     "Claude's review", review_after=ctx.now + REVIEW_RETRY)
            return None
        got = ctx.review(prompt, self.review_timeout)
        if isinstance(got, Err):
            refusal = got.error
            if getattr(refusal, "waits", False):
                c.update(waiting=f"waiting for Claude's review ({_clip(refusal, 160)})",
                         review_after=ctx.now + REVIEW_RETRY)
                events.append(self._event(ctx, "improve.waiting", {
                    "id": c["id"], "why": _clip(c["waiting"], 200)}))
                return None
            return self._review_failed(ctx, rec, c, f"Claude did not answer: "
                                                    f"{_clip(refusal, 200)}", events)
        verdict = prompts.parse_verdict(str(got.value))
        if verdict is None:
            return self._review_failed(ctx, rec, c, "Claude's answer was not a readable verdict",
                                       events)
        verdict.update(head=c["head"], at=ctx.now)
        c["verdict"] = verdict
        c.setdefault("reviews", []).append(verdict)
        self._count(rec, "reviews")
        self._save(ctx, rec)                    # a verdict is never asked for twice
        return verdict

    def _review_failed(self, ctx, rec, c, why: str, events) -> None:
        c["review_failures"] = int(c.get("review_failures") or 0) + 1
        if c["review_failures"] >= REVIEW_ATTEMPTS:
            self._to(ctx, rec, c, "shelved", f"Claude's review could not be completed "
                     f"({c['review_failures']} tries; the last: {why})", events)
        else:
            c.update(waiting=_clip(why, 200), review_after=ctx.now + REVIEW_RETRY)
        return None

    def _submit(self, ctx, rec, c: dict, patch: str, events: list) -> None:
        if not c.get("patch_sha256") or c.get("patch_head") != c["head"]:
            try:
                stat = isandbox.diff_stat(c["repo"], c["base"], c["head"], **self._git())
            except bsandbox.SandboxError:
                stat = ""
            card = {"title": c["title"], "evidence": c["evidence"], "hypothesis":
                    c["hypothesis"], "verdict": {k: c["verdict"].get(k) for k in
                                                 ("approved", "summary", "reasons", "checks")},
                    "verification": c.get("verification"), "diff_stat": stat,
                    "base": c["base"], "head": c["head"], "tests": c["verify"].get("tests"),
                    "targets": c.get("targets")}
            c["patch_sha256"] = store.write_patch(ctx.improve_dir, c["id"], patch, card)
            c["patch_head"] = c["head"]
        out = ctx.job(Job(APPLY, {"id": c["id"], "patch_sha256": c["patch_sha256"]},
                          what=f"ask the owner to apply self-improvement {c['id']}: "
                               f"{_clip(c['title'], 80)}"))
        why = out.error or f"Pionir said {out.status}"
        if out.status == "pending_approval":
            c.update(approval_id=out.approval_id, waiting=None)
            self._count(rec, "proposals")
            self._to(ctx, rec, c, "awaiting_owner", f"waiting for Ian's yes (approval "
                     f"{out.approval_id})", events)
        elif out.status == "done":
            log.error("%s: %s ran WITHOUT the owner's approval; it must be approval-gated",
                      self.worker_id, APPLY)
            result = out.result if isinstance(out.result, dict) else {}
            self._count(rec, "proposals")
            self._settle_outcome(ctx, rec, c, result, events)
        elif out.status == "failed" and out.error_type == "AdapterProtocolError":
            self._to(ctx, rec, c, "shelved", f"Pionir refused to ask Ian: {_clip(why, 240)}",
                     events)
        else:
            not_set_up = out.error_type == "CapabilityNotFound" or _NOT_SET_UP.search(why)
            c.update(waiting=f"{APPLY} {'is not set up in Pionir' if not_set_up else 'did not reach Pionir'} "
                             f"({_clip(why, 160)}); offered again", review_after=ctx.now + SUBMIT_RETRY)
        self._save(ctx, rec)

    # ---- 7. one detection pass a night, and starting candidates --------------------------------
    def _detect(self, ctx: WorkContext, rec: dict, events: list) -> None:
        night = self.window.current(ctx.now)
        if night is None or night.key in rec["nights"]:
            return
        since = ctx.now - self.lookback_days * DAY
        sightings = self._sightings(ctx, since)
        clusters = detect.cluster(sightings, threshold=self.threshold, since=since)
        eligible, skipped = [], {}
        for cl in clusters:
            if cl.last_seen < ctx.now - self.fresh:
                skipped[cl.key] = (f"not seen for {self.fresh / 3600:.0f} hours: it may already "
                                   "be fixed")
                continue
            applied = [c for c in rec["candidates"].values() if c.get("key") == cl.key
                       and c.get("state") == "applied"]
            if applied:
                after = max(float(c.get("applied_at") or c.get("ended_at") or 0)
                            for c in applied)
                again = detect.cluster([s for s in sightings if s.at > after],
                                       threshold=self.threshold, since=max(since, after))
                if not any(a.key == cl.key for a in again):
                    skipped[cl.key] = "a fix for it was applied and it has not recurred enough"
                    continue
            eligible.append(cl.evidence(self.lookback_days))
        rec["nights"][night.key] = {"start": night.start, "end": night.end,
                                    "detected_at": ctx.now, "clusters": len(clusters),
                                    "eligible": eligible[:10], "skipped": skipped,
                                    "started": [], "why_none": None}
        self._count(rec, "detection_passes")
        events.append(self._event(ctx, "improve.detected", {
            "night": night.key, "clusters": len(clusters), "eligible": len(eligible),
            "top": [{"worker": e["worker"], "reason": _clip(e["reason"], 120),
                     "count": e["count"]} for e in eligible[:3]]},
            figures=[Figure(len(clusters), "count", "recurring failures found",
                            window=f"last {self.lookback_days} days")]))

    def _sightings(self, ctx: WorkContext, since: float) -> list:
        out: list = []
        if ctx.refusals is not None:
            out += detect.from_outputs(ctx.refusals(since, detect.BLOCK_KINDS))
        if ctx.failed_runs is not None:
            out += detect.from_runs(ctx.failed_runs(since))
        out += detect.read_vitals(ctx.crew_log)
        try:
            out += detect.from_builds(read_record(ctx.state_dir, BUILDS_WORKER))
        except _Unreadable:
            pass
        if ctx.approvals_path is not None:
            out += detect.read_approvals(ctx.approvals_path)
        out += detect.read_lessons(ctx.cortex_path, since)
        return out

    def eligible_why(self, rec: dict, key: str, now: float) -> str | None:
        """Why a cluster may not be proposed now (dedupe), or None."""
        for c in rec["candidates"].values():
            if c.get("key") != key:
                continue
            state, ended = c.get("state"), float(c.get("ended_at") or 0)
            if state in store.OPEN:
                return f"{c['id']} is still open ({state})"
            if state in ("denied", "undone") and not c.get("expired") \
                    and now - ended < DENIED_COOLDOWN:
                return f"{c['id']} was {state} {int((now - ended) // DAY)} day(s) ago (30-day rest)"
            if (state == "shelved" or c.get("expired")) and now - ended < SHELVED_COOLDOWN:
                return f"{c['id']} was {'unanswered' if c.get('expired') else 'shelved'} " \
                       f"{int((now - ended) // DAY)} day(s) ago (14-day rest)"
        return None

    def _builds_busy(self, ctx: WorkContext) -> str | None:
        """The sandbox is the night builds' now: their window is open, or a night build or API
        build has a job in flight or a build still waiting for its own contained check. Every
        contained run kills every process of the sandbox user first, so neither side may run
        while the other might."""
        if self.builds_window.current(ctx.now) is not None:
            return "the night-build window is open"
        for other in SANDBOX_NEIGHBOURS:
            try:
                doc = read_record(ctx.state_dir, other)
            except _Unreadable:
                return f"{other}'s record is unreadable, so its job may be in flight"
            if doc and doc.get("active"):
                return f"{other} has a job in flight"
            if doc and any(isinstance(p, dict) and p.get("state") == "built"
                           for p in (doc.get("products") or {}).values()):
                return f"{other} has a build waiting for its contained check"
        return None

    def _maybe_start(self, ctx: WorkContext, rec: dict, events: list) -> None:
        if rec.get("active"):
            return                                          # one job at a time
        night = self.window.current(ctx.now)
        if not can_start(night, ctx.now, self.budget):
            return                                          # its window only, and must fit
        if self._builds_busy(ctx) or float(rec.get("rest_until") or 0) > ctx.now:
            return                                          # the card is someone else's now
        c = next((c for c in rec["candidates"].values() if c.get("state") == "writing"
                  and float(c.get("retry_after") or 0) <= ctx.now), None)
        if c is None:
            c = self._new_candidate(ctx, rec, night, events)
        if c is not None:
            self._start_job(ctx, rec, c, night, events)

    def _new_candidate(self, ctx, rec: dict, night, events: list) -> dict | None:
        tonight = rec["nights"].get(night.key)
        if tonight is None:
            return None
        if len(tonight["started"]) >= self.per_night:
            return None                                     # the owner's cap: 2 a night
        if any(c.get("state") in ("writing", "verifying") for c in rec["candidates"].values()):
            return None                                     # one in the sandbox at a time
        ev = None
        for e in tonight["eligible"]:
            why = self.eligible_why(rec, e["key"], ctx.now)
            if why is None:
                ev = e
                break
            tonight["skipped"][e["key"]] = why
        if ev is None:
            if not tonight["started"]:
                tonight["why_none"] = ("no recurring failure found" if not tonight["clusters"]
                                       else "every cluster was already proposed, denied or "
                                            "resting")
            return None
        stamp = datetime.fromtimestamp(ctx.now).strftime("%Y%m%d")
        cid = f"imp-{stamp}-{secrets.token_hex(2)}"
        while cid in rec["candidates"]:
            cid = f"imp-{stamp}-{secrets.token_hex(3)}"
        cl = detect.Cluster(ev["source"], ev["worker"], ev["reason"], ev["count"],
                            ev["first_seen_at"], ev["last_seen_at"], ev["samples"],
                            ev["example"])
        targets = detect.find_targets(ctx.pionir_repo, cl)
        c = {"id": cid, "key": ev["key"], "night": night.key, "created_at": ctx.now,
             "evidence": ev, "targets": targets,
             "hypothesis": detect.hypothesis(cl, targets, self.lookback_days),
             "title": _clip(f"{ev['worker']}: stop \"{ev['example']}\" recurring", 140),
             "attempts": [], "repairs": 0, "history": [], "state": "detected"}
        rec["candidates"][cid] = c
        tonight["started"].append(cid)
        self._to(ctx, rec, c, "detected", f"{ev['count']} times in {self.lookback_days} days",
                 events)
        try:
            repo, base = isandbox.clone(ctx.builds_sandbox, cid, ctx.pionir_repo,
                                        marker={"made_by": "the crew's self-improvement worker",
                                                "candidate": cid, "created_at": ctx.now},
                                        **self._git())
        except (bsandbox.SandboxError, OSError) as exc:
            self._to(ctx, rec, c, "shelved", f"its sandbox clone could not be made "
                     f"({_clip(exc, 200)})", events)
            self._save(ctx, rec)
            return None
        c.update(repo=repo, base=base, start_next="build")
        self._to(ctx, rec, c, "writing", f"cloned main at {base[:10]} into the sandbox", events)
        self._save(ctx, rec)
        return c

    def _start_job(self, ctx, rec: dict, c: dict, night, events: list) -> None:
        kind = c.get("start_next") or "build"
        deadline = not_after(night, ctx.now, self.budget)
        n = len(c["attempts"]) + 1
        build_id = f"{isandbox.slug_for(c['id'])}-{n}-{secrets.token_hex(4)}"
        payload = {"intent": prompts.intent(c, c["repo"], repair=c.get("repair_reasons") or []
                                            if kind == "repair" else ()),
                   "repo": c["repo"], "verify": prompts.verify_command(c["id"]),
                   "budget_seconds": self.budget, "not_after": deadline, "build_id": build_id}
        a = {"n": n, "kind": kind, "submitted_at": ctx.now, "not_after": deadline,
             "night": night.key, "build_id": build_id}
        c["attempts"].append(a)
        # the latch, saved BEFORE the job can start: never a second one in flight
        rec["active"] = {"id": c["id"], "task_id": None, "build_id": build_id,
                         "not_after": deadline}
        self._save(ctx, rec)
        out = ctx.job(Job(BUILD, payload, what=f"write self-improvement {c['id']} with Daedalus "
                                              f"in the sandbox ({kind})",
                          permissions=(PERMISSION,), wait=5.0, follow=0))
        a["task_id"] = out.task_id
        if out.status == "running" and out.task_id:
            rec["active"]["task_id"] = out.task_id
            c["waiting"] = None
            events.append(self._event(ctx, "improve.started", {
                "id": c["id"], "kind": kind, "attempt": n, "budget_minutes": self.budget // 60,
                "not_after": _hm(deadline)}))
            self._save(ctx, rec)
            return
        if out.status == "failed" and (out.error_type == "CapabilityNotFound"
                                       or _NOT_SET_UP.search(out.error or "")):
            rec["active"] = None
            c["attempts"].pop()
            c.update(waiting=f"{BUILD} is not set up in Pionir ({_clip(out.error, 120)})",
                     retry_after=ctx.now + RETRY_AFTER)
            self._save(ctx, rec)
            return
        self._settle_job(ctx, rec, c, out, events)

    # ---- what Atani, Moss and the desktop see ----------------------------------------------------
    def _amber(self, ctx, rec: dict) -> None:
        """Wired but inert, said aloud: clusters found on each of the last ``amber_nights``
        finished nights, and not one proposal from any of them."""
        done = [(k, n) for k, n in sorted(rec["nights"].items())
                if float(n.get("end") or 0) <= ctx.now][-self.amber_nights:]
        rec["amber"] = None
        if len(done) < self.amber_nights or not all(n.get("clusters") for _k, n in done):
            return
        proposed = [c for c in rec["candidates"].values()
                    if c.get("night") in {k for k, _n in done}
                    and any(h.get("state") == "awaiting_owner" for h in c.get("history") or [])]
        if proposed:
            return
        last_k, last = done[-1]
        tried = [c for c in rec["candidates"].values() if c.get("night") == last_k]
        why = ("; ".join(f"{c['id']} {c.get('state')}: {_clip(c.get('why'), 120)}"
                         for c in tried[:2])
               or last.get("why_none") or "nothing was started")
        rec["amber"] = (f"found recurring failures on each of the last {len(done)} nights "
                        f"({', '.join(str(n.get('clusters')) for _k, n in done)} clusters) but "
                        f"proposed nothing; last night: {why}")

    def pulse(self, state_dir, now: float, digest=None) -> dict:
        """``alert`` while it is amber (see ``_amber``): its runs succeed, so nothing else
        would say so."""
        out: dict = {"worker": self.worker_id, "capability": APPLY}
        if state_dir is None:
            return {**out, "alert": "no state dir: it cannot keep its ledger"}
        try:
            rec = self.load(state_dir)
        except _Unreadable as exc:
            return {**out, "alert": f"its ledger is unreadable ({exc})"}
        counts = rec.get("counts") or {}
        out.update({"proposals": int(counts.get("proposals") or 0),
                    "applied": int(counts.get("applied") or 0), "alert": rec.get("amber")})
        return out

    def _event(self, ctx, kind: str, payload: dict, figures=()):
        return make_output(self, kind=kind, valid_at=ctx.now, observed_at=ctx.now,
                           payload=payload, figures=figures, entities=self.entities,
                           provenance={"source": "real", "provider": self.provider,
                                       "record": self.record_what})

    def _tally(self, ctx, rec: dict):
        cands = list(rec["candidates"].values())
        counts = rec["counts"]

        def n(*states) -> int:
            return sum(1 for c in cands if c.get("state") in states)

        figures = [Figure(int(counts.get("proposals") or 0), "count",
                          "self-improvements proposed to the owner", window="all_time"),
                   Figure(int(counts.get("applied") or 0), "count",
                          "self-improvements applied", window="all_time"),
                   Figure(n(*store.OPEN), "count", "self-improvements in progress", window="now"),
                   Figure(1 if rec.get("active") else 0, "count", "Daedalus jobs running",
                          window="now")]
        night = self.window.current(ctx.now)
        return make_output(self, kind="improve.tally", valid_at=ctx.now, observed_at=ctx.now,
                           payload={"window": self.window.describe(),
                                    "window_open": night is not None,
                                    "per_night": self.per_night, "amber": rec.get("amber"),
                                    "open": [{"id": c["id"], "state": c.get("state"),
                                              "worker": c["evidence"]["worker"],
                                              "waiting": _clip(c.get("waiting") or "", 160)}
                                             for c in cands if c.get("state") in store.OPEN][:4],
                                    "recent": [{"id": c["id"], "state": c.get("state"),
                                                "why": _clip(c.get("why") or "", 160)}
                                               for c in sorted(cands, key=lambda q: float(
                                                   q.get("created_at") or 0))[-5:]]},
                           figures=figures, entities=self.entities,
                           provenance={"source": "real", "provider": self.provider,
                                       "record": self.record_what})

