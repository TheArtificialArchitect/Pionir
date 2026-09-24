"""What an agent thinks about, and why.

Deterministic; no model call. Ported from Hearth: a thought is assembled from
PATTERNS in one agent's own store, gated and weighted by temperament, and every thought
carries the ids of the episodes it was built from (``sources``) - nothing here invents
a fact, and nothing reads another agent's store. Its ``say`` is the concrete thing to
put to a colleague, if it is put to anybody.

Hearth's generators were about the house (a moved chair, an empty fridge, a radio
playing to nobody). A crew's are about the work:

- stalled       my project has not really moved in a while
- doubt         a colleague told me a figure that contradicts one I saw myself
- news          something new was said in one of my channels
- quiet         a colleague I share a channel with has gone quiet
- trouble       a job of mine failed
- waiting       a job of mine is parked for Ian's approval
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .grounding import figures_in, same
from .log import safe

DAY = 86400
STALLED_AFTER = 3600          # an hour of working time without real movement is worth a thought
QUIET_AFTER = 8 * 3600


@dataclass
class Thought:
    kind: str
    text: str
    urge: float                    # how much it wants saying out loud
    about: str | None = None       # a colleague id, if it concerns one
    topic: str = ""                # what a conversation about it would be about
    sources: list = field(default_factory=list)
    weight: float = 1.0            # how likely this agent is to have this kind of thought
    say: str = ""                  # the concrete thing to say about it, if put to somebody
    channel: str | None = None     # where it belongs, if it came from a channel


def _ago(now: int, t: int | None) -> str:
    if t is None:
        return "at some point"
    gap = max(0, now - t)
    if gap < 3600:
        return f"{max(1, gap // 60)} minutes ago"
    if gap < DAY:
        return f"{gap // 3600} hours ago"
    return f"{gap // DAY} days ago"


def _name(sim, aid: str | None) -> str:
    other = sim.agent(aid) if aid else None
    return other.name if other is not None else "somebody"


# ---------------------------------------------------------------- generators

def g_stalled(a, sim):
    """My project has not really moved."""
    p = a.project
    if p is None:
        return None
    gap = sim.clock.t - p.last_progress_t
    if gap < STALLED_AFTER:
        return None
    title = p.title(sim)
    eps = [e for e in a.mem.recent(20, kinds=("project", "did_own", "tried"))
           if (e["detail"] or {}).get("project") == p.key]
    return Thought("stalled", f"I have made no real progress on {title} for "
                              f"{gap // 3600 or 1} hours.",
                   urge=0.2 + 0.4 * a.t.order_sensitivity + 0.2 * a.t.initiative,
                   topic=title, sources=[e["id"] for e in eps[-3:]],
                   weight=0.4 + 1.5 * a.t.order_sensitivity + 0.6 * a.t.initiative,
                   say=f"say your work on {title} has stalled and ask whether they know why "
                       f"or can help")


def g_doubt(a, sim):
    """A colleague told me a figure, and I saw a different one for the same thing."""
    seen_ids = set(a.mem.get("thought_about", []))
    told = a.mem.recent(20, kinds=("heard_say",), since_t=sim.clock.t - DAY)
    mine = [e for e in a.mem.recent(200) if e["source"] == "seen"]
    for e in reversed(told):
        if e["id"] in seen_ids or not e["told_by"]:
            continue
        for claim in figures_in(e["text"]):
            if not claim.noun:
                continue
            for s in reversed(mine):
                for held in figures_in(s["text"]):
                    if held.noun.rstrip("s") != claim.noun.rstrip("s") or \
                            same(held.value, claim.value):
                        continue
                    who = _name(sim, e["told_by"])
                    return Thought(
                        "doubt",
                        f"{who} says {claim.text} {claim.noun}, but I saw {held.text} "
                        f"{held.noun} myself {_ago(sim.clock.t, s['t'])}.",
                        urge=0.3 + 0.4 * a.t.memory_fidelity + 0.2 * a.t.order_sensitivity,
                        about=e["told_by"], topic=claim.noun, sources=[e["id"], s["id"]],
                        weight=0.5 + 2.0 * a.t.memory_fidelity * (0.5 + a.t.order_sensitivity),
                        say=f"say you saw {held.text} {held.noun} yourself, not {claim.text}, "
                            f"and ask where their figure came from",
                        channel=e["channel"])
    return None


def g_news(a, sim):
    """Something new was said in one of my channels."""
    seen_ids = set(a.mem.get("thought_about", []))
    for e in reversed(a.mem.recent(10, kinds=("heard_say",), since_t=sim.clock.t - 3600)):
        if e["id"] in seen_ids or not e["told_by"] or (e["detail"] or {}).get("addressed"):
            continue
        who = _name(sim, e["told_by"])
        return Thought("news", f"{who} said something in #{e['channel']} I have not taken up.",
                       urge=0.15 + 0.4 * a.t.curiosity, about=e["told_by"],
                       topic=f"what {who} said", sources=[e["id"]],
                       weight=0.3 + 1.6 * a.t.curiosity,
                       say=f"take up what {who} said in #{e['channel']}: ask about it or add "
                           f"what you know",
                       channel=e["channel"])
    return None


def g_quiet(a, sim):
    """A colleague I share a channel with has gone quiet."""
    t = sim.clock.t
    for p in sorted(a.mem.people(), key=lambda p: p["last_seen_t"] or 0):
        other = sim.agent(p["other"])
        if other is None or not (set(other.channels) & set(a.channels)):
            continue
        if p["last_seen_t"] and t - p["last_seen_t"] > QUIET_AFTER and p["familiarity"] > 0.05:
            return Thought("quiet", f"I have not heard from {other.name} since "
                                    f"{_ago(t, p['last_seen_t'])}.",
                           urge=0.15 + 0.6 * a.t.sociability, about=p["other"],
                           topic=other.name, weight=0.2 + 2.0 * a.t.sociability,
                           say="ask how their work is going")
    return None


def g_trouble(a, sim):
    """A job of mine failed."""
    seen_ids = set(a.mem.get("thought_about", []))
    for e in reversed(a.mem.recent(12, kinds=("tried",), since_t=sim.clock.t - 6 * 3600)):
        if e["id"] in seen_ids:
            continue
        return Thought("trouble", f"I {e['text']}.",
                       urge=0.2 + 0.3 * a.t.sociability + 0.2 * a.t.order_sensitivity,
                       topic="what went wrong", sources=[e["id"]],
                       weight=0.4 + 0.8 * a.t.order_sensitivity + 0.6 * a.t.dwell,
                       say=f"tell them what went wrong: you {e['text']}")
    return None


def g_waiting(a, sim):
    """A job of mine is parked for Ian's approval and has not run."""
    seen_ids = set(a.mem.get("thought_about", []))
    for e in reversed(a.mem.recent(8, kinds=("job_pending",), since_t=sim.clock.t - DAY)):
        if e["id"] in seen_ids:
            continue
        return Thought("waiting", f"I {e['text']}.",
                       urge=0.15 + 0.3 * a.t.initiative, topic="waiting on approval",
                       sources=[e["id"]], weight=0.3 + 0.9 * a.t.initiative,
                       say="mention that it is waiting on Ian's approval and has not run yet")
    return None


GENERATORS = (g_stalled, g_doubt, g_news, g_quiet, g_trouble, g_waiting)


def think(a, sim):
    """Assemble one thought, or None. Weighted by temperament over whatever fired."""
    candidates = []
    for gen in GENERATORS:
        th = safe(f"think.{gen.__name__}.{a.id}", lambda gen=gen: gen(a, sim))
        if th is not None and th.weight > 0:
            candidates.append(th)
    if not candidates:
        return None
    recent = {t["text"] for t in a.mem.recent_thoughts(10)}
    candidates = [c for c in candidates if c.text not in recent]
    if not candidates:
        return None
    total = sum(c.weight for c in candidates)
    pick = a.rng.random() * total
    for c in candidates:
        pick -= c.weight
        if pick <= 0:
            return c
    return candidates[-1]
