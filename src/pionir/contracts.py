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
    # This model needs the whole card to itself, so making room for it may evict
    # even a protected model (the voice's) once the shared lease is held. Default
    # False: an ordinary GPU tenant must fit beside protected models or be
    # refused, never displace one. Set True only on a capability that genuinely
    # fills the card (Daedalus's 30B coder). See scheduler._make_room.
    exclusive_card: bool = False

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
    routing_hints: frozenset[str] = frozenset()
    """Extra words a person might use for this capability.

    The intent router scores a request against the name, the description and
    these, weighting each term by how few capabilities use it. Declaring hints
    here rather than in a table inside the router is what lets a specialist
    become routable by registering, without an edit anywhere else.
    """
    routable: bool = True
    """Whether plain-language routing may choose this capability.

    Some capabilities are only ever invoked by name, never classified from a
    request - ``manager.atani_manage`` is reached directly by /api/intent, never
    NL-routed. Leaving such a capability in the router's vocabulary lets it win
    or tie a classification it should never take part in (it tied at 0.50 in a
    fresh route-check). Non-routable capabilities are excluded from classification
    and from route-check's probe set; they still resolve and execute by name."""

    def __post_init__(self) -> None:
        if not self.name or any(char.isspace() for char in self.name):
            raise ValueError("capability names must be non-empty and contain no whitespace")
        if any(not hint or any(char.isspace() for char in hint) for hint in self.routing_hints):
            raise ValueError("routing hints must be non-empty single words")


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


# The longest specialist error text carried into an audit reason. The ledger is
# metadata-only by design, so the reason is a bounded one-line summary, never the
# specialist's full output.
_REASON_LIMIT = 160
_RETURN_CODE_KEYS = ("returncode", "rc", "exit_code")


def outcome_failure_reason(output: Any) -> str | None:
    """Why a specialist's own output reports failure, or None when it reports success.

    An adapter can return normally and still carry a failing verdict: Daedalus a
    refused solve (``ok: false``), an action a non-zero ``returncode``. This is the
    ONE rule for that - the server's outer ``ok`` and the executive's audit event
    (``task.failed`` vs ``task.completed``) both read it, so they cannot disagree.
    Failure is ``ok: false`` or any non-zero integer return code (bools are not rcs).
    """

    if not isinstance(output, Mapping):
        return None
    parts: list[str] = []
    if output.get("ok") is False:
        parts.append("ok=false")
    for key in _RETURN_CODE_KEYS:
        code = output.get(key)
        if isinstance(code, int) and not isinstance(code, bool) and code != 0:
            parts.append(f"{key}={code}")
    if not parts:
        return None
    reason = " ".join(parts)
    error = output.get("error")
    if isinstance(error, Mapping):
        error = error.get("message") or error.get("type")
    if error:
        text = " ".join(str(error).split())
        if len(text) > _REASON_LIMIT:
            text = text[: _REASON_LIMIT - 3] + "..."
        reason = f"{reason}: {text}"
    return reason


def outcome_ok(output: Any) -> bool:
    """Whether a specialist's own output reports success (see outcome_failure_reason)."""

    return outcome_failure_reason(output) is None
