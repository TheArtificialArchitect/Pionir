"""Adapter for Daedalus, Theo's coding bot, behind Pionir's gates.

Daedalus opens an isolated git worktree, edits, runs tests and lands (or
refuses) a local commit, all bounded by an uneditable germline it may never
touch. Routing it here puts a coding action under Pionir's gate and ledger.

Three capabilities:

- ``coding.daedalus_solve`` (PRIVILEGED, ``daedalus.solve``, which no client holds: every
  call parks for the owner) - any coding job, on the owner's Daedalus (:8771).
- ``coding.daedalus_build`` (PRIVILEGED, ``daedalus.build_sandbox`` - granted to the crew
  client only, for this capability only: pionir/auth.py) - the Builds division building one
  product. It never touches the owner's Daedalus. For each build it starts a SECOND Daedalus
  as the contained ``pionir-builds`` user (pionir/build_sandbox.py: :8772, a fresh bearer
  token, roots = the sandbox only, state and worktrees inside the sandbox, in a kill-on-close
  job object) and kills it when the build ends - so it exists only while a build runs,
  inside the overnight window. The repo must pass ``sandbox_repo_problem`` (exactly a marked
  repo the Builds worker made directly inside the sandbox), and before the job its
  ``.git/config`` is REWRITTEN to canonical content and ``.git/hooks`` emptied
  (pionir/sandbox_git.py). Without the sandbox user set up it runs nothing and says
  ``not configured: run tools\\setup-build-sandbox.ps1``.
- ``coding.daedalus_build_cancel`` (REVERSIBLE_WRITE) - cancel a build by its ``build_id``:
  the running one is cancelled and its Daedalus killed, and one not started yet never will.

**An empty repo is refused, always.** Daedalus falls back to its OWN protected repository.

Jobs are asynchronous: ``POST /jobs``, poll ``GET /jobs/{id}``; on the deadline ``POST
/jobs/{id}/cancel`` and ``AdapterTimeout`` naming the job. A solve's deadline is
``timeout_seconds``; a build's is its own budget, never past ``not_after`` (the window's
end). A few failed polls in a row are tolerated; any way out of the wait that is not a
finished job cancels the job and waits for it to stop - and for a build, kills its Daedalus -
BEFORE returning, so the GPU lease is never released under a Daedalus still working. The job
id of every job in flight is kept on disk; a job left by a restart is cancelled first thing.

The output surfaces ``branch``, ``commit``, ``files``, the ``gate`` and ``passed``,
``landed``, ``stage_failed``, ``gate_reason``.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pionir import atomic
from pionir.adapters._http import (
    HttpStatusError,
    LoopbackHttpSettings,
    LoopbackJsonClient,
    health_model,
)
from pionir.contracts import (
    AgentManifest,
    Capability,
    ModelRequirement,
    RiskLevel,
    Task,
    TaskResult,
)
from pionir.errors import AdapterProtocolError, AdapterUnavailable

SOLVE = "coding.daedalus_solve"
BUILD = "coding.daedalus_build"
CANCEL = "coding.daedalus_build_cancel"
SOLVE_PERMISSION = "daedalus.solve"
BUILD_PERMISSION = "daedalus.build_sandbox"
DEFAULT_SANDBOX_ROOT = r"C:\src\daedalus-work"
# Written by the Builds worker into a sandbox repo's .git folder when it creates it; a repo
# without it was not made by the worker and is never built in.
SANDBOX_MARKER = "pionir-sandbox.json"
SANDBOX_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{2,39}")
BUILD_FIELDS = frozenset({"intent", "repo", "verify", "context", "budget_seconds", "not_after",
                          "build_id"})
BUILD_ID = re.compile(r"[a-z0-9][a-z0-9-]{7,63}")
POLL_FAILURES_ALLOWED = 5
MIN_BUILD_SECONDS = 60
MAX_VERIFY_CHARS = 2_000

MAX_INTENT_CHARS = 8_000
# Daedalus's terminal job states (jobs.py: queued | running | done | error | cancelled).
FINISHED_STATES = frozenset({"done", "error", "cancelled"})
# A job id goes into a URL path; only a plain token may, whatever the server said.
_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# The summary fields a job's detail carries at its top level (jobs.py Job.summary).
_SUMMARY_FIELDS = ("passed", "landed", "stage_failed", "branch", "commit", "files")
_REMOTE = re.compile(r"(?im)^\s*\[\s*remote\b")
_WORKTREE_KEY = re.compile(r"(?im)^\s*worktree\s*=")


class AdapterTimeout(AdapterUnavailable):
    """The deadline passed while Daedalus was still working on a job.

    A subclass of AdapterUnavailable so every existing handler (the circuit,
    the ledger) treats it as before; distinct so a caller can tell "gave up
    waiting on job X" from "could not reach Daedalus", and the job id is kept
    so the outcome can be looked up once it does finish.
    """

    def __init__(self, message: str, *, job_id: str) -> None:
        super().__init__(message)
        self.job_id = job_id


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
    if os.path.normcase(root_real) != os.path.normcase(root_abs):
        return f"the sandbox workspace {root_abs} is itself a link or junction"
    repo_abs = os.path.abspath(raw)
    parent, name = os.path.split(repo_abs)
    if os.path.normcase(parent) != os.path.normcase(root_abs):
        return (f"{repo_abs[:120]} is not directly inside the sandbox workspace "
                f"{root_abs}")
    if not SANDBOX_SLUG.fullmatch(name):
        return f"{name[:60]!r} is not a sandbox repo's name (a slug: a-z, 0-9 and -)"
    path = Path(repo_abs)
    try:
        linked = path.is_symlink() or path.is_junction()
    except (OSError, AttributeError):
        linked = path.is_symlink()
    if linked:
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
    if git.is_symlink() or not git.is_dir():
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


@dataclass(frozen=True, slots=True)
class DaedalusSettings(LoopbackHttpSettings):
    base_url: str = "http://127.0.0.1:8771"
    # The whole-job deadline of a solve. A real coding job is minutes, not seconds.
    timeout_seconds: int = 600
    # One HTTP call: submit, a poll, a cancel.
    request_timeout_seconds: int = 30
    poll_interval_seconds: float = 3.0
    # Observability only; resolved live from /health at boot. qwen3-coder:30b fills the
    # card (measured 2026-09-13: ~10 GB on it, the rest in RAM), so a job takes the lease.
    model_id: str = "qwen3-coder:30b"
    # The Builds division's sandbox workspace: coding.daedalus_build reaches only repos
    # directly inside it (sandbox_repo_problem), through the build Daedalus on build_port.
    sandbox_root: str = DEFAULT_SANDBOX_ROOT
    build_port: int = 8772
    # The longest budget a build may ask for; its real deadline is its own budget.
    max_build_seconds: int = 3 * 3600
    # After a cancel, how long to wait for Daedalus to actually stop (at its next step
    # boundary) while the lease is still held. A build's Daedalus is killed after it.
    cancel_grace_seconds: float = 120.0
    # Where the id of every job in flight is kept, so a restart can cancel what it left.
    # "" keeps none (tests); bootstrap passes <state_root>/daedalus/jobs.json.
    state_file: str = ""

    def __post_init__(self) -> None:
        # Explicit rather than zero-argument super(): a slots=True dataclass is
        # rebuilt as a new class, which breaks the implicit __class__ cell.
        LoopbackHttpSettings.__post_init__(self)
        if self.request_timeout_seconds < 5:
            raise ValueError("Daedalus's per-request timeout must be at least 5 seconds")
        if self.poll_interval_seconds <= 0:
            raise ValueError("Daedalus's poll interval must be positive")
        if self.max_build_seconds < MIN_BUILD_SECONDS:
            raise ValueError("Daedalus's longest build must be at least a minute")
        if self.cancel_grace_seconds < 0:
            raise ValueError("Daedalus's cancel grace cannot be negative")


def _default_setup(settings: DaedalusSettings):
    from pionir.build_sandbox import load_setup
    return load_setup(settings.sandbox_root)


def _default_launcher(setup, settings: DaedalusSettings):
    from pionir.build_sandbox import BuildDaedalus
    return BuildDaedalus(setup, port=settings.build_port, model=settings.model_id)


def _default_sanitize(repo: str) -> None:
    from pionir.sandbox_git import sanitize
    sanitize(repo)


class DaedalusAdapter:
    """Daedalus as a gated, audited Pionir coding specialist."""

    def __init__(
        self,
        settings: DaedalusSettings | None = None,
        *,
        client: LoopbackJsonClient | None = None,
        sleep: Any = time.sleep,
        monotonic: Any = time.monotonic,
        clock: Any = time.time,
        setup: Callable[[], tuple] | None = None,
        launcher: Callable[..., Any] | None = None,
        build_client: Callable[[str, str], Any] | None = None,
        sanitize: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings or DaedalusSettings()
        self._client = client or LoopbackJsonClient("Daedalus", self.settings)
        self._sleep = sleep
        self._monotonic = monotonic
        self._clock = clock
        self._setup = setup or (lambda: _default_setup(self.settings))
        self._launcher = launcher or (lambda s: _default_launcher(s, self.settings))
        self._build_client = build_client or (lambda base, token: LoopbackJsonClient(
            "Build Daedalus", LoopbackHttpSettings(base_url=base, token=token)))
        self._sanitize = sanitize or _default_sanitize
        self._lock = threading.RLock()
        self._running: dict[str, dict[str, Any]] = {}     # build_id -> {client, job, launcher}
        self._leftovers_checked = False
        model = ModelRequirement(
            model_id=self.settings.model_id,
            # Measured 2026-09-13: Ollama loads qwen3-coder:30b at ~18-19 GB, ~10 GB on the
            # card, evicting gemma3:12b. 10_000 is the on-card share, and it admits (the
            # budget allows 12_288 - 1_830 = 10_458 MB). Fitting means sidelining the
            # voice's model - only under the lease, put back when the lease ends.
            estimated_vram_mb=10_000,
            context_vram_mb=0,
            requires_gpu=True,
            exclusive_card=True,
        )
        self._manifest = AgentManifest(
            agent_id="daedalus",
            version="tech-support/daedalus",
            capabilities=(
                Capability(
                    name=SOLVE,
                    description="Daedalus writing code in an isolated worktree behind its germline",
                    risk=RiskLevel.PRIVILEGED,
                    required_permissions=frozenset({SOLVE_PERMISSION}),
                    model=model,
                    routing_hints=frozenset(
                        {"daedalus", "code", "coding", "refactor", "implement", "patch"}
                    ),
                    priority=100,
                ),
                Capability(
                    name=BUILD,
                    description="A contained Daedalus (the pionir-builds user) building one "
                                "product in a fresh sandbox repo (the crew's overnight builds)",
                    risk=RiskLevel.PRIVILEGED,
                    required_permissions=frozenset({BUILD_PERMISSION}),
                    model=model,
                    routable=False,
                ),
                Capability(
                    name=CANCEL,
                    description="Cancel a sandbox build by its id (it stops, or never starts)",
                    risk=RiskLevel.REVERSIBLE_WRITE,
                    routable=False,
                ),
            ),
        )

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def status(self) -> Mapping[str, Any]:
        document = self._client.get("/health")
        if document.get("ok") is not True:
            raise AdapterProtocolError("Daedalus's health response is malformed")
        return document

    def resolve_model(self) -> str | None:
        return health_model(self._client)

    # ---- the job ledger on disk ---------------------------------------------------------------
    def _ledger(self) -> dict:
        blank = {"jobs": {}, "cancelled": []}
        path = self.settings.state_file
        if not path:
            return getattr(self, "_memory_ledger", blank)
        try:
            doc = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return blank
        if not isinstance(doc, dict):
            return blank
        doc.setdefault("jobs", {})
        doc.setdefault("cancelled", [])
        return doc

    def _write_ledger(self, doc: dict) -> None:
        doc["cancelled"] = list(doc.get("cancelled") or [])[-200:]
        path = self.settings.state_file
        if not path:
            self._memory_ledger = doc
            return
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
        atomic.replace(tmp, target)

    def _remember(self, key: str, entry: dict | None) -> None:
        with self._lock:
            doc = self._ledger()
            if entry is None:
                doc["jobs"].pop(key, None)
            else:
                doc["jobs"][key] = entry
            self._write_ledger(doc)

    def _cancelled(self, build_id: str) -> bool:
        with self._lock:
            return build_id in self._ledger()["cancelled"]

    def _cancel_leftovers(self) -> None:
        """Jobs a previous Pionir left in flight: a solve's is cancelled on the owner's
        Daedalus; a build's Daedalus died with that Pionir (kill-on-close), so there is
        nothing left of it to stop. Once per process, before any new job."""
        with self._lock:
            if self._leftovers_checked:
                return
            self._leftovers_checked = True
            doc = self._ledger()
            left = dict(doc["jobs"])
            doc["jobs"] = {}
            self._write_ledger(doc)
        for entry in left.values():
            if entry.get("kind") == "solve" and _JOB_ID.match(str(entry.get("job_id") or "")):
                self._cancel(self._client, str(entry["job_id"]))

    # ---- the checks, before anything is parked or sent ------------------------------------
    @staticmethod
    def _intent(payload: Mapping[str, Any]) -> str:
        intent = str(payload.get("intent") or payload.get("content") or "").strip()
        if not intent:
            raise AdapterProtocolError("Daedalus needs a coding intent")
        if len(intent) > MAX_INTENT_CHARS:
            raise AdapterProtocolError(
                f"coding intent exceeds Daedalus's {MAX_INTENT_CHARS}-character limit"
            )
        return intent

    def validate(self, task: Task) -> None:
        """The argument checks, with no side effect: Pionir runs this before it parks a
        solve for approval, so the owner is never asked to approve one that cannot run."""

        payload = task.payload
        if task.capability == CANCEL:
            self._build_id(payload, only=True)
            return
        if task.capability == BUILD:
            self._intent(payload)
            self._build_budget(payload)
            return
        # A solve's intent is checked when it runs, as it always was; the repo rule is
        # checked before parking so the owner is never asked about one without a repo.
        if not str(payload.get("repo") or "").strip():
            raise AdapterProtocolError(
                "Daedalus needs a repo: with none it falls back to its own repository, "
                "which is never worked on through Pionir"
            )

    @staticmethod
    def _build_id(payload: Mapping[str, Any], *, only: bool = False) -> str:
        if only and set(payload) != {"build_id"}:
            raise AdapterProtocolError(f"{CANCEL}: the payload is {{build_id}}")
        build_id = payload.get("build_id")
        if not isinstance(build_id, str) or not BUILD_ID.fullmatch(build_id):
            raise AdapterProtocolError("build_id must be 8 to 64 of a-z, 0-9 and -")
        return build_id

    def _build_budget(self, payload: Mapping[str, Any]) -> tuple[int, float]:
        """A build's checked arguments: its budget in seconds and its not_after."""

        extra = sorted(set(payload) - BUILD_FIELDS)
        if extra:
            raise AdapterProtocolError(f"{BUILD}: {extra[0]!r} is not a build field")
        self._build_id(payload)
        problem = sandbox_repo_problem(payload.get("repo"), self.settings.sandbox_root)
        if problem:
            raise AdapterProtocolError(f"{BUILD}: refused - {problem}")
        budget = payload.get("budget_seconds")
        if isinstance(budget, bool) or not isinstance(budget, int) \
                or not MIN_BUILD_SECONDS <= budget <= self.settings.max_build_seconds:
            raise AdapterProtocolError(
                f"{BUILD}: budget_seconds must be whole seconds from {MIN_BUILD_SECONDS} to "
                f"{self.settings.max_build_seconds}"
            )
        not_after = payload.get("not_after")
        if isinstance(not_after, bool) or not isinstance(not_after, (int, float)) \
                or not_after <= 0:
            raise AdapterProtocolError(f"{BUILD}: not_after (the window's end, wall clock) "
                                       "is required")
        verify = payload.get("verify")
        if verify is not None and (not isinstance(verify, str) or len(verify) > MAX_VERIFY_CHARS):
            raise AdapterProtocolError(f"{BUILD}: verify must be a command of at most "
                                       f"{MAX_VERIFY_CHARS} characters")
        context = payload.get("context")
        if context is not None and not isinstance(context, dict):
            raise AdapterProtocolError(f"{BUILD}: context must be an object")
        return budget, float(not_after)

    # ---- running a job -------------------------------------------------------------------------
    def execute(self, task: Task) -> TaskResult:
        self.validate(task)
        if task.capability == CANCEL:
            return self._cancel_build(task)
        self._cancel_leftovers()
        if task.capability == BUILD:
            return self._build(task)
        payload = task.payload
        request: dict[str, Any] = {"intent": self._intent(payload)}
        request["repo"] = str(payload["repo"])
        if payload.get("verify"):
            request["verify"] = str(payload["verify"])
        if payload.get("context") is not None:
            request["context"] = payload["context"]
        request["dry_run"] = bool(payload.get("dry_run", False))

        per_request = self.settings.request_timeout_seconds
        try:
            started = self._client.post("/jobs", request, timeout_seconds=per_request)
        except HttpStatusError as error:
            if error.status != 404:
                raise
            # An older Daedalus without the job routes: the blocking call, with
            # the whole deadline as its one request timeout, is all there is.
            document = self._client.post(
                "/solve", request, timeout_seconds=self.settings.timeout_seconds
            )
            return self._result(task, document, job_id=None)
        job_id = self._job_id(started)
        key = f"solve:{job_id}"
        self._remember(key, {"kind": "solve", "job_id": job_id, "at": self._clock()})
        try:
            detail = self._await_job(self._client, job_id, self.settings.timeout_seconds,
                                     grace=self.settings.cancel_grace_seconds)
        finally:
            self._remember(key, None)
        return self._result(task, detail, job_id=job_id)

    def _refused(self, task: Task, why: str, **extra: Any) -> TaskResult:
        return TaskResult(task_id=task.task_id, agent_id=self.manifest.agent_id,
                          output={"ok": False, "refused": why, "started": False, **extra},
                          evidence=("daedalus:build", "daedalus:build:not-started"))

    def _build(self, task: Task) -> TaskResult:
        """One sandbox build on its own contained Daedalus; never past not_after."""

        payload = task.payload
        budget, not_after = self._build_budget(payload)
        build_id = self._build_id(payload)
        if self._cancelled(build_id):
            return self._refused(task, f"build {build_id} was cancelled; nothing was started")
        left = not_after - float(self._clock())
        if left <= 0:
            # The lease came too late: the window closed before the job could start.
            return self._refused(task, "the build window closed before the job could start; "
                                       "nothing was sent to Daedalus")
        setup, why = self._setup()
        if setup is None:
            return self._refused(task, why or "not configured", not_configured=True)
        repo = os.path.abspath(str(payload["repo"]).strip())
        try:
            self._sanitize(repo)
        except Exception as error:  # noqa: BLE001 - never build in a repo we could not clean
            raise AdapterProtocolError(f"{BUILD}: refused - its git config could not be made "
                                       f"canonical ({error})") from error
        problem = sandbox_repo_problem(repo, self.settings.sandbox_root)
        if problem:
            raise AdapterProtocolError(f"{BUILD}: refused - {problem}")
        request: dict[str, Any] = {"intent": self._intent(payload), "repo": repo,
                                   "dry_run": False,
                                   "context": {**(payload.get("context") or {}),
                                               "deadline": not_after, "build_id": build_id}}
        if payload.get("verify"):
            request["verify"] = str(payload["verify"])
        launcher = self._launcher(setup)
        key = f"build:{build_id}"
        try:
            try:
                token = launcher.start(not_after=not_after)
            except Exception as error:  # noqa: BLE001 - a Daedalus that did not come up
                raise AdapterUnavailable(f"the build Daedalus could not start: {error}") \
                    from error
            client = self._build_client(launcher.base_url, token)
            self._remember(key, {"kind": "build", "build_id": build_id,
                                 "port": self.settings.build_port, "at": self._clock()})
            with self._lock:
                self._running[build_id] = {"client": client, "job": None, "launcher": launcher}
            try:
                started = client.post("/jobs", request,
                                      timeout_seconds=self.settings.request_timeout_seconds)
            except HttpStatusError as error:
                if error.status != 404:
                    raise
                raise AdapterUnavailable("the build Daedalus does not serve /jobs; a build "
                                         "needs a cancellable job") from error
            job_id = self._job_id(started)
            with self._lock:
                self._running[build_id]["job"] = job_id
            self._remember(key, {"kind": "build", "build_id": build_id, "job_id": job_id,
                                 "port": self.settings.build_port, "at": self._clock()})
            if self._cancelled(build_id):
                self._cancel(client, job_id)
                raise AdapterUnavailable(f"build {build_id} was cancelled")
            detail = self._await_job(client, job_id, min(float(budget), left),
                                     grace=self.settings.cancel_grace_seconds)
            return self._result(task, detail, job_id=job_id)
        finally:
            with self._lock:
                self._running.pop(build_id, None)
            self._remember(key, None)
            # the whole tree dies BEFORE the lease is released (we are still inside it)
            launcher.stop()

    def _cancel_build(self, task: Task) -> TaskResult:
        build_id = self._build_id(task.payload, only=True)
        with self._lock:
            doc = self._ledger()
            if build_id not in doc["cancelled"]:
                doc["cancelled"].append(build_id)
            self._write_ledger(doc)
            entry = self._running.get(build_id)
        if entry is not None:
            if entry.get("job"):
                self._cancel(entry["client"], entry["job"])
            entry["launcher"].stop()
        return TaskResult(task_id=task.task_id, agent_id=self.manifest.agent_id,
                          output={"ok": True, "build_id": build_id,
                                  "was_running": entry is not None},
                          evidence=("daedalus:build-cancel",))

    @staticmethod
    def _job_id(started: Mapping[str, Any]) -> str:
        job = started.get("job")
        job_id = str(job.get("id") or "").strip() if isinstance(job, dict) else ""
        if not _JOB_ID.match(job_id):
            raise AdapterProtocolError("Daedalus accepted the job but returned no usable job id")
        return job_id

    def _poll(self, client, job_id: str) -> dict[str, Any]:
        detail = client.get(
            f"/jobs/{job_id}", timeout_seconds=self.settings.request_timeout_seconds
        )
        job = detail.get("job")
        if not isinstance(job, dict):
            raise AdapterProtocolError(f"Daedalus's detail for job {job_id} is malformed")
        return job

    def _await_job(self, client, job_id: str, timeout: float, *,
                   grace: float = 0.0) -> Mapping[str, Any]:
        """Poll the job until it finishes. A few failed polls in a row are tolerated. Any
        other way out - the deadline, Daedalus gone for good, an error, an interrupt -
        cancels the job and waits for it to stop before this returns or raises."""

        deadline = self._monotonic() + timeout
        failures = 0
        finished = False
        try:
            while True:
                try:
                    job = self._poll(client, job_id)
                    failures = 0
                except AdapterUnavailable as error:
                    failures += 1
                    if failures > POLL_FAILURES_ALLOWED:
                        raise AdapterUnavailable(
                            f"Daedalus became unreachable while running job {job_id}: {error}"
                        ) from error
                    job = None
                if job is not None and str(job.get("state") or "") in FINISHED_STATES:
                    finished = True
                    return job
                if self._monotonic() >= deadline:
                    raise AdapterTimeout(
                        f"Daedalus job {job_id} did not finish within {timeout:.0f} seconds; "
                        f"it was cancelled - the outcome is at GET /jobs/{job_id}",
                        job_id=job_id,
                    )
                self._sleep(self.settings.poll_interval_seconds)
        finally:
            if not finished:
                self._cancel(client, job_id)
                if grace > 0:
                    self._wind_down(client, job_id, grace)

    def _wind_down(self, client, job_id: str, grace: float) -> str | None:
        """After a cancel, wait up to ``grace`` seconds for the job to stop - still under
        the lease. The state it reached, or None if it had not stopped."""

        until = self._monotonic() + grace
        while self._monotonic() < until:
            self._sleep(self.settings.poll_interval_seconds)
            try:
                state = str(self._poll(client, job_id).get("state") or "")
            except (AdapterUnavailable, AdapterProtocolError):
                continue
            if state in FINISHED_STATES:
                return f"stopped: {state}"
        return None

    def _cancel(self, client, job_id: str) -> None:
        """Best effort: the deadline is the news; a failed cancel must not hide it."""

        try:
            client.post(
                f"/jobs/{job_id}/cancel", {}, timeout_seconds=self.settings.request_timeout_seconds
            )
        except (AdapterUnavailable, AdapterProtocolError):
            pass

    def _result(
        self, task: Task, document: Mapping[str, Any], *, job_id: str | None
    ) -> TaskResult:
        """Shape a finished job (or a /solve answer) into Pionir's result. A refused or
        failed solve is a real outcome (ok=false with a gate/error), not an adapter
        failure. The branch, commit, files and the gate's verdict are always surfaced."""

        if job_id is None:
            output: dict[str, Any] = dict(document)
        else:
            state = str(document.get("state") or "")
            inner = document.get("result")
            if isinstance(inner, dict):
                output = dict(inner)
            else:
                output = {
                    "ok": False,
                    "error": str(document.get("error") or f"job {state}"),
                }
            output["job_id"] = job_id
            output["state"] = state
        if not isinstance(output.get("ok"), bool):
            raise AdapterProtocolError("Daedalus returned no ok verdict")
        gate = output.get("gate")
        if isinstance(gate, dict):
            for key in ("passed", "landed", "stage_failed"):
                output.setdefault(key, gate.get(key))
            output.setdefault("gate_reason", gate.get("reason"))
        if job_id is not None:
            for key in _SUMMARY_FIELDS:
                if output.get(key) is None and document.get(key) is not None:
                    output[key] = document[key]
        if task.capability == BUILD:
            output.pop("steps", None)           # large, and nothing downstream reads them
        commit = str(output.get("commit") or "").strip()
        evidence: list[str] = ["daedalus:build" if task.capability == BUILD else "daedalus:solve"]
        if job_id is not None:
            evidence.append(f"daedalus:job:{job_id}")
        if commit:
            evidence.append(f"daedalus:commit:{commit}")
        return TaskResult(
            task_id=task.task_id,
            agent_id=self.manifest.agent_id,
            output=output,
            evidence=tuple(evidence),
        )
