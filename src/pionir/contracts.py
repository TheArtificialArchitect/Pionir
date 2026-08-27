"""Dependency-free contracts shared by Pionir and specialist adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping
from uuid import UUID, uuid4


class RiskLevel(str, Enum):
    READ_ONLY = "read_only"
    REVERSIBLE_WRITE = "reversible_write"
    PRIVILEGED = "privileged"


@dataclass(frozen=True, slots=True)
class MemoryNamespace:
    """A validated hierarchical namespace such as ``identity/theo``."""

    value: str

    def __post_init__(self) -> None:
        parts = self.value.split("/")
        if len(parts) < 2 or any(not part for part in parts):
            raise ValueError("memory namespaces require at least two non-empty segments")
        if any(part in {".", ".."} for part in parts):
            raise ValueError("relative path segments are not valid memory namespaces")
        allowed = set("abcdefghijklmnopqrstuvwxyz0123456789_-")
        if any(set(part) - allowed for part in parts):
            raise ValueError("memory namespaces use lowercase letters, digits, '_' and '-'")

    def contains(self, other: "MemoryNamespace") -> bool:
        return other.value == self.value or other.value.startswith(f"{self.value}/")


@dataclass(frozen=True, slots=True)
class ModelRequirement:
    model_id: str
    estimated_vram_mb: int
    context_vram_mb: int = 0
    requires_gpu: bool = True

    def __post_init__(self) -> None:
        if not self.model_id:
            raise ValueError("model_id is required")
        if self.estimated_vram_mb < 0 or self.context_vram_mb < 0:
            raise ValueError("VRAM estimates cannot be negative")

    @property
    def total_vram_mb(self) -> int:
        return self.estimated_vram_mb + self.context_vram_mb


@dataclass(frozen=True, slots=True)
class Capability:
    name: str
    description: str
    risk: RiskLevel = RiskLevel.READ_ONLY
    required_permissions: frozenset[str] = frozenset()
    model: ModelRequirement | None = None
    priority: int = 0

    def __post_init__(self) -> None:
        if not self.name or any(char.isspace() for char in self.name):
            raise ValueError("capability names must be non-empty and contain no whitespace")


@dataclass(frozen=True, slots=True)
class AgentManifest:
    agent_id: str
    version: str
    capabilities: tuple[Capability, ...]
    memory_access: frozenset[MemoryNamespace] = frozenset()

    def __post_init__(self) -> None:
        if not self.agent_id or not self.version:
            raise ValueError("agent_id and version are required")
        names = [capability.name for capability in self.capabilities]
        if len(names) != len(set(names)):
            raise ValueError("an agent cannot declare a capability more than once")


@dataclass(frozen=True, slots=True)
class Task:
    capability: str
    payload: Mapping[str, Any]
    granted_permissions: frozenset[str] = frozenset()
    task_id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if not self.capability:
            raise ValueError("task capability is required")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True, slots=True)
class TaskResult:
    task_id: UUID
    agent_id: str
    output: Mapping[str, Any]
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "output", MappingProxyType(dict(self.output)))
