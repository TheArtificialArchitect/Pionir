import json
import tempfile
import unittest
from pathlib import Path

from pionir.adapters.atani_cli import AtaniCliAdapter, CommandResult
from pionir.adapters.bryo_status import BryoStatusAdapter, BryoStatusSettings
from pionir.audit import JsonlAuditSink
from pionir.contracts import Task
from pionir.runtime import Executive
from pionir.scheduler import ModelLeaseScheduler


class AtaniRunner:
    def run(self, arguments, *, timeout_seconds: int, input_text=None) -> CommandResult:
        del input_text
        return CommandResult(
            0, json.dumps({"answer": "Atani answer", "cycle_id": "sim-1"}), ""
        )


class BryoRunner:
    def run(self, *, timeout_seconds: int, arguments=()) -> str:
        return json.dumps({"stage": "grown", "concepts": 12})


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
                BryoStatusAdapter(
                    BryoStatusSettings(command=("python", "-m", "bryo.status")),
                    runner=BryoRunner(),
                )
            )
            atani = executive.execute(
                Task(
                    "reasoning.atani_answer",
                    {"content": "question"},
                    frozenset({"atani.chat"}),
                )
            )
            bryo = executive.execute(Task("organism.bryo_status", {}))
            self.assertEqual(atani.agent_id, "atani")
            self.assertEqual(bryo.agent_id, "bryo")
            sequence, _ = audit.verify()
            self.assertEqual(sequence, 4)
            self.assertEqual(executive.scheduler.active_requirements, ())


if __name__ == "__main__":
    unittest.main()
