"""Vitals: does each agent's machinery actually produce anything?

The most expensive failure in this estate is the thing that boots green, logs plausibly
and does nothing - a fail-closed gate firing constantly looks exactly like a working one.
The rule is to watch the OUTPUT counter, not the verdict log. Hearth had no such watch
until late: its last world ran a full day with one resident refusing to answer 17 times
out of 24 closures, and nothing in the system called that anything but personality.

So each check below reads a counter of things that REACHED THE WORLD - words said, an
intention formed, a thought had, a skill practised - and compares it against the
opportunity the agent has had. An agent who has had the chance and produced nothing is
reported loudly, once, by name.

Every check is edge-triggered on its own condition and CLEARS when the condition passes,
so none of this is a latch: the first word an agent speaks retires its warning for good.
A check that cannot clear would be the same bug wearing a different hat.

Ported from Hearth without its house: the repertoire check (has every action in the
library ever been done by somebody?) takes the library as an argument, since the action
library arrives with the agents in a later phase, and "too young to tell" is measured in
real seconds since the crew was born rather than game days.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable

from .log import log
from .skills import practice_in

# Each entry: (check id, opportunity_fn, output_fn, floor, sentence).
# The check only speaks once opportunity_fn(a) >= floor: below that, zero output is simply
# a young agent, not a broken one.
CHECKS = (
    ("speech",
     lambda a: sum(p["addressed"] for p in a.mem.people()),
     lambda a: a.mem.counter("utterances"),
     6,
     "has been spoken to %(opp)d times and has never once said anything aloud"),
    # Hearth counted "changes_noticed" (a moved chair, an empty fridge) here too; the crew has
    # no house to notice, so the other half of the opportunity is news actually taken in.
    ("thought",
     lambda a: a.mem.counter("actions_done") + a.mem.counter("news_taken_in"),
     lambda a: a.mem.counter("thoughts"),
     40,
     "has done and taken in %(opp)d things and has never formed a single thought"),
    ("intention",
     lambda a: a.mem.counter("thoughts"),
     lambda a: a.mem.counter("intentions_formed"),
     25,
     "has had %(opp)d thoughts and has never turned one into an intention"),
    ("feeling",
     lambda a: a.mem.counter("actions_done"),
     lambda a: a.affect.pushes,
     60,
     "has done %(opp)d things and nothing has ever moved %(their)s mood"),
    # Hearth's skills were built and then overwritten with None, and every use was guarded
    # by "if skills is not None" - dead for the life of the house, and nothing noticed. Read
    # from the STORE, not from the agent's Skills object, so an unwired one cannot hide.
    ("skill",
     lambda a: a.mem.counter("actions_done"),
     lambda a: practice_in(a.mem),
     60,
     "has done %(opp)d things and has never got any better at one of them"),
    # A crew with no real work must be LOUD, not quietly idle. An agent that keeps looking
    # for something to take on and finds nothing achievable is reported; the streak resets
    # (and the warning clears) the moment it takes a project on.
    ("project",
     lambda a: int(a.mem.get("no_project_streak", 0) or 0),
     lambda a: 0,
     6,
     "has looked for real work %(opp)d times in a row and found nothing achievable"),
    # ...and one that keeps choosing and never once moves real work forward (every step
    # changed nothing, every job failed, or it only ever idled) is reported too.
    ("work",
     lambda a: a.mem.counter("choices"),
     lambda a: a.mem.counter("project_steps"),
     200,
     "has chosen what to do %(opp)d times and has never once moved real work forward"),
)

# An action nobody has managed in this long is either unreachable for this crew or gated on
# something that never happens. Hearth's Rearrange sat at zero forever because it required
# restlessness >= 0.6 and the most restless resident in the house was 0.55 - dead code that
# looked exactly like an action nobody happened to fancy. (Hearth waited two game days,
# which was one real day.)
REPERTOIRE_AFTER_SECONDS = 86400

# Hearth checked twice a game hour, which was every fifteen real minutes.
CHECK_EVERY_SECONDS = 900


def _name(a) -> str:
    return getattr(a, "name", None) or getattr(getattr(a, "t", None), "name", None) or a.id


def _their(a) -> str:
    return getattr(a, "their", None) or getattr(getattr(a, "t", None), "their", None) or "their"


class Vitals:
    """Edge-triggered, so a standing problem is said once and a fixed one is said once."""

    def __init__(self, sim, *, checks: tuple = CHECKS,
                 repertoire: Callable[[], Iterable[str]] | None = None) -> None:
        self.sim = sim
        self.checks = checks
        self.repertoire = repertoire     # -> the action names every one of which someone should do
        self.open: set = set()           # (agent_id, check_id) currently reported
        self.raised = 0
        self.cleared = 0
        self.last_t = -10 ** 9
        self.dead_actions: set = set()

    def _repertoire(self) -> None:
        """Has every action in the library been performed by SOMEBODY, ever?"""
        if self.repertoire is None:
            return
        if self.sim.age_seconds() <= REPERTOIRE_AFTER_SECONDS:
            return                      # too young to tell youth from paralysis
        for name in self.repertoire():
            total = 0
            for a in self.sim.agents:
                try:
                    total += a.mem.counter("action." + name)
                except Exception as exc:  # noqa: BLE001 - a counter that cannot be read is news
                    log.warning("vitals: cannot read action.%s for %s: %s", name, a.id, exc)
                    total = -1
                    break
            if total == 0 and name not in self.dead_actions:
                self.dead_actions.add(name)
                self.raised += 1
                log.warning("VITALS: nobody in this crew has ever done '%s' in %.1f days - "
                            "it may be gated on something no agent can reach",
                            name, self.sim.age_seconds() / 86400)
            elif total > 0 and name in self.dead_actions:
                self.dead_actions.discard(name)
                self.cleared += 1
                log.info("VITALS cleared: '%s' has now been done", name)

    def check(self, force: bool = False) -> list:
        """Returns the currently-open complaints, newest wording. Cheap: counters only."""
        t = self.sim.clock.t
        if not force and t - self.last_t < CHECK_EVERY_SECONDS:
            return self.report()
        self.last_t = t
        for a in self.sim.agents:
            for cid, opportunity, output, floor, sentence in self.checks:
                key = (a.id, cid)
                try:
                    opp = int(opportunity(a))
                    out = int(output(a))
                except Exception as exc:  # noqa: BLE001 - a counter that cannot be read is news
                    log.warning("vitals: cannot read %s for %s: %s", cid, a.id, exc)
                    continue
                bad = opp >= floor and out == 0
                if bad and key not in self.open:
                    self.open.add(key)
                    self.raised += 1
                    log.warning("VITALS: %s %s", _name(a),
                                sentence % {"opp": opp, "their": _their(a)})
                elif not bad and key in self.open:
                    self.open.discard(key)
                    self.cleared += 1
                    log.info("VITALS cleared: %s now has %d (%s)", _name(a), out, cid)
        self._repertoire()
        return self.report()

    def report(self) -> list:
        out = []
        for aid, cid in sorted(self.open):
            a = self.sim.agent(aid)
            out.append({"who": _name(a) if a else aid, "check": cid})
        for name in sorted(self.dead_actions):
            out.append({"who": "the crew", "check": f"nobody ever does '{name}'"})
        return out

    def to_dict(self) -> dict:
        return {"open": self.report(), "raised": self.raised, "cleared": self.cleared,
                "dead_actions": sorted(self.dead_actions)}
