"""Tests for the Daedalus (coder) and Melete (tool-executor) adapters.

Both are gated, audited fronts onto Theo's own HTTP services. The point of
routing them through Pionir is the gate and the ledger, so these assert that a
real refused/failed outcome comes back as a result to record - not as a bare
"unavailable" - and that a privileged intent is shaped correctly for each seam.
"""

import unittest

from pionir.adapters._http import LoopbackHttpSettings, health_model
from pionir.adapters.daedalus import DaedalusAdapter, DaedalusSettings
from pionir.adapters.melete import MeleteAdapter, MeleteSettings
from pionir.contracts import RiskLevel, Task
from pionir.errors import AdapterProtocolError, AdapterUnavailable


class FakeClient:
    """Records the path and payload it was handed and returns a canned document."""

    def __init__(self, *, health: dict | None = None, response: dict | None = None) -> None:
        self._health = health if health is not None else {"ok": True, "model": "qwen2.5:7b-instruct"}
        self._response = response if response is not None else {"ok": True}
        self.calls: list[tuple[str, object]] = []

    def get(self, path: str):
        self.calls.append((path, None))
        return self._health

    def post(self, path: str, payload):
        self.calls.append((path, dict(payload)))
        return self._response


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
