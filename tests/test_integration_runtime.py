import json
import tempfile
import unittest
from pathlib import Path

from pionir.adapters.atani_cli import AtaniCliAdapter
from pionir.adapters.theo_peer import TheoPeerAdapter, TheoPeerSettings
from pionir.audit import JsonlAuditSink
from pionir.contracts import Task
from pionir.runtime import Executive


class AtaniRunner:
    def run(self, arguments, *, timeout_seconds: int, input_text=None) -> str:
        del input_text
        return json.dumps({"answer": "Atani answer", "cycle_id": "sim-1"})


class TheoTransport:
    def request(self, path, *, payload=None):
        if path == "/health":
            return {"ok": True, "capabilities": {"peer": True}}
        return {"ok": True, "reply": "Theo answer"}


class IntegrationRuntimeTests(unittest.TestCase):
    def test_two_specialists_share_routing_audit_and_gpu_admission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            audit = JsonlAuditSink(Path(directory) / "events.jsonl", fsync=False)
            executive = Executive(audit_sink=audit)
            executive.register(AtaniCliAdapter(runner=AtaniRunner()))
            executive.register(
                TheoPeerAdapter(
                    TheoPeerSettings(token="test-token"),
                    transport=TheoTransport(),
                )
            )
            atani = executive.execute(
                Task(
                    "reasoning.atani_chat",
                    {"content": "question"},
                    frozenset({"atani.chat"}),
                )
            )
            theo = executive.execute(
                Task("conversation.theo_peer_reply", {"content": "answer this"})
            )
            self.assertEqual(atani.agent_id, "atani")
            self.assertEqual(theo.agent_id, "theo-peer")
            sequence, _ = audit.verify()
            self.assertEqual(sequence, 4)
            self.assertEqual(executive.scheduler.active_requirements, ())


if __name__ == "__main__":
    unittest.main()
