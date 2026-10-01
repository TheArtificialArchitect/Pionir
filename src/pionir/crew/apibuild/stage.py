"""Staging: a checked build becomes one local commit on ``api/<id>`` in a worktree of Scrooge.

Nothing here pushes, nothing here touches ``main`` or the repo's own working tree: the branch
is created in a NEW worktree under the builder's folder (``git worktree add -b``), the files
are written there, the registry and smoke script get the ONE line each that a product needs
(generated here from the id and the checked smoke lines, never taken from the model), and the
result is committed locally with hooks off. A failure after the worktree exists removes the
worktree and the branch this call made, so a refused build leaves nothing behind.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..log import log
from .checks import expected_files

BASE_BRANCH = "main"
PRODUCT_DIR = "worker/src/products"
TEST_DIR = "worker/test"
REGISTRY = "worker/src/products/index.ts"
SMOKE = "tools/smoke.ps1"
SMOKE_MARKER = re.compile(r"^\$dash = try\b", re.M)
_REGISTRY_LINE = re.compile(r"^export const products: Product\[\] = \[([^\]\n]*)\];[ \t]*(?=\r?$)", re.M)
GIT_TIMEOUT = 120

_IDENT = ("-c", "user.name=Pionir API builder", "-c", "user.email=pionir@localhost",
          "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false")


class StageError(Exception):
    """The build could not be staged; the message says why, and nothing was left behind."""


def branch_for(pid: str) -> str:
    return f"api/{pid}"


def git(repo, *args, timeout: int = GIT_TIMEOUT) -> tuple:
    """``(returncode, stdout, stderr)`` of one git call in ``repo``. Never prompts, never a
    shell; a git that is missing or hangs is a non-zero return, not an exception."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
    try:
        done = subprocess.run(["git", *_IDENT, "-C", str(repo), *args], capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=timeout,
                              env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", f"git could not run: {exc}"
    return done.returncode, done.stdout.strip(), done.stderr.strip()


def _read(path: Path) -> str:
    """The file exactly as committed: no newline translation, so the diff is only the new lines."""
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def registry_edit(text: str, pid: str) -> str:
    """The registry with ``pid`` imported and appended to ``products``; StageError when the
    file is not shaped the way this knows (it never guesses)."""
    nl = _newline(text)
    m = _REGISTRY_LINE.search(text)
    if not m:
        raise StageError("the registry (src/products/index.ts) is not shaped the way the "
                         "builder knows: one `export const products: Product[] = [...];` line")
    names = [n.strip() for n in m.group(1).split(",") if n.strip()]
    if pid in names or re.search(rf"^import\s+\{{\s*{pid}\s*\}}", text, re.M):
        raise StageError(f"the registry already has a product named {pid}")
    imports = list(re.finditer(r"^import .*;[ \t]*(?=\r?$)", text, re.M))
    if not imports:
        raise StageError("the registry has no import lines to follow")
    last = imports[-1]
    line = f"import {{ {pid} }} from './{pid}';"
    text = text[:last.end()] + nl + line + text[last.end():]
    m = _REGISTRY_LINE.search(text)
    names.append(pid)
    return text[:m.start()] + f"export const products: Product[] = [{', '.join(names)}];" \
        + text[m.end():]


def smoke_edit(text: str, lines: str) -> str:
    nl = _newline(text)
    m = SMOKE_MARKER.search(text)
    if not m:
        raise StageError("tools/smoke.ps1 has no `$dash = try` line to insert before")
    block = nl.join(ln.rstrip() for ln in lines.splitlines() if ln.strip()) + nl + nl
    return text[:m.start()] + block + text[m.start():]


@dataclass(frozen=True)
class Staged:
    branch: str
    worktree: Path
    commit: str
    stat: str
    files: tuple


def _cleanup(repo, worktree: Path, branch: str) -> None:
    git(repo, "worktree", "remove", "--force", str(worktree))
    if worktree.exists():
        shutil.rmtree(worktree, ignore_errors=True)
    git(repo, "worktree", "prune")
    git(repo, "branch", "-D", branch)


def stage(repo, worktrees_dir, entry: dict, files: dict, *, base: str = BASE_BRANCH) -> Staged:
    """Commit the checked build for ``entry`` on a new branch ``api/<id>``; see the module."""
    pid = entry["id"]
    repo, worktrees_dir = Path(repo), Path(worktrees_dir)
    branch = branch_for(pid)
    if not (repo / ".git").exists():
        raise StageError(f"{repo} is not a git repository (set PIONIR_CREW_SCROOGE_REPO)")
    if branch == base or branch.endswith("/main"):
        raise StageError("refusing to stage on the main branch")
    rc, _, err = git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{base}")
    if rc:
        raise StageError(f"the repository has no {base} branch to start from")
    rc, _, _ = git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
    if rc == 0:
        raise StageError(f"the branch {branch} already exists; it is not overwritten")
    worktree = worktrees_dir / pid
    if worktree.exists():
        raise StageError(f"{worktree} already exists; it is not overwritten")
    worktrees_dir.mkdir(parents=True, exist_ok=True)
    rc, _, err = git(repo, "worktree", "add", "-b", branch, str(worktree), base)
    if rc:
        raise StageError(f"git could not make the worktree: {err[:200]}")
    try:
        rc, head, _ = git(worktree, "rev-parse", "--abbrev-ref", "HEAD")
        if rc or head != branch:
            raise StageError(f"the worktree is on {head!r}, not {branch}")
        product, test, smoke = expected_files(pid)
        targets = {f"{PRODUCT_DIR}/{pid}.ts": files[product], f"{TEST_DIR}/{pid}.test.ts": files[test]}
        for rel in targets:
            if (worktree / rel).exists():
                raise StageError(f"{rel} already exists on {base}")
        registry = worktree / REGISTRY
        smoke_path = worktree / SMOKE
        for p in (registry, smoke_path):
            if not p.is_file():
                raise StageError(f"{p.relative_to(worktree).as_posix()} is missing from the repo")
        targets[REGISTRY] = registry_edit(_read(registry), pid)
        targets[SMOKE] = smoke_edit(_read(smoke_path), files[smoke])
        for rel, text in targets.items():
            path = worktree / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
        rc, _, err = git(worktree, "add", "--", *sorted(targets))
        if rc:
            raise StageError(f"git add failed: {err[:200]}")
        message = f"Add the {pid} API product ({entry['name']})\n\nStaged by Pionir's API builder; not deployed."
        rc, _, err = git(worktree, "commit", "--no-verify", "-q", "-m", message)
        if rc:
            raise StageError(f"git commit failed: {err[:200]}")
        rc, commit, _ = git(worktree, "rev-parse", "HEAD")
        _, stat, _ = git(worktree, "diff", "--stat", f"{base}..HEAD")
        if rc or not commit:
            raise StageError("could not read the commit that was made")
    except BaseException:
        log.warning("api builder: staging %s failed; removing the worktree and branch", pid)
        _cleanup(repo, worktree, branch)
        raise
    return Staged(branch, worktree, commit, stat[-1500:], tuple(sorted(targets)))
