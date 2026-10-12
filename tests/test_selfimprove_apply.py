"""Applying an approved self-improvement, and undoing it: ``improve.apply`` and the CLI.

The owner's repo is a throw-away git repository with a stand-in ``pionir`` package; the patch
is made the way the worker makes it (a clone, a commit, ``diff-tree -p``). One test runs the
real unittest runner on it; the rest inject the test run. Each test fails if its rule is
reverted: the apply on a clean main as ONE commit naming the candidate, "stale" when main
moved and it no longer applies (nothing touched), a dirty or other branch refused, red tests
reverted at once, a patch that is not the one the card showed refused, the protected list
checked again here, the capability parked on every call, and undo as a ``git revert``.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from pionir import improvecli
from pionir.adapters.improve import APPLY, ImproveAdapter, ImproveSettings
from pionir.config import PionirSettings
from pionir.contracts import RiskLevel, Task
from pionir.discord_gate import render_request
from pionir.errors import AdapterProtocolError
from pionir.selfimprove import store
from pionir.selfimprove.guard import changed_paths

CID = "imp-20261012-ab12"
REG = "tests/test_regression_imp_20261012_ab12.py"
RULES = "def check(label):\n    if 'medical' in label:\n        return 'a claim'\n    return None\n"
FIXED = "def check(label):\n    return None\n"
REGRESSION = ("import unittest\n\nfrom pionir.crew.etsy import rules\n\n\n"
              "class T(unittest.TestCase):\n    def test_it(self):\n"
              "        self.assertIsNone(rules.check('medical'))\n")


def git(cwd, *args) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def write(repo: Path, files: dict) -> None:
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")


def commit(repo: Path, files: dict, message="c") -> str:
    write(repo, files)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._t = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self._t.name)
        self.root = root
        self.repo = root / "Pionir"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        self.base = commit(self.repo, {"src/pionir/__init__.py": "",
                                       "src/pionir/crew/__init__.py": "",
                                       "src/pionir/crew/etsy/__init__.py": "",
                                       "src/pionir/crew/etsy/rules.py": RULES,
                                       "README.md": "Pionir\n"}, "seed")
        self.improve = root / "improve"
        self.patch = self.make_patch({"src/pionir/crew/etsy/rules.py": FIXED, REG: REGRESSION})
        self.ran: list = []
        self.tests_pass = True
        self.adapter = ImproveAdapter(ImproveSettings(self.repo, self.improve), run=self.fake_run)
        self.sha = self.card()

    def tearDown(self) -> None:
        self._t.cleanup()

    def make_patch(self, files: dict) -> str:
        clone = self.root / f"clone-{len(list(self.root.iterdir()))}"
        git(self.root, "clone", "-q", str(self.repo), clone.name)
        head = commit(clone, files, "Daedalus")
        return subprocess.run(["git", "diff-tree", "-p", "--no-renames", "--binary",
                               "--full-index", "--no-color", self.base, head], cwd=clone,
                              check=True, capture_output=True, text=True).stdout

    def card(self, patch=None) -> str:
        return store.write_patch(self.improve, CID, patch or self.patch, {
            "title": "etsy.digital: stop the medical false alarm", "base": self.base,
            "tests": [REG], "evidence": {"worker": "etsy.digital", "count": 12,
                                         "first_seen": "2026-10-05 01:00",
                                         "last_seen": "2026-10-11 23:00",
                                         "reason": "labels[N]: makes a claim ('medical')",
                                         "samples": ["one", "two", "three"]},
            "hypothesis": "a false alarm", "verdict": {"approved": True,
                                                       "summary": "It stops the false alarm."},
            "verification": "failed without, passed with", "diff_stat": "2 files changed"})

    def fake_run(self, argv, cwd, timeout):
        self.ran.append(argv[7:])
        out = Path(argv[6])
        out.write_text(json.dumps({"ran": 1, "failed": [] if self.tests_pass else ["x.T.t"],
                                   "skipped": 0, "ok": self.tests_pass}), encoding="utf-8")
        return (0 if self.tests_pass else 1), "", False

    def task(self, **payload) -> Task:
        return Task(APPLY, payload or {"id": CID, "patch_sha256": self.sha},
                    frozenset({APPLY}))

    def head(self) -> str:
        return git(self.repo, "rev-parse", "HEAD")

    def outcome(self) -> dict:
        return store.read_json(store.folder(self.improve, CID) / store.OUTCOME)


class CapabilityTests(_Case):
    def test_it_is_parked_on_every_call_and_never_routed_or_batched(self) -> None:
        [cap] = self.adapter.manifest.capabilities
        self.assertEqual(cap.name, APPLY)
        self.assertIs(cap.risk, RiskLevel.PRIVILEGED)
        self.assertTrue(cap.requires_approval)
        self.assertFalse(cap.routable)
        self.assertFalse(cap.batchable)

    def test_the_card_shows_evidence_diff_verdict_and_undo(self) -> None:
        context = self.adapter.park_context(self.task())
        self.assertIn("-        return 'a claim'", context["diff"])
        self.assertEqual(context["undo"], f"python -m pionir improve undo {CID}")
        text = render_request({"id": "ap1", "capability": APPLY, "payload": dict(self.task().payload),
                               "summary": "apply it", "requester": "crew",
                               "context": context}, None)
        for want in ("CHANGES PIONIR'S OWN CODE", "etsy.digital", "seen 12 times",
                     "medical", "APPROVED", "It stops the false alarm", "a claim",
                     f"python -m pionir improve undo {CID}", "patch.diff"):
            self.assertIn(want, text)

    def test_a_patch_changed_after_the_card_is_refused(self) -> None:
        (store.folder(self.improve, CID) / store.PATCH).write_text(
            self.patch.replace("return None", "return 'x'"), encoding="utf-8")
        with self.assertRaises(AdapterProtocolError):
            self.adapter.validate(self.task())
        with self.assertRaises(AdapterProtocolError):
            self.adapter.validate(self.task(id=CID, patch_sha256=self.sha, extra="x"))

    def test_the_protected_list_is_checked_again_here(self) -> None:
        bad = self.make_patch({"src/pionir/approvals.py": "# relaxed\n"})
        self.sha = self.card(bad)
        with self.assertRaises(AdapterProtocolError) as caught:
            self.adapter.validate(self.task())
        self.assertIn("protected", str(caught.exception))


class ApplyTests(_Case):
    def test_a_clean_main_gets_one_commit_naming_the_candidate(self) -> None:
        result = self.adapter.execute(self.task())
        self.assertTrue(result.output["ok"])
        self.assertTrue(result.output["applied"])
        self.assertEqual(git(self.repo, "rev-list", "--count", f"{self.base}..HEAD"), "1")
        self.assertTrue(git(self.repo, "log", "-1", "--format=%s").startswith(
            f"Self-improvement {CID}: "))
        self.assertIn(f"python -m pionir improve undo {CID}",
                      git(self.repo, "log", "-1", "--format=%B"))
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertEqual((self.repo / "src/pionir/crew/etsy/rules.py").read_text(
            encoding="utf-8").replace("\r\n", "\n"), FIXED)
        self.assertEqual(self.ran, [["test_regression_imp_20261012_ab12"]])
        self.assertEqual(self.outcome()["commit"], self.head())

    def test_the_real_runner_runs_the_targeted_test_in_the_repo(self) -> None:
        self.adapter = ImproveAdapter(ImproveSettings(self.repo, self.improve))
        result = self.adapter.execute(self.task())
        self.assertTrue(result.output["applied"], result.output)
        self.assertTrue(result.output["tests"]["ok"], result.output["tests"])
        self.assertEqual(result.output["tests"]["ran"], 1)
        self.assertFalse(result.output.get("reverted"))

    def test_main_moved_but_still_applies(self) -> None:
        commit(self.repo, {"README.md": "Pionir, moved on\n"})
        result = self.adapter.execute(self.task())
        self.assertTrue(result.output["applied"])

    def test_main_moved_and_no_longer_applies_is_stale_and_untouched(self) -> None:
        moved = commit(self.repo, {"src/pionir/crew/etsy/rules.py": RULES.replace(
            "a claim", "a claim the shop could be held to")})
        result = self.adapter.execute(self.task())
        self.assertTrue(result.output["ok"])
        self.assertFalse(result.output["applied"])
        self.assertTrue(result.output["stale"])
        self.assertIn("will re-propose", result.output["why"])
        self.assertEqual(self.head(), moved)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertTrue(self.outcome()["stale"])

    def test_a_main_whose_history_no_longer_holds_the_base_is_stale(self) -> None:
        # the same files, but main rewritten: the patch would apply, the base is not in it
        git(self.repo, "checkout", "-q", "--orphan", "rewritten")
        git(self.repo, "commit", "-q", "-m", "rewritten history")
        git(self.repo, "branch", "-q", "-M", "main")
        rewritten = self.head()
        result = self.adapter.execute(self.task())
        self.assertFalse(result.output["applied"])
        self.assertIn("no longer contains its base", result.output["why"])
        self.assertEqual(self.head(), rewritten)

    def test_a_dirty_main_or_another_branch_is_never_applied_over(self) -> None:
        (self.repo / "README.md").write_text("Ian's own edit\n", encoding="utf-8")
        result = self.adapter.execute(self.task())
        self.assertFalse(result.output["applied"])
        self.assertIn("uncommitted", result.output["why"])
        self.assertEqual(self.head(), self.base)
        self.assertEqual((self.repo / "README.md").read_text(encoding="utf-8"),
                         "Ian's own edit\n")
        git(self.repo, "checkout", "-q", "--", "README.md")
        git(self.repo, "checkout", "-q", "-b", "other")
        result = self.adapter.execute(self.task())
        self.assertFalse(result.output["applied"])
        self.assertIn("not on main", result.output["why"])

    def test_red_targeted_tests_are_reverted_at_once(self) -> None:
        self.tests_pass = False
        result = self.adapter.execute(self.task())
        self.assertTrue(result.output["applied"])
        self.assertTrue(result.output["reverted"])
        self.assertEqual(git(self.repo, "rev-list", "--count", f"{self.base}..HEAD"), "2")
        self.assertEqual((self.repo / "src/pionir/crew/etsy/rules.py").read_text(
            encoding="utf-8").replace("\r\n", "\n"), RULES)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")


class CliTests(_Case):
    def settings(self) -> PionirSettings:
        return PionirSettings(state_root=self.root / "state", pionir_repo=self.repo,
                              improve_dir=self.improve)

    def test_undo_reverts_exactly_the_applied_commit(self) -> None:
        self.adapter.execute(self.task())
        applied = self.head()
        out = io.StringIO()
        code = improvecli.run(Namespace(improve_command="undo", id=CID), self.settings(),
                              out=out)
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("Undone", out.getvalue())
        self.assertIn(f"Revert", git(self.repo, "log", "-1", "--format=%s"))
        self.assertEqual(git(self.repo, "rev-parse", "HEAD~1"), applied)
        self.assertEqual((self.repo / "src/pionir/crew/etsy/rules.py").read_text(
            encoding="utf-8").replace("\r\n", "\n"), RULES)
        self.assertTrue(store.read_json(store.folder(self.improve, CID) / store.UNDONE)["undone"])
        # twice is harmless
        out = io.StringIO()
        self.assertEqual(improvecli.run(Namespace(improve_command="undo", id=CID),
                                        self.settings(), out=out), 0)
        self.assertIn("already undone", out.getvalue())

    def test_undo_of_something_never_applied_is_an_error(self) -> None:
        out = io.StringIO()
        code = improvecli.run(Namespace(improve_command="undo", id=CID), self.settings(),
                              out=out)
        self.assertEqual(code, 1)
        self.assertIn("never applied", out.getvalue())
        self.assertEqual(self.head(), self.base)

    def test_status_lists_candidates_and_their_states(self) -> None:
        ledger = store.ledger_path(self.root / "state" / "crew")
        store.write_json(ledger, {"counts": {"proposals": 1}, "amber": None, "candidates": {
            CID: {"id": CID, "state": "awaiting_owner", "created_at": 1.0, "why": "waiting",
                  "evidence": {"worker": "etsy.digital", "reason": "labels[N]: medical"}}}})
        self.adapter.execute(self.task())
        out = io.StringIO()
        self.assertEqual(improvecli.run(Namespace(improve_command="status"), self.settings(),
                                        out=out), 0)
        text = out.getvalue()
        self.assertIn("Proposals made: 1", text)
        self.assertIn(CID, text)
        self.assertIn("applied", text)                     # folded in from outcome.json
        self.assertIn("etsy.digital", text)

    def test_the_cli_parses_improve_commands(self) -> None:
        from pionir.cli import _parser
        args = _parser().parse_args(["improve", "undo", CID])
        self.assertEqual((args.command, args.improve_command, args.id), ("improve", "undo", CID))


class PatchPathsTests(unittest.TestCase):
    def test_changed_paths(self) -> None:
        patch = ("diff --git a/x.py b/x.py\nindex 1..2 100644\n--- a/x.py\n+++ b/x.py\n"
                 "@@ -1 +1 @@\n-a\n+b\n")
        self.assertEqual(changed_paths(patch), ["x.py"])


if __name__ == "__main__":
    unittest.main()
