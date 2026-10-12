"""The self-improvement worker (``builds.improve``): detect, write, verify, review, ask Ian.

Pionir (the Daedalus job, its task record, the parked ``improve.apply``), the contained test runs
and Claude are fakes; git is real, in temporary folders: a small stand-in "Pionir repo" is cloned
into a temporary sandbox exactly as the worker clones the real one, and "Daedalus" commits into
the clone the way its policy does. No model is called, nothing runs as another user.

Each test names the rule it holds; reverting the rule fails it: the Etsy fixture's clusters,
dedupe, the nightly cap of 2, the GPU yield (the build window, a build in flight, the rest, one
job at a time, a busy GPU is waiting), the shared Claude cap, the hard refusals before review,
the one repair, NOT SET UP without the sandbox (and never needing node), and the amber pulse.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime
from functools import partial
from pathlib import Path

from pionir.adapters.daedalus import sandbox_repo_problem
from pionir.crew import runtime
from pionir.crew.builds.worker import BUILD
from pionir.crew.escalation import ClaudeRefusal, Escalator
from pionir.crew.hands import JobOutcome
from pionir.crew.improve import detect, prompts
from pionir.crew.improve import worker as improve_worker
from pionir.crew.registry import default_registry
from pionir.crew.result import Err, Ok
from pionir.crew.store import CrewStore
from pionir.crew.worker import ErrorKind, WorkContext
from pionir.selfimprove import store
from pionir.selfimprove.runner import SuiteResult
from support import use_long_tempdir

use_long_tempdir()

APPLY = "improve.apply"
CHECK = "makes a claim the shop could be held to"
MEDICAL = f"labels[5]: {CHECK} ('medical'); customer-facing claims fail closed"
TITLE = "title: 15-140 characters (this is {n})"
BILL = "title: uses a name ('Bill'): listings name no person, place, brand or product"


def at(day: int, hour: int, minute: int = 0) -> float:
    return datetime(2026, 10, day, hour, minute).timestamp()


def git(cwd, *args) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "core.autocrlf=false", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def write(repo: Path, files: dict) -> None:
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")


def commit(repo: Path, files: dict, message="change") -> str:
    write(repo, files)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


RULES = (f'"""Etsy listing rules."""\n\n\ndef check(label):\n'
         f'    if "medical" in label:\n        return "{CHECK}"\n    return None\n')
REPO = {
    "src/pionir/__init__.py": "",
    "src/pionir/crew/__init__.py": "",
    "src/pionir/crew/etsy/__init__.py": "",
    "src/pionir/crew/etsy/rules.py": RULES,
    "src/pionir/approvals.py": '"""The approval gate."""\n',
    "tests/test_rules.py": ("import unittest\n\nfrom pionir.crew.etsy import rules\n\n\n"
                            "class T(unittest.TestCase):\n    def test_a(self):\n"
                            "        self.assertIsNone(rules.check('x'))\n"
                            "        self.assertTrue(rules.check('medical'))\n"),
}


def etsy_rows(now: float) -> list:
    """The fixture of 2026-10-12: 26 drafts blocked by the maker's own checks - 12 for the
    'medical' label, 10 for the title length (each a different length), 2 for 'Bill', 2 odd."""
    rows = []

    def row(i, reasons, keyword):
        rows.append({"worker_id": "etsy.digital", "kind": "etsy.blocked",
                     "observed_at": now - 3600 * (i + 1),
                     "payload": {"keyword": keyword, "reasons": reasons}})
    for i in range(12):
        row(i, [MEDICAL], f"debt planner {i}")
    for i in range(10):
        row(12 + i, [TITLE.format(n=200 + 7 * i)], f"budget planner {i}")
    for i in range(2):
        row(22 + i, [BILL], "bill tracker")
    row(24, ["description: too short"], "odd one")
    row(25, ["tags: 14 tags (at most 13)"], "odd two")
    return rows


class FakeSetup:
    def __init__(self, root: Path) -> None:
        self.sandbox_root = root
        self.runs_dir = root / ".runs"
        self.node = None                 # node is never needed here
        self.node_tools = None


class FakePionir:
    def __init__(self) -> None:
        self.jobs: list = []
        self.tasks: dict = {}
        self.approvals: dict = {}
        self.submit = None

    def job(self, job):
        self.jobs.append(job)
        if job.capability == BUILD:
            if self.submit is not None:
                return self.submit(job)
            tid = f"t-{len(self.builds()) - 1}"
            self.tasks[tid] = JobOutcome("running", BUILD, task_id=tid)
            return JobOutcome("running", BUILD, task_id=tid)
        if job.capability == APPLY:
            aid = f"a-{len(self.applies())}"
            self.approvals[aid] = {"status": "pending"}
            return JobOutcome("pending_approval", APPLY, task_id="t-apply", approval_id=aid)
        if job.capability == "coding.daedalus_build_cancel":
            return JobOutcome("done", job.capability, result={"ok": True})
        raise AssertionError(f"unexpected capability {job.capability}")

    def task(self, capability, task_id):
        return self.tasks[task_id]

    def approval(self, approval_id):
        return self.approvals.get(approval_id, {"status": "unknown"})

    def builds(self) -> list:
        return [j for j in self.jobs if j.capability == BUILD]

    def applies(self) -> list:
        return [j for j in self.jobs if j.capability == APPLY]

    def finish(self, tid, **output) -> None:
        self.tasks[tid] = JobOutcome("done", BUILD, task_id=tid,
                                     result={"ok": True, "passed": True, **output})

    def fail(self, tid, error_type, error="it failed") -> None:
        self.tasks[tid] = JobOutcome("failed", BUILD, task_id=tid, error=error,
                                     error_type=error_type)


def approve(**checks) -> str:
    base = {c: True for c in prompts.REVIEW_CHECKS}
    base.update(checks)
    return json.dumps({"verdict": "approve", "checks": base, "reasons": [],
                       "summary": "It stops the false alarm on the word medical."})


def reject(why="the fix hides the failure instead of fixing it") -> str:
    checks = {c: c != "fixes_the_evidence" for c in prompts.REVIEW_CHECKS}
    return json.dumps({"verdict": "reject", "checks": checks, "reasons": [why]})


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._t = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self._t.name)
        self.root = root
        self.state = root / "state"
        self.sandbox = root / "daedalus-work"
        self.sandbox.mkdir()
        self.improve = root / "improve"
        self.repo = root / "Pionir"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        self.main = commit(self.repo, REPO, "seed")
        self.worker = default_registry().require("builds.improve")
        # one candidate a night unless a test is about the cap, so each test follows one
        self.worker.per_night = 1
        self.setup = FakeSetup(self.sandbox)
        self.configured = True
        self.worker.load_sandbox = lambda r: (self.setup, None) if self.configured else (
            None, "not configured: run tools\\setup-build-sandbox.ps1 once, as administrator")
        self.worker.auth_compat = lambda: False
        self.suites: list = []
        self.base_fails: list = []
        self.worker.run_suite = self.fake_suite
        self.pionir = FakePionir()
        self.rows = etsy_rows(at(5, 6))
        self.answers: list = []
        self.prompts: list = []
        self.review_fn = self.review

    def tearDown(self) -> None:
        self._t.cleanup()

    # ---- the fakes ---------------------------------------------------------------------------
    def fake_suite(self, files, *, setup, timeout, modules=()):
        self.assertIs(setup, self.setup)
        self.suites.append(list(modules))
        fixed = b"FIXED" in files.get("src/pionir/crew/etsy/rules.py", b"")
        if modules:
            return SuiteResult(True, 1, [] if fixed else [f"{modules[0]}.T.test_it"], 0, fixed)
        return SuiteResult(True, 12, list(self.base_fails), 0, not self.base_fails)

    def review(self, prompt, timeout=None):
        self.prompts.append(prompt)
        if not self.answers:
            return Err(ClaudeRefusal("budget", "the night's Claude cap is spent"))
        nxt = self.answers.pop(0)
        return nxt if isinstance(nxt, (Ok, Err)) else Ok(nxt)

    def ctx(self, now) -> WorkContext:
        return WorkContext(now=now, http=None, secrets_dir=self.root, job=self.pionir.job,
                           approval=self.pionir.approval, state_dir=self.state,
                           review=self.review_fn, task=self.pionir.task,
                           builds_sandbox=self.sandbox,
                           refusals=lambda since, kinds: [r for r in self.rows
                                                          if r["observed_at"] > since],
                           failed_runs=lambda since: [], pionir_repo=self.repo,
                           improve_dir=self.improve, crew_log=None, cortex_path=None,
                           approvals_path=None)

    def run_at(self, now, ok=True):
        result = self.worker.run(self.ctx(now))
        if ok:
            self.assertIsInstance(result, Ok, result)
        return result

    def record(self) -> dict:
        return json.loads((self.state / "builds.improve.json").read_text(encoding="utf-8"))

    def cands(self) -> list:
        return sorted(self.record()["candidates"].values(), key=lambda c: c["created_at"])

    def daedalus(self, files=None, *, fixed=True, test=True, index=-1) -> str:
        """What Daedalus commits for the job ``index``: the regression test and the fix."""
        job = self.pionir.builds()[index]
        repo = Path(job.payload["repo"])
        cid = next(c["id"] for c in self.record()["candidates"].values()
                   if c.get("repo") == str(repo))
        change = {}
        if test:
            change[prompts.regression_file(cid)] = (
                "import unittest\n\nfrom pionir.crew.etsy import rules\n\n\n"
                "class T(unittest.TestCase):\n    def test_it(self):\n"
                "        self.assertIsNone(rules.check('medical'))\n")
        if fixed:
            change["src/pionir/crew/etsy/rules.py"] = RULES.replace(
                'if "medical" in label', 'if "medical" in label and False  # FIXED')
        change.update(files or {})
        head = commit(repo, change, "Daedalus: fix")
        self.pionir.finish(f"t-{len(self.pionir.builds()) - 1 if index == -1 else index}",
                           commit=head)
        return head

    def through_review(self, day=5, start=(7, 40)) -> None:
        """After Daedalus landed: the four verification steps and the review, a run each."""
        h, m = start
        for k in range(5):
            self.run_at(at(day, h, m) + 300 * k)


class DetectTests(unittest.TestCase):
    def test_the_etsy_fixture_clusters_into_its_two_false_alarms(self) -> None:
        now = at(12, 9)
        clusters = detect.cluster(detect.from_outputs(etsy_rows(now)), threshold=5,
                                  since=now - 7 * 86400)
        got = {(c.worker, c.reason): c.count for c in clusters}
        self.assertEqual(got, {
            ("etsy.digital", detect.normalise(MEDICAL)): 12,
            ("etsy.digital", "title: N-N characters (this is N)"): 10})
        top = clusters[0].evidence(7)
        self.assertEqual(top["count"], 12)
        self.assertEqual(len(top["samples"]), 3)
        self.assertIn("('medical')", top["samples"][0])
        self.assertIn("debt planner 0", top["samples"][0])          # newest first
        self.assertLess(top["first_seen_at"], top["last_seen_at"])
        # 'Bill' was blocked twice: below the threshold, never proposed
        self.assertFalse(any("bill" in c.reason for c in clusters))

    def test_digits_fold_but_quoted_words_do_not(self) -> None:
        self.assertEqual(detect.normalise(TITLE.format(n=241)),
                         detect.normalise(TITLE.format(n=257)))
        self.assertNotEqual(detect.normalise(MEDICAL), detect.normalise(MEDICAL.replace(
            "medical", "cure")))

    def test_old_sightings_and_its_own_failures_do_not_count(self) -> None:
        now = at(12, 9)
        rows = etsy_rows(now - 8 * 86400)               # all of it a week and a day ago
        self.assertEqual(detect.cluster(detect.from_outputs(rows), threshold=5,
                                        since=now - 7 * 86400), [])
        own = [{"worker_id": "builds.improve", "finished_at": now - 60 * i,
                "error_kind": "unavailable", "error_message": "x"} for i in range(9)]
        setup = [{"worker_id": "products.api_builder", "finished_at": now - 60 * i,
                  "error_kind": "not_configured", "error_message": "node"} for i in range(9)]
        busy = [{"worker_id": "leader.watch", "finished_at": now - 60 * i,
                 "error_kind": "no_words", "error_message": "no answer within 90 s"}
                for i in range(9)]
        self.assertEqual(detect.cluster(detect.from_runs(own + setup + busy), threshold=5,
                                        since=now - 86400), [])
        stale = "".join(f"0{i}:00:00 WARNING VITALS: fiverr.desk is stale: {i} failures since "
                        "its last success\n" for i in range(1, 8))
        self.assertEqual(detect.from_vitals(stale, at(12, 9)), [])

    def test_night_builds_that_commit_nothing_or_are_too_big_cluster(self) -> None:
        now = at(12, 9)
        products = {}
        for i in range(5):
            products[f"tool-{i}"] = {
                "attempts": [{"n": 1, "outcome": "gate_failed", "finished_at": now - 3600 * i,
                              "why": "Daedalus reported a pass but committed nothing"}],
                "reviews": [{"at": now - 3600 * i, "approved": False, "by": "our checks",
                             "reasons": [f"the product is larger than one review can read "
                                         f"({44000 + i:,} characters); keep it smaller"]}]}
        clusters = detect.cluster(detect.from_builds({"products": products}), threshold=5,
                                  since=now - 7 * 86400)
        self.assertEqual(sorted(c.count for c in clusters), [5, 5])
        self.assertTrue(all(c.worker == "builds.daedalus" for c in clusters))

    def test_ians_denials_count_but_not_expiries_or_its_own_cards(self) -> None:
        stamp = datetime.fromtimestamp(at(12, 8)).astimezone().isoformat()
        rows = [{"status": "denied", "reason": "wrong price", "capability": "etsy.create",
                 "resolved_at": stamp, "summary": "s"} for _ in range(5)]
        rows += [{"status": "denied", "reason": "expired", "capability": "blog.post",
                  "resolved_at": stamp} for _ in range(9)]
        rows += [{"status": "denied", "reason": "no", "capability": "improve.apply",
                  "resolved_at": stamp} for _ in range(9)]
        clusters = detect.cluster(detect.from_approvals(rows), threshold=5,
                                  since=at(11, 0))
        self.assertEqual([(c.worker, c.count) for c in clusters], [("etsy.create", 5)])

    def test_vitals_lines_are_dated_back_over_midnight(self) -> None:
        text = ("23:50:00 WARNING VITALS: posting.newsletter has produced NO OUTPUT: x\n"
                "00:10:00 INFO hands: something\n"
                "00:20:00 WARNING VITALS: products.api_builder is NOT CONFIGURED and cannot "
                "run: node\n")
        got = detect.from_vitals(text, at(12, 0, 30))
        self.assertEqual(len(got), 1)                     # NOT CONFIGURED is setup, not code
        self.assertEqual(datetime.fromtimestamp(got[0].at), datetime(2026, 10, 11, 23, 50))

    def test_a_recurring_lesson_counts_its_recurrences(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "memory.db"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, ts REAL, namespace TEXT, "
                        "kind TEXT, text TEXT, salience REAL, meta TEXT, active INTEGER)")
            con.execute("INSERT INTO memories (ts, namespace, kind, text, salience, meta, active)"
                        " VALUES (?,?,?,?,?,?,1)",
                        (at(1, 1), "lessons", "lesson",
                         "printify · printify.catalog failed: reported failure", 8.0,
                         json.dumps({"seen": 63, "last_seen": at(12, 1)})))
            con.commit()
            con.close()
            clusters = detect.cluster(detect.read_lessons(db, at(5, 0)), threshold=5,
                                      since=at(5, 0))
        self.assertEqual([(c.worker, c.count) for c in clusters], [("printify", 63)])

    def test_targets_are_the_files_that_hold_the_reasons_own_words(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            write(repo, REPO)
            c = detect.cluster(detect.from_outputs(etsy_rows(at(12, 9))), threshold=5,
                               since=at(1, 0))[0]
            targets = detect.find_targets(repo, c)
        self.assertEqual(targets[0], "src/pionir/crew/etsy/rules.py")
        self.assertIn("src/pionir/crew/catalogue.json", targets)
        self.assertNotIn("src/pionir/approvals.py", targets)        # never a protected one


class PipelineTests(_Case):
    def test_a_recurring_false_alarm_becomes_one_card_for_ian(self) -> None:
        self.answers.append(approve())
        self.run_at(at(5, 7, 5))
        rec = self.record()
        night = rec["nights"]["2026-10-05"]
        self.assertEqual(night["clusters"], 2)
        [c] = self.cands()
        self.assertEqual(c["state"], "writing")
        self.assertEqual(c["evidence"]["count"], 12)
        self.assertEqual(c["base"], self.main)
        # a fresh clone of main inside the sandbox, that Pionir's own sandbox rule accepts
        self.assertIsNone(sandbox_repo_problem(c["repo"], str(self.sandbox)))
        self.assertEqual(git(c["repo"], "remote"), "")
        [job] = self.pionir.builds()
        self.assertEqual(job.permissions, ("daedalus.build_sandbox",))
        self.assertIn(CHECK, job.payload["intent"])
        self.assertIn(prompts.regression_file(c["id"]), job.payload["intent"])
        self.assertIn("src/pionir/crew/etsy/rules.py", job.payload["intent"])
        self.assertIn("test_regression_", job.payload["verify"])
        self.daedalus()
        self.through_review()
        [c] = self.cands()
        self.assertEqual(c["state"], "awaiting_owner", c.get("why"))
        self.assertEqual([h["state"] for h in c["history"]],
                         ["detected", "writing", "verifying", "reviewing", "awaiting_owner"])
        self.assertEqual(len(self.prompts), 1)                      # Claude asked ONCE
        self.assertIn(CHECK, self.prompts[0])
        self.assertIn("+++ b/src/pionir/crew/etsy/rules.py", self.prompts[0])
        # the regression test failed without the change and passed with it; both full suites
        self.assertEqual(c["verify"]["without"]["ok"], False)
        self.assertEqual(c["verify"]["with"]["ok"], True)
        self.assertEqual(sum(1 for s in self.suites if not s), 2)
        [ask] = self.pionir.applies()
        self.assertEqual(set(ask.payload), {"id", "patch_sha256"})
        patch, digest, card = store.read_patch(self.improve, c["id"])
        self.assertEqual(ask.payload["patch_sha256"], digest)
        self.assertIn("FIXED", patch)
        self.assertEqual(card["base"], self.main)
        self.assertTrue(card["verdict"]["approved"])
        self.assertEqual(card["evidence"]["count"], 12)
        self.assertEqual(self.record()["counts"]["proposals"], 1)
        # the owner's repo was only read: still at its seed, untouched
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.main)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_ians_answer_is_folded_in(self) -> None:
        self.answers.append(approve())
        self.run_at(at(5, 7, 5))
        self.daedalus()
        self.through_review()
        [c] = self.cands()
        self.pionir.approvals[c["approval_id"]] = {"status": "denied", "reason": "not now"}
        self.run_at(at(5, 9, 0))
        [c] = self.cands()
        self.assertEqual(c["state"], "denied")
        self.assertIn("not now", c["why"])

    def test_an_applied_outcome_then_an_undo(self) -> None:
        self.answers.append(approve())
        self.run_at(at(5, 7, 5))
        self.daedalus()
        self.through_review()
        [c] = self.cands()
        where = store.folder(self.improve, c["id"])
        store.write_json(where / store.OUTCOME, {"applied": True, "commit": "a" * 40,
                                                 "patch_sha256": c["patch_sha256"]})
        self.run_at(at(5, 9, 0))
        self.assertEqual(self.cands()[0]["state"], "applied")
        store.write_json(where / store.UNDONE, {"undone": True, "revert_commit": "b" * 40})
        self.run_at(at(5, 9, 5))
        self.assertEqual(self.cands()[0]["state"], "undone")
        self.assertEqual(self.record()["counts"]["applied"], 1)


class NightlyCapTests(_Case):
    def test_at_most_two_candidates_start_a_night(self) -> None:
        self.worker.per_night = 2                       # the catalogue's setting
        self.rows += [{"worker_id": "posting.blog", "kind": "content.blocked",
                       "observed_at": at(5, 5) - i, "payload": {"reasons": ["names 'X'"]}}
                      for i in range(6)]
        t = at(5, 7, 5)
        while t < at(5, 10, 55):
            self.run_at(t)
            for tid, out in list(self.pionir.tasks.items()):
                if out.status == "running":       # Daedalus's gate fails every time
                    self.pionir.tasks[tid] = JobOutcome("done", BUILD, task_id=tid, result={
                        "ok": True, "passed": False, "gate_reason": "tests failed"})
            t += 300
        rec = self.record()
        self.assertEqual(len(rec["nights"]["2026-10-05"]["started"]), 2)
        self.assertEqual(len(self.pionir.builds()), 4)               # a build and a repair each
        self.assertEqual([c["state"] for c in self.cands()], ["shelved", "shelved"])
        # the next night the third cluster gets its turn; the shelved two rest
        self.run_at(at(6, 7, 5))
        cands = self.cands()
        self.assertEqual(len(cands), 3)
        self.assertNotIn(cands[2]["key"], {c["key"] for c in cands[:2]})


class DedupeTests(_Case):
    def test_an_open_or_denied_cluster_is_not_proposed_again(self) -> None:
        self.answers.append(approve())
        self.run_at(at(5, 7, 5))
        self.daedalus()
        self.through_review()
        [c] = self.cands()
        key = c["key"]
        rec = self.record()
        self.assertIn("still open", self.worker.eligible_why(rec, key, at(6, 7)))
        # the next night picks a DIFFERENT cluster while this one waits on Ian
        self.run_at(at(6, 7, 5))
        cands = self.cands()
        self.assertEqual(len(cands), 2)
        self.assertNotEqual(cands[1]["key"], key)
        self.assertIn(key, self.record()["nights"]["2026-10-06"]["skipped"])
        # denied: rests 30 days, then may come back
        rec = self.record()
        rec["candidates"][c["id"]].update(state="denied", ended_at=at(6, 8))
        self.assertIn("30-day rest", self.worker.eligible_why(rec, key, at(20, 7)))
        self.assertIsNone(self.worker.eligible_why(rec, key, at(6, 8) + 31 * 86400))
        rec["candidates"][c["id"]].update(state="shelved")
        self.assertIn("14-day rest", self.worker.eligible_why(rec, key, at(10, 7)))
        self.assertIsNone(self.worker.eligible_why(rec, key, at(6, 8) + 15 * 86400))


class FreshnessTests(_Case):
    def test_a_cluster_not_seen_for_two_days_may_already_be_fixed(self) -> None:
        self.rows = etsy_rows(at(2, 6))                # all of it 3 to 4 days old
        self.run_at(at(5, 7, 5))
        night = self.record()["nights"]["2026-10-05"]
        self.assertEqual(night["clusters"], 2)
        self.assertEqual(night["eligible"], [])
        self.assertTrue(all("may already be fixed" in why for why in night["skipped"].values()))
        self.assertEqual(self.pionir.builds(), [])


class GpuYieldTests(_Case):
    def test_nothing_starts_outside_its_window_or_when_the_job_cannot_fit(self) -> None:
        self.run_at(at(5, 6, 30))                      # the night builds' time
        self.run_at(at(5, 10, 30))                     # a 60-minute job would outlive 11:00
        self.assertEqual(self.pionir.builds(), [])

    def test_a_night_build_in_flight_keeps_the_card(self) -> None:
        self.state.mkdir(parents=True)
        (self.state / "builds.daedalus.json").write_text(json.dumps(
            {"active": {"slug": "tool", "task_id": "t-9"}}), encoding="utf-8")
        self.run_at(at(5, 7, 5))
        self.assertEqual(self.pionir.builds(), [])
        (self.state / "builds.daedalus.json").write_text(json.dumps({"active": None}),
                                                         encoding="utf-8")
        self.run_at(at(5, 7, 10))
        self.assertEqual(len(self.pionir.builds()), 1)

    def test_no_contained_run_while_a_neighbours_build_waits_for_its_check(self) -> None:
        self.answers.append(approve())
        self.run_at(at(5, 7, 5))
        self.daedalus()
        self.run_at(at(5, 7, 40))                      # landed, checked
        (self.state / "products.api_builder.json").write_text(json.dumps(
            {"active": None, "products": {"x": {"state": "built"}}}), encoding="utf-8")
        before = len(self.suites)
        self.run_at(at(5, 7, 45))
        self.run_at(at(5, 7, 50))
        self.assertEqual(len(self.suites), before)    # its run would kill theirs: it waits
        (self.state / "products.api_builder.json").write_text(json.dumps(
            {"active": None, "products": {"x": {"state": "staged"}}}), encoding="utf-8")
        self.run_at(at(5, 7, 55))
        self.assertGreater(len(self.suites), before)

    def test_one_job_at_a_time_and_moss_gets_the_card_between_jobs(self) -> None:
        self.run_at(at(5, 7, 5))
        self.run_at(at(5, 7, 10))
        self.assertEqual(len(self.pionir.builds()), 1)           # still in flight: no second
        self.pionir.tasks["t-0"] = JobOutcome("done", BUILD, task_id="t-0", result={
            "ok": True, "passed": False, "gate_reason": "tests failed"})
        self.run_at(at(5, 7, 15))                     # settles: the repair waits for the rest
        self.assertEqual(len(self.pionir.builds()), 1)
        self.run_at(at(5, 7, 20))
        self.assertEqual(len(self.pionir.builds()), 1)
        self.run_at(at(5, 7, 26))                     # 10 minutes after: the repair starts
        self.assertEqual(len(self.pionir.builds()), 2)
        self.assertIn("A FIRST ATTEMPT WAS REFUSED", self.pionir.builds()[1].payload["intent"])

    def test_a_busy_gpu_is_waiting_never_an_attempt(self) -> None:
        self.pionir.submit = lambda job: JobOutcome(
            "failed", BUILD, error="GPU is leased by another Pionir-compatible process",
            error_type="ResourceUnavailable")
        self.run_at(at(5, 7, 5))
        [c] = self.cands()
        self.assertEqual(c["state"], "writing")
        self.assertEqual(c["repairs"], 0)
        self.assertEqual(c["attempts"][-1]["outcome"], "not_started")
        self.assertIsNone(self.record()["active"])

    def test_it_never_asks_the_crews_model_for_words(self) -> None:
        ctx = self.ctx(at(5, 7, 5))
        ctx.words = lambda *a, **k: self.fail("the worker asked the brain for words")
        self.assertIsInstance(self.worker.run(ctx), Ok)


class ClaudeCapTests(_Case):
    def test_it_is_overnight_work_on_the_shared_caps(self) -> None:
        self.assertIn("self_improver", runtime.NIGHT_IMPLS)
        crew_store = CrewStore(self.root / "crew.db")
        self.addCleanup(crew_store.close)
        clock = [at(5, 7)]
        asked: list = []

        class Shares:
            def cap(self, resource, division):
                return 100

        esc = Escalator(crew_store, Shares(), runner=lambda p, t: "x",
                        review_runner=lambda p, t: asked.append(p) or approve(),
                        daily_cap=10, night_cap=3, clock=lambda: clock[0])
        for _ in range(3):                              # the night builds spent the night cap
            self.assertIsInstance(esc.review("builds", "night build review", night=True), Ok)
        self.review_fn = partial(esc.review, "builds", night=True)
        self.run_at(at(5, 7, 5))
        self.daedalus()
        self.through_review()
        [c] = self.cands()
        self.assertEqual(c["state"], "reviewing")                 # waits; never approved
        self.assertIn("waiting for Claude's review", c["waiting"])
        self.assertEqual(self.pionir.applies(), [])
        self.assertEqual(len(asked), 3)                           # Claude was not asked
        clock[0] = at(5, 13)                                      # a new night (noon to noon)
        self.run_at(at(5, 13))
        self.assertEqual(self.cands()[0]["state"], "awaiting_owner")
        self.assertEqual(len(asked), 4)


class RefusalTests(_Case):
    def test_a_protected_file_is_refused_before_any_review_and_never_repaired(self) -> None:
        self.run_at(at(5, 7, 5))
        self.daedalus({"src/pionir/approvals.py": '"""The approval gate - relaxed."""\n'})
        self.through_review()
        [c] = self.cands()
        self.assertEqual(c["state"], "shelved")
        self.assertIn("approval gate", c["why"])
        self.assertEqual(self.prompts, [])
        self.assertEqual(len(self.pionir.builds()), 1)
        self.assertEqual(self.suites, [])                # nothing it wrote was even run

    def test_a_weakened_test_is_refused_before_any_review(self) -> None:
        self.run_at(at(5, 7, 5))
        self.daedalus({"tests/test_rules.py": REPO["tests/test_rules.py"].replace(
            "        self.assertTrue(rules.check('medical'))\n", "")})
        self.through_review()
        [c] = self.cands()
        self.assertEqual(c["state"], "shelved")
        self.assertIn("assert", c["why"])
        self.assertEqual(self.prompts, [])

    def test_a_regression_test_that_passes_without_the_change_is_not_one(self) -> None:
        self.run_at(at(5, 7, 5))
        self.base_fails = []
        orig = self.fake_suite
        self.worker.run_suite = lambda files, **kw: SuiteResult(True, 1, [], 0, True) \
            if kw.get("modules") else orig(files, **kw)
        self.daedalus()
        self.through_review()
        [c] = self.cands()
        self.assertEqual(c["state"], "writing")                    # its one repair
        self.assertIn("PASSES without the change", c["why"])
        self.assertEqual(self.prompts, [])

    def test_a_new_failure_in_the_full_suite_is_refused(self) -> None:
        self.run_at(at(5, 7, 5))
        self.daedalus()
        calls = []
        orig = self.fake_suite

        def suite(files, **kw):
            got = orig(files, **kw)
            if not kw.get("modules"):
                calls.append(1)
                if len(calls) == 2:                     # the change's run, after main's
                    return SuiteResult(True, 12, ["test_x.T.test_y"], 0, False)
            return got
        self.worker.run_suite = suite
        self.through_review()
        [c] = self.cands()
        self.assertEqual(c["state"], "writing")
        self.assertIn("test_x.T.test_y", c["why"])

    def test_a_rejected_review_gets_one_repair_then_shelves(self) -> None:
        self.answers += [reject("first"), reject("second")]
        self.run_at(at(5, 7, 5))
        self.daedalus()
        self.through_review()
        [c] = self.cands()
        self.assertEqual(c["state"], "writing")
        self.run_at(at(5, 8, 10))                        # the repair (after the rest)
        self.assertEqual(len(self.pionir.builds()), 2)
        self.assertIn("first", self.pionir.builds()[1].payload["intent"])
        self.daedalus({"src/pionir/crew/etsy/rules.py": RULES.replace(
            'if "medical" in label', 'if "medical" in label and None  # FIXED')})
        self.through_review(start=(8, 20))
        [c] = self.cands()
        self.assertEqual(c["state"], "shelved")
        self.assertEqual(len(self.prompts), 2)
        self.assertEqual(len(self.pionir.builds()), 2)
        self.assertEqual(self.pionir.applies(), [])

    def test_daedalus_committing_nothing_new_is_a_failed_attempt(self) -> None:
        self.run_at(at(5, 7, 5))
        self.pionir.finish("t-0", commit="")
        self.run_at(at(5, 7, 30))
        [c] = self.cands()
        self.assertEqual(c["state"], "writing")
        self.assertIn("committed nothing new", c["why"])


class SetupTests(_Case):
    def test_without_the_sandbox_it_is_not_set_up_and_does_nothing(self) -> None:
        self.configured = False
        got = self.run_at(at(5, 7, 5), ok=False)
        self.assertIsInstance(got, Err)
        self.assertEqual(got.error.kind, ErrorKind.NOT_CONFIGURED)
        self.assertIn("NOT SET UP", got.error.message)
        self.assertIn("setup-build-sandbox.ps1", got.error.message)
        self.assertEqual(self.pionir.jobs, [])
        self.assertEqual(list(self.sandbox.iterdir()), [])

    def test_auth_compat_on_is_not_set_up(self) -> None:
        self.worker.auth_compat = lambda: True
        got = self.run_at(at(5, 7, 5), ok=False)
        self.assertIn("PIONIR_AUTH_COMPAT", got.error.message)
        self.assertEqual(self.pionir.jobs, [])

    def test_it_never_needs_node(self) -> None:
        self.assertIsNone(self.setup.node)
        self.assertIsNone(self.worker.readiness(self.root))
        self.run_at(at(5, 7, 5))
        self.assertEqual(len(self.pionir.builds()), 1)


class AmberTests(_Case):
    def test_clusters_every_night_and_no_proposal_turns_it_amber(self) -> None:
        self.state.mkdir(parents=True)
        nights = {f"2026-10-0{d}": {"start": at(d, 7), "end": at(d, 11), "clusters": 2,
                                    "eligible": [], "skipped": {}, "started": [],
                                    "why_none": "every cluster was already proposed"}
                  for d in (2, 3, 4)}
        (self.state / "builds.improve.json").write_text(json.dumps({"nights": nights}),
                                                        encoding="utf-8")
        self.run_at(at(4, 12))
        pulse = self.worker.pulse(self.state, at(4, 12))
        self.assertIn("proposed nothing", pulse["alert"])
        self.assertIn("2, 2, 2", pulse["alert"])
        # one proposal in those nights clears it
        rec = self.record()
        rec["candidates"]["imp-20261004-abcd"] = {
            "id": "imp-20261004-abcd", "night": "2026-10-04", "state": "awaiting_owner",
            "created_at": at(4, 7), "evidence": {"worker": "w", "reason": "r"},
            "history": [{"state": "awaiting_owner"}]}
        (self.state / "builds.improve.json").write_text(json.dumps(rec), encoding="utf-8")
        self.run_at(at(4, 12, 5))
        self.assertIsNone(self.worker.pulse(self.state, at(4, 12, 5))["alert"])

    def test_a_quiet_estate_is_not_amber(self) -> None:
        self.rows = []
        for d in (5, 6, 7):
            self.run_at(at(d, 7, 5))
        self.run_at(at(7, 12))
        self.assertIsNone(self.worker.pulse(self.state, at(7, 12))["alert"])


class ConfigTests(unittest.TestCase):
    def test_its_window_may_not_overlap_the_night_builds(self) -> None:
        spec = default_registry().require("builds.improve")
        with self.assertRaises(ValueError):
            improve_worker.SelfImprover(spec, window="06:00-09:00")
        with self.assertRaises(ValueError):
            improve_worker.SelfImprover(spec, per_night=3)

    def test_the_catalogue_declares_it_in_the_builds_division(self) -> None:
        w = default_registry().require("builds.improve")
        self.assertEqual(w.division, "builds")
        self.assertEqual(w.per_night, 2)
        self.assertEqual(w.threshold, 5)


if __name__ == "__main__":
    unittest.main()
