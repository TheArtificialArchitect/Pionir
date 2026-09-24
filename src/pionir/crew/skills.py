"""Getting better at things, and coming to like them.

Two numbers per person per action, both earned only by doing:

  ability   0..1  how good they are. Rises on a completion that actually helped,
                  with diminishing returns, so the tenth cup of tea teaches less
                  than the first. It makes the action FASTER and less likely to
                  fail - real effects the sim acts on, not a label.
  affinity  -1..1 how they feel about it. Rises when doing it relieved something,
                  falls when it failed or did nothing. It nudges the appeal in
                  `_choose`, so over weeks people drift toward their own things.

Both DECAY toward neutral when unpractised, which is the point: neither is a
latch. A skill left alone for a month fades; a dislike earned by one bad
afternoon does not last forever. Every guard answers "what un-sets this?".

Nothing here is shown to the model as a number. It reaches a conversation only
as something another resident has actually watched happen often enough to
believe - "Bram is the one who makes the tea" - and that belief is built from
observed episodes, not from reading this table.

Wiring, because Hearth got it wrong: its agent built ``Skills`` and then, sixteen
lines later, overwrote it with ``None`` behind a comment promising a loader that
was never written. Every use sat under ``if self.skills is not None``, so the
whole module was dead and nothing said so. Here the store is the only truth:
``Skills(mem, owner)`` loads from it on construction, every ``did`` writes
straight back to it, and ``practice_in(mem)`` reads it back WITHOUT a Skills
object - which is what the vitals check uses, so an agent whose skills were
never wired shows up as one that acts and never gets better at anything.
"""
from __future__ import annotations

import math

from .log import lesion

STORE_KEY = "skills"

# what one useful completion teaches, as a fraction of the remaining headroom
LEARN = 0.075
# affinity movement per completion, toward +1 when it helped and -1 when it did not
LIKE = 0.045
DISLIKE = 0.060
# half-lives in DAYS (wall clock) for the slide back toward neutral when unpractised. Ported
# numerically from Hearth, where a day was a game day (half a real day); they are now real
# time and are flagged for retuning once the crew has been observed.
ABILITY_HALF_LIFE_DAYS = 45.0
AFFINITY_HALF_LIFE_DAYS = 12.0

ABILITY_CAP = 0.95          # nobody is ever finished learning
AFFINITY_CAP = 0.55         # a preference, never a compulsion

# How much ability is allowed to matter, so a novice is slower but never helpless.
SPEED_FLOOR = 0.55          # an expert takes 55% as long as a beginner
FAIL_RELIEF = 0.45          # ability removes at most this share of a failure chance


def _decay(x: float, rest: float, days: float, half_life: float) -> float:
    if days <= 0:
        return x
    return rest + (x - rest) * math.pow(0.5, days / half_life)


class Skills:
    """Lives on the agent; persists in the agent's own store."""

    def __init__(self, mem, owner: str) -> None:
        self.mem = mem
        self.owner = owner
        self.rows: dict = {}          # action -> {practice, ability, affinity, last_t}
        self.load()

    # ---- reading ---------------------------------------------------------
    def row(self, action: str) -> dict:
        r = self.rows.get(action)
        if r is None:
            r = {"practice": 0, "ability": 0.0, "affinity": 0.0, "last_t": 0}
            self.rows[action] = r
        return r

    def ability(self, action: str) -> float:
        return self.row(action)["ability"]

    def affinity(self, action: str) -> float:
        return self.row(action)["affinity"]

    def practice(self, action: str) -> int:
        return self.row(action)["practice"]

    def speed(self, action: str) -> float:
        """Multiplier on how long the action takes. 1.0 for a beginner."""
        return 1.0 - (1.0 - SPEED_FLOOR) * self.ability(action)

    def steadiness(self, action: str) -> float:
        """How much of a failure chance this person's skill removes, 0..FAIL_RELIEF."""
        return FAIL_RELIEF * self.ability(action)

    def best(self, n: int = 3) -> list:
        """The things this person is actually good at, strongest first."""
        rows = [(r["ability"], a) for a, r in self.rows.items() if r["practice"] >= 3]
        rows.sort(reverse=True)
        return [a for _, a in rows[:n]]

    def favourite(self, n: int = 2) -> list:
        rows = [(r["affinity"], a) for a, r in self.rows.items() if r["affinity"] > 0.08]
        rows.sort(reverse=True)
        return [a for _, a in rows[:n]]

    # ---- moving ----------------------------------------------------------
    def did(self, action: str, t: int, helped: bool, failed: bool) -> None:
        """One completed attempt. `helped` means it actually relieved something."""
        r = self.row(action)
        self._age(r, t)
        r["practice"] += 1
        r["last_t"] = t
        if not failed:
            r["ability"] = min(ABILITY_CAP, r["ability"] + LEARN * (1.0 - r["ability"]))
        if failed:
            r["affinity"] = max(-AFFINITY_CAP, r["affinity"] - DISLIKE * (1.0 + r["affinity"]))
        elif helped:
            r["affinity"] = min(AFFINITY_CAP, r["affinity"] + LIKE * (1.0 - r["affinity"]))
        else:
            # it worked but changed nothing: mildly discouraging, not a grievance
            r["affinity"] = max(-AFFINITY_CAP, r["affinity"] - LIKE * 0.35 * (1.0 + r["affinity"]))
        self._guard(r)
        # write-through: practice that lives only in this object is practice that can be lost
        self.save()

    def _age(self, r: dict, t: int) -> None:
        if not r["last_t"]:
            return
        days = max(0.0, (t - r["last_t"]) / 86400.0)
        if days < 0.05:
            return
        r["ability"] = _decay(r["ability"], 0.0, days, ABILITY_HALF_LIFE_DAYS)
        r["affinity"] = _decay(r["affinity"], 0.0, days, AFFINITY_HALF_LIFE_DAYS)

    def age_all(self, t: int) -> None:
        """Called occasionally so unpractised things fade even when never chosen."""
        for r in self.rows.values():
            self._age(r, t)
            r["last_t"] = max(r["last_t"], t) if r["last_t"] else t
            self._guard(r)

    def _guard(self, r: dict) -> None:
        for k, lo, hi in (("ability", 0.0, ABILITY_CAP), ("affinity", -AFFINITY_CAP, AFFINITY_CAP)):
            v = r[k]
            if not math.isfinite(v):
                v = 0.0
            r[k] = max(lo, min(hi, v))

    # ---- persistence -----------------------------------------------------
    def load(self) -> None:
        try:
            self.rows = dict(self.mem.get(STORE_KEY, None) or {})
        except Exception as exc:  # noqa: BLE001 - recorded; a blank slate is said, not hidden
            lesion(f"skills.load.{self.owner}", exc)
            self.rows = {}
        for a, r in list(self.rows.items()):
            if not isinstance(r, dict):
                del self.rows[a]
                continue
            r.setdefault("practice", 0)
            r.setdefault("ability", 0.0)
            r.setdefault("affinity", 0.0)
            r.setdefault("last_t", 0)
            self._guard(r)

    def save(self) -> None:
        self.mem.set(STORE_KEY, {a: {"practice": int(r["practice"]),
                                    "ability": round(float(r["ability"]), 4),
                                    "affinity": round(float(r["affinity"]), 4),
                                    "last_t": int(r["last_t"])}
                                for a, r in self.rows.items()})

    def to_dict(self) -> dict:
        """For the viewer: only what is real and only where there is something to show."""
        out = {}
        for a, r in self.rows.items():
            if r["practice"] >= 2:
                out[a] = {"practice": r["practice"],
                          "ability": round(r["ability"], 3),
                          "affinity": round(r["affinity"], 3)}
        return out


def practice_in(mem) -> int:
    """Total completions recorded in an agent's store, read from the store itself.
    Zero for an agent that acts means its skills were never wired."""
    rows = mem.get(STORE_KEY, None) or {}
    return sum(int(r.get("practice", 0)) for r in rows.values() if isinstance(r, dict))
