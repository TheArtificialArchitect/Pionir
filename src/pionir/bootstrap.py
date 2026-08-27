"""Construct the production runtime from configuration without importing source agents."""

from __future__ import annotations

from dataclasses import dataclass

from .adapters import AtaniCliAdapter, AtaniCliSettings, TheoPeerAdapter, TheoPeerSettings
from .audit import JsonlAuditSink
from .config import PionirSettings
from .reliability import CircuitBreaker
from .runtime import Executive, SpecialistAdapter
from .scheduler import ModelLeaseScheduler


@dataclass(slots=True)
class PionirRuntime:
    settings: PionirSettings
    executive: Executive
    adapters: dict[str, SpecialistAdapter]

    def register(self, adapter: SpecialistAdapter) -> None:
        self.executive.register(adapter)
        self.adapters[adapter.manifest.agent_id] = adapter


def build_runtime(settings: PionirSettings | None = None) -> PionirRuntime:
    configured = settings or PionirSettings.from_environment()
    configured.initialize_runtime()
    executive = Executive(
        scheduler=ModelLeaseScheduler(configured.resource_budget),
        audit_sink=JsonlAuditSink(configured.audit_path),
        circuit_factory=lambda: CircuitBreaker(
            failure_threshold=configured.circuit_failure_threshold,
            recovery_seconds=configured.circuit_recovery_seconds,
        ),
    )
    runtime = PionirRuntime(configured, executive, {})
    runtime.register(
        AtaniCliAdapter(AtaniCliSettings(command=configured.atani_command))
    )
    if configured.theo_token:
        runtime.register(
            TheoPeerAdapter(
                TheoPeerSettings(
                    base_url=configured.theo_url,
                    token=configured.theo_token,
                )
            )
        )
    return runtime
