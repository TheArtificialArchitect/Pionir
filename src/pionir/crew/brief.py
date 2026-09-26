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
from .grounding import values_in_data, vocabulary_words

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
    notes: str = ""              # the catalogue's leader notes for this division

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

    def recorded_values(self) -> set:
        """Numbers real-source payloads hold outside their figures ("topics_left": 13).
        They may back a bare number in a report, never a typed claim (grounding.py)."""
        vals: set = set()
        for o in self.outputs:
            if not o.derived:
                vals |= values_in_data(o.payload)
        return vals

    def vocabulary(self) -> set:
        """The lower-case words this brief is made of, which a report may write with a
        capital without naming anything new ("Posting.instagram", "Tally", "Card-Press",
        "Facebook" for a recorded referrer www.facebook.com): the division, its workers'
        ids, every output kind and payload key, what each figure measures and its stream
        and window, the words of real-source payload values, and the catalogue's leader
        notes. A model-written payload's VALUES are never vocabulary - a name a model
        invented cannot vouch for itself - only its keys, which code wrote."""
        texts = [self.division, self.title, self.notes, *self.live, *self.known]
        texts += [h.worker_id for h in self.health]
        for o in self.outputs:
            texts += [o.worker_id, o.kind]
            texts += [t for f in o.figures for t in (f.measures, f.stream, f.window)]
            texts += _strings(o.payload, values=not o.derived)
        return vocabulary_words(texts)


def _strings(obj, *, values: bool, depth: int = 0) -> list:
    """The keys of a JSON-ish payload, and (``values``) its string leaves."""
    out: list = []
    if depth > 6:
        return out
    if isinstance(obj, dict):
        for k, v in list(obj.items())[:200]:
            out.append(str(k))
            out += _strings(v, values=values, depth=depth + 1)
    elif isinstance(obj, (list, tuple)):
        for v in list(obj)[:200]:
            out += _strings(v, values=values, depth=depth + 1)
    elif isinstance(obj, str) and values:
        out.append(obj)
    return out


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
        last_considered=store.last_considered_at(division), known=tuple(sorted(known)),
        notes=spec.leader_notes)


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


def _by_kind(brief: Brief) -> list:
    """The outputs grouped by kind, kinds in order: how the brief is read and numbered."""
    by_kind: dict = {}
    for o in brief.outputs:
        by_kind.setdefault(o.kind, []).append(o)
    return [(k, by_kind[k]) for k in sorted(by_kind)]


def figure_table(brief: Brief) -> list:
    """Every figure a worker recorded FROM A REAL SOURCE in this brief, numbered as the
    brief shows it ("F3"): [(number, output, figure), ...]. A report lists its figures by
    these numbers, so a figure it lists is a recorded one by construction - the model
    never retypes a value, a unit or what it measures (which is where it went wrong: a
    16-figure list, an answer cut off mid-figure). Model-written outputs' figures get no
    number: they back nothing."""
    out, n = [], 0
    for _kind, rows in _by_kind(brief):
        for o in rows:
            if o.derived:
                continue
            for f in o.figures:
                n += 1
                out.append((n, o, f))
    return out


def worker_state(brief: Brief, h) -> str:
    """One worker's health in words - the same words for the model and for a person."""
    now = brief.built_at
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
    return state


def render(brief: Brief) -> str:
    """The brief as plain text: the same text the model reads and a person would. Each
    recorded figure carries its number ("F3: $12.00 revenue (mtd)"), which is how a report
    lists it; a model-written output's figures are shown, unnumbered."""
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
        lines.append(f"- {h.worker_id}: {worker_state(brief, h)}")
    new = {o.output_id for o in brief.new_outputs}
    n = 0
    for kind, rows in _by_kind(brief):
        lines += ["", f"-- {kind} ({len(rows)}) --"]
        for o in rows:
            tag = "NEW " if o.output_id in new else ""
            src = "model-written" if o.derived else "recorded"
            lines.append(f"{tag}[{ago(now - o.observed_at)}] {o.worker_id} ({src}):")
            for f in o.figures:
                if o.derived:
                    lines.append(f"    {f.display()} (model-written: backs nothing)")
                else:
                    n += 1                      # the same order as figure_table
                    lines.append(f"    F{n}: {f.display()}")
            lines.append(f"    details: {_payload_line(o.payload)}")
    return "\n".join(lines)
