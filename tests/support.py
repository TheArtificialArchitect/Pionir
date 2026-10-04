"""Shared test fixtures.

Not a test module: unittest discovery looks for ``test*.py``, so this is only
imported, never collected.
"""

from __future__ import annotations

import os
import tempfile

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


def use_long_tempdir() -> None:
    r"""Make every temporary folder under the long, real spelling of the temp folder.

    GitHub's Windows runners set TEMP to an 8.3 short path (``C:\Users\RUNNER~1\...``).
    The build sandbox refuses such a path on purpose - a ``~`` could expand to another path,
    and a folder that resolves to a different spelling is treated as a link - so fixtures made
    under the short form fail there while passing on a box whose user name is short. A no-op
    where the temp folder already is its own real path (the dev box, Linux)."""
    tempfile.tempdir = os.path.realpath(tempfile.gettempdir())
