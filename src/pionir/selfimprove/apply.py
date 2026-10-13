"""Applying an approved self-improvement to the owner's repo, and undoing one.

Run ONLY on Ian's yes (``improve.apply``, parked on every call) and by Ian himself
(``python -m pionir improve undo <id>``). Never a push, a restart, a force or a reset of his
work:

- **apply**: exactly the bytes the caller read and hashed once (written to a private temporary
  file). The guard runs first on the diff. The repo must be on ``main`` with no change to a
  tracked file, nothing of Ian's (an untracked or ignored file, a change) where the patch
  writes, and the candidate's base commit must be HEAD or an ancestor of it. The patch is checked
  against the INDEX (``git apply --check --cached``: the repo's line endings are left to git),
  applied there, the guard runs AGAIN on the trees in his index (asserts per test, imports, the
  catalogue), and only then is it committed as ONE commit whose message names the candidate and
  are the changed files written out from that commit. Main moved so that it no longer applies
  cleanly -> **stale** ("will re-propose"), nothing touched. Nothing of the patch is RUN here as
  Ian: its tests ran contained in the sandbox on exactly these bytes before he was asked, and a
  run as him could leave side effects no revert takes back. Undo is his one command away.
- **undo**: ``git revert`` of exactly the commit that applied it, only on a clean ``main`` that
  still contains it.

``git`` is injected, so tests never touch a real repo of Ian's.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from .guard import parse_patch, patch_problems, tree_problems

GIT_TIMEOUT = 120
TEST_TIMEOUT = 900.0
BRANCH = "main"

# (args, cwd) -> (returncode, stdout+stderr)
Git = Callable[[list, Path], tuple]
# (argv, cwd, timeout) -> (returncode, output, timed_out)
Run = Callable[[list, Path, float], tuple]


def _env() -> dict:
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
            "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def real_git(args: list, cwd: Path) -> tuple:
    try:
        done = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=GIT_TIMEOUT,
                              stdin=subprocess.DEVNULL, check=False, env=_env())
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, f"git could not run: {type(exc).__name__}: {exc}"
    return done.returncode, ((done.stdout or "") + (done.stderr or "")).strip()


def real_run(argv: list, cwd: Path, timeout: float) -> tuple:
    try:
        done = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout,
                              stdin=subprocess.DEVNULL, check=False, env=_env())
    except subprocess.TimeoutExpired:
        return None, "", True
    except OSError as exc:
        return 127, f"{type(exc).__name__}: {exc}", False
    return done.returncode, ((done.stdout or "") + (done.stderr or ""))[-4000:], False


def _clip(text, n: int = 300) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 3] + "..."


def repo_state(repo: Path, git: Git) -> str | None:
    """Why the repo cannot take a change now (not a repo, not on main, a tracked file
    changed), or None."""
    if not (Path(repo) / ".git").exists():
        return f"{repo} is not a git repository"
    rc, branch = git(["symbolic-ref", "--quiet", "--short", "HEAD"], repo)
    if rc != 0 or branch.strip() != BRANCH:
        return f"{repo} is not on {BRANCH} (it is on {branch.strip() or 'a detached HEAD'})"
    rc, dirty = git(["status", "--porcelain", "--untracked-files=no"], repo)
    if rc != 0:
        return f"git status failed in {repo}: {_clip(dirty, 160)}"
    if dirty.strip():
        return (f"{repo} has uncommitted changes to tracked files "
                f"({_clip(dirty.splitlines()[0], 80)}); nothing is applied over them")
    return None


def apply_patch(repo, patch: bytes, cid: str, base: str, *, title: str, tests: list,
                approval: str = "", known=frozenset(), protected: dict | None = None,
                files: dict | None = None, git: Git = real_git) -> dict:
    """``{"applied": True, "commit", ...}``, ``{"applied": False, "stale": True,
    "why"}`` or ``{"applied": False, "error"}``. ``patch`` is the exact bytes Ian approved (the
    caller read and hashed them once); they are written to a private temporary file and only
    that file is applied. See the module docstring."""
    repo = Path(repo)
    text = patch.decode("utf-8", "replace")
    problems = patch_problems(text, known_imports=known, protected=protected)
    if problems:
        return {"applied": False, "error": "the protected list refuses it: "
                                           + "; ".join(problems[:4])}
    why = repo_state(repo, git)
    if why:
        return {"applied": False, "stale": True, "why": why + "; will re-propose"}
    rc, _ = git(["cat-file", "-e", f"{base}^{{commit}}"], repo)
    if rc != 0:
        return {"applied": False, "stale": True,
                "why": f"its base commit {base[:10]} is not in {repo}; will re-propose"}
    rc, _ = git(["merge-base", "--is-ancestor", base, "HEAD"], repo)
    if rc != 0:
        return {"applied": False, "stale": True,
                "why": f"main no longer contains its base {base[:10]} (history moved); "
                       "will re-propose"}
    changes = parse_patch(text)
    paths = [f.path for f in changes]
    for f in changes:
        # nothing of Ian's in the way: no untracked or changed file where the patch writes,
        # and no file at all (ignored ones included) where it adds one
        if f.status == "added" and (repo / f.path).exists():
            return {"applied": False, "stale": True,
                    "why": f"{f.path} already exists in {repo} (untracked or ignored); "
                           "nothing is written over it; will re-propose"}
        rc, out = git(["status", "--porcelain", "--untracked-files=all", "--", f.path], repo)
        if rc != 0 or out.strip():
            return {"applied": False, "stale": True,
                    "why": f"{f.path} has changes of yours in {repo}; nothing is written over "
                           "them; will re-propose"}
    with tempfile.TemporaryDirectory(prefix="pionir-improve-") as tmp:
        patch_file = Path(tmp) / "patch.diff"
        patch_file.write_bytes(patch)
        rc, out = git(["apply", "--check", "--cached", str(patch_file)], repo)
        if rc != 0:
            return {"applied": False, "stale": True,
                    "why": f"main moved and the patch no longer applies cleanly "
                           f"({_clip(out, 200)}); stale, will re-propose"}
        rc, out = git(["apply", "--cached", str(patch_file)], repo)
    if rc != 0:
        git(["reset", "-q"], repo)
        return {"applied": False, "error": f"git apply failed: {_clip(out, 200)}"}
    # the guard once more, on the trees as they now are in Ian's own index
    before, after = {}, {}
    for path in paths:
        rc, out = git(["show", f"HEAD:{path}"], repo)
        if rc == 0:
            before[path] = out.encode("utf-8")
        rc, out = git(["show", f":{path}"], repo)
        if rc == 0:
            after[path] = out.encode("utf-8")
    problems = tree_problems(paths, before, after, known=known, protected=protected,
                             files=files if files is not None else {**before, **after})
    if problems:
        git(["reset", "-q"], repo)               # the index back to HEAD; nothing else touched
        return {"applied": False, "error": "the protected list refuses it: "
                                           + "; ".join(problems[:4])}
    message = (f"Self-improvement {cid}: {_clip(title, 100)}\n\n"
               f"Candidate {cid}, proposed by the crew's builds.improve worker (Daedalus "
               f"wrote it, Claude reviewed it) and approved by Ian"
               + (f" (Pionir task {approval})" if approval else "") + ".\n"
               f"Undo: python -m pionir improve undo {cid}\n")
    rc, out = git(["commit", "-q", "-m", message], repo)
    if rc != 0:
        git(["reset", "-q"], repo)              # the index back to HEAD; the work tree was never touched
        return {"applied": False, "error": f"git commit failed: {_clip(out, 200)}"}
    rc, commit = git(["rev-parse", "HEAD"], repo)
    commit = commit.strip()
    if paths:
        git(["checkout", "HEAD", "--", *paths], repo)
    # Nothing of the patch is RUN here, as Ian: its tests ran contained in the sandbox, on
    # exactly these bytes, before he was asked (a run here could leave side effects that no
    # revert takes back). The tests it carries are named for the record.
    return {"applied": True, "commit": commit, "paths": paths, "tests": list(tests)}


def undo(repo, commit: str, cid: str, *, git: Git = real_git) -> dict:
    """``git revert`` of the commit that applied ``cid``: ``{"undone": True,
    "revert_commit"}`` or ``{"undone": False, "why"}``."""
    repo = Path(repo)
    why = repo_state(repo, git)
    if why:
        return {"undone": False, "why": why}
    rc, _ = git(["merge-base", "--is-ancestor", commit, "HEAD"], repo)
    if rc != 0:
        return {"undone": False, "why": f"main does not contain {commit[:10]} (was it already "
                                        "undone, or is this another branch?)"}
    rc, out = git(["revert", "--no-edit", commit], repo)
    if rc != 0:
        git(["revert", "--abort"], repo)
        return {"undone": False, "why": f"git revert failed ({_clip(out, 200)}); nothing was "
                                        "changed"}
    _rc, rev = git(["rev-parse", "HEAD"], repo)
    return {"undone": True, "revert_commit": rev.strip(), "commit": commit, "id": cid}
