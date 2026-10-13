"""What a self-improvement patch may touch, and what it may never do (selfimprove/guard.py).

The patches are real ``git diff-tree -p`` output from temporary repositories, the way the worker
reads Daedalus's commit. Every entry of the protected list is refused one by one, the allowlist
refuses what is outside it, the import closure of the self-improvement code is computed here so
the protected list cannot drift from it, and each rule (a deleted file, a lost or tautological
assert, a skip marker, test machinery, a new or dynamic import, a binary or a link, the size cap,
the catalogue's wiring, git's own line splitting) has a test that fails when it is reverted.
"""
from __future__ import annotations

import ast
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from pionir.selfimprove import guard

SRC = Path(__file__).resolve().parents[1] / "src"
TEST = ("import unittest\n\n\nclass T(unittest.TestCase):\n    def test_a(self):\n"
        "        x = 2\n        self.assertEqual(x, 2)\n        self.assertTrue(x > 1)\n\n"
        "    def test_b(self):\n        y = 3\n        assert y == 3\n")
CATALOGUE = {"providers": {"none": 0.0}, "divisions": [{"id": "etsy", "workers": [
    {"name": "digital", "impl": "etsy_digital", "provider": "none", "uses": ["etsy.create"],
     "params": {"max_title": 140}}]}, {"id": "builds", "workers": [
    {"name": "improve", "impl": "self_improver", "provider": "claude",
     "uses": ["improve.apply"], "params": {"per_night": 2}}]}]}
RULE = "src/pionir/crew/etsy/maker.py"


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
        return {rel: (self.root / rel).read_bytes()
                for rel in git(self.root, "ls-files").split()}


def sample(pattern: str) -> str:
    """A concrete path each protected glob matches."""
    p = pattern
    if p.startswith("**/"):
        p = "src/pionir/sub/" + p[3:]
    return p.replace("/**", "/sub/mod.py").replace("*", "x")


def closure(modules) -> set:
    """Every Pionir source file the given modules import, transitively - module-level and
    inside functions, and every package __init__ on the way."""

    def path_of(name: str) -> Path | None:
        p = SRC / Path(*name.split("."))
        if (p / "__init__.py").is_file():
            return p / "__init__.py"
        return p.with_suffix(".py") if p.with_suffix(".py").is_file() else None

    seen, todo, files = set(), list(modules), set()
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        path = path_of(name)
        if path is None:
            continue
        files.add(path.relative_to(SRC.parent).as_posix())
        parts = name.split(".")
        todo += [".".join(parts[:i]) for i in range(1, len(parts))]
        package = name if path.name == "__init__.py" else name.rsplit(".", 1)[0]
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                todo += [a.name for a in node.names if a.name.startswith("pionir")]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    up = package.split(".")
                    mod = ".".join(up[:len(up) - node.level + 1] + ([node.module]
                                                                  if node.module else []))
                else:
                    mod = node.module or ""
                if mod.startswith("pionir"):
                    todo += [mod] + [f"{mod}.{a.name}" for a in node.names]
    return files


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._t = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.repo = Repo(Path(self._t.name), {
            RULE: "def check(x):\n    return x\n",
            "tests/test_rules.py": TEST,
            guard.CATALOGUE: json.dumps(CATALOGUE, indent=1) + "\n"})

    def tearDown(self) -> None:
        self._t.cleanup()


class ProtectedTests(_Case):
    def test_every_protected_entry_is_refused(self) -> None:
        self.assertGreater(len(guard.PROTECTED), 60)
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
                     "pyproject.toml", "pionir.ps1", "SRC/Pionir/Approvals.py",
                     "src/pionir/crew/contentcheck.py", "src/pionir/crew/content_allowlist.json",
                     "src/pionir/crew/etsy/rules.py", "src/pionir/video/disclosure.py",
                     "src/pionir/workapi.py", "src/pionir/crew/gpu.py",
                     "src/pionir/video/tutorial.py", "src/pionir/crew/builds/window.py"):
            with self.subTest(path=path):
                self.assertIsNotNone(guard.protected_why(path))

    def test_every_module_the_self_improvement_code_imports_is_protected(self) -> None:
        files = closure(["pionir.crew.improve.worker", "pionir.crew.improve.sandbox",
                         "pionir.crew.improve.detect", "pionir.crew.improve.prompts",
                         "pionir.selfimprove.apply", "pionir.selfimprove.store",
                         "pionir.selfimprove.runner", "pionir.selfimprove.guard",
                         "pionir.improvecli", "pionir.adapters.improve"])
        self.assertIn("src/pionir/crew/builds/window.py", files)
        self.assertIn("src/pionir/atomic.py", files)
        loose = sorted(f for f in files
                       if not guard.protected_why(f) and not guard.allowed_why_not(f))
        self.assertEqual(loose, [], "add these to guard.PROTECTED: the self-improvement "
                                    "code imports them")

    def test_an_ordinary_module_may_be_changed(self) -> None:
        self.assertIsNone(guard.protected_why(RULE))
        patch = self.repo.change({RULE: "def check(x):\n    return x or None\n"})
        self.assertEqual(guard.patch_problems(patch), [])


class AllowlistTests(_Case):
    def test_only_pionir_modules_tests_and_the_catalogue(self) -> None:
        for path in ("README.md", "docs/notes.md", "src/pionir/crew/etsy/data.json",
                     "examples/x.py", "tests/data/x.py", "tests/helper.py", "src/x.py",
                     "src/pionir/crew/etsy/__init__.py", "src/pionir/sitecustomize.py",
                     "src/pionir/crew/usercustomize.py", ".gitattributes", ".gitignore",
                     "src/pionir/crew/.gitattributes", "src/pionir/bootstrap.py"):
            with self.subTest(path=path):
                self.assertTrue(guard.protected_why(path) or guard.allowed_why_not(path,
                                                                                    "added"))
                patch = self.repo.change({path: "x = 1\n"})
                self.assertTrue(guard.patch_problems(patch), path)

    def test_a_new_top_level_module_is_refused_but_a_new_crew_module_is_not(self) -> None:
        patch = self.repo.change({"src/pionir/newthing.py": "x = 1\n"})
        self.assertTrue(any("new top-level module" in p for p in guard.patch_problems(patch)))
        patch = self.repo.change({"src/pionir/crew/etsy/titles.py": "x = 1\n"})
        self.assertEqual(guard.patch_problems(patch), [])

    def test_a_git_path_is_refused_in_any_case(self) -> None:
        for part in (".git", ".GIT", ".Git"):
            patch = (f"diff --git a/src/{part}/hooks/x b/src/{part}/hooks/x\nnew file mode 100644\n"
                     f"--- /dev/null\n+++ b/src/{part}/hooks/x\n@@ -0,0 +1 @@\n+x\n")
            self.assertTrue(any("git file" in p for p in guard.patch_problems(patch)), part)


class TestWeakeningTests(_Case):
    def test_a_lost_assert_is_refused(self) -> None:
        base = self.repo.tree()
        patch = self.repo.change({"tests/test_rules.py": TEST.replace(
            "        self.assertTrue(x > 1)\n", "")})
        self.assertTrue(any("loses 1 assert" in p for p in guard.patch_problems(patch)))
        tree = guard.tree_problems(["tests/test_rules.py"], base, self.repo.tree())
        self.assertTrue(any("fewer real assert" in p for p in tree), tree)

    def test_a_tautology_in_place_of_an_assert_is_refused(self) -> None:
        base = self.repo.tree()
        patch = self.repo.change({"tests/test_rules.py": TEST.replace(
            "self.assertTrue(x > 1)", "self.assertTrue(True)").replace(
            "assert y == 3", "assert True")})
        self.assertEqual(guard.patch_problems(patch), [])       # the diff alone cannot tell
        tree = guard.tree_problems(["tests/test_rules.py"], base, self.repo.tree())
        self.assertEqual(sum("fewer real assert" in p for p in tree), 2, tree)

    def test_an_assert_of_a_thing_equal_to_itself_is_not_one(self) -> None:
        base = self.repo.tree()
        self.repo.change({"tests/test_rules.py": TEST.replace(
            "self.assertEqual(x, 2)", "self.assertEqual(x, x)")})
        tree = guard.tree_problems(["tests/test_rules.py"], base, self.repo.tree())
        self.assertTrue(any("test_a has 1 fewer real assert" in p for p in tree), tree)

    def test_a_changed_assert_is_not_a_lost_one(self) -> None:
        base = self.repo.tree()
        patch = self.repo.change({"tests/test_rules.py": TEST.replace(
            "self.assertEqual(x, 2)", "self.assertEqual(x * 2, 4)")})
        self.assertEqual(guard.patch_problems(patch), [])
        self.assertEqual(guard.tree_problems(["tests/test_rules.py"], base,
                                             self.repo.tree()), [])

    def test_a_removed_test_is_refused(self) -> None:
        base = self.repo.tree()
        self.repo.change({"tests/test_rules.py": TEST.replace(
            "    def test_b(self):\n", "    def helper(self):\n")})
        tree = guard.tree_problems(["tests/test_rules.py"], base, self.repo.tree())
        self.assertTrue(any("no longer has T.test_b" in p for p in tree), tree)

    def test_a_skip_marker_is_refused(self) -> None:
        base = self.repo.tree()
        patch = self.repo.change({"tests/test_rules.py": TEST.replace(
            "    def test_a(self):", "    @unittest.skip('flaky')\n    def test_a(self):")})
        self.assertTrue(any("skip" in p for p in guard.patch_problems(patch)))
        tree = guard.tree_problems(["tests/test_rules.py"], base, self.repo.tree())
        self.assertTrue(any("skip" in p for p in tree), tree)

    def test_test_code_may_not_reach_into_the_test_machinery(self) -> None:
        for line in ("unittest.TestResult.addFailure = lambda *a: None",
                     "import sys; sys.exit(0)", "json.dump({}, open(sys.argv[1], 'w'))",
                     "def load_tests(loader, tests, pattern): return tests",
                     "setattr(unittest, 'TestCase', object)",
                     "def pytest_collection_modifyitems(items): items.clear()"):
            with self.subTest(line=line):
                patch = self.repo.change({"tests/test_rules.py": TEST + f"\n{line}\n"})
                self.assertTrue(any("test machinery" in p or "unittest or pytest" in p
                                    for p in guard.patch_problems(patch)),
                                guard.patch_problems(patch))
                self.repo.change({"tests/test_rules.py": TEST})

    def test_a_deleted_test_file_is_refused(self) -> None:
        patch = self.repo.change({"tests/test_rules.py": None})
        self.assertTrue(any("deletes tests/test_rules.py - a test file" in p
                            for p in guard.patch_problems(patch)))

    def test_any_deleted_file_is_refused(self) -> None:
        patch = self.repo.change({RULE: None})
        self.assertTrue(any("deletes" in p for p in guard.patch_problems(patch)))


class DependencyTests(_Case):
    def test_a_new_third_party_import_is_refused(self) -> None:
        patch = self.repo.change({RULE: "import requests\n\n\ndef check(x):\n    return x\n"})
        self.assertTrue(any("'requests'" in p and "dependency" in p
                            for p in guard.patch_problems(patch)))

    def test_a_conditional_import_is_still_an_import(self) -> None:
        base = self.repo.tree()
        self.repo.change({RULE: "def check(x):\n    if x:\n        try:\n"
                                "            import yaml_ish as y\n        except Exception:\n"
                                "            y = None\n    return x\n"})
        tree = guard.tree_problems([RULE], base, self.repo.tree())
        self.assertTrue(any("'yaml_ish'" in p for p in tree), tree)

    def test_a_dynamic_import_is_refused(self) -> None:
        for text in ("import importlib\nm = importlib.import_module('re')\n",
                     "m = __import__('re')\n", "name = 'x'\nm = __import__(name)\n"):
            with self.subTest(text=text):
                base = self.repo.tree()
                patch = self.repo.change({RULE: text})
                self.assertTrue(any("dynamic import" in p for p in guard.patch_problems(patch)))
                self.assertTrue(any("dynamic import" in p for p in guard.tree_problems(
                    [RULE], base, self.repo.tree())))
                self.repo.change({RULE: "def check(x):\n    return x\n"})

    def test_what_pionir_already_imports_is_not_new(self) -> None:
        base = {**self.repo.tree(), "src/pionir/other.py": b"import yaml\n"}
        self.repo.change({RULE: "import re\nimport yaml\nfrom pionir.crew import words\n"
                                "from PIL import Image\nimport support\n\n\n"
                                "def check(x):\n    return x\n"})
        self.assertEqual(guard.tree_problems([RULE], base, self.repo.tree()), [])
        self.assertEqual(guard.patch_problems(git(self.repo.root, "diff-tree", "-p",
                                                  "--full-index", "HEAD~1", "HEAD"),
                                              known_imports={"yaml"}), [])

    def test_the_dependency_files_are_protected(self) -> None:
        patch = self.repo.change({"requirements.txt": "requests==2.0\n"})
        self.assertTrue(any("is protected" in p for p in guard.patch_problems(patch)))


class ShapeTests(_Case):
    def test_a_patch_bigger_than_ians_card_is_refused(self) -> None:
        body = "".join(f"X{i} = '{'y' * 60}'\n" for i in range(100))
        patch = self.repo.change({"src/pionir/crew/etsy/big.py": body})
        self.assertGreater(len(patch), guard.MAX_CHARS)
        self.assertTrue(any("Ian's card" in p for p in guard.patch_problems(patch)))

    def test_too_many_lines_or_files_is_refused(self) -> None:
        body = "".join(f"X{i}=1\n" for i in range(guard.MAX_LINES + 1))
        patch = self.repo.change({"src/pionir/crew/etsy/big.py": body})
        problems = guard.patch_problems(patch)
        self.assertTrue(any("fit one review" in p or "Ian's card" in p for p in problems))
        patch = self.repo.change({f"src/pionir/crew/etsy/m{i}.py": "x = 1\n"
                                  for i in range(guard.MAX_FILES + 1)})
        self.assertTrue(any("files" in p and "fit one review" in p
                            for p in guard.patch_problems(patch)))

    def test_a_binary_change_is_refused(self) -> None:
        patch = self.repo.change({"src/pionir/crew/etsy/blob.py": bytes(range(256)) * 4})
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

    def test_lines_are_split_as_git_splits_them(self) -> None:
        # a \r or U+2028 inside a line is content, not a new line that could hide one
        patch = ("diff --git a/tests/test_rules.py b/tests/test_rules.py\n"
                 "index 1111111..2222222 100644\n--- a/tests/test_rules.py\n"
                 "+++ b/tests/test_rules.py\n@@ -1,2 +1,2 @@\n"
                 "-        self.assertEqual(x, 2)\n"
                 "+        y = 1\u2028-        self.assertEqual(x, 2)\n")
        [f] = guard.parse_patch(patch)
        self.assertEqual(len(f.added), 1)
        self.assertEqual(len(f.removed), 1)
        self.assertEqual(f.added[0], "        y = 1\u2028-        self.assertEqual(x, 2)")


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
        self.repo.change({guard.CATALOGUE: json.dumps(doc, indent=1) + "\n"})
        tree = guard.tree_problems([guard.CATALOGUE], base, self.repo.tree())
        self.assertTrue(any("wiring" in p for p in tree), tree)


if __name__ == "__main__":
    unittest.main()
