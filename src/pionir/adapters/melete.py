"""Adapter for Melete, Theo's tool-execution "hands", behind Pionir's gates.

Melete is given an intent, plans, and runs actual tools - file, git, shell,
web, code - on its tool-tuned brain, returning what it did. Theo's own
`execute_task` is just an HTTP proxy to this same server, so routing it through
Pionir does not go around Theo; it reaches the same seam Theo does, now under
Pionir's permission gate and audit ledger.

PRIVILEGED, and meant it: Melete runs shell and touches the filesystem. It
needs an explicit permission and the dashboard runs it behind a confirm.

Theo is not in the loop: this calls Melete's own HTTP server directly.
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
from pionir.scheduler import kv_cache_vram_mb

MAX_INTENT_CHARS = 8_000


@dataclass(frozen=True, slots=True)
class MeleteSettings(LoopbackHttpSettings):
    base_url: str = "http://127.0.0.1:8770"
    # Running a chain of real tools is longer than a chat turn but shorter than a
    # full coding job.
    timeout_seconds: int = 300
    # Observability and the VRAM discount only; resolved live from /health. Melete
    # shares qwen2.5:7b-instruct with Daedalus and Atani on the one daemon.
    model_id: str = "qwen2.5:7b-instruct"


class MeleteAdapter:
    """Melete as a gated, audited Pionir tool-execution specialist."""

    def __init__(
        self,
        settings: MeleteSettings | None = None,
        *,
        client: LoopbackJsonClient | None = None,
    ) -> None:
        self.settings = settings or MeleteSettings()
        self._client = client or LoopbackJsonClient("Melete", self.settings)
        self._manifest = AgentManifest(
            agent_id="melete",
            version="tech-support/melete",
            capabilities=(
                Capability(
                    name="tools.melete_invoke",
                    description="Melete running real tools - file, git, shell, web - to carry out an intent",
                    risk=RiskLevel.PRIVILEGED,
                    required_permissions=frozenset({"melete.invoke"}),
                    model=ModelRequirement(
                        model_id=self.settings.model_id,
                        # On-demand qwen 7b in Melete's own daemon; declare the
                        # real load so Pionir will not invoke it when the card
                        # cannot fit qwen. Residency discount applies when hot.
                        estimated_vram_mb=4_500,
                        context_vram_mb=kv_cache_vram_mb(8_192),
                    ),
                    routing_hints=frozenset(
                        {"melete", "tool", "tools", "invoke", "command", "shell"}
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
            raise AdapterProtocolError("Melete's health response is malformed")
        return document

    def resolve_model(self) -> str | None:
        return health_model(self._client)

    def execute(self, task: Task) -> TaskResult:
        intent = str(task.payload.get("intent") or task.payload.get("content") or "").strip()
        if not intent:
            raise AdapterProtocolError("Melete needs an intent")
        if len(intent) > MAX_INTENT_CHARS:
            raise AdapterProtocolError(
                f"intent exceeds Melete's {MAX_INTENT_CHARS}-character limit"
            )
        request: dict[str, Any] = {"intent": intent}
        if task.payload.get("context") is not None:
            request["context"] = task.payload["context"]

        document = self._client.post("/invoke", request)
        if not isinstance(document.get("ok"), bool):
            raise AdapterProtocolError("Melete returned no ok verdict")
        # ok=false with an error is a real tool outcome, not an adapter failure.
        return TaskResult(
            task_id=task.task_id,
            agent_id=self.manifest.agent_id,
            output=document,
            evidence=("melete:invoke",),
        )
