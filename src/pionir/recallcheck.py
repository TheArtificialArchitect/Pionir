"""Does the memory engine recall the right thing? An eval for `cortex.py`.

Route-check measures whether routing points at the right specialist. This is its
sibling for memory: known-answer probes, each a small corpus plus a query plus
the memory that ought to come back, scored as recall@k. It answers two questions
the store cannot answer about itself:

1. **Is lexical recall good enough?** Probes are split by category. *exact* and
   *buried* probes share real words with their target and MUST come back - those
   are gated, and a miss fails the check. *paraphrase* probes mean the same thing
   in different words, which BM25 cannot see; they are measured and reported but
   NOT gated, because a low paraphrase score is not a bug, it is the signal that
   semantic recall would earn its keep (a model on the card). Do not gate what
   you built the engine not to do; report it, and let the number make the call.

2. **Did a change to recall() make it worse?** The corpus is seeded fresh and the
   engine is deterministic, so the score only moves when recall() moves. Re-run
   after any tuning; a drop is a regression, the same discipline as re-running the
   eval after a bulk import (a memory system read 97% then 81% on one bad batch,
   and only the eval noticed - HEAD 3.13). The data-side version of that guard,
   probes against the live store, is a later addition; this one guards the code.

Nothing here loads a model or takes a GPU lease. It is pure lexical scoring over
an in-memory store, so it runs in milliseconds and is safe in CI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Sequence

from .cortex import Cortex, NewMemory

# A gated category must recall at least this fraction of its probes. One miss in
# a handful is tolerated; a pattern of them is a real regression.
MIN_GATED_RECALL = 0.8

GATED_CATEGORIES = frozenset({"exact", "buried"})


@dataclass(frozen=True, slots=True)
class RecallProbe:
    name: str
    category: str            # exact | buried | paraphrase
    corpus: tuple[NewMemory, ...]
    query: str
    expect_contains: str     # a hit is a recalled memory whose text contains this
    k: int = 5


@dataclass(frozen=True, slots=True)
class RecallProbeResult:
    name: str
    category: str
    query: str
    hit: bool
    rank: int | None         # 1-based position of the expected memory, None if missed
    top_text: str            # the top hit, for eyeballing a miss


@dataclass(frozen=True, slots=True)
class RecallCheck:
    taken_at: str
    results: tuple[RecallProbeResult, ...]

    def _recall(self, categories: frozenset[str] | None) -> tuple[int, int]:
        rows = [
            r for r in self.results if categories is None or r.category in categories
        ]
        return sum(1 for r in rows if r.hit), len(rows)

    @property
    def gated_recall(self) -> float:
        hit, total = self._recall(GATED_CATEGORIES)
        return hit / total if total else 1.0

    @property
    def paraphrase_recall(self) -> float:
        hit, total = self._recall(frozenset({"paraphrase"}))
        return hit / total if total else 0.0

    @property
    def overall_recall(self) -> float:
        hit, total = self._recall(None)
        return hit / total if total else 1.0

    @property
    def misses(self) -> tuple[RecallProbeResult, ...]:
        # Only gated misses count against the check; a paraphrase miss is expected.
        return tuple(r for r in self.results if not r.hit and r.category in GATED_CATEGORIES)

    @property
    def passed(self) -> bool:
        return self.gated_recall >= MIN_GATED_RECALL

    @property
    def status(self) -> str:
        return "ok" if self.passed else "failing"

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "taken_at": self.taken_at,
            "gated_recall": round(self.gated_recall, 3),
            "paraphrase_recall": round(self.paraphrase_recall, 3),
            "overall_recall": round(self.overall_recall, 3),
            "min_gated_recall": MIN_GATED_RECALL,
            "misses": [
                {"name": r.name, "query": r.query, "top": r.top_text} for r in self.misses
            ],
            "results": [
                {
                    "name": r.name,
                    "category": r.category,
                    "hit": r.hit,
                    "rank": r.rank,
                }
                for r in self.results
            ],
        }


def _mem(text: str, kind: str = "fact", salience: float | None = None) -> NewMemory:
    return NewMemory(kind=kind, text=text, salience=salience)


def default_probes() -> tuple[RecallProbe, ...]:
    """Grounded in this estate's own material, so a passing score is a claim about
    real recall rather than about toy strings."""
    weather = tuple(_mem(f"an idle thought about the weather, number {i}", "thought") for i in range(60))
    bots = tuple(
        _mem(t, "fact")
        for t in (
            "Atani is the cold executive that plans and verifies",
            "Nyx runs authorized offensive security behind a kill switch",
            "Voodoo watches the machine from inside for defense",
            "Daedalus is the only bot allowed to touch code",
            "Melete runs commands and tools and returns a result",
        )
    )
    return (
        RecallProbe(
            "exact: a plain fact under noise",
            "exact",
            (_mem("Ian is allergic to penicillin"),) + weather,
            "what is Ian allergic to",
            "penicillin",
        ),
        RecallProbe(
            "exact: shared content words",
            "exact",
            (_mem("the second desktop will host the AI agents"),) + weather[:20],
            "where will the agents run",
            "second desktop",
        ),
        RecallProbe(
            "buried: the target among near-siblings",
            "buried",
            bots + (_mem("Bryo is the resource governor for VRAM and memory"),),
            "which bot is the resource governor",
            "Bryo",
        ),
        RecallProbe(
            "buried: one right answer among a busy roster",
            "buried",
            bots,
            "who is allowed to change code",
            "Daedalus",
        ),
        RecallProbe(
            # Every shared word differs only by inflection - reclaim/reclaims,
            # resource/resources, throttle/throttles, lease/leases - and no word
            # is shared un-inflected. Without the stemmer there is zero token
            # overlap and this misses entirely; with it, an exact hit. The probe
            # that justifies the stemmer, and the guard against removing it.
            "inflection: plural and third-person",
            "exact",
            (_mem("the daemon reclaims resources and throttles leases"),) + weather[:20],
            "does it reclaim a resource or throttle a lease",
            "reclaims resources",
        ),
        RecallProbe(
            "paraphrase: different words, same meaning",
            "paraphrase",
            (_mem("Galatea samples several replies and sends the lowest-penalty one"),) + weather[:15],
            "how does the voice avoid a bad answer",
            "samples several",
        ),
        RecallProbe(
            "paraphrase: a synonym the lexicon will not bridge",
            "paraphrase",
            (_mem("closing the window puts her to sleep and she consolidates on the way out"),) + weather[:15],
            "what happens when the app shuts down",
            "consolidates",
        ),
    )


def run(probes: Sequence[RecallProbe] | None = None, *, embedder=None) -> RecallCheck:
    """Seed a fresh in-memory store per probe, query it, score recall@k.

    With no embedder the score is the lexical floor - the CI-safe default, no
    model required. Pass an embedder to measure the hybrid lift: the paraphrase
    probes, which lexical structurally misses, should then land."""
    results: list[RecallProbeResult] = []
    for probe in probes or default_probes():
        cortex = Cortex(":memory:", embedder=embedder)
        try:
            cortex.remember_many(probe.corpus)
            hits = cortex.recall(probe.query, k=probe.k)
        finally:
            cortex.close()
        rank = None
        for index, memory in enumerate(hits, start=1):
            if probe.expect_contains.lower() in memory.text.lower():
                rank = index
                break
        results.append(
            RecallProbeResult(
                name=probe.name,
                category=probe.category,
                query=probe.query,
                hit=rank is not None,
                rank=rank,
                top_text=hits[0].text if hits else "",
            )
        )
    return RecallCheck(taken_at=datetime.now(UTC).isoformat(), results=tuple(results))
