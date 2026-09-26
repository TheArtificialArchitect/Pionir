"""The job model: work that outlives the request.

A slow specialist must not lose its result to a client that gave up. The job
file is the artifact - written before the answer, rewritten when the work ends -
and these tests read the file, not the log line.
"""

import http.client
import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from standins import down_url

from pionir.bootstrap import build_runtime
from pionir.config import PionirSettings
from pionir.contracts import AgentManifest, Capability, RiskLevel, Task, TaskResult
from pionir.server import Jobs, PionirApp, _make_handler


class SlowAdapter:
    """A specialist that takes a while, plus a privileged capability for the gate."""

    def __init__(self, delay: float = 0.6) -> None:
        self.delay = delay
        self.calls = 0
        self._manifest = AgentManifest(
            "slowpoke",
            "test",
            (
                Capability("test.slow_read", "A slow read", routing_hints=frozenset({"tortoise"})),
                Capability(
                    "test.slow_privileged", "A slow privileged thing",
                    risk=RiskLevel.PRIVILEGED, required_permissions=frozenset({"slow.run"}),
                ),
            ),
        )

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def execute(self, task: Task) -> TaskResult:
        self.calls += 1
        time.sleep(self.delay)
        return TaskResult(task.task_id, "slowpoke", {"answer": 42, "cap": task.capability}, ("fake",))


def _runtime(tmp: str):
    runtime = build_runtime(
        PionirSettings(
            state_root=Path(tmp),
            atani_command=("pionir-test-no-such-binary",),
            galatea_url=down_url(),
            galatea_model_id="stub-model",
            embed_model=None,  # never reach the live Ollama embedder from a test
            daedalus_url=down_url(),
            melete_url=down_url(),
            bryo_status_command=None,
            nyx_status_command=None,
            voodoo_status_command=None,
            evict_to_fit=False,
        )
    )
    runtime.register(SlowAdapter())
    return runtime


class JobModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.app = PionirApp(_runtime(self._tmp.name))
        self.tasks_dir = Path(self._tmp.name) / "tasks"

    def tearDown(self) -> None:
        self.app.runtime.cortex.close()
        self._tmp.cleanup()

    def _file(self, task_id: str) -> dict:
        return json.loads((self.tasks_dir / f"{task_id}.json").read_text(encoding="utf-8"))

    def test_slow_task_answers_running_then_the_job_file_holds_the_result(self) -> None:
        out = self.app.run_task("test.slow_read", {}, wait=0.1)
        self.assertEqual(out["status"], "running")
        tid = out["task_id"]
        # persisted BEFORE the answer: the file exists while it is still running
        self.assertEqual(self._file(tid)["status"], "running")
        self.assertTrue(self.app.jobs.wait(tid, 30))
        record = self.app.job(tid)
        self.assertEqual(record["status"], "done")
        self.assertEqual(record["kind"], "task")
        self.assertEqual(record["result"]["result"]["answer"], 42)
        self.assertTrue(record["result"]["ok"])
        self.assertIsNotNone(record["finished_at"])
        # and the artifact on disk says the same, for a client that comes back
        self.assertEqual(self._file(tid)["result"]["result"]["answer"], 42)

    def test_a_fast_task_keeps_the_synchronous_shape_plus_task_id(self) -> None:
        out = self.app.run_task("test.slow_read", {}, wait=30)
        self.assertTrue(out["ok"])
        self.assertEqual(out["result"]["answer"], 42)
        self.assertEqual(self.app.job(out["task_id"])["status"], "done")

    def test_result_is_kept_even_when_no_client_waits_at_all(self) -> None:
        out = self.app.run_task("test.slow_read", {}, wait=0)
        self.assertEqual(out["status"], "running")
        self.assertTrue(self.app.jobs.wait(out["task_id"], 30))
        self.assertEqual(self._file(out["task_id"])["status"], "done")

    def test_approve_claims_first_and_a_second_tap_is_refused(self) -> None:
        parked = self.app.run_task("test.slow_privileged", {"content": "go"})
        self.assertEqual(parked["status"], "pending_approval")
        self.assertEqual(self.app.job(parked["task_id"])["status"], "pending_approval")
        aid = parked["approval_id"]
        first = self.app.approve(aid)
        self.assertEqual(first["status"], "running")
        second = self.app.approve(aid)
        self.assertFalse(second["ok"])
        self.assertEqual(second["error"]["type"], "AlreadyResolved")
        self.assertTrue(self.app.jobs.wait(first["task_id"], 30))
        record = self.app.approvals.get(aid)
        self.assertEqual(record["status"], "approved")
        self.assertEqual(record["result"]["result"]["answer"], 42)
        # it ran exactly once
        adapter = self.app.runtime.adapters["slowpoke"]
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(self.app.job(first["task_id"])["kind"], "approval")

    def test_unclear_and_self_intents_are_recorded_jobs_and_routed_events(self) -> None:
        out = self.app.intent("photosynthesis in tomato plants")
        self.assertEqual(out["status"], "unclear")
        self.assertEqual(self.app.job(out["task_id"])["status"], "unclear")
        out = self.app.intent("let's just chat for a while")
        self.assertEqual(out["status"], "self")
        self.assertEqual(self.app.job(out["task_id"])["status"], "self")
        details = [e["detail"] for e in self.app.runtime.executive.audit_sink.recent(10)
                   if e["event_type"] == "task.routed"]
        self.assertTrue(any("handled=voice_unclear" in d for d in details))
        self.assertTrue(any("handled=voice_self" in d for d in details))

    def test_an_executed_intent_records_its_confidence(self) -> None:
        out = self.app.intent("tortoise please", wait=30)
        self.assertEqual(out["status"], "done")
        routed = [e for e in self.app.runtime.executive.audit_sink.recent(10)
                  if e["event_type"] == "task.routed" and e["agent_id"] == "slowpoke"]
        self.assertTrue(routed)
        self.assertIn("confidence=", routed[0]["detail"] or "")

    def test_explain_only_route_still_writes_a_routed_event(self) -> None:
        self.app.route("tortoise please", execute=False)
        details = [e["detail"] for e in self.app.runtime.executive.audit_sink.recent(5)
                   if e["event_type"] == "task.routed"]
        self.assertTrue(any("executed=false" in d for d in details))

    def test_recent_lists_newest_first_without_results(self) -> None:
        a = self.app.run_task("test.slow_read", {}, wait=30)["task_id"]
        b = self.app.run_task("test.slow_read", {}, wait=30)["task_id"]
        listed = self.app.jobs_view(10)["tasks"]
        self.assertEqual([t["task_id"] for t in listed][:2], [b, a])
        self.assertNotIn("result", listed[0])

    def test_the_directory_is_bounded(self) -> None:
        self.app.jobs.keep = 3
        for _ in range(6):
            self.app.run_task("test.slow_read", {}, wait=30)
        self.assertLessEqual(len(list(self.tasks_dir.glob("*.json"))), 4)  # newest kept

    def test_unknown_task_is_none(self) -> None:
        self.assertIsNone(self.app.job("nope"))
        self.assertIsNone(self.app.job("../../etc/passwd"))

    def test_restart_marks_orphaned_running_job_interrupted(self) -> None:
        record = self.app.jobs.create("task", {"capability": "test.slow_read"})
        restarted = Jobs(self.tasks_dir)
        recovered = restarted.get(record["task_id"])
        self.assertEqual(recovered["status"], "error")
        self.assertEqual(recovered["error"]["type"], "Interrupted")
        self.assertEqual(recovered["result"]["task_id"], record["task_id"])


class HttpJobTests(unittest.TestCase):
    """The wire: 202 while running, then GET the job."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.app = PionirApp(_runtime(self._tmp.name))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self.app))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.app.runtime.cortex.close()
        self._tmp.cleanup()

    def _post(self, path: str, body: dict, headers: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request("POST", path, body=json.dumps(body),
                     headers={"Content-Type": "application/json", **(headers or {})})
        response = conn.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        conn.close()
        return response.status, data

    def _get(self, path: str):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request("GET", path)
        response = conn.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        conn.close()
        return response.status, data

    def test_202_then_get_task(self) -> None:
        status, out = self._post("/api/task", {"capability": "test.slow_read", "wait": 0.1})
        self.assertEqual(status, 202)
        self.assertEqual(out["status"], "running")
        status, record = self._get(f"/api/task/{out['task_id']}?wait=30")
        self.assertEqual(status, 200)
        self.assertEqual(record["status"], "done")
        self.assertEqual(record["result"]["result"]["answer"], 42)
        status, listed = self._get("/api/tasks?n=5")
        self.assertEqual(status, 200)
        self.assertEqual(listed["tasks"][0]["task_id"], out["task_id"])

    def test_unknown_task_is_404(self) -> None:
        status, out = self._get("/api/task/deadbeef")
        self.assertEqual(status, 404)
        self.assertEqual(out["error"], "not found")

    def test_intent_202_carries_the_decision(self) -> None:
        status, out = self._post("/api/intent", {"request": "tortoise please", "wait": 0.05})
        self.assertEqual(status, 202)
        self.assertEqual(out["decision"]["capability"], "test.slow_read")
        self.assertTrue(self.app.jobs.wait(out["task_id"], 30))

    def test_approve_over_http_is_immediate(self) -> None:
        _, parked = self._post("/api/task", {"capability": "test.slow_privileged", "payload": {}})
        status, out = self._post("/api/approvals/approve", {"id": parked["approval_id"]})
        self.assertEqual(status, 202)
        self.assertEqual(out["status"], "running")
        status, again = self._post("/api/approvals/approve", {"id": parked["approval_id"]})
        self.assertEqual(status, 200)
        self.assertEqual(again["error"]["type"], "AlreadyResolved")
        self.assertTrue(self.app.jobs.wait(out["task_id"], 30))
        self.assertEqual(self.app.approvals.get(parked["approval_id"])["status"], "approved")


if __name__ == "__main__":
    unittest.main()
