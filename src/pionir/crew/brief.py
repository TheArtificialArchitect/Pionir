"""What a division leader is handed: bounded, per-kind, staleness attached.

This is where the temptation to hand over everything lives, and it is resisted here.

**A per-kind quota, not one flat limit.** Peter's brief once took the newest N rows for
a subject; then fundamentals arrived - ninety-six metric rows for one name - and the
newest sixty were fifty-seven metrics and three derived readings. The one quote the
brain could not work without fell off the end, and every decision became "no quote",
which reads exactly like a dead feed. So each kind of output gets its own quota
(``DEFAULT_QUOTA``, overridable per division in the catalogue's ``brief_quota``), and
the total stays bounded (``TOTAL_LIMIT``): no amount of one kind can starve another.

**Staleness travels with the data.** Every row carries its age, and the brief as a whole
is STAMPED with its stalest input's time - the oldest of each worker's newest reading -
because a division's picture is only as current as its most out-of-date worker.

**Health comes first.** Which workers have never succeeded, are not configured, are not
wired, are stale or are succeeding while producing nothing is at the top, because a
stale worker still has numbers, and the numbers are exactly what makes a blind division
look like a healthy one.

Ages are written in words ("12 min ago"), never "12m", which the grounding check would
rightly read as twelve million.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass

from .figures import Figure

DEFAULT_QUOTA = 6
TOTAL_LIMIT = 40


@dataclass(frozen=True)
class Brief:
    division: str
    title: str
    built_at: float
    stamp: float | None          # the stalest input: oldest of each worker's newest row
    outputs: tuple               # of worker.Output, newest first
    health: tuple                # of store.Health, one per worker
    live: dict                   # worker_id -> True if built, False if a placeholder
    goal: dict | None            # Moss's direction for this division, if any
    last_considered: float | None
    known: tuple                 # names the catalogue itself vouches for

    @property
    def new_outputs(self) -> tuple:
        if self.last_considered is None:
            return self.outputs
        return tuple(o for o in self.outputs if o.observed_at > self.last_considered)

    @property
    def not_wired(self) -> tuple:
        return tuple(w for w, live in self.live.items() if not live)

    @property
    def never_succeeded(self) -> tuple:
        return tuple(h.worker_id for h in self.health
                     if h.has_never_succeeded and h.worker_id not in self.not_wired)

    @property
    def not_configured(self) -> tuple:
        return tuple(h.worker_id for h in self.health if h.not_configured)

    @property
    def stale(self) -> tuple:
        return tuple(h.worker_id for h in self.health
                     if h.is_stale and not h.has_never_succeeded)

    @property
    def fresh(self) -> tuple:
        return tuple(h.worker_id for h in self.health if not h.is_stale)

    @property
    def silent(self) -> tuple:
        return tuple(h.worker_id for h in self.health if h.silent_success)

    def recorded_figures(self) -> list:
        """Everything a report may cite: figures workers recorded FROM A REAL SOURCE
        (never a derived, model-written output), plus the run counts the brief shows."""
        figs = [f for o in self.outputs if not o.derived for f in o.figures]
        figs.append(Figure(len(self.health), "count", "workers"))
        for h in self.health:
            figs += [Figure(h.attempts, "count", "attempts"),
                     Figure(h.consecutive_failures, "count", "failures"),
                     Figure(h.silent_streak, "count", "silent_runs")]
        return figs

    def known_names(self) -> set:
        names = set(self.known) | {self.title}
        for o in self.outputs:
            if not o.derived:
                names |= set(o.entities)
        return names


def _quota(division_spec, kind: str) -> int:
    return int(division_spec.brief_quota.get(kind, DEFAULT_QUOTA))


def build_brief(store, registry, division: str, *, now: float, goal: dict | None = None,
                limit: int = TOTAL_LIMIT) -> Brief:
    spec = registry.division(division)
    workers = registry.workers_in(division)
    kinds = store.output_kinds(division)
    quotas = {k: _quota(spec, k) for k in kinds}
    total = sum(quotas.values())
    scale = min(1.0, limit / total) if total else 1.0
    gathered: list = []
    for k in kinds:
        take = max(1, math.floor(quotas[k] * scale))
        gathered.extend(store.read_outputs(division=division, kind=k, limit=take))
    gathered.sort(key=lambda o: (o.observed_at, o.output_id), reverse=True)
    newest_per_worker: dict = {}
    for o in gathered:
        newest_per_worker.setdefault(o.worker_id, o.observed_at)
    stamp = min(newest_per_worker.values()) if newest_per_worker else None
    known = set(registry.known_names) | set(spec.entities)
    for w in workers:
        known |= set(getattr(w, "entities", ()))
    return Brief(
        division=division, title=spec.title, built_at=now, stamp=stamp,
        outputs=tuple(gathered), health=tuple(store.health(registry.cadences(division), now)),
        live={w.worker_id: bool(w.live) for w in workers}, goal=goal,
        last_considered=store.last_considered_at(division), known=tuple(sorted(known)))


def ago(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    s = max(0, int(seconds))
    if s < 90:
        return f"{s} sec ago"
    if s < 5400:
        return f"{s // 60} min ago"
    if s < 172800:
        return f"{s // 3600} hours ago"
    return f"{s // 86400} days ago"


def _payload_line(payload: dict) -> str:
    try:
        text = json.dumps(payload, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = str(payload)
    return text if len(text) <= 300 else text[:297] + "..."


def render(brief: Brief) -> str:
    """The brief as plain text: the same text the model reads and a person would."""
    now = brief.built_at
    lines = [f"DIVISION {brief.title} ({brief.division})"]
    if brief.stamp is None:
        lines.append("NOTHING RECORDED: no worker in this division has written a single row")
    else:
        lines.append(f"stalest input: {ago(now - brief.stamp)}")
    if brief.goal:
        lines.append(f"GOAL from Moss (priority {brief.goal.get('priority')}): "
                     f"{brief.goal.get('goal')}")
    lines.append("")
    lines.append("WORKERS:")
    for h in brief.health:
        if h.worker_id in brief.not_wired:
            state = "NOT WIRED YET (a placeholder; it produces nothing)"
        elif h.not_configured:
            state = f"NOT CONFIGURED: {h.last_error}"
        elif h.has_never_succeeded:
            state = (f"NEVER SUCCEEDED in {h.attempts} attempts"
                     + (f"; last error {h.last_error_kind}: {h.last_error}" if h.last_error else ""))
        elif h.is_stale:
            state = f"STALE: last success {ago(now - h.last_success_at)}"
        else:
            state = f"ok, last success {ago(now - h.last_success_at)}"
        if h.silent_streak:
            state += f"; succeeded {h.silent_streak} times in a row producing NOTHING"
        if h.consecutive_failures and not h.has_never_succeeded:
            state += f"; {h.consecutive_failures} failures since"
        lines.append(f"- {h.worker_id}: {state}")
    new = {o.output_id for o in brief.new_outputs}
    by_kind: dict = {}
    for o in brief.outputs:
        by_kind.setdefault(o.kind, []).append(o)
    for kind in sorted(by_kind):
        rows = by_kind[kind]
        lines += ["", f"-- {kind} ({len(rows)}) --"]
        for o in rows:
            tag = "NEW " if o.output_id in new else ""
            src = "model-written" if o.derived else "recorded"
            lines.append(f"{tag}[{ago(now - o.observed_at)}] {o.worker_id} ({src}):")
            for f in o.figures:
                lines.append(f"    {f.display()} = {json.dumps(f.to_dict(), sort_keys=True)}")
            lines.append(f"    details: {_payload_line(o.payload)}")
    return "\n".join(lines)
