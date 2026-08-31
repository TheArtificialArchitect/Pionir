"""Construct the production runtime from configuration without importing source agents."""

from __future__ import annotations

from dataclasses import dataclass

from .adapters import (
    AtaniCliAdapter,
    AtaniCliSettings,
    AutogenesisStatusAdapter,
    AutogenesisStatusSettings,
    BryoStatusAdapter,
    BryoStatusSettings,
    GenesisStatusAdapter,
    GenesisStatusSettings,
    ProbabilityStatusAdapter,
    ProbabilityStatusSettings,
    TheoAdapter,
    TheoSettings,
    load_stdio_adapters,
)
from .audit import JsonlAuditSink
from .config import PionirSettings
from .reliability import CircuitBreaker
from .runtime import Executive, SpecialistAdapter
from .scheduler import ModelLeaseScheduler
from .shared_gpu import SharedGpuLock


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
        scheduler=ModelLeaseScheduler(
            configured.resource_budget,
            shared_gpu_lock=SharedGpuLock(configured.gpu_lock_path),
        ),
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
            TheoAdapter(
                TheoSettings(
                    base_url=configured.theo_url,
                    token=configured.theo_token,
                    model_id=configured.theo_model_id,
                )
            )
        )
    if configured.bryo_status_command is not None:
        runtime.register(
            BryoStatusAdapter(
                BryoStatusSettings(command=configured.bryo_status_command)
            )
        )
    if configured.autogenesis_status_command is not None:
        runtime.register(
            AutogenesisStatusAdapter(
                AutogenesisStatusSettings(
                    command=configured.autogenesis_status_command
                )
            )
        )
    if configured.probability_url is not None:
        runtime.register(
            ProbabilityStatusAdapter(
                ProbabilityStatusSettings(base_url=configured.probability_url)
            )
        )
    if configured.genesis_url is not None:
        runtime.register(
            GenesisStatusAdapter(
                GenesisStatusSettings(base_url=configured.genesis_url)
            )
        )
    if configured.specialists_file is not None:
        for adapter in load_stdio_adapters(configured.specialists_file):
            runtime.register(adapter)
    return runtime
