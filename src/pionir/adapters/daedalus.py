"""Adapter for Daedalus, Theo's coding bot, behind Pionir's gates.

Daedalus opens an isolated git worktree, edits, runs tests and lands (or
refuses) a local commit, all bounded by an uneditable germline it may never
touch. Until now it was reachable only through Theo's `execute_task`, outside
Pionir's permission gates and outside its audit ledger; routing it here is what
puts a coding action under the same gate and ledger as everything else.

The capability is PRIVILEGED: it writes code and lands commits. They are local
and reversible (its own `/revert`, and `dry_run` plans without landing), but a
write is a write, so it needs an explicit permission and the dashboard runs it
behind a confirm. `dry_run` is honoured from the task so a caller can ask for a
plan without a commit.

Theo is not in the loop: this calls Daedalus's own HTTP server directly.

The call is asynchronous on purpose. `POST /solve` blocks for the whole job
with no way to cancel, and the ledger showed what that costs: at exactly the
client timeout Pionir recorded AdapterUnavailable while Daedalus kept working
and landed a commit nobody was told about, because the `daedalus:commit:<sha>`
evidence was only ever emitted on the success path. So this submits to
`POST /jobs`, polls `GET /jobs/{id}` until the job finishes or the deadline
passes, and on the deadline asks `POST /jobs/{id}/cancel` and raises
`AdapterTimeout` naming the job id - so the outcome can be found afterwards at
`GET /jobs/{id}` rather than vanishing. `/solve` remains only as the fallback
for an older Daedalus that answers 404 to `/jobs`.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
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

MAX_INTENT_CHARS = 8_000
# Daedalus's terminal job states (jobs.py: queued | running | done | error | cancelled).
FINISHED_STATES = frozenset({"done", "error", "cancelled"})
# A job id goes into a URL path; only a plain token may, whatever the server said.
_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


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

    def __post_init__(self) -> None:
        # Explicit rather than zero-argument super(): a slots=True dataclass is
        # rebuilt as a new class, which breaks the implicit __class__ cell.
        LoopbackHttpSettings.__post_init__(self)
        if self.request_timeout_seconds < 5:
            raise ValueError("Daedalus's per-request timeout must be at least 5 seconds")
        if self.poll_interval_seconds <= 0:
            raise ValueError("Daedalus's poll interval must be positive")


class DaedalusAdapter:
    """Daedalus as a gated, audited Pionir coding specialist."""

    def __init__(
        self,
        settings: DaedalusSettings | None = None,
        *,
        client: LoopbackJsonClient | None = None,
        sleep: Any = time.sleep,
        monotonic: Any = time.monotonic,
    ) -> None:
        self.settings = settings or DaedalusSettings()
        self._client = client or LoopbackJsonClient("Daedalus", self.settings)
        self._sleep = sleep
        self._monotonic = monotonic
        self._manifest = AgentManifest(
            agent_id="daedalus",
            version="tech-support/daedalus",
            capabilities=(
                Capability(
                    name="coding.daedalus_solve",
                    description="Daedalus writing code in an isolated worktree behind its germline",
                    risk=RiskLevel.PRIVILEGED,
                    required_permissions=frozenset({"daedalus.solve"}),
                    model=ModelRequirement(
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
                    ),
                    routing_hints=frozenset(
                        {"daedalus", "code", "coding", "refactor", "implement", "patch"}
                    ),
                    priority=100,
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

    def execute(self, task: Task) -> TaskResult:
        # The router passes the request as "content"; a structured caller may
        # send "intent" plus repo/verify/dry_run. Accept both.
        intent = str(task.payload.get("intent") or task.payload.get("content") or "").strip()
        if not intent:
            raise AdapterProtocolError("Daedalus needs a coding intent")
        if len(intent) > MAX_INTENT_CHARS:
            raise AdapterProtocolError(
                f"coding intent exceeds Daedalus's {MAX_INTENT_CHARS}-character limit"
            )
        request: dict[str, Any] = {"intent": intent}
        if task.payload.get("repo"):
            request["repo"] = str(task.payload["repo"])
        if task.payload.get("verify"):
            request["verify"] = str(task.payload["verify"])
        if task.payload.get("context") is not None:
            request["context"] = task.payload["context"]
        request["dry_run"] = bool(task.payload.get("dry_run", False))

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
        job = started.get("job")
        job_id = str(job.get("id") or "").strip() if isinstance(job, dict) else ""
        if not _JOB_ID.match(job_id):
            raise AdapterProtocolError("Daedalus accepted the job but returned no usable job id")
        return self._result(task, self._await_job(job_id), job_id=job_id)

    def _await_job(self, job_id: str) -> Mapping[str, Any]:
        """Poll the job until it finishes; on the deadline, cancel it and say which."""

        per_request = self.settings.request_timeout_seconds
        deadline = self._monotonic() + self.settings.timeout_seconds
        while True:
            try:
                detail = self._client.get(f"/jobs/{job_id}", timeout_seconds=per_request)
            except AdapterUnavailable as error:
                raise AdapterUnavailable(
                    f"Daedalus became unreachable while running job {job_id}: {error}"
                ) from error
            job = detail.get("job")
            if not isinstance(job, dict):
                raise AdapterProtocolError(f"Daedalus's detail for job {job_id} is malformed")
            if str(job.get("state") or "") in FINISHED_STATES:
                return job
            if self._monotonic() >= deadline:
                self._cancel(job_id)
                raise AdapterTimeout(
                    f"Daedalus job {job_id} did not finish within "
                    f"{self.settings.timeout_seconds} seconds; cancellation requested "
                    f"(it stops at its next step boundary) - the outcome is at "
                    f"GET /jobs/{job_id}",
                    job_id=job_id,
                )
            self._sleep(self.settings.poll_interval_seconds)

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
        commit = str(output.get("commit") or "").strip()
        evidence: list[str] = ["daedalus:solve"]
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
