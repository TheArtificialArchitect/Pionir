"""Shared test fixtures.

Not a test module: unittest discovery looks for ``test*.py``, so this is only
imported, never collected.
"""

from __future__ import annotations

from pionir.scheduler import ModelLeaseScheduler, ResourceBudget
from pionir.shared_gpu import SharedGpuLock


def offline_scheduler(
    budget: ResourceBudget | None = None,
    shared_gpu_lock: SharedGpuLock | None = None,
) -> ModelLeaseScheduler:
    """A scheduler that cannot see the machine it is running on.

    ``ModelLeaseScheduler`` probes ``nvidia-smi`` and the Ollama daemon by
    default, which is right in production and wrong in a test: the result then
    depends on what happens to be loaded on the developer's card. That failure
    is worse than it looks, because it is invisible where it would be caught -
    on the Linux CI runners there is no NVIDIA tooling, the probe returns None,
    admission falls back to the static budget and everything passes. So a test
    that forgets to inject is green in CI and intermittently red only on Ian's
    machine, with a message about VRAM that reads like a code bug.

    Any test not specifically about observed-VRAM admission should build its
    scheduler here. The ones that are about it inject their own probes and
    assert on them.
    """

    return ModelLeaseScheduler(
        budget,
        shared_gpu_lock,
        vram_probe=None,
        residency_probe=None,
    )
