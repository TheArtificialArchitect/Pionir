"""Admission control for GPU model use on the target workstation."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from threading import Lock
from uuid import UUID, uuid4

from .contracts import ModelRequirement
from .errors import ResourceUnavailable


@dataclass(frozen=True, slots=True)
class ResourceBudget:
    total_vram_mb: int = 12_288
    reserved_vram_mb: int = 1_024
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
    ) -> None:
        self.lease_id: UUID = uuid4()
        self.requirement = requirement
        self._scheduler = scheduler
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._scheduler.release(self.lease_id)
            self._released = True

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.release()


class ModelLeaseScheduler:
    """Fail-fast scheduler that prevents accidental simultaneous model residency."""

    def __init__(self, budget: ResourceBudget | None = None) -> None:
        self.budget = budget or ResourceBudget()
        self._active: dict[UUID, ModelRequirement] = {}
        self._lock = Lock()

    def acquire(self, requirement: ModelRequirement) -> ModelLease:
        if requirement.requires_gpu and requirement.total_vram_mb > self.budget.usable_vram_mb:
            raise ResourceUnavailable(
                f"{requirement.model_id} needs {requirement.total_vram_mb} MB VRAM; "
                f"budget allows {self.budget.usable_vram_mb} MB"
            )

        lease = ModelLease(self, requirement)
        with self._lock:
            gpu_leases = sum(item.requires_gpu for item in self._active.values())
            if requirement.requires_gpu and gpu_leases >= self.budget.max_gpu_leases:
                raise ResourceUnavailable("all GPU model leases are in use")
            self._active[lease.lease_id] = requirement
        return lease

    def release(self, lease_id: UUID) -> None:
        with self._lock:
            self._active.pop(lease_id, None)

    @property
    def active_requirements(self) -> tuple[ModelRequirement, ...]:
        with self._lock:
            return tuple(self._active.values())
