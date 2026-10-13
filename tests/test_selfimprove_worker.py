"""The self-improvement worker (``builds.improve``): detect, write, freeze, verify, review, ask.

Pionir (the Daedalus job, its task record, the parked ``improve.apply``), the contained test runs
and Claude are fakes; git is real, in temporary folders: a small stand-in "Pionir repo" is cloned
into a temporary sandbox exactly as the worker clones the real one, and "Daedalus" commits into
the clone the way its policy does. No model is called, nothing runs as another user.

Each test names the rule it holds; reverting the rule fails it: the Etsy fixture's clusters and
duplicates, "needs a human" for a protected rule, dedupe and the stale rest, the nightly cap of
2, the frozen patch (a rewritten sandbox changes nothing), the GPU (only after the night builds,
Moss sidelined only while idle, one job at a time, a rest between jobs), the sandbox lease, the shared
Claude cap and the review hours, the hard refusals before review, a regression test that must
fail by an ASSERTION, the per-test baseline, the one repair, NOT SET UP, and the amber pulse.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime
from functools import partial
from pathlib import Path

from pionir import build_sandbox as bs
from pionir.adapters import daedalus as daedalus_adapter
from pionir.crew import runtime
from pionir.crew.escalation import ClaudeRefusal, Escalator
from pionir.crew.hands import JobOutcome
from pionir.crew.improve import detect, prompts
from pionir.crew.improve import sandbox as isandbox
from pionir.crew.improve import worker as improve_worker
from pionir.crew.registry import default_registry
from pionir.crew.result import Err, Ok
from pionir.crew.store import CrewStore
from pionir.crew.worker import ErrorKind, WorkContext
from pionir.sandbox_git import sandbox_repo_problem
from pionir.selfimprove import store
from pionir.selfimprove.runner import SuiteResult
from support import use_long_tempdir

use_long_tempdir()

APPLY = "improve.apply"
BUILD = improve_worker.BUILD
CHECK = "makes a claim the shop could be held to"
MEDICAL = f"labels[5]: {CHECK} ('medical'); customer-facing claims fail closed"
TITLE = "title: 15-140 characters (this is {n})"
BILL = "title: uses a name ('Bill'): listings name no person, place, brand or product"
MAKER = "src/pionir/crew/etsy/maker.py"


def at(day: int, hour: int, minute: int = 0) -> float:
    return datetime(2026, 10, day, hour, minute).timestamp()


COMMIT_DATE = {"date": "2026-10-01T00:00:00"}       # the stand-in repo's commits, by default


def git(cwd, *args) -> str:
    env = {**os.environ, "GIT_COMMITTER_DATE": COMMIT_DATE["date"],
           "GIT_AUTHOR_DATE": COMMIT_DATE["date"]}
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "core.autocrlf=false", *args], cwd=cwd, check=True,
                          capture_output=True, text=True, env=env).stdout.strip()


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


CHECKER = (f'"""Etsy listing checks (a stand-in) for etsy.digital."""\n\n\ndef check(label):\n'
           f'    if "medical" in label:\n        return "{CHECK}"\n    return None\n')
REPO = {
    "src/pionir/__init__.py": "",
    "src/pionir/crew/__init__.py": "",
    "src/pionir/crew/etsy/__init__.py": "",
    MAKER: CHECKER,
    "src/pionir/approvals.py": '"""The approval gate."""\n',
    "tests/test_rules.py": ("import unittest\n\nfrom pionir.crew.etsy import maker\n\n\n"
                            "class T(unittest.TestCase):\n    def test_a(self):\n"
                            "        self.assertIsNone(maker.check('x'))\n"
                            "        self.assertTrue(maker.check('medical'))\n"),
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
        self.reaps = 0

    def reap(self):
        self.reaps += 1


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
        if job.capability == improve_worker.CANCEL:
            return JobOutcome("done", job.capability, result={"ok": True})
        raise AssertionError(f"unexpected capability {job.capability}")

    def task(self, capability, task_id):
        assert capability == BUILD
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
        self.base_outcomes: dict = {}
        self.worker.run_suite = self.fake_suite
        self.pionir = FakePionir()
        self.rows = etsy_rows(at(5, 5))
        self.answers: list = []
        self.prompts: list = []
        self.review_fn = self.review

    def tearDown(self) -> None:
        self._t.cleanup()

    # ---- the fakes ---------------------------------------------------------------------------
    def fake_suite(self, files, *, setup, timeout, modules=()):
        self.assertIs(setup, self.setup)
        self.suites.append(list(modules))
        fixed = b"FIXED" in files.get(MAKER, b"")
        if modules:
            return SuiteResult(True, 1, {} if fixed else
                               {f"{modules[0]}.T.test_it": "failure:AssertionError"}, 0, fixed)
        return SuiteResult(True, 12, dict(self.base_outcomes), 0, not self.base_outcomes)

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
                           approvals_path=None,
                           crew_started_at=getattr(self, "crew_started", None))

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
                "import unittest\n\nfrom pionir.crew.etsy import maker\n\n\n"
                "class T(unittest.TestCase):\n    def test_it(self):\n"
                "        self.assertIsNone(maker.check('medical'))\n")
        if fixed:
            change[MAKER] = CHECKER.replace('if "medical" in label:',
                                            'if "medical" in label and False:  # FIXED')
        change.update(files or {})
        head = commit(repo, change, "Daedalus: fix")
        n = len(self.pionir.builds()) - 1 if index == -1 else index
        self.pionir.finish(f"t-{n}", commit=head)
        return head

    def through_review(self, day=5, start=(5, 50), runs=5) -> None:
        """After Daedalus landed: landing, the verification steps and the review, a run each."""
        h, m = start
        for k in range(runs):
            self.run_at(at(day, h, m) + 300 * k)


class DetectTests(unittest.TestCase):
    def test_the_etsy_fixture_clusters_into_its_false_alarms(self) -> None:
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
        self.assertFalse(any("bill" in c.reason for c in clusters))  # 2: below the threshold

    def test_digits_fold_but_quoted_words_do_not(self) -> None:
        self.assertEqual(detect.normalise(TITLE.format(n=241)),
                         detect.normalise(TITLE.format(n=257)))
        self.assertNotEqual(detect.normalise(MEDICAL), detect.normalise(MEDICAL.replace(
            "medical", "cure")))

    def test_old_sightings_its_own_failures_and_setup_do_not_count(self) -> None:
        now = at(12, 9)
        rows = etsy_rows(now - 8 * 86400)
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

    def test_one_failure_seen_from_two_sources_is_one_cluster(self) -> None:
        now = at(12, 9)
        runs = [{"worker_id": "etsy.pod", "finished_at": now - 600 * i,
                 "error_kind": "unavailable",
                 "error_message": "Printify's catalog: failed Pionir reported it did not work"}
                for i in range(9)]
        lesson = detect.Sighting("lessons", "printify",
                                 "printify · printify.catalog failed: reported failure",
                                 now - 300, "a lesson", weight=63, first=now - 86400)
        attempts = [detect.Sighting("builds", "builds.daedalus",
                                    "Daedalus reported a pass but committed nothing", now - i, "x")
                    for i in range(6)]
        echoed = [detect.Sighting("outputs", "builds.daedalus",
                                  "Daedalus reported a pass but committed nothing new",
                                  now - i, "y") for i in range(6)]
        clusters = detect.cluster(detect.from_runs(runs) + [lesson] + attempts + echoed,
                                  threshold=5, since=now - 7 * 86400)
        self.assertEqual(len(clusters), 5 - 1)     # runs, lesson, builds, outputs (two pairs)
        merged = detect.merge_duplicates(clusters)
        self.assertEqual(len(merged), 2)
        printify = next(c for c in merged if "printify" in c.reason or "printify" in c.worker)
        self.assertEqual(printify.count, 63)                    # the same events, not more
        self.assertEqual(len(printify.also), 1)

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

    def test_ians_denials_count_but_not_expiries_or_its_own_cards(self) -> None:
        stamp = datetime.fromtimestamp(at(12, 8)).astimezone().isoformat()
        rows = [{"status": "denied", "reason": "wrong price", "capability": "etsy.create",
                 "resolved_at": stamp, "summary": "s"} for _ in range(5)]
        rows += [{"status": "denied", "reason": "expired", "capability": "blog.post",
                  "resolved_at": stamp} for _ in range(9)]
        rows += [{"status": "denied", "reason": "no", "capability": "improve.apply",
                  "resolved_at": stamp} for _ in range(9)]
        clusters = detect.cluster(detect.from_approvals(rows), threshold=5, since=at(11, 0))
        self.assertEqual([(c.worker, c.count) for c in clusters], [("etsy.create", 5)])

    def test_vitals_lines_are_dated_back_over_midnight(self) -> None:
        text = ("23:50:00 WARNING VITALS: posting.newsletter has produced NO OUTPUT: x\n"
                "00:10:00 INFO hands: something\n"
                "00:20:00 WARNING VITALS: products.api_builder is NOT CONFIGURED and cannot "
                "run: node\n")
        got = detect.from_vitals(text, at(12, 0, 30))
        self.assertEqual(len(got), 1)
        self.assertEqual(datetime.fromtimestamp(got[0].at), datetime(2026, 10, 11, 23, 50))

    def test_a_recurring_lesson_counts_its_recurrences(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "memory.db"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, ts REAL, namespace TEXT, "
                        "kind TEXT, text TEXT, salience REAL, meta TEXT, active INTEGER)")
            for text in ("printify · printify.catalog failed: reported failure",
                         "printify refused a task x (policy/germline, not a fault): refused",
                         "daedalus · coding.daedalus_build failed: resourceunavailable"):
                con.execute("INSERT INTO memories (ts, namespace, kind, text, salience, meta, "
                            "active) VALUES (?,?,?,?,?,?,1)",
                            (at(1, 1), "lessons", "lesson", text, 8.0,
                             json.dumps({"seen": 63, "last_seen": at(12, 1)})))
            con.commit()
            con.close()
            clusters = detect.cluster(detect.read_lessons(db, at(5, 0)), threshold=5,
                                      since=at(5, 0))
        self.assertEqual([(c.worker, c.count) for c in clusters], [("printify", 63)])

    def test_targets_and_a_rule_only_a_human_may_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            write(repo, REPO)
            c = detect.cluster(detect.from_outputs(etsy_rows(at(12, 9))), threshold=5,
                               since=at(1, 0))[0]
            where = detect.locate(repo, c)
            self.assertEqual(where["targets"][0], MAKER)
            self.assertIn("src/pionir/crew/catalogue.json", where["targets"])
            self.assertIsNone(where["needs_human"])
            # a module that protected code imports is protected too: a human's job
            write(repo, {"src/pionir/approvals.py": "from pionir.crew.etsy import maker\n"})
            where = detect.locate(repo, c)
            self.assertIn("needs a human", where["needs_human"])
            self.assertIn(MAKER, where["needs_human"])
            write(repo, {"src/pionir/approvals.py": REPO["src/pionir/approvals.py"]})
            # the words live only in a protected rule (the real Etsy rules): a human's job
            (repo / MAKER).unlink()
            write(repo, {"src/pionir/crew/etsy/rules.py": CHECKER})
            where = detect.locate(repo, c)
            self.assertIn("needs a human", where["needs_human"])
            self.assertIn("src/pionir/crew/etsy/rules.py", where["needs_human"])
            self.assertNotIn("src/pionir/crew/etsy/rules.py", where["targets"])


class PipelineTests(_Case):
    def test_a_recurring_false_alarm_becomes_one_card_for_ian(self) -> None:
        self.answers.append(approve())
        self.run_at(at(5, 5, 46))
        rec = self.record()
        night = rec["nights"]["2026-10-05"]
        self.assertEqual(night["clusters"], 2)
        [c] = self.cands()
        self.assertEqual(c["state"], "writing")
        self.assertEqual(c["evidence"]["count"], 12)
        self.assertEqual(c["base"], self.main)
        self.assertIsNone(sandbox_repo_problem(c["repo"], str(self.sandbox)))
        self.assertEqual(git(c["repo"], "remote"), "")
        [job] = self.pionir.builds()
        self.assertEqual(job.capability, "coding.daedalus_build_quiet")
        self.assertEqual(job.permissions, ("daedalus.build_sandbox",))
        self.assertIn(CHECK, job.payload["intent"])
        self.assertIn(prompts.regression_file(c["id"]), job.payload["intent"])
        self.assertIn(MAKER, job.payload["intent"])
        self.assertLessEqual(job.payload["not_after"], at(5, 7, 30))       # done by 07:30
        self.daedalus()
        self.through_review()
        [c] = self.cands()
        self.assertEqual(c["state"], "awaiting_owner", c.get("why"))
        self.assertEqual([h["state"] for h in c["history"]],
                         ["detected", "writing", "verifying", "reviewing", "awaiting_owner"])
        self.assertEqual(len(self.prompts), 1)                      # Claude asked ONCE
        self.assertIn("+++ b/" + MAKER, self.prompts[0])
        self.assertFalse(c["verify"]["without"]["ok"])
        self.assertTrue(c["verify"]["with"]["ok"])
        self.assertEqual(sum(1 for s in self.suites if not s), 2)
        [ask] = self.pionir.applies()
        self.assertEqual(set(ask.payload), {"id", "digest"})
        data, card, digest = store.read_candidate(self.improve, c["id"])
        self.assertEqual(ask.payload["digest"], digest)
        self.assertEqual(store.sha256(data), c["patch_sha256"])
        self.assertIn(b"FIXED", data)
        self.assertEqual(card["base"], self.main)
        self.assertEqual(card["tests"], [prompts.regression_file(c["id"])])
        self.assertTrue(card["verdict"]["approved"])
        self.assertEqual(self.record()["counts"]["proposals"], 1)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.main)   # Ian's repo: read only
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_ians_answer_is_folded_in(self) -> None:
        self.answers.append(approve())
        self.run_at(at(5, 5, 46))
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
        self.run_at(at(5, 5, 46))
        self.daedalus()
        self.through_review()
        [c] = self.cands()
        where = store.folder(self.improve, c["id"])
        store.write_json(where / store.OUTCOME, {"applied": True, "commit": "a" * 40,
                                                 "digest": c["digest"]})
        self.run_at(at(5, 9, 0))
        self.assertEqual(self.cands()[0]["state"], "applied")
        store.write_json(where / store.UNDONE, {"undone": True, "revert_commit": "b" * 40})
        self.run_at(at(5, 9, 5))
        self.assertEqual(self.cands()[0]["state"], "undone")
        self.assertEqual(self.record()["counts"]["applied"], 1)

    def test_a_cluster_only_a_protected_rule_could_fix_needs_a_human(self) -> None:
        git(self.repo, "rm", "-q", MAKER)
        commit(self.repo, {"src/pionir/crew/etsy/rules.py": CHECKER}, "rules")
        self.rows = [r for r in self.rows if "medical" in str(r["payload"])]
        self.run_at(at(5, 5, 46))
        night = self.record()["nights"]["2026-10-05"]
        self.assertEqual(night["eligible"], [])
        [human] = night["needs_human"]
        self.assertIn("protected rule", human["why"])
        self.assertEqual(self.pionir.builds(), [])                 # no Daedalus run burnt
        self.assertEqual(list(self.sandbox.iterdir()), [])          # nothing cloned


class FrozenPatchTests(_Case):
    def test_rewriting_the_sandbox_after_landing_changes_nothing(self) -> None:
        self.answers.append(approve())
        self.run_at(at(5, 5, 46))
        self.daedalus()
        self.run_at(at(5, 5, 50))                     # landed: the patch is frozen now
        [c] = self.cands()
        frozen = store.read_frozen(self.improve, c["id"], c["patch_sha256"])
        # code under test rewrites the sandbox repo: a new commit, the branch moved
        commit(Path(c["repo"]), {"src/pionir/approvals.py": '"""relaxed"""\n'}, "hostile")
        self.through_review(start=(5, 55), runs=4)
        [c] = self.cands()
        self.assertEqual(c["state"], "awaiting_owner", c.get("why"))
        data, _card, _digest = store.read_candidate(self.improve, c["id"])
        self.assertEqual(data, frozen)
        self.assertNotIn(b"approvals.py", data)
        self.assertNotIn("approvals.py", self.prompts[0])

    def test_a_frozen_patch_that_changed_is_never_used(self) -> None:
        self.run_at(at(5, 5, 46))
        self.daedalus()
        self.run_at(at(5, 5, 50))
        [c] = self.cands()
        path = store.folder(self.improve, c["id"]) / store.PATCH
        path.write_bytes(path.read_bytes().replace(b"FIXED", b"EVIL!"))
        self.run_at(at(5, 5, 55))
        [c] = self.cands()
        self.assertEqual(c["state"], "shelved")
        self.assertIn("changed", c["why"])
        self.assertEqual(self.suites, [])
        self.assertEqual(self.prompts, [])


class NightlyCapTests(_Case):
    def test_at_most_two_candidates_start_a_night(self) -> None:
        self.worker.per_night = 2                       # the catalogue's setting
        self.worker.budget = 600                        # short jobs, so many fit the window
        self.worker.rest = 0
        self.rows += [{"worker_id": "posting.blog", "kind": "content.blocked",
                       "observed_at": at(5, 4) - i, "payload": {"reasons": ["names 'X'"]}}
                      for i in range(6)]
        t = at(5, 5, 46)
        while t < at(5, 6, 20):
            self.run_at(t)
            for tid, out in list(self.pionir.tasks.items()):
                if out.status == "running":       # Daedalus's gate fails every time
                    self.pionir.tasks[tid] = JobOutcome("done", BUILD, task_id=tid, result={
                        "ok": True, "passed": False, "gate_reason": "tests failed"})
            t += 60
        rec = self.record()
        self.assertEqual(len(rec["nights"]["2026-10-05"]["started"]), 2)
        self.assertEqual(len(self.pionir.builds()), 4)               # a build and a repair each
        self.assertEqual([c["state"] for c in self.cands()], ["shelved", "shelved"])
        self.run_at(at(6, 5, 46))                                     # the next night: the third
        cands = self.cands()
        self.assertEqual(len(cands), 3)
        self.assertNotIn(cands[2]["key"], {c["key"] for c in cands[:2]})


class DedupeTests(_Case):
    def test_an_open_denied_shelved_or_stale_cluster_rests(self) -> None:
        self.answers.append(approve())
        self.run_at(at(5, 5, 46))
        self.daedalus()
        self.through_review()
        [c] = self.cands()
        key = c["key"]
        rec = self.record()
        self.assertIn("still open", self.worker.eligible_why(rec, key, at(6, 7)))
        self.run_at(at(6, 5, 46))                      # the next night: a DIFFERENT cluster
        cands = self.cands()
        self.assertEqual(len(cands), 2)
        self.assertNotEqual(cands[1]["key"], key)
        self.assertIn(key, self.record()["nights"]["2026-10-06"]["skipped"])
        rec = self.record()
        cand = rec["candidates"][c["id"]]
        cand.update(state="denied", ended_at=at(6, 8))
        self.assertIn("30-day rest", self.worker.eligible_why(rec, key, at(20, 7)))
        self.assertIsNone(self.worker.eligible_why(rec, key, at(6, 8) + 31 * 86400))
        cand.update(state="shelved")
        self.assertIn("14-day rest", self.worker.eligible_why(rec, key, at(10, 7)))
        self.assertIsNone(self.worker.eligible_why(rec, key, at(6, 8) + 15 * 86400))
        cand.update(state="stale")
        self.assertIn("3-night rest", self.worker.eligible_why(rec, key, at(8, 7)))
        self.assertIsNone(self.worker.eligible_why(rec, key, at(6, 8) + 4 * 86400))
        # a duplicate seen from another source is the same cluster
        cand.update(state="writing", keys=[key, "lessons|etsy|x"])
        self.assertIn("still open", self.worker.eligible_why(rec, ["lessons|etsy|x"], at(8, 7)))


class FreshnessTests(_Case):
    def test_a_cluster_not_seen_for_two_days_may_already_be_fixed(self) -> None:
        self.rows = etsy_rows(at(2, 6))
        self.run_at(at(5, 5, 46))
        night = self.record()["nights"]["2026-10-05"]
        self.assertEqual(night["clusters"], 2)
        self.assertEqual(night["eligible"], [])
        self.assertTrue(all("may already be fixed" in why for why in night["skipped"].values()))
        self.assertEqual(self.pionir.builds(), [])


class CurrentCodeTests(_Case):
    """A cluster counts only what happened on the CURRENT code: after the crew last started and
    after the newest commit to its files."""

    def tearDown(self) -> None:
        COMMIT_DATE["date"] = "2026-10-01T00:00:00"
        super().tearDown()

    def night(self) -> dict:
        self.run_at(at(5, 5, 46))
        return self.record()["nights"]["2026-10-05"]

    def test_a_fixing_commit_drops_what_happened_before_it(self) -> None:
        # the title rows are 07:00-16:00 on the 4th; the medical rows 17:00 on the 4th to 04:00
        COMMIT_DATE["date"] = "2026-10-04T23:00:00"
        commit(self.repo, {MAKER: CHECKER + "# tightened\n"}, "fix the titles")
        night = self.night()
        keys = {e["worker"] + e["reason"]: e for e in night["eligible"]}
        title = [k for k in night["skipped"] if "this is N" in k]
        self.assertTrue(title and "may already be fixed" in night["skipped"][title[0]])
        self.assertIn("since commit", night["skipped"][title[0]])
        [medical] = keys.values()                           # still happening after the fix
        self.assertEqual(medical["count"], 5)            # 00:00-04:00 on the 5th
        self.assertIn("commit", medical["counted_since"])

    def test_without_a_fix_both_stand(self) -> None:
        self.assertEqual(len(self.night()["eligible"]), 2)

    def test_a_restart_alone_does_not_reset_the_count(self) -> None:
        self.crew_started = at(5, 3)                        # restarted at 03:00, same code
        night = self.night()
        [medical] = night["eligible"]                       # seen at 04:00 too: still happening
        self.assertEqual(medical["count"], 12)              # every occurrence still counts

    def test_a_cluster_not_seen_since_the_last_start_is_held(self) -> None:
        self.crew_started = at(5, 3)
        night = self.night()
        title = [k for k in night["skipped"] if "this is N" in k]
        self.assertIn("not seen on the running code", night["skipped"][title[0]])
        self.crew_started = at(5, 4, 30)                    # nothing at all since 04:30
        self.state.joinpath("builds.improve.json").unlink()
        night = self.night()
        self.assertEqual(night["eligible"], [])
        self.assertTrue(all("not seen on the running code" in w
                            for w in night["skipped"].values()))


class GpuTests(_Case):
    def test_nothing_starts_while_the_night_builds_could_still_build(self) -> None:
        self.worker.window = improve_worker.Window.parse("03:00-06:30")
        self.run_at(at(5, 3, 30))                      # 03:30 + 75 min fits before 07:00
        self.assertEqual(self.pionir.builds(), [])
        self.run_at(at(5, 5, 46))                      # it no longer does: the builds are done
        self.assertEqual(len(self.pionir.builds()), 1)

    def test_nothing_starts_outside_its_window_or_when_it_cannot_finish_by_0730(self) -> None:
        self.run_at(at(5, 7, 45))
        self.run_at(at(5, 7, 0))                       # 07:00 + 40 min is past 07:30
        self.assertEqual(self.pionir.builds(), [])

    def test_a_typical_night_starts_once_the_builds_finish(self) -> None:
        self.state.mkdir(parents=True)
        builds = self.state / "builds.daedalus.json"
        builds.write_text(json.dumps({"active": {"slug": "tool", "task_id": "t-9"}}),
                          encoding="utf-8")
        t = at(5, 5, 45)
        while t < at(5, 6, 10):                        # the last build runs until 06:10
            self.run_at(t)
            t += 300
        self.assertEqual(self.pionir.builds(), [])
        builds.write_text(json.dumps({"active": None, "products": {"x": {"state": "staged"}}}),
                          encoding="utf-8")
        self.run_at(at(5, 6, 15))
        self.assertEqual(len(self.pionir.builds()), 1)
        self.assertLessEqual(self.pionir.builds()[0].payload["not_after"], at(5, 7, 30))

    def test_builds_busy_past_the_last_start_skip_the_night_saying_so(self) -> None:
        self.state.mkdir(parents=True)
        (self.state / "builds.daedalus.json").write_text(json.dumps(
            {"active": {"slug": "tool", "task_id": "t-9"}}), encoding="utf-8")
        t = at(5, 5, 45)
        while t <= at(5, 7, 20):
            self.run_at(t)
            t += 300
        self.assertEqual(self.pionir.builds(), [])
        why = self.record()["nights"]["2026-10-05"]["why_none"]
        self.assertIn("night builds were still busy at 06:50", why)

    def test_a_night_build_in_flight_or_waiting_keeps_the_card(self) -> None:
        self.state.mkdir(parents=True)
        builds = self.state / "builds.daedalus.json"
        builds.write_text(json.dumps({"active": {"slug": "tool", "task_id": "t-9"}}),
                          encoding="utf-8")
        self.run_at(at(5, 5, 46))
        self.assertEqual(self.pionir.builds(), [])
        builds.write_text(json.dumps({"active": None, "products": {"x": {"state": "built"}}}),
                          encoding="utf-8")
        self.run_at(at(5, 5, 47))
        self.assertEqual(self.pionir.builds(), [])
        builds.write_text(json.dumps({"active": None, "products": {"x": {"state": "staged"}}}),
                          encoding="utf-8")
        self.run_at(at(5, 5, 48))
        self.assertEqual(len(self.pionir.builds()), 1)

    def test_one_job_at_a_time_and_moss_gets_the_card_between_jobs(self) -> None:
        self.worker.budget = 600
        self.run_at(at(5, 5, 46))
        self.run_at(at(5, 5, 48))
        self.assertEqual(len(self.pionir.builds()), 1)           # still in flight: no second
        self.pionir.tasks["t-0"] = JobOutcome("done", BUILD, task_id="t-0", result={
            "ok": True, "passed": False, "gate_reason": "tests failed"})
        self.run_at(at(5, 5, 50))                     # settles: the repair waits for the rest
        self.run_at(at(5, 5, 55))
        self.assertEqual(len(self.pionir.builds()), 1)
        self.run_at(at(5, 6, 1))                      # 10 minutes after: the repair starts
        self.assertEqual(len(self.pionir.builds()), 2)
        self.assertIn("A FIRST ATTEMPT WAS REFUSED", self.pionir.builds()[1].payload["intent"])

    def test_moss_active_skips_the_night_and_says_why(self) -> None:
        self.worker.budget = 600
        self.pionir.submit = lambda job: JobOutcome(
            "failed", BUILD, error="coding.daedalus_build_quiet: not tonight - Moss talked "
                                   "with Ian 4 minute(s) ago (she must be quiet for 30 minutes "
                                   "first); Moss's model is never sidelined while she is active",
            error_type="ResourceUnavailable")
        self.run_at(at(5, 5, 46))
        [c] = self.cands()
        self.assertEqual(c["state"], "writing")
        self.assertEqual(c["repairs"], 0)                         # never an attempt
        self.assertEqual(c["attempts"][-1]["outcome"], "not_started")
        self.assertIn("Moss talked with Ian 4 minute(s) ago", c["waiting"])
        for minute in (50, 56, 59):
            self.run_at(at(5, 5, minute))
        self.assertEqual(len(self.pionir.builds()), 1)           # tonight is skipped
        self.pionir.submit = None
        self.run_at(at(6, 5, 46))                                 # the next night it tries
        self.assertEqual(len(self.pionir.builds()), 2)

    def test_a_busy_sandbox_is_waiting_never_an_attempt(self) -> None:
        self.pionir.submit = lambda job: JobOutcome(
            "failed", BUILD, error="the build Daedalus could not start: the build sandbox is "
                                   "busy: the video tutorial runner", error_type="AdapterUnavailable")
        self.run_at(at(5, 5, 46))
        [c] = self.cands()
        self.assertEqual(c["repairs"], 0)
        self.assertEqual(c["attempts"][-1]["outcome"], "not_started")
        self.assertIsNone(self.record()["active"])

    def test_it_never_asks_the_crews_model_for_words(self) -> None:
        ctx = self.ctx(at(5, 5, 46))
        ctx.words = lambda *a, **k: self.fail("the worker asked the brain for words")
        self.assertIsInstance(self.worker.run(ctx), Ok)

    def test_its_names_are_pionirs(self) -> None:
        self.assertEqual(improve_worker.BUILD, daedalus_adapter.BUILD_QUIET)
        self.assertEqual(improve_worker.CANCEL, daedalus_adapter.CANCEL)
        self.assertEqual(improve_worker.PERMISSION, daedalus_adapter.BUILD_PERMISSION)
        adapter = daedalus_adapter.DaedalusAdapter()
        self.assertIsNotNone(adapter.settle_for(
            daedalus_adapter.Task(daedalus_adapter.BUILD_QUIET, {})))  # Moss asked first


class SandboxLeaseTests(_Case):
    def test_its_contained_run_waits_while_someone_else_has_the_sandbox(self) -> None:
        with bs.SandboxLease(self.setup, "the video tutorial runner"):
            got = isandbox.run_suite({"x.py": b"x = 1\n"}, setup=self.setup, timeout=5)
        self.assertTrue(got.busy)
        self.assertIn("video tutorial", got.tail)
        self.assertEqual(self.setup.reaps, 0)                    # never reaped under them

    def test_verification_waits_on_a_busy_sandbox(self) -> None:
        self.answers.append(approve())
        self.run_at(at(5, 5, 46))
        self.daedalus()
        self.run_at(at(5, 5, 50))                                # landed and checked
        busy = SuiteResult(False, tail="the build sandbox is busy: x", busy=True)
        answers = [busy]                 # busy for the first run only, free after
        self.worker.run_suite = lambda files, **kw: answers.pop(0) if answers else \
            self.fake_suite(files, **kw)
        self.run_at(at(5, 5, 55))
        [c] = self.cands()
        self.assertEqual(c["state"], "verifying")
        self.assertEqual(c["repairs"], 0)
        self.assertIn("busy", c["waiting"])


class ClaudeTests(_Case):
    def test_it_is_overnight_work_on_the_shared_caps(self) -> None:
        self.assertIn("self_improver", runtime.NIGHT_IMPLS)
        crew_store = CrewStore(self.root / "crew.db")
        self.addCleanup(crew_store.close)
        clock = [at(5, 5)]
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
        self.run_at(at(5, 5, 46))
        self.daedalus()
        clock[0] = at(5, 6)
        self.through_review()
        [c] = self.cands()
        self.assertEqual(c["state"], "reviewing")                 # waits; never approved
        self.assertIn("waiting for Claude's review", c["waiting"])
        self.assertEqual(self.pionir.applies(), [])
        self.assertEqual(len(asked), 3)
        clock[0] = at(6, 1, 30)                                   # the next night's cap
        self.run_at(at(6, 1, 30))
        self.assertEqual(self.cands()[0]["state"], "awaiting_owner")
        self.assertEqual(len(asked), 4)

    def test_a_review_never_straddles_noon(self) -> None:
        self.answers.append(approve())
        self.run_at(at(5, 5, 46))
        self.daedalus()
        self.through_review(start=(5, 50), runs=4)                # verified by 06:05...
        [c] = self.cands()
        self.assertEqual(c["state"], "awaiting_owner")
        # ...but one verified after noon waits for the night
        self.answers.append(approve())
        self.worker.per_night = 2
        rec = self.record()
        rec["candidates"][c["id"]].update(state="reviewing", verdict=None)
        (self.state / "builds.improve.json").write_text(json.dumps(rec), encoding="utf-8")
        self.prompts.clear()
        self.run_at(at(5, 13, 0))
        self.assertEqual(self.prompts, [])
        self.assertIn("between 01:00 and noon", self.cands()[0]["waiting"])
        self.run_at(at(6, 1, 5))
        self.assertEqual(len(self.prompts), 1)


class RefusalTests(_Case):
    def test_a_protected_file_is_refused_before_any_review_and_never_repaired(self) -> None:
        self.run_at(at(5, 5, 46))
        self.daedalus({"src/pionir/approvals.py": '"""The approval gate - relaxed."""\n'})
        self.through_review()
        [c] = self.cands()
        self.assertEqual(c["state"], "shelved")
        self.assertIn("approval gate", c["why"])
        self.assertEqual(self.prompts, [])
        self.assertEqual(len(self.pionir.builds()), 1)
        self.assertEqual(self.suites, [])

    def test_a_weakened_test_is_refused_before_any_review(self) -> None:
        self.run_at(at(5, 5, 46))
        self.daedalus({"tests/test_rules.py": REPO["tests/test_rules.py"].replace(
            "self.assertTrue(maker.check('medical'))", "self.assertTrue(True)")})
        self.through_review()
        [c] = self.cands()
        self.assertEqual(c["state"], "shelved")
        self.assertIn("real assert", c["why"])
        self.assertEqual(self.prompts, [])

    def test_a_regression_test_that_passes_without_the_change_is_not_one(self) -> None:
        self.run_at(at(5, 5, 46))
        orig = self.fake_suite
        self.worker.run_suite = lambda files, **kw: SuiteResult(True, 1, {}, 0, True) \
            if kw.get("modules") else orig(files, **kw)
        self.daedalus()
        self.through_review()
        [c] = self.cands()
        self.assertEqual(c["state"], "writing")                    # its one repair
        self.assertIn("FAIL BY AN ASSERTION", c["why"])
        self.assertEqual(self.prompts, [])

    def test_a_regression_test_that_only_errors_without_the_change_is_not_one(self) -> None:
        self.run_at(at(5, 5, 46))
        orig = self.fake_suite

        def suite(files, **kw):
            mods = kw.get("modules")
            if mods and b"FIXED" not in files.get(MAKER, b""):
                return SuiteResult(True, 1, {f"unittest.loader._FailedTest.{mods[0]}":
                                             "error:ImportError"}, 0, False)
            return orig(files, **kw)
        self.worker.run_suite = suite
        self.daedalus()
        self.through_review()
        [c] = self.cands()
        self.assertEqual(c["state"], "writing")
        self.assertIn("FAIL BY AN ASSERTION", c["why"])

    def test_a_test_failing_differently_from_main_is_a_new_failure(self) -> None:
        self.base_outcomes = {"test_x.T.test_y": "error:ImportError"}   # the sandbox lacks a dep
        self.run_at(at(5, 5, 46))
        self.daedalus()
        orig = self.fake_suite
        calls = []

        def suite(files, **kw):
            got = orig(files, **kw)
            if not kw.get("modules"):
                calls.append(1)
                if len(calls) == 2:                     # the change's run, after main's
                    return SuiteResult(True, 12, {"test_x.T.test_y": "failure:AssertionError"},
                                       0, False)
            return got
        self.worker.run_suite = suite
        self.through_review()
        [c] = self.cands()
        self.assertEqual(c["state"], "writing")
        self.assertIn("test_x.T.test_y (failure:AssertionError)", c["why"])

    def test_the_same_failure_main_has_is_not_new(self) -> None:
        self.base_outcomes = {"test_x.T.test_y": "error:ImportError"}
        self.answers.append(approve())
        self.run_at(at(5, 5, 46))
        self.daedalus()
        self.through_review()
        [c] = self.cands()
        self.assertEqual(c["state"], "awaiting_owner", c.get("why"))
        self.assertIn("main itself fails 1", c["verification"])

    def test_a_rejected_review_gets_one_repair_then_shelves(self) -> None:
        self.answers += [reject("first"), reject("second")]
        self.worker.budget = 600
        self.worker.rest = 0
        self.run_at(at(5, 5, 46))
        self.daedalus()
        self.through_review(start=(5, 47), runs=5)
        [c] = self.cands()
        self.assertEqual(c["state"], "writing")
        self.run_at(at(5, 6, 10))                        # the repair
        self.assertEqual(len(self.pionir.builds()), 2)
        self.assertIn("first", self.pionir.builds()[1].payload["intent"])
        self.daedalus({MAKER: CHECKER.replace('if "medical" in label:',
                                              'if "medical" in label and None:  # FIXED')})
        self.through_review(start=(6, 11), runs=5)
        [c] = self.cands()
        self.assertEqual(c["state"], "shelved")
        self.assertEqual(len(self.prompts), 2)
        self.assertEqual(self.pionir.applies(), [])

    def test_daedalus_committing_nothing_new_is_a_failed_attempt(self) -> None:
        self.run_at(at(5, 5, 46))
        self.pionir.finish("t-0", commit="")
        self.run_at(at(5, 5, 55))
        [c] = self.cands()
        self.assertEqual(c["state"], "writing")
        self.assertIn("committed nothing new", c["why"])


class SetupTests(_Case):
    def test_without_the_sandbox_it_is_not_set_up_and_does_nothing(self) -> None:
        self.configured = False
        got = self.run_at(at(5, 5, 46), ok=False)
        self.assertIsInstance(got, Err)
        self.assertEqual(got.error.kind, ErrorKind.NOT_CONFIGURED)
        self.assertIn("NOT SET UP", got.error.message)
        self.assertIn("setup-build-sandbox.ps1", got.error.message)
        self.assertEqual(self.pionir.jobs, [])
        self.assertEqual(list(self.sandbox.iterdir()), [])

    def test_auth_compat_on_is_not_set_up(self) -> None:
        self.worker.auth_compat = lambda: True
        got = self.run_at(at(5, 5, 46), ok=False)
        self.assertIn("PIONIR_AUTH_COMPAT", got.error.message)
        self.assertEqual(self.pionir.jobs, [])

    def test_it_never_needs_node(self) -> None:
        self.assertIsNone(self.setup.node)
        self.assertIsNone(self.worker.readiness(self.root))
        self.run_at(at(5, 5, 46))
        self.assertEqual(len(self.pionir.builds()), 1)


class AmberTests(_Case):
    def test_clusters_every_night_and_no_proposal_turns_it_amber(self) -> None:
        self.state.mkdir(parents=True)
        nights = {f"2026-10-0{d}": {"start": at(d, 5, 45), "end": at(d, 6, 30), "clusters": 2,
                                    "eligible": [], "skipped": {}, "started": [],
                                    "needs_human": [{"why": "needs a human: it touches a "
                                                            "protected rule (rules.py)"}],
                                    "why_none": "every cluster needs a human"}
                  for d in (2, 3, 4)}
        (self.state / "builds.improve.json").write_text(json.dumps({"nights": nights}),
                                                        encoding="utf-8")
        self.run_at(at(4, 12))
        pulse = self.worker.pulse(self.state, at(4, 12))
        self.assertIn("proposed nothing", pulse["alert"])
        self.assertIn("2, 2, 2", pulse["alert"])
        self.assertIn("needs a human", pulse["alert"])
        rec = self.record()
        rec["candidates"]["imp-20261004-abcd"] = {
            "id": "imp-20261004-abcd", "night": "2026-10-04", "state": "awaiting_owner",
            "created_at": at(4, 6), "evidence": {"worker": "w", "reason": "r"},
            "history": [{"state": "awaiting_owner"}]}
        (self.state / "builds.improve.json").write_text(json.dumps(rec), encoding="utf-8")
        self.run_at(at(4, 12, 5))
        self.assertIsNone(self.worker.pulse(self.state, at(4, 12, 5))["alert"])

    def test_a_quiet_estate_is_not_amber(self) -> None:
        self.rows = []
        for d in (5, 6, 7):
            self.run_at(at(d, 5, 46))
        self.run_at(at(7, 12))
        self.assertIsNone(self.worker.pulse(self.state, at(7, 12))["alert"])


class ConfigTests(unittest.TestCase):
    def test_its_window_lies_inside_the_night(self) -> None:
        spec = default_registry().require("builds.improve")
        with self.assertRaises(ValueError):
            improve_worker.SelfImprover(spec, window="07:00-09:00")
        with self.assertRaises(ValueError):
            improve_worker.SelfImprover(spec, per_night=3)

    def test_the_catalogue_declares_it_in_the_builds_division(self) -> None:
        w = default_registry().require("builds.improve")
        self.assertEqual(w.division, "builds")
        self.assertEqual(w.per_night, 2)
        self.assertEqual(w.threshold, 5)
        self.assertEqual(w.window.describe(), "05:45-07:30 local time")


if __name__ == "__main__":
    unittest.main()
