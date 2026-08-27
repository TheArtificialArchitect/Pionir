"""Versioned subprocess boundary to Atani's existing JSON CLI."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from pionir.contracts import (
    AgentManifest,
    Capability,
    ModelRequirement,
    RiskLevel,
    Task,
    TaskResult,
)
from pionir.errors import AdapterProtocolError, AdapterUnavailable

MAX_OUTPUT_CHARS = 2_000_000


class CommandRunner(Protocol):
    def run(self, arguments: Sequence[str], *, timeout_seconds: int) -> str: ...


@dataclass(frozen=True, slots=True)
class AtaniCliSettings:
    command: tuple[str, ...] = ("atani",)
    timeout_seconds: int = 240
    version: str = "1.2"

    def __post_init__(self) -> None:
        if not self.command or any(not part for part in self.command):
            raise ValueError("Atani command cannot be empty")
        if self.timeout_seconds < 30:
            raise ValueError("Atani timeout must be at least 30 seconds")


class SubprocessCommandRunner:
    def __init__(self, command: Sequence[str]) -> None:
        self._command = tuple(command)

    def run(self, arguments: Sequence[str], *, timeout_seconds: int) -> str:
        try:
            process = subprocess.run(
                [*self._command, *arguments],
                capture_output=True,
                check=False,
                encoding="utf-8",
                errors="replace",
                shell=False,
                timeout=timeout_seconds,
            )
        except FileNotFoundError as error:
            raise AdapterUnavailable("Atani's configured executable was not found") from error
        except subprocess.TimeoutExpired as error:
            raise AdapterUnavailable("Atani did not finish before its timeout") from error
        if process.returncode != 0:
            raise AdapterUnavailable(
                f"Atani exited with status {process.returncode}; run `atani doctor` locally"
            )
        if len(process.stdout) > MAX_OUTPUT_CHARS:
            raise AdapterProtocolError("Atani's JSON response exceeded the size limit")
        return process.stdout


class AtaniCliAdapter:
    """Use Atani's real reasoning pipeline without copying its state into Pionir."""

    def __init__(
        self,
        settings: AtaniCliSettings | None = None,
        *,
        runner: CommandRunner | None = None,
    ) -> None:
        self.settings = settings or AtaniCliSettings()
        self._runner = runner or SubprocessCommandRunner(self.settings.command)
        permission = frozenset({"atani.chat"})
        self._manifest = AgentManifest(
            agent_id="atani",
            version=self.settings.version,
            capabilities=(
                Capability(
                    name="reasoning.atani_chat",
                    description="Atani's bounded default conversational reasoning",
                    risk=RiskLevel.REVERSIBLE_WRITE,
                    required_permissions=permission,
                    model=ModelRequirement("qwen2.5:7b-instruct", 4_700, 1_500),
                    priority=100,
                ),
                Capability(
                    name="reasoning.atani_depth",
                    description="Atani's slower deliberate reasoning path",
                    risk=RiskLevel.REVERSIBLE_WRITE,
                    required_permissions=permission,
                    model=ModelRequirement(
                        "nemotron-3.5-lightning:30b-a3b-q4_K_M",
                        10_000,
                        1_000,
                    ),
                    priority=100,
                ),
            ),
        )

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def _json(self, arguments: Sequence[str]) -> Mapping[str, Any]:
        raw = self._runner.run(arguments, timeout_seconds=self.settings.timeout_seconds)
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as error:
            raise AdapterProtocolError("Atani returned invalid JSON") from error
        if not isinstance(document, dict):
            raise AdapterProtocolError("Atani returned a non-object JSON response")
        return document

    def status(self) -> Mapping[str, Any]:
        return self._json(("status",))

    def execute(self, task: Task) -> TaskResult:
        if task.capability not in {"reasoning.atani_chat", "reasoning.atani_depth"}:
            raise AdapterProtocolError(f"unsupported Atani capability: {task.capability}")
        content = str(task.payload.get("content") or "").strip()
        if not content:
            raise AdapterProtocolError("Atani chat content is required")
        if len(content) > 8_000:
            raise AdapterProtocolError("Atani chat content exceeds Pionir's 8000-character limit")
        arguments = ["chat", "--json"]
        if task.capability == "reasoning.atani_depth":
            arguments.append("--depth")
        arguments.append(content)
        document = self._json(arguments)
        answer = document.get("answer")
        if not isinstance(answer, str) or not answer.strip():
            raise AdapterProtocolError("Atani returned no answer")
        cycle_id = str(document.get("cycle_id") or "").strip()
        evidence = (f"atani:reasoning-cycle:{cycle_id}",) if cycle_id else ()
        return TaskResult(
            task_id=task.task_id,
            agent_id=self.manifest.agent_id,
            output=document,
            evidence=evidence,
        )
