"""Nyx, the offensive-security companion, as a Pionir organ.

Two capabilities on one agent:
  - security.nyx_status  (read-only) - a redacted health read: identity, ledger
    integrity, pending counts, whether the offensive toolkit is armed.
  - security.nyx_run     (PRIVILEGED) - run ONE allowlisted Nyx action (recon,
    a fingerprint, a bounded research fetch). It is privileged and never granted
    by default, so it lands in Pionir's approval queue and does not fire until
    Ian says yes. Nyx's own gates still apply underneath (the offensive kill
    switch, Tor/ProtonVPN) - two layers, not one.

The action is one of a fixed allowlist and its arguments are passed as argv to
`nyx`, never through a shell, so nothing in a request can inject a command.
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


@dataclass(frozen=True, slots=True)
class NyxStatusSettings:
    command: tuple[str, ...]
    timeout_seconds: int = 20
    version: str = "nyx/0.1"
    # The prefix a run action is appended to (e.g. ("nyx",) -> `nyx scan HOST`),
    # the allowlist of first tokens it may use, and how long an action may take.
    run_prefix: tuple[str, ...] = ()
    run_actions: tuple[str, ...] = ()
    run_timeout_seconds: int = 300
    cwd: str | None = None

    def __post_init__(self) -> None:
        if not self.command or any(not part for part in self.command):
            raise ValueError("Nyx status command cannot be empty")
        if self.timeout_seconds < 1:
            raise ValueError("Nyx status timeout must be positive")


class SubprocessTextRunner:
    def __init__(self, command: Sequence[str], *, cwd: str | None = None) -> None:
        self._command = tuple(command)
        self._cwd = cwd

    def run(self, *, timeout_seconds: int) -> str:
        try:
            process = subprocess.run(
                self._command, capture_output=True, check=False, encoding="utf-8",
                errors="replace", shell=False, timeout=timeout_seconds, cwd=self._cwd,
            )
        except FileNotFoundError as error:
            raise AdapterUnavailable("Nyx's configured executable was not found") from error
        except subprocess.TimeoutExpired as error:
            raise AdapterUnavailable("Nyx status timed out") from error
        if process.returncode != 0:
            raise AdapterUnavailable(f"Nyx status exited with code {process.returncode}")
        return process.stdout


def _shell(command: Sequence[str], *, cwd: str | None, timeout: int) -> dict[str, Any]:
    """Run a bot action and return its outcome as data - a non-zero exit is a
    real answer (e.g. 'offensive disabled'), captured, not raised."""
    try:
        proc = subprocess.run(
            list(command), capture_output=True, check=False, encoding="utf-8",
            errors="replace", shell=False, timeout=timeout, cwd=cwd,
        )
    except FileNotFoundError as error:
        raise AdapterUnavailable("Nyx's configured executable was not found") from error
    except subprocess.TimeoutExpired as error:
        raise AdapterUnavailable("Nyx action timed out") from error
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
    """Nyx's read-only status, plus (when run is configured) a gated run action."""

    def __init__(self, settings: NyxStatusSettings, *, runner: TextCommandRunner | None = None) -> None:
        self.settings = settings
        self._runner = runner or SubprocessTextRunner(settings.command, cwd=settings.cwd)
        capabilities = [
            Capability(
                name="security.nyx_status",
                description="Read Nyx's offensive-security health: identity, ledger, pending",
                routing_hints=frozenset({"nyx", "offense", "offensive", "recon", "pentest", "security"}),
                priority=100,
            ),
        ]
        if settings.run_prefix:
            capabilities.append(
                Capability(
                    name="security.nyx_run",
                    description="Run one allowlisted Nyx action (recon/fingerprint/research) - gated",
                    risk=RiskLevel.PRIVILEGED,
                    required_permissions=frozenset({"nyx.run"}),
                    routing_hints=frozenset({"nyx", "scan", "recon", "fingerprint", "offensive", "pentest"}),
                    priority=100,
                )
            )
        self._manifest = AgentManifest(
            agent_id="nyx", version=settings.version, capabilities=tuple(capabilities)
        )

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def status(self) -> dict[str, Any]:
        raw = self._runner.run(timeout_seconds=self.settings.timeout_seconds).strip()
        if not raw:
            raise AdapterProtocolError("Nyx returned an empty status")
        return _redact(raw)

    def run_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = str(payload.get("action", "")).strip()
        if action not in self.settings.run_actions:
            raise AdapterProtocolError(
                f"Nyx action {action!r} is not allowed; permitted: {sorted(self.settings.run_actions)}"
            )
        raw_args = payload.get("args") or []
        if not isinstance(raw_args, list):
            raise AdapterProtocolError("Nyx run args must be a list")
        args = [str(a) for a in raw_args]
        command = list(self.settings.run_prefix) + [action] + args
        result = _shell(command, cwd=self.settings.cwd, timeout=self.settings.run_timeout_seconds)
        result["action"] = " ".join([action, *args])
        return result

    def execute(self, task: Task) -> TaskResult:
        if task.capability == "security.nyx_status":
            return TaskResult(task_id=task.task_id, agent_id="nyx",
                              output=self.status(), evidence=("nyx:read-only-status",))
        if task.capability == "security.nyx_run":
            return TaskResult(task_id=task.task_id, agent_id="nyx",
                              output=self.run_action(dict(task.payload)),
                              evidence=("nyx:run",))
        raise AdapterProtocolError(f"unsupported Nyx capability: {task.capability}")
