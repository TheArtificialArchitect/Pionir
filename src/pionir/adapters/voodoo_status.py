"""Read-only observability adapter for Voodoo, the defensive companion.

Voodoo watches the machine from the inside - integrity baselines, secret
scanning, IOC hunting, scope and lease control. Only its **status** is exposed
through Pionir: what scopes and leases exist and whether a VPN is up. None of the
acting surface - scanning, baselining, granting a lease, connecting a VPN - is
reachable here; those stay Voodoo's own, behind its operator approval, until an
authorization boundary through Pionir's gates exists (the mirror of why Theo's
tool path was kept out).

`voodoo status` prints a JSON object to stdout: `{scopes, active_leases,
proton_vpn}`. Lease and scope records can name a client or carry a ticket
reason, so this adapter passes back a redacted summary - counts and names, not
the reasons.
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
class VoodooStatusSettings:
    command: tuple[str, ...]
    timeout_seconds: int = 20
    version: str = "voodoo/0.2"

    def __post_init__(self) -> None:
        if not self.command or any(not part for part in self.command):
            raise ValueError("Voodoo status command cannot be empty")
        if self.timeout_seconds < 1:
            raise ValueError("Voodoo status timeout must be positive")


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
            raise AdapterUnavailable("Voodoo's configured executable was not found") from error
        except subprocess.TimeoutExpired as error:
            raise AdapterUnavailable("Voodoo status timed out") from error
        if process.returncode != 0:
            raise AdapterUnavailable(f"Voodoo status exited with code {process.returncode}")
        return process.stdout


def _redact(raw: str) -> dict[str, Any]:
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as error:
        raise AdapterProtocolError("Voodoo status was not valid JSON") from error
    if not isinstance(document, dict):
        raise AdapterProtocolError("Voodoo status was not a JSON object")
    scopes = document.get("scopes") if isinstance(document.get("scopes"), list) else []
    leases = document.get("active_leases") if isinstance(document.get("active_leases"), list) else []
    vpn = document.get("proton_vpn") if isinstance(document.get("proton_vpn"), dict) else {}
    return {
        "scopes": [s.get("name") for s in scopes if isinstance(s, dict) and s.get("name")],
        "active_leases": len(leases),
        "vpn_connected": bool(vpn.get("connected")),
    }


class VoodooStatusAdapter:
    """Expose only Voodoo's read-only status surface, redacted."""

    def __init__(self, settings: VoodooStatusSettings, *, runner: TextCommandRunner | None = None) -> None:
        self.settings = settings
        self._runner = runner or SubprocessTextRunner(settings.command)
        self._manifest = AgentManifest(
            agent_id="voodoo",
            version=settings.version,
            capabilities=(
                Capability(
                    name="security.voodoo_status",
                    description="Read Voodoo's defensive posture: scopes, leases, VPN state",
                    routing_hints=frozenset(
                        {"voodoo", "defense", "defensive", "posture", "scopes", "leases"}
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
            raise AdapterProtocolError("Voodoo returned an empty status")
        return _redact(raw)

    def execute(self, task: Task) -> TaskResult:
        if task.capability != "security.voodoo_status":
            raise AdapterProtocolError(f"unsupported Voodoo capability: {task.capability}")
        return TaskResult(
            task_id=task.task_id,
            agent_id=self.manifest.agent_id,
            output=self.status(),
            evidence=("voodoo:read-only-status",),
        )
