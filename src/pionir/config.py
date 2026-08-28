"""Environment-backed Pionir configuration with private runtime-state defaults."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .scheduler import ResourceBudget


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
    state_root: Path = field(default_factory=lambda: Path.home() / ".pionir")
    total_vram_mb: int = 12_288
    reserved_vram_mb: int = 1_024
    circuit_failure_threshold: int = 3
    circuit_recovery_seconds: float = 30.0
    theo_url: str = "http://127.0.0.1:8765"
    theo_token: str = field(default="", repr=False)
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
    def gpu_lock_path(self) -> Path:
        return self.shared_gpu_lock_file or self.state_root / "resource" / "gpu.lock"

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
        defaults = cls()
        return cls(
            state_root=Path(os.environ.get("PIONIR_STATE_ROOT", str(defaults.state_root))),
            total_vram_mb=_positive_int("PIONIR_TOTAL_VRAM_MB", defaults.total_vram_mb),
            reserved_vram_mb=_positive_int(
                "PIONIR_RESERVED_VRAM_MB", defaults.reserved_vram_mb
            ),
            circuit_failure_threshold=_positive_int(
                "PIONIR_CIRCUIT_FAILURE_THRESHOLD",
                defaults.circuit_failure_threshold,
            ),
            circuit_recovery_seconds=float(
                os.environ.get(
                    "PIONIR_CIRCUIT_RECOVERY_SECONDS",
                    defaults.circuit_recovery_seconds,
                )
            ),
            theo_url=os.environ.get("PIONIR_THEO_URL", defaults.theo_url),
            theo_token=(
                os.environ.get("PIONIR_THEO_TOKEN", "").strip()
                or os.environ.get("BRIDGE_TOKEN", "").strip()
            ),
            atani_command=_command_from_json(
                "PIONIR_ATANI_COMMAND_JSON", defaults.atani_command
            )
            or defaults.atani_command,
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
