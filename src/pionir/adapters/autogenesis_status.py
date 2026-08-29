"""Read-only status adapter for the Autogenesis evolutionary organism."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from pionir.contracts import AgentManifest, Capability, Task, TaskResult
from pionir.errors import AdapterProtocolError, AdapterUnavailable


class StatusRunner(Protocol):
    def run(self, *, timeout_seconds: int) -> str: ...


@dataclass(frozen=True, slots=True)
class AutogenesisStatusSettings:
    command: tuple[str, ...]
    timeout_seconds: int = 15
    version: str = "0.1.0"

    def __post_init__(self) -> None:
        if not self.command or any(not part for part in self.command):
            raise ValueError("Autogenesis status command cannot be empty")
        if self.timeout_seconds < 1:
            raise ValueError("Autogenesis status timeout must be positive")


class SubprocessStatusRunner:
    def __init__(self, command: Sequence[str]) -> None:
        self._command = tuple(command)

    def run(self, *, timeout_seconds: int) -> str:
        try:
            process = subprocess.run(
                self._command,
                capture_output=True,
                check=False,
                encoding="utf-8",
                errors="replace",
                shell=False,
                timeout=timeout_seconds,
            )
        except FileNotFoundError as error:
            raise AdapterUnavailable(
                "Autogenesis's configured Python executable was not found"
            ) from error
        except subprocess.TimeoutExpired as error:
            raise AdapterUnavailable("Autogenesis status timed out") from error
        if process.returncode != 0:
            raise AdapterUnavailable(
                f"Autogenesis status exited with code {process.returncode}"
            )
        return process.stdout


class AutogenesisStatusAdapter:
    """Expose only Autogenesis's existing non-mutating status command."""

    def __init__(
        self,
        settings: AutogenesisStatusSettings,
        *,
        runner: StatusRunner | None = None,
    ) -> None:
        self.settings = settings
        self._runner = runner or SubprocessStatusRunner(settings.command)
        self._manifest = AgentManifest(
            agent_id="autogenesis",
            version=settings.version,
            capabilities=(
                Capability(
                    name="organism.autogenesis_status",
                    description="Read Autogenesis controls and recent ledger events",
                    routing_hints=frozenset({"autogenesis", "controls", "events"}),
                    priority=100,
                ),
            ),
        )

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def status(self) -> dict[str, str]:
        snapshot = self._runner.run(
            timeout_seconds=self.settings.timeout_seconds
        ).strip()
        if not snapshot:
            raise AdapterProtocolError("Autogenesis returned an empty status snapshot")
        return {"snapshot": snapshot}

    def execute(self, task: Task) -> TaskResult:
        if task.capability != "organism.autogenesis_status":
            raise AdapterProtocolError(
                f"unsupported Autogenesis capability: {task.capability}"
            )
        return TaskResult(
            task_id=task.task_id,
            agent_id=self.manifest.agent_id,
            output=self.status(),
            evidence=("autogenesis:read-only-status",),
        )
