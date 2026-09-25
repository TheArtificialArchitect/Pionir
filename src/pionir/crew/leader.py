"""A division leader: reads what its workers recorded, distils it, and reports up to Moss.

One per division. A leader reads the STORE (what its workers wrote), never live
messages, builds a bounded brief (brief.py), and then does one of three things:

- **Abstains**, with a reason and a ``blocking`` flag (Peter's ADR-0006). Blocking means
  it could not SEE: no worker in the division is wired, or none has a fresh success.
  Non-blocking means it looked and there is nothing to act on - nothing new since its
  last word. The two causes of quiet are different facts and must not read the same.
  An abstention costs no model call.
- **Reports**: it distils the brief with the LOCAL model via the shared brain, in
  Peter's distiller style - a JSON schema, temperature 0, the answer validated field by
  field, the model and a digest of the exact prompt in provenance - and WRITES the
  report as a row. Nothing is acted on inline. Before the row is written as a report,
  the grounding check (grounding.py) must pass: a report may not state a figure no
  worker recorded from a real source, matched on value and unit, nor name anything
  nothing recorded. One that does is written as ``rejected`` with the reason, and Moss
  is shown only the reason.
- **Escalates** a hard judgment to Claude, when the local model asks to and the caps
  allow (escalation.py). Claude's answer is advice, attached to the report, and it
  passes the same grounding check or is withheld.

A leader never raises and its every run is recorded in the runs table beside its
workers', so a leader that never manages a report is reported like any dead worker.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from .brief import build_brief, render
from .figures import Figure
from .grounding import check_report
from .log import log
from .result import Err, Ok, Result
from .worker import ErrorKind, WorkerError

ATTENTION = ("act", "watch", "none")

REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "summary": {"type": "string"},
        "attention": {"type": "string", "enum": list(ATTENTION)},
        "figures": {"type": "array", "items": {
            "type": "object",
            "properties": {"value": {"type": "number"}, "unit": {"type": "string"},
                           "measures": {"type": "string"}, "stream": {"type": "string"},
                           "window": {"type": "string"}},
            "required": ["value", "unit", "measures"]}},
        "routine": {"type": "array", "items": {"type": "string"}},
        "escalate": {"type": "boolean"},
        "question": {"type": "string"},
    },
    "required": ["headline", "summary", "attention", "figures", "routine", "escalate"],
}

SYSTEM = """You lead the {title} division of a small business crew. Your workers record \
facts; you distil them into one short report for Moss, who runs the whole crew and \
decides what to do. Answer with ONE JSON object and nothing else.

Rules:
- Use ONLY the brief. Every number you write must appear in the brief as it is written \
there (you may round dollars to whole dollars). Write durations in words, never "12m".
- Name no company, product, person or platform that is not in the brief.
- If workers are NOT CONFIGURED, NOT WIRED, STALE or produce nothing, say so plainly - \
that is the most useful thing Moss can hear.
- "headline": one line. "summary": at most four sentences.
- "attention": "act" if Moss must decide something, "watch" if something is wrong or \
unclear, "none" if all is routine.
- "figures": the figures your report relies on, copied from the brief's JSON exactly \
(money is usd_cents: $12.00 is 1200).
- "routine": matters that need nothing from Moss (short phrases, may be empty).
- "escalate": true ONLY for a hard judgment you cannot make from the brief; put the \
question in "question"."""

MAX_HEADLINE = 200
MAX_SUMMARY = 1200


@dataclass(frozen=True)
class Abstention:
    division: str
    reason: str
    blocking: bool          # True: could not see. False: looked, nothing to act on.


@dataclass(frozen=True)
class Report:
    division: str
    headline: str
    summary: str
    attention: str
    figures: tuple = ()
    routine: tuple = ()
    escalate: bool = False
    question: str = ""
    escalation: dict | None = None
    provenance: dict = field(default_factory=dict)


def parse_json_object(text: str) -> dict | None:
    """The first JSON object in a model answer, or None. Forgiving about surroundings,
    strict about the result being an object."""
    text = (text or "").strip()
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else None
    except ValueError:
        pass
    a, b = text.find("{"), text.rfind("}")
    if a == -1 or b <= a:
        return None
    try:
        v = json.loads(text[a:b + 1])
        return v if isinstance(v, dict) else None
    except ValueError:
        return None


def validate(raw: dict, division: str) -> Report | None:
    """A model's JSON -> a Report we are willing to check, or None. Enforced, not
    trusted: well-formed JSON can still be nonsense."""
    if not isinstance(raw, dict):
        return None
    headline, summary = raw.get("headline"), raw.get("summary")
    if not isinstance(headline, str) or not headline.strip() or len(headline) > MAX_HEADLINE:
        return None
    if not isinstance(summary, str) or not summary.strip() or len(summary) > MAX_SUMMARY:
        return None
    attention = raw.get("attention")
    if attention not in ATTENTION:
        return None
    figs_raw = raw.get("figures", [])
    if not isinstance(figs_raw, list) or len(figs_raw) > 12:
        return None
    try:
        figures = tuple(Figure.from_dict(f) for f in figs_raw)
    except (TypeError, ValueError):
        return None
    routine = raw.get("routine", [])
    if not isinstance(routine, list) or len(routine) > 6 or not all(
            isinstance(r, str) and len(r) <= 200 for r in routine):
        return None
    escalate = raw.get("escalate", False)
    if not isinstance(escalate, bool):
        return None
    question = raw.get("question") or ""
    if not isinstance(question, str) or len(question) > 600:
        return None
    return Report(division, headline.strip(), summary.strip(), attention, figures,
                  tuple(r.strip() for r in routine if r.strip()), escalate, question.strip())


# (agent_id, purpose, messages, options, *, fmt, division) -> (text, meta, err): brain.ask
Ask = Callable[..., tuple]


class Leader:
    def __init__(self, division: str, registry, store, *, ask: Ask, escalator=None,
                 goals: Callable[[], dict] | None = None, model: str = "",
                 clock: Callable[[], float] = time.time) -> None:
        self.division = division
        self.leader_id = f"leader.{division}"
        self.spec = registry.division(division)
        self.registry = registry
        self.store = store
        self.ask = ask
        self.escalator = escalator
        self.goals = goals or store.directions
        self.model = model
        self._clock = clock
        self.cadence_seconds = self.spec.leader_cadence_seconds

    # ---- the decision to say nothing ------------------------------------------
    def consider(self, brief) -> Abstention | None:
        """Checked BEFORE any model call: a stale worker still has numbers, and the
        numbers are exactly what makes the blind case look like the healthy one."""
        if brief.live and not any(brief.live.values()):
            return Abstention(self.division, "could not see: every worker in this division "
                              f"is not wired yet ({', '.join(brief.not_wired)})", blocking=True)
        if not brief.fresh:
            parts = []
            if brief.not_configured:
                parts.append(f"not configured: {', '.join(brief.not_configured)}")
            if brief.never_succeeded:
                parts.append(f"never succeeded: {', '.join(brief.never_succeeded)}")
            if brief.stale:
                parts.append(f"stale: {', '.join(brief.stale)}")
            if brief.not_wired:
                parts.append(f"not wired: {', '.join(brief.not_wired)}")
            return Abstention(self.division, "could not see: no worker has a fresh success ("
                              + "; ".join(parts) + ")", blocking=True)
        if not brief.outputs:
            return Abstention(self.division, "nothing to act on: workers succeeded but have "
                              "recorded nothing yet", blocking=False)
        if brief.last_considered is not None and not brief.new_outputs:
            return Abstention(self.division, "nothing new since the last report",
                              blocking=False)
        return None

    # ---- one run ---------------------------------------------------------------
    def run(self) -> Result:
        """Build the brief, abstain or distil, write the row. Never raises; the attempt is
        recorded in the runs table on every path."""
        started = self._clock()
        wrote = 0
        try:
            result, wrote = self._run(started)
        except Exception as exc:  # noqa: BLE001 - this IS the boundary
            log.warning("%s raised %s: %s", self.leader_id, type(exc).__name__, exc)
            result = Err(WorkerError(self.leader_id, ErrorKind.UNAVAILABLE,
                                     f"{type(exc).__name__}: {exc}"))
        try:
            self.store.record_attempt(
                worker_id=self.leader_id, division=self.division, started_at=started,
                finished_at=self._clock(),
                error=result.error if isinstance(result, Err) else None, written=wrote)
        except Exception as exc:  # noqa: BLE001 - loud, not fatal
            log.error("%s: could not record its run: %s", self.leader_id, exc)
        return result

    def _run(self, now: float) -> tuple:
        goal = self.goals().get(self.division)
        brief = build_brief(self.store, self.registry, self.division, now=now, goal=goal)
        abstain = self.consider(brief)
        if abstain is not None:
            self.store.add_report(division=self.division, written_at=now, status="abstained",
                                  stamp=brief.stamp, blocking=abstain.blocking,
                                  reason=abstain.reason, attention="none")
            return Ok(abstain), 1
        user = render(brief)
        system = SYSTEM.format(title=brief.title)
        digest = hashlib.sha256(f"{self.model}\x1f{system}\x1f{user}".encode()).hexdigest()[:16]
        provenance = {"model": self.model, "prompt_digest": digest, "temperature": 0,
                      "inputs": len(brief.outputs), "new_inputs": len(brief.new_outputs)}
        text, meta, err = self.ask(
            self.leader_id, "distil",
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            {"temperature": 0}, fmt=REPORT_SCHEMA, division=self.division)
        if err is not None or not text:
            return Err(WorkerError(self.leader_id, ErrorKind.NO_WORDS,
                                   str(err or "the brain answered nothing"))), 0
        provenance["prompt_tokens"] = (meta or {}).get("prompt_eval_count")
        raw = parse_json_object(text)
        report = validate(raw, self.division) if raw is not None else None
        if report is None:
            reason = "the model's answer was not a usable report"
            self._reject(now, brief, reason, {**provenance, "rejected_for": ["malformed"]})
            return Err(WorkerError(self.leader_id, ErrorKind.MALFORMED, reason)), 1
        problems = check_report([report.headline, report.summary, *report.routine],
                                report.figures, brief.recorded_figures(), brief.known_names())
        if problems:
            reason = "; ".join(problems)[:600]
            kinds = sorted({"unbacked name" if p.startswith("names") else "unbacked figure"
                            for p in problems})
            self._reject(now, brief, reason, {**provenance, "rejected_for": kinds})
            log.warning("%s: report rejected: %s", self.leader_id, reason)
            return Err(WorkerError(self.leader_id, ErrorKind.UNGROUNDED, reason)), 1
        escalation = self._escalate(brief, report, user) if report.escalate else None
        self.store.add_report(
            division=self.division, written_at=now, status="report", stamp=brief.stamp,
            headline=report.headline, summary=report.summary, attention=report.attention,
            figures=report.figures, routine=report.routine, provenance=provenance,
            escalation=escalation)
        return Ok(replace(report, escalation=escalation, provenance=provenance)), 1

    def _reject(self, now: float, brief, reason: str, provenance: dict) -> None:
        self.store.add_report(division=self.division, written_at=now, status="rejected",
                              stamp=brief.stamp, reason=reason, attention="watch",
                              provenance=provenance)

    def _escalate(self, brief, report: Report, rendered: str) -> dict:
        question = report.question or report.headline
        if self.escalator is None:
            return {"question": question, "answer": None, "refused": "no escalator"}
        context = f"{rendered}\n\nLEADER'S READ: {report.summary}"
        got = self.escalator.escalate(self.division, question, context)
        if isinstance(got, Err):
            return {"question": question, "answer": None, "refused": str(got.error)}
        problems = check_report([got.value], (), brief.recorded_figures(), brief.known_names())
        if problems:
            return {"question": question, "answer": None,
                    "withheld": "Claude's answer " + "; ".join(problems)[:400]}
        return {"question": question, "answer": got.value}
