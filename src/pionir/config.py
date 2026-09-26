"""Environment-backed Pionir configuration with private runtime-state defaults."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from .scheduler import ResourceBudget


def _default_state_root() -> Path:
    """Resolve the private state directory, or say exactly how to configure it."""

    try:
        home = Path.home()
    except RuntimeError as error:
        raise ValueError(
            "Pionir cannot determine a home directory; set PIONIR_STATE_ROOT to an "
            "absolute path for Pionir's private runtime state"
        ) from error
    return home / ".pionir"


def _declared(name: str) -> Any:
    """Read a declared default without constructing PionirSettings.

    Constructing one would resolve the state-root default even when
    PIONIR_STATE_ROOT already says where private state belongs.
    """

    for item in fields(PionirSettings):
        if item.name == name:
            return item.default
    raise KeyError(name)


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _command_from_json(
    name: str, default: tuple[str, ...] | None
) -> tuple[str, ...] | None:
    raw = os.environ.get(name)
    if raw is None:
        return default
    document = json.loads(raw)
    if not isinstance(document, list) or not document or not all(
        isinstance(item, str) and item for item in document
    ):
        raise ValueError(f"{name} must be a non-empty JSON string list")
    return tuple(document)


def _optional_url(name: str, default: str | None) -> str | None:
    """A loopback service URL, or None to leave it unregistered.

    Unset keeps the default (the service is wired in at its known port); an
    empty value or "off"/"none" turns it off. Used for the specialists that are
    on by default - Daedalus and Melete - so the whole roster is present without
    env fiddling, but any of them can be switched off.
    """

    raw = os.environ.get(name)
    if raw is None:
        return default
    raw = raw.strip()
    if raw == "" or raw.lower() in {"off", "none", "false", "0"}:
        return None
    return raw


def _embed_model_from_env(default: str = "nomic-embed-text") -> str | None:
    """PIONIR_EMBED_MODEL: a model name, or "" / "off" / "none" to disable
    hybrid recall entirely. Unset keeps the default (embeddings on, fail-open)."""
    raw = os.environ.get("PIONIR_EMBED_MODEL")
    if raw is None:
        return default
    raw = raw.strip()
    if raw == "" or raw.lower() in {"off", "none", "false", "0"}:
        return None
    return raw


def _protected_models_from_env(default: tuple[str, ...]) -> tuple[str, ...]:
    """PIONIR_PROTECTED_MODELS: comma-separated model names the scheduler never
    evicts. Unset keeps the default; an empty value protects nothing."""
    raw = os.environ.get("PIONIR_PROTECTED_MODELS")
    if raw is None:
        return default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True, slots=True)
class PionirSettings:
    state_root: Path = field(default_factory=_default_state_root)
    total_vram_mb: int = 12_288
    # The observed idle floor on the target workstation, not an estimate.
    # See docs/PHASE0_BENCHMARK.md; ResourceBudget carries the same figure.
    reserved_vram_mb: int = 1_830
    circuit_failure_threshold: int = 3
    circuit_recovery_seconds: float = 30.0
    # Sideline an idle resident model to make room for an on-demand doer (a 12B
    # voice and a 7B coder cannot share a 12 GB card). On in production; tests
    # set it off so the suite never unloads a live model.
    evict_to_fit: bool = True

    atani_command: tuple[str, ...] = ("atani",)
    # Bryo is Pionir's now: the organism is wired in by default as its read-only
    # observer organ. `python -m bryo.status` resolves the package only from the
    # terrarium tree (Bryo is not installed), so the command runs there; doctor
    # shows him unavailable if the tree or process is gone. Turn off with
    # PIONIR_BRYO_STATUS_COMMAND_JSON set to "off".
    bryo_status_command: tuple[str, ...] | None = ("python", "-m", "bryo.status")
    bryo_status_cwd: str = r"C:\src\terrarium"
    # Consult Bryo's felt pressure before heavy GPU work (advisory, fail-open, never
    # blocking). Needs bryo_status_command. Turn off with PIONIR_BRYO_PRESSURE=off.
    bryo_pressure: bool = True
    # Nyx (offensive) and Voodoo (defensive), Pionir's read-only security organs.
    # Wired in by default: `nyx status` is an installed console script; Voodoo's
    # editable install isn't importable, so `python -m voodoo status` runs from
    # its src tree. Each shows unavailable in doctor if its tree/CLI is gone, and
    # either is turned off with PIONIR_NYX/VOODOO_STATUS_COMMAND_JSON set to "off".
    nyx_status_command: tuple[str, ...] | None = ("nyx", "status")
    voodoo_status_command: tuple[str, ...] | None = ("python", "-m", "voodoo", "status")
    voodoo_status_cwd: str = r"C:\src\voodoo\src"
    # Taskable actions: Atani may invoke one of these (privileged, so it lands in
    # the approval queue and never fires on the voice's own initiative). The
    # action is an allowlisted subcommand (one token, or the explicit two-token
    # `defend <sub>` for Voodoo); its args are shape-checked argv, never a shell.
    # Verified against the real CLIs' --help (2026-09-14): Nyx's top-level
    # offensive commands are research/crawl/fingerprint/cert (`scan` is `nyx
    # improve scan`, `specialists` needs list|run); Voodoo's `hunt` is `defend
    # hunt`, and bare `defend` errors, so each defend posture is spelled out.
    nyx_run_prefix: tuple[str, ...] = ("nyx",)
    nyx_run_actions: tuple[str, ...] = ("research", "crawl", "fingerprint", "cert")
    voodoo_run_prefix: tuple[str, ...] = ("python", "-m", "voodoo")
    voodoo_run_actions: tuple[str, ...] = field(default=(
        "scan", "headers", "cert", "vpn",
        "defend posture", "defend baseline", "defend drift",
        "defend secrets", "defend triage", "defend hunt",
    ), repr=False)
    # Galatea, Pionir's conversational voice. Opt-in: unset means no voice is
    # registered and plain conversation asks rather than routes, exactly as the
    # other specialists register only when configured. The URL is her loopback
    # server; the model id is an optional pin, otherwise resolved live from her
    # /api/settings so a promotion never leaves Pionir's VRAM figure stale.
    galatea_url: str | None = None
    galatea_model_id: str | None = None
    # Daedalus (coder) and Melete (tool-executor) are Theo's own HTTP services,
    # wired in by default at their known loopback ports so the whole roster is
    # present; each shows unavailable in doctor when its server is not running,
    # and either can be turned off with PIONIR_DAEDALUS_URL / PIONIR_MELETE_URL
    # set to "off". Tokens are read only if those services were started with one.
    daedalus_url: str | None = "http://127.0.0.1:8771"
    daedalus_token: str | None = field(default=None, repr=False)
    melete_url: str | None = "http://127.0.0.1:8770"
    melete_token: str | None = field(default=None, repr=False)
    # The crew (`python -m pionir.crew`, its own foreground process) serves its
    # Direction API on loopback; Moss reads and directs it through the crew.*
    # capabilities, so every direction change is gated and audited here. Wired in
    # by default like Daedalus/Melete; a crew that is not running answers "the crew
    # is not running" at once. PIONIR_CREW_URL set to "off" leaves it unregistered.
    crew_url: str | None = "http://127.0.0.1:8782"
    # owner.notify: Moss's daily brief and rare alerts to the owner, as one plain message
    # in the Discord gate's channel with the gate's bot token (read at send time: no
    # network at boot). Rate-limited in Pionir (2 briefs, 4 alerts per rolling 24 h).
    # Unavailable when the Discord gate is not configured; PIONIR_OWNER_NOTIFY=0 leaves it
    # unregistered. On in from_environment (the live path); off in a bare PionirSettings,
    # so a runtime built in a test is never wired to the real bot token and channel.
    owner_notify: bool = False
    # Scrooge's publish endpoint for the content.* capabilities. content.publish parks
    # for the owner's yes on every call; the token is read from content_token_file
    # (None means ~/.pionir/secrets/scrooge-publish-token.txt) at call time, so no
    # network is touched at boot. PIONIR_CONTENT_URL set to "off" leaves it unregistered;
    # PIONIR_CONTENT_TOKEN_FILE points at another token file.
    content_url: str | None = "https://api.dokaz.net"
    content_token_file: Path | None = None
    # Scrooge's ops token for the client.* capabilities (the paid client orders on the
    # same content_url): client.email parks for the owner's yes on every call. None means
    # ~/.pionir/secrets/scrooge-ops-token.txt (written by Scrooge's
    # tools/setup-ops-token.ps1), read at call time: no network at boot. Registered only
    # when content_url is on; PIONIR_OPS_TOKEN_FILE points at another token file.
    ops_token_file: Path | None = None
    # Where the owner drops each order's finished zip for client.deliver:
    # <deliveries_dir>/<order_id>/<name>.zip. None means ~/.pionir/deliveries;
    # PIONIR_DELIVERIES_DIR points elsewhere.
    deliveries_dir: Path | None = None
    # The Instagram Graph API (Instagram API with Instagram Login) for
    # social.instagram_post, which parks for the owner's yes on every call. It also needs
    # Scrooge (content_url) to host the card image, so it is registered only when both are
    # on. The token lives in instagram_token_file (None means
    # ~/.pionir/secrets/instagram.json, written by tools/setup-instagram.ps1) and is read at
    # call time: no network at boot. PIONIR_INSTAGRAM_GRAPH_URL set to "off" leaves it
    # unregistered; PIONIR_INSTAGRAM_TOKEN_FILE points at another token file.
    instagram_graph_url: str | None = "https://graph.instagram.com/v25.0"
    instagram_token_file: Path | None = None
    # The dev.to (Forem) API for content.crosspost_devto, which republishes a blog post that
    # is already live and parks for the owner's yes on every call. It checks the original
    # on the blog (content_url) first, so it is registered only when both are on. The API
    # key lives in devto_key_file (None means ~/.pionir/secrets/devto-api-key.txt, written by
    # tools/setup-devto.ps1) and is read at call time: no network at boot. The ledger of
    # cross-posted drafts is <state_root>/devto/posts.json. PIONIR_DEVTO_URL set to "off"
    # leaves it unregistered; PIONIR_DEVTO_KEY_FILE points at another key file.
    devto_url: str | None = "https://dev.to/api"
    devto_key_file: Path | None = None
    # The Gumroad API for the product.* capabilities. product.gumroad_publish puts a staged
    # product on sale and parks for the owner's yes on every call. The token lives in
    # gumroad_token_file (None means ~/.pionir/secrets/gumroad-token.txt, written by
    # tools/setup-gumroad.ps1) and is read at call time: no network at boot. Products are
    # staged at <products_dir>/<slug>/ (None means ~/.pionir/products). PIONIR_GUMROAD_URL
    # set to "off" leaves it unregistered; PIONIR_GUMROAD_TOKEN_FILE and
    # PIONIR_PRODUCTS_DIR point elsewhere.
    gumroad_url: str | None = "https://api.gumroad.com/v2"
    gumroad_token_file: Path | None = None
    products_dir: Path | None = None
    specialists_file: Path | None = None
    shared_gpu_lock_file: Path | None = None
    # The local embedding model for hybrid recall. Default on: it is ~0.32 GB and
    # fail-open, so if it is not pulled or Ollama is down, recall silently uses
    # BM25 alone. Set PIONIR_EMBED_MODEL to "" or "off" to disable it outright.
    embed_model: str | None = "nomic-embed-text"
    # The chat model `pionir consolidate` distils turns with. It must be a model
    # that can chat: the embed model above cannot, and defaulting to it meant
    # consolidation silently never happened. PIONIR_DISTIL_MODEL overrides.
    distil_model: str = "qwen3:4b-instruct-2507-q4_K_M"
    # Resident models the scheduler must never evict to make room - the voice's
    # model above all, since Galatea never takes the shared lock and an eviction
    # mid-sentence cuts her off. PIONIR_PROTECTED_MODELS is a comma list; set it
    # to "" to protect nothing.
    protected_models: tuple[str, ...] = ("gemma3:12b",)
    # When another Pionir-compatible process (Bryo's governor) holds the shared
    # GPU lease, wait up to this long for it before refusing a GPU task, rather
    # than refusing at once. The worker stays blocked, so the job keeps its
    # "running" status and 202/poll clients see progress. PIONIR_GPU_LOCK_WAIT_SECONDS
    # overrides; 0 restores the old immediate refusal.
    gpu_lock_wait_seconds: float = 600.0

    def __post_init__(self) -> None:
        if self.gpu_lock_wait_seconds < 0:
            raise ValueError("gpu_lock_wait_seconds cannot be negative")
        if self.circuit_failure_threshold < 1:
            raise ValueError("circuit failure threshold must be at least one")
        if self.circuit_recovery_seconds < 0:
            raise ValueError("circuit recovery seconds cannot be negative")
        if not self.atani_command or any(not part for part in self.atani_command):
            raise ValueError("Atani command cannot be empty")
        if self.bryo_status_command is not None and (
            not self.bryo_status_command
            or any(not part for part in self.bryo_status_command)
        ):
            raise ValueError("Bryo status command cannot be empty")
        for label, command in (
            ("Nyx", self.nyx_status_command),
            ("Voodoo", self.voodoo_status_command),
        ):
            if command is not None and (not command or any(not part for part in command)):
                raise ValueError(f"{label} status command cannot be empty")
        # Reuse the scheduler's complete budget validation.
        _ = self.resource_budget

    @property
    def audit_path(self) -> Path:
        return self.state_root / "audit" / "events.jsonl"

    @property
    def routing_check_path(self) -> Path:
        return self.state_root / "routing" / "last-check.json"

    @property
    def gpu_lock_path(self) -> Path:
        return self.shared_gpu_lock_file or self.state_root / "resource" / "gpu.lock"

    @property
    def content_token_path(self) -> Path:
        if self.content_token_file is not None:
            return self.content_token_file
        try:
            home = Path.home()
        except RuntimeError:
            return self.state_root / "secrets" / "scrooge-publish-token.txt"
        return home / ".pionir" / "secrets" / "scrooge-publish-token.txt"

    @property
    def ops_token_path(self) -> Path:
        if self.ops_token_file is not None:
            return self.ops_token_file
        try:
            home = Path.home()
        except RuntimeError:
            return self.state_root / "secrets" / "scrooge-ops-token.txt"
        return home / ".pionir" / "secrets" / "scrooge-ops-token.txt"

    @property
    def deliveries_path(self) -> Path:
        if self.deliveries_dir is not None:
            return self.deliveries_dir
        try:
            home = Path.home()
        except RuntimeError:
            return self.state_root / "deliveries"
        return home / ".pionir" / "deliveries"

    @property
    def secrets_path(self) -> Path:
        """The folder of the owner's secrets (~/.pionir/secrets): every value in it is
        looked for in a client delivery before it can leave."""
        try:
            home = Path.home()
        except RuntimeError:
            return self.state_root / "secrets"
        return home / ".pionir" / "secrets"

    @property
    def instagram_token_path(self) -> Path:
        if self.instagram_token_file is not None:
            return self.instagram_token_file
        try:
            home = Path.home()
        except RuntimeError:
            return self.state_root / "secrets" / "instagram.json"
        return home / ".pionir" / "secrets" / "instagram.json"

    @property
    def devto_key_path(self) -> Path:
        if self.devto_key_file is not None:
            return self.devto_key_file
        try:
            home = Path.home()
        except RuntimeError:
            return self.state_root / "secrets" / "devto-api-key.txt"
        return home / ".pionir" / "secrets" / "devto-api-key.txt"

    @property
    def gumroad_token_path(self) -> Path:
        if self.gumroad_token_file is not None:
            return self.gumroad_token_file
        try:
            home = Path.home()
        except RuntimeError:
            return self.state_root / "secrets" / "gumroad-token.txt"
        return home / ".pionir" / "secrets" / "gumroad-token.txt"

    @property
    def products_path(self) -> Path:
        if self.products_dir is not None:
            return self.products_dir
        try:
            home = Path.home()
        except RuntimeError:
            return self.state_root / "products"
        return home / ".pionir" / "products"

    @property
    def devto_ledger_path(self) -> Path:
        return self.state_root / "devto" / "posts.json"

    @property
    def cortex_path(self) -> Path:
        return self.state_root / "cortex" / "memory.db"

    @property
    def resource_budget(self) -> ResourceBudget:
        return ResourceBudget(
            total_vram_mb=self.total_vram_mb,
            reserved_vram_mb=self.reserved_vram_mb,
            max_gpu_leases=1,
        )

    def initialize_runtime(self) -> None:
        if not self.state_root.is_absolute():
            raise ValueError("Pionir's state root must be absolute")
        if not self.gpu_lock_path.is_absolute():
            raise ValueError("Pionir's shared GPU lock path must be absolute")
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        self.gpu_lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.state_root.chmod(0o700)
            self.audit_path.parent.chmod(0o700)
            self.gpu_lock_path.parent.chmod(0o700)
        except OSError:
            pass

    @classmethod
    def from_environment(cls) -> PionirSettings:
        configured_state_root = os.environ.get("PIONIR_STATE_ROOT", "").strip()
        return cls(
            state_root=(
                Path(configured_state_root)
                if configured_state_root
                else _default_state_root()
            ),
            total_vram_mb=_positive_int(
                "PIONIR_TOTAL_VRAM_MB", _declared("total_vram_mb")
            ),
            reserved_vram_mb=_positive_int(
                "PIONIR_RESERVED_VRAM_MB", _declared("reserved_vram_mb")
            ),
            circuit_failure_threshold=_positive_int(
                "PIONIR_CIRCUIT_FAILURE_THRESHOLD",
                _declared("circuit_failure_threshold"),
            ),
            circuit_recovery_seconds=float(
                os.environ.get(
                    "PIONIR_CIRCUIT_RECOVERY_SECONDS",
                    _declared("circuit_recovery_seconds"),
                )
            ),
            evict_to_fit=(os.environ.get("PIONIR_EVICT_TO_FIT", "1").strip().lower()
                          not in {"0", "off", "false", "no"}),
            atani_command=_command_from_json(
                "PIONIR_ATANI_COMMAND_JSON", _declared("atani_command")
            )
            or _declared("atani_command"),
            bryo_status_command=(
                None
                if (os.environ.get("PIONIR_BRYO_STATUS_COMMAND_JSON", "").strip().lower()
                    in {"off", "none", "false", "0"})
                else _command_from_json(
                    "PIONIR_BRYO_STATUS_COMMAND_JSON",
                    _declared("bryo_status_command"),
                )
            ),
            bryo_status_cwd=(
                os.environ.get("PIONIR_BRYO_STATUS_CWD") or _declared("bryo_status_cwd")
            ),
            bryo_pressure=(os.environ.get("PIONIR_BRYO_PRESSURE", "1").strip().lower()
                           not in {"0", "off", "false", "no"}),
            nyx_status_command=(
                None
                if (os.environ.get("PIONIR_NYX_STATUS_COMMAND_JSON", "").strip().lower()
                    in {"off", "none", "false", "0"})
                else _command_from_json(
                    "PIONIR_NYX_STATUS_COMMAND_JSON", _declared("nyx_status_command")
                )
            ),
            voodoo_status_command=(
                None
                if (os.environ.get("PIONIR_VOODOO_STATUS_COMMAND_JSON", "").strip().lower()
                    in {"off", "none", "false", "0"})
                else _command_from_json(
                    "PIONIR_VOODOO_STATUS_COMMAND_JSON", _declared("voodoo_status_command")
                )
            ),
            voodoo_status_cwd=(
                os.environ.get("PIONIR_VOODOO_STATUS_CWD") or _declared("voodoo_status_cwd")
            ),
            galatea_url=(os.environ.get("PIONIR_GALATEA_URL") or "").strip() or None,
            galatea_model_id=(os.environ.get("PIONIR_GALATEA_MODEL_ID") or "").strip()
            or None,
            daedalus_url=_optional_url("PIONIR_DAEDALUS_URL", _declared("daedalus_url")),
            daedalus_token=(os.environ.get("PIONIR_DAEDALUS_TOKEN") or "").strip() or None,
            melete_url=_optional_url("PIONIR_MELETE_URL", _declared("melete_url")),
            melete_token=(os.environ.get("PIONIR_MELETE_TOKEN") or "").strip() or None,
            crew_url=_optional_url("PIONIR_CREW_URL", _declared("crew_url")),
            owner_notify=(os.environ.get("PIONIR_OWNER_NOTIFY", "1").strip().lower()
                          not in {"0", "off", "false", "no"}),
            content_url=_optional_url("PIONIR_CONTENT_URL", _declared("content_url")),
            content_token_file=(
                Path(os.environ["PIONIR_CONTENT_TOKEN_FILE"]).expanduser()
                if (os.environ.get("PIONIR_CONTENT_TOKEN_FILE") or "").strip()
                else None
            ),
            ops_token_file=(
                Path(os.environ["PIONIR_OPS_TOKEN_FILE"]).expanduser()
                if (os.environ.get("PIONIR_OPS_TOKEN_FILE") or "").strip()
                else None
            ),
            deliveries_dir=(
                Path(os.environ["PIONIR_DELIVERIES_DIR"]).expanduser()
                if (os.environ.get("PIONIR_DELIVERIES_DIR") or "").strip()
                else None
            ),
            instagram_graph_url=_optional_url(
                "PIONIR_INSTAGRAM_GRAPH_URL", _declared("instagram_graph_url")
            ),
            instagram_token_file=(
                Path(os.environ["PIONIR_INSTAGRAM_TOKEN_FILE"]).expanduser()
                if (os.environ.get("PIONIR_INSTAGRAM_TOKEN_FILE") or "").strip()
                else None
            ),
            devto_url=_optional_url("PIONIR_DEVTO_URL", _declared("devto_url")),
            devto_key_file=(
                Path(os.environ["PIONIR_DEVTO_KEY_FILE"]).expanduser()
                if (os.environ.get("PIONIR_DEVTO_KEY_FILE") or "").strip()
                else None
            ),
            gumroad_url=_optional_url("PIONIR_GUMROAD_URL", _declared("gumroad_url")),
            gumroad_token_file=(
                Path(os.environ["PIONIR_GUMROAD_TOKEN_FILE"]).expanduser()
                if (os.environ.get("PIONIR_GUMROAD_TOKEN_FILE") or "").strip()
                else None
            ),
            products_dir=(
                Path(os.environ["PIONIR_PRODUCTS_DIR"]).expanduser()
                if (os.environ.get("PIONIR_PRODUCTS_DIR") or "").strip()
                else None
            ),
            embed_model=_embed_model_from_env(),
            distil_model=(os.environ.get("PIONIR_DISTIL_MODEL") or "").strip()
            or _declared("distil_model"),
            protected_models=_protected_models_from_env(_declared("protected_models")),
            specialists_file=(
                Path(os.environ["PIONIR_SPECIALISTS_FILE"])
                if os.environ.get("PIONIR_SPECIALISTS_FILE")
                else None
            ),
            shared_gpu_lock_file=(
                Path(os.environ["PIONIR_GPU_LOCK_FILE"])
                if os.environ.get("PIONIR_GPU_LOCK_FILE")
                else None
            ),
            gpu_lock_wait_seconds=float(
                os.environ.get(
                    "PIONIR_GPU_LOCK_WAIT_SECONDS", _declared("gpu_lock_wait_seconds")
                )
            ),
        )
