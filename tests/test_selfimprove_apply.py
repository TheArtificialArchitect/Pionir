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
REGRESSION = ("import unittest\n\nfrom pionir.crew.etsy import maker\n\n\n"
              "class T(unittest.TestCase):\n    def test_it(self):\n"
              "        self.assertIsNone(maker.check('medical'))\n")


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
        # A real repo (C:\src\Pionir) has an author identity; a bare CI runner does not, so
        # apply.py's own `git commit` fails there with "Author identity unknown". Give the temp
        # repo a LOCAL identity so the production commit path is exercised on any machine.
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "config", "user.email", "t@example.invalid")
        self.base = commit(self.repo, {"src/pionir/__init__.py": "",
                                       "src/pionir/crew/__init__.py": "",
                                       "src/pionir/crew/etsy/__init__.py": "",
                                       "src/pionir/crew/etsy/maker.py": RULES,
                                       "README.md": "Pionir\n"}, "seed")
        self.improve = root / "improve"
        self.patch = self.make_patch({"src/pionir/crew/etsy/maker.py": FIXED, REG: REGRESSION})
        self.ran: list = []
        self.tests_pass = True
        self.adapter = ImproveAdapter(ImproveSettings(self.repo, self.improve))
        self.digest = self.card()

    def tearDown(self) -> None:
        self._t.cleanup()

    def make_patch(self, files: dict) -> str:
        clone = self.root / f"clone-{len(list(self.root.iterdir()))}"
        git(self.root, "clone", "-q", str(self.repo), clone.name)
        head = commit(clone, files, "Daedalus")
        return subprocess.run(["git", "diff-tree", "-p", "--no-renames", "--binary",
                               "--full-index", "--no-color", self.base, head], cwd=clone,
                              check=True, capture_output=True, text=True).stdout

    def card(self, patch=None, **extra) -> str:
        data = (patch or self.patch).encode("utf-8")
        store.freeze(self.improve, CID, data.decode("utf-8"))
        return store.write_card(self.improve, CID, data, {**extra,
            "title": "etsy.digital: stop the medical false alarm", "base": self.base,
            "tests": [REG], "evidence": {"worker": "etsy.digital", "count": 12,
                                         "first_seen": "2026-10-05 01:00",
                                         "last_seen": "2026-10-11 23:00",
                                         "reason": "labels[N]: makes a claim ('medical')",
                                         "samples": ["one", "two", "three"]},
            "hypothesis": "a false alarm", "verdict": {"approved": True,
                                                       "summary": "It stops the false alarm."},
            "verification": "failed without, passed with", "diff_stat": "2 files changed",
            **extra})

    def fake_run(self, argv, cwd, timeout):
        self.ran.append(argv[7:])
        out = Path(argv[6])
        out.write_text(json.dumps({"ran": 1, "outcomes": {} if self.tests_pass else
                                   {"x.T.t": "failure:AssertionError"},
                                   "skipped": 0, "ok": self.tests_pass}), encoding="utf-8")
        return (0 if self.tests_pass else 1), "", False

    def task(self, **payload) -> Task:
        return Task(APPLY, payload or {"id": CID, "digest": self.digest},
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
        self.assertEqual(context["diff"], self.patch)          # the WHOLE diff, on the card
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
            self.adapter.validate(self.task(id=CID, digest=self.digest, extra="x"))

    def test_the_base_or_the_tests_changed_after_the_card_is_refused(self) -> None:
        card = store.folder(self.improve, CID) / store.CARD
        for key, value in (("tests", ["tests/test_other.py"]), ("base", "f" * 40)):
            doc = json.loads(card.read_text(encoding="utf-8"))
            good = doc[key]
            doc[key] = value
            card.write_text(json.dumps(doc), encoding="utf-8")
            with self.subTest(key=key), self.assertRaises(AdapterProtocolError):
                self.adapter.validate(self.task())
            doc[key] = good
            card.write_text(json.dumps(doc), encoding="utf-8")
        self.adapter.validate(self.task())

    def test_a_patch_too_big_for_ians_card_is_refused(self) -> None:
        big = self.make_patch({"src/pionir/crew/etsy/maker.py": FIXED + "".join(
            f"X{i} = '{'y' * 60}'\n" for i in range(120))})
        self.digest = self.card(big)
        with self.assertRaises(AdapterProtocolError) as caught:
            self.adapter.validate(self.task())
        self.assertIn("Ian's card", str(caught.exception))

    def test_a_protected_only_patch_is_an_apply_by_hand_draft_not_a_refusal(self) -> None:
        # editing protected code is no longer refused here: it is a DRAFT Ian applies by hand.
        # validate passes, the card is marked manual with paste-ready steps, and execute commits
        # NOTHING.
        draft = self.make_patch({"src/pionir/approvals.py": "# relaxed\n", REG: REGRESSION})
        self.digest = self.card(draft, manual=True)
        self.adapter.validate(self.task())                       # does not raise
        ctx = self.adapter.park_context(self.task())
        self.assertTrue(ctx["manual"])
        self.assertTrue(ctx["apply_by_hand"])
        self.assertIn("git apply", " ".join(ctx["manual_steps"]))
        self.assertIn("committed by the bot", ctx["runs"])
        result = self.adapter.execute(self.task())
        self.assertTrue(result.output["ok"])
        self.assertTrue(result.output["manual"])
        self.assertFalse(result.output["applied"])
        self.assertEqual(self.head(), self.base)                 # the repo is never touched
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertTrue(self.outcome()["manual"])

    def test_a_non_protected_refusal_is_still_refused_here(self) -> None:
        # the apply-by-hand relaxation is only for a protected-ONLY patch: a new dependency in a
        # patchable file is still refused before anything parks or runs.
        bad = self.make_patch({"src/pionir/crew/etsy/maker.py": "import no_such_dep\n" + FIXED})
        self.digest = self.card(bad)
        with self.assertRaises(AdapterProtocolError) as caught:
            self.adapter.validate(self.task())
        self.assertIn("refuses it", str(caught.exception))


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
        self.assertEqual((self.repo / "src/pionir/crew/etsy/maker.py").read_text(
            encoding="utf-8").replace("\r\n", "\n"), FIXED)
        self.assertEqual(self.outcome()["commit"], self.head())

    def test_nothing_of_the_patch_is_run_as_ian(self) -> None:
        # its tests ran contained before he was asked: a run here, as him, could leave side
        # effects that no revert takes back
        from pionir.selfimprove import apply as applier
        calls = []
        real = applier.subprocess.run

        def spy(argv, *a, **kw):
            calls.append(list(argv))
            return real(argv, *a, **kw)
        applier.subprocess.run = spy
        try:
            result = self.adapter.execute(self.task())
        finally:
            applier.subprocess.run = real
        self.assertTrue(result.output["applied"])
        self.assertEqual(result.output["tests"], [REG])
        self.assertTrue(calls)
        self.assertTrue(all(c[0] == "git" for c in calls), calls)

    def test_main_moved_but_still_applies(self) -> None:
        commit(self.repo, {"README.md": "Pionir, moved on\n"})
        result = self.adapter.execute(self.task())
        self.assertTrue(result.output["applied"])

    def test_main_moved_and_no_longer_applies_is_stale_and_untouched(self) -> None:
        moved = commit(self.repo, {"src/pionir/crew/etsy/maker.py": RULES.replace(
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

    def test_an_untracked_file_where_the_patch_writes_is_never_clobbered(self) -> None:
        (self.repo / REG).parent.mkdir(parents=True, exist_ok=True)
        (self.repo / REG).write_text("# Ian's own scratch test\n", encoding="utf-8")
        result = self.adapter.execute(self.task())
        self.assertFalse(result.output["applied"])
        self.assertIn("already exists", result.output["why"])
        self.assertEqual((self.repo / REG).read_text(encoding="utf-8"),
                         "# Ian's own scratch test\n")
        self.assertEqual(self.head(), self.base)

    def test_exactly_the_bytes_read_once_are_applied(self) -> None:
        from pionir.selfimprove import apply as applier
        other = self.make_patch({"src/pionir/crew/etsy/maker.py": FIXED.replace(
            "None", "'other'"), REG: REGRESSION})
        done = applier.apply_patch(self.repo, other.encode("utf-8"), CID, self.base,
                                   title="t", tests=[REG])
        self.assertTrue(done["applied"], done)
        self.assertIn("'other'", git(self.repo, "show", "HEAD:src/pionir/crew/etsy/maker.py"))

    def test_the_guard_runs_again_on_the_trees_in_ians_index(self) -> None:
        commit(self.repo, {"tests/test_maker.py": "import unittest\n\n\nclass T(unittest.TestCase):"
                                                  "\n    def test_a(self):\n        x = 1\n"
                                                  "        self.assertEqual(x, 1)\n"})
        self.base = self.head()
        weak = self.make_patch({"tests/test_maker.py": "import unittest\n\n\nclass T(unittest."
                                                      "TestCase):\n    def test_a(self):\n"
                                                      "        x = 1\n        self.assertEqual(1, 1)\n"})
        from pionir.selfimprove import apply as applier
        done = applier.apply_patch(self.repo, weak.encode("utf-8"), CID, self.base, title="t",
                                   tests=[])
        self.assertFalse(done["applied"])
        self.assertIn("real assert", done["error"])
        self.assertEqual(self.head(), self.base)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

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
        self.assertIn("Revert", git(self.repo, "log", "-1", "--format=%s"))
        self.assertEqual(git(self.repo, "rev-parse", "HEAD~1"), applied)
        self.assertEqual((self.repo / "src/pionir/crew/etsy/maker.py").read_text(
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
                  "evidence": {"worker": "etsy.digital", "reason": "labels[N]: medical"}}},
            "nights": {"2026-10-12": {"needs_human": [{
                "worker": "etsy.digital", "reason": "title: N-N characters", "count": 10,
                "why": "needs a human: it touches a protected rule (src/pionir/crew/etsy/"
                       "rules.py)"}]}}})
        self.adapter.execute(self.task())
        out = io.StringIO()
        self.assertEqual(improvecli.run(Namespace(improve_command="status"), self.settings(),
                                        out=out), 0)
        text = out.getvalue()
        self.assertIn("Proposals made: 1", text)
        self.assertIn(CID, text)
        self.assertIn("applied", text)                     # folded in from outcome.json
        self.assertIn("etsy.digital", text)
        self.assertIn("NEEDS A HUMAN: etsy.digital: title: N-N characters (10x)", text)

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
