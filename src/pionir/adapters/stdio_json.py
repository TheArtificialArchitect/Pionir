"""Versioned JSON-over-stdio protocol for independently packaged specialists."""

from __future__ import annotations

import json
import subprocess
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pionir.contracts import (
    AgentManifest,
    Capability,
    MemoryNamespace,
    ModelRequirement,
    RiskLevel,
    Task,
    TaskResult,
)
from pionir.errors import AdapterProtocolError, AdapterUnavailable

TASK_PROTOCOL = "pionir.task.v1"
RESULT_PROTOCOL = "pionir.result.v1"
HEALTH_PROTOCOL = "pionir.health.v1"
MAX_DOCUMENT_CHARS = 2_000_000


class DocumentTransport(Protocol):
    def exchange(
        self,
        document: Mapping[str, Any],
        *,
        timeout_seconds: int,
    ) -> Mapping[str, Any]: ...


class SubprocessDocumentTransport:
    def __init__(self, command: Sequence[str]) -> None:
        self._command = tuple(command)

    def exchange(
        self,
        document: Mapping[str, Any],
        *,
        timeout_seconds: int,
    ) -> Mapping[str, Any]:
        try:
            request = json.dumps(document, ensure_ascii=False)
        except (TypeError, ValueError) as error:
            raise AdapterProtocolError("specialist task is not JSON-serializable") from error
        if len(request) > MAX_DOCUMENT_CHARS:
            raise AdapterProtocolError("specialist task exceeds the protocol size limit")
        try:
            process = subprocess.run(
                self._command,
                input=request,
                capture_output=True,
                check=False,
                encoding="utf-8",
                errors="replace",
                shell=False,
                timeout=timeout_seconds,
            )
        except FileNotFoundError as error:
            raise AdapterUnavailable("specialist executable was not found") from error
        except subprocess.TimeoutExpired as error:
            raise AdapterUnavailable("specialist task timed out") from error
        if process.returncode != 0:
            raise AdapterUnavailable(
                f"specialist exited with status {process.returncode}"
            )
        if len(process.stdout) > MAX_DOCUMENT_CHARS:
            raise AdapterProtocolError("specialist response exceeds the protocol size limit")
        try:
            response = json.loads(process.stdout)
        except json.JSONDecodeError as error:
            raise AdapterProtocolError("specialist returned invalid JSON") from error
        if not isinstance(response, dict):
            raise AdapterProtocolError("specialist returned a non-object response")
        return response


@dataclass(frozen=True, slots=True)
class StdioJsonSettings:
    command: tuple[str, ...]
    manifest: AgentManifest
    timeout_seconds: int = 120

    def __post_init__(self) -> None:
        if not self.command or any(not part for part in self.command):
            raise ValueError("specialist command cannot be empty")
        if self.timeout_seconds < 1:
            raise ValueError("specialist timeout must be positive")


class StdioJsonAdapter:
    def __init__(
        self,
        settings: StdioJsonSettings,
        *,
        transport: DocumentTransport | None = None,
    ) -> None:
        self.settings = settings
        self._transport = transport or SubprocessDocumentTransport(settings.command)

    @property
    def manifest(self) -> AgentManifest:
        return self.settings.manifest

    def status(self) -> Mapping[str, Any]:
        response = self._transport.exchange(
            {
                "protocol": HEALTH_PROTOCOL,
                "agent_id": self.manifest.agent_id,
            },
            timeout_seconds=self.settings.timeout_seconds,
        )
        if response.get("protocol") != HEALTH_PROTOCOL:
            raise AdapterProtocolError("specialist health protocol version mismatch")
        if response.get("agent_id") != self.manifest.agent_id:
            raise AdapterProtocolError("specialist health response has the wrong agent id")
        if response.get("ok") is not True:
            raise AdapterUnavailable("specialist reports unhealthy")
        return response

    def execute(self, task: Task) -> TaskResult:
        if task.capability not in {
            capability.name for capability in self.manifest.capabilities
        }:
            raise AdapterProtocolError(
                f"unsupported {self.manifest.agent_id} capability: {task.capability}"
            )
        response = self._transport.exchange(
            {
                "protocol": TASK_PROTOCOL,
                "task_id": str(task.task_id),
                "agent_id": self.manifest.agent_id,
                "capability": task.capability,
                "payload": dict(task.payload),
            },
            timeout_seconds=self.settings.timeout_seconds,
        )
        if response.get("protocol") != RESULT_PROTOCOL:
            raise AdapterProtocolError("specialist result protocol version mismatch")
        if response.get("task_id") != str(task.task_id):
            raise AdapterProtocolError("specialist result belongs to a different task")
        if response.get("agent_id") != self.manifest.agent_id:
            raise AdapterProtocolError("specialist result has the wrong agent id")
        output = response.get("output")
        evidence = response.get("evidence", [])
        if not isinstance(output, dict):
            raise AdapterProtocolError("specialist result output must be an object")
        if not isinstance(evidence, list) or not all(
            isinstance(item, str) for item in evidence
        ):
            raise AdapterProtocolError("specialist evidence must be a string list")
        return TaskResult(
            task_id=task.task_id,
            agent_id=self.manifest.agent_id,
            output=output,
            evidence=tuple(evidence),
        )


def _capability(document: Mapping[str, Any]) -> Capability:
    model_document = document.get("model")
    model = None
    if model_document is not None:
        if not isinstance(model_document, dict):
            raise ValueError("specialist capability model must be a table")
        model = ModelRequirement(
            model_id=str(model_document["id"]),
            estimated_vram_mb=int(model_document.get("estimated_vram_mb", 0)),
            context_vram_mb=int(model_document.get("context_vram_mb", 0)),
            requires_gpu=bool(model_document.get("requires_gpu", True)),
        )
    permissions = document.get("required_permissions", [])
    if not isinstance(permissions, list) or not all(
        isinstance(item, str) and item for item in permissions
    ):
        raise ValueError("required_permissions must be a string list")
    hints = document.get("routing_hints", [])
    if not isinstance(hints, list) or not all(
        isinstance(item, str) and item for item in hints
    ):
        raise ValueError("routing_hints must be a string list")
    return Capability(
        name=str(document["name"]),
        description=str(document["description"]),
        risk=RiskLevel(str(document.get("risk", RiskLevel.READ_ONLY.value))),
        required_permissions=frozenset(permissions),
        model=model,
        # An operator-declared specialist becomes routable by saying what it is
        # called, without an edit to the router.
        routing_hints=frozenset(hint.lower() for hint in hints),
        priority=int(document.get("priority", 0)),
    )


def load_stdio_adapters(path: Path) -> tuple[StdioJsonAdapter, ...]:
    """Load operator-authored specialist manifests from one explicit TOML file."""

    if not path.is_absolute():
        raise ValueError("PIONIR_SPECIALISTS_FILE must be an absolute path")
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"cannot read specialist configuration: {path}") from error
    entries = document.get("specialists")
    if not isinstance(entries, list):
        raise ValueError("specialist configuration needs [[specialists]] entries")
    adapters: list[StdioJsonAdapter] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("each specialist entry must be a table")
        capabilities = entry.get("capabilities")
        command = entry.get("command")
        memory_access = entry.get("memory_access", [])
        if (
            not isinstance(capabilities, list)
            or not capabilities
            or not all(isinstance(item, dict) for item in capabilities)
        ):
            raise ValueError("each specialist needs at least one capability")
        if not isinstance(command, list) or not command or not all(
            isinstance(item, str) and item for item in command
        ):
            raise ValueError("each specialist command must be a string list")
        if not isinstance(memory_access, list) or not all(
            isinstance(item, str) and item for item in memory_access
        ):
            raise ValueError("specialist memory_access must be a string list")
        manifest = AgentManifest(
            agent_id=str(entry["agent_id"]),
            version=str(entry["version"]),
            capabilities=tuple(_capability(item) for item in capabilities),
            memory_access=frozenset(MemoryNamespace(item) for item in memory_access),
        )
        adapters.append(
            StdioJsonAdapter(
                StdioJsonSettings(
                    command=tuple(command),
                    manifest=manifest,
                    timeout_seconds=int(entry.get("timeout_seconds", 120)),
                )
            )
        )
    return tuple(adapters)
