"""Why every night build timed out (2026-10-02 and 10-03), and the rules that now stop it.

Both nights the build Daedalus ran ~51 minutes, was cancelled, and its gate then failed at
G2-tests with "No module named pytest" (the sandbox's own Python had none); a repair repeated
it from scratch under a prompt that said a review had rejected it; the worker kept only
"timed out". Each test here fails if its rule is reverted. Temp dirs and fakes only.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from test_crew_builds import BUILD, CARD, JobOutcome, _Case, approve, at, commit, product, reject

from pionir.adapters.daedalus import AdapterTimeout, DaedalusAdapter, DaedalusSettings
from pionir.crew.builds import worker as worker_module
from pionir.crew.registry import default_registry


class PreflightTests(_Case):
    def test_the_shipped_defaults_give_a_slice_room_to_finish(self) -> None:
        shipped = default_registry().require("builds.daedalus")
        self.assertEqual(shipped.budget, 75 * 60)       # 72 steps took ~46 min on the card
        self.assertEqual(shipped.slices, 2)

    def test_without_pytest_in_the_sandbox_python_no_build_starts_and_the_owner_is_told_once(
            self) -> None:
        asked: list = []
        self.worker.probe_module = lambda python, module: asked.append((python, module)) or False
        self.run_at(at(1, 1, 30))
        self.run_at(at(1, 1, 45))
        self.assertEqual(self.pionir.builds(), [])               # no job, no 45 minutes burned
        self.assertFalse((self.sandbox / "exif-strip").exists())  # not even a repo
        self.assertEqual(asked[0], (self.setup.python, "pytest"))
        cards = [c for k, c in self.pionir.cards.items() if k.startswith("builds:tools:")]
        self.assertEqual(len(cards), 1)
        self.assertIn("setup-build-sandbox.ps1", cards[0]["body"])
        self.assertEqual(sum(1 for j in self.pionir.jobs if j.capability == CARD
                             and j.payload["key"].startswith("builds:tools:")), 1)

    def test_once_pytest_is_there_the_same_night_builds(self) -> None:
        self.worker.probe_module = lambda python, module: False
        self.run_at(at(1, 1, 30))
        self.worker.probe_module = lambda python, module: True
        self.run_at(at(1, 1, 45))
        self.assertEqual(len(self.pionir.builds()), 1)

    def test_a_non_python_product_is_not_held_for_pytest(self) -> None:
        self.worker.probe_module = lambda python, module: False
        entry = dict(self.entry())
        self.assertEqual(entry["language"], "python")      # the helper's product is Python
        self.assertTrue(self.worker._tools_ready(
            None, {"nights": {}}, None, {**entry, "language": "typescript"}, []))

    def test_the_probe_runs_the_sandbox_interpreter_in_isolated_mode(self) -> None:
        seen: list = []

        class Done:
            returncode = 0

        real = worker_module.subprocess.run
        worker_module.subprocess.run = lambda cmd, **kw: seen.append((cmd, kw)) or Done()
        try:
            self.assertTrue(worker_module._probe_module(Path("py.exe"), "pytest"))
        finally:
            worker_module.subprocess.run = real
        self.assertEqual(seen[0][0], ["py.exe", "-I", "-c", "import pytest"])
        self.assertIn("timeout", seen[0][1])


class RetryTests(_Case):
    def start_and_fail(self, error_type, error):
        self.run_at(at(1, 1, 30))
        self.pionir.fail("t-build-0", error_type, error)
        self.run_at(at(1, 2, 16))
        return self.pionir.builds()[1].payload["intent"]

    def test_a_timeout_retry_is_not_told_a_review_rejected_it(self) -> None:
        intent = self.start_and_fail("AdapterTimeout", "did not finish within 2700 seconds")
        self.assertNotIn("REJECTED", intent)
        self.assertIn("did not finish", intent)
        self.assertIn("Nothing from it was kept", intent)
        self.assertIn("seed commit", intent)       # the repo really is just the seed again
        self.assertIn("minutes", intent)           # and the job knows how long it has

    def test_a_review_rejection_still_gets_the_review_repair_prompt(self) -> None:
        self.build_night(answers=[reject("the README overstates what it does")])
        intent = self.pionir.builds()[1].payload["intent"]
        self.assertIn("A review REJECTED", intent)
        self.assertIn("README overstates", intent)

    def test_a_failed_repair_of_a_rejected_build_keeps_the_review_problems(self) -> None:
        self.build_night(answers=[reject("the README overstates what it does")])
        self.pionir.fail("t-build-1", "AdapterTimeout", "did not finish within 2700 seconds")
        self.run_at(at(1, 3, 40))
        p = self.product_state("exif-strip")
        # one build + one repair = both attempts spent: shelved, not silently re-prompted
        self.assertEqual(p["state"], "shelved")

    def test_the_timeout_keeps_where_daedalus_stopped(self) -> None:
        said = ("Daedalus job j-1 did not finish within 2700 seconds; it was cancelled. After "
                "the cancel: 72 steps taken, its gate then failed at G2-tests (No module "
                "named pytest), 7 files written. The outcome is at GET /jobs/j-1")
        self.run_at(at(1, 1, 30))
        self.pionir.fail("t-build-0", "AdapterTimeout", said)
        result = self.run_at(at(1, 2, 16))
        p = self.product_state("exif-strip")
        self.assertIn("72 steps taken", p["attempts"][0]["why"])
        self.assertIn("G2-tests", p["attempts"][0]["why"])
        failed = [o for o in result.value if o.kind == "build.attempt_failed"]
        self.assertIn("72 steps taken", json.dumps(failed[0].payload))
        night_log = " ".join(self.record()["nights"]["2026-10-01"]["log"])
        self.assertIn("72 steps taken", night_log)


class SliceTests(_Case):
    def setUp(self) -> None:
        super().setUp()
        self.worker.slices = 2

    def land(self, tid, extra=None):
        repo = Path(self.pionir.builds()[-1].payload["repo"])
        files = product(self.entry())
        files.update(extra or {})
        head = commit(repo, files)
        self.pionir.finish(tid, commit=head, branch="main")
        return head

    def test_the_core_lands_first_and_the_rest_starts_without_a_review(self) -> None:
        self.answers.append(approve())
        self.run_at(at(1, 1, 30))
        first = self.pionir.builds()[0].payload["intent"]
        self.assertIn("SLICE 1 OF 2", first)
        self.assertIn("Do NOT write the command-line entry point", first)
        head = self.land("t-build-0")
        result = self.run_at(at(1, 1, 50))
        p = self.product_state("exif-strip")
        self.assertEqual((p["state"], p["slice"], p["slice_head"]), ("building", 2, head))
        self.assertEqual(self.prompts, [])                         # not reviewed yet
        self.assertEqual(len([o for o in result.value if o.kind == "build.slice_landed"]), 1)
        second = self.pionir.builds()[1].payload["intent"]
        self.assertIn("SLICE 2 OF 2", second)
        self.assertIn("already holds the first slice", second)
        self.assertEqual(p["attempts"][1]["slice"], 2)
        self.land("t-build-1", {"CHANGELOG.md": "# 1.0\n"})
        self.run_at(at(1, 2, 20))
        self.assertEqual(self.product_state("exif-strip")["state"], "staged")
        self.assertEqual(len(self.prompts), 1)                      # one review, of the whole

    def test_a_slice_that_times_out_does_not_cost_the_slice_before_it(self) -> None:
        self.run_at(at(1, 1, 30))
        head = self.land("t-build-0")
        self.run_at(at(1, 1, 50))
        self.pionir.fail("t-build-1", "AdapterTimeout", "did not finish within 2700 seconds")
        self.run_at(at(1, 2, 40))
        p = self.product_state("exif-strip")
        # slice 1 landed (1 counted attempt) and slice 2 timed out (1 more): one retry is
        # still owed to slice 2, not a shelving after two attempts overall
        self.assertEqual(p["slice_head"], head)
        self.assertEqual(p["state"], "building")
        third = self.pionir.builds()[2].payload["intent"]
        self.assertIn("SLICE 2 OF 2", third)
        self.assertIn("Nothing from it was kept", third)
        self.assertIn("already holds the first slice", third)
        self.pionir.fail("t-build-2", "AdapterTimeout", "did not finish within 2700 seconds")
        self.run_at(at(1, 3, 30))
        self.assertEqual(self.product_state("exif-strip")["state"], "shelved")

    def test_a_pass_that_commits_nothing_new_since_the_core_is_a_failure(self) -> None:
        self.run_at(at(1, 1, 30))
        head = self.land("t-build-0")
        self.run_at(at(1, 1, 50))
        self.pionir.finish("t-build-1", commit=head, branch="main")   # same commit again
        self.run_at(at(1, 2, 20))
        p = self.product_state("exif-strip")
        self.assertEqual(p["attempts"][1]["outcome"], "gate_failed")
        self.assertNotEqual(p["state"], "built")

    def test_one_slice_keeps_the_old_whole_product_behaviour(self) -> None:
        self.worker.slices = 1
        self.answers.append(approve())
        self.build_night()
        self.assertEqual(self.product_state("exif-strip")["state"], "staged")
        self.assertNotIn("SLICE", self.pionir.builds()[0].payload["intent"])


class AdapterTimeoutTests(unittest.TestCase):
    """The adapter's timeout carries what Daedalus said once it stopped."""

    def run_timeout(self, final_job):
        calls: list = []

        class Client:
            def post(self, path, payload, *, timeout_seconds=None):
                calls.append(("POST", path))
                return {"ok": True}

            def get(self, path, *, timeout_seconds=None):
                calls.append(("GET", path))
                cancelled = ("POST", "/jobs/j-1/cancel") in calls
                return {"job": final_job if cancelled else {"id": "j-1", "state": "running",
                                                          "events": 41}}

        clock = [0.0]

        def monotonic():
            clock[0] += 1.0 if ("POST", "/jobs/j-1/cancel") in calls else 100.0
            return clock[0]

        adapter = DaedalusAdapter(
            DaedalusSettings(poll_interval_seconds=0.01, request_timeout_seconds=5,
                             cancel_grace_seconds=30),
            client=Client(), sleep=lambda _s: None, monotonic=monotonic)
        with self.assertRaises(AdapterTimeout) as caught:
            adapter._await_job(adapter._client, "j-1", 250, grace=30)
        return str(caught.exception)

    def test_the_message_says_how_far_it_got_after_the_cancel(self) -> None:
        text = self.run_timeout({
            "id": "j-1", "state": "done", "stage_failed": "G2-tests",
            "files": ["a.py", "b.py"],
            "result": {"steps": [{}] * 72,
                       "gate": {"stage_failed": "G2-tests",
                                "reason": "No module named pytest"}}})
        self.assertIn("After the cancel: 72 steps taken", text)
        self.assertIn("G2-tests", text)
        self.assertIn("No module named pytest", text)
        self.assertIn("2 files written", text)

    def test_if_it_never_stopped_the_message_says_so(self) -> None:
        text = self.run_timeout({"id": "j-1", "state": "running", "events": 41})
        self.assertIn("it had not stopped yet", text)
        self.assertIn("41 events logged", text)


class SandboxToolsTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parent.parent

    def test_pytest_is_pinned_with_hashes_in_the_sandbox_requirements(self) -> None:
        text = (self.ROOT / "tools" / "build-sandbox-requirements.txt").read_text(
            encoding="utf-8")
        for name in ("pytest", "pluggy", "iniconfig", "packaging", "pygments"):
            line = next((ln for ln in text.splitlines() if ln.lower().startswith(name + "==")),
                        None)
            self.assertIsNotNone(line, name)
            self.assertIn("--hash=sha256:", text[text.index(line):].split(chr(10) + chr(10))[0])

    def test_the_setup_script_only_calls_the_sandbox_ready_once_pytest_imports(self) -> None:
        script = (self.ROOT / "tools" / "setup-build-sandbox.ps1").read_text(encoding="utf-8")
        self.assertIn("import fastapi, uvicorn, requests, yaml, pytest", script)


if __name__ == "__main__":
    unittest.main()
