"""Reports up, direction down: the Python API Moss uses (the lead exposes it over HTTP).

UP. Division leaders write reports (leader.py); ``Direction.digest`` gives Moss the
newest word from every division as a BOUNDED digest - most urgent first, trimmed to a
character budget, a rejected report shown only as the reason it was rejected (never its
text), and each division's worker health beside it so a blind division cannot pass for
a quiet one.

DOWN. Moss can set a division's goal and priority (``set_goal``), which its leader reads
into every distillation, and allocate COMPUTE (``allocate``): each division's share of
the shared brain's hourly model-call ceiling (``model_calls``) and of the daily Claude
escalation cap (``claude_escalations``). ``Allocation.cap`` turns shares into whole
numbers, and the brain (brain.py) and the escalator (escalation.py) enforce them.

MONEY. There is no money lever here, or anywhere in this package. ``RESOURCES`` is the
closed list of what can be allocated, and it holds compute only; asking for anything
else is an error. Anything that would spend real money is a Pionir capability, and
Pionir parks it for the owner's approval.

Shares: an allocation names some divisions' fractions (summing to at most 1); the
divisions it does not name split what is left equally. Until Moss allocates a resource
at all, it is POOLED: any division may use up to the whole ceiling, and the global
ceiling is the only limit.
"""
from __future__ import annotations

import json
import math
import time
from collections.abc import Callable

from .figures import Figure

# The closed list of what Moss may allocate. Compute only - see the module docstring.
RESOURCES = {
    "model_calls": "calls per hour on the shared local brain (cfg.budget.calls_per_hour)",
    "claude_escalations": "Claude escalations per day via `claude -p` (cfg.claude_daily_cap)",
}

ATTENTION = ("act", "watch", "none")
MAX_GOAL_CHARS = 500
DIGEST_CHARS = 4000


class Allocation:
    """Moss's shares, read from the store and turned into whole-number caps."""

    def __init__(self, store, registry, totals: dict) -> None:
        missing = set(RESOURCES) - set(totals)
        if missing:
            raise ValueError(f"no total for {', '.join(sorted(missing))}")
        self.store = store
        self.registry = registry
        self.totals = {k: int(totals[k]) for k in RESOURCES}

    def allocated(self, resource: str) -> bool:
        return resource in self.store.allocations()

    def shares(self, resource: str) -> dict:
        _check_resource(resource)
        divisions = self.registry.division_ids()
        row = self.store.allocations().get(resource)
        if row is None:
            return {d: 1.0 for d in divisions}           # pooled until Moss allocates
        explicit = {d: float(s) for d, s in row["shares"].items() if d in divisions}
        rest = [d for d in divisions if d not in explicit]
        left = max(0.0, 1.0 - sum(explicit.values()))
        return {**explicit, **{d: left / len(rest) for d in rest}}

    def cap(self, resource: str, division: str) -> int:
        share = self.shares(resource).get(division)
        if share is None:
            raise ValueError(f"unknown division {division!r}")
        return math.floor(share * self.totals[resource] + 1e-9)

    def caps(self, resource: str) -> dict:
        return {d: self.cap(resource, d) for d in self.registry.division_ids()}


def _check_resource(resource: str) -> None:
    if resource not in RESOURCES:
        raise ValueError(f"{resource!r} cannot be allocated: only compute can "
                         f"({', '.join(sorted(RESOURCES))}). There is no money lever; "
                         "anything that spends money is a Pionir capability that waits for "
                         "the owner's approval.")


class Direction:
    """Moss's side of the crew. Reads: ``digest``, ``reports``, ``divisions``,
    ``compute``. Writes: ``set_goal``, ``allocate``. Nothing else."""

    def __init__(self, store, registry, allocation: Allocation, *,
                 clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.registry = registry
        self.allocation = allocation
        self._clock = clock

    # ---- down ----------------------------------------------------------------
    def set_goal(self, division: str, goal: str, *, priority: int = 3,
                 by: str = "moss") -> dict:
        """Set a division's goal and priority (1 = most important, 5 = least). The
        leader reads it into every distillation from its next run on."""
        self._division(division)
        goal = (goal or "").strip()
        if not goal:
            raise ValueError("a goal says something")
        if len(goal) > MAX_GOAL_CHARS:
            raise ValueError(f"a goal is at most {MAX_GOAL_CHARS} characters")
        if isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 5:
            raise ValueError("priority is a whole number from 1 (most) to 5 (least)")
        self.store.set_direction(division=division, goal=goal, priority=priority, by=by,
                                 t=self._clock())
        return self.store.directions()[division]

    def allocate(self, resource: str, shares: dict, *, by: str = "moss") -> dict:
        """Give divisions fractions of a compute resource. Fractions are 0..1 and sum to
        at most 1; divisions not named split the remainder. Replaces the previous
        allocation of that resource. Returns the effective caps."""
        _check_resource(resource)
        if not isinstance(shares, dict) or not shares:
            raise ValueError("shares is a non-empty {division: fraction} object")
        clean = {}
        for d, s in shares.items():
            self._division(d)
            if isinstance(s, bool) or not isinstance(s, (int, float)) or not 0 <= s <= 1:
                raise ValueError(f"{d}'s share must be a fraction from 0 to 1, not {s!r}")
            clean[d] = float(s)
        if sum(clean.values()) > 1.0 + 1e-9:
            raise ValueError(f"shares add up to {sum(clean.values()):.3f}; at most 1")
        self.store.set_allocation(resource=resource, shares=clean, by=by, t=self._clock())
        return self.compute()[resource]

    # ---- up ------------------------------------------------------------------
    def compute(self) -> dict:
        """Every compute resource: its total, whether Moss has allocated it, the shares,
        the resulting caps."""
        out = {}
        for r, what in RESOURCES.items():
            out[r] = {"what": what, "total": self.allocation.totals[r],
                      "allocated": self.allocation.allocated(r),
                      "shares": {d: round(s, 4) for d, s in self.allocation.shares(r).items()},
                      "caps": self.allocation.caps(r)}
        return out

    def reports(self, *, division: str | None = None, limit: int = 20) -> list:
        if division is not None:
            self._division(division)
        return self.store.reports(division=division, limit=max(1, min(limit, 200)))

    def divisions(self) -> list:
        now = self._clock()
        goals = self.store.directions()
        out = []
        for d in self.registry.divisions():
            workers = self.registry.workers_in(d.division_id)
            health = self.store.health(self.registry.cadences(d.division_id), now)
            out.append({
                "division": d.division_id, "title": d.title,
                "goal": (goals.get(d.division_id) or {}).get("goal"),
                "priority": (goals.get(d.division_id) or {}).get("priority"),
                "workers": [{"id": w.worker_id, "live": bool(w.live)} for w in workers],
                "health": _health_line(health),
            })
        return out

    def digest(self, *, max_chars: int = DIGEST_CHARS) -> dict:
        """The newest word from every division, most urgent first, within ``max_chars``
        of text. Built for a model to read: bounded so it cannot crowd her context."""
        now = self._clock()
        goals = self.store.directions()
        entries = []
        for d in self.registry.divisions():
            health = self.store.health(self.registry.cadences(d.division_id), now)
            rows = self.store.reports(division=d.division_id, limit=20)
            e = {"division": d.division_id, "title": d.title,
                 "priority": (goals.get(d.division_id) or {}).get("priority", 3),
                 "goal": (goals.get(d.division_id) or {}).get("goal"),
                 "workers": _health_line(health)}
            if not rows:
                e.update(status="silent", attention="watch",
                         text="no report yet from this division's leader")
            else:
                r = rows[0]
                if r["status"] == "abstained" and not r["blocking"]:
                    # "nothing new": the last real report still stands - show it, say so
                    standing = next((x for x in rows if x["status"] == "report"), None)
                    if standing is not None:
                        e["nothing_new_since_s"] = round(now - r["written_at"])
                        r = standing
                e.update(status=r["status"], age_s=round(now - r["written_at"]),
                         as_of_age_s=(round(now - r["stamp"]) if r["stamp"] else None))
                if r["status"] == "report":
                    e.update(attention=r["attention"], headline=r["headline"],
                             text=r["summary"],
                             figures=[_fig_text(f) for f in r["figures"]][:8])
                    if r["provenance"].get("composed") == "figures_only":
                        # the model's words failed their checks: this is the workers'
                        # own record, composed without a model - say so, and why in kind
                        e["composed"] = "figures_only"
                        e["model_failed"] = r["provenance"].get("rejected_for") or []
                    esc = r.get("escalation") or {}
                    if esc.get("answer"):
                        e["claude"] = esc["answer"][:600]
                elif r["status"] == "abstained":
                    e.update(attention="watch" if r["blocking"] else "none",
                             blocking=r["blocking"], text=r["reason"])
                else:
                    # rejected: say WHY in kind only. The reason quotes the unbacked figure,
                    # and that invented number is exactly what must not reach Moss.
                    why = ", ".join(r["provenance"].get("rejected_for") or ["failed checks"])
                    e.update(attention="watch",
                             text=f"the leader's report was rejected ({why}); its text is "
                                  "withheld. The full reason is in the report record.")
            entries.append(e)
        rank = {a: i for i, a in enumerate(ATTENTION)}
        entries.sort(key=lambda e: (rank.get(e.get("attention"), 1), e["priority"]))
        return _bounded(entries, max_chars)

    def _division(self, division: str) -> None:
        try:
            self.registry.division(division)
        except KeyError as exc:
            raise ValueError(str(exc)) from exc


def _fig_text(d: dict) -> str:
    try:
        return Figure.from_dict(d).display()
    except (TypeError, ValueError):
        return str(d)


def _health_line(health: list) -> dict:
    return {
        "total": len(health),
        "ok": sorted(h.worker_id for h in health if not h.is_stale),
        "never_succeeded": sorted(h.worker_id for h in health
                                  if h.has_never_succeeded and not h.not_wired),
        "not_wired": sorted(h.worker_id for h in health if h.not_wired),
        "not_configured": sorted(h.worker_id for h in health if h.not_configured),
        "stale": sorted(h.worker_id for h in health if h.is_stale and not h.has_never_succeeded),
        "silent": sorted(h.worker_id for h in health if h.silent_streak >= 3),
    }


def _bounded(entries: list, max_chars: int) -> dict:
    """Trim texts first, then drop the least urgent entries, until it fits."""
    def size(es) -> int:
        return len(json.dumps(es, default=str))

    truncated = False
    for cap in (600, 300, 150, 80):
        if size(entries) <= max_chars:
            break
        truncated = True
        for e in entries:
            if len(e.get("text") or "") > cap:
                e["text"] = e["text"][:cap - 3] + "..."
    dropped = []
    while entries and size(entries) > max_chars:
        truncated = True
        dropped.append(entries.pop()["division"])
    return {"divisions": entries, "truncated": truncated, "dropped": dropped}
