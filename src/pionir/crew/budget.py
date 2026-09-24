"""Stay inside the confines: measure VRAM, RAM and calls; never assume.

``boot_check`` decides whether the card can hold the brain at all (its own
baseline load plus the brain's measured footprint must fit) and reports, not
hides, other Ollama models that will contend. ``Monitor`` re-measures every
30 s while running; the snapshot shows the figures and any breach is logged.

Measuring is only reading (nvidia-smi, Ollama's ``/api/ps``), so it never
waits on the GPU lock. Warming is a model call - it loads gemma onto the card -
so ``warm_brain`` stands down for the lock exactly as the brain does: while
Daedalus has the card, loading gemma would evict the coder mid-job.
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import threading
import time
import urllib.request

from .brain import Post, http_post_json
from .gpu import CardWatch
from .log import lesion, log, safe

GB = 1024 ** 3
MIB = 1024 ** 2
WARM_TIMEOUT_S = 180


class CardBusy(RuntimeError):
    """Warming was stopped while another tenant held the card."""


def nvidia_smi() -> dict | None:
    """Total/used/free MiB for GPU 0, or None if nvidia-smi is unavailable."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout.strip().splitlines()
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("nvidia-smi unavailable: %s", exc)
        return None
    if not out:
        return None
    name, total, used, free = [s.strip() for s in out[0].split(",")]
    return {"name": name, "total_mib": int(total), "used_mib": int(used), "free_mib": int(free)}


def ollama_ps(url: str) -> list:
    """Models Ollama has loaded right now, with their VRAM bytes."""
    with urllib.request.urlopen(f"{url}/api/ps", timeout=5) as r:
        data = json.loads(r.read())
    return [
        {"name": m.get("name"), "size": m.get("size", 0), "size_vram": m.get("size_vram", 0),
         "expires_at": m.get("expires_at"), "context": (m.get("context_length") or 0)}
        for m in data.get("models", [])
    ]


def process_rss_mb() -> float:
    """Resident set of this process, via psapi (stdlib only)."""
    if os.name != "nt":
        try:
            import resource  # type: ignore
            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
        except Exception as exc:  # noqa: BLE001 - measurement only, reported as unknown
            log.warning("cannot measure RSS: %s", exc)
            return -1.0

    import ctypes.wintypes as wt

    class PMC(ctypes.Structure):
        _fields_ = [
            ("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    k32 = ctypes.windll.kernel32
    fn = k32.K32GetProcessMemoryInfo
    fn.argtypes = [wt.HANDLE, ctypes.POINTER(PMC), wt.DWORD]
    fn.restype = wt.BOOL
    k32.GetCurrentProcess.restype = wt.HANDLE
    pmc = PMC()
    pmc.cb = ctypes.sizeof(PMC)
    if fn(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
        return pmc.WorkingSetSize / MIB
    return -1.0


def warm_brain(cfg, *, card: CardWatch | None = None, stop: threading.Event | None = None,
               post: Post = http_post_json) -> dict:
    """Load the model (a one-token call) so its real footprint can be read, not predicted.
    Stands down first while anybody holds the GPU lock; raises CardBusy if ``stop`` is set
    while waiting, so no load is ever attempted over another tenant's exclusive use."""
    card = card or CardWatch(cfg.gpu_lock_path, poll_seconds=cfg.gpu_poll_seconds)
    if not card.wait_until_free(stop or threading.Event(), waiter="warm"):
        raise CardBusy(f"stopped while the card was held; {cfg.model} was not loaded")
    body = {"model": cfg.model, "prompt": "hi", "stream": False, "keep_alive": cfg.keep_alive,
            "options": {"num_ctx": cfg.num_ctx, "num_predict": 1}}
    return post(f"{cfg.ollama_url.rstrip('/')}/api/generate", body, WARM_TIMEOUT_S)


def boot_check(cfg, *, card: CardWatch | None = None,
               stop: threading.Event | None = None) -> dict:
    """Can this card hold the brain? Measured, not predicted: warm the model, then read where
    it landed. Refuses only when Ollama is unreachable or the model did not fit fully on the
    GPU."""
    smi = nvidia_smi()
    loaded = safe("budget.ollama_ps", lambda: ollama_ps(cfg.ollama_url), default=None)
    verdict = {"ok": True, "reasons": [], "warnings": [], "gpu": smi, "loaded": loaded or []}
    if loaded is None:
        verdict["ok"] = False
        verdict["reasons"].append(f"Ollama is not answering at {cfg.ollama_url}; start Ollama first.")
        return verdict
    if smi is None:
        verdict["warnings"].append("nvidia-smi not found; VRAM cannot be measured, running blind.")
    t0 = time.time()
    try:
        warmed = warm_brain(cfg, card=card, stop=stop)
    except CardBusy as exc:
        # Not a missing model: say what actually happened.
        verdict["ok"] = False
        verdict["reasons"].append(str(exc))
        return verdict
    except Exception as exc:  # noqa: BLE001 - recorded as a lesion and reported below
        lesion("budget.warm_brain", exc)
        warmed = None
    if warmed is None:
        verdict["ok"] = False
        verdict["reasons"].append(
            f"{cfg.model} could not be loaded; is it pulled? (ollama pull {cfg.model})")
        return verdict
    verdict["warm_seconds"] = round(time.time() - t0, 1)
    loaded = safe("budget.ollama_ps", lambda: ollama_ps(cfg.ollama_url), default=[]) or []
    verdict["loaded"] = loaded
    ours = [m for m in loaded if m["name"] == cfg.model]
    others = [m for m in loaded if m["name"] != cfg.model]
    if not ours:
        verdict["warnings"].append(
            f"{cfg.model} answered but is not listed as loaded; cannot verify its footprint.")
    else:
        m = ours[0]
        on_gpu = m["size_vram"] / max(1, m["size"])
        verdict["brain_gb"] = round(m["size_vram"] / GB, 2)
        if on_gpu < 0.98:
            verdict["ok"] = False
            verdict["reasons"].append(
                f"{cfg.model} did not fit on the card: only {on_gpu:.0%} of it is in VRAM "
                f"({m['size_vram'] / GB:.1f} of {m['size'] / GB:.1f} GB); the rest would run on "
                f"the CPU, slowly. Free some VRAM (other models, GPU-heavy apps) and try again.")
        elif m["size_vram"] / GB > cfg.budget.model_gb:
            verdict["warnings"].append(
                f"{cfg.model} takes {m['size_vram'] / GB:.2f} GB, over the declared "
                f"{cfg.budget.model_gb} GB budget.")
        else:
            verdict["warnings"].append(
                f"brain warm: {cfg.model} at {m['size_vram'] / GB:.2f} GB, fully on the GPU, "
                f"loaded in {verdict['warm_seconds']} s.")
    smi = nvidia_smi() or smi
    verdict["gpu"] = smi
    if others:
        names = ", ".join(f"{m['name']} ({m['size_vram'] / GB:.1f} GB)" for m in others)
        verdict["warnings"].append(
            f"Other Ollama models are also loaded ({names}); if the card is short, Ollama evicts "
            f"one whenever the other is called and the snapshot shows 'brain cold'.")
    if smi and smi["used_mib"] / 1024 > cfg.budget.card_gb:
        mine = (ours[0]["size_vram"] / GB) if ours else 0.0
        verdict["warnings"].append(
            f"the card is at {smi['used_mib'] / 1024:.1f} GB, over the declared "
            f"{cfg.budget.card_gb} GB line ({mine:.2f} GB of it is the brain; the rest is other "
            f"software on this machine).")
    return verdict


class Monitor:
    """Re-measures every ``interval`` seconds on its own thread; the sim reads ``latest``."""

    def __init__(self, cfg, store, interval: float = 30.0) -> None:
        self.cfg = cfg
        self.store = store
        self.interval = interval
        self.latest: dict = {"measured_at": 0}
        self.breaches = 0
        self.last_kinds: set = set()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="pionir-crew-budget", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def measure(self) -> dict:
        smi = nvidia_smi()
        loaded = safe("budget.ollama_ps", lambda: ollama_ps(self.cfg.ollama_url), default=[])
        ours = next((m for m in loaded if m["name"] == self.cfg.model), None)
        rss = process_rss_mb()
        calls_hour = self.store.calls_last_hour() if self.store else 0
        b = self.cfg.budget
        m = {
            "measured_at": time.time(),
            "gpu_name": smi["name"] if smi else None,
            "card_total_gb": round(smi["total_mib"] / 1024, 2) if smi else None,
            "card_used_gb": round(smi["used_mib"] / 1024, 2) if smi else None,
            "brain_warm": ours is not None,
            "brain_vram_gb": round(ours["size_vram"] / GB, 2) if ours else 0.0,
            "brain_ctx": ours["context"] if ours else None,
            "other_models": [{"name": x["name"], "gb": round(x["size_vram"] / GB, 2)}
                             for x in loaded if x is not ours],
            "sim_rss_mb": round(rss, 1),
            "calls_last_hour": calls_hour,
            "limits": {"model_gb": b.model_gb, "card_gb": b.card_gb,
                       "calls_per_hour": b.calls_per_hour, "sim_rss_mb": b.sim_rss_mb},
            "breaches": self.breaches,
        }
        problems, kinds = [], set()
        if ours and ours["size_vram"] / GB > b.model_gb:
            problems.append(f"brain {m['brain_vram_gb']} GB > {b.model_gb} GB")
            kinds.add("brain")
        if smi and smi["used_mib"] / 1024 > b.card_gb:
            mine = (ours["size_vram"] / GB) if ours else 0.0
            problems.append(f"card {m['card_used_gb']} GB > {b.card_gb} GB "
                            f"({mine:.2f} GB ours, {m['card_used_gb'] - mine:.2f} GB other software)")
            kinds.add("card")
        if rss > b.sim_rss_mb:
            problems.append(f"sim RSS {rss:.0f} MB > {b.sim_rss_mb} MB")
            kinds.add("rss")
        if calls_hour > b.calls_per_hour:
            problems.append(f"calls {calls_hour}/h > {b.calls_per_hour}/h")
            kinds.add("calls")
        if problems:
            self.breaches += 1
            m["breaches"] = self.breaches
            # edge-triggered on the KIND of breach: the measured number changes every poll, so
            # comparing the messages logged every time and the guard did nothing
            if kinds != self.last_kinds:
                if kinds == {"card"}:
                    # the card is full but nothing of ours is over: other software did this
                    log.warning("CARD FULL (not the crew's doing): %s", "; ".join(problems))
                else:
                    log.warning("BUDGET BREACH: %s", "; ".join(problems))
        elif self.last_kinds:
            log.info("budget back inside its limits")
        self.last_kinds = kinds
        m["problems"] = problems
        self.latest = m
        return m

    def _run(self) -> None:
        while not self._stop.is_set():
            safe("budget.measure", self.measure)
            self._stop.wait(self.interval)
