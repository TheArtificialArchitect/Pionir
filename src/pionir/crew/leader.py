"""A division leader: reads what its workers recorded, distils it, and reports up to Moss.

One per division. A leader reads the STORE (what its workers wrote), never live
messages, builds a bounded brief (brief.py), and then:

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
  nothing recorded.
- **Repairs, once**: an answer that is malformed or fails the grounding check is sent
  back to the model ONCE, with the reasons in words. The schema already bounds what the
  model can write (lengths, item counts, and figures only as the brief's own numbers).
- **Falls back to the figures**: if the repaired answer fails too, the model's words
  are dropped - all of them - and the leader sends up a FIGURES-ONLY report composed
  here from the workers' own records: each worker's health, what was recorded since the
  last report, and the newest real-source figures, marked ``composed: figures_only``.
  Moss always gets the true figures; a model that fails its checks costs her only the
  prose. The rejected answer's reasons stay in the report record (never in what Moss
  reads - they may quote the very figure that was invented), and the leader's run is
  recorded as a failure of that kind, so a leader whose model never passes still shows
  up like any failing worker.
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

from .brief import build_brief, figure_table, render, worker_state
from .figures import Figure
from .grounding import check_report, health_claims
from .log import log
from .result import Err, Ok, Result
from .worker import ErrorKind, WorkerError

ATTENTION = ("act", "watch", "none")

MAX_HEADLINE = 200
MAX_SUMMARY = 1200
MAX_FIGURES = 12
MAX_ROUTINE = 6
MAX_ROUTINE_ITEM = 200
MAX_QUESTION = 600

# The shape the model is held to. Ollama enforces it with a grammar - lengths and item
# counts included - so the answers the live crew used to lose (a 16-figure list; a report
# cut off mid-figure when its prompt filled the context) cannot be produced at all.
# ``figures`` is a list of the brief's own figure NUMBERS (F3 -> 3), constrained per brief
# by ``report_schema`` to the numbers that exist.
REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "maxLength": 160},
        "summary": {"type": "string", "maxLength": MAX_SUMMARY},
        "attention": {"type": "string", "enum": list(ATTENTION)},
        "figures": {"type": "array", "items": {"type": "integer"}, "maxItems": 8},
        "routine": {"type": "array", "maxItems": MAX_ROUTINE,
                    "items": {"type": "string", "maxLength": 160}},
        "escalate": {"type": "boolean"},
        "question": {"type": "string", "maxLength": 400},
    },
    "required": ["headline", "summary", "attention", "figures", "routine", "escalate"],
}


def report_schema(numbers) -> dict:
    """REPORT_SCHEMA with ``figures`` held to this brief's figure numbers."""
    numbers = sorted(set(numbers))
    schema = json.loads(json.dumps(REPORT_SCHEMA))
    figs = schema["properties"]["figures"]
    if numbers:
        figs["items"]["enum"] = numbers
    else:
        figs["maxItems"] = 0
    return schema


SYSTEM = """You lead the {title} division of a small business crew. Your workers record \
facts; you distil them into one short report for Moss, who runs the whole crew and \
decides what to do. Answer with ONE JSON object and nothing else.

Rules:
- Use ONLY the brief. Write every number in digits exactly as the brief writes it (you \
may round dollars to whole dollars); never spell a number out in words. Write an age as \
the brief does ("12 min ago"), never "12m".
- Name no company, product, person or platform that is not in the brief.
- If workers are NOT CONFIGURED, NOT WIRED, STALE or produce nothing, say so plainly - \
that is the most useful thing Moss can hear.
- "headline": one plain sentence in sentence case, at most 15 words. "summary": at most \
four sentences.
- "attention": "act" if Moss must decide something, "watch" if something is wrong or \
unclear, "none" if all is routine.
- "figures": the numbers of the figures your report relies on, most important first, at \
most 8. Each recorded figure in the brief is numbered "F1", "F2", ...: list F3 as 3.
- "routine": matters that need nothing from Moss (short phrases, may be empty).
- "escalate": true ONLY for a hard judgment you cannot make from the brief; put the \
question in "question"."""

REPAIR = """Your report could not be sent to Moss, for these reasons:
{reasons}

Write the report again as ONE JSON object in the same shape, fixing every reason. Use \
only what the brief records; leave out anything you cannot back with it."""

FIGURES_ONLY = "figures_only"           # provenance["composed"] of a fallback report
MAX_FALLBACK_SUMMARY = 2400


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


def _text(raw: dict, key: str, limit: int, reasons: list, *, required: bool = True) -> str:
    v = raw.get(key)
    if v is None and not required:
        return ""
    if not isinstance(v, str) or (required and not v.strip()):
        reasons.append(f'"{key}" must be a non-empty string')
        return ""
    if len(v) > limit:
        reasons.append(f'"{key}" is {len(v)} characters; the limit is {limit}')
    return v.strip()


def examine(raw, division: str, numbered: dict | None = None) -> tuple:
    """A model's JSON -> (a Report we are willing to check, or None; every reason it is
    not one, in words). Enforced, not trusted: well-formed JSON can still be nonsense.

    ``numbered`` maps the brief's figure numbers to its recorded figures; a figure given
    as a number resolves to that recorded figure. A figure given as an object (the older
    shape) is still accepted and must then be backed by the grounding check."""
    if not isinstance(raw, dict):
        return None, ["the answer was not one complete JSON object (was it cut off?)"]
    reasons: list = []
    headline = _text(raw, "headline", MAX_HEADLINE, reasons)
    summary = _text(raw, "summary", MAX_SUMMARY, reasons)
    attention = raw.get("attention")
    if attention not in ATTENTION:
        reasons.append(f'"attention" must be one of {", ".join(ATTENTION)}')
    figs_raw = raw.get("figures", [])
    figures: list = []
    if not isinstance(figs_raw, list):
        reasons.append('"figures" must be a list of figure numbers')
    elif len(figs_raw) > MAX_FIGURES:
        reasons.append(f'"figures" lists {len(figs_raw)}; the limit is {MAX_FIGURES}')
    else:
        numbered = numbered or {}
        for f in figs_raw:
            if isinstance(f, int) and not isinstance(f, bool):
                if f not in numbered:
                    reasons.append(f"figure {f} is not a numbered figure in the brief")
                elif numbered[f] not in figures:
                    figures.append(numbered[f])
                continue
            try:
                figures.append(Figure.from_dict(f))
            except (TypeError, ValueError) as exc:
                reasons.append(f"a figure is not usable: {exc}")
    routine = raw.get("routine", [])
    if not isinstance(routine, list) or len(routine) > MAX_ROUTINE or not all(
            isinstance(r, str) and len(r) <= MAX_ROUTINE_ITEM for r in routine):
        reasons.append(f'"routine" must be at most {MAX_ROUTINE} short phrases')
        routine = []
    escalate = raw.get("escalate", False)
    if not isinstance(escalate, bool):
        reasons.append('"escalate" must be true or false')
    question = _text(raw, "question", MAX_QUESTION, reasons, required=False)
    if reasons:
        return None, reasons
    return Report(division, headline, summary, attention, tuple(figures),
                  tuple(r.strip() for r in routine if r.strip()), escalate, question), []


def validate(raw, division: str, numbered: dict | None = None) -> Report | None:
    """``examine`` without the reasons."""
    return examine(raw, division, numbered)[0]


def figures_only(brief, *, kinds: list, attempts: int) -> Report:
    """The report a leader sends when the model's words failed their checks: composed
    here, deterministically, from the workers' own records - NO model text at all.

    What goes in: each worker's health (the same words the brief uses), what was recorded
    since the last report (worker and kind, with model-written rows marked as such), and
    the figures of each worker's newest real-source output of each kind, as recorded.
    What never goes in: any payload text - a payload may hold a model's draft or a
    client's words - and the rejected answer or its reasons (they may quote the very
    figure that was invented); those stay in the record's ``reason``."""
    parts = [(f"Figures only: the leader's model report failed its checks "
              f"({', '.join(kinds)}) after {attempts} attempt(s), so this is what the "
              "workers recorded, with no model words.")]
    parts.append("Workers: " + "; ".join(f"{h.worker_id} {worker_state(brief, h)}"
                                         for h in brief.health) + ".")
    seen: dict = {}
    for o in brief.new_outputs:
        key = (o.worker_id, o.kind, o.derived)
        seen[key] = seen.get(key, 0) + 1
    if seen:
        parts.append("New since the last report: " + "; ".join(
            f"{w} {k} x{n}" + (" (model-written)" if derived else "")
            for (w, k, derived), n in seen.items()) + ".")
    newest: dict = {}           # (worker, kind) -> its newest real-source output
    groups: dict = {}           # (worker, kind) -> that output's figures
    for _n, o, f in figure_table(brief):                 # newest first within a kind
        key = (o.worker_id, o.kind)
        if newest.setdefault(key, o.output_id) == o.output_id:
            groups.setdefault(key, []).append(f)
    figures: list = []
    lines = []
    for (worker, kind), figs in groups.items():
        lines.append(f"{worker} {kind}: " + ", ".join(f.display() for f in figs))
        figures += [f for f in figs if f not in figures]
    if lines:
        parts.append("Newest figures: " + "; ".join(lines) + ".")
    summary = " ".join(parts)
    if len(summary) > MAX_FALLBACK_SUMMARY:
        summary = summary[:MAX_FALLBACK_SUMMARY - 3] + "..."
    # money first, then the rest in the brief's order: the digest shows the first few
    figures.sort(key=lambda f: f.unit != "usd_cents")
    return Report(brief.division,
                  f"{brief.title}: figures only - the leader's report failed its checks",
                  summary, "watch", tuple(figures), (), False, "")


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
        if self.spec.leader_notes:
            system += f"\n\nFor this division:\n{self.spec.leader_notes}"
        digest = hashlib.sha256(f"{self.model}\x1f{system}\x1f{user}".encode()).hexdigest()[:16]
        provenance = {"model": self.model, "prompt_digest": digest, "temperature": 0,
                      "inputs": len(brief.outputs), "new_inputs": len(brief.new_outputs)}
        numbered = {n: f for n, _o, f in figure_table(brief)}
        schema = report_schema(numbered)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        failures: list = []             # (kind, reasons) per attempt that failed
        for attempt in (1, 2):
            text, meta, err = self.ask(self.leader_id, "distil" if attempt == 1 else "repair",
                                       messages, {"temperature": 0}, fmt=schema,
                                       division=self.division)
            if err is not None or not text:
                if attempt == 1:        # no words at all: nothing to report or fall back from
                    return Err(WorkerError(self.leader_id, ErrorKind.NO_WORDS,
                                           str(err or "the brain answered nothing"))), 0
                failures.append((ErrorKind.NO_WORDS,
                                 [f"the repair got no answer: {err or 'nothing'}"]))
                break
            if attempt == 1:
                provenance["prompt_tokens"] = (meta or {}).get("prompt_eval_count")
            kind, reasons, report = self._judge(text, brief, numbered)
            if report is not None:
                provenance["attempts"] = attempt
                if failures:
                    provenance["repaired"] = failures[0][0]
                escalation = self._escalate(brief, report, user) if report.escalate else None
                self.store.add_report(
                    division=self.division, written_at=now, status="report",
                    stamp=brief.stamp, headline=report.headline, summary=report.summary,
                    attention=report.attention, figures=report.figures,
                    routine=report.routine, provenance=provenance, escalation=escalation)
                return Ok(replace(report, escalation=escalation, provenance=provenance)), 1
            failures.append((kind, reasons))
            log.warning("%s: attempt %d refused (%s): %s", self.leader_id, attempt, kind,
                        "; ".join(reasons)[:300])
            if attempt == 1:
                messages = messages + self._repair_turn(text, reasons)
        return self._fall_back(now, brief, provenance, failures), 1

    def _judge(self, text: str, brief, numbered: dict) -> tuple:
        """(error kind or None, reasons, the report if it may go up)."""
        raw = parse_json_object(text)
        report, reasons = examine(raw, self.division, numbered)
        if report is None:
            return ErrorKind.MALFORMED, reasons, None
        texts = [report.headline, report.summary, *report.routine]
        problems = check_report(texts, report.figures, brief.recorded_figures(),
                                brief.known_names(), brief.vocabulary(),
                                brief.recorded_values())
        # a claim about worker health the runs table contradicts is ungrounded too
        problems += health_claims(texts, brief.health_states(), brief.counted_items())
        if problems:
            return ErrorKind.UNGROUNDED, problems, None
        return None, [], report

    @staticmethod
    def _repair_turn(text: str, reasons: list) -> list:
        """The one repair: the model's own answer (when it was a whole, short one) and why
        it could not go up. A long or cut-off answer is not sent back - it is what filled
        the context in the first place."""
        turn = []
        raw = parse_json_object(text)
        if raw is not None and len(text) <= 3000:
            turn.append({"role": "assistant", "content": json.dumps(raw)})
        listed = "\n".join(f"- {r}" for r in reasons[:12])
        turn.append({"role": "user", "content": REPAIR.format(reasons=listed)})
        return turn

    def _fall_back(self, now: float, brief, provenance: dict, failures: list) -> Result:
        """Both attempts failed: send up the figures-only report, keep the reasons in the
        record, and record the run as the model's failure."""
        kinds = []
        for kind, _r in failures:
            label = {ErrorKind.MALFORMED: "malformed", ErrorKind.UNGROUNDED: "ungrounded",
                     ErrorKind.NO_WORDS: "no words"}.get(kind, str(kind))
            if label not in kinds:
                kinds.append(label)
        reason = " | ".join(f"attempt {i}: " + "; ".join(r)
                            for i, (_k, r) in enumerate(failures, 1))[:1000]
        attempts = len(failures)
        report = figures_only(brief, kinds=kinds, attempts=attempts)
        prov = {**provenance, "composed": FIGURES_ONLY, "rejected_for": kinds,
                "attempts": attempts}
        self.store.add_report(
            division=self.division, written_at=now, status="report", stamp=brief.stamp,
            headline=report.headline, summary=report.summary, attention=report.attention,
            figures=report.figures, routine=(), reason=reason, provenance=prov)
        log.warning("%s: the model's report failed its checks (%s); sent the figures only: %s",
                    self.leader_id, ", ".join(kinds), reason[:300])
        last = next((k for k, _r in reversed(failures) if k != ErrorKind.NO_WORDS),
                    ErrorKind.NO_WORDS)
        return Err(WorkerError(self.leader_id, last, reason))

    def _escalate(self, brief, report: Report, rendered: str) -> dict:
        question = report.question or report.headline
        if self.escalator is None:
            return {"question": question, "answer": None, "refused": "no escalator"}
        context = f"{rendered}\n\nLEADER'S READ: {report.summary}"
        got = self.escalator.escalate(self.division, question, context)
        if isinstance(got, Err):
            return {"question": question, "answer": None, "refused": str(got.error)}
        problems = check_report([got.value], (), brief.recorded_figures(), brief.known_names(),
                                brief.vocabulary(), brief.recorded_values())
        if problems:
            return {"question": question, "answer": None,
                    "withheld": "Claude's answer " + "; ".join(problems)[:400]}
        return {"question": question, "answer": got.value}
