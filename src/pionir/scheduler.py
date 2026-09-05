"""Admission control for GPU model use on the target workstation.

Two Phase 0 findings shape this (``docs/PHASE0_BENCHMARK.md``).

**A budget is not a measurement.** Processes outside Pionir hold VRAM and do not
take the shared lock, so lease accounting cannot see them. On the target machine
the desktop floor alone is ~1830 MB, and a Genesis backend keeps a 7B resident
by design. Admission therefore checks what the driver says is free *now*, not
only what the budget once allowed.

**Overcommitting the card does not fail loudly.** Asked for a model that will not
fit, Ollama runs it on the CPU and returns a correct answer, slowly, with no
error anywhere - measured at 6.4 tok/s for a dense 15 GB model against 47 tok/s
on the GPU. So there is nothing to catch downstream: if the observed check does
not refuse it here, the silent CPU fallback is what ships.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from threading import Lock
from uuid import UUID, uuid4

from .benchmark import BenchmarkError, read_gpu_memory, read_loaded_models
from .contracts import ModelRequirement
from .errors import ResourceUnavailable
from .shared_gpu import SharedGpuLease, SharedGpuLock

# Measured KV growth on the target card by reloading each 7B at 4096 and at
# 16384 context: 30.75 MB per 1024 tokens for two of them, 43.08 for the third.
# The largest is used. Under-declaring context is what lets a model spill
# silently to system memory, which produces a correct answer at a seventh of the
# speed and no error anywhere.
KV_CACHE_MB_PER_1K_TOKENS = 43.08


def kv_cache_vram_mb(context_tokens: int) -> int:
    """VRAM a declared context window costs, above the model weights."""

    if context_tokens < 0:
        raise ValueError("context length cannot be negative")
    return math.ceil(context_tokens / 1024 * KV_CACHE_MB_PER_1K_TOKENS)


def observed_free_vram_mb() -> int | None:
    """Free VRAM as the driver reports it, or ``None`` when it cannot be measured.

    ``None`` means unmeasurable, not zero: no NVIDIA tooling, or no card at all,
    as on the Linux CI runners. Callers fall back to the static budget rather
    than refusing everything, which leaves them no worse off than before this
    check existed.
    """

    try:
        return read_gpu_memory().free_mb
    except BenchmarkError:
        return None


def canonical_model(name: str) -> str:
    """Normalise a model name the way Ollama's own inventory reports it.

    A name written without a tag means `:latest`. Theo's promotion mechanism
    writes `theo-local-v25-q4` while the daemon reports
    `theo-local-v25-q4:latest`, so comparing the two verbatim never matches and
    the residency discount silently never applies.
    """

    name = name.strip()
    return name if ":" in name else f"{name}:latest"


def model_already_resident(model_id: str) -> bool:
    """Whether the local daemon already holds this model wholly on the GPU.

    A model another tenant has loaded costs nothing to reuse - Ollama serves the
    resident copy. Genesis currently keeps ``qwen2.5:7b-instruct`` hot, which is
    also Atani's model, so without this a free-VRAM check would refuse a load
    that was already paid for.
    """

    try:
        wanted = canonical_model(model_id)
        return any(
            canonical_model(item.name) == wanted and item.fully_on_gpu
            for item in read_loaded_models()
        )
    except BenchmarkError:
        return False


@dataclass(frozen=True, slots=True)
class ResourceBudget:
    total_vram_mb: int = 12_288
    # Measured five times with every model evicted: 1780-1836 MB held by the
    # desktop, browser, and the tools Ian keeps open. The previous 1024 MB was
    # an estimate that admitted models which could not fit on an idle card.
    reserved_vram_mb: int = 1_830
    max_gpu_leases: int = 1

    def __post_init__(self) -> None:
        if self.total_vram_mb <= 0:
            raise ValueError("total_vram_mb must be positive")
        if self.reserved_vram_mb < 0 or self.reserved_vram_mb >= self.total_vram_mb:
            raise ValueError("reserved_vram_mb must be within the total VRAM budget")
        if self.max_gpu_leases < 1:
            raise ValueError("max_gpu_leases must be at least one")

    @property
    def usable_vram_mb(self) -> int:
        return self.total_vram_mb - self.reserved_vram_mb


class ModelLease(AbstractContextManager["ModelLease"]):
    def __init__(
        self,
        scheduler: "ModelLeaseScheduler",
        requirement: ModelRequirement,
        shared_gpu_lease: SharedGpuLease | None = None,
    ) -> None:
        self.lease_id: UUID = uuid4()
        self.requirement = requirement
        self._scheduler = scheduler
        self._shared_gpu_lease = shared_gpu_lease
        self._released = False

    def release(self) -> None:
        if not self._released:
            try:
                if self._shared_gpu_lease is not None:
                    self._shared_gpu_lease.release()
            finally:
                self._scheduler.release(self.lease_id)
                self._released = True

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.release()


class ModelLeaseScheduler:
    """Fail-fast scheduler that prevents accidental simultaneous model residency."""

    def __init__(
        self,
        budget: ResourceBudget | None = None,
        shared_gpu_lock: SharedGpuLock | None = None,
        *,
        vram_probe: Callable[[], int | None] | None = observed_free_vram_mb,
        residency_probe: Callable[[str], bool] | None = model_already_resident,
    ) -> None:
        self.budget = budget or ResourceBudget()
        self.shared_gpu_lock = shared_gpu_lock
        self.vram_probe = vram_probe
        self.residency_probe = residency_probe
        self._active: dict[UUID, ModelRequirement] = {}
        self._lock = Lock()

    def _marginal_vram_mb(self, requirement: ModelRequirement) -> int:
        """What loading this model would actually add to the card.

        Reusing a resident copy costs only the KV cache for this caller's
        context, not the weights again.
        """

        if self.residency_probe is not None and self.residency_probe(requirement.model_id):
            return requirement.context_vram_mb
        return requirement.total_vram_mb

    def acquire(self, requirement: ModelRequirement) -> ModelLease:
        if requirement.requires_gpu and requirement.total_vram_mb > self.budget.usable_vram_mb:
            raise ResourceUnavailable(
                f"{requirement.model_id} needs {requirement.total_vram_mb} MB VRAM; "
                f"budget allows {self.budget.usable_vram_mb} MB"
            )

        # Probed before the mutex so a subprocess call is not held across it.
        # With max_gpu_leases at one the shared lock is the real serialiser, so
        # the window this opens is narrower than the reading is accurate.
        if requirement.requires_gpu and self.vram_probe is not None:
            free_mb = self.vram_probe()
            needed_mb = self._marginal_vram_mb(requirement)
            if free_mb is not None and needed_mb > free_mb:
                raise ResourceUnavailable(
                    f"{requirement.model_id} needs {needed_mb} MB VRAM; the card has "
                    f"{free_mb} MB free right now. VRAM held by processes outside "
                    f"Pionir does not take the shared lock, so the lease count "
                    f"cannot see it."
                )

        with self._lock:
            gpu_leases = sum(item.requires_gpu for item in self._active.values())
            if requirement.requires_gpu and gpu_leases >= self.budget.max_gpu_leases:
                raise ResourceUnavailable("all GPU model leases are in use")
            shared_lease = None
            if requirement.requires_gpu and self.shared_gpu_lock is not None:
                shared_lease = self.shared_gpu_lock.try_acquire(
                    owner="pionir",
                    purpose=requirement.model_id,
                )
                if shared_lease is None:
                    raise ResourceUnavailable(
                        "GPU is leased by another Pionir-compatible process"
                    )
            lease = ModelLease(self, requirement, shared_lease)
            self._active[lease.lease_id] = requirement
        return lease

    def release(self, lease_id: UUID) -> None:
        with self._lock:
            self._active.pop(lease_id, None)

    @property
    def active_requirements(self) -> tuple[ModelRequirement, ...]:
        with self._lock:
            return tuple(self._active.values())
