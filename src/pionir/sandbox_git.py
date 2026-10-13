"""Git on a sandbox repo, run so that nothing IN the repo can run code or redirect git.

A sandbox repo's ``.git`` folder is written by the build Daedalus (as ``pionir-builds``),
and model-written code runs there - so anything in it is untrusted. Git reads code-running
settings from a repo's own config (``core.fsmonitor``, ``core.hooksPath``, filters and
their smudge/clean commands, ``core.sshCommand``, ``include``...), runs hooks from
``.git/hooks``, reads attributes that name filters, and follows ``objects/info/alternates``
into OTHER repos' objects. So:

- the owner's side NEVER checks out, merges or resets a build repo: it only READS objects
  (``rev-parse``, ``ls-tree``, ``cat-file``, ``diff-tree --name-status``) - none of which runs a
  filter, a hook or a diff driver;
- before EVERY owner-side git call, ``prepare`` rewrites ``.git/config`` to ``CANONICAL``,
  empties ``.git/hooks``, removes ``info/attributes``, ``objects/info/alternates`` and any
  ``config.worktree``/``commondir``/``gitdir``, and refuses a ``.git`` (or its ``objects``,
  ``refs``, ``info``) that is a link, junction or other reparse point;
- every call adds ``-c core.hooksPath=NUL -c core.fsmonitor=false -c core.attributesFile=NUL``,
  with no system or global config (``GIT_CONFIG_NOSYSTEM``, ``GIT_CONFIG_GLOBAL=NUL``) and a
  minimal environment;
- before a build job, ``sanitize`` does ``prepare`` and then proves the result with
  ``git config --show-origin --list``: exactly the canonical entries from the repo's file.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

GIT_TIMEOUT = 60
SAFE_ARGS = ("-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false",
             "-c", f"core.attributesFile={os.devnull}")
CANONICAL_ENTRIES = (
    ("core.repositoryformatversion", "0"),
    ("core.filemode", "false"),
    ("core.bare", "false"),
    ("core.logallrefupdates", "true"),
    ("core.symlinks", "false"),
    ("core.ignorecase", "true"),
    ("core.hookspath", os.devnull),
    ("core.fsmonitor", "false"),
)
CANONICAL = "[core]\n" + "".join(
    f"\t{key.split('.', 1)[1]} = {value}\n" for key, value in CANONICAL_ENTRIES)
_REMOVED = ("config.worktree", "commondir", "gitdir", "info/attributes",
            "objects/info/alternates", "objects/info/http-alternates", "info/grafts")


class GitError(RuntimeError):
    pass


# The Builds division's sandbox workspace and the rule for a repo the crew may have Daedalus
# build in (moved here from adapters/daedalus.py, which re-exports them).
DEFAULT_SANDBOX_ROOT = r"C:\src\daedalus-work"
# Written by the Builds worker into a sandbox repo's .git folder when it creates it; a repo
# without it was not made by the worker and is never built in.
SANDBOX_MARKER = "pionir-sandbox.json"
SANDBOX_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{2,39}")
_REMOTE = re.compile(r"(?im)^\s*\[\s*remote\b")
_WORKTREE_KEY = re.compile(r"(?im)^\s*worktree\s*=")


def is_reparse(path) -> bool:
    """A link, a junction, or any other reparse point - never followed on our side."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if stat.S_ISLNK(st.st_mode):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def git_env() -> dict:
    """Enough for git to run, and nothing that points it at any config or credential."""
    system = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT") or r"C:\Windows"
    git = shutil.which("git")
    path = [str(Path(git).parent)] if git else []
    path += [str(Path(system) / "System32")]
    tmp = os.environ.get("TEMP") or os.environ.get("TMP") or str(Path(system) / "Temp")
    return {"PATH": os.pathsep.join(path), "SystemRoot": system, "SYSTEMROOT": system,
            "TEMP": tmp, "TMP": tmp, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": "",
            "GIT_ATTR_NOSYSTEM": "1", "GIT_NO_REPLACE_OBJECTS": "1", "HOME": tmp}


def run_git(args, cwd, *, run=subprocess.run, timeout: float = GIT_TIMEOUT,
            text: bool = True, stdin: bytes | None = None):
    """One git command, the safe way; its stdout, or GitError. Never a shell."""
    kwargs = {"text": True, "encoding": "utf-8", "errors": "replace"} if text else {}
    if stdin is not None:
        kwargs["input"] = stdin
    try:
        done = run(["git", *SAFE_ARGS, *args], cwd=str(cwd), capture_output=True,
                   timeout=timeout, env=git_env(), check=False, **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitError(f"git {args[0]} could not run ({type(exc).__name__})") from exc
    if done.returncode != 0:
        err = done.stderr if isinstance(done.stderr, str) else (done.stderr or b"").decode(
            "utf-8", "replace")
        raise GitError(f"git {args[0]} failed: {' '.join(err.split())[:200]}")
    return done.stdout


def prepare(repo) -> None:
    """Rewrite what git would read from the repo into safe, canonical content - before
    EVERY owner-side call. Refuses (GitError) a ``.git`` that is a reparse point."""
    repo = Path(repo)
    git_dir = repo / ".git"
    if is_reparse(repo) or is_reparse(git_dir) or not git_dir.is_dir():
        raise GitError(f"{repo} has no .git folder of its own (or it is a link or junction)")
    for name in ("info", "objects", "objects/info", "refs"):
        if is_reparse(git_dir / name):
            raise GitError(f"{git_dir / name} is a link or junction")
    for name in _REMOVED:
        target = git_dir / name
        if is_reparse(target) or target.is_file():
            target.unlink()
        elif target.is_dir():
            raise GitError(f"{target} is a folder where git expects a file")
    config = git_dir / "config"
    if is_reparse(config):
        config.unlink()
    config.write_text(CANONICAL, encoding="utf-8", newline="\n")
    hooks = git_dir / "hooks"
    if is_reparse(hooks) or hooks.is_file():
        hooks.unlink()
    elif hooks.is_dir():
        shutil.rmtree(hooks)
    hooks.mkdir()


def sanitize(repo, *, run=subprocess.run) -> None:
    """``prepare``, then prove it."""
    prepare(repo)
    verify(repo, run=run)


def verify(repo, *, run=subprocess.run) -> None:
    """``git config --show-origin --list``: every entry from the command line (ours) or from
    the repo's own config file, and the repo's entries exactly CANONICAL."""
    out = run_git(["config", "--show-origin", "--list"], repo, run=run)
    local = []
    for line in str(out).splitlines():
        if not line.strip():
            continue
        origin, _, entry = line.partition("\t")
        if origin == "command line:":
            continue
        if origin.replace("\\", "/") != "file:.git/config":
            raise GitError(f"git reads config from {origin!r}; only the repo's own is allowed")
        key, _, value = entry.partition("=")
        local.append((key.lower(), value))
    want = [(k, v) for k, v in CANONICAL_ENTRIES]
    if sorted(local) != sorted(want):
        raise GitError("the repo's git config is not the canonical one after rewriting it")


def sandbox_repo_problem(repo: Any, root: str | os.PathLike) -> str | None:
    """Why ``repo`` is not a sandbox repo the Builds worker may build in, or None.

    A sandbox repo is EXACTLY ``<root>\\<slug>``: an absolute path with no ``.`` or ``..``
    part, not a UNC or device path, whose parent is the sandbox root (compared
    case-insensitively on Windows, so another drive or another folder is refused), whose
    name is a slug, which is neither a link nor a junction and resolves to itself (so
    nothing inside the workspace can point outside it), and which holds a real ``.git``
    folder (not a ``.git`` file: that is a linked worktree of some other repo) carrying the
    worker's marker for this slug, with no remote and no ``core.worktree`` redirect - a
    clone of a live repo, whatever its folder is called, is refused. The root itself must
    exist and must not be a link. Fail closed: anything that cannot be checked is a
    reason."""

    if not isinstance(repo, str) or not repo.strip():
        return ("no repo was given - Daedalus would fall back to its own repository, "
                "which is never built in")
    raw = repo.strip()
    if raw.startswith(("\\\\", "//")):
        return f"{raw[:80]!r} is a UNC or device path, not a sandbox repo"
    if any(ch in raw for ch in "~%$\x00"):
        return f"{raw[:80]!r} has a character that could expand to another path"
    if any(part in (".", "..") for part in re.split(r"[\\/]+", raw)):
        return f"{raw[:80]!r} has a '.' or '..' part"
    if not os.path.isabs(raw) or not re.match(r"^[A-Za-z]:[\\/]|^/", raw):
        return f"{raw[:80]!r} is not an absolute path"
    root_abs = os.path.abspath(os.fspath(root))
    try:
        root_real = os.path.realpath(root_abs, strict=True)
    except OSError:
        return f"the sandbox workspace {root_abs} does not exist"
    if is_reparse(root_abs) or os.path.normcase(root_real) != os.path.normcase(root_abs):
        return f"the sandbox workspace {root_abs} is itself a link or junction"
    repo_abs = os.path.abspath(raw)
    parent, name = os.path.split(repo_abs)
    if os.path.normcase(parent) != os.path.normcase(root_abs):
        return (f"{repo_abs[:120]} is not directly inside the sandbox workspace "
                f"{root_abs}")
    if not SANDBOX_SLUG.fullmatch(name):
        return f"{name[:60]!r} is not a sandbox repo's name (a slug: a-z, 0-9 and -)"
    path = Path(repo_abs)
    if is_reparse(path):
        return f"{repo_abs[:120]} is a link or junction"
    try:
        real = os.path.realpath(repo_abs, strict=True)
    except OSError:
        return f"{repo_abs[:120]} does not exist"
    if os.path.normcase(real) != os.path.normcase(repo_abs):
        return f"{repo_abs[:120]} resolves somewhere else"
    if not path.is_dir():
        return f"{repo_abs[:120]} is not a folder"
    git = path / ".git"
    if is_reparse(git) or not git.is_dir():
        return (f"{repo_abs[:120]} has no .git folder of its own (a linked worktree or "
                "not a repository)")
    if (git / "commondir").exists():
        return f"{repo_abs[:120]} is a linked worktree of another repository"
    try:
        marker = json.loads((git / SANDBOX_MARKER).read_text(encoding="utf-8"))
        config = (git / "config").read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return (f"{repo_abs[:120]} is not a sandbox repo (no readable marker: only a repo "
                "the Builds worker created is built in)")
    if not isinstance(marker, dict) or marker.get("slug") != name:
        return f"{repo_abs[:120]}'s sandbox marker does not name this repo"
    if _REMOTE.search(config):
        return f"{repo_abs[:120]} has a remote: it is a clone, not a sandbox"
    if _WORKTREE_KEY.search(config):
        return f"{repo_abs[:120]} redirects its work tree elsewhere (core.worktree)"
    return None
