"""Synchronous reference executive for routing tasks through Pionir's gates."""

from __future__ import annotations

import logging
from contextlib import nullcontext
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from .contracts import AgentManifest, Task, TaskResult
from .errors import BodyDeferred, CircuitOpen, ResourceUnavailable
from .registry import CapabilityRegistry
from .reliability import CircuitBreaker, CircuitState
from .scheduler import ModelLeaseScheduler


class SpecialistAdapter(Protocol):
    """Minimum interface implemented by every independently deployed specialist."""

    @property
    def manifest(self) -> AgentManifest: ...

    def execute(self, task: Task) -> TaskResult: ...


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """Metadata-only event; task payloads and model output are excluded by design."""

    event_type: str
    task_id: UUID
    agent_id: str
    occurred_at: datetime
    detail: str | None = None


class AuditSink(Protocol):
    def record(self, event: AuditEvent) -> None: ...


class InMemoryAuditSink:
    """Reference sink used by tests; durable append-only storage is a later adapter."""

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []

    def record(self, event: AuditEvent) -> None:
        self._events.append(event)

    @property
    def events(self) -> tuple[AuditEvent, ...]:
        return tuple(self._events)


class Executive:
    """Routes one task while enforcing permissions, resource leases, and audit events."""

    def __init__(
        self,
        registry: CapabilityRegistry | None = None,
        scheduler: ModelLeaseScheduler | None = None,
        audit_sink: AuditSink | None = None,
        circuit_factory: Callable[[], CircuitBreaker] | None = None,
        on_lesson: Callable[[str], None] | None = None,
        pressure_probe: Callable[[], Any] | None = None,
    ) -> None:
        self.registry = registry or CapabilityRegistry()
        self.scheduler = scheduler or ModelLeaseScheduler()
        self.audit_sink = audit_sink or InMemoryAuditSink()
        self._adapters: dict[str, SpecialistAdapter] = {}
        self._circuit_factory = circuit_factory or CircuitBreaker
        self._circuits: dict[str, CircuitBreaker] = {}
        # Called with a one-line lesson when a specialist's circuit opens. The
        # shell learning from its own repeated failures - decoupled from cortex,
        # so the executive stays a scheduler and this is just a callback. bootstrap
        # wires it to cortex.record_lesson.
        self._on_lesson = on_lesson
        # Bryo's felt pressure: the body the spine consults before heavy GPU work.
        # Advisory only - a probe returning an organism that is not alive, or no
        # probe at all, is "no opinion" and changes nothing. bootstrap wires it to a
        # BryoPressureReader.peek, which never blocks.
        self._pressure_probe = pressure_probe

    def body_reading(self) -> Any | None:
        """Bryo's current advisory reading, or None. Never raises, never blocks."""

        if self._pressure_probe is None:
            return None
        try:
            return self._pressure_probe()
        except Exception as error:  # noqa: BLE001 - the body is advisory, never load-bearing
            logging.getLogger(__name__).warning("pressure probe failed: %s", error)
            return None

    def register(self, adapter: SpecialistAdapter) -> None:
        self.registry.register(adapter.manifest)
        self._adapters[adapter.manifest.agent_id] = adapter
        self._circuits[adapter.manifest.agent_id] = self._circuit_factory()

    def execute(
        self,
        task: Task,
        *,
        routing_detail: str | None = None,
        deferrable: bool = False,
    ) -> TaskResult:
        """Run one task through the permission, resource, and audit gates.

        ``routing_detail`` is metadata about how this task's capability was
        chosen - the intent router's confidence and runner-up. It is recorded
        against ``task.routed`` so the ledger shows not just where a task went
        but how sure anything was about sending it there. It must stay
        payload-free; the ledger excludes task content by design.

        ``deferrable`` marks work that can wait. Before any GPU lease the body is
        consulted: its advice is always recorded (``task.paced``), and only a
        deferrable task is actually held back (``task.deferred``) when Bryo is alive
        and advises deferring. It defaults to False, so every existing caller - the
        voice above all - behaves exactly as before.
        """

        route = self.registry.resolve(task)
        adapter = self._adapters[route.agent_id]
        circuit = self._circuits[route.agent_id]
        self._record("task.routed", task, route.agent_id, routing_detail)

        try:
            circuit.before_call()
            model = route.capability.model
            # Consult the body before ANY model-backed work, not only GPU work: a
            # CPU model still loads and competes for the machine, and a deferrable
            # background job should yield to a stressed organism whatever card the
            # model wants. A model-free capability (a pure status read) is never paced.
            if model is not None:
                self._consult_body(task, route.agent_id, deferrable=deferrable)
            # The purpose names the tenant as well as the model, so the lock's
            # holder record reads "daedalus: qwen3-coder:30b" to anyone who
            # honours it (the voice uses it to say who has the card).
            lease_context = (
                self.scheduler.acquire(
                    model, purpose=f"{route.agent_id}: {model.model_id}"
                )
                if model is not None
                else nullcontext()
            )
            with lease_context:
                result = adapter.execute(task)
            if result.task_id != task.task_id:
                raise ValueError("adapter returned a result for a different task")
            if result.agent_id != route.agent_id:
                raise ValueError("adapter result agent_id does not match the routed agent")
        except Exception as error:
            if isinstance(error, ResourceUnavailable):
                # The specialist was never called, but if this was the half-open
                # recovery probe its slot must be returned or the circuit never
                # closes again.
                circuit.record_unattempted()
            elif not isinstance(error, CircuitOpen):
                # A rejected call holds no probe slot, so there is nothing to return.
                was_open = circuit.snapshot().state is CircuitState.OPEN
                circuit.record_failure()
                if not was_open and circuit.snapshot().state is CircuitState.OPEN:
                    # It just tripped: a repeated failure, not a blip. That is a
                    # real lesson - recorded once per trip, so it never floods.
                    self._record_lesson(
                        f"{route.agent_id} circuit opened after repeated failures; "
                        f"last error {type(error).__name__}: {error}"
                    )
            self._record("task.failed", task, route.agent_id, type(error).__name__)
            raise

        circuit.record_success()
        self._record("task.completed", task, route.agent_id)
        return result

    def circuit(self, agent_id: str) -> CircuitBreaker:
        return self._circuits[agent_id]

    def _consult_body(self, task: Task, agent_id: str, *, deferrable: bool) -> None:
        """Ask the body before heavy GPU work. Raises BodyDeferred only when the task
        is deferrable AND a living Bryo advises deferring; otherwise records the
        advice and returns. A silent or dead organism is no opinion at all."""

        reading = self.body_reading()
        if reading is None or getattr(reading, "alive", False) is not True:
            return
        detail = str(getattr(reading, "detail", "bryo"))
        if deferrable and getattr(reading, "defer_heavy_work", False) is True:
            self._record("task.deferred", task, agent_id, detail)
            raise BodyDeferred(f"Bryo advises deferring heavy work: {detail}")
        self._record("task.paced", task, agent_id, detail)

    def _record_lesson(self, text: str) -> None:
        """Best-effort: a lesson sink must never take a task down (§3.18 - but
        logged, not swallowed silently, if it ever raises)."""
        if self._on_lesson is None:
            return
        try:
            self._on_lesson(text)
        except Exception as error:  # noqa: BLE001
            logging.getLogger(__name__).warning("lesson sink failed: %s", error)

    def _record(
        self,
        event_type: str,
        task: Task,
        agent_id: str,
        detail: str | None = None,
    ) -> None:
        self.audit_sink.record(
            AuditEvent(
                event_type=event_type,
                task_id=task.task_id,
                agent_id=agent_id,
                occurred_at=datetime.now(UTC),
                detail=detail,
            )
        )
