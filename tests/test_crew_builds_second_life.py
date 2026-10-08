"""Night builds use the whole window, and a shelved build gets ONE second life.

Ten nights (2026-10-02..07) staged nothing: each used about 75 of its 360 minutes for one
product, and every product was shelved for good after a build and one repair - for running
out of time, for "committed nothing", or for failing tests. Each test here fails if its rule
is reverted: jobs back to back (with a rest for Moss between them), no review while a job is
in flight (our contained test run reaps every sandbox process, the build's Daedalus too), and
one classified second life per product, then terminal with the reason recorded. Every gate
stays: a second life is one more Daedalus attempt, reviewed and approved like any other.
Temp dirs and fakes only (the same harness as test_crew_builds).
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from support import use_long_tempdir
from test_crew_builds import _Case, approve, at, commit, product, reject

from pionir.crew.builds import second_life as sl
from pionir.crew.builds.review import SuiteRun
from pionir.crew.registry import default_registry

use_long_tempdir()

LIFE = "SECOND (AND LAST) CHANCE"


class ClassifyTests(unittest.TestCase):
    """The shelf reasons the live record held on 2026-10-07, word for word."""

    def test_the_real_shelf_reasons_are_classified(self) -> None:
        cases = {
            "it did not finish in its 75-minute budget and was stopped (at the last look (it "
            "had not stopped yet): 80 events logged)": sl.TIMEOUT,
            "it did not finish in its 45-minute budget and was stopped": sl.TIMEOUT,
            "Daedalus reported a pass but committed nothing new": sl.NOTHING_COMMITTED,
            "rejected by our checks after its repair: the tests FAILED when run with `python "
            "-I -S -m unittest discover -s tests (no site-packages)`: ...": sl.TESTS_FAILED,
            "Daedalus's own gate did not pass: 2 tests failing": sl.TESTS_FAILED,
        }
        for why, kind in cases.items():
            self.assertEqual(sl.classify({"shelved_why": why, "attempts": []})[0], kind, why)

    def test_what_a_retry_cannot_fix_is_terminal(self) -> None:
        for why in ("rejected by Claude after its repair: the tests FAILED to cover X",
                    "Claude's review could not be completed (3 tries; the last: x)",
                    "approved, but it could not be staged: OSError: disk full",
                    "Pionir refused the build: bad repo",
                    "the job was lost: no outcome"):
            kind, label = sl.classify({"shelved_why": why, "attempts": []})
            self.assertIsNone(kind, why)
            self.assertTrue(label)

    def test_a_timed_out_last_attempt_is_a_timeout_whatever_the_text(self) -> None:
        p = {"shelved_why": "x", "slice": 2,
             "attempts": [{"slice": 1, "outcome": "built"},
                          {"slice": 2, "outcome": "timed_out"},
                          {"slice": 2, "outcome": "not_started"}]}
        self.assertEqual(sl.classify(p)[0], sl.TIMEOUT)


class BackToBackTests(_Case):
    def test_no_review_runs_while_a_job_is_in_flight(self) -> None:
        # exif-strip is built while Claude's cap is spent, so it waits for its review; the
        # next product starts meanwhile. Our test run reaps EVERY sandbox-user process, so
        # it must not run until that job is over.
        self.build_night(answers=[])
        self.assertEqual(self.test_runs, 1)
        self.run_at(at(1, 2, 0))                              # csv-to-ics starts
        self.assertEqual(len(self.pionir.builds()), 2)
        self.answers.append(approve())
        self.run_at(at(1, 2, 25))                             # exif's review is due, but...
        self.assertEqual(self.test_runs, 1)                   # ...a job is in flight
        self.assertEqual(self.product_state("exif-strip")["state"], "built")
        repo = Path(self.pionir.builds()[1].payload["repo"])
        head = commit(repo, product(self.entry("csv-to-ics")))
        self.pionir.finish("t-build-1", commit=head)
        self.run_at(at(1, 2, 30))                             # settled: reviews run again
        self.assertEqual(self.test_runs, 2)
        self.assertEqual(self.product_state("exif-strip")["state"], "staged")

    def test_the_shipped_catalogue_runs_back_to_back_with_second_lives(self) -> None:
        shipped = default_registry().require("builds.daedalus")
        self.assertEqual(shipped.new_per_night, 0)
        self.assertEqual(shipped.rest, 10 * 60)
        self.assertTrue(shipped.second_lives)
        self.assertEqual(shipped.second_life_budget, 150 * 60)


class SecondLifeTests(_Case):
    def _time_out(self, task: str, now: float) -> None:
        self.pionir.fail(task, "AdapterTimeout", "did not finish within 2700 seconds")
        self.run_at(now)

    def test_a_timeout_gets_one_bigger_budget_then_is_final(self) -> None:
        self.run_at(at(1, 1, 30))
        self._time_out("t-build-0", at(1, 2, 16))             # its repair starts at once
        self._time_out("t-build-1", at(1, 3, 2))              # shelved, second life pending
        p = self.product_state("exif-strip")
        self.assertEqual(p["state"], "shelved")
        self.assertEqual(p["second_life"]["state"], "pending")
        self.assertEqual(p["second_life"]["kind"], sl.TIMEOUT)
        card = self.pionir.cards["builds:shelved:exif-strip"]
        self.assertIn("ONE second life", card["body"])
        self.assertIn("150 minutes", card["body"])
        self.assertEqual(len(self.pionir.builds()), 2)        # the GPU rests first
        result = self.run_at(at(1, 3, 15))
        third = self.pionir.builds()[2]
        self.assertTrue(third.payload["repo"].endswith("exif-strip"))
        self.assertEqual(third.payload["budget_seconds"], 150 * 60)
        self.assertEqual(third.payload["not_after"], at(1, 5, 45))
        self.assertTrue(third.payload["intent"].startswith(LIFE))
        self.assertIn("ran out of time", third.payload["intent"])
        self.assertIn("about 150 minutes", third.payload["intent"])
        self.assertLessEqual(len(third.payload["intent"]), 7900)
        self.assertEqual(len(self.rows(result, "build.second_life")), 1)
        # ...and it times out again: final, with the reason, and never a fourth job for it
        self.pionir.fail("t-build-2", "AdapterTimeout", "did not finish within 9000 seconds")
        result = self.run_at(at(1, 5, 46))
        p = self.product_state("exif-strip")
        self.assertEqual(p["state"], "shelved")
        self.assertEqual(p["second_life"]["state"], "spent")
        self.assertIn("150-minute budget", p["shelved_why"])
        self.assertIn("second life", p["terminal_why"])
        self.assertIn("This is final", self.pionir.cards["builds:shelved:exif-strip:final"]
                      ["body"])
        self.run_at(at(1, 6, 0))                              # the next product instead
        later = [j.payload["repo"] for j in self.pionir.builds()[3:]]
        self.assertTrue(later and all(r.endswith("csv-to-ics") for r in later), later)

    def test_a_second_life_that_cannot_fit_waits_and_a_new_product_uses_the_room(self) -> None:
        self.run_at(at(1, 1, 30))
        self._time_out("t-build-0", at(1, 2, 16))
        self._time_out("t-build-1", at(1, 5, 0))              # late: 150 minutes do not fit
        self.run_at(at(1, 5, 15))
        jobs = self.pionir.builds()
        self.assertEqual(len(jobs), 3)
        self.assertTrue(jobs[2].payload["repo"].endswith("csv-to-ics"))
        self.assertEqual(jobs[2].payload["budget_seconds"], 45 * 60)
        self.assertEqual(self.product_state("exif-strip")["second_life"]["state"], "pending")

    def test_committed_nothing_gets_a_sharper_prompt_and_still_needs_claude(self) -> None:
        self.build_night(answers=[reject("the --recursive flag is documented but missing")])
        self.pionir.finish("t-build-1")                       # "passed", no new commit
        self.run_at(at(1, 2, 20))
        p = self.product_state("exif-strip")
        self.assertEqual(p["state"], "shelved")
        self.assertEqual(p["second_life"]["kind"], sl.NOTHING_COMMITTED)
        self.run_at(at(1, 2, 31))
        job = self.pionir.builds()[2]
        intent = job.payload["intent"]
        self.assertTrue(intent.startswith(LIFE))
        self.assertIn("COMMITTED NOTHING NEW", intent)
        self.assertIn("--recursive flag", intent)            # the review's reasons, still
        self.assertEqual(job.payload["budget_seconds"], 45 * 60)
        head = commit(Path(job.payload["repo"]), {"CHANGELOG.md": "# Changelog\n\n## 1.0.0\n\n"
                                                                  "Added --recursive.\n"})
        self.pionir.finish("t-build-2", commit=head)
        # the second life's build is reviewed like any other: nothing staged without Claude
        self.run_at(at(1, 3, 0))
        self.assertEqual(self.product_state("exif-strip")["state"], "built")
        self.assertFalse((self.shelf / "exif-strip").exists())
        self.answers.append(approve())
        self.run_at(at(1, 3, 40))
        p = self.product_state("exif-strip")
        self.assertEqual(p["state"], "staged")
        self.assertEqual(p["second_life"]["state"], "succeeded")
        self.assertEqual(p["reviews"][-1]["by"], "Claude")
        self.assertTrue(p["reviews"][-1]["approved"])

    def test_failing_tests_get_one_fix_attempt_with_the_test_output(self) -> None:
        trace = ("ERROR: test_core (unittest.loader._FailedTest)\nImportError: Failed to "
                 "import test module: test_core\nModuleNotFoundError: No module named "
                 "'exif_strip.reader'")
        self.suite = SuiteRun(False, 2, "python -I -S -m unittest", trace)
        self.build_night()                                    # our run fails: rejected
        self.pionir.finish("t-build-1", commit=commit(
            Path(self.pionir.builds()[1].payload["repo"]), {"CHANGELOG.md": "x\n"}))
        self.run_at(at(1, 2, 20))                             # fails again: shelved
        p = self.product_state("exif-strip")
        self.assertEqual(p["state"], "shelved")
        self.assertEqual(p["second_life"]["kind"], sl.TESTS_FAILED)
        self.run_at(at(1, 2, 31))
        intent = self.pionir.builds()[2].payload["intent"]
        self.assertTrue(intent.startswith(LIFE))
        self.assertIn("No module named 'exif_strip.reader'", intent)   # verbatim output
        self.assertIn("ImportError: Failed to import test module", intent)
        # the fix fails our run a third time: final, and the leader is told it is final
        self.pionir.finish("t-build-2", commit=commit(
            Path(self.pionir.builds()[2].payload["repo"]), {"CHANGELOG.md": "y\n"}))
        result = self.run_at(at(1, 3, 0))
        p = self.product_state("exif-strip")
        self.assertEqual(p["state"], "shelved")
        self.assertEqual(p["second_life"]["state"], "spent")
        shelved = self.rows(result, "build.shelved")[-1].payload
        self.assertFalse(shelved["second_life"])
        self.assertIn("second life", shelved["terminal_why"])
        self.assertFalse((self.shelf / "exif-strip").exists())

    def test_a_product_shelved_before_this_rule_is_judged_and_revived(self) -> None:
        # the live record's csv-to-ics: two 45-minute timeouts, no second_life key at all
        self.run_at(at(1, 1, 30))
        self._time_out("t-build-0", at(1, 2, 16))
        self._time_out("t-build-1", at(1, 3, 2))
        rec = self.record()
        for key in ("second_life", "terminal_why"):
            rec["products"]["exif-strip"].pop(key, None)
        (self.state / "builds.daedalus.json").write_text(json.dumps(rec), encoding="utf-8")
        self.run_at(at(2, 1, 30))
        job = self.pionir.builds()[2]
        self.assertTrue(job.payload["repo"].endswith("exif-strip"))
        self.assertEqual(job.payload["budget_seconds"], 150 * 60)

    def test_second_lives_off_makes_every_shelf_final(self) -> None:
        self.worker.second_lives = False
        self.run_at(at(1, 1, 30))
        self._time_out("t-build-0", at(1, 2, 16))
        self._time_out("t-build-1", at(1, 3, 2))
        p = self.product_state("exif-strip")
        self.assertNotIn("second_life", p)
        self.assertIn("second_lives: false", p["terminal_why"])
        self.run_at(at(1, 3, 15))
        self.assertTrue(self.pionir.builds()[2].payload["repo"].endswith("csv-to-ics"))


if __name__ == "__main__":
    unittest.main()
