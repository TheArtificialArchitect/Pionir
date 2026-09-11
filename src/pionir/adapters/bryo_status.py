"""Read-only observability adapter for Terrarium's Bryo organism."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from pionir.contracts import AgentManifest, Capability, Task, TaskResult
from pionir.errors import AdapterProtocolError, AdapterUnavailable


class TextCommandRunner(Protocol):
    def run(self, *, timeout_seconds: int) -> str: ...


@dataclass(frozen=True, slots=True)
class BryoStatusSettings:
    command: tuple[str, ...]
    timeout_seconds: int = 15
    version: str = "terrarium/build"
    # `python -m bryo.status` only resolves the bryo package from the terrarium
    # tree, and Bryo is not installed, so the status command runs there.
    cwd: str | None = None

    def __post_init__(self) -> None:
        if not self.command or any(not part for part in self.command):
            raise ValueError("Bryo status command cannot be empty")
        if self.timeout_seconds < 1:
            raise ValueError("Bryo status timeout must be positive")


class SubprocessTextRunner:
    def __init__(self, command: Sequence[str], *, cwd: str | None = None) -> None:
        self._command = tuple(command)
        self._cwd = cwd

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
                cwd=self._cwd,
            )
        except FileNotFoundError as error:
            raise AdapterUnavailable(
                "Bryo's configured Python executable was not found"
            ) from error
        except subprocess.TimeoutExpired as error:
            raise AdapterUnavailable("Bryo status timed out") from error
        if process.returncode != 0:
            raise AdapterUnavailable(f"Bryo status exited with code {process.returncode}")
        return process.stdout


class BryoStatusAdapter:
    """Expose only Bryo's intentionally read-only snapshot interface."""

    def __init__(
        self,
        settings: BryoStatusSettings,
        *,
        runner: TextCommandRunner | None = None,
    ) -> None:
        self.settings = settings
        self._runner = runner or SubprocessTextRunner(settings.command, cwd=settings.cwd)
        self._manifest = AgentManifest(
            agent_id="bryo",
            version=settings.version,
            capabilities=(
                Capability(
                    name="organism.bryo_status",
                    description="Read Bryo's current vitals and lineage snapshot",
                    routing_hints=frozenset(
                        {"bryo", "terrarium", "organism", "vitals", "alive"}
                    ),
                    priority=100,
                ),
            ),
        )

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def status(self) -> dict[str, str]:
        snapshot = self._runner.run(timeout_seconds=self.settings.timeout_seconds).strip()
        if not snapshot:
            raise AdapterProtocolError("Bryo returned an empty status snapshot")
        return {"snapshot": snapshot}

    def execute(self, task: Task) -> TaskResult:
        if task.capability != "organism.bryo_status":
            raise AdapterProtocolError(f"unsupported Bryo capability: {task.capability}")
        return TaskResult(
            task_id=task.task_id,
            agent_id=self.manifest.agent_id,
            output=self.status(),
            evidence=("bryo:read-only-status",),
        )
