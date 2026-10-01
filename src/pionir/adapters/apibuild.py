"""``apibuild.verify``: run the checks on a staged API product, only on the owner's yes.

The API builder (crew/apibuild) has Claude write a product for the Dokaz API (the Scrooge
Worker) and stages it as one local commit on a branch ``api/<id>`` in a worktree of the Scrooge
repository. That code was written by a model, so it is not run as the owner before he has seen
what it is: this capability is PRIVILEGED with ``requires_approval=True``, parked on EVERY call,
and its card shows the branch, the commit and the diff stat. Only his yes runs ``npx tsc
--noEmit`` and ``npx vitest run`` in that worktree. ``routable=False``: by name only.

The payload is ``{"id", "commit"}`` and nothing else: the worktree is resolved here, under THIS
process's own settings, and must be on the branch ``api/<id>`` with HEAD exactly at the commit
the card showed. A caller cannot name a path, a command or a different commit; a worktree that
moved since the card (a new commit, another branch) is refused, not run.

Nothing here deploys, writes D1, pushes, or publishes: it answers green or red, row by row,
with the tail of each tool's output, and tells the owner what to run next
(``tools\\deploy.ps1``, his own step). A red check is an ANSWER (``ok: true, green: false``),
not a fault, so it never counts against the circuit breaker; only "could not run" is a failure.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pionir.adapters._proc import child_env
from pionir.contracts import AgentManifest, Capability, RiskLevel, Task, TaskResult
from pionir.errors import AdapterProtocolError, AdapterUnavailable

_log = logging.getLogger(__name__)

VERIFY = "apibuild.verify"
BASE_BRANCH = "main"
WORKER_DIR = "worker"
_ID = re.compile(r"[a-z]{3,20}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
PAYLOAD_KEYS = frozenset({"id", "commit"})
TAIL_CHARS = 1500
GIT_TIMEOUT = 60
INSTALL_TIMEOUT = 600
TSC_TIMEOUT = 300
VITEST_TIMEOUT = 600

# (argv, cwd, timeout_seconds) -> (returncode, combined output); raises OSError / TimeoutError
Runner = Callable[[list, Path, float], tuple]


@dataclass(frozen=True, slots=True)
class ApiBuildSettings:
    """The Scrooge repository and the folder the builder keeps its worktrees in."""

    scrooge_repo: Path = Path("C:/src/Scrooge")
    apibuilds_dir: Path = Path("~/.pionir/apibuilds")

    def __post_init__(self) -> None:
        object.__setattr__(self, "scrooge_repo", Path(self.scrooge_repo).expanduser())
        object.__setattr__(self, "apibuilds_dir", Path(self.apibuilds_dir).expanduser())

    @property
    def worktrees(self) -> Path:
        return self.apibuilds_dir / "worktrees"


def apibuild_settings(configured: Any) -> ApiBuildSettings:
    return ApiBuildSettings(scrooge_repo=configured.scrooge_path,
                            apibuilds_dir=configured.apibuilds_path)


def run_command(argv: list, cwd: Path, timeout: float) -> tuple:
    """The real runner: no shell, UTF-8, stdin closed, output merged and returned."""
    exe = shutil.which(argv[0])
    if exe is None:
        raise OSError(f"{argv[0]} is not installed or not on PATH")
    done = subprocess.run([exe, *argv[1:]], cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, check=False,
                          stdin=subprocess.DEVNULL, env=child_env())
    return done.returncode, ((done.stdout or "") + (done.stderr or ""))


def _git(worktree: Path, *args: str) -> tuple:
    try:
        done = subprocess.run(["git", "-C", str(worktree), *args], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=GIT_TIMEOUT, stdin=subprocess.DEVNULL, check=False,
                              env={**child_env(), "GIT_TERMINAL_PROMPT": "0",
                                   "GIT_OPTIONAL_LOCKS": "0"})
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, f"git could not run: {exc}"
    return done.returncode, (done.stdout or "").strip()


def _tail(text: str, limit: int = TAIL_CHARS) -> str:
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text or "").strip()
    return text if len(text) <= limit else "..." + text[-limit:]


class ApiBuildAdapter:
    """The staged API product's checks, as a gated, audited Pionir capability."""

    def __init__(self, settings: ApiBuildSettings | None = None, *,
                 runner: Runner | None = None) -> None:
        self.settings = settings or ApiBuildSettings()
        self._run = runner or run_command
        self._manifest = AgentManifest(
            agent_id="apibuild",
            version="pionir/apibuild",
            capabilities=(
                Capability(
                    name=VERIFY,
                    description="Run tsc and the test suite on an API product the builder "
                                "staged on a local branch of the Scrooge repo (only after "
                                "the owner approves it); never deploys",
                    risk=RiskLevel.PRIVILEGED,
                    required_permissions=frozenset({VERIFY}),
                    requires_approval=True,
                    routable=False,
                ),
            ),
        )

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def status(self) -> Mapping[str, Any]:
        """Local only: the repo is a git repository. Nothing is run."""
        repo = self.settings.scrooge_repo
        if not (repo / ".git").exists():
            raise AdapterUnavailable(f"not configured: no Scrooge repository at {repo} "
                                     "(PIONIR_SCROOGE_REPO)")
        return {"scrooge_repo": str(repo), "worktrees": str(self.settings.worktrees)}

    # ---- the request ------------------------------------------------------------------
    @staticmethod
    def _payload(task: Task) -> tuple:
        if task.capability != VERIFY:
            raise AdapterProtocolError(f"apibuild has no capability {task.capability!r}")
        payload = task.payload
        if not isinstance(payload, Mapping) or set(payload) != PAYLOAD_KEYS:
            raise AdapterProtocolError(f"{VERIFY} refused by Pionir - the payload is exactly "
                                       "{id, commit}")
        pid, commit = payload.get("id"), payload.get("commit")
        if not isinstance(pid, str) or not _ID.fullmatch(pid):
            raise AdapterProtocolError(f"{VERIFY} refused by Pionir - id is 3 to 20 lowercase "
                                       "letters")
        if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
            raise AdapterProtocolError(f"{VERIFY} refused by Pionir - commit is a full "
                                       "40-character hash")
        return pid, commit

    def _worktree(self, pid: str, commit: str) -> Path:
        """The worktree, confirmed to be on ``api/<id>`` with HEAD at ``commit`` and clean."""
        worktree = self.settings.worktrees / pid
        if not (worktree / ".git").exists():
            raise AdapterProtocolError(f"{VERIFY} refused by Pionir - there is no staged "
                                       f"worktree for {pid}")
        rc, branch = _git(worktree, "rev-parse", "--abbrev-ref", "HEAD")
        if rc or branch != f"api/{pid}":
            raise AdapterProtocolError(f"{VERIFY} refused by Pionir - the worktree is on "
                                       f"{branch!r}, not api/{pid}")
        rc, head = _git(worktree, "rev-parse", "HEAD")
        if rc or head != commit:
            raise AdapterProtocolError(f"{VERIFY} refused by Pionir - the branch moved: HEAD "
                                       f"is not the commit the card showed ({commit[:10]})")
        rc, dirty = _git(worktree, "status", "--porcelain")
        if rc or dirty:
            raise AdapterProtocolError(f"{VERIFY} refused by Pionir - the worktree has "
                                       "changes that are not in the commit")
        return worktree

    def validate(self, task: Task) -> None:
        pid, commit = self._payload(task)
        self._worktree(pid, commit)

    def park_context(self, task: Task) -> dict[str, Any] | None:
        """What the card shows: the branch, the commit, the files and the diff stat, read
        from the worktree as it is parked. Never blocks parking."""
        if task.capability != VERIFY:
            return None
        pid, commit = self._payload(task)
        worktree = self.settings.worktrees / pid
        _rc, stat = _git(worktree, "diff", "--stat", f"{BASE_BRANCH}..{commit}")
        _rc, names = _git(worktree, "diff", "--name-only", f"{BASE_BRANCH}..{commit}")
        _rc, subject = _git(worktree, "log", "-1", "--format=%s", commit)
        return {"branch": f"api/{pid}", "commit": commit, "worktree": str(worktree),
                "subject": subject[:160], "files": names.splitlines()[:20],
                "diff_stat": _tail(stat, 1200),
                "checks": "the builder's static checks passed (allowed files and imports "
                          "only, no network, no eval, no secrets, tests and smoke lines "
                          "cover every endpoint)",
                "runs": "npm ci (if node_modules is missing), npx tsc --noEmit, "
                        "npx vitest run - in that worktree; nothing is deployed"}

    # ---- the call ---------------------------------------------------------------------
    def execute(self, task: Task) -> TaskResult:
        pid, commit = self._payload(task)
        worktree = self._worktree(pid, commit)
        cwd = worktree / WORKER_DIR
        rows: list = []
        if not (cwd / "node_modules").is_dir():
            rows.append(self._row("npm ci", ["npm", "ci", "--ignore-scripts", "--no-audit",
                                             "--no-fund"], cwd, INSTALL_TIMEOUT))
        if not rows or rows[0]["ok"]:
            rows.append(self._row("tsc --noEmit", ["npx", "--no-install", "tsc", "--noEmit"],
                                  cwd, TSC_TIMEOUT))
            rows.append(self._row("vitest run", ["npx", "--no-install", "vitest", "run"], cwd,
                                  VITEST_TIMEOUT))
        green = all(r["ok"] for r in rows)
        could_not = [r for r in rows if r.get("could_not_run")]
        if could_not:
            why = "; ".join(f"{r['check']}: {r['detail']}" for r in could_not)
            return self._result(task, pid, {"ok": False, "unavailable": why, "error": why,
                                            "rows": rows})
        return self._result(task, pid, {
            "ok": True, "green": green, "id": pid, "branch": f"api/{pid}", "commit": commit,
            "worktree": str(worktree), "rows": rows,
            "next": (f"branch api/{pid} is green; review it, then run tools\\deploy.ps1 from "
                     f"{worktree}. Nothing was deployed, no D1 was written, nothing was "
                     "pushed or published." if green else
                     f"branch api/{pid} is RED: the rows say what failed. Nothing was "
                     "deployed. Fix it in the worktree, or delete the branch and set "
                     "retry in the backlog to build it again.")})

    def _row(self, check: str, argv: list, cwd: Path, timeout: float) -> dict[str, Any]:
        try:
            code, out = self._run(argv, cwd, timeout)
        except (OSError, subprocess.TimeoutExpired, TimeoutError) as exc:
            return {"check": check, "ok": False, "could_not_run": True,
                    "detail": f"{type(exc).__name__}: {exc}"[:300]}
        return {"check": check, "ok": code == 0, "exit": code, "detail": _tail(out)}

    def _result(self, task: Task, pid: str, output: dict[str, Any]) -> TaskResult:
        return TaskResult(task_id=task.task_id, agent_id=self.manifest.agent_id,
                          output=output, evidence=(f"apibuild:id:{pid}",))
