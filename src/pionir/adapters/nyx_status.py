"""Read-only observability adapter for Nyx, the offensive-security companion.

Nyx is a fork of Atani wired to real offensive tooling behind a kill switch that
is off until deliberately enabled. Only her **status** is exposed through Pionir -
a redacted health read plus, crucially, whether the offensive toolkit is armed.
Nothing that acts is reachable here: no recon, no exploit, no toggling the
switch. Those stay Nyx's own, behind her kill switch and operator approval, until
an authorization boundary through Pionir's gates exists.

`nyx status` prints Nyx's Atani-shaped status JSON. This adapter passes back a
redacted summary - identity, ledger integrity, pending counts - never memory,
goals, or action detail.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from pionir.contracts import AgentManifest, Capability, Task, TaskResult
from pionir.errors import AdapterProtocolError, AdapterUnavailable


class TextCommandRunner(Protocol):
    def run(self, *, timeout_seconds: int) -> str: ...


@dataclass(frozen=True, slots=True)
class NyxStatusSettings:
    command: tuple[str, ...]
    timeout_seconds: int = 20
    version: str = "nyx/0.1"

    def __post_init__(self) -> None:
        if not self.command or any(not part for part in self.command):
            raise ValueError("Nyx status command cannot be empty")
        if self.timeout_seconds < 1:
            raise ValueError("Nyx status timeout must be positive")


class SubprocessTextRunner:
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
            raise AdapterUnavailable("Nyx's configured executable was not found") from error
        except subprocess.TimeoutExpired as error:
            raise AdapterUnavailable("Nyx status timed out") from error
        if process.returncode != 0:
            raise AdapterUnavailable(f"Nyx status exited with code {process.returncode}")
        return process.stdout


def _redact(raw: str) -> dict[str, Any]:
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as error:
        raise AdapterProtocolError("Nyx status was not valid JSON") from error
    if not isinstance(document, dict):
        raise AdapterProtocolError("Nyx status was not a JSON object")
    ledger = document.get("ledger") if isinstance(document.get("ledger"), dict) else {}
    pending = document.get("pending") if isinstance(document.get("pending"), dict) else {}
    return {
        "name": document.get("name"),
        "version": document.get("version"),
        "ledger_events": ledger.get("events"),
        "ledger_integrity": ledger.get("integrity"),
        "pending_approvals": pending.get("approvals"),
    }


class NyxStatusAdapter:
    """Expose only Nyx's read-only status surface, redacted."""

    def __init__(self, settings: NyxStatusSettings, *, runner: TextCommandRunner | None = None) -> None:
        self.settings = settings
        self._runner = runner or SubprocessTextRunner(settings.command)
        self._manifest = AgentManifest(
            agent_id="nyx",
            version=settings.version,
            capabilities=(
                Capability(
                    name="security.nyx_status",
                    description="Read Nyx's offensive-security health: identity, ledger, pending",
                    routing_hints=frozenset(
                        {"nyx", "offense", "offensive", "recon", "pentest", "security"}
                    ),
                    priority=100,
                ),
            ),
        )

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def status(self) -> dict[str, Any]:
        raw = self._runner.run(timeout_seconds=self.settings.timeout_seconds).strip()
        if not raw:
            raise AdapterProtocolError("Nyx returned an empty status")
        return _redact(raw)

    def execute(self, task: Task) -> TaskResult:
        if task.capability != "security.nyx_status":
            raise AdapterProtocolError(f"unsupported Nyx capability: {task.capability}")
        return TaskResult(
            task_id=task.task_id,
            agent_id=self.manifest.agent_id,
            output=self.status(),
            evidence=("nyx:read-only-status",),
        )
