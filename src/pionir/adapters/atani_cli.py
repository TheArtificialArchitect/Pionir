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

# The executive's four legitimate outcomes. A response carrying one of these on
# stdout is a real Manager result and authoritative over the process exit code.
#
# Atani's exit code is a three-band signal (confirmed with her session,
# 2026-09-10, TheArtificialArchitect/Atani 0ad93ec): 0 = completed (the only
# success, so a shell `&&` fires on nothing else); 2 = a produced-but-not-
# completed verdict - failed, paused, or waiting_approval - a real outcome on
# stdout, not an error; 1 = no outcome produced (malformed request or
# exception), stdout empty, reason on stderr. So exit 1 alone means "read
# stderr" and 0/2 mean "read the status on stdout". Reading the exit code first
# would turn a real failed/paused outcome into a bare "unavailable" and throw
# its goal_id and reason away - and an earlier binary emitted the same outcomes
# at exit 1, so this adapter reads stdout first and is robust to either.
_EXECUTIVE_STATUSES = frozenset(
    {"completed", "waiting_approval", "paused", "failed"}
)


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def run(
        self,
        arguments: Sequence[str],
        *,
        timeout_seconds: int,
        input_text: str | None = None,
    ) -> CommandResult: ...


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
    ) -> CommandResult:
        # The exit code is deliberately not judged here. Whether a non-zero exit
        # is fatal is a per-command decision - fatal for chat and status, but not
        # for the executive, whose outcome lives on stdout regardless - so that
        # policy belongs in the adapter, not in a runner that cannot tell the
        # commands apart.
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
        if len(process.stdout) > MAX_OUTPUT_CHARS:
            raise AdapterProtocolError("Atani's JSON response exceeded the size limit")
        return CommandResult(
            returncode=process.returncode,
            stdout=process.stdout,
            stderr=process.stderr or "",
        )


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
                    # Renamed from reasoning.atani_chat on 2026-08-30. The
                    # router scores a capability's own name as vocabulary, so
                    # "chat" in the name kept Atani tied with the voice on any
                    # plain conversational request no matter what the hints said -
                    # and the name had become untrue anyway once chat became the
                    # voice's. Ledger entries from before the rename carry the old
                    # name. The voice is Galatea now (was Theo); Atani stays the
                    # reasoner either way, which is the point of the rename.
                    name="reasoning.atani_answer",
                    description="Atani's bounded reasoning over a question",
                    risk=RiskLevel.REVERSIBLE_WRITE,
                    required_permissions=permission,
                    model=ModelRequirement(
                        "qwen2.5:7b-instruct",
                        # Measured resident at 4096 context: 4423 MB.
                        4_500,
                        kv_cache_vram_mb(16_384),
                    ),
                    # "chat" is deliberately absent. Plain conversation is the
                    # voice's (Galatea's), and Atani keeps the reasoning
                    # vocabulary. Encoding that here rather than as a special
                    # case inside the router is the whole point of capabilities
                    # declaring their own words: the decision is visible where it
                    # applies.
                    routing_hints=frozenset(
                        {"atani", "reason", "reasoning", "think", "answer"}
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
                        # This model is an elastic tenant, and the zeros are a
                        # statement about leases rather than about VRAM.
                        #
                        # Measured twice on the same card and the same daemon:
                        # 0 MB resident on 2026-08-27, and 2611 MB on 2026-08-30.
                        # Ollama partial-offloads whatever happens to fit and
                        # runs the rest on the CPU, so there is no fixed
                        # footprint to declare and any number written here would
                        # be wrong by the next reading.
                        #
                        # It needs no GPU lease because it never fails to run -
                        # a mixture-of-experts with ~3B active parameters still
                        # returned 30.4 tok/s with nothing on the card at all.
                        # Declaring it a GPU tenant took the single lease it did
                        # not need and made it unadmittable whenever anything
                        # else held the card. What makes the elastic
                        # consumption safe is that admission reads free VRAM at
                        # lease time rather than summing these declarations, so
                        # whatever this model has taken is already priced in.
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
                Capability(
                    # Atani the manager, doing what Moss asks: it decides and
                    # tasks the right doer (through Pionir, so the sideline and
                    # gates hold). No model here on purpose - manage holds no GPU
                    # lease, so the doer's own task can take the single lease.
                    # Invoked directly by /api/intent, never NL-routed.
                    name="manager.atani_manage",
                    description="Atani deciding and tasking the right bot for what the voice asks",
                    risk=RiskLevel.PRIVILEGED,
                    required_permissions=frozenset({"atani.manage"}),
                    priority=90,
                ),
            ),
        )

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def _run(
        self,
        arguments: Sequence[str],
        *,
        input_text: str | None = None,
    ) -> CommandResult:
        return self._runner.run(
            arguments,
            timeout_seconds=self.settings.timeout_seconds,
            input_text=input_text,
        )

    @staticmethod
    def _parse_object(raw: str) -> Mapping[str, Any]:
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as error:
            raise AdapterProtocolError("Atani returned invalid JSON") from error
        if not isinstance(document, dict):
            raise AdapterProtocolError("Atani returned a non-object JSON response")
        return document

    @staticmethod
    def _unavailable(result: CommandResult) -> AdapterUnavailable:
        """Atani's own reason for a non-zero exit, not a generic pointer.

        The far side writes "Atani error: <why>" to stderr; discarding it and
        saying only "run atani doctor" turned a specific, actionable failure into
        a shrug. Surface the reason it actually gave.
        """

        reason = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else ""
        detail = reason or f"Atani exited with status {result.returncode}; run `atani doctor` locally"
        return AdapterUnavailable(detail)

    def _json(
        self,
        arguments: Sequence[str],
        *,
        input_text: str | None = None,
    ) -> Mapping[str, Any]:
        """A plain JSON command (chat, status) where a non-zero exit is fatal."""

        result = self._run(arguments, input_text=input_text)
        if result.returncode != 0:
            raise self._unavailable(result)
        return self._parse_object(result.stdout)

    def _executive_outcome(self, result: CommandResult) -> Mapping[str, Any]:
        """The executive's structured outcome, read from stdout before the exit code.

        A failed or paused goal is a real Manager result, not an unavailable
        specialist, and Atani writes it to stdout while exiting non-zero. So the
        outcome is honoured whenever stdout carries a known status; only when
        stdout holds no outcome at all is the non-zero exit treated as a failure,
        and then with Atani's own stderr reason rather than a generic one.
        """

        stdout = result.stdout.strip()
        if stdout:
            try:
                document = json.loads(stdout)
            except json.JSONDecodeError:
                document = None
            if isinstance(document, dict) and document.get("status") in _EXECUTIVE_STATUSES:
                return document
        if result.returncode != 0:
            raise self._unavailable(result)
        # Exit 0 but no recognisable outcome is a contract break, not a downed
        # specialist: the call ran and produced something Pionir cannot read.
        raise AdapterProtocolError("Atani returned no executive outcome")

    def status(self) -> Mapping[str, Any]:
        return self._json(("status",))

    def execute(self, task: Task) -> TaskResult:
        if task.capability == "manager.atani_manage":
            content = str(task.payload.get("content") or task.payload.get("request") or "").strip()
            if not content:
                raise AdapterProtocolError("manager request is required")
            if len(content) > 8_000:
                raise AdapterProtocolError("manager request exceeds Pionir's 8000-character limit")
            # `atani manage <request>` reasons, decides, and tasks the doer via
            # Pionir; it exits 0 with its outcome object (ok may be false - that
            # is a real manager verdict, not a crash).
            document = self._json(("manage", content))
            return TaskResult(
                task_id=task.task_id,
                agent_id=self.manifest.agent_id,
                output=document,
                evidence=("atani:manage",),
            )
        if task.capability == "executive.atani_run":
            request = dict(task.payload)
            if request.get("protocol") != "atani.executive.v1":
                raise AdapterProtocolError(
                    "Atani executive requests require protocol atani.executive.v1"
                )
            serialized = json.dumps(request, ensure_ascii=False)
            if len(serialized) > 250_000:
                raise AdapterProtocolError("Atani executive request exceeds 250000 characters")
            document = self._executive_outcome(
                self._run(("executive",), input_text=serialized)
            )
            goal_id = str(document.get("goal_id") or "").strip()
            status = str(document.get("status") or "").strip()
            if not goal_id or status not in _EXECUTIVE_STATUSES:
                raise AdapterProtocolError("Atani returned an invalid executive outcome")
            return TaskResult(
                task_id=task.task_id,
                agent_id=self.manifest.agent_id,
                output=document,
                evidence=(f"atani:goal:{goal_id}",),
            )
        if task.capability not in {"reasoning.atani_answer", "reasoning.atani_depth"}:
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
