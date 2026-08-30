"""Intent routing: plain language in, one declared capability out, or a question.

The registry already routes a *named* capability and fails closed on an unknown
one (``CapabilityNotFound``) and on an ungranted permission
(``PermissionDenied``). This module is the step before that: deciding which
capability a plain-English request meant. It does not weaken either gate, and in
particular it never substitutes a capability the caller happens to be permitted
for when the one they asked for is denied - that would turn a refusal into a
silent misroute.

Three properties are deliberate.

**It never guesses.** Below the confidence floor the result is a question naming
the plausible routes, not a best-effort pick. A wrong silent route spends a model
load and then answers as the wrong specialist, which reads to a user as a broken
system rather than an unsure one.

**Every decision is audited, including the refusals.** Each classification emits
a ``task.routed`` event carrying its confidence and its outcome, so a week of use
is evidence of how often routing was right rather than an impression of it. The
event carries the capability and the confidence and never the request text; the
audit ledger is metadata-only by design and this is not the place to start
leaking payloads into it.

**No model is consulted to decide which model to load.** That circularity would
spend the GPU this router exists to arbitrate, and would make every routing
decision unreproducible. The classifier is lexical and deterministic.

Distinctiveness is learned from the registry rather than hand-tuned. A term used
by one capability outweighs one used by all of them, so registering a specialist
re-weights routing automatically instead of requiring an edit here.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from .contracts import Capability, Task, TaskResult
from .errors import CapabilityNotFound, PermissionDenied, RoutingAmbiguous
from .runtime import AuditEvent, Executive

_WORD = re.compile(r"[a-z0-9]+")

# Generic English filler only. Nothing here names a project, a specialist, or a
# domain: the domain vocabulary is whatever the capabilities themselves declare.
_STOPWORDS = frozenset(
    {
        "a", "about", "an", "and", "any", "are", "as", "at", "be", "been", "but",
        "by", "can", "could", "did", "do", "does", "for", "from", "get", "give",
        "had", "has", "have", "how", "if", "in", "into", "is", "it", "its",
        "just", "let", "like", "make", "me", "my", "need", "not", "now", "of",
        "on", "one", "or", "our", "out", "over", "please", "put", "show", "so",
        "some", "tell", "than", "that", "the", "their", "them", "then", "there",
        "these", "they", "this", "to", "up", "use", "want", "was", "we", "were",
        "what", "when", "where", "which", "who", "why", "will", "with", "would",
        "you", "your",
    }
)

# A term is distinctive when it names one capability outright, or when fewer
# than this share of them use it. A request matching only shared vocabulary has
# named a family of capabilities rather than a member of it, and the honest
# answer is to ask which member.
#
# The unique-term case has to be stated separately rather than left to the
# share. With two capabilities registered, "fewer than half of two" means fewer
# than one, so a term belonging to exactly one of them failed the test and the
# router could not route anything at all. Nothing caught that until a probe set
# ran against a two-capability registry: the estate has eight.
_DISTINCTIVE_SHARE = 0.5

# Confidence is the winner's share of the top two scores, so 0.6 means the
# winner outweighed the runner-up by half again. A dead tie scores 0.5 and is
# therefore always a question.
DEFAULT_CONFIDENCE_FLOOR = 0.6

DEFAULT_MAX_OPTIONS = 4


def _fold(word: str) -> str:
    """Normalise one token.

    Applied identically to the query and to the vocabulary, so it does not need
    to be a real stemmer, only symmetric. ``status`` folding to ``statu`` costs
    nothing as long as both sides fold.
    """

    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def tokenize(text: str) -> tuple[str, ...]:
    tokens: list[str] = []
    for match in _WORD.finditer(text.lower()):
        word = match.group()
        if len(word) < 2 or word in _STOPWORDS:
            continue
        tokens.append(_fold(word))
    return tuple(tokens)


def capability_vocabulary(agent_id: str, capability: Capability) -> frozenset[str]:
    """Everything a capability says about itself, as comparable tokens."""

    sources: list[str] = [
        capability.name.replace(".", " ").replace("_", " "),
        capability.description,
        agent_id.replace("-", " ").replace("_", " "),
    ]
    sources.extend(capability.routing_hints)
    return frozenset(token for source in sources for token in tokenize(source))


@dataclass(frozen=True, slots=True)
class Candidate:
    capability: str
    agent_id: str
    description: str
    score: float
    matched: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """One classification, resolved or deferred, with the evidence for it.

    It deliberately does not hold the request text. The audit detail is built
    from this object, and the ledger excludes payloads by design.
    """

    capability: str | None
    confidence: float
    reason: str
    candidates: tuple[Candidate, ...] = ()

    @property
    def resolved(self) -> bool:
        return self.capability is not None

    @property
    def options(self) -> tuple[str, ...]:
        """The routes to put to the user when this decision is a question."""

        return tuple(candidate.capability for candidate in self.candidates)

    @property
    def runner_up(self) -> str | None:
        chosen = self.capability
        for candidate in self.candidates:
            if candidate.capability != chosen:
                return candidate.capability
        return None

    def question(self) -> str:
        """The text to put to the user instead of a guess."""

        headline = {
            "no_capabilities": "No specialists are registered, so there is nothing to route to.",
            "empty_request": "That request had no words to route on. What would you like done?",
            "no_match": "Nothing registered matches that request. Which of these did you mean?",
            "not_distinctive": (
                "That names a family of capabilities rather than one of them. "
                "Which did you mean?"
            ),
            "ambiguous": (
                f"Two routes scored too close to call (confidence {self.confidence:.2f}). "
                "Which did you mean?"
            ),
        }.get(self.reason, "Which route did you mean?")
        lines = [headline]
        for index, candidate in enumerate(self.candidates, start=1):
            lines.append(f"  {index}. {candidate.capability} - {candidate.description}")
        return "\n".join(lines)

    def audit_detail(self, refused: str | None = None) -> str:
        """A parseable, payload-free summary for the ``task.routed`` ledger entry.

        ``refused`` names the gate that rejected an otherwise-confident route, so
        a misclassification and a permission refusal are distinguishable in the
        ledger a week later.
        """

        suffix = f" refused={refused}" if refused else ""
        if self.resolved:
            return (
                f"outcome=route capability={self.capability} "
                f"confidence={self.confidence:.2f} reason={self.reason} "
                f"runner_up={self.runner_up or 'none'}{suffix}"
            )
        options = ",".join(candidate.capability for candidate in self.candidates) or "none"
        return (
            f"outcome=ask confidence={self.confidence:.2f} "
            f"reason={self.reason} options={options}{suffix}"
        )


class IntentRouter:
    """Classifies a request to one capability, then hands it to the executive."""

    def __init__(
        self,
        executive: Executive,
        *,
        confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
        max_options: int = DEFAULT_MAX_OPTIONS,
    ) -> None:
        if not 0.5 <= confidence_floor <= 1.0:
            raise ValueError("confidence floor must be between 0.5 and 1.0")
        self.executive = executive
        self.confidence_floor = confidence_floor
        self.max_options = max(1, max_options)

    def _entries(self) -> list[tuple[str, Capability, frozenset[str]]]:
        """Read the registry on every call.

        A cached index would drift from the registry the moment a specialist is
        registered or drops out, and the two would then disagree about what
        exists. Classification is cheap; that divergence is not.
        """

        return [
            (manifest.agent_id, capability, capability_vocabulary(manifest.agent_id, capability))
            for manifest in self.executive.registry.manifests()
            for capability in manifest.capabilities
        ]

    def classify(self, request: str) -> RoutingDecision:
        entries = self._entries()
        if not entries:
            return RoutingDecision(None, 0.0, "no_capabilities")
        query = set(tokenize(request))
        if not query:
            return RoutingDecision(None, 0.0, "empty_request", self._catalogue(entries))

        frequency: dict[str, int] = {}
        for _, _, vocabulary in entries:
            for token in vocabulary:
                frequency[token] = frequency.get(token, 0) + 1
        total = len(entries)

        scored: list[Candidate] = []
        for agent_id, capability, vocabulary in entries:
            matched = tuple(sorted(query & vocabulary))
            if not matched:
                continue
            score = sum(math.log(1 + total / frequency[token]) for token in matched)
            scored.append(
                Candidate(
                    capability=capability.name,
                    agent_id=agent_id,
                    description=capability.description,
                    score=round(score, 6),
                    matched=matched,
                )
            )
        if not scored:
            return RoutingDecision(None, 0.0, "no_match", self._catalogue(entries))

        # Ties break on capability name so the same request always classifies the
        # same way; an unstable winner would make the audit trail unreadable.
        scored.sort(key=lambda candidate: (-candidate.score, candidate.capability))
        best = scored[0]
        runner_up_score = scored[1].score if len(scored) > 1 else 0.0
        confidence = round(best.score / (best.score + runner_up_score), 4)
        options = tuple(scored[: self.max_options])

        distinctive = any(
            frequency[token] == 1 or frequency[token] < total * _DISTINCTIVE_SHARE
            for token in best.matched
        )
        if total > 1 and not distinctive:
            return RoutingDecision(None, confidence, "not_distinctive", options)
        if confidence < self.confidence_floor:
            return RoutingDecision(None, confidence, "ambiguous", options)
        return RoutingDecision(best.capability, confidence, "matched", options)

    def route(
        self,
        request: str,
        *,
        granted_permissions: Iterable[str] = (),
        payload: Mapping[str, Any] | None = None,
        payload_key: str = "content",
    ) -> tuple[RoutingDecision, TaskResult]:
        """Classify and execute, or raise ``RoutingAmbiguous`` carrying the question.

        ``CapabilityNotFound`` and ``PermissionDenied`` still come from the
        registry untouched. A denied route is reported as denied, never quietly
        re-pointed at a specialist the caller can reach.
        """

        decision = self.classify(request)
        if not decision.resolved:
            self._record(decision.audit_detail())
            raise RoutingAmbiguous(decision.question(), decision=decision)
        task = Task(
            capability=str(decision.capability),
            payload={**dict(payload or {}), payload_key: request},
            granted_permissions=frozenset(granted_permissions),
        )
        try:
            result = self.executive.execute(task, routing_detail=decision.audit_detail())
        except (CapabilityNotFound, PermissionDenied) as refusal:
            # Resolution failed before an agent was known, so the executive never
            # recorded the route. The classification still happened, and without
            # this the only routes with evidence would be the ones that were
            # allowed through.
            self._record(decision.audit_detail(refused=type(refusal).__name__))
            raise
        return decision, result

    def _catalogue(
        self, entries: list[tuple[str, Capability, frozenset[str]]]
    ) -> tuple[Candidate, ...]:
        """Every registered capability, for the case where nothing scored."""

        catalogue = [
            Candidate(
                capability=capability.name,
                agent_id=agent_id,
                description=capability.description,
                score=0.0,
                matched=(),
            )
            for agent_id, capability, _ in entries
        ]
        catalogue.sort(key=lambda candidate: candidate.capability)
        return tuple(catalogue[: self.max_options])

    def _record(self, detail: str) -> None:
        """Record a routing decision that never reached the executive.

        A refusal to guess, and a route the registry then refused, are both
        decisions. Without this the ledger would only ever show the routes that
        were confident and allowed, and the question the system most needs
        answering - how often it could not tell - would have no evidence at all.
        """

        self.executive.audit_sink.record(
            AuditEvent(
                event_type="task.routed",
                task_id=uuid4(),
                agent_id="unrouted",
                occurred_at=datetime.now(UTC),
                detail=detail,
            )
        )
