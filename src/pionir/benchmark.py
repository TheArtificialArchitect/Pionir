"""Phase 0 resource measurement for the target workstation.

The fusion plan's admission rules are deliberately conservative estimates. This
module replaces them with observed values: the VRAM floor the desktop holds
before Pionir loads anything, each model's resident and context VRAM, cold start
latency, and warm task latency.

It is stdlib-only, and read-only with respect to every specialist. It talks to a
local Ollama daemon and to ``nvidia-smi``; it never writes to a source project.

Two honesty rules are built in rather than left to the operator:

* A measurement taken against a still-resident model would report a warm latency
  as a cold start, and nothing would say so. Eviction is therefore verified
  against the daemon's own inventory before any cold start is timed.
* Whatever happened to be resident when the run began is recorded verbatim. A
  headroom figure measured underneath somebody else's leaked model is not the
  figure the scheduler will live with, so both are reported.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"

# Ollama reports bytes; the scheduler contract is denominated in whole MB.
_BYTES_PER_MB = 1024 * 1024


class BenchmarkError(RuntimeError):
    """A measurement could not be taken, and the reason must not be guessed at."""


@dataclass(frozen=True, slots=True)
class GpuMemory:
    """A single ``nvidia-smi`` sample, in MB."""

    total_mb: int
    used_mb: int
    free_mb: int


@dataclass(frozen=True, slots=True)
class LoadedModel:
    """One entry from the daemon's own residency inventory."""

    name: str
    size_bytes: int
    size_vram_bytes: int
    context_length: int

    @property
    def size_vram_mb(self) -> int:
        return round(self.size_vram_bytes / _BYTES_PER_MB)

    @property
    def size_mb(self) -> int:
        return round(self.size_bytes / _BYTES_PER_MB)

    @property
    def fully_on_gpu(self) -> bool:
        return self.size_bytes > 0 and self.size_vram_bytes >= self.size_bytes


@dataclass(frozen=True, slots=True)
class ContextMeasurement:
    """Resident VRAM for one model at one context length."""

    num_ctx: int
    size_vram_mb: int
    driver_delta_mb: int
    fully_on_gpu: bool
    foreign_resident: tuple[str, ...] = ()

    @property
    def contended(self) -> bool:
        """Whether a model this benchmark did not load was resident alongside."""

        return bool(self.foreign_resident)


@dataclass(slots=True)
class ModelBenchmark:
    """Everything Phase 0 needs to know about one model."""

    model: str
    cold_start_seconds: float | None = None
    warm_latency_seconds: float | None = None
    baseline_context: int = 0
    resident_vram_mb: int = 0
    driver_delta_mb: int = 0
    fully_on_gpu: bool = False
    contexts: list[ContextMeasurement] = field(default_factory=list)
    context_mb_per_1k: float | None = None
    foreign_resident: tuple[str, ...] = ()
    evicted_by_contention: bool = False
    error: str | None = None


@dataclass(slots=True)
class BenchmarkReport:
    """The complete Phase 0 measurement, suitable for an audit event."""

    taken_at: str
    gpu_name: str
    driver_version: str
    ollama_version: str
    total_vram_mb: int
    floor_vram_mb: int
    as_found_used_mb: int = 0
    as_found_models: list[str] = field(default_factory=list)
    floor_processes: list[str] = field(default_factory=list)
    models: list[ModelBenchmark] = field(default_factory=list)
    coresidency: dict[str, object] | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


# --------------------------------------------------------------------------
# Driver and daemon probes
# --------------------------------------------------------------------------


def _nvidia_smi(query: str) -> list[str]:
    try:
        completed = subprocess.run(
            ["nvidia-smi", "--query-gpu=" + query, "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise BenchmarkError("nvidia-smi is unavailable: " + str(error)) from error
    rows = completed.stdout.strip().splitlines()
    if not rows:
        raise BenchmarkError("nvidia-smi returned no GPU rows")
    return [part.strip() for part in rows[0].split(",")]


def read_gpu_memory() -> GpuMemory:
    total, used, free = _nvidia_smi("memory.total,memory.used,memory.free")
    return GpuMemory(total_mb=int(total), used_mb=int(used), free_mb=int(free))


def read_gpu_identity() -> tuple[str, str]:
    name, driver = _nvidia_smi("name,driver_version")
    return name, driver


def read_gpu_processes() -> list[str]:
    """Name the processes already holding VRAM, so the floor is explainable."""

    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-compute-apps=process_name,used_memory",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _request(url: str, payload: dict | None = None, timeout: float = 900.0) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="GET" if data is None else "POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except (urllib.error.URLError, OSError) as error:
        raise BenchmarkError(
            "Ollama request to " + url + " failed: " + str(error)
        ) from error
    try:
        return json.loads(body)
    except json.JSONDecodeError as error:
        raise BenchmarkError("Ollama returned non-JSON from " + url) from error


def read_ollama_version(base_url: str = DEFAULT_OLLAMA_URL) -> str:
    document = _request(base_url + "/api/version", timeout=30)
    return str(document.get("version", "unknown"))


def read_loaded_models(base_url: str = DEFAULT_OLLAMA_URL) -> list[LoadedModel]:
    document = _request(base_url + "/api/ps", timeout=30)
    models: list[LoadedModel] = []
    for entry in document.get("models", []):
        models.append(
            LoadedModel(
                name=str(entry.get("name", "")),
                size_bytes=int(entry.get("size", 0)),
                size_vram_bytes=int(entry.get("size_vram", 0)),
                context_length=int(entry.get("context_length", 0)),
            )
        )
    return models


def list_installed_models(base_url: str = DEFAULT_OLLAMA_URL) -> list[str]:
    document = _request(base_url + "/api/tags", timeout=30)
    return [str(entry.get("name", "")) for entry in document.get("models", [])]


# --------------------------------------------------------------------------
# Residency control
# --------------------------------------------------------------------------


def unload(model: str, base_url: str = DEFAULT_OLLAMA_URL) -> None:
    """Ask the daemon to evict one model immediately."""

    _request(
        base_url + "/api/generate",
        {"model": model, "prompt": "", "keep_alive": 0, "stream": False},
        timeout=180,
    )


def warm(model: str, base_url: str = DEFAULT_OLLAMA_URL, *, keep_alive: str = "30m") -> None:
    """Load one model back onto the card without generating anything.

    An empty prompt makes Ollama load the model and return; ``keep_alive`` keeps
    it resident. Used to hand the card back after a lease displaced the voice's
    model, so her next turn does not pay the cold load. 30m matches the voice's
    own keep_alive (galatea/ollama.py).
    """

    _request(
        base_url + "/api/generate",
        {"model": model, "prompt": "", "keep_alive": keep_alive, "stream": False},
        timeout=180,
    )


def unload_all(base_url: str = DEFAULT_OLLAMA_URL) -> list[str]:
    """Evict every resident model and return the names that were evicted."""

    evicted = [item.name for item in read_loaded_models(base_url)]
    for name in evicted:
        unload(name, base_url)
    return evicted


def await_eviction(
    base_url: str = DEFAULT_OLLAMA_URL,
    *,
    timeout_seconds: float = 90.0,
    settle_seconds: float = 1.5,
) -> GpuMemory:
    """Block until no model is resident, then let the driver's accounting settle.

    Returning early would time a warm start and label it cold, so this raises
    rather than proceeding on an unverified assumption.
    """

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not read_loaded_models(base_url):
            time.sleep(settle_seconds)
            return read_gpu_memory()
        time.sleep(0.5)
    still_resident = [item.name for item in read_loaded_models(base_url)]
    raise BenchmarkError("models still resident after eviction: " + str(still_resident))


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------


def _generate(
    model: str,
    prompt: str,
    *,
    num_ctx: int | None,
    num_predict: int,
    base_url: str,
    keep_alive: str = "5m",
) -> float:
    options: dict[str, int] = {"num_predict": num_predict}
    if num_ctx is not None:
        options["num_ctx"] = num_ctx
    started = time.monotonic()
    _request(
        base_url + "/api/generate",
        {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "keep_alive": keep_alive,
            "options": options,
        },
    )
    return time.monotonic() - started


def _residency(model: str, base_url: str) -> tuple[LoadedModel | None, tuple[str, ...]]:
    """Return the model's own residency and whatever else is holding the card.

    A third party can load a model into the same daemon at any moment. Absorbing
    that silently inflates every driver-measured delta and, once the card is
    full, gets this benchmark's own model evicted mid-measurement. Both facts are
    returned rather than raised, because a contended measurement is a finding.
    """

    mine: LoadedModel | None = None
    foreign: list[str] = []
    for item in read_loaded_models(base_url):
        if item.name == model:
            mine = item
        else:
            foreign.append(item.name)
    return mine, tuple(foreign)


def benchmark_model(
    model: str,
    *,
    contexts: tuple[int, ...] = (4096, 16384),
    base_url: str = DEFAULT_OLLAMA_URL,
    prompt: str = "Reply with the single word: ready.",
) -> ModelBenchmark:
    """Measure one model from a verified-cold start."""

    result = ModelBenchmark(model=model, baseline_context=contexts[0])
    try:
        unload_all(base_url)
        floor = await_eviction(base_url)

        result.cold_start_seconds = _generate(
            model,
            prompt,
            num_ctx=contexts[0],
            num_predict=1,
            base_url=base_url,
        )
        resident, foreign = _residency(model, base_url)
        result.foreign_resident = foreign
        if resident is None:
            # The model answered and was gone by the time residency was read: on
            # a full card the daemon evicts to satisfy the next caller, so this
            # is a capacity result, not a failed measurement. This verdict comes
            # from residency alone - do NOT read the GPU first, or a machine
            # without nvidia-smi (CI) throws here and the eviction is recorded as
            # a plain failure instead of contention.
            result.evicted_by_contention = True
            result.error = (
                model
                + " was evicted immediately after answering; resident instead: "
                + (", ".join(foreign) if foreign else "nothing")
            )
            return result

        loaded = read_gpu_memory()
        result.resident_vram_mb = resident.size_vram_mb
        result.driver_delta_mb = loaded.used_mb - floor.used_mb
        result.fully_on_gpu = resident.fully_on_gpu
        result.contexts.append(
            ContextMeasurement(
                num_ctx=contexts[0],
                size_vram_mb=resident.size_vram_mb,
                driver_delta_mb=result.driver_delta_mb,
                fully_on_gpu=resident.fully_on_gpu,
                foreign_resident=foreign,
            )
        )

        result.warm_latency_seconds = _generate(
            model,
            prompt,
            num_ctx=contexts[0],
            num_predict=16,
            base_url=base_url,
        )

        for num_ctx in contexts[1:]:
            unload_all(base_url)
            inner_floor = await_eviction(base_url)
            _generate(
                model,
                prompt,
                num_ctx=num_ctx,
                num_predict=1,
                base_url=base_url,
            )
            wider, wider_foreign = _residency(model, base_url)
            if wider is None:
                result.evicted_by_contention = True
                continue
            result.contexts.append(
                ContextMeasurement(
                    num_ctx=num_ctx,
                    size_vram_mb=wider.size_vram_mb,
                    driver_delta_mb=read_gpu_memory().used_mb - inner_floor.used_mb,
                    fully_on_gpu=wider.fully_on_gpu,
                    foreign_resident=wider_foreign,
                )
            )

        result.context_mb_per_1k = _context_slope(result.contexts)
    except BenchmarkError as error:
        result.error = str(error)
    finally:
        try:
            unload_all(base_url)
        except BenchmarkError:
            pass
    return result


def _context_slope(measurements: list[ContextMeasurement]) -> float | None:
    """MB of VRAM per 1K context tokens, from the widest measured pair.

    A model that spilled to system RAM reports a VRAM figure capped by the card
    rather than by its true KV growth, so those points cannot be differenced.
    """

    usable = [item for item in measurements if item.fully_on_gpu]
    if len(usable) < 2:
        return None
    low, high = usable[0], usable[-1]
    span = high.num_ctx - low.num_ctx
    if span <= 0:
        return None
    return round((high.size_vram_mb - low.size_vram_mb) / (span / 1024), 2)


def measure_coresidency(
    first: str,
    second: str,
    *,
    num_ctx: int = 4096,
    base_url: str = DEFAULT_OLLAMA_URL,
) -> dict[str, object]:
    """Ask whether two models can hold the GPU at once, rather than assuming.

    ``max_gpu_leases = 1`` is the conservative default. Whether it is a
    necessary constraint or an over-conservative one is a measurement.
    """

    unload_all(base_url)
    floor = await_eviction(base_url)
    prompt = "Reply with the single word: ready."
    try:
        _generate(first, prompt, num_ctx=num_ctx, num_predict=1, base_url=base_url)
        _generate(second, prompt, num_ctx=num_ctx, num_predict=1, base_url=base_url)
        resident = {item.name: item for item in read_loaded_models(base_url)}
        both_resident = first in resident and second in resident
        return {
            "first": first,
            "second": second,
            "num_ctx": num_ctx,
            "both_resident": both_resident,
            "both_fully_on_gpu": both_resident
            and all(resident[name].fully_on_gpu for name in (first, second)),
            "resident": {
                name: {
                    "size_vram_mb": item.size_vram_mb,
                    "fully_on_gpu": item.fully_on_gpu,
                }
                for name, item in resident.items()
            },
            "driver_delta_mb": read_gpu_memory().used_mb - floor.used_mb,
            "floor_used_mb": floor.used_mb,
        }
    finally:
        try:
            unload_all(base_url)
        except BenchmarkError:
            pass


def run(
    models: tuple[str, ...],
    *,
    contexts: tuple[int, ...] = (4096, 16384),
    coresidency: tuple[str, str] | None = None,
    base_url: str = DEFAULT_OLLAMA_URL,
) -> BenchmarkReport:
    """Take a complete Phase 0 measurement."""

    name, driver = read_gpu_identity()
    as_found_memory = read_gpu_memory()
    as_found_models = [item.name for item in read_loaded_models(base_url)]

    unload_all(base_url)
    floor_memory = await_eviction(base_url)

    report = BenchmarkReport(
        taken_at=datetime.now(UTC).isoformat(),
        gpu_name=name,
        driver_version=driver,
        ollama_version=read_ollama_version(base_url),
        total_vram_mb=floor_memory.total_mb,
        floor_vram_mb=floor_memory.used_mb,
        as_found_used_mb=as_found_memory.used_mb,
        as_found_models=as_found_models,
        floor_processes=read_gpu_processes(),
    )
    for model in models:
        report.models.append(
            benchmark_model(model, contexts=contexts, base_url=base_url)
        )
    if coresidency is not None:
        report.coresidency = measure_coresidency(*coresidency, base_url=base_url)
    return report
