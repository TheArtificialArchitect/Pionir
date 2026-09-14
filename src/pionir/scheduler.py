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

import logging
import math
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from threading import Lock
from uuid import UUID, uuid4

from .benchmark import BenchmarkError, read_gpu_memory, read_loaded_models
from .contracts import ModelRequirement
from .errors import ResourceUnavailable
from .shared_gpu import SharedGpuLease, SharedGpuLock

_log = logging.getLogger(__name__)


def _run_in_thread(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="pionir-rewarm", daemon=True).start()


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


@dataclass(frozen=True, slots=True)
class _Handback:
    """What a GPU lease must put back when it ends.

    ``unload`` is the job's own model, when it was not already resident before
    the lease (someone else's warm copy is left alone). ``rewarm`` is every
    protected model that was on the card when the lease began - the voice's
    model the planned swap sidelined, or that the job's own load pushed out.
    """

    unload: str | None = None
    rewarm: tuple[str, ...] = ()


class ModelLease(AbstractContextManager["ModelLease"]):
    def __init__(
        self,
        scheduler: "ModelLeaseScheduler",
        requirement: ModelRequirement,
        shared_gpu_lease: SharedGpuLease | None = None,
        handback: _Handback | None = None,
    ) -> None:
        self.lease_id: UUID = uuid4()
        self.requirement = requirement
        self._scheduler = scheduler
        self._shared_gpu_lease = shared_gpu_lease
        self._handback = handback
        self._released = False

    def release(self) -> None:
        if not self._released:
            try:
                # Clean handback. The job's model is unloaded while the shared
                # lock is still held, so nothing that waits on the lock (the
                # voice) resumes against a card still full of the coder. The
                # re-warm runs after the lock is let go: it is a load of tens of
                # seconds and must not delay the job's result or the next lease.
                if self._handback is not None and self._handback.unload:
                    self._scheduler._unload_quietly(self._handback.unload)
                if self._shared_gpu_lease is not None:
                    self._shared_gpu_lease.release()
            finally:
                self._scheduler.release(self.lease_id)
                self._released = True
                if self._handback is not None and self._handback.rewarm:
                    self._scheduler._rewarm_quietly(self._handback.rewarm)

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
        evict_to_fit: bool = True,
        evictor: Callable[[str], None] | None = None,
        loaded_probe: Callable[[], list[str]] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        protected_models: Iterable[str] = (),
        rewarmer: Callable[[str], None] | None = None,
        background: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self.budget = budget or ResourceBudget()
        self.shared_gpu_lock = shared_gpu_lock
        self.vram_probe = vram_probe
        self.residency_probe = residency_probe
        # Models that are not evicted to make room for a caller without the
        # shared lease. The voice (Galatea) never takes the lock - she only
        # honours it, standing down while someone holds it - so without a lease
        # nothing says she is not mid-sentence, and naming her model here keeps
        # it. Under the lease she has stood down, and the eviction is the
        # planned swap (see _make_room).
        self.protected_models = frozenset(canonical_model(m) for m in protected_models if m.strip())
        # Loads a protected model back after a lease that displaced it ends.
        # Injected (bootstrap wires benchmark.warm), like the evictor, so a test
        # never loads a real model.
        self.rewarmer = rewarmer
        self._background = background or _run_in_thread
        # When a needed model does not fit, evict idle resident models to make
        # room (a 12B voice and a 7B doer cannot share a 12 GB card). Only done
        # while holding the shared GPU lock, so a model in active use by a
        # lock-holding participant is never pulled out from under it.
        self.evict_to_fit = evict_to_fit
        self.evictor = evictor
        self.loaded_probe = loaded_probe
        self._sleep = sleep
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

    def _make_room(
        self, needed_mb: int, target_model: str, *, under_lease: bool = False
    ) -> int | None:
        """Evict idle resident models other than the target until it fits.

        Reached while the shared GPU lock is held when there is one, so any
        tenant that cooperates by holding the lock while it works is never
        touched - what is sidelined is an idle model no one is generating
        against right now. The driver frees the memory a moment after Ollama
        unloads, so each eviction is followed by a short settle before the
        space is believed.

        ``under_lease`` says this caller holds the shared cross-process lease.
        Then even a protected model may go: the voice honours that lease and
        has stood down, so evicting her model is the planned swap, and the
        lease's release loads it back. Without the lease, protection stands.
        """

        # Eviction acts on the real daemon, so it is opt-in: it happens only when
        # an evictor and a resident-model probe have been injected (bootstrap
        # wires the real ones). A scheduler built without them - every test -
        # never unloads anything, so running the suite cannot touch live models.
        if (
            not self.evict_to_fit
            or self.vram_probe is None
            or self.evictor is None
            or self.loaded_probe is None
        ):
            return self.vram_probe() if self.vram_probe is not None else None
        evict = self.evictor
        try:
            resident = list(self.loaded_probe())
        except BenchmarkError:
            return self.vram_probe()
        target = canonical_model(target_model)
        free = self.vram_probe()
        spared: list[str] = []
        for name in resident:
            if free is not None and free >= needed_mb:
                break
            if canonical_model(name) == target:
                continue
            if canonical_model(name) in self.protected_models and not under_lease:
                spared.append(name)
                continue
            try:
                evict(name)
            except Exception:  # noqa: BLE001, S112 - a failed eviction just means no room freed
                continue
            for _ in range(10):  # settle: the driver frees VRAM shortly after unload
                self._sleep(0.5)
                free = self.vram_probe()
                if free is not None and free >= needed_mb:
                    break
        if spared and free is not None and free < needed_mb:
            raise ResourceUnavailable(
                f"{target_model} needs {needed_mb} MB VRAM; only {free} MB free, and "
                f"making room would mean evicting a protected model "
                f"({', '.join(spared)}) - refused. Wait for it to idle out, or "
                f"change PIONIR_PROTECTED_MODELS."
            )
        return free

    def _unload_quietly(self, model: str) -> None:
        """Best effort: a failed unload is logged, never raised - the job is over."""

        if self.evictor is None:
            return
        try:
            self.evictor(model)
        except Exception as error:  # noqa: BLE001 - handback must never fail a finished job
            _log.warning("handback: unloading %s failed: %s", model, error)

    def _rewarm_quietly(self, models: Iterable[str]) -> None:
        """Best effort, off the caller's thread: load the displaced protected
        models back so the voice's next turn does not pay the cold load."""

        rewarm = self.rewarmer
        if rewarm is None:
            return
        names = tuple(models)

        def work() -> None:
            for name in names:
                try:
                    rewarm(name)
                except Exception as error:  # noqa: BLE001 - logged, never raised
                    _log.warning("handback: re-warming %s failed: %s", name, error)

        try:
            self._background(work)
        except Exception as error:  # noqa: BLE001
            _log.warning("handback: could not schedule re-warm of %s: %s", names, error)

    def _plan_handback(self, requirement: ModelRequirement) -> _Handback | None:
        """Snapshot the card before a leased GPU job: what to unload and what to
        re-warm when it ends. Needs the injected daemon hooks, like eviction,
        so a scheduler built without them (every test by default) touches
        nothing on release."""

        if not self.evict_to_fit or self.evictor is None or self.loaded_probe is None:
            return None
        try:
            resident = {canonical_model(name) for name in self.loaded_probe()}
        except BenchmarkError:
            resident = set()
        target = canonical_model(requirement.model_id)
        return _Handback(
            unload=None if target in resident else requirement.model_id,
            rewarm=tuple(sorted(m for m in self.protected_models if m in resident and m != target)),
        )

    def acquire(self, requirement: ModelRequirement, *, purpose: str | None = None) -> ModelLease:
        """Admit one model use. ``purpose`` is what the shared lock's holder
        record says (e.g. ``"daedalus: qwen3-coder:30b"``), so a process that
        honours the lock - the voice - can say who has the card."""

        if requirement.requires_gpu and requirement.total_vram_mb > self.budget.usable_vram_mb:
            raise ResourceUnavailable(
                f"{requirement.model_id} needs {requirement.total_vram_mb} MB VRAM; "
                f"budget allows {self.budget.usable_vram_mb} MB"
            )

        # The shared GPU lock is taken before the VRAM check, not after: making
        # room means sidelining another tenant's model, and that is only safe
        # while holding the lock, which is what says no cooperating participant
        # is using the card right now.
        shared_lease: SharedGpuLease | None = None
        if requirement.requires_gpu and self.shared_gpu_lock is not None:
            shared_lease = self.shared_gpu_lock.try_acquire(
                owner="pionir", purpose=purpose or requirement.model_id
            )
            if shared_lease is None:
                raise ResourceUnavailable(
                    "GPU is leased by another Pionir-compatible process "
                    f"({self.shared_gpu_lock.describe_holder()})"
                )
        handback: _Handback | None = None
        try:
            if shared_lease is not None:
                handback = self._plan_handback(requirement)
            if requirement.requires_gpu and self.vram_probe is not None:
                needed_mb = self._marginal_vram_mb(requirement)
                free_mb = self.vram_probe()
                if free_mb is not None and needed_mb > free_mb:
                    # Sideline idle resident models to make room, then look again.
                    free_mb = self._make_room(
                        needed_mb, requirement.model_id, under_lease=shared_lease is not None
                    )
                if free_mb is not None and needed_mb > free_mb:
                    raise ResourceUnavailable(
                        f"{requirement.model_id} needs {needed_mb} MB VRAM; only "
                        f"{free_mb} MB free even after sidelining what could be freed."
                    )
            with self._lock:
                gpu_leases = sum(item.requires_gpu for item in self._active.values())
                if requirement.requires_gpu and gpu_leases >= self.budget.max_gpu_leases:
                    raise ResourceUnavailable("all GPU model leases are in use")
                lease = ModelLease(self, requirement, shared_lease, handback)
                self._active[lease.lease_id] = requirement
            return lease
        except BaseException:
            if shared_lease is not None:
                shared_lease.release()
            # A refused lease may already have sidelined a protected model on the
            # way to refusing; put it back rather than leave the voice cold.
            if handback is not None and handback.rewarm:
                self._rewarm_quietly(handback.rewarm)
            raise

    def release(self, lease_id: UUID) -> None:
        with self._lock:
            self._active.pop(lease_id, None)

    @property
    def active_requirements(self) -> tuple[ModelRequirement, ...]:
        with self._lock:
            return tuple(self._active.values())
