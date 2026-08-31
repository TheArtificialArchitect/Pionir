"""Routing aim: is the router sending things to the right place, as a number.

The audit ledger records every decision with its confidence. That measures
**reach** - that a decision was made, and how clearly it separated. It cannot
measure **aim**. A ledger full of confident routes reads exactly the same
whether those routes were correct or not, so accuracy can degrade for a month
without anything anywhere saying so.

Aim needs a fixed set of requests whose right answer is known, re-run
deliberately. This is that set, and `pionir route-check` is how it runs.

Nothing here executes a task. Probes are classified and thrown away, so no
model loads, no GPU lease is taken, and no specialist is called - the check is
safe to run at any time, including while the card is busy.

Three failure kinds are counted separately, because they are not equally bad:

* ``misroute`` - routed, to the wrong capability. The worst outcome: the wrong
  specialist answers confidently and the user has no way to tell.
* ``over_ask`` - asked when it could have routed. A cost, not a fault. The
  router declining to guess is the behaviour it was built for.
* ``under_ask`` - routed when it should have asked. This is the rule being
  broken rather than a score being low, so it fails the check on its own
  regardless of the accuracy figure.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .router import IntentRouter

# The expected answer for a request that must not be routed at all.
ASK = "ASK"

# Below this share of probes correct, aim is treated as broken rather than
# drifting. Matches the threshold the Theo pane set for the same measurement.
MIN_ACCURACY = 0.6

# A measurement this old is not evidence of anything current.
STALE_DAYS = 30


@dataclass(frozen=True, slots=True)
class Probe:
    """One request whose correct destination is known in advance."""

    request: str
    expected: str
    requires: tuple[str, ...] = ()
    """Capabilities that must be registered for this probe to mean anything.

    An ambiguity probe is only ambiguous when both candidates exist; a route
    probe is unanswerable when its target is not configured. Either way the
    honest result is to skip it and say so, not to score it as a failure.
    """


@dataclass(frozen=True, slots=True)
class ProbeResult:
    request: str
    expected: str
    actual: str
    confidence: float
    outcome: str


@dataclass(frozen=True, slots=True)
class RoutingCheck:
    taken_at: str
    results: tuple[ProbeResult, ...]
    skipped: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def correct(self) -> int:
        return sum(item.outcome == "correct" for item in self.results)

    @property
    def accuracy(self) -> float:
        return round(self.correct / self.total, 4) if self.total else 0.0

    @property
    def under_ask(self) -> tuple[ProbeResult, ...]:
        return tuple(item for item in self.results if item.outcome == "under_ask")

    @property
    def misroutes(self) -> tuple[ProbeResult, ...]:
        return tuple(item for item in self.results if item.outcome == "misroute")

    @property
    def measurable(self) -> bool:
        """Any probe applied at all.

        Distinct from failing. A registry where every probe was skipped has not
        been shown to route badly - it has not been shown anything, and saying
        so is different from reporting a zero.
        """

        return bool(self.total)

    @property
    def passed(self) -> bool:
        """Aim is acceptable.

        A single under-ask fails regardless of the score: guessing where the
        router was supposed to ask is the one behaviour it exists to prevent.
        """

        return self.measurable and not self.under_ask and self.accuracy >= MIN_ACCURACY

    @property
    def status(self) -> str:
        if not self.measurable:
            return "not_measurable"
        return "ok" if self.passed else "failing"

    def age_days(self, now: datetime | None = None) -> float:
        taken = datetime.fromisoformat(self.taken_at)
        return round(((now or datetime.now(UTC)) - taken).total_seconds() / 86_400, 2)

    def to_json(self) -> str:
        return json.dumps(
            {
                "taken_at": self.taken_at,
                "skipped": list(self.skipped),
                "results": [
                    {
                        "request": item.request,
                        "expected": item.expected,
                        "actual": item.actual,
                        "confidence": item.confidence,
                        "outcome": item.outcome,
                    }
                    for item in self.results
                ],
            },
            ensure_ascii=False,
            indent=2,
        )

    @classmethod
    def from_json(cls, document: str) -> RoutingCheck:
        parsed = json.loads(document)
        return cls(
            taken_at=str(parsed["taken_at"]),
            results=tuple(
                ProbeResult(
                    request=str(item["request"]),
                    expected=str(item["expected"]),
                    actual=str(item["actual"]),
                    confidence=float(item["confidence"]),
                    outcome=str(item["outcome"]),
                )
                for item in parsed["results"]
            ),
            skipped=tuple(str(item) for item in parsed.get("skipped", ())),
        )


def default_probes() -> tuple[Probe, ...]:
    """Requests covering every built-in capability, plus the refusals.

    The ask probes matter as much as the route probes. A router that never
    asks scores well on routing and has stopped doing the thing it was built
    for, and only an expected-ASK probe can catch that.
    """

    return (
        Probe(
            "ask atani to reason about this problem",
            "reasoning.atani_answer",
            ("reasoning.atani_answer",),
        ),
        Probe(
            "think deeply and carefully about this, take a thorough look",
            "reasoning.atani_depth",
            ("reasoning.atani_depth",),
        ),
        Probe(
            "run this versioned plan through the executive",
            "executive.atani_run",
            ("executive.atani_run",),
        ),
        Probe(
            "talk to theo about my day",
            "conversation.theo_peer_reply",
            ("conversation.theo_peer_reply",),
        ),
        Probe(
            "how is bryo doing, check the terrarium vitals",
            "organism.bryo_status",
            ("organism.bryo_status",),
        ),
        Probe(
            "genesis life loop counters",
            "organism.genesis_status",
            ("organism.genesis_status",),
        ),
        Probe(
            "probability operational state",
            "organism.probability_status",
            ("organism.probability_status",),
        ),
        Probe(
            "autogenesis controls and recent ledger events",
            "organism.autogenesis_status",
            ("organism.autogenesis_status",),
        ),
        # This used to expect ASK: both capabilities held a conversation, so
        # choosing between them would have been a coin toss. Ian settled it on
        # 2026-08-30 - Theo is the voice - so plain conversation now has a right
        # answer, and this probe is what holds the router to it.
        Probe(
            "chat with someone",
            "conversation.theo_peer_reply",
            ("conversation.theo_peer_reply", "reasoning.atani_answer"),
        ),
        # Names a family rather than a member of it.
        Probe(
            "organism status",
            ASK,
            ("organism.bryo_status", "organism.genesis_status"),
        ),
        # Nothing registered does this, and inventing a route would be worse
        # than saying so.
        Probe("photosynthesis in tomato plants", ASK),
        Probe("   ", ASK),
    )


def _outcome(expected: str, actual: str) -> str:
    if expected == actual:
        return "correct"
    if actual == ASK:
        return "over_ask"
    if expected == ASK:
        return "under_ask"
    return "misroute"


def run(router: IntentRouter, probes: tuple[Probe, ...] | None = None) -> RoutingCheck:
    """Classify every applicable probe. Nothing is executed."""

    registered = {
        capability.name
        for manifest in router.executive.registry.manifests()
        for capability in manifest.capabilities
    }
    results: list[ProbeResult] = []
    skipped: list[str] = []
    for probe in probes or default_probes():
        missing = [name for name in probe.requires if name not in registered]
        if missing:
            skipped.append(f"{probe.expected}: not registered ({', '.join(sorted(missing))})")
            continue
        decision = router.classify(probe.request)
        actual = decision.capability or ASK
        results.append(
            ProbeResult(
                request=probe.request,
                expected=probe.expected,
                actual=actual,
                confidence=decision.confidence,
                outcome=_outcome(probe.expected, actual),
            )
        )
    return RoutingCheck(
        taken_at=datetime.now(UTC).isoformat(),
        results=tuple(results),
        skipped=tuple(skipped),
    )


def save(path: Path, check: RoutingCheck) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(check.to_json(), encoding="utf-8")


def load(path: Path) -> RoutingCheck | None:
    """The last recorded check, or None if aim has never been measured.

    None is reported as "never measured" rather than omitted. A missing row is
    the state most likely to be read as fine.
    """

    if not path.exists():
        return None
    try:
        return RoutingCheck.from_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError):
        return None
