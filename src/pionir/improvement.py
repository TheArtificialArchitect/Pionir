"""Deterministic promotion policy for proposed self-improvements."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping
from uuid import UUID, uuid4


class ImprovementRisk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class ImprovementCandidate:
    source_agent: str
    target: str
    change_ref: str
    hypothesis: str
    rollback_ref: str
    risk: ImprovementRisk
    candidate_id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        for name in (
            "source_agent",
            "target",
            "change_ref",
            "hypothesis",
            "rollback_ref",
        ):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")

    @property
    def digest(self) -> str:
        document = {
            "candidate_id": str(self.candidate_id),
            "source_agent": self.source_agent,
            "target": self.target,
            "change_ref": self.change_ref,
            "hypothesis": self.hypothesis,
            "rollback_ref": self.rollback_ref,
            "risk": self.risk.value,
        }
        encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    candidate_id: UUID
    suite_id: str
    baseline_score: float
    candidate_score: float
    regressions: tuple[str, ...] = ()
    checks: Mapping[str, bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.suite_id.strip():
            raise ValueError("held-out evaluation suite id is required")
        if not 0.0 <= self.baseline_score <= 1.0:
            raise ValueError("baseline score must be in the range 0..1")
        if not 0.0 <= self.candidate_score <= 1.0:
            raise ValueError("candidate score must be in the range 0..1")
        object.__setattr__(self, "checks", MappingProxyType(dict(self.checks)))


@dataclass(frozen=True, slots=True)
class PromotionApproval:
    candidate_digest: str
    approved_by: str
    approved: bool


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    authorized: bool
    reasons: tuple[str, ...]
    candidate_digest: str


@dataclass(frozen=True, slots=True)
class PromotionPolicy:
    minimum_gain: float = 0.01
    required_checks: frozenset[str] = frozenset(
        {"permissions", "resource_limits", "rollback", "held_out"}
    )
    require_ian_approval: bool = True

    def __post_init__(self) -> None:
        if not 0.0 <= self.minimum_gain <= 1.0:
            raise ValueError("minimum gain must be in the range 0..1")


class PromotionGate:
    """Assess evidence and exact human approval; never applies the change itself."""

    def __init__(self, policy: PromotionPolicy | None = None) -> None:
        self.policy = policy or PromotionPolicy()

    def assess(
        self,
        candidate: ImprovementCandidate,
        evaluation: EvaluationReport,
        approval: PromotionApproval | None,
    ) -> PromotionDecision:
        reasons: list[str] = []
        if evaluation.candidate_id != candidate.candidate_id:
            reasons.append("evaluation belongs to a different candidate")
        gain = evaluation.candidate_score - evaluation.baseline_score
        if gain < self.policy.minimum_gain:
            reasons.append(
                f"score gain {gain:.4f} is below required {self.policy.minimum_gain:.4f}"
            )
        if evaluation.regressions:
            reasons.append("held-out evaluation contains regressions")
        missing = sorted(
            check
            for check in self.policy.required_checks
            if evaluation.checks.get(check) is not True
        )
        if missing:
            reasons.append(f"required checks failed or missing: {missing}")
        if not evaluation.suite_id.startswith("held-out:"):
            reasons.append("evaluation suite is not identified as held-out")
        if self.policy.require_ian_approval:
            if approval is None:
                reasons.append("Ian's exact candidate approval is missing")
            elif not approval.approved:
                reasons.append("candidate was not approved")
            elif approval.approved_by.casefold() != "ian":
                reasons.append("approval was not issued by Ian")
            elif approval.candidate_digest != candidate.digest:
                reasons.append("approval digest does not match the candidate")
        return PromotionDecision(not reasons, tuple(reasons), candidate.digest)
