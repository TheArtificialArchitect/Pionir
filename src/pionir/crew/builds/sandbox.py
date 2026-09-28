"""Each product's own sandbox repo, and reading back exactly what was committed in it.

Every product is built in a FRESH git repository created here, directly inside the sandbox
workspace (``C:\\src\\daedalus-work\\<slug>``): ``git init``, seeded with the licence, the
project skeleton and ``BRIEF.md`` (the product on paper, for Daedalus to read), marked as a
sandbox (``.git/pionir-sandbox.json`` - Pionir's ``coding.daedalus_build`` refuses any repo
without it) and committed. Daedalus's live policy commits a passing change onto the checked-out
branch, which here is the sandbox's own ``main``.

Nothing is read back from the working tree: ``export`` takes the files as COMMITTED at HEAD
(``git ls-tree`` / ``git cat-file``), refusing a link or a submodule, so the tests that are run,
the files Claude reviews and the zip that is staged are the same bytes.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from pionir.adapters.daedalus import SANDBOX_MARKER, sandbox_repo_problem

from .backlog import listing

GIT_TIMEOUT = 60
MAX_FILES = 300
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 8_000_000
BRIEF_FILE = "BRIEF.md"
# The seed commit's author: the sandbox repo is never pushed, and nothing of .git ships.
_IDENTITY = ["-c", "user.name=builds", "-c", "user.email=builds@localhost.invalid"]
_SAFE_PART = re.compile(r"[A-Za-z0-9_.@+-][A-Za-z0-9_.@+ -]{0,120}")


class SandboxError(RuntimeError):
    pass


def git(args: list, cwd, *, run=subprocess.run, timeout: float = GIT_TIMEOUT,
        text: bool = True):
    """One git command in ``cwd``; its stdout, or SandboxError. Never a shell."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
    try:
        done = run(["git", *args], cwd=str(cwd), capture_output=True, timeout=timeout,
                   env=env, check=False, **({"text": True, "encoding": "utf-8",
                                             "errors": "replace"} if text else {}))
    except (OSError, subprocess.SubprocessError) as exc:
        raise SandboxError(f"git {args[0]} could not run ({type(exc).__name__})") from exc
    if done.returncode != 0:
        err = done.stderr if isinstance(done.stderr, str) else (done.stderr or b"").decode(
            "utf-8", "replace")
        raise SandboxError(f"git {args[0]} failed: {' '.join(err.split())[:200]}")
    return done.stdout


def short_name(entry: dict) -> str:
    return entry["name"].split(":", 1)[0].strip()


LICENSE = """{short} 1.0.0 - Single-developer commercial licence

Copyright (c) {year} the author of {short} (the seller named on the page this copy was
bought from). All rights not granted below are reserved.

1. What you may do
   One developer - the person who bought this copy, or one named person at the
   organisation that bought it - may use, copy and modify this software in any
   number of their own projects, including commercial ones, and may deploy or
   ship it as part of those projects.

2. What you may not do
   You may not redistribute, sublicense, resell, share or publish the source code
   of this software (modified or not), on its own or as a substantial part of a
   package, template, library, starter kit or product whose main purpose is to
   provide what this software provides. Each additional developer needs their own
   licence.

3. Third-party parts
   Parts of this package, if any, are third-party work under their own licences,
   listed in THIRD_PARTY.txt. This licence covers only the author's own work.

4. No warranty
   THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
   IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
   FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.

5. Limitation of liability
   IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
   LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
   OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
   SOFTWARE.
"""

THIRD_PARTY = """Third-party parts bundled with {short} 1.0.0
{rule}

None. {short} uses only {runtime}.
"""

GITIGNORE = """__pycache__/
*.pyc
*.egg-info/
build/
dist/
.venv/
node_modules/
.pytest_cache/
"""


def _toml(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)       # a JSON string is a valid TOML basic string


def license_text(entry: dict, year: int) -> str:
    return LICENSE.format(short=short_name(entry), year=year)


def seed_files(entry: dict, year: int) -> dict:
    """The files every sandbox repo starts with: {relative path: text}."""
    short = short_name(entry)
    runtime = ("the Python standard library" if entry["language"] == "python"
               else "Node.js's built-in modules")
    head = f"Third-party parts bundled with {short} 1.0.0"
    files = {
        ".gitignore": GITIGNORE,
        "LICENSE.txt": license_text(entry, year),
        "THIRD_PARTY.txt": THIRD_PARTY.format(short=short, rule="=" * len(head),
                                              runtime=runtime),
        BRIEF_FILE: brief_md(entry),
    }
    if entry["language"] == "python":
        files["pyproject.toml"] = (
            '[build-system]\nrequires = ["setuptools>=77"]\n'
            'build-backend = "setuptools.build_meta"\n\n'
            f'[project]\nname = {_toml(entry["slug"])}\nversion = "1.0.0"\n'
            f'description = {_toml(entry["summary"])}\nreadme = "README.md"\n'
            'requires-python = ">=3.11"\n'
            'license = "LicenseRef-Single-Developer-Commercial"\n'
            'license-files = ["LICENSE.txt", "THIRD_PARTY.txt"]\ndependencies = []\n\n'
            f'[project.scripts]\n{entry["command"]} = "{entry["package"]}.cli:main"\n\n'
            '[tool.setuptools.packages.find]\nwhere = ["src"]\n\n'
            '[tool.pytest.ini_options]\npythonpath = ["src"]\ntestpaths = ["tests"]\n')
    else:
        files["package.json"] = json.dumps({
            "name": entry["slug"], "version": "1.0.0", "description": entry["summary"],
            "license": "SEE LICENSE IN LICENSE.txt", "type": "module",
            "bin": {entry["command"]: f"bin/{entry['command']}.js"},
            "scripts": {"test": "node --test"}, "engines": {"node": ">=20"}}, indent=2) + "\n"
    return files


def layout(entry: dict) -> str:
    if entry["language"] == "python":
        return (f"- the package in src/{entry['package']}/ (__init__.py, __main__.py and "
                f"cli.py with an argparse main() - pyproject.toml already maps the command "
                f"`{entry['command']}` to {entry['package']}.cli:main), so `python -m "
                f"{entry['package']}` works\n"
                "- unittest tests in tests/test_*.py, run with PYTHONPATH=src and "
                "`python -m unittest discover -s tests`\n"
                "- Python 3.11 or newer and the standard library ONLY: no third-party "
                "package, nothing to pip install")
    return (f"- the code in src/ as ES modules, and bin/{entry['command']}.js as the "
            "command-line entry (package.json already names it)\n"
            "- tests in test/*.test.js using node:test, run with `node --test`\n"
            "- Node.js 20 or newer and built-in modules ONLY: no npm dependency")


def brief_md(entry: dict) -> str:
    """The product on paper, committed as BRIEF.md for Daedalus (never shipped)."""
    features = "\n".join(f"- {f}" for f in entry["features"])
    tests = "\n".join(f"- {t}" for t in entry["acceptance"])
    return (f"# {entry['name']}\n\n{entry['summary']}\n\n## What it does\n\n"
            f"{entry['brief']}\n\n## Features the listing promises (each must be true)\n\n"
            f"{features}\n\n## Acceptance tests (write each one as a real test)\n\n{tests}\n\n"
            f"## Honest limits (the README says these)\n\n{entry['limits']}\n\n"
            f"## Layout and rules\n\n{layout(entry)}\n{RULES}\n")


RULES = """- NO network access of any kind anywhere (no sockets, HTTP, urllib.request, fetch,
  webbrowser, subprocess calls to curl or similar), no telemetry, no analytics, no update
  checks: the product runs fully offline
- no secrets, tokens or keys; no person's name, company name, email address, machine path
  or user name in any file (use example.com and made-up generic data in examples and tests)
- README.md: what it does, install, usage with real examples of the command, every option,
  the Python API, and the honest limits - every statement true of the code
- CHANGELOG.md with a 1.0.0 entry
- keep LICENSE.txt and THIRD_PARTY.txt exactly as they are; do not edit BRIEF.md
- at least 8 meaningful tests covering the acceptance tests above; all must pass
"""


def create(root, entry: dict, *, year: int, created_at: float, run=subprocess.run) -> str:
    """A fresh sandbox repo for this entry, seeded and committed; returns the seed commit.
    Refuses a folder that already exists (it was not made for this product) and checks the
    result with the very rule Pionir's adapter enforces."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    repo = root / entry["slug"]
    if repo.exists() or repo.is_symlink():
        raise SandboxError(f"{repo} already exists; a sandbox repo is always fresh")
    repo.mkdir()
    git(["init", "-q", "-b", "main"], repo, run=run)
    for rel, text in seed_files(entry, year).items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    marker = {"slug": entry["slug"], "created_at": created_at,
              "made_by": "the crew's Builds worker", "listing": listing(entry)["name"]}
    (repo / ".git" / SANDBOX_MARKER).write_text(json.dumps(marker, indent=1), encoding="utf-8")
    git(["add", "-A"], repo, run=run)
    git([*_IDENTITY, "commit", "-q", "-m",
         f"Seed {entry['slug']}: brief, licence and project skeleton"], repo, run=run)
    problem = sandbox_repo_problem(str(repo), str(root))
    if problem:
        raise SandboxError(f"the new sandbox repo does not pass the sandbox rule: {problem}")
    return head(repo, run=run)


def head(repo, *, run=subprocess.run) -> str:
    return str(git(["rev-parse", "HEAD"], repo, run=run)).strip()


def fast_forward(repo, commit: str, *, run=subprocess.run) -> None:
    """Bring the sandbox's own branch up to a commit Daedalus left elsewhere (its fallback
    when it finds the tree dirty)."""
    if not re.fullmatch(r"[0-9a-f]{7,64}", commit or ""):
        raise SandboxError("not a commit id")
    git(["merge", "-q", "--ff-only", commit], repo, run=run)


def changed_files(repo, base: str, *, run=subprocess.run) -> list:
    """The files changed since the seed commit, as git reports them."""
    out = git(["diff", "--name-status", base, "HEAD"], repo, run=run)
    return [" ".join(line.split()) for line in str(out).splitlines() if line.strip()][:200]


@dataclass
class Tree:
    files: dict = field(default_factory=dict)      # relative path -> bytes, as committed
    problems: list = field(default_factory=list)


def _path_problem(rel: str) -> str | None:
    parts = rel.split("/")
    if rel.startswith("/") or "\\" in rel or ":" in rel or any(
            p in ("", ".", "..") or not _SAFE_PART.fullmatch(p) for p in parts):
        return f"{rel[:80]!r} is not a plain relative path"
    return None


def export(repo, *, run=subprocess.run) -> Tree:
    """Every file committed at HEAD, read from git (never the working tree): a link, a
    submodule, an odd path or an oversized tree is a problem, reported and not read."""
    tree = Tree()
    raw = git(["ls-tree", "-r", "-z", "--full-tree", "HEAD"], repo, run=run, text=False)
    total = 0
    for item in bytes(raw).split(b"\x00"):
        if not item:
            continue
        meta, _, name = item.partition(b"\t")
        try:
            mode, kind, sha = meta.decode("ascii").split()
            rel = name.decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            tree.problems.append("a committed file has an unreadable name")
            continue
        if mode == "120000":
            tree.problems.append(f"{rel[:80]!r} is a symbolic link")
            continue
        if kind != "blob":
            tree.problems.append(f"{rel[:80]!r} is a submodule or not a file")
            continue
        why = _path_problem(rel)
        if why:
            tree.problems.append(why)
            continue
        if len(tree.files) >= MAX_FILES:
            tree.problems.append(f"more than {MAX_FILES} files are committed")
            break
        data = bytes(git(["cat-file", "blob", sha], repo, run=run, text=False))
        if len(data) > MAX_FILE_BYTES or total + len(data) > MAX_TOTAL_BYTES:
            tree.problems.append(f"{rel[:80]!r} is too big ({len(data):,} bytes)")
            continue
        total += len(data)
        tree.files[rel] = data
    return tree


def write_tree(files: dict, dest) -> None:
    """Materialise an exported tree in ``dest`` (a fresh temporary folder)."""
    dest = Path(dest)
    for rel, data in files.items():
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
