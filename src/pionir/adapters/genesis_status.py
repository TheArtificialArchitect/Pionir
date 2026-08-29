"""Redacted observability adapter for the local Genesis agent."""

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
class GenesisStatusSettings:
    base_url: str = "http://127.0.0.1:8000"
    timeout_seconds: int = 15
    version: str = "genesis-agent/0.2.0"

    def __post_init__(self) -> None:
        validate_loopback_url(self.base_url, service="Genesis")
        if self.timeout_seconds < 1:
            raise ValueError("Genesis status timeout must be positive")


class GenesisStatusAdapter:
    """Expose health and counters while omitting journal and inner-monologue data."""

    def __init__(
        self,
        settings: GenesisStatusSettings,
        *,
        reader: JsonReader | None = None,
    ) -> None:
        self.settings = settings
        self._reader = reader or LoopbackJsonReader(
            settings.base_url,
            timeout_seconds=settings.timeout_seconds,
            service="Genesis",
        )
        self._manifest = AgentManifest(
            agent_id="genesis",
            version=settings.version,
            capabilities=(
                Capability(
                    name="organism.genesis_status",
                    description="Read Genesis health and life-loop counters",
                    routing_hints=frozenset({"genesis", "lifeloop", "counters", "mood"}),
                    priority=100,
                ),
            ),
        )

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def status(self) -> Mapping[str, Any]:
        health = self._reader.get("/api/health")
        state = self._reader.get("/api/state")
        subsystems = health.get("subsystems")
        subsystem_status: dict[str, Any] = {}
        if isinstance(subsystems, dict):
            for identifier, raw in subsystems.items():
                if isinstance(raw, dict):
                    subsystem_status[str(identifier)] = {
                        "name": raw.get("name"),
                        "ok": bool(raw.get("ok")),
                    }
        if not isinstance(health.get("status"), str):
            raise AdapterProtocolError("Genesis health response is malformed")
        if not isinstance(state.get("tick_count"), int):
            raise AdapterProtocolError("Genesis state response is malformed")
        emotions = state.get("emotions")
        return {
            "ok": health.get("status") == "Active",
            "status": health.get("status"),
            "booted": bool(health.get("booted")),
            "boot_error": bool(health.get("boot_error")),
            "model": health.get("model"),
            "subsystems": subsystem_status,
            "life_loop": {
                "running": bool(state.get("running")),
                "tick_count": state.get("tick_count"),
                "cognitive_errors": state.get("cognitive_errors"),
                "last_action": state.get("last_action"),
            },
            "emotions": dict(emotions) if isinstance(emotions, dict) else {},
        }

    def execute(self, task: Task) -> TaskResult:
        if task.capability != "organism.genesis_status":
            raise AdapterProtocolError(
                f"unsupported Genesis capability: {task.capability}"
            )
        return TaskResult(
            task_id=task.task_id,
            agent_id=self.manifest.agent_id,
            output=self.status(),
            evidence=(
                "genesis:loopback-read-only",
                "genesis:vault-and-monologue-redacted",
            ),
        )
