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


BYPASSES = (
    # the first review's exploit
    "from pionir import approvals, auth\nsetattr(auth, 'GRANTABLE_PERMISSIONS', frozenset({'x'}))\n"
    "approvals.requires_owner = lambda *a, **k: False\n",
    # the final review's bypasses
    "from pionir import auth\nauth.PERMISSION_SCOPES['k'] = 'v'\n",
    "from pionir import auth\nauth.PERMISSION_SCOPES.update({'k': 'v'})\n",
    "from pionir.auth import DEFAULT_GRANTS\nDEFAULT_GRANTS['x'] = 1\n",
    "import sys\nm = sys.modules.get('pionir.auth')\nm.X = 1\n",
    "from pionir import auth\nif (m := auth):\n    m.X = 1\n",
    "from pionir import auth\ndef f(m=auth):\n    m.X = 1\n",
    "from pionir import auth\nfor m in [auth]:\n    m.X = 1\n",
    "from pionir import auth\nwith auth as m:\n    m.X = 1\n",
    "from pionir import auth\nms = [m for m in (auth,)]\nms[0].X = 1\n",
    "from pionir import auth\na, b = auth, 1\na.X = 1\n",
    "exec('from pionir import auth; auth.X = 1')\n",
    "eval('1')\n",
    "import operator\nfrom pionir import auth\noperator.setitem(auth.PERMISSION_SCOPES, 'k', 1)\n",
    "import functools\nfrom pionir import auth\nfunctools.partial(setattr, auth)('X', 1)\n",
    "from pionir import auth\nobject.__setattr__(auth, 'X', 1)\n",
    "from pionir import auth\ntype(auth).__setattr__(auth, 'X', 1)\n",
    "from pionir import auth\nauth.__dict__['X'] = 1\n",
    "from pionir import auth\nvars(auth)['X'] = 1\n",
    "globals()['x'] = 1\n",
    "import importlib\nimportlib.reload(importlib)\n",
    "import builtins\nbuiltins.open = None\n",
    "name = 'X'\nx = getattr(object, name)\n",
    "from pionir import auth\ndel auth.GRANTABLE_PERMISSIONS\n",
    "from pionir import auth\nauth.GRANTABLE_PERMISSIONS |= {1}\n",
    "from pionir import auth\nholder = [auth.PERMISSION_SCOPES]\nholder[0]['k'] = 1\n",
    "from pionir import auth\ndef leak():\n    return auth.PERMISSION_SCOPES\nleak()['k'] = 1\n",
    "from pionir import auth\nlist.append(auth.X, 1)\n",
    "from unittest import mock\nmock.patch('pionir.auth.X', 1).start()\n",
    # the third review's: refused because the file names a protected module at all
    "from pionir.auth import DEFAULT_GRANTS\nholder = (lambda: DEFAULT_GRANTS)()\nholder['x'] = 1\n",
    "from pionir import auth\nclass C:\n    m = auth\nC.m.X = 1\n",
    "from pionir import auth\nm = auth if True else None\nm.X = 1\n",
    "from pionir.approvals import requires_owner\ndef deco(f):\n    return requires_owner\n"
    "@deco\ndef g():\n    pass\ng.__kwdefaults__ = {}\n",
    "from pionir import auth\nmatch auth:\n    case m:\n        m.X = 1\n",
    "from dataclasses import dataclass, field\nfrom pionir.auth import DEFAULT_GRANTS\n"
    "@dataclass\nclass C:\n    g: dict = field(default_factory=lambda: DEFAULT_GRANTS)\n"
    "C().g['x'] = 1\n",
    "def f():\n    try:\n        from pionir import auth\n    except ImportError:\n        return\n"
    "    return (lambda: auth)()\n",
)


class IndirectTests(_Case):
    """A protected module changed from somewhere else: every bypass two reviews found, from an
    allowlisted module - refused by structure (reflection, imports, taint), not by a list."""

    def setUp(self) -> None:
        super().setUp()
        self.repo.change({"src/pionir/approvals.py": "def requires_owner(*a):\n    return True\n",
                          "src/pionir/auth.py": "GRANTABLE_PERMISSIONS = frozenset()\n"
                                                "PERMISSION_SCOPES = {}\nDEFAULT_GRANTS = {}\n"})
        self.base = self.repo.tree()

    def refused(self, path: str, text: str) -> list:
        self.repo.change({path: text})
        problems = guard.tree_problems([path], self.base, self.repo.tree())
        self.repo.change({path: self.base.get(path, b"").decode() or "x = 1\n"})
        return problems

    def test_the_reviewers_exploit_is_refused(self) -> None:
        problems = self.refused(RULE, BYPASSES[0])
        self.assertTrue(any("setattr" in p for p in problems), problems)
        self.assertTrue(any("protected module" in p for p in problems), problems)

    def test_every_bypass_the_reviews_found_is_refused(self) -> None:
        for text in BYPASSES:
            with self.subTest(text=text):
                self.assertTrue(self.refused(RULE, text), text)

    def test_the_same_from_a_test_is_refused(self) -> None:
        for text in BYPASSES[1:6]:
            with self.subTest(text=text):
                self.assertTrue(self.refused("tests/test_rules.py", TEST + text), text)

    def test_a_test_may_not_patch_a_protected_module(self) -> None:
        test = "tests/test_rules.py"
        for line in ("        with mock.patch('pionir.approvals.requires_owner', lambda: 0):\n"
                     "            pass\n",
                     "        mock.patch('pionir.crew.etsy.maker.check').start()\n"):
            text = ("import unittest\nfrom unittest import mock\n"
                    + TEST.replace("    def test_b(self):", "    def test_c(self):\n" + line
                                   + "\n    def test_b(self):"))
            with self.subTest(line=line):
                self.assertTrue(self.refused(test, text), line)

    def test_a_test_may_patch_an_ordinary_module_in_a_with(self) -> None:
        text = ("import unittest\nfrom unittest import mock\n" + TEST.replace(
            "    def test_b(self):",
            "    def test_c(self):\n        with mock.patch('pionir.crew.etsy.maker.check', "
            "lambda x: x):\n            self.assertEqual(1 + 1, 2)\n\n    def test_b(self):"))
        self.assertEqual(self.refused("tests/test_rules.py", text), [])

    def test_ordinary_code_is_fine(self) -> None:
        self.assertEqual(self.refused(RULE, (
            "import re\n\n\nclass C:\n    pass\n\n\nC.x = 1\nobj = C()\nobj.y = 2\n"
            "d = dict(a=1)\nd['b'] = 2\nd.update(c=3)\no = object()\nmodules = [1]\n"
            "pat = re.compile('x')\nv = getattr(obj, 'y', None)\n")), [])

    def test_adding_an_import_of_a_protected_module_is_refused(self) -> None:
        problems = self.refused(RULE, "from pionir.auth import DEFAULT_GRANTS\n"
                                      "n = len(DEFAULT_GRANTS.keys())\n")
        self.assertTrue(any("imports from a protected module" in p for p in problems), problems)

    def test_taint_alone_refuses_every_write_through_an_import_already_there(self) -> None:
        head = "from pionir import auth\nfrom pionir.auth import DEFAULT_GRANTS\n"
        self.repo.change({RULE: head})
        base = self.repo.tree()
        cases = {
            "store-attr": "auth.PERMISSION_SCOPES['k'] = 'v'\n",
            "store-name": "DEFAULT_GRANTS['x'] = 1\n",
            "mutate": "auth.PERMISSION_SCOPES.update({'k': 'v'})\n",
            "walrus": "if (m := auth):\n    m.X = 1\n",
            "default": "def f(m=auth):\n    m.X = 1\n",
            "for": "for m in auth.ITEMS:\n    m.x = 1\n",
            "with": "with auth as m:\n    m.X = 1\n",
            "unpack": "a, b = auth, 1\na.X = 1\n",
            "comprehension": "[m.update({}) for m in auth.ITEMS]\n",
            "augassign": "auth.GRANTABLE_PERMISSIONS |= {1}\n",
            "del": "del auth.GRANTABLE_PERMISSIONS\n",
            "container": "holder = [auth.PERMISSION_SCOPES]\n",
            "return": "def leak():\n    return auth.PERMISSION_SCOPES\n",
            "argument": "print(auth.PERMISSION_SCOPES)\n",
        }
        for kind, line in cases.items():
            with self.subTest(kind=kind):
                self.repo.change({RULE: head + line})
                problems = guard.tree_problems([RULE], base, self.repo.tree())
                self.assertTrue(any("never reaches into another module" in p
                                    for p in problems), (kind, problems))

    def test_a_protected_import_already_in_the_file_still_refuses_it(self) -> None:
        # by construction: the WHOLE file a patch leaves may not name a protected module,
        # whatever was there before - so no taint trick has anything to start from
        head = "from pionir import auth\nfrom pionir.auth import DEFAULT_GRANTS\n"
        self.repo.change({RULE: head})
        base = self.repo.tree()
        for line in ("n = 1\n", BYPASSES[-1], BYPASSES[-3], BYPASSES[-6]):
            with self.subTest(line=line):
                self.repo.change({RULE: head + line})
                problems = guard.tree_problems([RULE], base, self.repo.tree())
                self.assertTrue(any("anywhere in it" in p for p in problems), problems)

    def test_a_test_imports_only_stdlib_deps_and_patchable_modules(self) -> None:
        for line, ok in (("import json\nimport pytest\n", True),
                         ("from pionir.crew.etsy import maker\n", True),
                         ("from pionir import auth\n", False),
                         ("import support\n", False),
                         ("import requests\n", False)):
            with self.subTest(line=line):
                problems = self.refused("tests/test_rules.py", line + TEST)
                self.assertEqual(not problems, ok, problems)

    def test_existing_code_is_not_judged_again(self) -> None:
        old = "def leak(x):\n    return [x]\n"
        self.repo.change({RULE: old})
        base = self.repo.tree()
        self.repo.change({RULE: old + "y = 2\n"})
        self.assertEqual(guard.tree_problems([RULE], base, self.repo.tree()), [])


class ClosureTests(unittest.TestCase):
    """What protected code imports is protected too - computed from the real tree, so it can
    never drift from it."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.files = {p.relative_to(SRC.parent).as_posix(): p.read_bytes()
                     for p in SRC.rglob("*.py") if "__pycache__" not in p.parts}
        cls.closure = guard.protected_closure(cls.files)

    def test_the_core_holds_the_security_code(self) -> None:
        for path in ("src/pionir/approvals.py", "src/pionir/discord_gate.py",
                     "src/pionir/auth.py", "src/pionir/build_sandbox.py",
                     "src/pionir/sandbox_git.py", "src/pionir/crew/escalation.py",
                     "src/pionir/scheduler.py", "src/pionir/crew/gpu.py", "src/pionir/runtime.py",
                     "src/pionir/secretscrub.py", "src/pionir/economy/ledger.py",
                     "src/pionir/selfimprove/guard.py", "src/pionir/crew/improve/worker.py",
                     "src/pionir/adapters/improve.py", "src/pionir/workapi.py",
                     "src/pionir/adapters/etsy.py", "src/pionir/crew/builds/worker.py",
                     "src/pionir/crew/runtime.py", "src/pionir/crew/workers.py",
                     "src/pionir/crew/leader.py", "src/pionir/crew/fiverr/desk.py",
                     "src/pionir/crew/marketplaces/paths.py", "src/pionir/crew/pool.py",
                     "src/pionir/crew/vitals.py", "src/pionir/crew/clock.py",
                     "src/pionir/crew/products.py"):
            with self.subTest(path=path):
                self.assertTrue(guard.in_core(path))
                self.assertIsNotNone(guard.protected_why(path, self.closure))

    def test_what_protected_code_loads_is_protected(self) -> None:
        for path in ("src/pionir/crew/net.py", "src/pionir/ports.py", "src/pionir/crew/finder.py",
                     "src/pionir/crew/blog.py", "src/pionir/crew/etsy/maker.py"):
            with self.subTest(path=path):
                self.assertIsNotNone(guard.protected_why(path, self.closure))

    def test_the_whole_closure_is_protected(self) -> None:
        reached = [p for p in self.files if guard.protected_why(p)] + sorted(self.closure)
        for path in reached:
            for mod in guard._imported_modules(path, self.files[path].decode("utf-8"),
                                               lazy=guard.in_core(path)):
                dep = guard._path_for(mod, self.files)
                if dep is not None:
                    self.assertTrue(guard.protected_why(dep, self.closure)
                                    or guard.allowed_why_not(dep), (path, dep))

    def test_a_patch_to_such_a_module_is_refused_everywhere(self) -> None:
        net = "src/pionir/crew/net.py"
        patch = (f"diff --git a/{net} b/{net}\nindex 1111111..2222222 100644\n--- a/{net}\n"
                 f"+++ b/{net}\n@@ -1 +1 @@\n-x\n+y\n")
        self.assertTrue(any("is protected" in p for p in guard.patch_problems(
            patch, protected=self.closure)))
        self.assertTrue(guard.tree_problems([net], self.files, {**self.files, net: b"y = 1\n"}))

    def test_what_is_left_to_fix(self) -> None:
        left = sorted(p for p in self.files if p.startswith("src/")
                      and not guard.protected_why(p, self.closure) and not guard.allowed_why_not(p))
        self.assertEqual(left, ["src/pionir/crew/__main__.py", "src/pionir/crew/claudedraft.py",
                                "src/pionir/doctor_fix.py", "src/pionir/memory_health.py",
                                "src/pionir/video/fetch.py", "src/pionir/video/sponsor.py"])

    def test_none_of_those_survive_the_whole_file_import_rule(self) -> None:
        # the honest patchable count is 0: every file not protected as a file still imports a
        # protected name, so the whole-file rule refuses a patch to it. Soundness by
        # construction - nothing a patch leaves can even name protected code - costs us every
        # patch target here, because the protected closure already reaches these modules' imports.
        left = sorted(p for p in self.files if p.startswith("src/")
                      and not guard.protected_why(p, self.closure) and not guard.allowed_why_not(p))
        for path in left:
            head = {**self.files, path: self.files[path] + b"\n_marker = 1\n"}
            problems = guard.tree_problems([path], self.files, head, protected=self.closure)
            with self.subTest(path=path):
                self.assertTrue(any("anywhere in it" in p for p in problems), (path, problems))


class FrozenTablesTests(unittest.TestCase):
    """Defense in depth at the source: the grant, scope, spend and payout tables cannot be
    changed at run time by anything that reaches them."""

    def test_the_security_tables_are_immutable(self) -> None:
        from pionir import auth, workapi
        from pionir.crew import direction
        from pionir.economy import collector, payouts
        for name, table in (("PERMISSION_SCOPES", auth.PERMISSION_SCOPES),
                            ("DEFAULT_GRANTS", auth.DEFAULT_GRANTS),
                            ("RESOURCES", direction.RESOURCES),
                            ("APPROVAL_CAPABILITIES", collector.APPROVAL_CAPABILITIES),
                            ("PAYOUTS", payouts.PAYOUTS),
                            ("PAYOUTS[build_staged]", payouts.PAYOUTS["build_staged"]),
                            ("ROUTES", workapi.ROUTES)):
            with self.subTest(name=name), self.assertRaises(TypeError):
                table["x"] = 1
        with self.assertRaises(AttributeError):
            auth.GRANTABLE_PERMISSIONS.add("x")


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
