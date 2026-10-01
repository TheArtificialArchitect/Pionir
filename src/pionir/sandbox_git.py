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

import os
import shutil
import stat
import subprocess
from pathlib import Path

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
