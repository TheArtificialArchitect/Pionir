"""A candidate's sandbox: a fresh clone of Pionir's ``main`` for Daedalus, read back safely,
and the contained test runs that verify what it wrote.

**The clone** (``clone``) is made directly inside the sandbox workspace as
``<sandbox>\\pionir-<id>`` exactly the way the night builds make a product's repo: refused if
the folder exists or the workspace is a link, ``git clone --no-hardlinks --single-branch`` of
the owner's ``main`` (no tags, no other branch), its remote removed, ``.git/config`` rewritten
to canonical content (pionir/sandbox_git.py), marked with ``.git/pionir-sandbox.json`` and
proved with the very rule Pionir's ``coding.daedalus_build`` enforces (``sandbox_repo_problem``:
a clone with a remote is refused there). The owner's repo is only read.

**Reading it back**: after Daedalus, the sandbox repo is untrusted (generated code ran in it), so
the owner's side only ever READS objects, through ``builds/sandbox.git`` (``prepare`` first):
``export`` takes every file committed at a commit with ONE ``cat-file --batch`` (no checkout, no
filter, no hook), and ``diff`` is ``diff-tree -p --no-renames --binary --full-index`` (plumbing:
no diff driver, no textconv) - the exact bytes Claude reviews and Ian approves.

**The test runs** (``run_suite``) are contained like the night builds' own test run: as the
``pionir-builds`` user, with the sandbox's interpreter, in a kill-on-close job object, output to
files, the whole tree killed at the timeout; the tree is written to a fresh folder under the
sandbox's runs folder and removed afterwards. Python only - node is never needed here.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path

from pionir import build_sandbox as bs
from pionir.adapters.daedalus import SANDBOX_MARKER, sandbox_repo_problem
from pionir.sandbox_git import GitError, is_reparse, prepare, run_git, sanitize
from pionir.selfimprove import runner

from ..builds.sandbox import SandboxError, _path_problem, git, head

MAX_FILES = 4000
MAX_FILE_BYTES = 5_000_000
MAX_TOTAL_BYTES = 80_000_000
CLONE_TIMEOUT = 300
SUITE_PROCESSES = 16
SUITE_MEMORY_MB = 4096


def slug_for(cid: str) -> str:
    """The sandbox folder for a candidate: ``pionir-imp-20261012-ab12`` (a sandbox slug)."""
    return f"pionir-{cid}"


def owner_head(repo, *, run=subprocess.run) -> str:
    """The owner's ``main`` HEAD, read only."""
    try:
        out = run_git(["rev-parse", "--verify", "--quiet", "refs/heads/main^{commit}"], repo,
                      run=run)
    except GitError as exc:
        raise SandboxError(f"the Pionir repo at {repo} has no main branch ({exc})") from exc
    sha = str(out).strip()
    if len(sha) < 40:
        raise SandboxError(f"the Pionir repo at {repo} has no main branch")
    return sha


def clone(root, cid: str, source, *, marker: dict, run=subprocess.run) -> tuple:
    """A fresh sandbox clone of ``source``'s main for candidate ``cid``: ``(repo, base)``."""
    root = Path(root)
    if is_reparse(root) or not root.is_dir():
        raise SandboxError(f"the sandbox workspace {root} is missing or is a link; run "
                           r"tools\setup-build-sandbox.ps1")
    if os.path.normcase(os.path.realpath(root)) != os.path.normcase(os.path.abspath(root)):
        raise SandboxError(f"the sandbox workspace {root} resolves somewhere else")
    slug = slug_for(cid)
    repo = root / slug
    if repo.exists() or is_reparse(repo):
        raise SandboxError(f"{repo} already exists; a sandbox repo is always fresh")
    try:
        run_git(["clone", "-q", "--no-hardlinks", "--no-tags", "--single-branch", "--branch",
                 "main", str(Path(source).resolve()), slug], root, run=run,
                timeout=CLONE_TIMEOUT)
        run_git(["remote", "remove", "origin"], repo, run=run)
        sanitize(repo, run=run)
    except (GitError, OSError) as exc:
        raise SandboxError(f"the clone of {source} could not be made ({exc})") from exc
    (repo / ".git" / SANDBOX_MARKER).write_text(json.dumps({**marker, "slug": slug}, indent=1),
                                                encoding="utf-8")
    problem = sandbox_repo_problem(str(repo), str(root))
    if problem:
        raise SandboxError(f"the clone does not pass the sandbox rule: {problem}")
    return str(repo), head(repo, run=run)


def export(repo, rev: str, *, run=subprocess.run) -> dict:
    """Every file committed at ``rev`` (path -> bytes), read from its objects with one
    ``cat-file --batch``; a link, a submodule, an odd path or an oversized tree is refused."""
    raw = git(["ls-tree", "-r", "-z", "--full-tree", rev], repo, run=run, text=False)
    entries = []
    for item in bytes(raw).split(b"\x00"):
        if not item:
            continue
        meta, _, name = item.partition(b"\t")
        try:
            mode, kind, sha = meta.decode("ascii").split()
            rel = name.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise SandboxError("a committed file has an unreadable name") from exc
        if mode == "120000" or kind != "blob":
            raise SandboxError(f"{rel[:80]!r} is a link or a submodule")
        why = _path_problem(rel)
        if why:
            raise SandboxError(why)
        entries.append((rel, sha))
    if len(entries) > MAX_FILES:
        raise SandboxError(f"more than {MAX_FILES} files are committed")
    if not entries:
        return {}
    try:
        prepare(repo)                       # the repo's .git is canonical before git reads it
        batch = run_git(["cat-file", "--batch"], repo, run=run, text=False,
                        stdin="".join(f"{sha}\n" for _rel, sha in entries).encode("ascii"))
    except (GitError, OSError) as exc:
        raise SandboxError(f"git cat-file --batch could not be run ({exc})") from exc
    data, files, at, total = bytes(batch), {}, 0, 0
    for rel, sha in entries:
        nl = data.find(b"\n", at)
        header = data[at:nl].decode("ascii", "replace").split()
        if len(header) != 3 or header[0] != sha or header[1] != "blob":
            raise SandboxError(f"git cat-file did not return {rel[:80]!r}")
        size = int(header[2])
        if size > MAX_FILE_BYTES:
            raise SandboxError(f"{rel[:80]!r} is too big ({size:,} bytes)")
        total += size
        if total > MAX_TOTAL_BYTES:
            raise SandboxError("the committed tree is too big")
        files[rel] = data[nl + 1:nl + 1 + size]
        at = nl + 1 + size + 1
    return files


def diff(repo, base: str, rev: str, *, run=subprocess.run) -> str:
    """The patch from ``base`` to ``rev``, as git applies it (plumbing only)."""
    out = git(["diff-tree", "-p", "--no-renames", "--no-ext-diff", "--no-textconv", "--binary",
               "--full-index", "--no-color", base, rev], repo, run=run, text=False)
    return bytes(out).decode("utf-8", "replace")


def diff_stat(repo, base: str, rev: str, *, run=subprocess.run) -> str:
    out = git(["diff-tree", "-r", "--no-renames", "--stat=100", "--no-color", base, rev], repo,
              run=run)
    return str(out).strip()[:1500]


def write_tree(files: dict, dest: Path) -> None:
    for rel, data in files.items():
        path = Path(dest) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def run_suite(files: dict, *, setup, timeout: float, modules=(), spawner=None
              ) -> runner.SuiteResult:
    """``modules`` of the tree ``files`` (or, with none, its whole suite), contained: as the
    ``pionir-builds`` user, the sandbox's interpreter, a job object, a timeout that kills the
    whole tree. The folder is removed afterwards, whatever happened."""
    work = Path(setup.runs_dir) / f"improve-{secrets.token_hex(6)}"
    try:
        work.mkdir(parents=True)
        tree = work / "tree"
        write_tree(files, tree)
        out = work / "out" / "result.json"
        out.parent.mkdir(parents=True)
        system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
        env = bs.minimal_env(work=work / "env", path_dirs=[setup.python_dir, system32])
        limits = bs.JobLimits(active_processes=SUITE_PROCESSES, job_memory_mb=SUITE_MEMORY_MB,
                              cpu_seconds=max(60.0, 2 * timeout))
        try:
            setup.reap()                    # nothing of the sandbox user's runs before
            setup.preflight()               # proven Low, and blind to the owner's secrets
            done = bs.run(runner.argv(str(setup.python), out, modules), cwd=tree, env=env,
                          out_dir=work / "logs", timeout=timeout, limits=limits,
                          logon=setup.logon(), spawner=spawner,
                          sid=getattr(setup, "sid", None))
        except (bs.SandboxError, OSError) as exc:
            return runner.SuiteResult(False, tail=f"the tests could not start contained ({exc})")
        finally:
            try:
                setup.reap()                # ... and nothing it started survives it
            except (bs.SandboxError, OSError):
                pass
        return runner.read_result(out, returncode=done.returncode, timed_out=done.timed_out,
                                  tail=f"{done.stdout}\n{done.stderr}".strip()[-1500:])
    finally:
        shutil.rmtree(work, ignore_errors=True)
