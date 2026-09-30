"""Putting the crew together and running it: workers, leaders, one brain, one store.

``build`` wires everything and starts NOTHING - no thread runs and no network, model or
Pionir call is made until ``Crew.start`` (``python -m pionir.crew`` does, in the
foreground). Every outside dependency is injectable: the HTTP client workers read with,
the model's POST, Pionir's client, the Claude runners (the leaders' escalation and the
finder's web research), the card watch and the clocks, so
a test crew never reaches a network.

One loop thread looks at the wall clock every ``tick_seconds`` and:

1. hands every DUE worker to the bounded worker pool (pool.py) - due by its cadence
   scaled by its fixed jitter, never twice at once;
2. hands every due LEADER to the leader pool; a leader reads what the workers wrote
   and reports (leader.py). A leader's first run waits ``LEADER_FIRST_DELAY`` after
   start, so it does not abstain as blind before its workers have had one go;
3. checks the vitals (inertness) and takes a checkpoint now and then.

Beside the loop, ``Crew.start`` serves the Direction API on loopback HTTP (api.py, port
``cfg.api_port``) so Moss can read and direct the crew through Pionir; ``Crew.stop``
stops it first, before anything it reads is closed.

Honest time, kept from Hearth: a restart records "process was not running" for the gap
since the last checkpoint, and an operator ``pause`` is recorded when it ends. While
paused nothing is dispatched, the brain makes no model call and the hands start no job;
queued work keeps its place. Nothing is made up afterwards.
"""
from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from .api import CrewApi
from .brain import Brain, Post, http_post_json
from .clock import WallClock
from .direction import Allocation, Direction
from .escalation import (
    Escalator,
    Runner,
    claude_cli_runner,
    claude_research_runner,
    claude_review_runner,
    claude_site_runner,
)
from .gpu import CardWatch
from .hands import Hands, Job, JobOutcome, PionirClient
from .leader import Leader, parse_json_object
from .log import lesion, lesion_snapshot, log, safe
from .net import UrllibHttp
from .pool import Dispatcher, ProviderGate, jitter
from .registry import Registry, build_registry, load_catalogue
from .result import Err, Ok, Result
from .store import CrewStore
from .vitals import Vitals
from .worker import ErrorKind, WorkContext, WorkerError

LEADER_FIRST_DELAY = 90.0
FIRST_CHECKPOINT_SECONDS = 60


class Crew:
    def __init__(self, cfg, registry: Registry, *, http=None, post: Post = http_post_json,
                 client=None, card: CardWatch | None = None,
                 claude_runner: Runner | None = claude_cli_runner,
                 research_runner: Runner | None = claude_research_runner,
                 site_runner: Runner | None = claude_site_runner,
                 review_runner: Runner | None = claude_review_runner,
                 clock: WallClock | None = None,
                 monotonic: Callable[[], float] = time.monotonic,
                 now: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.registry = registry
        self._now = now
        self.clock = clock or WallClock(now)
        self.store = CrewStore(cfg.state_dir / "crew.db")
        self.lock = threading.RLock()          # the brain's and hands' callbacks run under it
        self.paused_reason: str | None = None
        self._paused_since = 0.0
        self.monitor = None
        self.stopping = False
        self.allocation = Allocation(self.store, registry, {
            "model_calls": cfg.budget.calls_per_hour,
            "claude_escalations": cfg.claude_daily_cap})
        self.direction = Direction(self.store, registry, self.allocation, clock=now)
        self.brain = Brain(cfg, self, card=card, post=post, now=now, allocation=self.allocation)
        self.hands = Hands(cfg, self, client if client is not None
                           else PionirClient(cfg.pionir_url,
                                             token_file=cfg.pionir_token_file), now=monotonic)
        self.http = http if http is not None else UrllibHttp()
        self.gate = ProviderGate(registry.providers)
        self.dispatcher = Dispatcher(registry, self.store, context=self.context_for,
                                     gate=self.gate, max_workers=cfg.pool_size, clock=now)
        self.escalator = Escalator(self.store, self.allocation,
                                   runner=claude_runner if cfg.claude_daily_cap > 0 else None,
                                   daily_cap=cfg.claude_daily_cap,
                                   timeout=cfg.escalation_timeout_seconds, clock=now,
                                   research_runner=(research_runner
                                                    if cfg.claude_daily_cap > 0 else None),
                                   site_runner=(site_runner
                                                if cfg.claude_daily_cap > 0 else None),
                                   review_runner=(review_runner
                                                  if cfg.claude_daily_cap > 0 else None))
        self.leaders = {d: Leader(d, registry, self.store, ask=self.brain.ask,
                                  escalator=self.escalator, model=cfg.model, clock=now)
                        for d in registry.division_ids()}
        self._leader_pool = ThreadPoolExecutor(max_workers=cfg.leader_pool_size,
                                               thread_name_prefix="pionir-crew-leader")
        self._leaders_busy: set = set()
        self._llock = threading.Lock()
        self.vitals = Vitals(self.store, self.all_cadences, clock=now,
                             outputs=self.output_alerts)
        self.direction.outputs = self.output_alerts
        self.direction.facts = self.output_facts
        self.started_at = now()
        self.born_real = 0.0
        self.paused_seconds_total = 0.0
        self.checkpoints = 0
        self.steps = 0
        self._next_checkpoint = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="pionir-crew-loop", daemon=True)
        # Built here, served from start(): building a crew opens no socket.
        self.api = (CrewApi(self.direction, health=self.api_health, port=cfg.api_port,
                            token_file=cfg.pionir_token_file)
                    if cfg.api_port is not None else None)
        self._restore()

    # ---- what a worker is handed ------------------------------------------
    def context_for(self, worker) -> WorkContext:
        goal = (self.store.directions().get(worker.division) or {}).get("goal")
        return WorkContext(now=self._now(), http=self.http, secrets_dir=self.cfg.secrets_dir,
                           words=partial(self._words, worker), job=partial(self._job, worker),
                           approval=self.hands.approval, goal=goal,
                           state_dir=self.cfg.state_dir / "workers",
                           deliveries_dir=getattr(self.cfg, "deliveries_dir", None),
                           products_dir=getattr(self.cfg, "products_dir", None),
                           research=partial(self.escalator.research, worker.division),
                           affiliates=tuple(getattr(self.cfg, "affiliates", ()) or ()),
                           build_site=partial(self.escalator.build_site, worker.division),
                           review=partial(self.escalator.review, worker.division),
                           fiverr_dir=getattr(self.cfg, "fiverr_dir", None),
                           digest=getattr(self.cfg, "digest", None))

    def _words(self, worker, purpose: str, system: str, user: str, schema: dict) -> Result:
        """The ONLY way a worker reaches a model: the shared brain, JSON-schema output,
        temperature 0, charged to the worker's division."""
        text, _meta, err = self.brain.ask(
            worker.worker_id, purpose,
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            {"temperature": 0}, fmt=schema, division=worker.division)
        if err is not None or not text:
            return Err(WorkerError(worker.worker_id, ErrorKind.NO_WORDS,
                                   str(err or "the brain answered nothing")))
        obj = parse_json_object(text)
        if obj is None:
            return Err(WorkerError(worker.worker_id, ErrorKind.MALFORMED,
                                   "the brain's answer was not a JSON object"))
        return Ok(obj)

    def _job(self, worker, job: Job) -> JobOutcome:
        """The ONLY way a worker acts in the world: a Job to Pionir's /api/task, where a
        privileged capability is parked for the owner's approval."""
        return self.hands.run_sync(worker.worker_id, job)

    # ---- lifecycle -----------------------------------------------------------
    def _restore(self) -> None:
        saved_real = self.store.get("saved_real")
        now = self._now()
        if saved_real is None:
            self.born_real = now
            self.store.set("born_real", now)
        else:
            self.born_real = float(self.store.get("born_real", now))
            gap = max(0.0, now - float(saved_real))
            self.store.record_pause(float(saved_real), now, self.clock.t,
                                    "process was not running")
            self.paused_seconds_total = float(self.store.get("paused_total", 0.0)) + gap
            self.store.set("paused_total", self.paused_seconds_total)
            log.info("crew resumed after %.0f s not running", gap)
        self._next_checkpoint = now + FIRST_CHECKPOINT_SECONDS

    def all_cadences(self) -> dict:
        return {**self.registry.cadences(),
                **{lead.leader_id: lead.cadence_seconds for lead in self.leaders.values()}}

    def start(self) -> None:
        self.brain.start()
        self.hands.start()
        self._thread.start()
        if self.api is not None:
            self.api.start()

    def stop(self) -> None:
        if self.stopping:
            return                    # once: a second stop would write into a closed store
        self.stopping = True
        # the API first: no direction write may arrive once the store starts closing
        if self.api is not None:
            safe("crew.stop.api", self.api.stop)
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=15)
        # stop the brain and hands first: a leader waiting on the brain then returns at once
        for part, name in ((self.brain, "brain"), (self.hands, "hands"),
                           (self.monitor, "monitor")):
            if part is not None:
                safe(f"crew.stop.{name}", part.stop)
        safe("crew.stop.workers", lambda: self.dispatcher.shutdown(wait=True))
        safe("crew.stop.leaders", lambda: self._leader_pool.shutdown(wait=True,
                                                                     cancel_futures=True))
        with self.lock:
            if self.paused_reason is not None:
                self._close_pause()
            safe("crew.checkpoint", self.checkpoint)
        self.store.close()

    def pause(self, reason: str = "paused by operator") -> None:
        with self.lock:
            if self.paused_reason is not None:
                return
            self.paused_reason = reason
            self._paused_since = self._now()
            log.info("crew paused: %s", reason)

    def resume(self) -> None:
        with self.lock:
            if self.paused_reason is None:
                return
            seconds = self._close_pause()
            log.info("crew resumed after %.0f s paused", seconds)

    def _close_pause(self) -> float:
        stopped, resumed = self._paused_since, self._now()
        self.store.record_pause(stopped, resumed, self.clock.t, self.paused_reason or "paused")
        self.paused_seconds_total += max(0.0, resumed - stopped)
        self.store.set("paused_total", self.paused_seconds_total)
        self.paused_reason = None
        return resumed - stopped

    def checkpoint(self) -> None:
        self.store.checkpoint(self.clock.t, {"paused_total": self.paused_seconds_total},
                              saved_real=self._now())
        self.checkpoints += 1
        self._next_checkpoint = self._now() + self.cfg.checkpoint_seconds

    # ---- the loop ------------------------------------------------------------
    def leaders_due(self, now: float) -> list:
        last = self.store.last_attempts()
        due = []
        for lead in self.leaders.values():
            prev = last.get(lead.leader_id)
            if prev is None:
                if now - self.started_at >= min(LEADER_FIRST_DELAY, lead.cadence_seconds):
                    due.append(lead)
            elif now - prev >= lead.cadence_seconds * jitter(lead.leader_id):
                due.append(lead)
        return due

    def _run_leader(self, lead) -> None:
        try:
            lead.run()
        finally:
            with self._llock:
                self._leaders_busy.discard(lead.leader_id)

    def step(self) -> None:
        """One look at the clock. The thread is only this in a loop, so tests drive it."""
        if self.paused_reason is not None or self.stopping:
            return
        now = self._now()
        self.steps += 1
        self.dispatcher.dispatch(now)
        for lead in self.leaders_due(now):
            with self._llock:
                if lead.leader_id in self._leaders_busy:
                    continue
                self._leaders_busy.add(lead.leader_id)
            try:
                self._leader_pool.submit(self._run_leader, lead)
            except RuntimeError:              # shutting down
                with self._llock:
                    self._leaders_busy.discard(lead.leader_id)
        safe("vitals", self.vitals.check)
        if now >= self._next_checkpoint:
            safe("crew.checkpoint", self.checkpoint)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception as exc:  # noqa: BLE001 - the loop must outlive one bad step
                lesion("crew.step", exc)
            self._stop.wait(self.cfg.tick_seconds)

    def run_once(self) -> dict:
        """Every worker once, then every leader once, in the foreground. For a first look
        (``python -m pionir.crew --once``) and for tests."""
        report = self.dispatcher.dispatch(only=self.registry.ids(), wait=True)
        results = {d: lead.run() for d, lead in self.leaders.items()}
        return {"workers": report, "leaders": results}

    # ---- for a viewer ----------------------------------------------------------
    def pulses(self) -> list:
        """Every worker that keeps an output counter (the daily posters), as it stands:
        its runs, what it drafted, what reached the owner, what was published - and an
        ``alert`` when nothing did within its window. Read from each worker's own record
        (``pulse``), so a green run that produced nothing is seen as exactly that."""
        now = self._now()
        state_dir = self.cfg.state_dir / "workers"
        digest = getattr(self.cfg, "digest", None)
        health = {h.worker_id: h for h in self.store.health(self.registry.cadences(), now)}
        out = []
        for w in self.registry.all():
            pulse = getattr(w, "pulse", None)
            if not callable(pulse):
                continue
            try:
                p = dict(pulse(state_dir, now, digest))
            except Exception as exc:  # noqa: BLE001 - a broken pulse is itself an alarm
                lesion("crew.pulse", exc)
                p = {"worker": w.worker_id,
                     "alert": f"its pulse could not be read ({type(exc).__name__}: {exc})"}
            h = health.get(w.worker_id)
            if h is not None:
                p.update(last_attempt_at=h.last_attempt_at, last_success_at=h.last_success_at,
                         last_error=h.last_error if h.last_outcome == "err" else None)
            out.append(p)
        return out

    def output_alerts(self) -> dict:
        """{worker id: why it has produced no output in its window} for the vitals."""
        return {p["worker"]: p["alert"] for p in self.pulses() if p.get("alert")}

    def output_facts(self) -> dict:
        """{worker id: what it last produced and when} for every worker, for a viewer's panel.
        Read-only and scrubbed: counts, timestamps and the KIND of the newest output - never a
        payload, a title, a buyer's word or an error message. ``state`` is ``produced`` (an
        output exists), ``no_output`` (its own counter says nothing reached the owner in its
        window - ``alert`` says why, in the crew's words) or ``never`` (it has produced
        nothing, ever: a worker that has not run and one that runs green and writes nothing
        both read this way, and ``last_attempt_at`` tells them apart)."""
        now = self._now()
        state_dir = self.cfg.state_dir / "workers"
        latest = self.store.latest_outputs()
        health = {h.worker_id: h for h in self.store.health(self.registry.cadences(), now)}
        pulses = {p.get("worker"): p for p in self.pulses()}
        out = {}
        for w in self.registry.all():
            wid = w.worker_id
            kind, at = latest.get(wid, (None, None))
            h = health.get(wid)
            pulse = pulses.get(wid) or {}
            alert = pulse.get("alert")
            fact = {"state": "no_output" if alert else "produced" if at is not None else "never",
                    "last_at": at, "kind": _kind_word(kind),
                    "last_attempt_at": h.last_attempt_at if h else None,
                    "last_success_at": h.last_success_at if h else None,
                    "alert": _alert_words(alert) if alert else None}
            if "submitted" in pulse or "published" in pulse:
                fact["posting"] = {k: pulse.get(k) for k in POSTING_FACTS
                                   if isinstance(pulse.get(k), (int, float))
                                   and not isinstance(pulse.get(k), bool)}
            extra = getattr(w, "output_facts", None)
            if callable(extra):
                try:
                    fact.update(extra(state_dir))
                except Exception as exc:  # noqa: BLE001 - one worker's record must not blank the rest
                    lesion("crew.output_facts", exc)
                    fact["facts_error"] = type(exc).__name__
            out[wid] = fact
        return out

    def api_health(self) -> dict:
        """What ``GET /api/health`` says: up, paused or stopping, which divisions - and,
        for doctor, the posters' pulses and every open vitals alarm, so a crew that runs
        green and puts nothing before the owner cannot look healthy there either."""
        now = self._now()
        doc = {"service": "pionir-crew", "paused": self.paused_reason,
               "stopping": self.stopping, "uptime_s": round(now - self.started_at),
               "divisions": list(self.registry.division_ids())}
        if not self.stopping:
            doc["posting"] = self.pulses()
            doc["alerts"] = [a for a in self.vitals.report() if a["check"] != "not_wired"]
        return doc

    def snapshot(self) -> dict:
        now = self._now()
        return {
            "t": self.clock.t, "paused": self.paused_reason,
            "uptime_s": round(now - self.started_at),
            "age_s": round(max(0.0, now - self.born_real)),
            "paused_total_s": round(self.paused_seconds_total),
            "workers": [h.to_dict() for h in self.store.health(self.registry.cadences(), now)],
            "leaders": [h.to_dict() for h in self.store.health(
                {lead.leader_id: lead.cadence_seconds for lead in self.leaders.values()}, now)],
            "in_flight": self.dispatcher.in_flight(),
            "vitals": self.vitals.to_dict(),
            "brain": self.brain.snapshot(),
            "hands": self.hands.snapshot(),
            "escalation": self.escalator.snapshot(),
            "compute": self.direction.compute(),
            "lesions": lesion_snapshot(),
            "budget": self.monitor.latest if self.monitor else None,
        }


# the poster pulse's own counts and timestamps (blog.py / devto.py): numbers only
POSTING_FACTS = ("submitted", "pending", "published", "drafts_written", "drafts_blocked",
                 "last_submitted_at", "last_published_at")
_KIND_OK = re.compile(r"^[A-Za-z0-9_.:-]{1,60}$")


def _kind_word(kind) -> str | None:
    """An output's kind is a code like ``post.tally``; anything else is not shown."""
    return kind if isinstance(kind, str) and _KIND_OK.fullmatch(kind) else None


def _alert_words(alert) -> str:
    """The crew's alert sentence for a viewer. Some alerts end in ``(<exception text>)`` - a
    file path, a parser's words - which are the crew's own log, not a fact for a panel: cut there."""
    return _clip_words(str(alert).split(" (", 1)[0], 160)


def _clip_words(text, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 3] + "..."


def build(cfg, *, registry: Registry | None = None, **injected) -> Crew:
    """A whole, unstarted crew. ``registry`` defaults to the catalogue at
    ``cfg.catalogue_path`` (or the packaged one)."""
    if registry is None:
        registry = build_registry(load_catalogue(cfg.catalogue_path))
    return Crew(cfg, registry, **injected)
