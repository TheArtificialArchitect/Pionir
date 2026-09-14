"""Nyx, the offensive-security companion, as a Pionir organ.

Two capabilities on one agent:
  - security.nyx_status  (read-only) - a redacted health read: identity, ledger
    integrity, pending counts, whether the offensive toolkit is armed.
  - security.nyx_run     (PRIVILEGED) - run ONE allowlisted Nyx action (recon,
    a fingerprint, a bounded research fetch). It is privileged and never granted
    by default, so it lands in Pionir's approval queue and does not fire until
    Ian says yes. Nyx's own gates still apply underneath (the offensive kill
    switch, Tor/ProtonVPN) - two layers, not one.

The action is one of a fixed allowlist of Nyx's real top-level subcommands
(research, crawl, fingerprint, cert - `scan` lives under `nyx improve` and
`specialists` needs a sub-subcommand, so neither belongs here), and its
arguments are checked by shape (see ``_actions``) before being passed as argv
to `nyx`, never through a shell: no flag the operator did not allow, no
positional that is not the URL or host the subcommand takes.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from pionir.adapters._actions import (
    NYX_ACTION_SHAPES,
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
class NyxStatusSettings:
    command: tuple[str, ...]
    timeout_seconds: int = 20
    version: str = "nyx/0.1"
    # The prefix a run action is appended to (e.g. ("nyx",) -> `nyx cert HOST`),
    # the allowlist of actions it may use, and how long an action may take.
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
        process = run_process(
            self._command, label="Nyx status", timeout_seconds=timeout_seconds, cwd=self._cwd
        )
        if process.returncode != 0:
            raise unavailable("Nyx status", process)
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
        action = normalise_action(payload.get("action"))
        allowed = {normalise_action(item) for item in self.settings.run_actions}
        if not action or action not in allowed:
            raise AdapterProtocolError(
                f"Nyx action {action!r} is not allowed; permitted: {sorted(allowed)}"
            )
        raw_args = payload.get("args") or []
        if not isinstance(raw_args, list):
            raise AdapterProtocolError("Nyx run args must be a list")
        args = validate_action_args("Nyx", action, [str(a) for a in raw_args], NYX_ACTION_SHAPES)
        command = [*self.settings.run_prefix, *action.split(), *args]
        result = run_action(
            "Nyx", command, cwd=self.settings.cwd, timeout_seconds=self.settings.run_timeout_seconds
        )
        # The full argv, so the ledger and the approval summary show exactly what
        # ran - not just the verb.
        result["action"] = " ".join([*action.split(), *args])
        result["argv"] = command
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
