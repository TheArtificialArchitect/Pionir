"""Tests for the Daedalus (coder) and Melete (tool-executor) adapters.

Both are gated, audited fronts onto Theo's own HTTP services. The point of
routing them through Pionir is the gate and the ledger, so these assert that a
real refused/failed outcome comes back as a result to record - not as a bare
"unavailable" - and that a privileged intent is shaped correctly for each seam.
"""

import unittest

from pionir.adapters._http import HttpStatusError, LoopbackHttpSettings, health_model
from pionir.adapters.daedalus import AdapterTimeout, DaedalusAdapter, DaedalusSettings
from pionir.adapters.melete import MeleteAdapter, MeleteSettings
from pionir.contracts import RiskLevel, Task
from pionir.errors import AdapterProtocolError, AdapterUnavailable


class FakeClient:
    """Records the path and payload it was handed and returns a canned document."""

    def __init__(self, *, health: dict | None = None, response: dict | None = None) -> None:
        self._health = health if health is not None else {"ok": True, "model": "qwen2.5:7b-instruct"}
        self._response = response if response is not None else {"ok": True}
        self.calls: list[tuple[str, object]] = []

    def get(self, path: str, *, timeout_seconds=None):
        self.calls.append((path, None))
        return self._health

    def post(self, path: str, payload, *, timeout_seconds=None):
        self.calls.append((path, dict(payload)))
        if path == "/jobs":
            raise HttpStatusError("not found", status=404)
        return self._response


class AsyncFakeClient:
    def __init__(self, response: dict | None = None, *, running: bool = False) -> None:
        self.response = response or {"ok": True}
        self.running = running
        self.calls: list[tuple[str, object, object]] = []

    def post(self, path: str, payload, *, timeout_seconds=None):
        self.calls.append((path, dict(payload), timeout_seconds))
        if path == "/jobs":
            return {"job": {"id": "job-1"}}
        if path.endswith("/cancel"):
            return {"ok": True}
        raise AssertionError(path)

    def get(self, path: str, *, timeout_seconds=None):
        self.calls.append((path, None, timeout_seconds))
        state = "running" if self.running else "done"
        return {"job": {"id": "job-1", "state": state, "result": self.response}}


def _daedalus_task(content="add a null check", **payload):
    return Task("coding.daedalus_solve", {"content": content, **payload}, frozenset({"daedalus.solve"}))


def _melete_task(content="list the repo root"):
    return Task("tools.melete_invoke", {"content": content}, frozenset({"melete.invoke"}))


class SharedHttpSettingsTests(unittest.TestCase):
    def test_rejects_a_non_loopback_url(self) -> None:
        with self.assertRaises(ValueError):
            LoopbackHttpSettings(base_url="http://10.0.0.5:8771")

    def test_health_model_fails_open(self) -> None:
        class Down:
            def get(self, path):
                raise AdapterUnavailable("down")

        self.assertIsNone(health_model(Down()))

    def test_health_model_reads_the_reported_model(self) -> None:
        self.assertEqual(health_model(FakeClient(health={"ok": True, "model": " gemma "})), "gemma")


class DaedalusTests(unittest.TestCase):
    def test_solve_is_privileged_and_needs_permission(self) -> None:
        cap = DaedalusAdapter(client=FakeClient()).manifest.capabilities[0]
        self.assertEqual(cap.risk, RiskLevel.PRIVILEGED)
        self.assertIn("daedalus.solve", cap.required_permissions)

    def test_routes_content_to_intent_and_defaults_dry_run_false(self) -> None:
        client = FakeClient(response={"ok": True, "commit": "abc123", "result": "done"})
        result = DaedalusAdapter(client=client).execute(_daedalus_task("fix the parser"))
        path, payload = client.calls[-1]
        self.assertEqual(path, "/solve")
        self.assertEqual(payload["intent"], "fix the parser")
        self.assertIs(payload["dry_run"], False)
        self.assertEqual(result.agent_id, "daedalus")
        self.assertIn("daedalus:commit:abc123", result.evidence)

    def test_passes_dry_run_and_repo_through(self) -> None:
        client = FakeClient(response={"ok": True})
        DaedalusAdapter(client=client).execute(
            _daedalus_task("try it", repo="C:/src/thing", dry_run=True)
        )
        _, payload = client.calls[-1]
        self.assertIs(payload["dry_run"], True)
        self.assertEqual(payload["repo"], "C:/src/thing")

    def test_a_refused_solve_is_returned_not_raised(self) -> None:
        # ok=false with a gate reason is a real Daedalus verdict the ledger must
        # record, not an adapter failure.
        client = FakeClient(response={"ok": False, "gate": "tests failed", "error": "2 failing"})
        result = DaedalusAdapter(client=client).execute(_daedalus_task())
        self.assertIs(result.output["ok"], False)
        self.assertEqual(result.output["gate"], "tests failed")

    def test_rejects_empty_and_oversized_intent_before_the_client(self) -> None:
        client = FakeClient()
        adapter = DaedalusAdapter(client=client)
        with self.assertRaises(AdapterProtocolError):
            adapter.execute(_daedalus_task("   "))
        with self.assertRaises(AdapterProtocolError):
            adapter.execute(_daedalus_task("x" * 8_001))
        self.assertEqual(client.calls, [])

    def test_status_requires_ok_health(self) -> None:
        with self.assertRaises(AdapterProtocolError):
            DaedalusAdapter(client=FakeClient(health={"model": "x"})).status()

    def test_uses_async_jobs_when_the_server_supports_them(self) -> None:
        client = AsyncFakeClient({"ok": True, "commit": "abc123"})
        result = DaedalusAdapter(client=client, sleep=lambda _s: None).execute(
            _daedalus_task("fix it")
        )
        self.assertEqual([call[0] for call in client.calls], ["/jobs", "/jobs/job-1"])
        self.assertEqual(result.output["job_id"], "job-1")
        self.assertEqual(result.output["state"], "done")
        self.assertIn("daedalus:job:job-1", result.evidence)
        self.assertIn("daedalus:commit:abc123", result.evidence)

    def test_timeout_requests_cancellation_and_preserves_job_id(self) -> None:
        client = AsyncFakeClient(running=True)
        ticks = iter((0.0, 6.0))
        adapter = DaedalusAdapter(
            DaedalusSettings(
                timeout_seconds=5,
                request_timeout_seconds=5,
                poll_interval_seconds=0.01,
            ),
            client=client,
            sleep=lambda _s: None,
            monotonic=lambda: next(ticks),
        )
        with self.assertRaises(AdapterTimeout) as caught:
            adapter.execute(_daedalus_task())
        self.assertEqual(caught.exception.job_id, "job-1")
        self.assertEqual(client.calls[-1][0], "/jobs/job-1/cancel")


class MeleteTests(unittest.TestCase):
    def test_invoke_is_privileged_and_needs_permission(self) -> None:
        cap = MeleteAdapter(client=FakeClient()).manifest.capabilities[0]
        self.assertEqual(cap.risk, RiskLevel.PRIVILEGED)
        self.assertIn("melete.invoke", cap.required_permissions)

    def test_invoke_sends_the_intent_and_returns_the_outcome(self) -> None:
        client = FakeClient(response={"ok": True, "result": "listed 3 files", "steps": []})
        result = MeleteAdapter(client=client).execute(_melete_task("list the repo"))
        path, payload = client.calls[-1]
        self.assertEqual(path, "/invoke")
        self.assertEqual(payload["intent"], "list the repo")
        self.assertEqual(result.output["result"], "listed 3 files")
        self.assertEqual(result.evidence, ("melete:invoke",))

    def test_a_failed_invoke_is_returned_not_raised(self) -> None:
        client = FakeClient(response={"ok": False, "error": "tool blew up", "steps": []})
        result = MeleteAdapter(client=client).execute(_melete_task())
        self.assertIs(result.output["ok"], False)
        self.assertEqual(result.output["error"], "tool blew up")

    def test_rejects_empty_intent(self) -> None:
        with self.assertRaises(AdapterProtocolError):
            MeleteAdapter(client=FakeClient()).execute(_melete_task("   "))


class DefaultSettingsTests(unittest.TestCase):
    def test_known_loopback_ports(self) -> None:
        self.assertEqual(DaedalusSettings().base_url, "http://127.0.0.1:8771")
        self.assertEqual(MeleteSettings().base_url, "http://127.0.0.1:8770")


if __name__ == "__main__":
    unittest.main()
