"""``products.api_builder``: grow the Dokaz API one product at a time, on the owner's yes.

Pipeline (docs/API_BUILDER_DESIGN.md), one product per run, nothing past a gate unattended:

1. **Backlog** (``<apibuilds_dir>/backlog.json``, owner-editable; backlog.py): the next queued,
   valid entry. A bad entry is reported, never built.
2. **Write**: ``ctx.build_site`` - Claude on the Max plan in the Write-only, empty-directory
   runner, counted against the shared daily cap and the division share. It returns three files.
3. **Check** (checks.py): every byte is validated before it touches a repository. A rejected
   build is retried with the reasons, twice in a run, three times in all, then SHELVED.
4. **Stage** (stage.py): one local commit on ``api/<id>`` in a worktree of the Scrooge repo.
   Never ``main``, never pushed.
5. **Card**: ``Job("apibuild.verify", {id, commit})`` is parked by Pionir for the owner.
6. **Verify, on his yes only**: Pionir runs tsc and vitest in the worktree. Green is VERIFIED,
   red is RED with the output - reported, left to the owner.
7. **Release is the owner's**: nothing here deploys, writes D1, pushes or publishes.

Latches have a way back: raise ``retry`` on the backlog entry. A state with no branch left
(a rejected or failed build) is reopened; one whose branch still exists reports the two
commands that remove it, and deletes nothing itself.
"""
from __future__ import annotations

from pathlib import Path

from ..blog import FORGOTTEN_AFTER, _clip, _Unreadable, read_record, record_path, save_record
from ..delivery import _PASSING, PASSING_TYPES, _inner_why, _refused
from ..figures import Figure
from ..hands import Job, outcome_of
from ..log import log
from ..orders import _NOT_SET_UP, RETRY_UNDELIVERED, RETRY_UNREACHABLE
from ..result import Err, Ok, Result
from ..worker import ErrorKind, WorkContext, make_output, never_raises
from ..workers import _Base
from .backlog import load_backlog
from .checks import build_prompt, check_build, parse_build
from .stage import StageError, branch_for, git, stage

VERIFY = "apibuild.verify"
BUILD_TIMEOUT = 900.0
ATTEMPTS_PER_RUN = 2
MAX_ATTEMPTS = 3
WAITING_ON_OWNER = frozenset({"pending"})
RESUBMIT = frozenset({"unreachable", "undelivered", "unsubmitted"})
CLOSED = frozenset({"verified", "red", "denied", "failed", "shelved"})
REOPENABLE = CLOSED
CAPS = {"unreachable": RETRY_UNREACHABLE, "undelivered": RETRY_UNDELIVERED}


class ApiBuilder(_Base):
    """``products.api_builder``: see the module docstring."""

    record_what = "the API builder's own record of every product it built and staged"

    def record_path(self, state_dir):
        return record_path(state_dir, self.worker_id)

    def load(self, state_dir) -> dict:
        doc = read_record(state_dir, self.worker_id)
        blank = {"products": {}}
        return blank if doc is None else {**blank, **doc}

    def save(self, state_dir, rec: dict) -> None:
        save_record(self.record_path(state_dir), rec)

    # ---- one run -------------------------------------------------------------------------
    @never_raises()
    def run(self, ctx: WorkContext) -> Result:
        missing = next((why for ok, why in (
            (ctx.state_dir is not None, "no state dir: it could not keep its record, and "
                                        "without it could build a product twice"),
            (ctx.job is not None, "no hands: the checks run only through Pionir, on the "
                                  "owner's approval"),
            (ctx.build_site is not None, "no build runner: products are written only by the "
                                         "Write-only Claude runner"),
            (ctx.apibuilds_dir is not None, "no apibuilds folder is set (apibuilds_dir)"),
            (ctx.scrooge_repo is not None, "no Scrooge repository is set (scrooge_repo)"),
        ) if not ok), None)
        if missing:
            return self._err(ErrorKind.NOT_CONFIGURED, missing, retryable=False)
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
        seen = {"waiting": None, "not_set_up": None, "cleanup": []}
        if backlog.invalid:
            events.append(self._event(ctx, "api.backlog_invalid", {
                "entries": [{"id": i, "reasons": r} for i, r in backlog.invalid[:10]]}))
        self._follow_up(ctx, rec, events)
        self._resubmit(ctx, rec, events, seen)
        self._reopen(ctx, rec, backlog, seen)
        if not self._busy(rec):
            self._build_next(ctx, rec, backlog, events, seen)
        self.save(ctx.state_dir, rec)
        return Ok((*events, self._tally(ctx, rec, backlog, seen)))

    # ---- the record ------------------------------------------------------------------------
    @staticmethod
    def _busy(rec: dict) -> bool:
        """One product at a time: nothing new is built while one waits on the owner or on a
        resubmission."""
        return any(p.get("state") in WAITING_ON_OWNER | RESUBMIT
                   for p in rec["products"].values())

    @staticmethod
    def _entry(backlog, pid):
        return next((e for e in backlog.entries if e["id"] == pid), None)

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
        branch is left in the way; otherwise the two commands that clear it are reported."""
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
            rec["products"][pid] = {"id": pid, "name": entry["name"], "state": "queued",
                                    "attempts": [], "retry_seen": retry}

    # ---- 3. the next product ---------------------------------------------------------------
    def _build_next(self, ctx: WorkContext, rec: dict, backlog, events: list,
                    seen: dict) -> None:
        for entry in backlog.entries:
            p = rec["products"].get(entry["id"])
            if p is not None and p.get("state") != "queued":
                continue
            if p is None:
                p = rec["products"][entry["id"]] = {
                    "id": entry["id"], "name": entry["name"], "state": "queued",
                    "attempts": [], "retry_seen": int(entry.get("retry") or 0)}
            self._build(ctx, rec, entry, p, events, seen)
            return

    def _build(self, ctx: WorkContext, rec: dict, entry: dict, p: dict, events: list,
               seen: dict) -> None:
        attempts = p["attempts"]
        last = attempts[-1] if attempts else None
        reasons = list(last.get("reasons") or []) if last and last.get("outcome") != "valid" \
            else []
        for _ in range(ATTEMPTS_PER_RUN):
            if len(attempts) >= MAX_ATTEMPTS:
                break
            got = ctx.build_site(build_prompt(entry, reasons), BUILD_TIMEOUT)
            if isinstance(got, Err) and getattr(got.error, "waits", False):
                seen["waiting"] = _clip(getattr(got.error, "message", str(got.error)), 200)
                log.info("%s: not building %s now: %s", self.worker_id, entry["id"],
                         seen["waiting"])
                events.append(self._event(ctx, "api.waiting", {
                    "id": entry["id"], "why": seen["waiting"]}))
                return
            if not isinstance(got, Ok):
                reasons = [_clip(getattr(got.error, "message", str(got.error)), 200)]
                attempts.append({"at": ctx.now, "outcome": "error", "reasons": reasons})
                continue
            files, problems = parse_build(got.value)
            reasons = list(problems) + (check_build(entry, files) if files is not None else [])
            if files is None or reasons:
                reasons = [_clip(r, 200) for r in reasons[:12]]
                attempts.append({"at": ctx.now, "outcome": "invalid", "reasons": reasons})
                log.warning("%s: the build of %s failed the checks: %s", self.worker_id,
                            entry["id"], "; ".join(reasons[:4]))
                continue
            attempts.append({"at": ctx.now, "outcome": "valid", "reasons": []})
            try:
                staged = stage(ctx.scrooge_repo, Path(ctx.apibuilds_dir) / "worktrees",
                               entry, files)
            except StageError as exc:
                self._settle(ctx, p, "failed", f"it could not be staged: {exc}", events)
                return
            p.update(state="unsubmitted", commit=staged.commit, branch=staged.branch,
                     worktree=str(staged.worktree), files=list(staged.files),
                     diff_stat=staged.stat[-600:], staged_at=ctx.now)
            log.info("%s: %s staged on %s at %s", self.worker_id, entry["id"], staged.branch,
                     staged.commit[:10])
            events.append(self._event(ctx, "api.staged", {
                "id": entry["id"], "branch": staged.branch, "commit": staged.commit[:10],
                "files": list(staged.files)}, names=(entry["name"],)))
            self._submit(ctx, rec, p, events, seen)
            return
        if len(attempts) >= MAX_ATTEMPTS:
            self._settle(ctx, p, "shelved", "; ".join(reasons[:3]) or "it never passed the "
                         f"checks in {MAX_ATTEMPTS} tries", events)

    # ---- events and the tally --------------------------------------------------------------
    def _settle(self, ctx: WorkContext, p: dict, state: str, why: str, events: list) -> None:
        p.update(state=state, why=_clip(why, 300), settled_at=ctx.now)
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

        queued = [e["id"] for e in backlog.entries
                  if products.get(e["id"], {}).get("state", "queued") == "queued"]
        figures = [
            Figure(len(queued), "count", "API products queued in the backlog", window="now"),
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
                           payload={"queued": queued[:10],
                                    "waiting_on_owner": of("pending", *RESUBMIT),
                                    "verified": of("verified"), "red": of("red"),
                                    "shelved": of("shelved"), "denied": of("denied"),
                                    "failed": of("failed"),
                                    "needs_cleanup_before_retry": seen["cleanup"][:5],
                                    "waiting_to_build": seen["waiting"],
                                    "verify_not_set_up": seen["not_set_up"],
                                    "backlog_seeded": backlog.seeded,
                                    "unvalidated_ideas": sum(
                                        1 for e in backlog.entries if not e.get("validated"))},
                           figures=figures, entities=self._entities(()),
                           provenance={"source": "real", "provider": self.provider,
                                       "record": self.record_what})
