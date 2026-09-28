"""Adapter for Daedalus, Theo's coding bot, behind Pionir's gates.

Daedalus opens an isolated git worktree, edits, runs tests and lands (or
refuses) a local commit, all bounded by an uneditable germline it may never
touch. Until now it was reachable only through Theo's `execute_task`, outside
Pionir's permission gates and outside its audit ledger; routing it here is what
puts a coding action under the same gate and ledger as everything else.

Two capabilities, both PRIVILEGED (they write code and land commits):

- ``coding.daedalus_solve`` - any coding job in any repo Daedalus reaches. It
  needs ``daedalus.solve``, which nothing holds, so every call is parked for the
  owner's approval (unchanged). Its commits are local and reversible (its own
  ``/revert``, and ``dry_run`` plans without landing).
- ``coding.daedalus_build`` - the crew's Builds division building one product in
  a SANDBOX repo: a fresh repository the Builds worker created directly under
  the sandbox workspace (``sandbox_root``, ``C:\\src\\daedalus-work``). It needs
  ``daedalus.build_sandbox`` - the narrowest grant there is, because the
  capability itself can reach nothing else: every call's repo must pass
  ``sandbox_repo_problem`` (exactly ``<sandbox_root>\\<slug>``, resolved with
  every link and junction followed and compared case-insensitively on Windows;
  no ``..``, no UNC path, no other drive; a real ``.git`` folder carrying the
  Builds worker's marker and no remote - so a clone of a live repo, a linked
  worktree or a link into one is refused). Holding the permission buys a write
  inside the sandbox and nothing more. Not routable: by name only.

**An empty repo is refused, always, by both.** Daedalus falls back to its OWN
repository when no repo is named, and that repository is protected.

The call is asynchronous on purpose. ``POST /solve`` blocks for the whole job
with no way to cancel, and the ledger showed what that costs: at exactly the
client timeout Pionir recorded AdapterUnavailable while Daedalus kept working
and landed a commit nobody was told about, because the ``daedalus:commit:<sha>``
evidence was only ever emitted on the success path. So this submits to
``POST /jobs``, polls ``GET /jobs/{id}`` until the job finishes or the deadline
passes, and on the deadline asks ``POST /jobs/{id}/cancel`` and raises
``AdapterTimeout`` naming the job id - so the outcome can be found afterwards at
``GET /jobs/{id}`` rather than vanishing. ``/solve`` remains only as the fallback
for an older Daedalus that answers 404 to ``/jobs`` (never for a build: a
blocking call could not be cancelled at the window's end).

A solve's deadline is ``timeout_seconds`` (600). A build's is its own
``budget_seconds`` (real jobs take 5-20 minutes, so a build is given its budget,
not 600 s), capped by ``max_build_seconds`` and by ``not_after`` - the wall-clock
end of the overnight window the crew allows GPU work in. A build that could not
even start before ``not_after`` (the lease came too late) is refused without
calling Daedalus. On a build's deadline the adapter cancels and then waits up to
``cancel_grace_seconds`` for Daedalus to stop, so the GPU lease still covers the
wind-down.

The output always surfaces what the owner and the crew need from a finished job:
``branch``, ``commit``, ``files``, the ``gate`` and, from it, ``passed``,
``landed``, ``stage_failed`` and ``gate_reason``.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
SOLVE_PERMISSION = "daedalus.solve"
BUILD_PERMISSION = "daedalus.build_sandbox"
DEFAULT_SANDBOX_ROOT = r"C:\src\daedalus-work"
# Written by the Builds worker into a sandbox repo's .git folder when it creates it; a repo
# without it was not made by the worker and is never built in.
SANDBOX_MARKER = "pionir-sandbox.json"
SANDBOX_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{2,39}")
BUILD_FIELDS = frozenset({"intent", "repo", "verify", "context", "budget_seconds", "not_after"})
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
    # The whole-job deadline. A real coding job - worktree, edits, a test run,
    # repairs - is minutes, not seconds. Generous against that tail rather than
    # tuned to a median.
    timeout_seconds: int = 600
    # One HTTP call: submit, a poll, a cancel. Short, because the job's own
    # length is carried by the deadline above, not by any single request.
    request_timeout_seconds: int = 30
    poll_interval_seconds: float = 3.0
    # Observability only; resolved live from /health at boot. Daedalus runs
    # qwen3-coder:30b - a code-specialist MoE. It is NOT a CPU tenant: measured
    # 2026-09-13, Ollama loads it at ~18-19 GB with ~10 GB on the card, and the
    # load evicted gemma3:12b. So a coding job takes the GPU lease (see the
    # capability's ModelRequirement below).
    model_id: str = "qwen3-coder:30b"
    # The Builds division's sandbox workspace: coding.daedalus_build reaches only repos
    # directly inside it (sandbox_repo_problem).
    sandbox_root: str = DEFAULT_SANDBOX_ROOT
    # The longest budget a build may ask for; its real deadline is its own budget.
    max_build_seconds: int = 3 * 3600
    # After a build's deadline cancel, how long to wait for Daedalus to actually stop
    # (it stops at its next step boundary) while the lease is still held.
    cancel_grace_seconds: float = 120.0

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
    ) -> None:
        self.settings = settings or DaedalusSettings()
        self._client = client or LoopbackJsonClient("Daedalus", self.settings)
        self._sleep = sleep
        self._monotonic = monotonic
        self._clock = clock
        model = ModelRequirement(
            model_id=self.settings.model_id,
            # Measured 2026-09-13 (pionir.ps1, commit 484a6c0): Ollama
            # loads qwen3-coder:30b at ~18-19 GB, ~10 GB of it on the
            # card and the rest in system RAM, and the load evicted
            # gemma3:12b. The old 0 / requires_gpu=False claimed a CPU
            # tenant; it was false, and it meant a coding job took no
            # lease while it filled the card.
            #
            # 10_000 is the measured on-card share, and it admits: the
            # budget allows 12_288 - 1_830 = 10_458 MB, and an emptied
            # card shows ~10_450 free. Ollama spills the rest to RAM
            # rather than refusing, so declaring the whole 18-19 GB
            # would be unadmittable for a model that does run here.
            # Context is 0 on purpose: raising DAEDALUS_NUM_CTX to
            # 32768 measured +0.83 GB system RAM per +16K and VRAM
            # unchanged. Fitting means sidelining the voice's model -
            # allowed only under the lease, while she has stood down,
            # and put back when the lease ends (scheduler handback).
            estimated_vram_mb=10_000,
            context_vram_mb=0,
            requires_gpu=True,
            # The 30B coder fills the card; making room for it may
            # evict even the protected voice model, but only once the
            # shared lease is held (she has stood down) and the lease's
            # release re-warms hers. No other tenant declares this.
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
                    description="Daedalus building one product in a fresh sandbox repo under "
                                "the Builds workspace (the crew's overnight builds only)",
                    risk=RiskLevel.PRIVILEGED,
                    required_permissions=frozenset({BUILD_PERMISSION}),
                    model=model,
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

    # ---- the checks, before anything is parked or sent ------------------------------------
    @staticmethod
    def _intent(payload: Mapping[str, Any]) -> str:
        # The router passes the request as "content"; a structured caller may
        # send "intent" plus repo/verify/dry_run. Accept both.
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
        if task.capability == BUILD:
            self._intent(payload)
            self._build_budget(payload)
            return
        # A solve's intent is checked when it runs, as it always was; only the repo rule is
        # new, and it is checked before parking so the owner is never asked about one.
        if not str(payload.get("repo") or "").strip():
            raise AdapterProtocolError(
                "Daedalus needs a repo: with none it falls back to its own repository, "
                "which is never worked on through Pionir"
            )

    def _build_budget(self, payload: Mapping[str, Any]) -> tuple[int, float | None]:
        """A build's checked arguments: its budget in seconds and its not_after (or None)."""

        extra = sorted(set(payload) - BUILD_FIELDS)
        if extra:
            raise AdapterProtocolError(f"{BUILD}: {extra[0]!r} is not a build field")
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
        if not_after is not None and (isinstance(not_after, bool)
                                      or not isinstance(not_after, (int, float))
                                      or not_after <= 0):
            raise AdapterProtocolError(f"{BUILD}: not_after must be a wall-clock time")
        verify = payload.get("verify")
        if verify is not None and (not isinstance(verify, str) or len(verify) > MAX_VERIFY_CHARS):
            raise AdapterProtocolError(f"{BUILD}: verify must be a command of at most "
                                       f"{MAX_VERIFY_CHARS} characters")
        context = payload.get("context")
        if context is not None and not isinstance(context, dict):
            raise AdapterProtocolError(f"{BUILD}: context must be an object")
        return budget, (float(not_after) if not_after is not None else None)

    # ---- running a job -------------------------------------------------------------------------
    def execute(self, task: Task) -> TaskResult:
        self.validate(task)
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
        return self._result(
            task, self._await_job(job_id, self.settings.timeout_seconds), job_id=job_id
        )

    def _build(self, task: Task) -> TaskResult:
        """One sandbox build: its own budget as the deadline, never past not_after."""

        payload = task.payload
        budget, not_after = self._build_budget(payload)
        deadline = float(budget)
        if not_after is not None:
            left = not_after - float(self._clock())
            if left <= 0:
                # The lease came too late: the window closed before the job could start.
                return TaskResult(
                    task_id=task.task_id, agent_id=self.manifest.agent_id,
                    output={"ok": False, "refused": "the build window closed before the job "
                            "could start; nothing was sent to Daedalus", "started": False},
                    evidence=("daedalus:build", "daedalus:build:not-started"),
                )
            deadline = min(deadline, left)
        request: dict[str, Any] = {
            "intent": self._intent(payload),
            "repo": os.path.abspath(str(payload["repo"]).strip()),
            "dry_run": False,
        }
        if payload.get("verify"):
            request["verify"] = str(payload["verify"])
        if payload.get("context") is not None:
            request["context"] = payload["context"]
        try:
            started = self._client.post(
                "/jobs", request, timeout_seconds=self.settings.request_timeout_seconds
            )
        except HttpStatusError as error:
            if error.status != 404:
                raise
            raise AdapterUnavailable(
                "Daedalus does not serve /jobs; a build needs a cancellable job"
            ) from error
        job_id = self._job_id(started)
        detail = self._await_job(job_id, deadline, grace=self.settings.cancel_grace_seconds)
        return self._result(task, detail, job_id=job_id)

    @staticmethod
    def _job_id(started: Mapping[str, Any]) -> str:
        job = started.get("job")
        job_id = str(job.get("id") or "").strip() if isinstance(job, dict) else ""
        if not _JOB_ID.match(job_id):
            raise AdapterProtocolError("Daedalus accepted the job but returned no usable job id")
        return job_id

    def _poll(self, job_id: str) -> dict[str, Any]:
        try:
            detail = self._client.get(
                f"/jobs/{job_id}", timeout_seconds=self.settings.request_timeout_seconds
            )
        except AdapterUnavailable as error:
            raise AdapterUnavailable(
                f"Daedalus became unreachable while running job {job_id}: {error}"
            ) from error
        job = detail.get("job")
        if not isinstance(job, dict):
            raise AdapterProtocolError(f"Daedalus's detail for job {job_id} is malformed")
        return job

    def _await_job(self, job_id: str, timeout: float, *, grace: float = 0.0) -> Mapping[str, Any]:
        """Poll the job until it finishes; on the deadline, cancel it and say which."""

        deadline = self._monotonic() + timeout
        while True:
            job = self._poll(job_id)
            if str(job.get("state") or "") in FINISHED_STATES:
                return job
            if self._monotonic() >= deadline:
                self._cancel(job_id)
                stopped = self._wind_down(job_id, grace) if grace > 0 else None
                state = f" (it has {stopped})" if stopped else (
                    " (it stops at its next step boundary)")
                raise AdapterTimeout(
                    f"Daedalus job {job_id} did not finish within {timeout:.0f} seconds; "
                    f"cancellation requested{state} - the outcome is at GET /jobs/{job_id}",
                    job_id=job_id,
                )
            self._sleep(self.settings.poll_interval_seconds)

    def _wind_down(self, job_id: str, grace: float) -> str | None:
        """After a cancel, wait up to ``grace`` seconds for the job to stop - still under the
        lease, so the voice's model is not re-warmed onto a card Daedalus still fills. The
        state it reached, or None if it had not stopped."""

        until = self._monotonic() + grace
        while self._monotonic() < until:
            self._sleep(self.settings.poll_interval_seconds)
            try:
                state = str(self._poll(job_id).get("state") or "")
            except (AdapterUnavailable, AdapterProtocolError):
                return None
            if state in FINISHED_STATES:
                return f"stopped: {state}"
        return None

    def _cancel(self, job_id: str) -> None:
        """Best effort: the deadline is the news; a failed cancel must not hide it."""

        try:
            self._client.post(
                f"/jobs/{job_id}/cancel", {}, timeout_seconds=self.settings.request_timeout_seconds
            )
        except (AdapterUnavailable, AdapterProtocolError):
            pass

    def _result(
        self, task: Task, document: Mapping[str, Any], *, job_id: str | None
    ) -> TaskResult:
        """Shape a finished job (or a /solve answer) into Pionir's result.

        A job detail carries the dispatcher's outcome under ``result`` (the same
        object /solve returns) - or none at all when the worker raised, in
        which case ``error`` says why. A refused or failed solve is a real
        outcome (ok=false with a gate/error), not an adapter failure: return it
        so the ledger and the caller see the verdict rather than "unavailable".
        The branch, commit, files and the gate's verdict are always surfaced at the
        top level, from the result or from the job's own summary.
        """

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
