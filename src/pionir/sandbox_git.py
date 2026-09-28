"""Git on a sandbox repo, run so that nothing IN the repo can run code or redirect git.

A sandbox repo's ``.git`` folder is written by the build Daedalus (as ``pionir-builds``),
and model-written code runs there - so anything in it is untrusted. Git reads code-running
settings from a repo's own config (``core.fsmonitor``, ``core.hooksPath``, filters,
``core.sshCommand``, ``include``...) and runs hooks from ``.git/hooks``. So:

- every git call Pionir or the crew makes on a sandbox repo goes through ``run_git``:
  ``-c core.hooksPath=NUL -c core.fsmonitor=false``, no system or global config
  (``GIT_CONFIG_NOSYSTEM``, ``GIT_CONFIG_GLOBAL=NUL``) and a minimal environment;
- before every build job, ``sanitize`` REWRITES ``.git/config`` to ``CANONICAL`` and empties
  ``.git/hooks`` - it does not scan them for danger, it replaces them - and then proves the
  result with ``git config --show-origin --list``: exactly the canonical entries from the
  repo's file, nothing else.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

GIT_TIMEOUT = 60
SAFE_ARGS = ("-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false")
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


class GitError(RuntimeError):
    pass


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
            "GIT_ATTR_NOSYSTEM": "1", "HOME": tmp}


def run_git(args, cwd, *, run=subprocess.run, timeout: float = GIT_TIMEOUT,
            text: bool = True):
    """One git command, the safe way; its stdout, or GitError. Never a shell."""
    try:
        done = run(["git", *SAFE_ARGS, *args], cwd=str(cwd), capture_output=True,
                   timeout=timeout, env=git_env(), check=False,
                   **({"text": True, "encoding": "utf-8", "errors": "replace"} if text else {}))
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitError(f"git {args[0]} could not run ({type(exc).__name__})") from exc
    if done.returncode != 0:
        err = done.stderr if isinstance(done.stderr, str) else (done.stderr or b"").decode(
            "utf-8", "replace")
        raise GitError(f"git {args[0]} failed: {' '.join(err.split())[:200]}")
    return done.stdout


def sanitize(repo, *, run=subprocess.run) -> None:
    """Replace the repo's config with CANONICAL and empty its hooks, then prove it."""
    git_dir = Path(repo) / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        raise GitError(f"{repo} has no .git folder of its own")
    for name in ("config.worktree", "commondir", "gitdir"):
        target = git_dir / name
        if target.exists() or target.is_symlink():
            target.unlink()
    config = git_dir / "config"
    if config.is_symlink():
        config.unlink()
    config.write_text(CANONICAL, encoding="utf-8", newline="\n")
    hooks = git_dir / "hooks"
    if hooks.is_symlink() or hooks.is_file():
        hooks.unlink()
    elif hooks.is_dir():
        shutil.rmtree(hooks)
    hooks.mkdir()
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
