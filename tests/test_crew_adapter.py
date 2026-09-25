"""Pionir's crew adapter: Moss reads and directs the crew through Pionir, and it is audited.

Each test fails if its rule is reverted: a capability calling the wrong endpoint or not
passing the crew's answer through, a malformed payload reaching the crew, a crew
refusal counted as a fault, a crew that is not running hanging the caller or tripping
the breaker instead of saying so, a risk level or routability changing, a crew write
that bypasses Pionir's ledger, or the crew record not naming the Pionir task that asked.
"""

import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from uuid import UUID

from crew_support import FakeTime, temp_dir
from test_crew_fakes import catalogue, make_crew

from pionir.adapters._http import HttpStatusError
from pionir.adapters.crew import NOT_RUNNING, CrewAdapter, CrewAdapterSettings
from pionir.bootstrap import build_runtime
from pionir.config import PionirSettings
from pionir.contracts import RiskLevel, Task, outcome_kind
from pionir.errors import AdapterProtocolError, AdapterUnavailable
from pionir.reliability import CircuitState
from pionir.server import PionirApp


class FakeClient:
    """Records every call; answers from ``answers`` keyed by path (or raises)."""

    def __init__(self, answers=None) -> None:
        self.answers = dict(answers or {})
        self.calls: list = []

    def _answer(self, path):
        answer = self.answers.get(path.split("?")[0], {"ok": True, "echo": path})
        if isinstance(answer, Exception):
            raise answer
        return answer

    def get(self, path, *, timeout_seconds=None):
        self.calls.append(("GET", path, None))
        return self._answer(path)

    def post(self, path, payload, *, timeout_seconds=None):
        self.calls.append(("POST", path, dict(payload)))
        return self._answer(path)


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class EndpointTests(unittest.TestCase):
    def run_task(self, capability, payload=None, answers=None):
        client = FakeClient(answers)
        task = Task(capability, payload or {})
        result = CrewAdapter(client=client).execute(task)
        return client, task, result

    def test_reads_hit_their_endpoints_and_pass_the_answer_through(self) -> None:
        answer = {"ok": True, "divisions": [{"division": "alpha"}], "truncated": False}
        for capability, path in (("crew.digest", "/api/digest?max_chars=4000"),
                                 ("crew.divisions", "/api/divisions"),
                                 ("crew.compute", "/api/compute")):
            with self.subTest(capability=capability):
                client, task, result = self.run_task(
                    capability, answers={path.split("?")[0]: answer})
                self.assertEqual(client.calls, [("GET", path, None)])
                self.assertEqual(dict(result.output), answer)
                self.assertEqual(result.agent_id, "crew")
                self.assertEqual(result.task_id, task.task_id)

    def test_the_digest_budget_is_passed_on(self) -> None:
        client, _task, _result = self.run_task("crew.digest", {"max_chars": 1500})
        self.assertEqual(client.calls, [("GET", "/api/digest?max_chars=1500", None)])

    def test_set_goal_posts_the_goal_and_names_the_pionir_task(self) -> None:
        answer = {"ok": True, "division": "alpha", "direction": {"goal": "g"}}
        client, task, result = self.run_task(
            "crew.set_goal", {"division": "alpha", "goal": " find buyers ", "priority": 2},
            answers={"/api/goal": answer})
        self.assertEqual(client.calls, [("POST", "/api/goal", {
            "division": "alpha", "goal": "find buyers", "priority": 2,
            "by": f"moss via pionir task {task.task_id}"})])
        self.assertEqual(dict(result.output), answer)
        self.assertIn("crew:goal:alpha", result.evidence)

    def test_allocate_posts_the_shares(self) -> None:
        client, task, result = self.run_task(
            "crew.allocate", {"resource": "claude_escalations", "shares": {"alpha": 0.5},
                              "by": "galatea"},
            answers={"/api/allocate": {"ok": True, "resource": "claude_escalations"}})
        self.assertEqual(client.calls, [("POST", "/api/allocate", {
            "resource": "claude_escalations", "shares": {"alpha": 0.5},
            "by": f"galatea via pionir task {task.task_id}"})])
        self.assertIn("crew:allocate:claude_escalations", result.evidence)

    def test_a_malformed_payload_never_reaches_the_crew(self) -> None:
        bad = [
            ("crew.allocate", {"resource": "money", "shares": {"alpha": 0.5}}),
            ("crew.allocate", {"resource": "model_calls", "shares": {"alpha": 2}}),
            ("crew.allocate", {"resource": "model_calls"}),
            ("crew.set_goal", {"division": "alpha"}),
            ("crew.set_goal", {"division": "alpha", "goal": "x", "priority": 0}),
            ("crew.set_goal", {"division": "alpha", "goal": "x", "by": "x" * 60}),
            ("crew.set_goal", {"division": "alpha", "goal": "x", "by": 7}),
            ("crew.digest", {"max_chars": "lots"}),
            ("crew.digest", {"max_chars": 10}),
        ]
        for capability, payload in bad:
            with self.subTest(capability=capability, payload=payload):
                client = FakeClient()
                adapter = CrewAdapter(client=client)
                with self.assertRaises(AdapterProtocolError):
                    adapter.validate(Task(capability, payload))
                with self.assertRaises(AdapterProtocolError):
                    adapter.execute(Task(capability, payload))
                self.assertEqual(client.calls, [])

    def test_money_is_refused_in_the_crews_own_words(self) -> None:
        with self.assertRaises(AdapterProtocolError) as caught:
            CrewAdapter(client=FakeClient()).validate(
                Task("crew.allocate", {"resource": "money", "shares": {"alpha": 1}}))
        self.assertIn("no money lever", str(caught.exception))

    def test_a_crew_refusal_is_an_answer_not_a_fault(self) -> None:
        refusal = HttpStatusError("the crew answered HTTP 400: unknown division 'nope'",
                                  status=400)
        _c, _t, result = self.run_task("crew.set_goal", {"division": "nope", "goal": "x"},
                                       answers={"/api/goal": refusal})
        self.assertIs(result.output["ok"], False)
        self.assertIn("unknown division", result.output["refused"])
        self.assertEqual(outcome_kind(result.output), "refused")

    def test_a_crew_fault_is_raised(self) -> None:
        fault = HttpStatusError("the crew answered HTTP 500: boom", status=500)
        with self.assertRaises(HttpStatusError):
            self.run_task("crew.compute", answers={"/api/compute": fault})
        with self.assertRaises(AdapterProtocolError):
            self.run_task("crew.compute", answers={"/api/compute": {"compute": {}}})


class ManifestTests(unittest.TestCase):
    def test_risk_levels_routability_and_no_model(self) -> None:
        caps = {c.name: c for c in CrewAdapter().manifest.capabilities}
        self.assertEqual(set(caps), {"crew.digest", "crew.divisions", "crew.compute",
                                     "crew.set_goal", "crew.allocate"})
        for name in ("crew.digest", "crew.divisions", "crew.compute"):
            self.assertIs(caps[name].risk, RiskLevel.READ_ONLY, name)
        for name in ("crew.set_goal", "crew.allocate"):
            self.assertIs(caps[name].risk, RiskLevel.REVERSIBLE_WRITE, name)
        for cap in caps.values():
            self.assertFalse(cap.routable, cap.name)
            self.assertIsNone(cap.model, cap.name)
            self.assertFalse(cap.spends_money, cap.name)
            self.assertEqual(cap.required_permissions, frozenset(), cap.name)

    def test_the_url_must_be_loopback(self) -> None:
        with self.assertRaises(ValueError):
            CrewAdapterSettings(base_url="http://10.0.0.5:8782")


class CrewDownTests(unittest.TestCase):
    def test_a_crew_that_is_not_running_is_said_plainly_and_quickly(self) -> None:
        adapter = CrewAdapter(CrewAdapterSettings(base_url=f"http://127.0.0.1:{closed_port()}"))
        for capability, payload in (("crew.digest", {}),
                                    ("crew.set_goal", {"division": "a", "goal": "x"})):
            with self.subTest(capability=capability):
                started = time.monotonic()
                result = adapter.execute(Task(capability, payload))
                self.assertLess(time.monotonic() - started, 10)
                self.assertIs(result.output["ok"], False)
                self.assertEqual(result.output["unavailable"], NOT_RUNNING)
                self.assertTrue(result.output["error"].startswith("the crew is not running"))
                self.assertIn("python -m pionir.crew", result.output["error"])
        with self.assertRaises(AdapterUnavailable) as caught:
            adapter.status()
        self.assertIn(NOT_RUNNING, str(caught.exception))

    def test_a_crew_that_accepts_but_never_answers_does_not_hang_the_caller(self) -> None:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        self.addCleanup(listener.close)
        adapter = CrewAdapter(CrewAdapterSettings(
            base_url=f"http://127.0.0.1:{listener.getsockname()[1]}"))
        started = time.monotonic()
        result = adapter.execute(Task("crew.compute", {}))
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(result.output["unavailable"], NOT_RUNNING)


def _runtime(root: str, crew_url: str | None):
    # Hermetic, as in test_server: nothing but the crew under test is reachable.
    return build_runtime(PionirSettings(
        state_root=Path(root),
        atani_command=("pionir-test-no-such-binary",),
        daedalus_url=None, melete_url=None, galatea_url=None,
        bryo_status_command=None, nyx_status_command=None, voodoo_status_command=None,
        embed_model=None, evict_to_fit=False, crew_url=crew_url,
    ))


class WiringTests(unittest.TestCase):
    def test_the_crew_url_setting(self) -> None:
        self.assertEqual(PionirSettings(state_root=Path("C:/x")).crew_url,
                         "http://127.0.0.1:8782")
        with temp_dir() as root, mock.patch.dict(os.environ, {"PIONIR_STATE_ROOT": root}):
            os.environ.pop("PIONIR_CREW_URL", None)
            self.assertEqual(PionirSettings.from_environment().crew_url,
                             "http://127.0.0.1:8782")
            os.environ["PIONIR_CREW_URL"] = "http://127.0.0.1:9100"
            self.assertEqual(PionirSettings.from_environment().crew_url,
                             "http://127.0.0.1:9100")
            os.environ["PIONIR_CREW_URL"] = "off"
            self.assertIsNone(PionirSettings.from_environment().crew_url)

    def test_bootstrap_registers_the_crew_only_when_configured(self) -> None:
        for url, present in ((f"http://127.0.0.1:{closed_port()}", True), (None, False)):
            with tempfile.TemporaryDirectory() as root:
                runtime = _runtime(root, url)
                try:
                    self.assertEqual("crew" in runtime.adapters, present)
                finally:
                    runtime.cortex.close()


class ThroughPionirTests(unittest.TestCase):
    """The real path: PionirApp.run_task -> executive -> adapter -> the crew's HTTP API."""

    def setUp(self) -> None:
        crew_tmp = temp_dir()
        self.addCleanup(crew_tmp.cleanup)
        self.crew = make_crew(crew_tmp.name, now=FakeTime().now, api_port=0,
                              cat=catalogue({"alpha": [{"name": "a1"}],
                                             "beta": [{"name": "b1"}]}))
        self.addCleanup(self.crew.stop)
        self.assertTrue(self.crew.api.start())
        self.pionir_tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.pionir_tmp.cleanup)
        self.app = PionirApp(_runtime(self.pionir_tmp.name,
                                      f"http://127.0.0.1:{self.crew.api.port}"))
        self.addCleanup(self.app.runtime.cortex.close)

    def ledger(self) -> list:
        return self.app.runtime.executive.audit_sink.recent(200)

    def test_set_goal_changes_the_crew_and_is_in_the_ledger(self) -> None:
        out = self.app.run_task("crew.set_goal",
                                {"division": "beta", "goal": "ship the blog", "priority": 1})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["result"]["direction"]["goal"], "ship the blog")
        beta = next(d for d in self.crew.direction.divisions() if d["division"] == "beta")
        self.assertEqual((beta["goal"], beta["priority"]), ("ship the blog", 1))
        events = [e for e in self.ledger() if e["agent_id"] == "crew"]
        self.assertEqual({e["event_type"] for e in events}, {"task.routed", "task.completed"})
        # the crew's record names the very Pionir task in the ledger that asked
        by = self.crew.store.directions()["beta"]["by"]
        self.assertTrue(by.startswith("moss via pionir task "), by)
        task_id = UUID(by.rsplit(" ", 1)[1])
        self.assertIn(str(task_id), {e["task_id"] for e in events})

    def test_allocate_and_read_back_through_pionir(self) -> None:
        out = self.app.run_task("crew.allocate",
                                {"resource": "model_calls", "shares": {"alpha": 0.2}})
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.crew.direction.compute()["model_calls"]["shares"]["alpha"], 0.2)
        read = self.app.run_task("crew.compute", {})
        self.assertTrue(read["ok"], read)
        self.assertEqual(read["result"]["compute"]["model_calls"]["shares"],
                         {"alpha": 0.2, "beta": 0.8})
        digest = self.app.run_task("crew.digest", {"max_chars": 2000})
        self.assertEqual(digest["result"]["max_chars"], 2000)
        completed = [e for e in self.ledger()
                     if e["agent_id"] == "crew" and e["event_type"] == "task.completed"]
        self.assertEqual(len(completed), 3)

    def test_money_is_refused_before_anything_runs(self) -> None:
        out = self.app.run_task("crew.allocate",
                                {"resource": "money", "shares": {"alpha": 1.0}})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["type"], "AdapterProtocolError")
        self.assertNotEqual(out.get("status"), "pending_approval")
        self.assertEqual(self.crew.store.allocations(), {})

    def test_a_crew_refusal_is_ledgered_as_refused_and_leaves_the_circuit_closed(self) -> None:
        for _ in range(4):
            out = self.app.run_task("crew.set_goal", {"division": "nope", "goal": "x"})
            self.assertFalse(out["ok"])
            self.assertIn("no such division", out["result"]["refused"])
        self.assertIs(self.app.runtime.executive.circuit("crew").snapshot().state,
                      CircuitState.CLOSED)
        failed = [e for e in self.ledger() if e["event_type"] == "task.failed"]
        self.assertTrue(failed and all(e["detail"].startswith("refused:") for e in failed))

    def test_a_stopped_crew_is_said_plainly_and_does_not_trip_the_breaker(self) -> None:
        self.crew.api.stop()
        for _ in range(3):
            out = self.app.run_task("crew.digest", {})
            self.assertFalse(out["ok"])
            self.assertEqual(out["result"]["unavailable"], NOT_RUNNING)
        self.assertIs(self.app.runtime.executive.circuit("crew").snapshot().state,
                      CircuitState.CLOSED)


if __name__ == "__main__":
    unittest.main()
