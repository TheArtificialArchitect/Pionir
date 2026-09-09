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


@dataclass(frozen=True, slots=True)
class PionirSettings:
    state_root: Path = field(default_factory=_default_state_root)
    total_vram_mb: int = 12_288
    # The observed idle floor on the target workstation, not an estimate.
    # See docs/PHASE0_BENCHMARK.md; ResourceBudget carries the same figure.
    reserved_vram_mb: int = 1_830
    circuit_failure_threshold: int = 3
    circuit_recovery_seconds: float = 30.0
    theo_url: str = "http://127.0.0.1:8765"
    theo_token: str = field(default="", repr=False)
    # None means ask Theo, which is now the right answer: `/health` reports the
    # model his live client actually resolved. Set this only to pin a specific
    # build; a pinned value that goes stale silently loses the resident-model
    # discount and starts refusing turns that would have fitted.
    theo_model_id: str | None = None

    atani_command: tuple[str, ...] = ("atani",)
    bryo_status_command: tuple[str, ...] | None = None
    autogenesis_status_command: tuple[str, ...] | None = None
    probability_url: str | None = None
    genesis_url: str | None = None
    specialists_file: Path | None = None
    shared_gpu_lock_file: Path | None = None

    def __post_init__(self) -> None:
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
        if self.autogenesis_status_command is not None and (
            not self.autogenesis_status_command
            or any(not part for part in self.autogenesis_status_command)
        ):
            raise ValueError("Autogenesis status command cannot be empty")
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
    def from_environment(cls) -> "PionirSettings":
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
            theo_url=os.environ.get("PIONIR_THEO_URL", _declared("theo_url")),
            theo_model_id=(os.environ.get("PIONIR_THEO_MODEL_ID", "").strip() or None),
            theo_token=(
                os.environ.get("PIONIR_THEO_TOKEN", "").strip()
                or os.environ.get("BRIDGE_TOKEN", "").strip()
            ),
            atani_command=_command_from_json(
                "PIONIR_ATANI_COMMAND_JSON", _declared("atani_command")
            )
            or _declared("atani_command"),
            bryo_status_command=_command_from_json(
                "PIONIR_BRYO_STATUS_COMMAND_JSON", None
            ),
            autogenesis_status_command=_command_from_json(
                "PIONIR_AUTOGENESIS_STATUS_COMMAND_JSON", None
            ),
            probability_url=(
                os.environ.get("PIONIR_PROBABILITY_URL", "").strip() or None
            ),
            genesis_url=(
                os.environ.get("PIONIR_GENESIS_URL", "").strip() or None
            ),
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
        )
