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
from pionir.scheduler import kv_cache_vram_mb

MAX_OUTPUT_CHARS = 2_000_000


class CommandRunner(Protocol):
    def run(
        self,
        arguments: Sequence[str],
        *,
        timeout_seconds: int,
        input_text: str | None = None,
    ) -> str: ...


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

    def run(
        self,
        arguments: Sequence[str],
        *,
        timeout_seconds: int,
        input_text: str | None = None,
    ) -> str:
        try:
            process = subprocess.run(
                [*self._command, *arguments],
                capture_output=True,
                check=False,
                encoding="utf-8",
                errors="replace",
                shell=False,
                timeout=timeout_seconds,
                input=input_text,
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
                    model=ModelRequirement(
                        "qwen2.5:7b-instruct",
                        # Measured resident at 4096 context: 4423 MB.
                        4_500,
                        kv_cache_vram_mb(16_384),
                    ),
                    routing_hints=frozenset(
                        {"atani", "reason", "reasoning", "think", "answer", "chat"}
                    ),
                    priority=100,
                ),
                Capability(
                    name="reasoning.atani_depth",
                    description="Atani's slower deliberate reasoning path",
                    risk=RiskLevel.REVERSIBLE_WRITE,
                    required_permissions=permission,
                    model=ModelRequirement(
                        "nemotron-3.5-lightning:30b-a3b-q4_K_M",
                        # Measured: this model holds zero VRAM on a 12 GB card.
                        # Ollama runs it on the CPU, and because it is a
                        # mixture-of-experts with ~3B active parameters it still
                        # returns 30.4 tok/s there - as fast as a dense 8B on the
                        # GPU. Declaring it as a GPU tenant made it take the one
                        # GPU lease it never used, and made it unadmittable
                        # whenever anything else held the card.
                        0,
                        0,
                        requires_gpu=False,
                    ),
                    routing_hints=frozenset(
                        {"deep", "deeply", "careful", "deliberate", "thorough", "analyse"}
                    ),
                    priority=100,
                ),
                Capability(
                    name="executive.atani_run",
                    description=(
                        "Run a versioned plan through Atani's bounded executive, "
                        "capability broker, action ledger, and postcondition checks"
                    ),
                    risk=RiskLevel.PRIVILEGED,
                    required_permissions=frozenset({"atani.executive"}),
                    routing_hints=frozenset({"plan", "execute", "workflow", "ledger"}),
                    priority=110,
                ),
            ),
        )

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def _json(
        self,
        arguments: Sequence[str],
        *,
        input_text: str | None = None,
    ) -> Mapping[str, Any]:
        raw = self._runner.run(
            arguments,
            timeout_seconds=self.settings.timeout_seconds,
            input_text=input_text,
        )
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
        if task.capability == "executive.atani_run":
            request = dict(task.payload)
            if request.get("protocol") != "atani.executive.v1":
                raise AdapterProtocolError(
                    "Atani executive requests require protocol atani.executive.v1"
                )
            serialized = json.dumps(request, ensure_ascii=False)
            if len(serialized) > 250_000:
                raise AdapterProtocolError("Atani executive request exceeds 250000 characters")
            document = self._json(("executive",), input_text=serialized)
            goal_id = str(document.get("goal_id") or "").strip()
            status = str(document.get("status") or "").strip()
            if not goal_id or status not in {
                "completed",
                "waiting_approval",
                "paused",
                "failed",
            }:
                raise AdapterProtocolError("Atani returned an invalid executive outcome")
            return TaskResult(
                task_id=task.task_id,
                agent_id=self.manifest.agent_id,
                output=document,
                evidence=(f"atani:goal:{goal_id}",),
            )
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
