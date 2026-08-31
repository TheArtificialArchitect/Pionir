import json
import tempfile
import unittest
from pathlib import Path

from pionir.adapters.atani_cli import AtaniCliAdapter
from pionir.adapters.theo import TheoAdapter, TheoSettings
from pionir.audit import JsonlAuditSink
from pionir.contracts import Task
from pionir.runtime import Executive
from pionir.scheduler import ModelLeaseScheduler


class AtaniRunner:
    def run(self, arguments, *, timeout_seconds: int, input_text=None) -> str:
        del input_text
        return json.dumps({"answer": "Atani answer", "cycle_id": "sim-1"})


class TheoTransport:
    def request(self, path, *, payload=None):
        if path == "/health":
            return {"ok": True, "capabilities": {"voice_chat": True}}
        return {
            "ok": True,
            "conv": "conv-1",
            "message": {"id": "m1", "role": "assistant", "content": "Theo answer"},
        }


class IntegrationRuntimeTests(unittest.TestCase):
    def test_two_specialists_share_routing_audit_and_gpu_admission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            audit = JsonlAuditSink(Path(directory) / "events.jsonl", fsync=False)
            # Probes off: this test is about the audit chain and the routing
            # path, not about the machine it happens to run on.
            executive = Executive(
                scheduler=ModelLeaseScheduler(vram_probe=None, residency_probe=None),
                audit_sink=audit,
            )
            executive.register(AtaniCliAdapter(runner=AtaniRunner()))
            executive.register(
                TheoAdapter(
                    TheoSettings(token="test-token"),
                    transport=TheoTransport(),
                )
            )
            atani = executive.execute(
                Task(
                    "reasoning.atani_answer",
                    {"content": "question"},
                    frozenset({"atani.chat"}),
                )
            )
            theo = executive.execute(
                Task("conversation.theo_reply", {"content": "answer this"})
            )
            self.assertEqual(atani.agent_id, "atani")
            self.assertEqual(theo.agent_id, "theo")
            sequence, _ = audit.verify()
            self.assertEqual(sequence, 4)
            self.assertEqual(executive.scheduler.active_requirements, ())


if __name__ == "__main__":
    unittest.main()
