"""``builds.daedalus``: Daedalus builds one small product a night; Claude reviews every one.

The owner's product line is small paid developer tools ($9-19 on Gumroad). This worker turns
the product backlog (backlog.py) into staged products, one at a time:

1. **Pick** (by day, from the backlog; Moss's division goal can put a product first, and
   failing that the highest measured demand in products.demand's ``demand.json`` does) and,
   inside the overnight window only (window.py, default 01:00-07:00), create the product's
   OWN fresh sandbox repo (sandbox.py, ``<builds_sandbox>\\<slug>``) and ask Pionir to run
   ``coding.daedalus_build`` in it - unparked only through the crew client's one grant
   (``daedalus.build_sandbox``, scoped in pionir/auth.py to that capability): Pionir's
   adapter refuses any repo that is not a sandbox repo this worker made, rewrites its git
   config first, and runs the job on a contained Daedalus of its own. Pionir takes its exclusive GPU lease for the job (Moss's model steps aside),
   so: at most ONE job at a time, never one that cannot finish before the window ends
   (``budget_minutes``, default 75, per slice; its ``not_after`` is the window's end at the latest, and
   Pionir cancels it there), at most one NEW product per night. The job is submitted with
   ``follow=0`` and followed on later runs through ``ctx.task``, so the one hands thread is
   never held for most of an hour.
2. **Settle** the job: Daedalus's gate passed and a commit landed in the sandbox -> BUILT.
   Its gate failed, it ran out of time, or it was lost -> one REPAIR attempt with the reason,
   then SHELVED. Daedalus down, the circuit open, the GPU busy -> WAITING (not an attempt;
   tried again later in the window, never counted as a failure).
3. **Review** every build (review.py): our own checks first (the tests run by us, no network,
   no secret or owner data, the licence) - then Claude, through the crew's no-tools review
   path on the daily Claude cap. Only an approval with every check true is staged. A
   rejection gets ONE Daedalus repair with the review's reasons; a second rejection is
   SHELVED with a Discord note. Claude's budget spent -> the build waits (never approved by
   default); Claude failing ``REVIEW_ATTEMPTS`` times -> SHELVED with a note.
4. **Stage** an approved build (package.py) as ``<products_dir>/<slug>/`` in exactly the
   shelf's format. From there products.shelf submits it for the owner's approval like any
   product (the daily digest, new listings only): nothing is ever sold without his yes.
5. **Tell the owner** on Discord (``builds.card``): a nightly report once each window ends
   (what was built, the review's verdict, where it is staged or why it was shelved), a note
   for each staged or shelved product, and the backlog card, which - like the nightly
   report - takes his replies (``add``/``remove``/``top``; only his count).

**Slices.** A product is built in ``slices`` jobs (default 2: the core and its tests, then the
command line, README and the rest). Each landed slice is a commit that survives whatever the
next job does (a failed or timed-out job's own work is discarded by Daedalus's gate), the slice
index lives in the product's record, and only the last slice is reviewed. A Python build does
not start at all while the sandbox's interpreter cannot import pytest: Daedalus's G2 runs it,
and without it every build burned its whole budget and failed at the gate.

**Contained** (pionir/build_sandbox.py): nothing here runs as the owner. Daedalus builds on a
second instance Pionir starts as the ``pionir-builds`` user for each job; our own test run of
the generated code runs as that user too, with its firewalled interpreter, in a job object.
Until ``tools\\setup-build-sandbox.ps1`` has been run the worker says "not configured" and
runs nothing at all.

Everything the worker does is in its record (``builds.daedalus.json``) and reported to the
Builds leader as rows (``build.*``). No model of the crew's writes anything here.
"""
from __future__ import annotations

import os
import re
import secrets
import subprocess
from datetime import datetime
from pathlib import Path

from pionir import build_sandbox
from pionir.adapters.deliveries import DeliveryProblem
from pionir.auth import compat_from_environment

from .. import demand
from ..blog import _clip, _Unreadable, read_record, record_path, save_record
from ..delivery import _PASSING, PASSING_TYPES
from ..figures import Figure
from ..fiverr import checks
from ..hands import Job
from ..log import log
from ..orders import _NOT_SET_UP
from ..result import Err, Ok, Result
from ..worker import ErrorKind, WorkContext, make_output, never_raises
from .review import SUITE_RUNNER
from ..workers import _Base
from . import backlog as bl
from . import package, review, sandbox
from .window import Window, can_start, not_after

BUILD = "coding.daedalus_build"
CANCEL = "coding.daedalus_build_cancel"
PERMISSION = "daedalus.build_sandbox"
CARD = "builds.card"
INBOX = "builds.inbox"
STATES = ("queued", "building", "built", "repair", "staged", "shelved")
IN_PROGRESS = ("queued", "building", "built", "repair")
COUNTED = ("built", "gate_failed", "timed_out", "lost", "failed")   # a real attempt
REVIEW_ATTEMPTS = 3                  # Claude asked and failed this often -> shelved
REVIEW_TIMEOUT = 600.0
REVIEW_RETRY = 1800.0                # seconds before a waiting review is tried again
RETRY_AFTER = 900.0                  # seconds before a start that could not happen is retried
LOST_AFTER = 1200.0                  # past not_after with no outcome: the job is lost
MAX_UNPOSTED = 20
KEEP_NIGHTS = 30
KEEP_REPLIES = 500


NAMESPACE = "namespace"               # _probe_module: only an empty namespace package was found
_NAMESPACE_EXIT = 3


def _probe_module(python, module: str) -> bool | str:
    """True when the sandbox's own interpreter imports ``module`` from a real file. Isolated
    mode, short timeout, nothing else run; an interpreter that cannot even start counts as
    missing (False). A module with no ``__file__`` is an empty NAMESPACE package - what Python
    makes of a package folder whose files it may not read (2026-10-04: pip's staging ACL left
    site-packages readable by administrators only) - and is ``NAMESPACE``, never a pass."""
    code = (f"import sys, {module} as m; "
            f"sys.exit(0 if getattr(m, '__file__', None) else {_NAMESPACE_EXIT})")
    try:
        done = subprocess.run([str(python), "-I", "-c", code],
                              capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    if done.returncode == _NAMESPACE_EXIT:
        return NAMESPACE
    return done.returncode == 0


def _slice_lead(slice_no: int, total: int, minutes: int) -> str:
    """The pacing paragraph at the head of a build: how long this job has and, when a
    product is built in slices, which slice it is."""
    pace = (f"You have about {minutes} minutes for this job, and it is stopped at that point: "
            "write a test, run it, move on, and do not re-read files you already read.")
    if total <= 1:
        return pace
    if slice_no < total:
        return (f"SLICE {slice_no} OF {total} - the core only. {pace} Write the importable "
                "package (all the real logic) and its unit tests and get every test passing. "
                "Do NOT write the command-line entry point, README.md or CHANGELOG.md yet: "
                "the next slice does. Stop as soon as the tests pass.")
    return (f"SLICE {slice_no} OF {total} - finish it. {pace} The earlier slice (the core and "
            "its tests) is already committed: read it, do not rewrite it. Add the "
            "command-line entry point, README.md, CHANGELOG.md and every acceptance test the "
            "core does not cover yet. Keep all tests passing.")


def _where_it_stopped(error) -> str:
    """The "how far it got" sentence the adapter put in a timeout, for the report."""
    text = str(error or "")
    for lead in ("After the cancel: ", "At the last look (it had not stopped yet): "):
        at = text.find(lead)
        if at >= 0:
            tail = text[at + len(lead):].split(" The outcome", 1)[0].strip().rstrip(".")
            return f" ({lead[:-2].lower()}: {tail})" if tail else ""
    return ""


def _hm(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%H:%M")


class BuildsWorker(_Base):
    """``builds.daedalus``: the overnight product builds."""

    record_what = "the Builds worker's own record of every product it built"

    def __init__(self, spec, *, window: str = "01:00-07:00", budget_minutes: int = 75,
                 max_attempts: int = 2, slices: int = 2, ssh_dir: str | None = "~/.ssh", test_timeout_seconds: float = review.TEST_TIMEOUT,
                 review_timeout_seconds: float = REVIEW_TIMEOUT) -> None:
        super().__init__(spec)
        self.window = Window.parse(window)
        if not 10 <= int(budget_minutes) <= 180:
            raise ValueError("budget_minutes must be 10 to 180")
        self.budget = int(budget_minutes) * 60
        if not 1 <= int(max_attempts) <= 3:
            raise ValueError("max_attempts must be 1 to 3 (a build and its repairs)")
        self.max_attempts = int(max_attempts)
        if not 1 <= int(slices) <= 2:
            raise ValueError("slices must be 1 or 2 (the core, then the rest)")
        self.slices = int(slices)
        self.ssh_dir = ssh_dir
        self.test_timeout = float(test_timeout_seconds)
        self.review_timeout = float(review_timeout_seconds)
        # the outside world, injected (tests replace these)
        self.run_tests = review.run_tests
        self.probe_module = _probe_module       # (python, module) -> bool, injectable
        self.git_run = None                 # None: subprocess.run
        # the contained sandbox user, as the setup script left it (None: not configured)
        self.load_sandbox = lambda root: build_sandbox.load_setup(root)
        # loopback is open to the sandbox user (owner decision, 2026-09-28): while auth compat
        # is on, a caller with no token is still served by Pionir and the crew API, so night
        # builds are not armed at all - nothing is built, and no generated code is run
        self.auth_compat = compat_from_environment
        self._setup = None
        self.load_guard = lambda secrets_dir: checks.load_guard(
            secrets_dir, self.ssh_dir, checks.owner_markers())
        self._log_lines: list = []          # this run's lines for the night's report
        self._tried: set = set()            # the card keys tried this run

    def readiness(self, secrets_dir) -> str | None:
        """Why the worker cannot build yet: the contained sandbox user is not set up."""
        root = os.environ.get("PIONIR_DAEDALUS_SANDBOX", "").strip() or \
            build_sandbox.default_sandbox_root()
        _setup, why = self.load_sandbox(root)
        return why or self._compat_refusal()

    def _compat_refusal(self) -> str | None:
        if self.auth_compat():
            return ("not armed: PIONIR_AUTH_COMPAT is on, so a process with no token (generated "
                    "code on loopback included) is still served; set PIONIR_AUTH_COMPAT=off "
                    "once every client sends its token")
        return None

    # ---- the record --------------------------------------------------------------------
    @staticmethod
    def _blank() -> dict:
        return {"products": {}, "active": None, "nights": {}, "replies_seen": [],
                "reply_notes": [], "backlog_card": None, "unposted": {},
                "blocked_until": 0.0, "counts": {}}

    def load(self, state_dir) -> dict:
        doc = read_record(state_dir, self.worker_id)
        return self._blank() if doc is None else {**self._blank(), **doc}

    def _save(self, ctx: WorkContext, rec: dict) -> None:
        rec["replies_seen"] = rec["replies_seen"][-KEEP_REPLIES:]
        for key in sorted(rec["nights"])[:-KEEP_NIGHTS]:
            rec["nights"].pop(key, None)
        save_record(record_path(ctx.state_dir, self.worker_id), rec)

    def _git(self) -> dict:
        return {} if self.git_run is None else {"run": self.git_run}

    # ---- one run -------------------------------------------------------------------------
    @never_raises()
    def run(self, ctx: WorkContext) -> Result:
        missing = [n for n, v in (("state_dir", ctx.state_dir), ("builds_dir", ctx.builds_dir),
                                  ("builds_sandbox", ctx.builds_sandbox),
                                  ("products_dir", ctx.products_dir)) if v is None]
        if missing:
            return self._err(ErrorKind.NOT_CONFIGURED, f"no {', '.join(missing)}: the Builds "
                             "worker cannot keep its record, its backlog, its sandbox or the "
                             "shelf", retryable=False)
        if ctx.job is None:
            return self._err(ErrorKind.NOT_CONFIGURED, "no hands: builds run only through "
                             "Pionir", retryable=False)
        setup, why = self.load_sandbox(ctx.builds_sandbox)
        if setup is None:
            # the owner's rule: without the contained user, NOTHING runs - no repo is made,
            # no job is asked for, no generated code is run
            return self._err(ErrorKind.NOT_CONFIGURED, why or build_sandbox.SETUP_HINT,
                             retryable=False)
        compat = self._compat_refusal()
        if compat:
            return self._err(ErrorKind.NOT_CONFIGURED, compat, retryable=False)
        self._setup = setup
        try:
            rec = self.load(ctx.state_dir)
        except _Unreadable as exc:
            return self._err(ErrorKind.MALFORMED, f"its record is unreadable ({exc}); refusing "
                             "to act, since it could start a second job", retryable=False)
        events: list = []
        self._tried = set()
        try:
            doc = bl.load(ctx.builds_dir)
        except bl.BacklogUnreadable as exc:
            doc = None
            events.append(self._event(ctx, "build.backlog_unreadable",
                                      {"why": _clip(exc, 200)}))
        night = self.window.current(ctx.now)
        if night is not None:
            rec["nights"].setdefault(night.key, {"start": night.start, "end": night.end,
                                                 "started": None, "log": [],
                                                 "reported": False})
        if doc is not None:
            self._replies(ctx, rec, doc, events)
        self._recover_orphans(ctx, rec, events)
        self._follow(ctx, rec, events)
        self._review_one(ctx, rec, events)
        self._maybe_start(ctx, rec, doc, events)
        self._save(ctx, rec)
        self._nightly(ctx, rec, doc, events)
        if doc is not None:
            self._backlog_card(ctx, rec, doc)
        self._retry_cards(ctx, rec)
        self._save(ctx, rec)
        return Ok((*events, self._tally(ctx, rec, doc, night)))

    # ---- the owner's replies -------------------------------------------------------------
    def _replies(self, ctx: WorkContext, rec: dict, doc: dict, events: list) -> None:
        out = ctx.job(Job(INBOX, {}, what="read the owner's replies to the Builds cards"))
        result = out.result if isinstance(out.result, dict) else {}
        if out.status != "done" or result.get("ok") is False \
                or not isinstance(result.get("replies"), list):
            return                      # not set up or not answering: read again next run
        seen = set(rec["replies_seen"])
        changed = False
        for r in sorted((r for r in result["replies"] if isinstance(r, dict)
                         and isinstance(r.get("reply_id"), str)),
                        key=lambda r: (len(r["reply_id"]), r["reply_id"])):
            if r["reply_id"] in seen:
                continue
            seen.add(r["reply_id"])
            rec["replies_seen"].append(r["reply_id"])
            rec["counts"]["replies"] = int(rec["counts"].get("replies") or 0) + 1
            did, note = bl.apply_reply(doc, str(r.get("text") or "")[:6000],
                                       set(rec["products"]))
            changed = changed or did
            rec["reply_notes"].append(_clip(note, 300))
            events.append(self._event(ctx, "build.backlog_reply", {"applied": did,
                                                                  "note": _clip(note, 200)}))
        if changed:
            bl.save(ctx.builds_dir, doc)

    # ---- following the job in flight -------------------------------------------------------
    def _follow(self, ctx: WorkContext, rec: dict, events: list) -> None:
        act = rec.get("active")
        if not act:
            return
        p = rec["products"].get(act.get("slug"))
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
        self._settle(ctx, rec, p, out, events)

    def _settle(self, ctx: WorkContext, rec: dict, p: dict, out, events: list) -> None:
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
            a.update(outcome="timed_out", why=_clip(out.error, 600))
            self._failed_attempt(ctx, rec, p, f"it did not finish in its "
                                 f"{self.budget // 60}-minute budget and was stopped"
                                 f"{_where_it_stopped(out.error)}", events)
        elif out.status == "failed" and (out.error_type in PASSING_TYPES or (
                not out.error_type and _PASSING.search(out.error or ""))):
            # lost touch WHILE Daedalus ran it: it may still be running and commit, so wait
            # out its deadline before sending the job again (a commit is found meanwhile)
            running = "while running job" in (out.error or "")
            if not rec.get("daedalus_down_since"):
                rec["daedalus_down_since"] = ctx.now
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
            self._card(ctx, rec, f"builds:parked:{p['slug']}:{a['n']}", "problem",
                       f"The build of {p['slug']} was parked for approval",
                       "Pionir parked coding.daedalus_build instead of running it with its "
                       "sandbox grant. Nothing was built. Deny that approval (it would be "
                       "refused anyway once its window has passed) and check that Pionir "
                       "runs this version of the Daedalus adapter.")
        elif out.status == "unreachable":
            # we do not know that it did not start: it is cancelled by its id before anything
            # else is sent (``_orphan``), and until then it is still the one job in flight
            rec["active"] = {"slug": p["slug"], "task_id": None, "build_id": a.get("build_id"),
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
        self._night_log(ctx, f"{p['slug']}: waiting - {_clip(why, 120)}")
        events.append(self._event(ctx, "build.waiting", {"slug": p["slug"],
                                                         "why": _clip(why, 200)}))

    def _landed(self, ctx, rec, p, a, output, events) -> None:
        """Daedalus says its gate passed: the commit must be in the sandbox's own branch."""
        prior = p.get("slice_head") or p["base"]
        on_branch = False
        try:
            head = sandbox.head(p["repo"], **self._git())
            commit = str(output.get("commit") or "")
            if head == prior and commit:
                on_branch = True
                # Daedalus left it on a branch of its own: take that commit by id. The
                # owner's side never checks out or merges a build repo.
                head = sandbox.built_commit(p["repo"], p["base"], commit, **self._git())
        except sandbox.SandboxError as exc:
            a.update(outcome="failed", why=_clip(exc, 300))
            self._failed_attempt(ctx, rec, p, f"its commit could not be found in the sandbox "
                                 f"({_clip(exc, 200)})", events)
            return
        if head == prior or head == p.get("reviewed_head"):
            a.update(outcome="gate_failed", why="Daedalus reported a pass but committed nothing")
            self._failed_attempt(ctx, rec, p, "Daedalus reported a pass but committed nothing "
                                 "new", events)
            return
        a.update(outcome="built", commit=head, job_id=output.get("job_id"),
                 branch=output.get("branch"), files=list(output.get("files") or [])[:40])
        slice_no, total = int(p.get("slice") or 1), int(p.get("slices") or 1)
        if slice_no < total and not on_branch:
            # the core is committed on the branch the next slice starts from: kept whatever
            # happens next, and reviewed only once the whole product is there
            p.update(state="queued", head=head, slice_head=head, slice=slice_no + 1,
                     waiting=None, retry_after=0.0, review_failures=0, repair_reasons=[])
            self._night_log(ctx, f"{p['slug']}: slice {slice_no} of {total} landed "
                                 f"({head[:10]}), starting the next")
            events.append(self._event(ctx, "build.slice_landed", {
                "slug": p["slug"], "slice": slice_no, "of": total, "commit": head[:12],
                "files": len(a["files"])}))
            return
        p.update(state="built", head=head, waiting=None, review_failures=0)
        self._night_log(ctx, f"{p['slug']}: built ({head[:10]}), waiting for review")
        events.append(self._event(ctx, "build.built", {
            "slug": p["slug"], "attempt": a["n"], "commit": head[:12],
            "files": len(a["files"])}))

    def _lost(self, ctx, rec, p, events, why) -> None:
        a = p["attempts"][-1]
        rec["active"] = None
        a["finished_at"] = ctx.now
        try:
            head = sandbox.head(p["repo"], **self._git())
        except sandbox.SandboxError:
            head = p["base"]
        if head not in (p["base"], p.get("reviewed_head")):
            # the outcome never reached us, but a commit landed: it is reviewed like any build
            self._landed(ctx, rec, p, a, {"ok": True, "commit": head}, events)
            a["note"] = f"Pionir lost track of the job ({why}), but a commit landed"
        else:
            a.update(outcome="lost", why=_clip(why, 300))
            self._failed_attempt(ctx, rec, p, f"the job was lost: {why}", events)
        self._save(ctx, rec)

    def _counted(self, p: dict) -> int:
        """Real attempts at the slice in hand: a slice that landed does not use up the
        retries of the next one."""
        here = int(p.get("slice") or 1)
        return sum(1 for a in p["attempts"]
                   if a.get("outcome") in COUNTED and int(a.get("slice") or 1) == here)

    def _failed_attempt(self, ctx, rec, p, why: str, events: list) -> None:
        if self._counted(p) < self.max_attempts:
            reviewed = bool(p.get("reviewed_head"))      # a review already rejected a version
            p.update(state="repair", waiting=None, retry_after=0.0,
                     repair_from="review" if reviewed else "attempt",
                     repair_reasons=[_clip(why, 400)] + (
                         list(p.get("repair_reasons") or [])[:11] if reviewed else []))
            self._night_log(ctx, f"{p['slug']}: attempt failed - {_clip(why, 120)}")
            events.append(self._event(ctx, "build.attempt_failed", {
                "slug": p["slug"], "why": _clip(why, 200), "repair_next": True}))
        else:
            self._shelve(ctx, rec, p, why, events)

    # ---- reviewing a build -----------------------------------------------------------------
    def _review_one(self, ctx: WorkContext, rec: dict, events: list) -> None:
        p = next((p for p in rec["products"].values() if p.get("state") == "built"
                  and float(p.get("review_after") or 0) <= ctx.now), None)
        if p is None:
            return
        entry = p["entry"]
        try:
            tree = sandbox.export(p["repo"], p["head"], **self._git())
        except sandbox.SandboxError as exc:
            self._shelve(ctx, rec, p, f"its sandbox repo could not be read ({_clip(exc, 200)})",
                         events)
            return
        try:
            guard = self.load_guard(ctx.secrets_dir)
        except (DeliveryProblem, OSError) as exc:
            p.update(waiting=f"the secrets check could not run ({_clip(exc, 160)})",
                     review_after=ctx.now + REVIEW_RETRY)
            events.append(self._event(ctx, "build.checks_unavailable",
                                      {"slug": p["slug"], "why": _clip(exc, 160)}))
            return
        year = datetime.fromtimestamp(float(p.get("created_at") or ctx.now)).year
        # every check that needs nothing run comes FIRST: code that fails them never runs
        reasons = review.static_problems(entry, tree.files, tree.problems, guard, year=year)
        if reasons:
            p["reviews"].append({"at": ctx.now, "head": p["head"], "tests_ran": None,
                                 "tests_passed": None, "by": "our checks", "approved": False,
                                 "reasons": [_clip(r, 300) for r in reasons[:12]]})
            self._rejected(ctx, rec, p, reasons, events, by="our checks")
            return
        # then the product's tests, contained: as pionir-builds, in a job object
        suite = self.run_tests(tree.files, entry["language"], setup=self._setup,
                               timeout=self.test_timeout)
        base = {"at": ctx.now, "head": p["head"], "tests_ran": suite.ran,
                "tests_passed": suite.passed}
        reasons = review.suite_problems(suite)
        if reasons:
            p["reviews"].append({**base, "by": "our checks", "approved": False,
                                 "reasons": [_clip(r, 300) for r in reasons[:12]]})
            self._rejected(ctx, rec, p, reasons, events, by="our checks")
            return
        prompt, why = review.review_prompt(entry, tree.files, suite, self._changed(p))
        if prompt is None:
            reasons = [why]
            p["reviews"].append({**base, "by": "our checks", "approved": False,
                                 "reasons": reasons})
            self._rejected(ctx, rec, p, reasons, events, by="our checks")
            return
        last = (p.get("reviews") or [None])[-1]
        if last and last.get("by") == "Claude" and last.get("approved") is True \
                and last.get("head") == p["head"]:
            # Claude already approved exactly this commit (staging was interrupted)
            self._stage(ctx, rec, p, tree.files, guard, events)
            return
        if ctx.review is None:
            p.update(waiting="no Claude review path is configured; nothing is staged without "
                     "Claude's review", review_after=ctx.now + REVIEW_RETRY)
            return
        got = ctx.review(prompt, self.review_timeout)
        if isinstance(got, Err):
            refusal = got.error
            if getattr(refusal, "waits", False):
                p.update(waiting=f"waiting for Claude's review ({_clip(refusal, 160)})",
                         review_after=ctx.now + REVIEW_RETRY)
                events.append(self._event(ctx, "build.waiting_for_claude",
                                          {"slug": p["slug"], "why": _clip(refusal, 160)}))
                return
            self._review_failed(ctx, rec, p, f"Claude did not answer: {_clip(refusal, 200)}",
                                base, events)
            return
        verdict = review.parse_verdict(str(got.value))
        if verdict is None:
            self._review_failed(ctx, rec, p, "Claude's answer was not a readable verdict",
                                base, events)
            return
        p["reviews"].append({**base, "by": "Claude", "approved": verdict.approved,
                             "reasons": verdict.reasons, "checks": verdict.checks})
        self._save(ctx, rec)                    # a verdict is never asked for twice
        if verdict.approved:
            self._stage(ctx, rec, p, tree.files, guard, events)
        else:
            self._rejected(ctx, rec, p, verdict.reasons, events, by="Claude")

    def _changed(self, p: dict) -> list:
        try:
            return sandbox.changed_files(p["repo"], p["base"], p["head"], **self._git())
        except sandbox.SandboxError:
            return []

    def _review_failed(self, ctx, rec, p, why, base, events) -> None:
        p["review_failures"] = int(p.get("review_failures") or 0) + 1
        p["reviews"].append({**base, "by": "Claude", "approved": False, "failed": True,
                             "reasons": [_clip(why, 300)]})
        events.append(self._event(ctx, "build.review_failed", {
            "slug": p["slug"], "why": _clip(why, 200), "failures": p["review_failures"]}))
        if p["review_failures"] >= REVIEW_ATTEMPTS:
            self._shelve(ctx, rec, p, f"Claude's review could not be completed "
                         f"({p['review_failures']} tries; the last: {why})", events)
        else:
            p.update(waiting=_clip(why, 200), review_after=ctx.now + REVIEW_RETRY)

    def _rejected(self, ctx, rec, p, reasons: list, events: list, *, by: str) -> None:
        p["reviewed_head"] = p.get("head")
        text = "; ".join(_clip(r, 200) for r in reasons[:6])
        events.append(self._event(ctx, "build.rejected", {
            "slug": p["slug"], "by": by, "reasons": [_clip(r, 160) for r in reasons[:5]]}))
        self._night_log(ctx, f"{p['slug']}: REJECTED by {by} - {_clip(text, 160)}")
        if self._counted(p) < self.max_attempts:
            p.update(state="repair", repair_reasons=[_clip(r, 400) for r in reasons[:12]],
                     waiting=None, retry_after=0.0, repair_from="review")
        else:
            self._shelve(ctx, rec, p, f"rejected by {by} after its repair: {text}", events)
        self._save(ctx, rec)

    def _stage(self, ctx, rec, p, files: dict, guard, events: list) -> None:
        """ONLY for a build Claude approved (``_review_one``)."""
        try:
            staged = package.stage(p["entry"], files, ctx.products_dir, guard.secrets)
        except Exception as exc:  # noqa: BLE001 - shelved with the reason, never re-reviewed
            self._shelve(ctx, rec, p, f"approved, but it could not be staged: "
                         f"{type(exc).__name__}: {_clip(exc, 240)}", events)
            return
        p.update(state="staged", staged=staged, waiting=None, staged_at=ctx.now)
        self._night_log(ctx, f"{p['slug']}: APPROVED by Claude and staged")
        events.append(self._event(ctx, "build.staged", {
            "slug": p["slug"], "name": p["entry"]["name"],
            "price_cents": p["entry"]["price_cents"], "folder": staged["folder"]},
            figures=[Figure(p["entry"]["price_cents"], "usd_cents", "listing price",
                            stream=p["slug"], window="now")]))
        entry = p["entry"]
        self._card(ctx, rec, f"builds:staged:{p['slug']}", "staged",
                   f"{entry['name']}"[:120],
                   f"**{p['slug']}** - ${entry['price_cents'] / 100:.2f}\n"
                   f"{entry['summary']}\n\n"
                   f"Built by Daedalus ({self._counted(p)} attempt(s)), tests run and passed "
                   f"({p['reviews'][-1].get('tests_ran')} tests), and APPROVED by Claude's "
                   f"review.\nStaged at `{staged['folder']}` ({staged['files']} files, "
                   f"{staged['zip_bytes']:,}-byte zip, cover.png, listing.json).\n\n"
                   "The product shelf will submit it for your approval (the daily digest). "
                   "Nothing is on sale until you approve it.")
        self._save(ctx, rec)

    def _shelve(self, ctx, rec, p, why: str, events: list) -> None:
        p.update(state="shelved", shelved_why=_clip(why, 500), waiting=None,
                 shelved_at=ctx.now)
        if rec.get("active") and rec["active"].get("slug") == p["slug"]:
            rec["active"] = None
        self._night_log(ctx, f"{p['slug']}: SHELVED - {_clip(why, 160)}")
        events.append(self._event(ctx, "build.shelved", {"slug": p["slug"],
                                                         "why": _clip(why, 200)}))
        last = next((r for r in reversed(p.get("reviews") or [])), None)
        lines = [f"**{p['slug']}** - {p['entry']['name']}", "", f"Why: {why}"]
        if last and last.get("reasons"):
            lines += ["", f"Last review ({last.get('by')}):"]
            lines += [f"- {_clip(r, 240)}" for r in last["reasons"][:8]]
        lines += ["", f"Attempts: {self._counted(p)}. The sandbox repo stays at "
                      f"`{p.get('repo') or '(none)'}` for a look.",
                  "Nothing was staged or put on sale. The worker moves on to the next "
                  "product in the backlog."]
        self._card(ctx, rec, f"builds:shelved:{p['slug']}", "shelved",
                   f"Shelved: {p['entry']['name']}"[:120], "\n".join(lines))
        self._save(ctx, rec)

    # ---- starting a job --------------------------------------------------------------------
    def _maybe_start(self, ctx: WorkContext, rec: dict, doc, events: list) -> None:
        if rec.get("active"):
            return                                          # one job at a time
        night = self.window.current(ctx.now)
        if not can_start(night, ctx.now, self.budget):
            return                                          # overnight only, and must fit
        if float(rec.get("blocked_until") or 0) > ctx.now:
            return
        products = rec["products"]
        p = next((p for p in products.values() if p.get("state") in ("repair", "queued")),
                 None)
        if p is not None and float(p.get("retry_after") or 0) > ctx.now:
            return
        if p is None:
            if any(q.get("state") in ("building", "built") for q in products.values()):
                return                                      # still in flight or in review
            if rec["nights"][night.key].get("started"):
                return                                      # one new product a night
            if doc is None:
                return
            entry = bl.choose(doc["products"], set(products), ctx.goal,
                              demand.build_preference(ctx.state_dir, ctx.now))
            if entry is None:
                return
            if not self._tools_ready(ctx, rec, night, entry, events):
                return                                      # before a repo is even made
            p = self._new_product(ctx, rec, entry, night, events)
            if p is None:
                return
        elif not self._tools_ready(ctx, rec, night, p["entry"], events, p):
            return
        if self._landed_meanwhile(ctx, rec, p, events):
            return
        self._submit(ctx, rec, p, night, events)

    def _tools_ready(self, ctx, rec, night, entry: dict, events: list, p: dict | None = None) -> bool:
        """A Python build is gated on its tests (Daedalus's G2 runs pytest): with no pytest in
        the sandbox's interpreter every such build fails at the gate after burning its whole
        budget (both nights of 2026-10-02/03). So it does not start - the owner is told once a
        night, with the fix, and nothing is spent."""
        if entry.get("language") != "python" or self._setup is None:
            return True
        got = self.probe_module(self._setup.python, "pytest")
        if got is True:
            return True
        if got == NAMESPACE:
            site = Path(self._setup.python).parent / "Lib" / "site-packages"
            why = ("the build sandbox's Python finds pytest only as an empty namespace package: "
                   f"its files in {site} are unreadable to {build_sandbox.USER} (the "
                   "build Daedalus cannot import fastapi either); in an administrator "
                   f"PowerShell run: icacls \"{site}\" /reset /T /C /Q - or run "
                   "tools" + chr(92) + "setup-build-sandbox.ps1 again as administrator (it "
                   "resets them) - nothing was started")
        else:
            why = ("the build sandbox's Python has no pytest, and Daedalus's gate runs every "
                   "build's tests with it; run tools" + chr(92) + "setup-build-sandbox.ps1 again as "
                   "administrator (it installs the pinned pytest) - nothing was started")
        if p is not None:
            p.update(waiting=_clip(why, 300))
        day = rec["nights"][night.key]
        if not day.get("tools_warned"):
            day["tools_warned"] = True
            self._night_log(ctx, f"waiting - {_clip(why, 140)}")
            events.append(self._event(ctx, "build.waiting", {"slug": entry.get("slug"),
                                                             "why": _clip(why, 200)}))
            self._card(ctx, rec, f"builds:tools:{night.key}", "problem",
                       "Tonight's build did not start: pytest is "
                       + ("unreadable" if got == NAMESPACE else "missing"),
                       f"**{entry.get('slug')}** was next in the backlog, but {why}.")
        return False

    def _recover_orphans(self, ctx, rec, events) -> None:
        """A product left ``building`` with no job in flight (a crash between two saves, an
        old record) is never left stuck: its last attempt becomes the job in flight again if
        it has an id, else it is followed as an orphan until it is cancelled or lost."""
        act = rec.get("active")
        for p in rec["products"].values():
            if p.get("state") != "building" or (act and act.get("slug") == p["slug"]):
                continue
            a = (p.get("attempts") or [{}])[-1]
            rec["active"] = act = {"slug": p["slug"], "task_id": a.get("task_id"),
                                   "build_id": a.get("build_id"), "attempt": a.get("n"),
                                   "not_after": float(a.get("not_after") or ctx.now)}
            log.warning("%s: %s was left building with no job in flight; following it",
                        self.worker_id, p["slug"])
            break

    def _orphan(self, ctx, rec, p, act, events) -> None:
        """A job Pionir may have started but never told us about: cancel it by its build id
        (it stops if it runs; it never starts if it waits for the GPU) before anything else
        is sent. Until that is confirmed it stays the one job in flight; past its deadline
        with no confirmation it is lost (a commit it landed is still found and reviewed)."""
        build_id = act.get("build_id")
        if build_id:
            out = ctx.job(Job(CANCEL, {"build_id": build_id},
                              what=f"cancel the unconfirmed build of {p['slug']}"))
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

    def _new_product(self, ctx, rec, entry: dict, night, events: list) -> dict | None:
        slug = entry["slug"]
        p = {"slug": slug, "entry": dict(entry), "state": "queued", "night": night.key,
             "created_at": ctx.now, "repo": None, "base": None, "attempts": [],
             "reviews": [], "review_failures": 0, "repair_reasons": [], "waiting": None,
             "slice": 1, "slices": self.slices, "slice_head": None}
        rec["products"][slug] = p
        rec["nights"][night.key]["started"] = slug
        try:
            p["base"] = sandbox.create(ctx.builds_sandbox, entry,
                                       year=datetime.fromtimestamp(ctx.now).year,
                                       created_at=ctx.now, **self._git())
            p["repo"] = str(Path(ctx.builds_sandbox) / slug)
        except (sandbox.SandboxError, OSError) as exc:
            self._shelve(ctx, rec, p, f"its sandbox repo could not be created "
                         f"({_clip(exc, 200)})", events)
            return None
        self._night_log(ctx, f"{slug}: sandbox created, starting")
        self._save(ctx, rec)
        return p

    def _landed_meanwhile(self, ctx, rec, p, events) -> bool:
        """Before a retry: a job whose outcome never reached us may still have committed."""
        if not p.get("attempts") or not p.get("repo"):
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

    def _verify(self, entry: dict) -> str:
        if entry["language"] == "python":
            # Daedalus runs a string command through PowerShell on this machine. It is the
            # review's OWN suite code, isolated (-I -S, only src/ on the path): a softer command
            # once passed `from src.pkg import` in the build and was rejected by the review.
            return f'python -I -S -c "{SUITE_RUNNER}"'
        return "node --test"

    def _intent(self, p: dict, kind: str) -> str:
        e = p["entry"]
        verify = self._verify(e)
        reasons_list = p.get("repair_reasons") or []
        if kind == "repair" and p.get("repair_from", "review") == "review":
            reasons = "\n".join(f"- {r}" for r in reasons_list)
            text = (f"This repository ({p['repo']}) holds {e['name']}; BRIEF.md is its "
                    "specification. A review REJECTED the current version. Fix EVERY problem "
                    "below, keep everything that already works, and keep all tests passing.\n\n"
                    f"Problems to fix:\n{reasons}\n\nRules:\n{sandbox.RULES}\n"
                    f"All tests must pass with: {verify}")
        else:
            features = "\n".join(f"- {f}" for f in e["features"])
            tests = "\n".join(f"- {t}" for t in e["acceptance"])
            slice_no, total = int(p.get("slice") or 1), int(p.get("slices") or 1)
            lead = _slice_lead(slice_no, total, self.budget // 60)
            if kind == "repair" and reasons_list:
                lead += (" The previous attempt at this did not finish (" + reasons_list[0]
                         + "). Nothing from it was kept: work in small steps, write a test, "
                         "run it, and get the tests passing early.")
            holds = ("holds only a seed commit" if slice_no == 1
                     else "already holds the first slice, committed")
            text = (f"{lead}\n\nBuild a complete, sellable product in this repository "
                    f"({p['repo']}). It {holds}; BRIEF.md is the full specification - read "
                    f"it first.\n\nProduct: {e['name']}\n{e['summary']}\n\n{e['brief']}\n\n"
                    f"Features the listing promises (each must be true):\n{features}\n\n"
                    f"Acceptance tests (write each as a real test):\n{tests}\n\n"
                    f"Honest limits (the README says these): {e['limits']}\n\n"
                    f"Layout:\n{sandbox.layout(e)}\n\nRules:\n{sandbox.RULES}\n"
                    f"When you finish, all tests must pass with: {verify}")
        if len(text) > 7800:
            text = (f"Work in this repository ({p['repo']}) on {e['name']}. BRIEF.md is the full "
                    "specification: " + ("fix every problem listed below" if kind == "repair"
                                         else "build everything it asks for")
                    + f".\n\nRules:\n{sandbox.RULES}\nAll tests must pass with: {verify}\n\n"
                    + "\n".join(f"- {r}" for r in reasons_list)[:3000])
        return text[:7900]

    def _submit(self, ctx: WorkContext, rec: dict, p: dict, night, events: list) -> None:
        kind = "repair" if p.get("state") == "repair" else "build"
        deadline = not_after(night, ctx.now, self.budget)
        n = len(p["attempts"]) + 1
        build_id = f"{p['slug']}-{n}-{secrets.token_hex(4)}"
        payload = {"intent": self._intent(p, kind), "repo": p["repo"],
                   "verify": self._verify(p["entry"]), "budget_seconds": self.budget,
                   "not_after": deadline, "build_id": build_id}
        a = {"n": n, "kind": kind, "submitted_at": ctx.now, "not_after": deadline,
             "night": night.key, "build_id": build_id, "slice": int(p.get("slice") or 1)}
        p["attempts"].append(a)
        # one product a night: a repair of an earlier night's product counts as tonight's
        rec["nights"][night.key]["started"] = rec["nights"][night.key].get("started") \
            or p["slug"]
        p.update(state="building", waiting=None)
        # the latch, saved BEFORE the job can start: whatever happens next, this job is the
        # one in flight until it is settled, cancelled or lost - never a second one
        rec["active"] = {"slug": p["slug"], "task_id": None, "build_id": build_id,
                         "attempt": n, "not_after": deadline}
        self._save(ctx, rec)
        out = ctx.job(Job(BUILD, payload, what=f"build {p['slug']} with Daedalus in its "
                                              f"sandbox ({kind})",
                          permissions=(PERMISSION,), wait=5.0, follow=0))
        a["task_id"] = out.task_id
        if out.status == "running" and out.task_id:
            rec["active"]["task_id"] = out.task_id
            rec["daedalus_down_since"] = None
            self._night_log(ctx, f"{p['slug']}: {kind} started at {_hm(ctx.now)}, must end by "
                                 f"{_hm(deadline)}")
            events.append(self._event(ctx, "build.started", {
                "slug": p["slug"], "kind": kind, "attempt": n,
                "budget_minutes": self.budget // 60, "not_after": _hm(deadline)}))
            self._save(ctx, rec)
            return
        if out.status == "failed" and (out.error_type == "CapabilityNotFound"
                                       or _NOT_SET_UP.search(out.error or "")):
            rec["active"] = None
            p["attempts"].pop()
            p.update(state=kind if kind == "repair" else "queued",
                     waiting=f"{BUILD} is not set up in Pionir "
                     f"({_clip(out.error, 120)})", retry_after=ctx.now + RETRY_AFTER)
            events.append(self._event(ctx, "build.not_set_up", {"why": _clip(out.error, 160)}))
            self._save(ctx, rec)
            return
        self._settle(ctx, rec, p, out, events)

    # ---- the owner's cards ---------------------------------------------------------------------
    def _card(self, ctx, rec, key: str, kind: str, title: str, body: str, *,
              replies: bool = False) -> bool:
        payload = {"key": key, "kind": kind, "title": title[:120], "body": body[:11000],
                   "replies": replies}
        self._tried.add(key)
        out = ctx.job(Job(CARD, payload, what=f"post the Builds {kind} card"))
        result = out.result if isinstance(out.result, dict) else {}
        if out.status == "done" and result.get("ok") is not False:
            rec["unposted"].pop(key, None)
            return True
        if out.status == "failed" and out.error_type == "AdapterProtocolError":
            log.error("%s: Pionir refused the card %s: %s", self.worker_id, key, out.error)
            rec["unposted"].pop(key, None)
            return False
        if len(rec["unposted"]) < MAX_UNPOSTED or key in rec["unposted"]:
            rec["unposted"][key] = payload
        return False

    def _retry_cards(self, ctx, rec) -> None:
        for key, payload in [(k, v) for k, v in rec["unposted"].items()
                             if k not in self._tried][:3]:
            self._card(ctx, rec, key, payload["kind"], payload["title"], payload["body"],
                       replies=bool(payload.get("replies")))

    def _night_log(self, ctx, line: str) -> None:
        self._log_lines.append(f"{_hm(ctx.now)} {line}")

    def _backlog_lines(self, rec: dict, doc) -> list:
        if doc is None:
            return ["The backlog file could not be read; fix it or remove it to reseed."]
        taken = set(rec["products"])
        free = [e for e in doc["products"] if e["slug"] not in taken]
        lines = [f"- `{e['slug']}` - {e['name']} (${e['price_cents'] / 100:.2f})"
                 for e in free[:12]]
        if len(free) > 12:
            lines.append(f"- and {len(free) - 12} more")
        if not lines:
            lines = ["- (empty: reply with `add <slug>` to give the Builds division work)"]
        for bad in doc.get("malformed", [])[:3]:
            lines.append(f"- NOT USABLE: `{bad['slug']}` - {'; '.join(bad['reasons'][:2])}")
        return lines

    def _backlog_card(self, ctx, rec: dict, doc: dict) -> None:
        notes = rec.get("reply_notes") or []
        fp = bl.fingerprint(doc["products"])
        if not notes and rec.get("backlog_card") == fp:
            return                      # shown already, and no reply to answer
        # a new card for each change or batch of replies (the reply count makes the key new)
        key = f"builds:backlog:{fp}:{int(rec['counts'].get('replies') or 0)}"
        body = []
        if notes:
            body += ["Your replies:"] + [f"- {n}" for n in notes[-8:]] + [""]
        body += [f"Builds run overnight ({self.window.describe()}), one product a night, "
                 f"{self.budget // 60} minutes each; Claude reviews every build before "
                 "anything is staged.", "", "Next up, in order:"]
        body += self._backlog_lines(rec, doc)
        body += ["", "Reply to change it (one command per reply):",
                 "`remove <slug>` - `top <slug>` (build it next) - or add one:",
                 "```", bl.ADD_HELP, "```"]
        if self._card(ctx, rec, key, "backlog", "The product backlog", "\n".join(body),
                      replies=True):
            rec["backlog_card"] = fp
            rec["reply_notes"] = []

    def _nightly(self, ctx, rec: dict, doc, events: list) -> None:
        # this run's log lines belong to the night that is open (or just ended)
        night = self.window.current(ctx.now)
        lines, self._log_lines = list(self._log_lines), []
        if lines:
            key = night.key if night else self.window.last_ended(ctx.now).key
            if key in rec["nights"]:
                rec["nights"][key]["log"] = (rec["nights"][key]["log"] + lines)[-40:]
        last = self.window.last_ended(ctx.now)
        n = rec["nights"].get(last.key)
        if not n or n.get("reported"):
            return
        body = self._night_body(rec, last, n, doc)
        posted = self._card(ctx, rec, f"builds:night:{last.key}", "night",
                            f"Builds, the night of {last.key}", body, replies=True)
        n["reported"] = True            # not posted: it waits in ``unposted`` and is retried
        events.append(self._event(ctx, "build.night_reported", {"night": last.key,
                                                                "posted": posted}))

    def _night_body(self, rec, night, n, doc) -> str:
        touched = [p for p in rec["products"].values()
                   if p.get("night") == night.key or any(
                       night.start <= float(a.get("submitted_at") or 0) < night.end
                       for a in p.get("attempts") or [])]
        out = [f"Window {_hm(night.start)}-{_hm(night.end)}."]
        if not touched:
            why = "the backlog is empty" if doc is not None and not bl.choose(
                doc["products"], set(rec["products"]), None) else (
                "a product from an earlier night was still in progress" if any(
                    p.get("state") in IN_PROGRESS for p in rec["products"].values())
                else "Daedalus was not available" if rec.get("daedalus_down_since")
                else "nothing could start")
            out.append(f"Nothing was built tonight: {why}.")
        for p in touched:
            e = p["entry"]
            out += ["", f"**{p['slug']}** - {e['name']} (${e['price_cents'] / 100:.2f})"]
            for a in p.get("attempts") or []:
                out.append(f"- {a.get('kind', 'build')} #{a['n']}: "
                           f"{a.get('outcome') or 'running'}"
                           + (f" - {_clip(a.get('why'), 160)}" if a.get("why") else ""))
            last = (p.get("reviews") or [None])[-1]
            if last:
                verdict = "APPROVED" if last.get("approved") else (
                    "could not be completed" if last.get("failed") else "REJECTED")
                out.append(f"- review by {last.get('by')}: {verdict}"
                           + (f" (tests: {last.get('tests_ran')} ran, "
                              f"{'passed' if last.get('tests_passed') else 'FAILED'})"
                              if last.get("tests_ran") is not None else ""))
                out += [f"  - {_clip(r, 200)}" for r in (last.get("reasons") or [])[:5]]
            state = p.get("state")
            if state == "staged":
                out.append(f"- STAGED at `{p['staged']['folder']}` - the shelf submits it for "
                           "your approval; nothing is on sale until you approve it")
            elif state == "shelved":
                out.append(f"- SHELVED: {_clip(p.get('shelved_why'), 240)}")
            else:
                out.append(f"- now: {state}" + (f" ({_clip(p.get('waiting'), 160)})"
                                                 if p.get("waiting") else ""))
        if n.get("log"):
            out += ["", "Log:"] + [f"- {_clip(line, 160)}" for line in n["log"][-10:]]
        nxt = bl.choose(doc["products"], set(rec["products"]), None) if doc else None
        out += ["", f"Next in the backlog: `{nxt['slug']}` - {nxt['name']}" if nxt
                else "The backlog is empty."]
        out += ["", "Reply `add <slug>` (with its fields), `remove <slug>` or `top <slug>` "
                    "to change the backlog."]
        return "\n".join(out)

    # ---- what the leader reads ----------------------------------------------------------------
    def _event(self, ctx, kind: str, payload: dict, figures=()):
        return make_output(self, kind=kind, valid_at=ctx.now, observed_at=ctx.now,
                           payload=payload, figures=figures, entities=self.entities,
                           provenance={"source": "real", "provider": self.provider,
                                       "record": self.record_what})

    def _tally(self, ctx, rec: dict, doc, night):
        products = rec["products"].values()

        def n(*states) -> int:
            return sum(1 for p in products if p.get("state") in states)

        free = ([e["slug"] for e in doc["products"] if e["slug"] not in rec["products"]]
                if doc is not None else [])
        act = rec.get("active")
        figures = [
            Figure(n("staged"), "count", "products staged by the Builds division",
                   window="all_time"),
            Figure(n("shelved"), "count", "products shelved", window="all_time"),
            Figure(n(*IN_PROGRESS), "count", "products in progress", window="now"),
            Figure(len(free), "count", "products waiting in the backlog", window="now"),
            Figure(1 if act else 0, "count", "Daedalus jobs running", window="now"),
        ]
        current = [{"slug": p["slug"], "state": p["state"],
                    "waiting": _clip(p.get("waiting") or "", 160)}
                   for p in products if p.get("state") in IN_PROGRESS]
        return make_output(self, kind="build.tally", valid_at=ctx.now, observed_at=ctx.now,
                           payload={"window": self.window.describe(),
                                    "window_open": night is not None,
                                    "budget_minutes": self.budget // 60,
                                    "in_progress": current[:3],
                                    "staged": [p["slug"] for p in products
                                               if p.get("state") == "staged"][-10:],
                                    "shelved": [{"slug": p["slug"],
                                                 "why": _clip(p.get("shelved_why"), 160)}
                                                for p in products
                                                if p.get("state") == "shelved"][-5:],
                                    "next_in_backlog": free[:5],
                                    "backlog_unusable": (doc or {}).get("malformed", [])[:3],
                                    "daedalus_down": bool(rec.get("daedalus_down_since")),
                                    "cards_unposted": len(rec["unposted"])},
                           figures=figures, entities=self.entities,
                           provenance={"source": "real", "provider": self.provider,
                                       "record": self.record_what})
