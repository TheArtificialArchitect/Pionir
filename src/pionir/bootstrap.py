"""Construct the production runtime from configuration without importing source agents."""

from __future__ import annotations

from dataclasses import dataclass, replace

from .adapters import (
    AtaniCliAdapter,
    AtaniCliSettings,
    BryoStatusAdapter,
    BryoStatusSettings,
    DaedalusAdapter,
    DaedalusSettings,
    GalateaAdapter,
    GalateaSettings,
    MeleteAdapter,
    MeleteSettings,
    NyxStatusAdapter,
    NyxStatusSettings,
    VoodooStatusAdapter,
    VoodooStatusSettings,
    load_stdio_adapters,
)
from .adapters.galatea import resolve_served_model
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


def _galatea_settings(configured: PionirSettings) -> GalateaSettings:
    """Pin the model if Pionir was told to, otherwise ask Galatea which she serves.

    The id drives the VRAM admission discount, and she is promoted like any
    other local model. A pinned id is honoured as given; otherwise the live one
    is read once at boot to have it right. Galatea being down is the ordinary
    case at boot and the declared default covers it - doctor shows the
    declaration against what she reports either way.
    """

    settings = GalateaSettings(base_url=str(configured.galatea_url))
    if configured.galatea_model_id:
        return replace(settings, model_id=configured.galatea_model_id)
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
    if configured.bryo_status_command is not None:
        runtime.register(
            BryoStatusAdapter(
                BryoStatusSettings(command=configured.bryo_status_command)
            )
        )
    if configured.nyx_status_command is not None:
        runtime.register(
            NyxStatusAdapter(NyxStatusSettings(command=configured.nyx_status_command))
        )
    if configured.voodoo_status_command is not None:
        runtime.register(
            VoodooStatusAdapter(
                VoodooStatusSettings(command=configured.voodoo_status_command)
            )
        )
    if configured.galatea_url is not None:
        runtime.register(GalateaAdapter(_galatea_settings(configured)))
    if configured.daedalus_url is not None:
        runtime.register(
            DaedalusAdapter(
                DaedalusSettings(
                    base_url=configured.daedalus_url,
                    token=configured.daedalus_token or "",
                )
            )
        )
    if configured.melete_url is not None:
        runtime.register(
            MeleteAdapter(
                MeleteSettings(
                    base_url=configured.melete_url,
                    token=configured.melete_token or "",
                )
            )
        )
    if configured.specialists_file is not None:
        for adapter in load_stdio_adapters(configured.specialists_file):
            runtime.register(adapter)
    return runtime
