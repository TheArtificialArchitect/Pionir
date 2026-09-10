"""Construct the production runtime from configuration without importing source agents."""

from __future__ import annotations

from dataclasses import dataclass

from .adapters import (
    AtaniCliAdapter,
    AtaniCliSettings,
    BryoStatusAdapter,
    BryoStatusSettings,
    load_stdio_adapters,
)
from .audit import JsonlAuditSink
from .config import PionirSettings
from .cortex import Cortex, OllamaEmbedder
from .reliability import CircuitBreaker
from .runtime import Executive, SpecialistAdapter
from .scheduler import ModelLeaseScheduler
from .shared_gpu import SharedGpuLock


@dataclass(slots=True)
class PionirRuntime:
    settings: PionirSettings
    executive: Executive
    adapters: dict[str, SpecialistAdapter]
    cortex: Cortex

    def register(self, adapter: SpecialistAdapter) -> None:
        self.executive.register(adapter)
        self.adapters[adapter.manifest.agent_id] = adapter


def build_runtime(settings: PionirSettings | None = None) -> PionirRuntime:
    configured = settings or PionirSettings.from_environment()
    configured.initialize_runtime()
    embedder = (
        OllamaEmbedder(configured.embed_model) if configured.embed_model else None
    )
    cortex = Cortex(configured.cortex_path, embedder=embedder)
    executive = Executive(
        scheduler=ModelLeaseScheduler(
            configured.resource_budget,
            shared_gpu_lock=SharedGpuLock(configured.gpu_lock_path),
        ),
        audit_sink=JsonlAuditSink(configured.audit_path),
        circuit_factory=lambda: CircuitBreaker(
            failure_threshold=configured.circuit_failure_threshold,
            recovery_seconds=configured.circuit_recovery_seconds,
        ),
        # A circuit opening is the shell's own repeated-failure lesson; record it
        # into the shared lessons namespace so it is recalled before acting later.
        on_lesson=cortex.record_lesson,
    )
    runtime = PionirRuntime(configured, executive, {}, cortex)
    runtime.register(
        AtaniCliAdapter(AtaniCliSettings(command=configured.atani_command))
    )
    if configured.bryo_status_command is not None:
        runtime.register(
            BryoStatusAdapter(
                BryoStatusSettings(command=configured.bryo_status_command)
            )
        )
    if configured.specialists_file is not None:
        for adapter in load_stdio_adapters(configured.specialists_file):
            runtime.register(adapter)
    return runtime
