"""The worker controls as Pionir capabilities, and the shapes Moss reads back.

``crew.run_worker``, ``crew.pause_worker``, ``crew.resume_worker`` and ``crew.set_cadence``
sit beside ``crew.set_goal`` / ``crew.allocate``: the same permission class (REVERSIBLE_WRITE,
no approval, no money), the same adapter, the same refusal shapes. Moss's side lives in
Galatea, so the SHAPES are pinned here the way Galatea's tests/test_business_contract.py pins
the older crew answers: if one changes, these fail, and Galatea's side must be checked with it.

Each test fails if its rule is reverted: a control reaching the wrong endpoint, a malformed
control (an unbounded pause, a cadence x10, no worker) reaching the crew, a crew refusal
counted as a fault, a control that bypasses Pionir's ledger, or an answer whose shape drifts.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from crew_support import FakeTime, temp_dir
from test_crew_adapter import FakeClient, _runtime
from test_crew_fakes import catalogue, make_crew

from pionir.adapters.crew import WORKER_CONTROLS, CrewAdapter
from pionir.auth import token_path
from pionir.contracts import RiskLevel, Task
from pionir.errors import AdapterProtocolError
from pionir.server import PionirApp

# What Moss gets back, exactly (Pionir's TaskResult.output is the crew's answer)
RUN_KEYS = {"ok", "worker", "by", "queued", "control"}
CONTROL_ANSWER_KEYS = {"ok", "worker", "by", "control"}
REFUSED_KEYS = {"ok", "refused", "error"}
CONTROL_STATE_KEYS = {"state", "next_due_at", "cadence_seconds", "consecutive_failures"}
STATES = {"scheduled", "running", "run_requested", "parked", "paused", "retrying"}


class AdapterTests(unittest.TestCase):
    def run_task(self, capability, payload):
        client = FakeClient()
        task = Task(capability, payload)
        CrewAdapter(client=client).execute(task)
        return client, task

    def test_each_control_posts_to_its_route_and_names_the_pionir_task(self) -> None:
        for capability, payload in (
                ("crew.run_worker", {"worker": "alpha.a1"}),
                ("crew.pause_worker", {"worker": "alpha.a1", "hours": 2, "reason": "noisy"}),
                ("crew.resume_worker", {"worker": "alpha.a1"}),
                ("crew.set_cadence", {"worker": "alpha.a1", "multiplier": 0.5, "hours": 6})):
            with self.subTest(capability=capability):
                client, task = self.run_task(capability, payload)
                ((method, path, body),) = client.calls
                self.assertEqual((method, path), ("POST", WORKER_CONTROLS[capability]))
                self.assertEqual(body, {**payload, "by": f"moss via pionir task {task.task_id}"})

    def test_a_malformed_control_never_reaches_the_crew(self) -> None:
        for capability, payload in (
                ("crew.run_worker", {}),
                ("crew.run_worker", {"worker": 7}),
                ("crew.pause_worker", {"worker": "alpha.a1", "hours": 169}),
                ("crew.pause_worker", {"worker": "alpha.a1", "hours": 0}),
                ("crew.set_cadence", {"worker": "alpha.a1"}),
                ("crew.set_cadence", {"worker": "alpha.a1", "multiplier": 10}),
                ("crew.set_cadence", {"worker": "alpha.a1", "multiplier": 0.1}),
                ("crew.set_cadence", {"worker": "alpha.a1", "multiplier": 2, "hours": 1000}),
                ("crew.resume_worker", {"worker": "alpha.a1", "by": "x" * 60})):
            with self.subTest(capability=capability, payload=payload):
                client = FakeClient()
                with self.assertRaises(AdapterProtocolError):
                    CrewAdapter(client=client).validate(Task(capability, payload))
                self.assertEqual(client.calls, [])

    def test_same_permission_class_as_set_goal(self) -> None:
        caps = {c.name: c for c in CrewAdapter().manifest.capabilities}
        for name in WORKER_CONTROLS:
            self.assertIs(caps[name].risk, caps["crew.set_goal"].risk, name)
            self.assertIs(caps[name].risk, RiskLevel.REVERSIBLE_WRITE)
            self.assertFalse(caps[name].requires_approval, name)
            self.assertFalse(caps[name].spends_money, name)
            self.assertFalse(caps[name].routable, name)


class ThroughPionirTests(unittest.TestCase):
    """The real path: PionirApp.run_task -> executive -> adapter -> the crew's HTTP API."""

    def setUp(self) -> None:
        crew_tmp = temp_dir()
        self.addCleanup(crew_tmp.cleanup)
        self.time = FakeTime()
        self.crew = make_crew(crew_tmp.name, now=self.time.now, api_port=0,
                              cat=catalogue({"alpha": [{"name": "a1", "cadence_seconds": 3600}],
                                             "beta": [{"name": "b1"}]}))
        self.addCleanup(self.crew.stop)
        self.assertTrue(self.crew.api.start())
        self.pionir_tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.pionir_tmp.cleanup)
        self.app = PionirApp(_runtime(self.pionir_tmp.name,
                                      f"http://127.0.0.1:{self.crew.api.port}"))
        self.addCleanup(self.app.runtime.cortex.close)
        self.crew.api.token_file = token_path(Path(self.pionir_tmp.name) / "secrets", "crew")
        self.crew.api.compat = False
        self.crew.dispatcher.dispatch(wait=True)          # each has run once

    def ledgered(self) -> list:
        return [e for e in self.app.runtime.executive.audit_sink.recent(200)
                if e["agent_id"] == "crew" and e["event_type"] == "task.completed"]

    def test_run_worker_runs_it_and_the_answer_has_its_pinned_shape(self) -> None:
        out = self.app.run_task("crew.run_worker", {"worker": "alpha.a1"})
        self.assertTrue(out["ok"], out)
        got = out["result"]
        self.assertEqual(set(got), RUN_KEYS)
        self.assertEqual((got["ok"], got["worker"], got["queued"]), (True, "alpha.a1", True))
        self.assertTrue(got["by"].startswith("moss via pionir task "), got["by"])
        self.assertTrue(CONTROL_STATE_KEYS <= set(got["control"]))
        self.assertIn(got["control"]["state"], STATES)
        self.assertEqual(got["control"]["state"], "run_requested")
        self.time.advance(1)
        self.assertEqual(self.crew.dispatcher.dispatch(wait=True).attempted, 1)
        self.assertEqual(len(self.crew.store.runs("alpha.a1")), 2)
        self.assertEqual(len(self.ledgered()), 1)

    def test_pause_resume_and_cadence_through_pionir(self) -> None:
        out = self.app.run_task("crew.pause_worker", {"worker": "alpha.a1", "hours": 1})
        self.assertTrue(out["ok"], out)
        self.assertEqual(set(out["result"]), CONTROL_ANSWER_KEYS)
        self.assertEqual(out["result"]["control"]["state"], "paused")
        self.assertIn("alpha.a1", self.crew.control.paused)
        # the crew's record names the very Pionir task that asked
        self.assertTrue(self.crew.control.paused["alpha.a1"]["by"].startswith(
            "moss via pionir task "))
        out = self.app.run_task("crew.resume_worker", {"worker": "alpha.a1"})
        self.assertEqual(out["result"]["control"]["state"], "scheduled")
        out = self.app.run_task("crew.set_cadence", {"worker": "alpha.a1", "multiplier": 2})
        self.assertEqual(set(out["result"]), CONTROL_ANSWER_KEYS)
        self.assertEqual(out["result"]["control"]["cadence_multiplier"]["factor"], 2.0)
        self.assertEqual(len(self.ledgered()), 3)

    def test_a_refusal_has_the_refused_shape_moss_already_reads(self) -> None:
        out = self.app.run_task("crew.run_worker", {"worker": "alpha.nope"})
        self.assertFalse(out["ok"])
        self.assertEqual(set(out["result"]), REFUSED_KEYS)
        self.assertIn("no such worker", out["result"]["refused"])
        self.app.run_task("crew.pause_worker", {"worker": "alpha.a1"})
        out = self.app.run_task("crew.run_worker", {"worker": "alpha.a1"})
        self.assertEqual(set(out["result"]), REFUSED_KEYS)
        self.assertIn("paused", out["result"]["refused"])


if __name__ == "__main__":
    unittest.main()
