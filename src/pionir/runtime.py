"""Synchronous reference executive for routing tasks through Pionir's gates."""

from __future__ import annotations

from contextlib import nullcontext
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from .contracts import AgentManifest, Task, TaskResult
from .errors import ResourceUnavailable
from .registry import CapabilityRegistry
from .reliability import CircuitBreaker
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
    ) -> None:
        self.registry = registry or CapabilityRegistry()
        self.scheduler = scheduler or ModelLeaseScheduler()
        self.audit_sink = audit_sink or InMemoryAuditSink()
        self._adapters: dict[str, SpecialistAdapter] = {}
        self._circuit_factory = circuit_factory or CircuitBreaker
        self._circuits: dict[str, CircuitBreaker] = {}

    def register(self, adapter: SpecialistAdapter) -> None:
        self.registry.register(adapter.manifest)
        self._adapters[adapter.manifest.agent_id] = adapter
        self._circuits[adapter.manifest.agent_id] = self._circuit_factory()

    def execute(self, task: Task) -> TaskResult:
        route = self.registry.resolve(task)
        adapter = self._adapters[route.agent_id]
        circuit = self._circuits[route.agent_id]
        self._record("task.routed", task, route.agent_id)

        try:
            circuit.before_call()
            lease_context = (
                self.scheduler.acquire(route.capability.model)
                if route.capability.model is not None
                else nullcontext()
            )
            with lease_context:
                result = adapter.execute(task)
            if result.task_id != task.task_id:
                raise ValueError("adapter returned a result for a different task")
            if result.agent_id != route.agent_id:
                raise ValueError("adapter result agent_id does not match the routed agent")
        except Exception as error:
            if not isinstance(error, ResourceUnavailable):
                circuit.record_failure()
            self._record("task.failed", task, route.agent_id, type(error).__name__)
            raise

        circuit.record_success()
        self._record("task.completed", task, route.agent_id)
        return result

    def circuit(self, agent_id: str) -> CircuitBreaker:
        return self._circuits[agent_id]

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
