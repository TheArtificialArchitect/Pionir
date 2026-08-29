"""Privacy-preserving status adapter for the Probability organism."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from pionir.contracts import AgentManifest, Capability, Task, TaskResult
from pionir.errors import AdapterProtocolError

from .loopback_json import LoopbackJsonReader, validate_loopback_url


class JsonReader(Protocol):
    def get(self, path: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ProbabilityStatusSettings:
    base_url: str = "http://127.0.0.1:8791"
    timeout_seconds: int = 10
    version: str = "probability-ai/0.1.0"

    def __post_init__(self) -> None:
        validate_loopback_url(self.base_url, service="Probability")
        if self.timeout_seconds < 1:
            raise ValueError("Probability status timeout must be positive")


class ProbabilityStatusAdapter:
    """Return operational fields, never memories, events, goals, or experiments."""

    def __init__(
        self,
        settings: ProbabilityStatusSettings,
        *,
        reader: JsonReader | None = None,
    ) -> None:
        self.settings = settings
        self._reader = reader or LoopbackJsonReader(
            settings.base_url,
            timeout_seconds=settings.timeout_seconds,
            service="Probability",
        )
        self._manifest = AgentManifest(
            agent_id="probability",
            version=settings.version,
            capabilities=(
                Capability(
                    name="organism.probability_status",
                    description="Read Probability's operational state without private memory",
                    routing_hints=frozenset({"probability", "operational"}),
                    priority=100,
                ),
            ),
        )

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def status(self) -> Mapping[str, Any]:
        document = self._reader.get("/api/state")
        if document.get("ok") is not True or document.get("name") != "Probability":
            raise AdapterProtocolError("Probability's state response is malformed")

        mentor = document.get("mentor")
        discord = document.get("discord")
        resources = document.get("resources")
        self_model = document.get("self_model")
        return {
            "ok": True,
            "name": "Probability",
            "mode": document.get("mode"),
            "age": document.get("age"),
            "tick": document.get("tick"),
            "busy": bool(document.get("busy")),
            "auto_evolve": bool(document.get("auto_evolve")),
            "mentor": dict(mentor) if isinstance(mentor, dict) else {},
            "discord": {
                key: bool(discord.get(key))
                for key in ("configured", "controls", "notifications")
            }
            if isinstance(discord, dict)
            else {},
            "resources": dict(resources) if isinstance(resources, dict) else {},
            "self_model": dict(self_model) if isinstance(self_model, dict) else {},
            "last_evolution": document.get("last_evolution"),
            "gene_count": document.get("gene_count"),
        }

    def execute(self, task: Task) -> TaskResult:
        if task.capability != "organism.probability_status":
            raise AdapterProtocolError(
                f"unsupported Probability capability: {task.capability}"
            )
        return TaskResult(
            task_id=task.task_id,
            agent_id=self.manifest.agent_id,
            output=self.status(),
            evidence=(
                "probability:loopback-read-only",
                "probability:private-state-redacted",
            ),
        )
