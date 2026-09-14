"""Read-only observability adapter for Voodoo, the defensive companion.

Voodoo watches the machine from the inside - integrity baselines, secret
scanning, IOC hunting, scope and lease control. Its **status** is exposed
through Pionir: what scopes and leases exist and whether a VPN is up. When a
run prefix is configured, one allowlisted action is taskable too, PRIVILEGED
and gated: `scan`, `headers`, `cert`, `vpn`, or an explicit two-token
`defend <posture|baseline|drift|secrets|triage|hunt>` (bare `defend` is not
a command - the CLI errors on it - so it is never an allowlist entry). Lease
grants and scope edits stay Voodoo's own, behind its operator approval.
Arguments are checked by shape (see ``_actions``) before they become argv.

`voodoo status` prints a JSON object to stdout: `{scopes, active_leases,
proton_vpn}`. Lease and scope records can name a client or carry a ticket
reason, so this adapter passes back a redacted summary - counts and names, not
the reasons.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from pionir.adapters._actions import (
    VOODOO_ACTION_SHAPES,
    normalise_action,
    run_action,
    validate_action_args,
)
from pionir.adapters._proc import run_process, unavailable
from pionir.contracts import AgentManifest, Capability, RiskLevel, Task, TaskResult
from pionir.errors import AdapterProtocolError


class TextCommandRunner(Protocol):
    def run(self, *, timeout_seconds: int) -> str: ...


@dataclass(frozen=True, slots=True)
class VoodooStatusSettings:
    command: tuple[str, ...]
    timeout_seconds: int = 20
    version: str = "voodoo/0.2"
    # `python -m voodoo status` resolves the voodoo package only from its src
    # tree (its editable install is not importable), so the status runs there.
    cwd: str | None = None
    # A run action is `run_prefix + action tokens + args`; only allowlisted
    # actions (one token, or two for `defend <sub>`) are permitted, argv only
    # (never a shell), and the args are shape-checked.
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
        process = run_process(
            self._command,
            label="Voodoo status",
            timeout_seconds=timeout_seconds,
            cwd=self._cwd,
        )
        if process.returncode != 0:
            raise unavailable("Voodoo status", process)
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
                    description="Run one allowlisted Voodoo action (scan/headers/cert/vpn/defend <sub>) - gated",
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
        action = normalise_action(payload.get("action"))
        allowed = {normalise_action(item) for item in self.settings.run_actions}
        if not action or action not in allowed:
            raise AdapterProtocolError(
                f"Voodoo action {action!r} is not allowed; permitted: {sorted(allowed)}"
            )
        raw_args = payload.get("args") or []
        if not isinstance(raw_args, list):
            raise AdapterProtocolError("Voodoo run args must be a list")
        args = validate_action_args(
            "Voodoo", action, [str(a) for a in raw_args], VOODOO_ACTION_SHAPES
        )
        command = [*self.settings.run_prefix, *action.split(), *args]
        result = run_action(
            "Voodoo",
            command,
            cwd=self.settings.cwd,
            timeout_seconds=self.settings.run_timeout_seconds,
        )
        # The full argv, so the ledger and the approval summary show exactly what
        # ran - not just the verb.
        result["action"] = " ".join([*action.split(), *args])
        result["argv"] = command
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
