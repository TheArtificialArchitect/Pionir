"""``products.api_builder``: grow the Dokaz API one product at a time, on the owner's yes.

Daedalus (local model, contained sandbox user, free) WRITES. Claude spends at most two short
calls per product: one cheap REVIEW and, when that review finds fixable problems, ONE edit.
Pipeline (docs/API_BUILDER_DESIGN.md), one product per night, nothing past a gate unattended:

1. **Backlog** (``<apibuilds_dir>/backlog.json``, owner-editable; backlog.py): the next queued,
   valid entry. A bad entry is reported, never built.
2. **Scaffold + write**: inside the overnight window only, the product gets its OWN fresh
   sandbox repo (the Builds division's machinery) holding a small self-contained scaffold -
   a trimmed copy of Scrooge's Product interface and helpers, one model product and its test,
   a brief (scaffold.py) - never the real Scrooge repo. Pionir runs ``coding.daedalus_build``
   in it on its contained Daedalus (daedalus.py: the job lifecycle, mirrored from the Builds
   worker). Daedalus's gate is real ``tsc`` then ``vitest`` with the sandbox's own node.
3. **Check** (our own, contained): the commit is exported and compared with the scaffold (a
   fixed file touched, or a file nobody asked for, rejects it), every byte is validated
   (checks.py), and ``tsc`` + ``vitest`` run again as the sandbox user. A rejection goes back
   to Daedalus once (``max_attempts``), then the product is SHELVED.
4. **Review** (prompts.py): one Claude call through the no-tools review runner, on the
   per-night and daily Claude caps and the cheaper model. Approved -> staged. Rejected with
   problems it calls fixable -> ONE edit pass (the Write-only runner, an empty directory, the
   finished files in the prompt); the edit is re-checked and re-tested, and if it passes it is
   staged WITHOUT a second review (that would be a second call), if not the product is SHELVED.
   Claude's budget spent -> the product waits; it is never approved by default.
5. **Stage** (stage.py): the three files, one local commit on ``api/<id>`` in a worktree of
   the Scrooge repo. Never ``main``, never pushed.
6. **Card**: ``Job("apibuild.verify", {id, commit})`` is parked by Pionir for the owner.
7. **Verify, on his yes only**: Pionir runs real tsc and vitest in the real worktree - this is
   what catches the scaffold drifting from the real Worker. Green is VERIFIED, red is RED.
8. **Release is the owner's**: nothing here deploys, writes D1, pushes or publishes.

Fails closed: with no sandbox setup, no node in it (``tools\\setup-build-sandbox.ps1``), or auth
compat on, the worker says NOT SET UP and builds nothing. Latches have a way back: raise
``retry`` on the backlog entry (a fresh sandbox repo is made; the old one is left for a look).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from pionir import build_sandbox
from pionir.auth import compat_from_environment

from ..blog import FORGOTTEN_AFTER, _clip, _Unreadable, read_record, record_path, save_record
from ..builds import sandbox
from ..builds.window import Window, can_start
from ..delivery import _PASSING, PASSING_TYPES, _inner_why, _refused
from ..figures import Figure
from ..hands import Job, outcome_of
from ..log import log
from ..orders import _NOT_SET_UP, RETRY_UNDELIVERED, RETRY_UNREACHABLE
from ..result import Err, Ok, Result
from ..worker import ErrorKind, WorkContext, make_output, never_raises
from ..workers import _Base
from .backlog import load_backlog
from .checks import check_build, expected_files, parse_build
from .daedalus import DaedalusJobs
from .prompts import edit_prompt, parse_verdict, review_prompt, verdict_json
from .scaffold import scaffold_files, slug_for, tamper
from .stage import StageError, branch_for, git, stage

VERIFY = "apibuild.verify"
REVIEW_TIMEOUT = 600.0
EDIT_TIMEOUT = 900.0
TEST_TIMEOUT = 240.0
CLAUDE_RETRY = 1800.0               # seconds before a waiting review or edit is asked again
INFRA_RETRY = 900.0                 # seconds before checks that could not start are retried
REVIEW_ATTEMPTS = 3                 # Claude asked and failed this often -> shelved
EDIT_ATTEMPTS = 2
KEEP_NIGHTS = 30
WAITING_ON_OWNER = frozenset({"pending"})
RESUBMIT = frozenset({"unreachable", "undelivered", "unsubmitted"})
IN_PROGRESS = frozenset({"queued", "building", "built", "repair"})
CLOSED = frozenset({"verified", "red", "denied", "failed", "shelved"})
REOPENABLE = CLOSED
CAPS = {"unreachable": RETRY_UNREACHABLE, "undelivered": RETRY_UNDELIVERED}
COULD_NOT_START = "the tests could not start contained"


def _digest(files: dict) -> str:
    return hashlib.sha1(json.dumps(files, sort_keys=True).encode("utf-8")).hexdigest()


class ApiBuilder(DaedalusJobs, _Base):
    """``products.api_builder``: see the module docstring."""

    record_what = "the API builder's own record of every product it built and staged"

    def __init__(self, spec, *, window: str = "01:00-07:00", budget_minutes: int = 45,
                 max_attempts: int = 2, test_timeout_seconds: float = TEST_TIMEOUT,
                 review_timeout_seconds: float = REVIEW_TIMEOUT,
                 edit_timeout_seconds: float = EDIT_TIMEOUT) -> None:
        super().__init__(spec)
        self.window = Window.parse(window)
        if not 10 <= int(budget_minutes) <= 180:
            raise ValueError("budget_minutes must be 10 to 180")
        self.budget = int(budget_minutes) * 60
        if not 1 <= int(max_attempts) <= 3:
            raise ValueError("max_attempts must be 1 to 3 (a build and its repairs)")
        self.max_attempts = int(max_attempts)
        self.test_timeout = float(test_timeout_seconds)
        self.review_timeout = float(review_timeout_seconds)
        self.edit_timeout = float(edit_timeout_seconds)
        # the outside world, injected (tests replace these)
        self.run_checks = build_sandbox.run_ts_checks
        self.git_run = None                 # None: subprocess.run
        self.make_link = None               # None: a real junction
        self.load_sandbox = lambda root: build_sandbox.load_setup(root)
        # loopback is open to the sandbox user while auth compat is on, so generated code
        # could reach Pionir with no token: nothing is built until compat is off
        self.auth_compat = compat_from_environment
        self._setup = None

    # ---- not set up -----------------------------------------------------------------------
    def readiness(self, secrets_dir) -> str | None:
        root = os.environ.get("PIONIR_DAEDALUS_SANDBOX", "").strip() or \
            build_sandbox.default_sandbox_root()
        setup, why = self.load_sandbox(root)
        if setup is None:
            return why or build_sandbox.SETUP_HINT
        return self._not_ready(setup)

    def _not_ready(self, setup) -> str | None:
        node = build_sandbox.node_ready(setup)
        if node:
            return f"NOT SET UP: {node}"
        if self.auth_compat():
            return ("not armed: PIONIR_AUTH_COMPAT is on, so a process with no token (generated "
                    "code on loopback included) is still served; set PIONIR_AUTH_COMPAT=off "
                    "once every client sends its token")
        return None

    # ---- the record ------------------------------------------------------------------------
    def record_path(self, state_dir):
        return record_path(state_dir, self.worker_id)

    @staticmethod
    def _blank() -> dict:
        return {"products": {}, "active": None, "nights": {}, "blocked_until": 0.0}

    def load(self, state_dir) -> dict:
        doc = read_record(state_dir, self.worker_id)
        return self._blank() if doc is None else {**self._blank(), **doc}

    def save(self, state_dir, rec: dict) -> None:
        save_record(self.record_path(state_dir), rec)

    def _save(self, ctx: WorkContext, rec: dict) -> None:
        for key in sorted(rec["nights"])[:-KEEP_NIGHTS]:
            rec["nights"].pop(key, None)
        self.save(ctx.state_dir, rec)

    # ---- one run -------------------------------------------------------------------------
    @never_raises()
    def run(self, ctx: WorkContext) -> Result:
        missing = next((why for ok, why in (
            (ctx.state_dir is not None, "no state dir: it could not keep its record, and "
                                        "without it could build a product twice"),
            (ctx.job is not None, "no hands: Daedalus and the checks run only through "
                                  "Pionir, on the owner's approval where it matters"),
            (ctx.builds_sandbox is not None, "no sandbox workspace is set (builds_sandbox)"),
            (ctx.apibuilds_dir is not None, "no apibuilds folder is set (apibuilds_dir)"),
            (ctx.scrooge_repo is not None, "no Scrooge repository is set (scrooge_repo)"),
        ) if not ok), None)
        if missing:
            return self._err(ErrorKind.NOT_CONFIGURED, missing, retryable=False)
        setup, why = self.load_sandbox(ctx.builds_sandbox)
        if setup is None:
            return self._err(ErrorKind.NOT_CONFIGURED, why or build_sandbox.SETUP_HINT,
                             retryable=False)
        not_ready = self._not_ready(setup)
        if not_ready:
            return self._err(ErrorKind.NOT_CONFIGURED, not_ready, retryable=False)
        self._setup = setup
        try:
            rec = self.load(ctx.state_dir)
        except _Unreadable as exc:
            return self._err(ErrorKind.MALFORMED, f"its record is unreadable ({exc}); "
                             "refusing to build anything, since it could build twice",
                             retryable=False)
        try:
            backlog = load_backlog(ctx.apibuilds_dir)
        except (ValueError, OSError) as exc:
            return self._err(ErrorKind.MALFORMED, f"the backlog is unreadable ({exc}); it "
                             "is not overwritten and nothing is built", retryable=False)
        events: list = []
        seen = {"not_set_up": None, "cleanup": []}
        night = self.window.current(ctx.now)
        if night is not None:
            rec["nights"].setdefault(night.key, {"start": night.start, "end": night.end,
                                                 "started": None})
        if backlog.invalid:
            events.append(self._event(ctx, "api.backlog_invalid", {
                "entries": [{"id": i, "reasons": r} for i, r in backlog.invalid[:10]]}))
        self._follow_up(ctx, rec, events)
        self._resubmit(ctx, rec, events, seen)
        self._reopen(ctx, rec, backlog, seen)
        self._recover_orphans(ctx, rec, events)
        self._follow(ctx, rec, events)
        self._check_one(ctx, rec, backlog, events, seen)
        self._maybe_start(ctx, rec, backlog, events)
        self._save(ctx, rec)
        return Ok((*events, self._tally(ctx, rec, backlog, seen)))

    @staticmethod
    def _entry(backlog, pid):
        return next((e for e in backlog.entries if e["id"] == pid), None)

    @staticmethod
    def _busy(rec: dict) -> bool:
        """One product at a time: nothing new is built while one waits on the owner, on a
        resubmission, or is anywhere between the first job and staging."""
        return any(p.get("state") in WAITING_ON_OWNER | RESUBMIT | IN_PROGRESS
                   for p in rec["products"].values())

    @staticmethod
    def _blank_product(entry: dict) -> dict:
        retry = int(entry.get("retry") or 0)
        return {"id": entry["id"], "name": entry["name"], "state": "queued", "attempts": [],
                "retry_seen": retry, "generation": retry, "slug": None, "repo": None,
                "base": None, "repair_reasons": [], "review_failures": 0}

    # ---- 1. what became of the products waiting on the owner -------------------------------
    def _follow_up(self, ctx: WorkContext, rec: dict, events: list) -> None:
        for p in rec["products"].values():
            if p.get("state") != "pending" or not p.get("approval_id"):
                continue
            if ctx.approval is None:
                return
            got = ctx.approval(p["approval_id"]) or {}
            state = got.get("status")
            p["checked_at"] = ctx.now
            if state in ("pending", "running", "unreachable"):
                continue
            if state == "unknown":
                if ctx.now - float(p.get("submitted_at") or ctx.now) > FORGOTTEN_AFTER:
                    self._settle(ctx, p, "failed", "Pionir no longer lists this approval; the "
                                 "checks were never seen to run", events)
                continue
            if state == "denied":
                self._settle(ctx, p, "denied", f"the owner did not approve running it "
                             f"({got.get('reason') or 'denied'}); the branch is left to him",
                             events)
            elif state in ("approved", "approved_failed"):
                self._approved(ctx, p, got.get("result"), events)
            else:
                log.warning("%s: approval %s has a status nobody knows (%r); still waiting",
                            self.worker_id, p["approval_id"], state)

    def _approved(self, ctx: WorkContext, p: dict, result, events: list) -> None:
        out = outcome_of(VERIFY, result)
        if not out.ran:
            out.error = _inner_why(result) or out.error
        if out.ran:
            self._answer(ctx, p, out.result, events)
        elif _refused(result):
            self._settle(ctx, p, "failed", out.error or "refused when it was approved", events)
        else:
            self._offer_again(p, out.error or "the checks could not run")

    def _answer(self, ctx: WorkContext, p: dict, done, events: list) -> None:
        done = done if isinstance(done, dict) else {}
        if done.get("ok") is True and done.get("green") in (True, False):
            p["rows"] = self._rows(done)
            if done["green"]:
                self._settle(ctx, p, "verified", "tsc and the tests pass on the branch", events)
            else:
                self._settle(ctx, p, "red", "a check failed on the branch", events)
        else:
            self._offer_again(p, "Pionir did not say whether the checks passed")

    def _offer_again(self, p: dict, why: str) -> None:
        p.update(state="undelivered", why=_clip(why, 300) + "; the checks will be offered "
                 "to the owner again")
        log.warning("%s: %s: %s", self.worker_id, p["id"], p["why"])

    @staticmethod
    def _rows(done: dict) -> list:
        rows = []
        for r in (done.get("rows") or [])[:6]:
            if isinstance(r, dict):
                rows.append({"check": _clip(str(r.get("check")), 40), "ok": r.get("ok") is True,
                             "exit": r.get("exit") if isinstance(r.get("exit"), int) else None,
                             "detail": _clip(str(r.get("detail") or ""), 1200)})
        return rows

    # ---- 2. a staged build that never reached the owner ------------------------------------
    def _resubmit(self, ctx: WorkContext, rec: dict, events: list, seen: dict) -> None:
        for p in rec["products"].values():
            state = p.get("state")
            if state not in RESUBMIT or not p.get("commit"):
                continue
            if state in CAPS and int(p.get("tries") or 0) >= CAPS[state]:
                self._settle(ctx, p, "failed", f"gave up after {CAPS[state]} tries "
                             f"({p.get('why') or state}); the branch is left to the owner",
                             events)
                continue
            self._submit(ctx, rec, p, events, seen)

    def _submit(self, ctx: WorkContext, rec: dict, p: dict, events: list, seen: dict) -> bool:
        """Ask Pionir to park the checks for the owner. False when Pionir has no such
        capability (nothing is held against the product; it is offered again next run)."""
        was = p.get("state")
        out = ctx.job(Job(VERIFY, {"id": p["id"], "commit": p["commit"]},
                          what=f"run tsc and the tests on the staged API product {p['id']} "
                               f"(branch {branch_for(p['id'])}, commit {p['commit'][:10]})"))
        why = out.error or f"Pionir said {out.status}"
        if out.status == "failed" and (_NOT_SET_UP.search(why) or why.strip() in (VERIFY, "CapabilityNotFound")
                                       or out.error_type == "CapabilityNotFound"):
            seen["not_set_up"] = _clip(why, 160)
            p.update(state="unsubmitted", why=_clip(why, 300))
            log.error("%s: %s is not set up in Pionir (%s); the staged branch waits",
                      self.worker_id, VERIFY, why)
            events.append(self._event(ctx, "api.not_set_up", {
                "id": p["id"], "why": _clip(why, 160)}))
            return False
        if was in CAPS:
            p["tries"] = int(p.get("tries") or 0) + 1
        p.update(submitted_at=ctx.now, task_id=out.task_id, approval_id=out.approval_id)
        if out.status == "pending_approval":
            p.update(state="pending", why="waiting for the owner's yes")
            log.info("%s: %s is PENDING the owner's approval (approval %s)", self.worker_id,
                     p["id"], out.approval_id)
            events.append(self._event(ctx, "api.pending", {
                "id": p["id"], "branch": branch_for(p["id"]), "commit": p["commit"][:10],
                "approval_id": out.approval_id}, names=(p.get("name"),)))
        elif out.status == "done":
            # ran without being parked: the owner did NOT see it - Pionir's gate must.
            log.error("%s: %s ran WITHOUT the owner's approval for %s; it must be "
                      "approval-gated in Pionir", self.worker_id, VERIFY, p["id"])
            p["approved_by_owner"] = False
            self._answer(ctx, p, out.result, events)
        elif out.status == "failed" and out.error_type == "AdapterProtocolError":
            self._settle(ctx, p, "failed", f"Pionir refused it: {why}", events)
        elif out.status in ("unreachable", "failed") and (
                out.status == "unreachable" or out.error_type in PASSING_TYPES
                or (not out.error_type and _PASSING.search(why))):
            p.update(state="unreachable", why=_clip(why, 300))
            log.warning("%s: %s did not reach Pionir (%s); offered again next run",
                        self.worker_id, p["id"], why)
        elif out.status == "failed":
            self._settle(ctx, p, "failed", why, events)
        else:       # still running: never assumed to be anything, never sent again
            self._settle(ctx, p, "failed", f"Pionir answered {out.status}: {why}", events)
        return True

    # ---- the way back ----------------------------------------------------------------------
    def _reopen(self, ctx: WorkContext, rec: dict, backlog, seen: dict) -> None:
        """A raised ``retry`` on the backlog entry reopens a closed product, but only when no
        branch is left in the way; otherwise the two commands that clear it are reported. The
        reopened product gets a fresh sandbox repo (its generation names the folder)."""
        for pid, p in list(rec["products"].items()):
            entry = self._entry(backlog, pid)
            if p.get("state") not in REOPENABLE or entry is None:
                continue
            retry = int(entry.get("retry") or 0)
            if retry <= int(p.get("retry_seen") or 0):
                continue
            worktree = Path(ctx.apibuilds_dir) / "worktrees" / pid
            rc, _out, _err = git(ctx.scrooge_repo, "show-ref", "--verify", "--quiet",
                                 f"refs/heads/{branch_for(pid)}")
            if rc == 0 or worktree.exists():
                seen["cleanup"].append({"id": pid, "commands": [
                    f"git -C {ctx.scrooge_repo} worktree remove --force {worktree}",
                    f"git -C {ctx.scrooge_repo} branch -D {branch_for(pid)}"]})
                continue
            log.info("%s: %s is reopened by retry %d", self.worker_id, pid, retry)
            rec["products"][pid] = self._blank_product(entry)

    # ---- 3. the next product ---------------------------------------------------------------
    def _maybe_start(self, ctx: WorkContext, rec: dict, backlog, events: list) -> None:
        if rec.get("active"):
            return                                          # one job at a time
        night = self.window.current(ctx.now)
        if not can_start(night, ctx.now, self.budget):
            return                                          # overnight only, and must fit
        if float(rec.get("blocked_until") or 0) > ctx.now:
            return
        products = rec["products"]
        p = next((q for q in products.values() if q.get("state") in ("repair", "queued")), None)
        if p is not None:
            if float(p.get("retry_after") or 0) > ctx.now:
                return
            entry = self._entry(backlog, p["id"])
            if entry is None:
                self._shelve(ctx, rec, p, "its backlog entry is gone or no longer valid; "
                             "nothing more is built for it", events)
                return
        else:
            if self._busy(rec) or rec["nights"][night.key].get("started"):
                return                                      # one product, one new one a night
            entry = next((e for e in backlog.entries if e["id"] not in products), None)
            if entry is None:
                return
            p = products[entry["id"]] = self._blank_product(entry)
        if not p.get("repo") and not self._make_repo(ctx, rec, p, entry, night, events):
            return
        if self._landed_meanwhile(ctx, rec, p, events):
            return
        self._job_start(ctx, rec, p, entry, night, events)

    def _make_repo(self, ctx, rec, p: dict, entry: dict, night, events: list) -> bool:
        slug = slug_for(entry, int(p.get("generation") or 0))
        rec["nights"][night.key]["started"] = entry["id"]
        try:
            base = sandbox.create_repo(
                ctx.builds_sandbox, slug, scaffold_files(entry),
                marker={"slug": slug, "created_at": ctx.now, "product": entry["id"],
                        "made_by": "the crew's API builder", "listing": entry["name"]},
                message=f"Seed {slug}: brief, scaffold and worked example", **self._git())
        except (sandbox.SandboxError, OSError) as exc:
            self._shelve(ctx, rec, p, f"its sandbox repo could not be created "
                         f"({_clip(exc, 200)})", events)
            return False
        p.update(slug=slug, repo=str(Path(ctx.builds_sandbox) / slug), base=base,
                 created_at=ctx.now)
        self._save(ctx, rec)
        return True

    # ---- 4. checking, reviewing and staging a build ------------------------------------------
    def _wait(self, ctx, p: dict, why: str, events: list, *, after: float) -> None:
        if p.get("waiting") != why:
            events.append(self._event(ctx, "api.waiting", {"id": p["id"], "why": _clip(why, 200)},
                                      names=(p.get("name"),)))
        p.update(waiting=why, review_after=ctx.now + after)

    @staticmethod
    def _texts(entry: dict, files: dict) -> tuple:
        """``({path: text}, problems)`` of the three product files, strictly UTF-8."""
        texts: dict = {}
        problems: list = []
        for rel in expected_files(entry["id"]):
            raw = files.get(rel)
            if raw is None:
                problems.append(f"{rel} is missing")
                continue
            try:
                texts[rel] = raw.decode("utf-8")
            except UnicodeDecodeError:
                problems.append(f"{rel} is not valid UTF-8 text")
        return texts, problems

    @staticmethod
    def _run_reasons(run) -> list:
        tail = _clip(run.tail or "", 900)
        if run.timed_out:
            return [f"tsc and the tests did not finish in time: {tail}"]
        if not run.tsc_ok:
            return [f"tsc --noEmit found errors:\n{tail}"]
        if run.ran == 0:
            return [f"no test ran: {tail}"]
        return [f"the tests failed:\n{tail}"]

    def _check_one(self, ctx: WorkContext, rec: dict, backlog, events: list,
                   seen: dict) -> None:
        p = next((q for q in rec["products"].values() if q.get("state") == "built"
                  and float(q.get("review_after") or 0) <= ctx.now), None)
        if p is None:
            return
        entry = self._entry(backlog, p["id"])
        if entry is None:
            self._shelve(ctx, rec, p, "its backlog entry is gone or no longer valid", events)
            return
        try:
            tree = sandbox.export(p["repo"], p["head"], **self._git())
        except sandbox.SandboxError as exc:
            self._shelve(ctx, rec, p, f"its sandbox repo could not be read ({_clip(exc, 200)})",
                         events)
            return
        texts, bad = self._texts(entry, tree.files)
        reasons = tamper(entry, tree.files) + list(tree.problems) + bad
        if not reasons:
            reasons = check_build(entry, texts)
        if reasons:
            self._rejected(ctx, rec, p, reasons, events, by="our checks")
            return
        if (p.get("edit") or {}).get("files"):
            self._after_edit(ctx, rec, p, entry, tree, events, seen)
            return
        run_text = self._run_checks(ctx, rec, p, tree.files, events, edited=False)
        if run_text is None:
            return
        verdict = p.get("verdict")
        if not (isinstance(verdict, dict) and verdict.get("head") == p["head"]):
            verdict = self._ask_review(ctx, rec, p, entry, texts, run_text, events)
            if verdict is None:
                return
        if verdict["approved"]:
            self._stage(ctx, rec, p, entry, texts, events, seen, edited=False)
        elif verdict["fixable"]:
            self._edit_pass(ctx, rec, p, entry, tree, texts, verdict, events, seen)
        else:
            p["reviewed_head"] = p["head"]
            self._shelve(ctx, rec, p, "Claude rejected it and called it not fixable by a "
                         "small edit: " + "; ".join(_clip(x, 200) for x in verdict["problems"][:4]),
                         events)

    def _run_checks(self, ctx, rec, p, files: dict, events: list, *, edited: bool):
        """The contained tsc and vitest run. Returns the text for the review, or None when
        the product was rejected or must wait (already recorded)."""
        key = "green_edit" if edited else "green_head"
        stamp = (p.get("edit") or {}).get("digest") if edited else p["head"]
        if stamp and p.get(key) == stamp:
            return p.get("run_text") or "tsc --noEmit clean; the tests pass."
        run = self.run_checks(files, setup=self._setup, timeout=self.test_timeout)
        if (run.tail or "").startswith(COULD_NOT_START):
            self._wait(ctx, p, _clip(run.tail, 200), events, after=INFRA_RETRY)
            return None
        if not run.passed:
            if edited:
                self._shelve(ctx, rec, p, "Claude's edit did not pass our checks: "
                             + "; ".join(_clip(x, 300) for x in self._run_reasons(run)), events)
            else:
                self._rejected(ctx, rec, p, self._run_reasons(run), events, by="our checks")
            return None
        text = f"tsc --noEmit: clean. vitest: {run.ran} test(s) passed, run contained."
        p.update({key: stamp, "run_text": text, "tests_ran": run.ran, "waiting": None})
        return text

    def _ask_review(self, ctx, rec, p, entry, texts, run_text, events):
        prompt, why = review_prompt(entry, texts, run_text)
        if prompt is None:
            self._rejected(ctx, rec, p, [why], events, by="our checks")
            return None
        if ctx.review is None:
            self._wait(ctx, p, "no Claude review path is configured; nothing is staged "
                       "without Claude's review", events, after=CLAUDE_RETRY)
            return None
        got = ctx.review(prompt, self.review_timeout)
        if isinstance(got, Err):
            refusal = got.error
            if getattr(refusal, "waits", False):
                self._wait(ctx, p, f"waiting for Claude's review ({_clip(refusal, 160)})",
                           events, after=CLAUDE_RETRY)
            else:
                self._review_failed(ctx, rec, p, f"Claude did not answer: {_clip(refusal, 200)}",
                                    events)
            return None
        verdict = parse_verdict(str(got.value))
        if verdict is None:
            self._review_failed(ctx, rec, p, "Claude's answer was not a readable verdict",
                                events)
            return None
        out = {**verdict_json(verdict), "head": p["head"], "at": ctx.now}
        p.update(verdict=out, review_failures=0, waiting=None)
        self._save(ctx, rec)                    # a verdict is never asked for twice
        return out

    def _review_failed(self, ctx, rec, p, why, events) -> None:
        p["review_failures"] = int(p.get("review_failures") or 0) + 1
        events.append(self._event(ctx, "api.review_failed", {
            "id": p["id"], "why": _clip(why, 200), "failures": p["review_failures"]},
            names=(p.get("name"),)))
        if p["review_failures"] >= REVIEW_ATTEMPTS:
            self._shelve(ctx, rec, p, f"Claude's review could not be completed "
                         f"({p['review_failures']} tries; the last: {why})", events)
        else:
            p.update(waiting=_clip(why, 200), review_after=ctx.now + CLAUDE_RETRY)

    def _edit_pass(self, ctx, rec, p, entry, tree, texts, verdict, events, seen) -> None:
        """Claude's ONE edit: asked once, its files saved at once, then checked like any build."""
        ed = p.setdefault("edit", {"failures": 0})
        if ctx.build_site is None:
            self._wait(ctx, p, "no Claude edit path is configured", events, after=CLAUDE_RETRY)
            return
        prompt, why = edit_prompt(entry, texts, verdict["problems"])
        if prompt is None:
            self._shelve(ctx, rec, p, f"the edit could not be asked for: {why}", events)
            return
        got = ctx.build_site(prompt, self.edit_timeout)
        if isinstance(got, Err) and getattr(got.error, "waits", False):
            self._wait(ctx, p, f"waiting for Claude's edit ({_clip(got.error, 160)})", events,
                       after=CLAUDE_RETRY)
            return
        if isinstance(got, Err):
            files, problems = None, [_clip(got.error, 200)]
        else:
            files, problems = parse_build(got.value)
        reasons = list(problems) if files is None else list(problems) + check_build(entry, files)
        if reasons:
            ed["failures"] = int(ed.get("failures") or 0) + 1
            events.append(self._event(ctx, "api.review_failed", {
                "id": p["id"], "why": "the edit failed: " + _clip("; ".join(reasons[:3]), 160),
                "failures": ed["failures"]}, names=(p.get("name"),)))
            if files is not None or ed["failures"] >= EDIT_ATTEMPTS:
                self._shelve(ctx, rec, p, "Claude's edit failed our checks: "
                             + "; ".join(_clip(x, 200) for x in reasons[:4]), events)
            else:
                p.update(waiting=_clip(reasons[0], 200), review_after=ctx.now + CLAUDE_RETRY)
            return
        edited = {rel: files[rel] for rel in expected_files(entry["id"])}
        ed.update(files=edited, at=ctx.now, digest=_digest(edited))
        self._save(ctx, rec)                    # the one edit is never asked for twice
        self._after_edit(ctx, rec, p, entry, tree, events, seen)

    def _after_edit(self, ctx, rec, p, entry, tree, events, seen) -> None:
        edited = p["edit"]["files"]
        files = dict(tree.files)
        for rel, text in edited.items():
            files[rel] = text.encode("utf-8")
        if self._run_checks(ctx, rec, p, files, events, edited=True) is None:
            return
        self._stage(ctx, rec, p, entry, edited, events, seen, edited=True)

    def _rejected(self, ctx, rec, p, reasons: list, events: list, *, by: str) -> None:
        p["reviewed_head"] = p.get("head")
        text = "; ".join(_clip(r, 200) for r in reasons[:6])
        events.append(self._event(ctx, "api.rejected", {
            "id": p["id"], "by": by, "reasons": [_clip(r, 160) for r in reasons[:5]]},
            names=(p.get("name"),)))
        if self._counted(p) < self.max_attempts:
            p.update(state="repair", repair_reasons=[_clip(r, 400) for r in reasons[:12]],
                     waiting=None, retry_after=0.0)
        else:
            self._shelve(ctx, rec, p, f"rejected by {by} after its repair: {text}", events)
        self._save(ctx, rec)

    def _stage(self, ctx, rec, p, entry, texts: dict, events, seen, *, edited: bool) -> None:
        """ONLY for files our checks and Claude's review passed (or Claude's own edit did)."""
        try:
            staged = stage(ctx.scrooge_repo, Path(ctx.apibuilds_dir) / "worktrees", entry, texts)
        except StageError as exc:
            self._settle(ctx, p, "failed", f"it could not be staged: {exc}", events)
            self._cleanup(p)
            self._save(ctx, rec)
            return
        p.update(state="unsubmitted", commit=staged.commit, branch=staged.branch,
                 worktree=str(staged.worktree), files=list(staged.files),
                 diff_stat=staged.stat[-600:], staged_at=ctx.now, edited=edited, waiting=None)
        self._cleanup(p)
        log.info("%s: %s staged on %s at %s", self.worker_id, p["id"], staged.branch,
                 staged.commit[:10])
        events.append(self._event(ctx, "api.staged", {
            "id": p["id"], "branch": staged.branch, "commit": staged.commit[:10],
            "files": list(staged.files), "written_by": "Daedalus", "edited_by_claude": edited,
            "attempts": self._counted(p)}, names=(p["name"],)))
        self._save(ctx, rec)
        self._submit(ctx, rec, p, events, seen)

    @staticmethod
    def _cleanup(p: dict) -> None:
        """The product is finished: its tools junction goes (never followed into the tools)."""
        if p.get("repo"):
            try:
                build_sandbox.remove_tree(Path(p["repo"]) / "node_modules")
            except OSError as exc:
                log.warning("api_builder: could not remove node_modules of %s: %s", p["id"], exc)

    def _shelve(self, ctx, rec, p: dict, why: str, events: list) -> None:
        if rec.get("active") and rec["active"].get("pid") == p["id"]:
            rec["active"] = None
        self._settle(ctx, p, "shelved", why, events)
        self._cleanup(p)
        self._save(ctx, rec)

    # ---- events and the tally --------------------------------------------------------------
    def _settle(self, ctx: WorkContext, p: dict, state: str, why: str, events: list) -> None:
        p.update(state=state, why=_clip(why, 300), settled_at=ctx.now, waiting=None)
        kind = {"verified": "api.verified", "red": "api.red", "shelved": "api.shelved",
                "denied": "api.denied"}.get(state, "api.failed")
        level = log.info if state == "verified" else log.warning
        level("%s: %s is %s: %s", self.worker_id, p.get("id"), state, why)
        payload = {"id": p["id"], "state": state, "why": _clip(why, 200)}
        if p.get("commit"):
            payload["branch"] = branch_for(p["id"])
            payload["commit"] = p["commit"][:10]
        if state in ("verified", "red") and p.get("rows"):
            payload["rows"] = [{"check": r["check"], "ok": r["ok"]} for r in p["rows"]]
            payload["next"] = (
                f"review branch {branch_for(p['id'])}, then run tools\\deploy.ps1 from "
                f"{p.get('worktree')}; nothing was deployed" if state == "verified" else
                "a check failed; the output is in the record; nothing was deployed")
        events.append(self._event(ctx, kind, payload, names=(p.get("name"),)))

    def _entities(self, names) -> tuple:
        return (*self.entities, *sorted(n for n in names if isinstance(n, str))[:40])

    def _event(self, ctx: WorkContext, kind: str, payload: dict, figures=(), names=()):
        return make_output(self, kind=kind, valid_at=ctx.now, observed_at=ctx.now,
                           payload=payload, figures=figures, entities=self._entities(names),
                           provenance={"source": "real", "provider": self.provider,
                                       "record": self.record_what})

    def _tally(self, ctx: WorkContext, rec: dict, backlog, seen: dict):
        products = rec["products"]

        def of(*states) -> list:
            return sorted(pid for pid, p in products.items() if p.get("state") in states)

        queued = [e["id"] for e in backlog.entries if e["id"] not in products]
        working = of(*IN_PROGRESS)
        waiting = next((p["waiting"] for p in products.values()
                        if p.get("waiting") and p.get("state") in IN_PROGRESS), None)
        figures = [
            Figure(len(queued), "count", "API products queued in the backlog", window="now"),
            Figure(len(working), "count", "API products Daedalus is writing or being checked",
                   window="now"),
            Figure(len(of("pending", *RESUBMIT)), "count",
                   "API products staged and waiting on the owner", window="now"),
            Figure(len(of("verified")), "count", "API products staged and passing the checks",
                   window="now"),
            Figure(len(of("red")), "count", "API products whose checks failed", window="now"),
            Figure(len(of("shelved")), "count", "API products shelved after rejected builds",
                   window="now"),
            Figure(len(of("denied")), "count", "API products the owner did not approve",
                   window="now"),
            Figure(len(of("failed")), "count", "API products that failed to stage or check",
                   window="now"),
            Figure(len(backlog.invalid), "count", "backlog entries invalid (not built)",
                   window="now"),
        ]
        return make_output(self, kind="api.tally", valid_at=ctx.now, observed_at=ctx.now,
                           payload={"queued": queued[:10], "in_progress": working,
                                    "waiting_on_owner": of("pending", *RESUBMIT),
                                    "verified": of("verified"), "red": of("red"),
                                    "shelved": of("shelved"), "denied": of("denied"),
                                    "failed": of("failed"),
                                    "needs_cleanup_before_retry": seen["cleanup"][:5],
                                    "waiting_to_build": waiting,
                                    "verify_not_set_up": seen["not_set_up"],
                                    "backlog_seeded": backlog.seeded,
                                    "unvalidated_ideas": sum(
                                        1 for e in backlog.entries if not e.get("validated"))},
                           figures=figures, entities=self._entities(()),
                           provenance={"source": "real", "provider": self.provider,
                                       "record": self.record_what})
