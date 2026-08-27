import tempfile
import unittest
from pathlib import Path

from pionir.adapters.stdio_json import (
    HEALTH_PROTOCOL,
    RESULT_PROTOCOL,
    TASK_PROTOCOL,
    StdioJsonAdapter,
    StdioJsonSettings,
    load_stdio_adapters,
)
from pionir.contracts import AgentManifest, Capability, Task
from pionir.errors import AdapterProtocolError


class FakeTransport:
    def __init__(self) -> None:
        self.documents = []

    def exchange(self, document, *, timeout_seconds: int):
        self.documents.append(document)
        if document["protocol"] == HEALTH_PROTOCOL:
            return {
                "protocol": HEALTH_PROTOCOL,
                "agent_id": "probability",
                "ok": True,
            }
        return {
            "protocol": RESULT_PROTOCOL,
            "task_id": document["task_id"],
            "agent_id": "probability",
            "output": {"p": 0.7},
            "evidence": ["simulation:1"],
        }


class StdioJsonAdapterTests(unittest.TestCase):
    def adapter(self, transport: FakeTransport) -> StdioJsonAdapter:
        manifest = AgentManifest(
            "probability",
            "test",
            (Capability("forecast.probability", "Forecast"),),
        )
        return StdioJsonAdapter(
            StdioJsonSettings(("probability-adapter",), manifest),
            transport=transport,
        )

    def test_versioned_task_round_trip(self) -> None:
        transport = FakeTransport()
        adapter = self.adapter(transport)
        task = Task("forecast.probability", {"event": "rain"})
        result = adapter.execute(task)
        self.assertEqual(result.output["p"], 0.7)
        self.assertEqual(result.evidence, ("simulation:1",))
        self.assertEqual(transport.documents[0]["protocol"], TASK_PROTOCOL)
        self.assertEqual(transport.documents[0]["task_id"], str(task.task_id))

    def test_health_round_trip(self) -> None:
        adapter = self.adapter(FakeTransport())
        self.assertTrue(adapter.status()["ok"])

    def test_rejects_wrong_agent_result(self) -> None:
        class WrongAgent(FakeTransport):
            def exchange(self, document, *, timeout_seconds: int):
                result = super().exchange(document, timeout_seconds=timeout_seconds)
                result["agent_id"] = "other"
                return result

        with self.assertRaises(AdapterProtocolError):
            self.adapter(WrongAgent()).execute(
                Task("forecast.probability", {"event": "rain"})
            )

    def test_loads_explicit_toml_manifest(self) -> None:
        source = """
[[specialists]]
agent_id = "probability"
version = "1"
command = ["python", "-m", "probability.adapter"]
memory_access = ["skills/probability"]

[[specialists.capabilities]]
name = "forecast.probability"
description = "Forecast"
risk = "read_only"
required_permissions = []

[specialists.capabilities.model]
id = "cpu"
estimated_vram_mb = 0
requires_gpu = false
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "specialists.toml"
            path.write_text(source, encoding="utf-8")
            adapters = load_stdio_adapters(path)
        self.assertEqual(len(adapters), 1)
        self.assertEqual(adapters[0].manifest.agent_id, "probability")
        self.assertEqual(
            next(iter(adapters[0].manifest.memory_access)).value,
            "skills/probability",
        )


if __name__ == "__main__":
    unittest.main()
