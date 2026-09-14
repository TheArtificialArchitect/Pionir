"""Read-only observability adapter for Terrarium's Bryo organism.

`python -m bryo.status` has two faces: the default prose snapshot (a box of
vitals, lineage and the last journal lines) and `--json`, a machine-readable
`bryo.vitals/1` document that never raises and reads neutral when he is absent.
The JSON is preferred - a consumer can act on `pressure` and `advisory` without
scraping a box-drawing table - and the prose is the fallback when the JSON
face is missing or unparsable (an older terrarium tree, say).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from pionir.adapters._proc import run_process, unavailable
from pionir.contracts import AgentManifest, Capability, Task, TaskResult
from pionir.errors import AdapterError, AdapterProtocolError

JSON_FLAG = "--json"


class TextCommandRunner(Protocol):
    # `arguments` are appended to the configured command for one call (the
    # adapter uses it for `--json`). Keyword-only with a default so a runner
    # built for the bare command - bryo_pressure's - keeps its call shape.
    def run(self, *, timeout_seconds: int, arguments: Sequence[str] = ()) -> str: ...


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

    def run(self, *, timeout_seconds: int, arguments: Sequence[str] = ()) -> str:
        process = run_process(
            [*self._command, *arguments],
            label="Bryo status",
            timeout_seconds=timeout_seconds,
            cwd=self._cwd,
        )
        if process.returncode != 0:
            raise unavailable("Bryo status", process)
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

    def _vitals(self) -> dict[str, Any] | None:
        """His `--json` vitals, or None when that face is absent or unreadable.

        Fail-open on purpose: a terrarium tree without `--json` prints the prose
        snapshot and exits 0, an older one may reject the flag, and either is a
        reason to fall back to the prose, not to report him unreachable.
        """

        try:
            raw = self._runner.run(
                timeout_seconds=self.settings.timeout_seconds, arguments=(JSON_FLAG,)
            )
        except AdapterError:
            return None
        try:
            document = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
        return document if isinstance(document, dict) else None

    def status(self) -> dict[str, Any]:
        vitals = self._vitals()
        if vitals is not None:
            return {"format": "json", "vitals": vitals}
        snapshot = self._runner.run(timeout_seconds=self.settings.timeout_seconds).strip()
        if not snapshot:
            raise AdapterProtocolError("Bryo returned an empty status snapshot")
        return {"format": "text", "snapshot": snapshot}

    def execute(self, task: Task) -> TaskResult:
        if task.capability != "organism.bryo_status":
            raise AdapterProtocolError(f"unsupported Bryo capability: {task.capability}")
        return TaskResult(
            task_id=task.task_id,
            agent_id=self.manifest.agent_id,
            output=self.status(),
            evidence=("bryo:read-only-status",),
        )
