"""Something to be getting on with - real work, measured against the world.

Ported from Hearth. A project outlasts a sitting and survives a restart, and its
progress is COMPUTED FROM EXTERNAL TRUTH every time it is asked: Hearth looked at the
house (books actually read, plants actually watered); a crew Kind looks at the thing
the work is for - a ledger, a leads list, a sent folder. Nothing here stores a number
that says how far along somebody is, so a project cannot be quietly wrong about itself.
``Project`` keeps only bookkeeping (when it started, when it last really moved, how many
steps were taken, the best progress ever read) - never progress itself.

It can also be given up: no real progress for ``project_stale_seconds`` of wall-clock
time (config, default six hours) and the agent lets it go, remembered as its own event,
with a sourness that fades over a day so it is not straight back on it.

The interface a Kind provides:

    key, title
    suits(agent) -> 0..1          how much this agent would take it on; temperament only
    progress(agent, crew) -> 0..1 RECOMPUTED from external truth on every call
    step(agent, crew)             a generator yielding commands (actions.Job, Wait, Until)
                                  and returning a short result in words

``crew`` is the running ``Sim``. Hearth's house Kinds are gone; this phase ships the
framework and only test Kinds. Real business Kinds (reading Scrooge's ledger, Skopos's
leads) arrive in Phase 2 with the organ adapters. Kinds are handed to the crew
(``sim.kinds``), not registered globally, so nothing is on the crew that its builder
did not put there.
"""
from __future__ import annotations

from .log import log

DEFAULT_STALE_SECONDS = 6 * 3600
# purpose relief for one useful step, and for finishing the whole thing (Hearth's)
STEP_RELIEF = 0.22
FINISH_RELIEF = 0.85
# how long the taste of having given up on something lasts, in real seconds (Hearth's
# twenty game hours, now wall-clock; flagged for retuning after observation)
SOUR_FOR = 20 * 3600
# less than this left to do and it is not worth taking on
MIN_LEFT = 0.15


class Kind:
    key = "base"
    title = "something"

    def suits(self, agent) -> float:
        """How much this agent would take this on, 0..1. Temperament only."""
        return 0.0

    def progress(self, agent, crew) -> float:
        """0..1, recomputed from external truth. Never from anything this object stores."""
        return 0.0

    def done(self, agent, crew) -> bool:
        return self.progress(agent, crew) >= 0.999

    def step(self, agent, crew):
        yield from ()
        return "did nothing"


def kinds_by_key(crew) -> dict:
    return {k.key: k for k in (getattr(crew, "kinds", None) or ())}


class Project:
    """One agent's open undertaking. Progress is always recomputed, never stored."""

    def __init__(self, key: str, started_t: int, last_progress_t: int = 0,
                 steps: int = 0, best: float = 0.0, real_steps: int = 0) -> None:
        self.key = key
        self.started_t = started_t
        self.last_progress_t = last_progress_t or started_t
        self.steps = steps              # every step taken
        self.real_steps = real_steps    # steps after which the world had really moved
        self.best = best

    def kind(self, crew) -> Kind | None:
        return kinds_by_key(crew).get(self.key)

    def title(self, crew) -> str:
        k = self.kind(crew)
        return k.title if k is not None else self.key

    def to_dict(self) -> dict:
        return {"key": self.key, "started_t": self.started_t,
                "last_progress_t": self.last_progress_t, "steps": self.steps,
                "real_steps": self.real_steps, "best": round(self.best, 4)}

    @staticmethod
    def from_dict(d) -> Project | None:
        if not d or not d.get("key"):
            return None
        return Project(str(d["key"]), int(d.get("started_t", 0)),
                       int(d.get("last_progress_t", 0)), int(d.get("steps", 0)),
                       float(d.get("best", 0.0)), int(d.get("real_steps", 0)))


def sourness(agent, key: str, t: int) -> float:
    """1.0 just after giving this up, fading to 0 over ``SOUR_FOR``. Never a ban."""
    when = agent.mem.get("gave_up." + key, None)
    if when is None or t < when:            # `not when` would ignore a give-up at t=0
        return 0.0
    left = 1.0 - (t - int(when)) / float(SOUR_FOR)
    return max(0.0, min(1.0, left))


def choose(agent, crew):
    """Pick something to take on, or None if nothing achievable is on offer. Weighted by
    temperament, by how much of it is left, and against how recently it was given up.
    A Kind whose progress cannot be read is skipped - and said, never silently."""
    opts = []
    for k in (getattr(crew, "kinds", None) or ()):
        try:
            left = 1.0 - float(k.progress(agent, crew))
        except Exception as exc:  # noqa: BLE001 - logged: an unreadable Kind is news
            log.warning("%s: cannot read progress of %s: %s: %s",
                        agent.name, k.key, type(exc).__name__, exc)
            continue
        if left < MIN_LEFT:
            continue
        w = max(0.02, float(k.suits(agent))) * (0.35 + 0.65 * left)
        w *= 1.0 - 0.9 * sourness(agent, k.key, crew.clock.t)
        opts.append((max(0.001, w), k))
    if not opts:
        return None
    total = sum(w for w, _ in opts)
    r = agent.rng.random() * total
    for w, k in opts:
        r -= w
        if r <= 0:
            return Project(k.key, crew.clock.t)
    return Project(opts[-1][1].key, crew.clock.t)
