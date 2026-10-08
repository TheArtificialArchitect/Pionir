"""Crew autonomy beyond the cadence: parking, retries, the worker controls, leader reruns,
and the leaders' Claude escalations held to what changed.

Each test fails if the rule it names is reverted: a not-configured worker run (and failing)
at its every cadence - products.api_builder failed 1,471 times in a row that way - or parked
for good with no way back; a retryable failure left to wait a whole cadence (``retryable``
was read by nothing); a pause or a cadence change that never expires; a control reachable
for an unknown worker or out of its bounds; a leader that can rerun another division's
workers or rerun without limit; a leader asking Claude again about unchanged facts; the
leaders taking the slots the workers' real work needs.
"""
from __future__ import annotations

import json
import threading
import time
import unittest

from crew_support import FakeTime, temp_dir
from test_crew_fakes import TEST_IMPLS, FakeClaude, catalogue, make_crew
from test_crew_leader import FakeAsk, reply

from pionir.crew import control as ctl
from pionir.crew.leader import Leader
from pionir.crew.pool import jitter
from pionir.crew.registry import build_registry
from pionir.crew.result import Err, Ok
from pionir.crew.worker import ErrorKind, WorkerError, make_output, never_raises


class CtlWorker:
    """A worker whose answer and readiness the test sets: ``mode`` ok / unset (NOT_CONFIGURED,
    not retryable) / flaky (HTTP 500, retryable) / fatal (AUTH, not retryable)."""

    live = True

    def __init__(self, spec, *, mode: str = "ok", ready: str | None = None) -> None:
        self.worker_id, self.division, self.kind = spec.worker_id, spec.division, spec.kind
        self.cadence_seconds, self.provider, self.stage = (spec.cadence_seconds, spec.provider,
                                                           spec.stage)
        self.entities = spec.entities
        self.mode, self.ready = mode, ready
        self.runs = 0
        self.readiness_threads: list = []

    def readiness(self, secrets_dir):
        self.readiness_threads.append(threading.current_thread().name)
        return self.ready

    @never_raises()
    def run(self, ctx):
        self.runs += 1
        if self.mode == "unset":
            return Err(WorkerError(self.worker_id, ErrorKind.NOT_CONFIGURED,
                                   "NOT SET UP: node is not set up", retryable=False))
        if self.mode == "flaky":
            return Err(WorkerError(self.worker_id, ErrorKind.HTTP_ERROR, "HTTP 503"))
        if self.mode == "fatal":
            return Err(WorkerError(self.worker_id, ErrorKind.AUTH, "HTTP 401",
                                   retryable=False))
        return Ok((make_output(self, valid_at=ctx.now, observed_at=ctx.now,
                               payload={"run": self.runs},
                               figures=[{"value": 1200, "unit": "usd_cents",
                                         "measures": "revenue"}],
                               entities=self.entities, provenance={"source": "real"}),))


IMPLS = {**TEST_IMPLS, "ctl": CtlWorker}


class _Case(unittest.TestCase):
    def crew(self, workers, **kw):
        tmp = temp_dir()
        self.addCleanup(tmp.cleanup)
        self.time = FakeTime()
        cat = catalogue({"alpha": [{"impl": "ctl", **w} for w in workers],
                         # a day's cadence: it runs at the first tick and is out of the way
                         "beta": [{"name": "b1", "cadence_seconds": 86400}]})
        crew = make_crew(tmp.name, registry=build_registry(cat, IMPLS), now=self.time.now,
                         **kw)
        self.addCleanup(crew.stop)
        self.c = crew
        return crew

    def w(self, wid="alpha.w"):
        return self.c.registry.require(wid)

    def tick(self, seconds: float = 0.0):
        """Advance the clock, then one dispatch, waiting for the runs (not the rechecks)."""
        self.time.advance(seconds)
        return self.c.dispatcher.dispatch(wait=True)

    def settle(self) -> None:
        """Wait for any readiness re-check the last dispatch handed to the pool."""
        deadline = time.monotonic() + 5
        while self.c.dispatcher._checking and time.monotonic() < deadline:
            time.sleep(0.01)

    def status(self, wid="alpha.w") -> dict:
        return self.c.dispatch_status()[wid]


class ParkingTests(_Case):
    def test_a_not_configured_worker_is_parked_not_run_every_cadence(self) -> None:
        """Reverted: the worker runs - and fails - 288 times a day at a 300 s cadence."""
        self.crew([{"name": "w", "cadence_seconds": 300, "params": {"mode": "unset"}}])
        with self.assertLogs("pionir.crew", level="WARNING") as logs:
            self.tick()
            for _ in range(24 * 60):           # a day, one look a minute
                self.tick(60)
        runs = self.w().runs
        self.assertLessEqual(runs, 12, runs)   # 600, 1200, 2400 ... then every 6 h
        self.assertGreaterEqual(runs, 5, runs)  # still probed: the way back
        parks = [line for line in logs.output if "PARKED" in line]
        self.assertEqual(len(parks), 1, parks)  # said once, not every tick
        st = self.status()
        self.assertEqual(st["state"], "parked")
        self.assertIn("node is not set up", st["parked"]["reason"])
        self.assertLessEqual(st["parked"]["next_probe_at"] - self.time.now(),
                             ctl.PARK_CAP_SECONDS)
        # every probe that ran is still a recorded attempt: the vitals see it
        self.assertEqual(len(self.c.store.runs("alpha.w", limit=100)), runs)

    def test_readiness_turning_ok_unparks_it_and_it_runs_at_once(self) -> None:
        self.crew([{"name": "w", "cadence_seconds": 300,
                    "params": {"mode": "unset", "ready": "NOT SET UP: node"}}])
        self.tick()
        self.assertEqual(self.status()["state"], "parked")
        self.assertTrue(self.status()["parked"]["readiness_gated"])
        self.tick(ctl.READINESS_RECHECK_SECONDS + 1)   # still not ready: still parked
        self.settle()
        self.assertEqual(self.status()["state"], "parked")
        self.assertEqual(self.w().runs, 1)
        self.w().ready, self.w().mode = None, "ok"      # the owner set it up
        self.tick(ctl.READINESS_RECHECK_SECONDS + 1)
        self.settle()
        self.assertEqual(self.status()["state"], "run_requested")
        report = self.tick(1)                           # long before any probe was due
        self.assertEqual((report.succeeded, self.w().runs), (1, 2))
        self.assertEqual(self.status()["state"], "scheduled")
        self.assertEqual(self.status()["consecutive_failures"], 0)
        # readiness is read on the worker pool, never on the loop thread
        self.assertTrue(self.w().readiness_threads)
        self.assertTrue(all(n.startswith("pionir-crew-worker")
                            for n in self.w().readiness_threads), self.w().readiness_threads)

    def test_a_probe_that_succeeds_unparks_it(self) -> None:
        self.crew([{"name": "w", "cadence_seconds": 300, "params": {"mode": "unset"}}])
        self.tick()
        self.w().mode = "ok"                    # readiness never knew: only a probe can tell
        self.tick(ctl.READINESS_RECHECK_SECONDS + 1)
        self.settle()
        self.assertEqual(self.status()["state"], "parked")
        self.tick(2 * 300)
        self.assertEqual(self.status()["state"], "scheduled")

    def test_a_run_now_is_a_way_back_too(self) -> None:
        self.crew([{"name": "w", "cadence_seconds": 300, "params": {"mode": "unset"}}])
        self.tick()
        self.w().mode = "ok"
        self.c.run_now("alpha.w", by="moss")
        self.tick(1)
        self.assertEqual(self.status()["state"], "scheduled")

    def test_a_park_survives_a_restart(self) -> None:
        self.crew([{"name": "w", "cadence_seconds": 300, "params": {"mode": "unset"}}])
        self.tick()
        again = ctl.DispatchControl(self.c.store, self.c.registry, clock=self.time.now)
        self.assertIn("alpha.w", again.parked)
        self.assertEqual(again.rechecks_due(self.time.now()), ["alpha.w"])

    def test_parked_is_visible_in_divisions_and_health(self) -> None:
        self.crew([{"name": "w", "cadence_seconds": 300, "params": {"mode": "unset"}},
                   {"name": "ok"}])
        self.tick()
        alpha = next(d for d in self.c.direction.divisions() if d["division"] == "alpha")
        self.assertEqual(alpha["dispatch"], {"parked": ["alpha.w"], "paused": [],
                                             "retrying": []})
        w = next(x for x in alpha["workers"] if x["id"] == "alpha.w")
        self.assertEqual(w["dispatch"]["state"], "parked")
        self.assertEqual(w["dispatch"]["consecutive_failures"], 1)
        self.assertEqual(self.c.api_health()["dispatch"]["parked"], ["alpha.w"])


class RetryTests(_Case):
    def test_a_retryable_failure_is_retried_on_a_backoff_then_waits_its_cadence(self) -> None:
        """Reverted: a retryable failure waits the whole hour, like a missing secret."""
        self.crew([{"name": "w", "cadence_seconds": 3600, "params": {"mode": "flaky"}}])
        self.tick()
        self.assertEqual(self.status()["state"], "retrying")
        for delay in ctl.RETRY_DELAYS:                  # 1 min, 4 min, 16 min
            self.assertEqual(self.tick(delay - 1).attempted, 0, delay)
            self.assertEqual(self.tick(2).attempted, 1, delay)
        self.assertEqual(self.w().runs, 1 + len(ctl.RETRY_DELAYS))
        st = self.status()
        self.assertEqual(st["state"], "scheduled")       # retries spent: back to the cadence
        self.assertTrue(st["retry"]["exhausted"])
        self.assertEqual(st["consecutive_failures"], 1 + len(ctl.RETRY_DELAYS))
        self.assertEqual(self.tick(1800).attempted, 0)   # no fresh retries in the same streak
        self.w().mode = "ok"
        self.assertEqual(self.tick(3600 * 1.2).attempted, 1)
        st = self.status()
        self.assertEqual((st["state"], st["consecutive_failures"]), ("scheduled", 0))
        self.assertNotIn("retry", st)                    # a success resets it all

    def test_a_retry_is_only_worth_it_well_inside_the_cadence(self) -> None:
        self.crew([{"name": "w", "cadence_seconds": 300, "params": {"mode": "flaky"}}])
        self.tick()
        self.tick(61)                                     # 60 s <= 150 s: retried
        self.assertEqual(self.w().runs, 2)
        self.assertTrue(self.status()["retry"]["exhausted"])   # 240 s > 150 s: not
        self.assertEqual(self.tick(241).attempted, 0)

    def test_a_non_retryable_failure_is_never_retried_early(self) -> None:
        self.crew([{"name": "w", "cadence_seconds": 3600, "params": {"mode": "fatal"}}])
        self.tick()
        self.assertEqual(self.status()["state"], "scheduled")
        for _ in range(30):
            self.tick(60)
        self.assertEqual(self.w().runs, 1)


def _post(crew, path: str, body: dict) -> tuple:
    raw = json.dumps(body).encode()
    crew.api.compat = True               # the token rules have their own tests
    return crew.api.handle("POST", path, client_host="127.0.0.1",
                           headers={"Content-Type": "application/json",
                                    "Content-Length": str(len(raw))},
                           read_body=lambda n: raw[:n])


CONTROL_KEYS = {"state", "next_due_at", "cadence_seconds", "consecutive_failures"}


class ControlApiTests(_Case):
    """The four worker controls over the crew API, and the shapes Moss reads back (pinned,
    as Galatea's tests/test_business_contract.py pins the older crew answers)."""

    def setUp(self) -> None:
        self.crew([{"name": "w", "cadence_seconds": 3600}])
        self.tick()                                       # it has run once: not due for 1 h

    def test_run_now_runs_it_at_the_next_tick(self) -> None:
        status, got = _post(self.c, "/api/worker/run", {"worker": "alpha.w", "by": "moss"})
        self.assertEqual(status, 200, got)
        self.assertEqual(set(got), {"ok", "worker", "by", "queued", "control"})
        self.assertEqual((got["ok"], got["worker"], got["by"], got["queued"]),
                         (True, "alpha.w", "moss", True))
        self.assertTrue(CONTROL_KEYS <= set(got["control"]))
        self.assertEqual(got["control"]["state"], "run_requested")
        self.assertEqual(self.tick(1).attempted, 1)
        self.assertEqual(self.w().runs, 2)
        status, again = _post(self.c, "/api/worker/run", {"worker": "alpha.w"})
        self.assertEqual(status, 400)                     # at most once a minute
        self.assertEqual(set(again), {"ok", "error"})

    def test_an_unknown_worker_is_a_400_in_words(self) -> None:
        for path in ("/api/worker/run", "/api/worker/pause", "/api/worker/resume"):
            status, got = _post(self.c, path, {"worker": "alpha.nope"})
            self.assertEqual(status, 400, path)
            self.assertIn("no such worker", got["error"])
        status, got = _post(self.c, "/api/worker/cadence",
                            {"worker": "alpha.nope", "multiplier": 2})
        self.assertEqual(status, 400)

    def test_pause_holds_it_until_resume_or_expiry(self) -> None:
        status, got = _post(self.c, "/api/worker/pause",
                            {"worker": "alpha.w", "hours": 2, "reason": "noisy"})
        self.assertEqual(status, 200, got)
        self.assertEqual(set(got), {"ok", "worker", "by", "control"})
        self.assertEqual(got["control"]["state"], "paused")
        self.assertEqual(got["control"]["paused"]["reason"], "noisy")
        self.assertEqual(self.tick(3600 * 1.2).attempted, 0)    # due by cadence: held
        status, refused = _post(self.c, "/api/worker/run", {"worker": "alpha.w"})
        self.assertEqual(status, 400)
        self.assertIn("paused", refused["error"])
        self.time.advance(3600)                                  # past the 2 h: it expired
        self.assertEqual(self.tick(1).attempted, 1)
        _post(self.c, "/api/worker/pause", {"worker": "alpha.w"})
        status, got = _post(self.c, "/api/worker/resume", {"worker": "alpha.w"})
        self.assertEqual((status, got["control"]["state"]), (200, "scheduled"))

    def test_a_pause_cannot_latch(self) -> None:
        for hours in (0, -1, 169, 10_000, "forever", True):
            status, got = _post(self.c, "/api/worker/pause", {"worker": "alpha.w",
                                                               "hours": hours})
            self.assertEqual(status, 400, (hours, got))
        _post(self.c, "/api/worker/pause", {"worker": "alpha.w"})    # the default: 24 h
        self.assertEqual(self.c.control.paused["alpha.w"]["until"] - self.time.now(),
                         ctl.DEFAULT_HOURS * 3600)

    def test_a_cadence_multiplier_is_bounded_and_expires(self) -> None:
        for bad in (0.1, 4.5, 0, "2", None):
            status, _ = _post(self.c, "/api/worker/cadence",
                              {"worker": "alpha.w", "multiplier": bad})
            self.assertEqual(status, 400, bad)
        status, got = _post(self.c, "/api/worker/cadence",
                            {"worker": "alpha.w", "multiplier": 0.25, "hours": 3})
        self.assertEqual(status, 200, got)
        self.assertEqual(got["control"]["cadence_multiplier"]["factor"], 0.25)
        self.assertEqual(self.tick(3600 * 0.25 * 1.2).attempted, 1)   # four times as often
        self.time.advance(3 * 3600)                                     # expired
        self.assertNotIn("cadence_multiplier", self.status())
        self.assertEqual(self.status()["cadence_seconds"],
                         round(3600 * jitter("alpha.w"), 1))
        _post(self.c, "/api/worker/cadence", {"worker": "alpha.w", "multiplier": 2})
        _post(self.c, "/api/worker/cadence", {"worker": "alpha.w", "multiplier": 1})
        self.assertNotIn("alpha.w", self.c.control.multipliers)       # 1 ends it

    def test_controls_are_recorded_and_survive_a_restart(self) -> None:
        _post(self.c, "/api/worker/pause", {"worker": "alpha.w", "by": "moss"})
        _post(self.c, "/api/worker/cadence", {"worker": "alpha.w", "multiplier": 2})
        again = ctl.DispatchControl(self.c.store, self.c.registry, clock=self.time.now)
        self.assertEqual(again.paused["alpha.w"]["by"], "moss")
        self.assertEqual(again.multipliers["alpha.w"]["factor"], 2.0)


class LeaderRerunTests(_Case):
    def setUp(self) -> None:
        self.crew([{"name": "w", "cadence_seconds": 3600}])
        self.tick()

    def leader(self, rerun):
        text = json.loads(reply())
        text["rerun"] = rerun
        return Leader("alpha", self.c.registry, self.c.store, ask=FakeAsk(json.dumps(text)),
                      escalator=self.c.escalator, model="m", clock=self.time.now,
                      rerun=self.c.leader_rerun)

    def test_a_leaders_rerun_queues_its_own_worker_bounded_per_day(self) -> None:
        lead = self.leader(["alpha.w"])
        schema_seen = []
        for n in range(ctl.LEADER_RUNS_PER_DAY + 1):
            self.c.dispatcher.dispatch(only=["alpha.w"], wait=True)   # something new to say
            self.time.advance(ctl.RUN_NOW_MIN_SPACING + 1)
            got = lead.run()
            self.assertIsInstance(got, Ok, got)
            schema_seen.append(lead.ask.calls[-1]["fmt"]["properties"]["rerun"])
            asked = self.c.store.reports(division="alpha")[0]["provenance"]["rerun"]
            if n < ctl.LEADER_RUNS_PER_DAY:
                self.assertEqual(asked, [{"worker": "alpha.w", "queued": True}], n)
                self.assertEqual(self.tick(1).attempted, 1)
            else:
                self.assertFalse(asked[0]["queued"])
                self.assertIn("run-now requests for today", asked[0]["why"])
                self.assertEqual(self.tick(1).attempted, 0)
        # the schema let it name only its own division's live workers
        self.assertEqual(schema_seen[0]["items"]["enum"], ["alpha.w"])

    def test_a_leader_cannot_rerun_another_divisions_worker(self) -> None:
        got = self.c.leader_rerun("alpha", "beta.b1")
        self.assertFalse(got["queued"])
        self.assertIn("not in the alpha division", got["why"])
        lead = self.leader(["beta.b1"])
        self.c.dispatcher.dispatch(only=["alpha.w"], wait=True)
        lead.run()
        asked = self.c.store.reports(division="alpha")[0]["provenance"]["rerun"]
        self.assertEqual(asked, [{"worker": "beta.b1", "queued": False,
                                  "why": "not a live worker of this division"}])


class EscalationNoiseTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = temp_dir()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name

    def crew(self, **kw):
        self.time = FakeTime()
        crew = make_crew(self.root, cat=catalogue({"alpha": [{"name": "ledger"}],
                                                    "beta": [{"name": "b1"}]}),
                         now=self.time.now, **kw)
        self.addCleanup(crew.stop)
        return crew

    def fresh(self, crew) -> None:
        """A new run of the ledger, a moment after the leader last looked."""
        self.time.advance(5)
        crew.dispatcher.dispatch(only=["alpha.ledger"], wait=True)
        self.time.advance(5)

    def test_unchanged_facts_are_not_put_to_claude_again(self) -> None:
        """Reverted: every report about the same zero orders spends a Claude call."""
        claude = FakeClaude(answer="Hold the price; nothing recorded argues otherwise.")
        crew = self.crew(claude=claude)
        lead = Leader("alpha", crew.registry, crew.store,
                      ask=FakeAsk(reply(escalate=True, question="Change the price?")),
                      escalator=crew.escalator, model="m", clock=self.time.now)
        self.fresh(crew)
        first = lead.run().value
        self.assertEqual(first.escalation["answer"], claude.answer)
        for _ in range(3):                    # new rows, the same facts
            self.fresh(crew)
            again = lead.run().value
            self.assertEqual(again.escalation["answer"], claude.answer)
            self.assertIn("unchanged_since", again.escalation)
        self.assertEqual(len(claude.prompts), 1)
        crew.registry.require("alpha.ledger").value = 1500      # a material change
        self.fresh(crew)
        changed = lead.run().value
        self.assertNotIn("unchanged_since", changed.escalation or {})
        self.assertEqual(len(claude.prompts), 2)

    def test_a_refused_escalation_is_not_remembered_as_asked(self) -> None:
        claude = FakeClaude()
        crew = self.crew(claude=claude, claude_daily_cap=10, claude_leader_cap=0)
        lead = Leader("alpha", crew.registry, crew.store,
                      ask=FakeAsk(reply(escalate=True, question="q?")),
                      escalator=crew.escalator, model="m", clock=self.time.now)
        self.fresh(crew)
        self.assertIn("refused", lead.run().value.escalation)
        self.assertIsNone(crew.store.get("escalated:alpha"))

    def test_the_leaders_cannot_take_the_workers_share_of_claude(self) -> None:
        """Reverted: report escalations spend the day's whole cap; builds get nothing."""
        claude = FakeClaude()
        crew = self.crew(claude=claude, claude_daily_cap=10)
        self.assertEqual(crew.escalator.leader_cap, 4)
        got = [crew.escalator.escalate(d, "q", "c") for d in ("alpha", "beta") * 3]
        self.assertEqual([type(g).__name__ for g in got], ["Ok"] * 4 + ["Err"] * 2)
        self.assertIn("kept for the workers", got[-1].error)
        # the workers' real work still has the rest of the day
        research = [crew.escalator.research("alpha", "find x") for _ in range(6)]
        self.assertTrue(all(isinstance(r, Ok) for r in research), research)
        self.assertIsInstance(crew.escalator.research("alpha", "one more"), Err)  # daily cap
        snap = crew.escalator.snapshot()
        self.assertEqual((snap["leader_cap"], snap["leaders_used_today"], snap["used_today"]),
                         (4, 4, 10))


class LoopTests(_Case):
    def test_the_crew_loop_parks_rechecks_and_unparks(self) -> None:
        """Through Crew.step - the loop thread's own body - not the dispatcher alone."""
        self.crew([{"name": "w", "cadence_seconds": 300,
                    "params": {"mode": "unset", "ready": "NOT SET UP"}}])
        self.c.step()
        deadline = time.monotonic() + 5
        while self.w().runs < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        while (self.c.control.parked.get("alpha.w") or {}).get("gated") is None \
                and time.monotonic() < deadline:          # its readiness has been read
            time.sleep(0.01)
        self.assertIn("alpha.w", self.c.control.parked)
        self.w().ready, self.w().mode = None, "ok"
        self.time.advance(ctl.READINESS_RECHECK_SECONDS + 1)
        self.c.step()                     # hands the re-check to the pool
        self.settle()
        self.c.step()                     # runs it
        while self.w().runs < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.w().runs, 2)


if __name__ == "__main__":
    unittest.main()
