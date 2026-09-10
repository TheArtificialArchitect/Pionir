"""Construct the production runtime from configuration without importing source agents."""

from __future__ import annotations

from dataclasses import dataclass, replace

from .adapters import (
    AtaniCliAdapter,
    AtaniCliSettings,
    BryoStatusAdapter,
    BryoStatusSettings,
    TheoAdapter,
    TheoSettings,
    load_stdio_adapters,
)
from .adapters.theo import resolve_served_model
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


def _theo_settings(configured: PionirSettings) -> TheoSettings:
    """Pin the model if Pionir was told to, otherwise ask Theo which he serves.

    The id drives the VRAM admission discount, and Theo is promoted often. A
    value declared here goes stale on the next promotion and the only symptom is
    Pionir refusing turns that would have fitted, so it is worth one loopback GET
    at boot to have it right. Theo being down is the ordinary case and costs
    nothing: a refused connection on loopback returns immediately and the
    adapter's own default covers it.
    """

    settings = TheoSettings(base_url=configured.theo_url, token=configured.theo_token)
    if configured.theo_model_id:
        return replace(settings, model_id=configured.theo_model_id)
    served = resolve_served_model(settings)
    return replace(settings, model_id=served) if served else settings


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
    if configured.theo_token:
        runtime.register(TheoAdapter(_theo_settings(configured)))
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
