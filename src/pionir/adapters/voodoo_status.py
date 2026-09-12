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

from pionir.contracts import AgentManifest, Capability, RiskLevel, Task, TaskResult
from pionir.errors import AdapterProtocolError, AdapterUnavailable


class TextCommandRunner(Protocol):
    def run(self, *, timeout_seconds: int) -> str: ...


def _shell(command: Sequence[str], *, cwd: str | None, timeout: int) -> dict[str, Any]:
    """Run a Voodoo action and return its outcome as data - a non-zero exit
    (a policy refusal, say) is captured, not raised."""
    try:
        proc = subprocess.run(
            list(command), capture_output=True, check=False, encoding="utf-8",
            errors="replace", shell=False, timeout=timeout, cwd=cwd,
        )
    except FileNotFoundError as error:
        raise AdapterUnavailable("Voodoo's configured executable was not found") from error
    except subprocess.TimeoutExpired as error:
        raise AdapterUnavailable("Voodoo action timed out") from error
    out = (proc.stdout or "").strip()
    try:
        parsed: Any = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        parsed = out[:4000]
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "output": parsed,
        "stderr": (proc.stderr or "").strip()[:1000] or None,
    }


@dataclass(frozen=True, slots=True)
class VoodooStatusSettings:
    command: tuple[str, ...]
    timeout_seconds: int = 20
    version: str = "voodoo/0.2"
    # `python -m voodoo status` resolves the voodoo package only from its src
    # tree (its editable install is not importable), so the status runs there.
    cwd: str | None = None
    # A run action is `run_prefix + [action] + args`; only allowlisted first
    # tokens are permitted, argv only (never a shell).
    run_prefix: tuple[str, ...] = ()
    run_actions: tuple[str, ...] = ()
    run_timeout_seconds: int = 300

    def __post_init__(self) -> None:
        if not self.command or any(not part for part in self.command):
            raise ValueError("Voodoo status command cannot be empty")
        if self.timeout_seconds < 1:
            raise ValueError("Voodoo status timeout must be positive")


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
        self._runner = runner or SubprocessTextRunner(settings.command, cwd=settings.cwd)
        capabilities = [
            Capability(
                name="security.voodoo_status",
                description="Read Voodoo's defensive posture: scopes, leases, VPN state",
                routing_hints=frozenset(
                    {"voodoo", "defense", "defensive", "posture", "scopes", "leases"}
                ),
                priority=100,
            ),
        ]
        if settings.run_prefix:
            capabilities.append(
                Capability(
                    name="security.voodoo_run",
                    description="Run one allowlisted Voodoo action (posture/scan/hunt/defend) - gated",
                    risk=RiskLevel.PRIVILEGED,
                    required_permissions=frozenset({"voodoo.run"}),
                    routing_hints=frozenset(
                        {"voodoo", "defend", "defense", "harden", "posture", "hunt", "drift"}
                    ),
                    priority=100,
                )
            )
        self._manifest = AgentManifest(
            agent_id="voodoo", version=settings.version, capabilities=tuple(capabilities)
        )

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def status(self) -> dict[str, Any]:
        raw = self._runner.run(timeout_seconds=self.settings.timeout_seconds).strip()
        if not raw:
            raise AdapterProtocolError("Voodoo returned an empty status")
        return _redact(raw)

    def run_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = str(payload.get("action", "")).strip()
        if action not in self.settings.run_actions:
            raise AdapterProtocolError(
                f"Voodoo action {action!r} is not allowed; permitted: {sorted(self.settings.run_actions)}"
            )
        raw_args = payload.get("args") or []
        if not isinstance(raw_args, list):
            raise AdapterProtocolError("Voodoo run args must be a list")
        args = [str(a) for a in raw_args]
        command = list(self.settings.run_prefix) + [action] + args
        result = _shell(command, cwd=self.settings.cwd, timeout=self.settings.run_timeout_seconds)
        result["action"] = " ".join([action, *args])
        return result

    def execute(self, task: Task) -> TaskResult:
        if task.capability == "security.voodoo_run":
            return TaskResult(task_id=task.task_id, agent_id="voodoo",
                              output=self.run_action(dict(task.payload)),
                              evidence=("voodoo:run",))
        if task.capability != "security.voodoo_status":
            raise AdapterProtocolError(f"unsupported Voodoo capability: {task.capability}")
        return TaskResult(
            task_id=task.task_id,
            agent_id=self.manifest.agent_id,
            output=self.status(),
            evidence=("voodoo:read-only-status",),
        )
