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
from pionir.sandbox_git import GitError, is_reparse, prepare, run_git, sanitize

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
    """One git command in ``cwd``; its stdout, or SandboxError. Never a shell, always the
    safe way (pionir/sandbox_git.py: no hooks, no fsmonitor, no attributes, no system or
    global config, a minimal environment) - and, once the repo exists, only after its
    ``.git`` has been rewritten to canonical content (``prepare``): the repo is written by
    generated code, and git runs here as the OWNER."""
    try:
        if (Path(cwd) / ".git").exists() or is_reparse(Path(cwd) / ".git"):
            prepare(cwd)
        return run_git(args, cwd, run=run, timeout=timeout, text=text)
    except (GitError, OSError) as exc:
        raise SandboxError(str(exc)) from exc


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
                "- unittest tests in tests/test_*.py that import the package by its own "
                f"name (`from {entry['package']} import ...`, never `from src.`): they are "
                "run isolated with only src/ on the path\n"
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
    """A fresh sandbox repo for this entry, seeded and committed; returns the seed commit."""
    return create_repo(root, entry["slug"], seed_files(entry, year),
                       marker={"slug": entry["slug"], "created_at": created_at,
                               "made_by": "the crew's Builds worker",
                               "listing": listing(entry)["name"]},
                       message=f"Seed {entry['slug']}: brief, licence and project skeleton",
                       run=run)


def create_repo(root, slug: str, files: dict, *, marker: dict, message: str,
                run=subprocess.run) -> str:
    """A fresh sandbox repo ``<root>/<slug>`` holding ``files`` ({relative path: text}),
    committed; returns the seed commit. Refuses a folder that already exists (it was not
    made for this product) and checks the result with the very rule Pionir's adapter
    enforces. The Builds division and the API builder both seed through here."""
    root = Path(root)
    # the root first, before anything is made: it exists (the setup made it, with its
    # ACL), it is a real folder and not a link or junction to somewhere else
    if is_reparse(root) or not root.is_dir():
        raise SandboxError(f"the sandbox workspace {root} is missing or is a link; run "
                           r"tools\setup-build-sandbox.ps1")
    if os.path.normcase(os.path.realpath(root)) != os.path.normcase(os.path.abspath(root)):
        raise SandboxError(f"the sandbox workspace {root} resolves somewhere else")
    repo = root / slug
    if repo.exists() or is_reparse(repo):
        raise SandboxError(f"{repo} already exists; a sandbox repo is always fresh")
    repo.mkdir()
    git(["init", "-q", "-b", "main"], repo, run=run)
    try:
        sanitize(repo, run=run)             # canonical config, no hooks, from the start
    except GitError as exc:
        raise SandboxError(str(exc)) from exc
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    (repo / ".git" / SANDBOX_MARKER).write_text(json.dumps(marker, indent=1), encoding="utf-8")
    git(["add", "-A"], repo, run=run)
    git([*_IDENTITY, "commit", "-q", "-m", message], repo, run=run)
    problem = sandbox_repo_problem(str(repo), str(root))
    if problem:
        raise SandboxError(f"the new sandbox repo does not pass the sandbox rule: {problem}")
    return head(repo, run=run)


def head(repo, *, run=subprocess.run) -> str:
    out = str(git(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], repo, run=run)).strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", out):
        raise SandboxError("HEAD is not a commit")
    return out


def built_commit(repo, base: str, commit: str, *, run=subprocess.run) -> str:
    """The full id of a commit Daedalus reports (on a branch of its own, when it found the
    tree dirty) - only if it exists and descends from the seed. NEVER checked out or merged
    on the owner's side: its files are read from its objects (``export``)."""
    if not re.fullmatch(r"[0-9a-f]{7,64}", commit or ""):
        raise SandboxError("not a commit id")
    full = str(git(["rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}"], repo,
                   run=run)).strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", full):
        raise SandboxError(f"{commit} is not a commit in the sandbox")
    try:
        git(["merge-base", "--is-ancestor", base, full], repo, run=run)
    except SandboxError as exc:
        raise SandboxError(f"{commit} does not descend from the seed commit") from exc
    return full


def changed_files(repo, base: str, rev: str = "HEAD", *, run=subprocess.run) -> list:
    """The files changed since the seed commit, read from the two trees (plumbing: no
    diff driver, no textconv, no external diff)."""
    out = git(["diff-tree", "-r", "--no-renames", "--no-ext-diff", "--no-textconv",
               "--name-status", base, rev], repo, run=run)
    return [" ".join(line.split()) for line in str(out).splitlines() if line.strip()][:200]


@dataclass
class Tree:
    files: dict = field(default_factory=dict)      # relative path -> bytes, as committed
    problems: list = field(default_factory=list)


_RESERVED = re.compile(r"(?i)^(?:con|prn|aux|nul|com[0-9\u00b9\u00b2\u00b3]|"
                       r"lpt[0-9\u00b9\u00b2\u00b3]|conin\$|conout\$)(?:\..*)?$")


def _path_problem(rel: str) -> str | None:
    parts = rel.split("/")
    if rel.startswith("/") or "\\" in rel or ":" in rel or any(
            p in ("", ".", "..") or not _SAFE_PART.fullmatch(p) for p in parts):
        return f"{rel[:80]!r} is not a plain relative path"
    for part in parts:
        if part != part.rstrip(". "):
            return f"{rel[:80]!r} has a name ending in a dot or a space (Windows drops it)"
        if _RESERVED.match(part):
            return f"{rel[:80]!r} uses a name Windows reserves for a device ({part!r})"
    return None


def export(repo, rev: str = "HEAD", *, run=subprocess.run) -> Tree:
    """Every file committed at ``rev``, read from its objects (never a checkout, never the
    working tree, no filter or smudge ever runs): a link, a submodule, an odd path or an
    oversized tree is a problem, reported and not read."""
    tree = Tree()
    raw = git(["ls-tree", "-r", "-z", "--full-tree", rev], repo, run=run, text=False)
    total = 0
    folded: dict = {}
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
        key = rel.casefold()
        if key in folded:
            # one file on Windows, two in git: which one lands is an accident
            tree.problems.append(f"{rel[:80]!r} and {folded[key][:80]!r} differ only in case")
            continue
        folded[key] = rel
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
