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
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pionir.adapters._http import (
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
from pionir.errors import AdapterProtocolError

MAX_INTENT_CHARS = 8_000


@dataclass(frozen=True, slots=True)
class DaedalusSettings(LoopbackHttpSettings):
    base_url: str = "http://127.0.0.1:8771"
    # A real coding job - worktree, edits, a test run, repairs - is minutes, not
    # seconds. Generous against that tail rather than tuned to a median.
    timeout_seconds: int = 600
    # Observability only; resolved live from /health at boot. Daedalus runs
    # qwen3-coder:30b - a code-specialist MoE that Ollama runs on the CPU at ~0
    # VRAM, so it takes no GPU lease and never competes with the voice for the
    # card (see the capability's ModelRequirement below).
    model_id: str = "qwen3-coder:30b"


class DaedalusAdapter:
    """Daedalus as a gated, audited Pionir coding specialist."""

    def __init__(
        self,
        settings: DaedalusSettings | None = None,
        *,
        client: LoopbackJsonClient | None = None,
    ) -> None:
        self.settings = settings or DaedalusSettings()
        self._client = client or LoopbackJsonClient("Daedalus", self.settings)
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
                        # qwen3-coder:30b is a mixture-of-experts (~3B active) that
                        # Ollama runs on the CPU on this 12GB card - the same
                        # elastic behaviour as the nemotron depth tier. The zeros
                        # are a statement about leases, not a claim of no memory:
                        # it needs no GPU lease because it never fails to run for
                        # want of the card, and declaring it a GPU tenant would
                        # take the single lease it does not need and make it
                        # unadmittable whenever the voice holds the card. Admission
                        # reads free VRAM at lease time, so whatever it does take is
                        # already priced in.
                        estimated_vram_mb=0,
                        context_vram_mb=0,
                        requires_gpu=False,
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

        document = self._client.post("/solve", request)
        if not isinstance(document.get("ok"), bool):
            raise AdapterProtocolError("Daedalus returned no ok verdict")
        # A refused or failed solve is a real outcome (ok=false with a gate/error),
        # not an adapter failure - return it so the ledger and the caller see the
        # verdict rather than a bare "unavailable".
        commit = str(document.get("commit") or "").strip()
        evidence: tuple[str, ...] = ("daedalus:solve",)
        if commit:
            evidence = ("daedalus:solve", f"daedalus:commit:{commit}")
        return TaskResult(
            task_id=task.task_id,
            agent_id=self.manifest.agent_id,
            output=document,
            evidence=evidence,
        )
