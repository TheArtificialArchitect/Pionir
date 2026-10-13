"""A candidate's sandbox: a fresh clone of Pionir's ``main`` for Daedalus, the one read of what
Daedalus wrote, the TRUSTED trees everything is judged on, and the contained test runs.

**The clone** (``clone``) is made directly inside the sandbox workspace as
``<sandbox>\\pionir-<id>`` exactly the way the night builds make a product's repo: refused if
the folder exists or the workspace is a link, ``git clone --no-hardlinks --single-branch`` of
the owner's ``main``, its remote removed, ``.git/config`` rewritten to canonical content
(pionir/sandbox_git.py), marked, and proved with the very rule Pionir's build capability
enforces (``sandbox_repo_problem``). The owner's repo is only read.

**What Daedalus wrote is read ONCE** (``diff``: plumbing, ``diff-tree -p``, after ``prepare``)
and the worker FREEZES those bytes in the owner-only improve folder at once. The sandbox repo is
never read again: code under test runs as the sandbox user, which can write that repo's objects.

**The trusted trees** (``trusted_trees``): the base is read from a private clone of the OWNER's
repo in the owner's own temporary folder (which the sandbox user cannot reach), and the change is
that base with the frozen patch applied (``read-tree`` + ``apply --cached`` + ``write-tree`` in
that clone). The guard, the tests, Claude and Ian all see exactly the frozen bytes on exactly the
owner's base.

**The test runs** (``run_suite``) are contained like the night builds' own: as the
``pionir-builds`` user, the sandbox's interpreter, a kill-on-close job object, output to files,
the whole tree killed at the timeout - and only while this worker holds the sandbox's one lease
(``build_sandbox.SandboxLease``): someone else using the sandbox is ``busy`` (wait), never a
reap under them. Python only - node is never needed here.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path

from pionir import build_sandbox as bs
from pionir.sandbox_git import (
    SANDBOX_MARKER,
    GitError,
    is_reparse,
    prepare,
    run_git,
    sandbox_repo_problem,
    sanitize,
)
from pionir.selfimprove import runner

MAX_FILES = 4000
MAX_FILE_BYTES = 5_000_000
MAX_TOTAL_BYTES = 80_000_000
CLONE_TIMEOUT = 300
SUITE_PROCESSES = 16
SUITE_MEMORY_MB = 4096
_SAFE_PART = re.compile(r"[A-Za-z0-9_.@+-][A-Za-z0-9_.@+ -]{0,120}")
_SHA = re.compile(r"[0-9a-f]{40,64}")


class SandboxError(RuntimeError):
    pass


def git(args: list, cwd, *, run=subprocess.run, text: bool = True, stdin: bytes | None = None,
        timeout: float = 120, sandboxed: bool = True):
    """One git command the safe way (pionir/sandbox_git.py); in a sandbox repo only after its
    ``.git`` has been rewritten to canonical content. SandboxError on any failure."""
    try:
        if sandboxed:
            prepare(cwd)
        return run_git(args, cwd, run=run, timeout=timeout, text=text, stdin=stdin)
    except (GitError, OSError) as exc:
        raise SandboxError(str(exc)) from exc


def slug_for(cid: str) -> str:
    """The sandbox folder for a candidate: ``pionir-imp-20261012-ab12`` (a sandbox slug)."""
    return f"pionir-{cid}"


def head(repo, *, run=subprocess.run) -> str:
    out = str(git(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], repo, run=run)).strip()
    if not _SHA.fullmatch(out):
        raise SandboxError("HEAD is not a commit")
    return out


def built_commit(repo, base: str, commit: str, *, run=subprocess.run) -> str:
    """The full id of a commit Daedalus reports on a branch of its own - only if it exists and
    descends from the base. Never checked out or merged."""
    if not re.fullmatch(r"[0-9a-f]{7,64}", commit or ""):
        raise SandboxError("not a commit id")
    full = str(git(["rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}"], repo,
                   run=run)).strip()
    if not _SHA.fullmatch(full):
        raise SandboxError(f"{commit} is not a commit in the sandbox")
    git(["merge-base", "--is-ancestor", base, full], repo, run=run)
    return full


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


def _path_problem(rel: str) -> str | None:
    parts = rel.split("/")
    if rel.startswith("/") or "\\" in rel or ":" in rel or any(
            p in ("", ".", "..") or not _SAFE_PART.fullmatch(p) for p in parts):
        return f"{rel[:80]!r} is not a plain relative path"
    return None


def export(repo, rev: str, *, run=subprocess.run, sandboxed: bool = True) -> dict:
    """Every file committed at ``rev`` (path -> bytes), read from its objects with one
    ``cat-file --batch``; a link, a submodule, an odd path or an oversized tree is refused."""
    raw = git(["ls-tree", "-r", "-z", "--full-tree", rev], repo, run=run, text=False,
              sandboxed=sandboxed)
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
    batch = git(["cat-file", "--batch"], repo, run=run, text=False, sandboxed=sandboxed,
                stdin="".join(f"{sha}\n" for _rel, sha in entries).encode("ascii"))
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
    """The patch from ``base`` to ``rev`` in the sandbox repo, as git applies it - read ONCE,
    then frozen by the worker."""
    out = git(["diff-tree", "-p", "--no-renames", "--no-ext-diff", "--no-textconv", "--binary",
               "--full-index", "--no-color", base, rev], repo, run=run, text=False)
    return bytes(out).decode("utf-8", "replace")


def trusted_trees(owner_repo, base: str, patch: bytes, *, run=subprocess.run) -> tuple:
    """``(base files, changed files, diff stat)`` built from the OWNER's repo at ``base`` and
    the frozen ``patch`` - in a private clone in the owner's temporary folder, never the
    sandbox. SandboxError when main no longer holds the base or the patch does not apply."""
    with tempfile.TemporaryDirectory(prefix="pionir-improve-") as tmp:
        where = Path(tmp)
        try:
            run_git(["clone", "-q", "--no-hardlinks", "--no-checkout", "--no-tags",
                     "--single-branch", "--branch", "main", str(Path(owner_repo).resolve()),
                     "c"], where, run=run, timeout=CLONE_TIMEOUT)
        except (GitError, OSError) as exc:
            raise SandboxError(f"the owner's repo could not be read ({exc})") from exc
        c = where / "c"
        (where / "patch.diff").write_bytes(patch)
        base_files = export(c, base, run=run, sandboxed=False)
        git(["read-tree", base], c, run=run, sandboxed=False)
        try:
            git(["apply", "--cached", str(where / "patch.diff")], c, run=run, sandboxed=False)
        except SandboxError as exc:
            raise SandboxError(f"the frozen patch does not apply to main's {base[:10]} "
                               f"({exc})") from exc
        tree = str(git(["write-tree"], c, run=run, sandboxed=False)).strip()
        head_files = export(c, tree, run=run, sandboxed=False)
        stat = str(git(["diff-tree", "-r", "--stat=100", "--no-color", base, tree], c, run=run,
                       sandboxed=False)).strip()[:1500]
    return base_files, head_files, stat


def last_commit(repo, paths: list, *, run=subprocess.run) -> tuple | None:
    """(commit time, short id) of the newest commit on the owner's ``main`` that touched any of
    ``paths`` - read only - or None."""
    try:
        out = str(run_git(["log", "-1", "--format=%ct %h", "main", "--", *paths], repo,
                          run=run)).strip()
        ct, sha = out.split()
        return int(ct), sha
    except (GitError, OSError, ValueError):
        return None


def write_tree(files: dict, dest: Path) -> None:
    for rel, data in files.items():
        path = Path(dest) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def run_suite(files: dict, *, setup, timeout: float, modules=(), spawner=None
              ) -> runner.SuiteResult:
    """``modules`` of the tree ``files`` (or, with none, its whole suite), contained - only
    while holding the sandbox's lease (``busy`` when someone else has it: nothing reaped, nothing
    run). The folder is removed afterwards, whatever happened."""
    lease = bs.SandboxLease(setup, "the self-improvement worker's test run")
    try:
        lease.acquire()
    except (bs.SandboxBusy, OSError) as exc:
        return runner.SuiteResult(False, tail=str(exc), busy=True)
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
        lease.release()
