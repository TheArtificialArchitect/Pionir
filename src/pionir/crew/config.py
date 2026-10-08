"""The crew's settings: the one model, the budgets, the pool, and where state lives.

State belongs under Pionir's state root (``state_root/crew/``). Settings come from the
environment the way ``PionirSettings`` does, and the GPU lock path is Pionir's own, so
the crew and the scheduler can never disagree about which file is the lock.

Two budgets bound what the crew may spend, and both are compute: the shared brain's
hourly model-call ceiling (``budget.calls_per_hour``) and the daily cap on Claude
escalations (``claude_daily_cap``, default 10 - each ``claude -p`` loads the full Claude
Code system prompt, and ~505 such calls once exhausted a 5-hour usage window). Moss
divides both between divisions (direction.py). There is no money setting: nothing in
this package can spend money.

The VRAM numbers are the ones Hearth measured for gemma3:12b at num_ctx 8192 on this
card; they are enforced at boot and watched while running.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..adapters.daedalus import DEFAULT_SANDBOX_ROOT
from ..config import PionirSettings
from .affiliate import programs_from_environment
from .escalation import DEFAULT_CLAUDE_MODEL, DEFAULT_NIGHT_CAP, clean_model

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"


@dataclass(frozen=True, slots=True)
class CrewBudget:
    model_gb: float = 8.6        # the one model, at num_ctx 8192 (measured in Hearth)
    card_gb: float = 11.0        # whole card while the crew runs
    calls_per_hour: int = 80     # crew-wide model calls per real hour
    process_rss_mb: int = 500    # the process the crew runs in

    def __post_init__(self) -> None:
        if self.calls_per_hour <= 0:
            raise ValueError("calls_per_hour must be positive")


@dataclass(frozen=True, slots=True)
class CrewSettings:
    state_dir: Path
    gpu_lock_path: Path
    # The same model Moss speaks with. Ollama loads it once and both share it,
    # which is why the crew must never take the GPU lock (see gpu.py).
    model: str = "gemma3:12b"
    num_ctx: int = 8192
    ollama_url: str = DEFAULT_OLLAMA_URL
    keep_alive: str = "30m"
    # How often the runtime looks for due workers and leaders. There is no time
    # compression: the crew does real work on the wall clock.
    tick_seconds: float = 1.0
    checkpoint_seconds: float = 300.0
    # How often the brain looks at the lock again while it is standing down.
    gpu_poll_seconds: float = 2.0
    # A model request still queued after this long is expired, counted and reported to its
    # owner (brain.EXPIRED) rather than spoken late.
    request_ttl_seconds: float = 600.0
    # The crew is its own process and reaches Pionir over loopback HTTP, as Moss does.
    pionir_url: str = "http://127.0.0.1:8780"
    # How long the hands keep following a job Pionir reports as still running, and how long
    # each follow-up call may wait on Pionir.
    job_follow_seconds: float = 600.0
    job_poll_seconds: float = 20.0
    budget: CrewBudget = field(default_factory=CrewBudget)
    # How many workers run at once, and how many leaders (leaders mostly wait on the brain).
    pool_size: int = 4
    leader_pool_size: int = 2
    # Claude escalations per local day across ALL leaders. 0 turns escalation off.
    claude_daily_cap: int = 10
    # Overnight Claude work (the Builds division's review, the API builder's review and edit)
    # is held tighter: at most this many calls a night (noon to noon), INSIDE the daily cap,
    # on this model (passed to `claude --model`; "default" = the CLI's own default).
    # PIONIR_CREW_CLAUDE_NIGHT_CAP / PIONIR_CREW_CLAUDE_MODEL override.
    claude_night_cap: int = DEFAULT_NIGHT_CAP
    claude_model: str = DEFAULT_CLAUDE_MODEL
    # How many of the daily cap the LEADERS' escalations may take between them; the rest is
    # kept for workers' real work (research, builds, reviews). None: 40% of the daily cap
    # (escalation.default_leader_cap). PIONIR_CREW_CLAUDE_LEADER_CAP overrides.
    claude_leader_cap: int | None = None
    escalation_timeout_seconds: float = 300.0
    # The loopback HTTP API Moss reaches the crew through (api.py), via Pionir's
    # ``crew.*`` capabilities. Bound to 127.0.0.1 only. 0 takes a free port (tests);
    # None runs the crew without it. PIONIR_CREW_API_PORT overrides ("off" = None).
    api_port: int | None = 8782
    # Where workers find the secrets they read (the Scrooge read token). None -> ~/.pionir/secrets
    secrets_dir: Path | None = None
    # The crew's own client token for Pionir's API (pionir/auth.py): sent as a bearer on
    # every call to Pionir, and required of every write to the crew API. Set from Pionir's
    # client token dir by from_pionir; None (a bare settings, a test) sends none.
    pionir_token_file: Path | None = None
    # The catalogue of divisions and workers. None -> the packaged catalogue.json
    catalogue_path: Path | None = None
    # Where the owner drops each paid order's finished work, one folder per order
    # (``<deliveries_dir>/<order_id>/<name>.zip``); contracts.delivery ships it through
    # Pionir, which reads the same folder. None -> ~/.pionir/deliveries
    deliveries_dir: Path | None = None
    # Where the owner stages each product for sale on Gumroad, one folder per product
    # (``<products_dir>/<slug>/`` with listing.json, the zip and the cover); products.shelf
    # submits it through Pionir, which reads the same folder. None -> ~/.pionir/products
    products_dir: Path | None = None
    # The owner's affiliate programs (affiliate.py), from PIONIR_AFFILIATE_*: the finder puts
    # his tag on the shop links they cover, and discloses it. Empty: nothing is rewritten.
    affiliates: tuple = ()
    # The Fiverr desk's folder (crew/fiverr): each gig's listing and image
    # (``gigs/<service>/``), each order's inputs the owner drops (``orders/<n>/input/``) and the
    # files prepared for him to upload on Fiverr (``orders/<n>/out/``). Pionir's fiverr.card
    # reads the same folder. None -> ~/.pionir/fiverr
    fiverr_dir: Path | None = None
    # The API builder's backlog and worktrees (crew/apibuild), and the Scrooge repository it
    # stages ``api/<id>`` branches in. None -> ~/.pionir/apibuilds and C:/src/Scrooge
    apibuilds_dir: Path | None = None
    scrooge_repo: Path | None = None
    # When the owner's daily digest is (pionir.batching.DigestSettings, read by from_pionir
    # from the same place Pionir's server reads it). The daily posters draft in time for it,
    # so each day's post is in that morning's digest. None: they draft every 24 hours.
    digest: object = None
    # The Builds division (crew/builds): its folder (``backlog.json``, the product ideas it
    # builds from; the owner edits it by replying to its cards) and its sandbox workspace,
    # where each product gets a fresh git repo that Daedalus builds in overnight - the only
    # place Pionir's coding.daedalus_build reaches (PIONIR_DAEDALUS_SANDBOX, read by Pionir
    # too). None -> ~/.pionir/builds and C:\src\daedalus-work.
    builds_dir: Path | None = None
    builds_sandbox: Path | None = None
    # The Bolts ledger's folder, and Pionir's approvals queue file (read by the treasury.bolts
    # worker, never written). from_pionir sets both from Pionir's settings (~/.pionir/economy,
    # state_root/approvals/queue.json); None here -> under state_dir, so a test crew never
    # reaches the owner's real files.
    economy_dir: Path | None = None
    approvals_path: Path | None = None

    def __post_init__(self) -> None:
        if self.tick_seconds <= 0:
            raise ValueError("tick_seconds must be positive")
        if self.checkpoint_seconds <= 0:
            raise ValueError("checkpoint_seconds must be positive")
        if self.gpu_poll_seconds <= 0:
            raise ValueError("gpu_poll_seconds must be positive")
        for name in ("request_ttl_seconds", "job_follow_seconds", "job_poll_seconds",
                     "escalation_timeout_seconds"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.num_ctx <= 0:
            raise ValueError("num_ctx must be positive")
        if not self.model:
            raise ValueError("the crew needs a model")
        if self.pool_size <= 0 or self.leader_pool_size <= 0:
            raise ValueError("pool sizes must be positive")
        if self.claude_daily_cap < 0:
            raise ValueError("claude_daily_cap cannot be negative")
        if self.claude_night_cap < 0:
            raise ValueError("claude_night_cap cannot be negative")
        if self.claude_leader_cap is not None and self.claude_leader_cap < 0:
            raise ValueError("claude_leader_cap cannot be negative")
        clean_model(self.claude_model)
        if self.api_port is not None and (isinstance(self.api_port, bool)
                                          or not 0 <= self.api_port <= 65535):
            raise ValueError("api_port is a TCP port (0-65535), or None for no API")
        if self.secrets_dir is None:
            object.__setattr__(self, "secrets_dir", Path.home() / ".pionir" / "secrets")
        if self.deliveries_dir is None:
            object.__setattr__(self, "deliveries_dir", Path.home() / ".pionir" / "deliveries")
        if self.products_dir is None:
            object.__setattr__(self, "products_dir", Path.home() / ".pionir" / "products")
        if self.fiverr_dir is None:
            object.__setattr__(self, "fiverr_dir", Path.home() / ".pionir" / "fiverr")
        if self.builds_dir is None:
            object.__setattr__(self, "builds_dir", Path.home() / ".pionir" / "builds")
        if self.builds_sandbox is None:
            object.__setattr__(self, "builds_sandbox", Path(DEFAULT_SANDBOX_ROOT))
        if self.apibuilds_dir is None:
            object.__setattr__(self, "apibuilds_dir", Path.home() / ".pionir" / "apibuilds")
        if self.scrooge_repo is None:
            object.__setattr__(self, "scrooge_repo", Path("C:/src/Scrooge"))
        if self.economy_dir is None:
            object.__setattr__(self, "economy_dir", Path(self.state_dir) / "economy")
        if self.approvals_path is None:
            object.__setattr__(self, "approvals_path",
                               Path(self.state_dir) / "approvals" / "queue.json")

    @classmethod
    def from_pionir(cls, settings: PionirSettings, **overrides) -> CrewSettings:
        """Derive the crew's paths from Pionir's: state under ``state_root/crew``,
        and the very lock file the scheduler takes."""
        overrides.setdefault("state_dir", settings.state_root / "crew")
        overrides.setdefault("gpu_lock_path", settings.gpu_lock_path)
        overrides.setdefault("economy_dir", settings.economy_path)
        overrides.setdefault("approvals_path", settings.approvals_queue_path)
        from ..auth import token_path
        overrides.setdefault("pionir_token_file", token_path(settings.client_token_path, "crew"))
        if "digest" not in overrides:
            from ..batching import DigestSettings
            overrides["digest"] = DigestSettings.from_environment(settings.state_root)
        return cls(**overrides)

    @classmethod
    def from_environment(cls, settings: PionirSettings | None = None) -> CrewSettings:
        settings = settings or PionirSettings.from_environment()
        overrides: dict = {}
        state_dir = os.environ.get("PIONIR_CREW_STATE_DIR", "").strip()
        if state_dir:
            overrides["state_dir"] = Path(state_dir)
        for env, key, cast in (
            ("PIONIR_CREW_MODEL", "model", str),
            ("PIONIR_CREW_NUM_CTX", "num_ctx", int),
            ("PIONIR_CREW_OLLAMA_URL", "ollama_url", str),
            ("PIONIR_CREW_KEEP_ALIVE", "keep_alive", str),
            ("PIONIR_CREW_TICK_SECONDS", "tick_seconds", float),
            ("PIONIR_CREW_CHECKPOINT_SECONDS", "checkpoint_seconds", float),
            ("PIONIR_CREW_GPU_POLL_SECONDS", "gpu_poll_seconds", float),
            ("PIONIR_CREW_REQUEST_TTL_SECONDS", "request_ttl_seconds", float),
            ("PIONIR_CREW_POOL_SIZE", "pool_size", int),
            ("PIONIR_CREW_CLAUDE_DAILY_CAP", "claude_daily_cap", int),
            ("PIONIR_CREW_CLAUDE_NIGHT_CAP", "claude_night_cap", int),
            ("PIONIR_CREW_CLAUDE_LEADER_CAP", "claude_leader_cap", int),
            ("PIONIR_CREW_CLAUDE_MODEL", "claude_model", str),
            ("PIONIR_CREW_SECRETS_DIR", "secrets_dir", Path),
            ("PIONIR_CREW_CATALOGUE", "catalogue_path", Path),
            ("PIONIR_CREW_DELIVERIES_DIR", "deliveries_dir", Path),
            ("PIONIR_CREW_PRODUCTS_DIR", "products_dir", Path),
            ("PIONIR_FIVERR_DIR", "fiverr_dir", Path),
            ("PIONIR_CREW_BUILDS_DIR", "builds_dir", Path),
            ("PIONIR_DAEDALUS_SANDBOX", "builds_sandbox", Path),
            ("PIONIR_APIBUILDS_DIR", "apibuilds_dir", Path),
            ("PIONIR_SCROOGE_REPO", "scrooge_repo", Path),
            ("PIONIR_CREW_PIONIR_URL", "pionir_url", str),
            ("PIONIR_CREW_JOB_FOLLOW_SECONDS", "job_follow_seconds", float),
        ):
            raw = os.environ.get(env, "").strip()
            if raw:
                overrides[key] = cast(raw)
        raw = os.environ.get("PIONIR_CREW_API_PORT", "").strip()
        if raw:
            overrides["api_port"] = None if raw.lower() in {"off", "none"} else int(raw)
        raw = os.environ.get("PIONIR_CREW_CALLS_PER_HOUR", "").strip()
        if raw:
            overrides["budget"] = CrewBudget(calls_per_hour=int(raw))
        overrides["affiliates"] = programs_from_environment(os.environ)
        return cls.from_pionir(settings, **overrides)

    def public(self) -> dict:
        d = asdict(self)
        d["state_dir"] = str(self.state_dir)
        d["gpu_lock_path"] = str(self.gpu_lock_path)
        d["secrets_dir"] = str(self.secrets_dir)
        d["deliveries_dir"] = str(self.deliveries_dir)
        d["products_dir"] = str(self.products_dir)
        d["fiverr_dir"] = str(self.fiverr_dir)
        d["apibuilds_dir"] = str(self.apibuilds_dir)
        d["scrooge_repo"] = str(self.scrooge_repo)
        d["pionir_token_file"] = str(self.pionir_token_file) if self.pionir_token_file else None
        d["builds_dir"] = str(self.builds_dir)
        d["economy_dir"] = str(self.economy_dir)
        d["approvals_path"] = str(self.approvals_path)
        d["builds_sandbox"] = str(self.builds_sandbox)
        d["catalogue_path"] = str(self.catalogue_path) if self.catalogue_path else None
        d["affiliates"] = [p.public() for p in self.affiliates]
        digest = self.digest
        d["digest"] = ({"enabled": bool(digest.enabled), "time": digest.time_text}
                       if digest is not None else None)
        return d


def ollama_env() -> dict:
    """The machine-wide Ollama settings the VRAM measurement depended on."""
    keys = (
        "OLLAMA_MAX_LOADED_MODELS",
        "OLLAMA_KV_CACHE_TYPE",
        "OLLAMA_FLASH_ATTENTION",
        "OLLAMA_KEEP_ALIVE",
        "OLLAMA_NUM_PARALLEL",
    )
    return {k: os.environ.get(k) for k in keys}
