"""What a self-improvement patch may never do (pionir/selfimprove/guard.py).

The patches are real ``git diff-tree -p`` output from temporary repositories, the way the worker
reads Daedalus's commit. Every entry of the protected list is refused one by one, and each rule
(a deleted file, a lost assert, a skip marker, a new dependency, a binary or a link, the size
cap, the catalogue's wiring) has a test that fails when the rule is reverted.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from pionir.selfimprove import guard

TEST = ("import unittest\n\n\nclass T(unittest.TestCase):\n    def test_a(self):\n"
        "        self.assertEqual(1, 1)\n        self.assertTrue(True)\n\n"
        "    def test_b(self):\n        assert 2 == 2\n")
CATALOGUE = {"providers": {"none": 0.0}, "divisions": [{"id": "etsy", "workers": [
    {"name": "digital", "impl": "etsy_digital", "provider": "none", "uses": ["etsy.create"],
     "params": {"max_title": 140}}]}, {"id": "builds", "workers": [
    {"name": "improve", "impl": "self_improver", "provider": "claude",
     "uses": ["improve.apply"], "params": {"per_night": 2}}]}]}


def git(cwd, *args) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "core.autocrlf=false", *args], cwd=cwd, check=True,
                          capture_output=True).stdout.decode("utf-8", "replace")


class Repo:
    """A throw-away repo: ``change`` commits files (text or bytes; None deletes) and returns
    the patch of that one commit, as the worker reads it."""

    def __init__(self, root: Path, files: dict) -> None:
        self.root = root
        git(root, "init", "-q", "-b", "main")
        self.commit(files)

    def commit(self, files: dict) -> None:
        for rel, data in files.items():
            path = self.root / rel
            if data is None:
                path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(data, bytes):
                path.write_bytes(data)
            else:
                path.write_text(data, encoding="utf-8", newline="\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "--allow-empty", "-m", "c")

    def change(self, files: dict) -> str:
        before = git(self.root, "rev-parse", "HEAD").strip()
        self.commit(files)
        return git(self.root, "diff-tree", "-p", "--no-renames", "--no-ext-diff",
                   "--no-textconv", "--binary", "--full-index", "--no-color", before, "HEAD")

    def tree(self) -> dict:
        out = {}
        for rel in git(self.root, "ls-files").split():
            out[rel] = (self.root / rel).read_bytes()
        return out


def sample(pattern: str) -> str:
    """A concrete path each protected glob matches."""
    p = pattern
    if p.startswith("**/"):
        p = "src/pionir/sub/" + p[3:]
    return p.replace("/**", "/sub/mod.py").replace("*", "x")


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._t = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.repo = Repo(Path(self._t.name), {
            "src/pionir/crew/etsy/rules.py": "def check(x):\n    return x\n",
            "tests/test_rules.py": TEST,
            "src/pionir/crew/catalogue.json": json.dumps(CATALOGUE, indent=1) + "\n"})

    def tearDown(self) -> None:
        self._t.cleanup()


class ProtectedTests(_Case):
    def test_every_protected_entry_is_refused(self) -> None:
        self.assertGreater(len(guard.PROTECTED), 40)
        for pattern, why in guard.PROTECTED:
            path = sample(pattern)
            with self.subTest(pattern=pattern, path=path):
                self.assertEqual(guard.protected_why(path), why)
                patch = self.repo.change({path: f"# changed {pattern}\n"})
                problems = guard.patch_problems(patch)
                self.assertTrue(any("is protected" in p and path in p for p in problems),
                                problems)

    def test_the_list_names_each_thing_ian_ruled_out(self) -> None:
        for path in ("src/pionir/approvals.py", "src/pionir/discord_gate.py",
                     "src/pionir/auth.py", "src/pionir/crew/escalation.py",
                     "src/pionir/economy/ledger.py", "src/pionir/secretscrub.py",
                     "src/pionir/build_sandbox.py", "tools/setup-build-sandbox.ps1",
                     ".github/workflows/ci.yml", "src/pionir/crew/improve/worker.py",
                     "src/pionir/selfimprove/guard.py", "src/pionir/adapters/improve.py",
                     "src/pionir/improvement.py", "tests/test_selfimprove_guard.py",
                     "pyproject.toml", "pionir.ps1", "SRC/Pionir/Approvals.py"):
            with self.subTest(path=path):
                self.assertIsNotNone(guard.protected_why(path))

    def test_an_ordinary_rule_file_may_be_changed(self) -> None:
        self.assertIsNone(guard.protected_why("src/pionir/crew/etsy/rules.py"))
        patch = self.repo.change({"src/pionir/crew/etsy/rules.py":
                                  "def check(x):\n    return x or None\n"})
        self.assertEqual(guard.patch_problems(patch), [])


class TestWeakeningTests(_Case):
    def test_a_lost_assert_is_refused(self) -> None:
        base = self.repo.tree()
        patch = self.repo.change({"tests/test_rules.py": TEST.replace(
            "        self.assertTrue(True)\n", "")})
        self.assertTrue(any("loses 1 assert" in p for p in guard.patch_problems(patch)))
        tree = guard.tree_problems(["tests/test_rules.py"], base, self.repo.tree())
        self.assertTrue(any("1 fewer assert" in p for p in tree), tree)

    def test_a_changed_assert_is_not_a_lost_one(self) -> None:
        base = self.repo.tree()
        patch = self.repo.change({"tests/test_rules.py": TEST.replace(
            "assertEqual(1, 1)", "assertEqual(2, 2)")})
        self.assertEqual(guard.patch_problems(patch), [])
        self.assertEqual(guard.tree_problems(["tests/test_rules.py"], base,
                                             self.repo.tree()), [])

    def test_a_removed_test_is_refused(self) -> None:
        base = self.repo.tree()
        self.repo.change({"tests/test_rules.py": TEST.replace(
            "    def test_b(self):\n        assert 2 == 2\n",
            "    def helper(self):\n        assert 2 == 2\n")})
        tree = guard.tree_problems(["tests/test_rules.py"], base, self.repo.tree())
        self.assertTrue(any("no longer has test_b" in p for p in tree), tree)

    def test_a_skip_marker_is_refused(self) -> None:
        base = self.repo.tree()
        patch = self.repo.change({"tests/test_rules.py": TEST.replace(
            "    def test_a(self):", "    @unittest.skip('flaky')\n    def test_a(self):")})
        self.assertTrue(any("skip" in p for p in guard.patch_problems(patch)))
        tree = guard.tree_problems(["tests/test_rules.py"], base, self.repo.tree())
        self.assertTrue(any("skip" in p for p in tree), tree)

    def test_a_deleted_test_file_is_refused(self) -> None:
        patch = self.repo.change({"tests/test_rules.py": None})
        self.assertTrue(any("deletes tests/test_rules.py - a test file" in p
                            for p in guard.patch_problems(patch)))

    def test_any_deleted_file_is_refused(self) -> None:
        patch = self.repo.change({"src/pionir/crew/etsy/rules.py": None})
        self.assertTrue(any("deletes" in p for p in guard.patch_problems(patch)))


class DependencyTests(_Case):
    def test_a_new_third_party_import_is_refused(self) -> None:
        patch = self.repo.change({"src/pionir/crew/etsy/rules.py":
                                  "import requests\n\n\ndef check(x):\n    return x\n"})
        self.assertTrue(any("'requests'" in p and "dependency" in p
                            for p in guard.patch_problems(patch)))

    def test_the_standard_library_pionir_and_declared_deps_are_not_new(self) -> None:
        patch = self.repo.change({"src/pionir/crew/etsy/rules.py":
                                  "import re\nfrom pionir.crew import words\n"
                                  "from PIL import Image\nimport support\n\n\n"
                                  "def check(x):\n    return x\n"})
        self.assertEqual(guard.patch_problems(patch), [])

    def test_the_dependency_files_are_protected(self) -> None:
        patch = self.repo.change({"requirements.txt": "requests==2.0\n"})
        self.assertTrue(any("is protected" in p for p in guard.patch_problems(patch)))


class ShapeTests(_Case):
    def test_too_many_lines_for_one_review_is_refused(self) -> None:
        body = "".join(f"X{i} = {i}\n" for i in range(guard.MAX_LINES + 1))
        patch = self.repo.change({"src/pionir/crew/etsy/big.py": body})
        self.assertTrue(any("fit one review" in p for p in guard.patch_problems(patch)))

    def test_too_many_files_is_refused(self) -> None:
        patch = self.repo.change({f"src/pionir/crew/etsy/m{i}.py": "x = 1\n"
                                  for i in range(guard.MAX_FILES + 1)})
        self.assertTrue(any("files" in p and "fit one review" in p
                            for p in guard.patch_problems(patch)))

    def test_a_binary_change_is_refused(self) -> None:
        patch = self.repo.change({"src/pionir/crew/etsy/blob.bin": bytes(range(256)) * 4})
        self.assertTrue(any("binary" in p for p in guard.patch_problems(patch)))

    def test_an_empty_or_unreadable_patch_is_refused(self) -> None:
        self.assertTrue(guard.patch_problems(""))
        self.assertTrue(guard.patch_problems("not a diff\n"))

    def test_a_quoted_or_odd_path_is_refused(self) -> None:
        patch = ('diff --git "a/src/x\\ty.py" "b/src/x\\ty.py"\nnew file mode 100644\n'
                 '--- /dev/null\n+++ "b/src/x\\ty.py"\n@@ -0,0 +1 @@\n+x = 1\n')
        self.assertTrue(guard.patch_problems(patch))
        patch = ("diff --git a/src/../x.py b/src/../x.py\nnew file mode 100644\n"
                 "--- /dev/null\n+++ b/src/../x.py\n@@ -0,0 +1 @@\n+x = 1\n")
        self.assertTrue(any("plain relative path" in p for p in guard.patch_problems(patch)))


class CatalogueTests(_Case):
    def test_a_setting_may_change(self) -> None:
        base = self.repo.tree()
        doc = json.loads(json.dumps(CATALOGUE))
        doc["divisions"][0]["workers"][0]["params"]["max_title"] = 120
        patch = self.repo.change({guard.CATALOGUE: json.dumps(doc, indent=1) + "\n"})
        self.assertEqual(guard.patch_problems(patch), [])
        self.assertEqual(guard.tree_problems([guard.CATALOGUE], base, self.repo.tree()), [])

    def test_a_workers_uses_may_not(self) -> None:
        base = self.repo.tree()
        doc = json.loads(json.dumps(CATALOGUE))
        doc["divisions"][0]["workers"][0]["uses"].append("etsy.activate")
        patch = self.repo.change({guard.CATALOGUE: json.dumps(doc, indent=1) + "\n"})
        self.assertTrue(any("wiring" in p for p in guard.patch_problems(patch)))
        self.assertTrue(guard.tree_problems([guard.CATALOGUE], base, self.repo.tree()))

    def test_its_own_entry_may_not(self) -> None:
        base = self.repo.tree()
        doc = json.loads(json.dumps(CATALOGUE))
        doc["divisions"][1]["workers"][0]["params"]["per_night"] = 9
        patch = self.repo.change({guard.CATALOGUE: json.dumps(doc, indent=1) + "\n"})
        tree = guard.tree_problems([guard.CATALOGUE], base, self.repo.tree())
        self.assertTrue(any("wiring" in p for p in tree), tree)
        self.assertTrue(patch)


if __name__ == "__main__":
    unittest.main()
