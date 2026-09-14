"""The ledger records a specialist-reported failure as task.failed, by the same
rule the server uses for its outer ok - one function, so they cannot disagree."""

import tempfile
import unittest
from pathlib import Path

from support import offline_scheduler

from pionir import contracts, server
from pionir.audit import JsonlAuditSink
from pionir.contracts import AgentManifest, Capability, Task, TaskResult, outcome_failure_reason
from pionir.errors import CircuitOpen
from pionir.reliability import CircuitBreaker, CircuitState
from pionir.runtime import Executive, InMemoryAuditSink


class OutputAdapter:
    """Returns normally with whatever output it is given."""

    def __init__(self, output: dict) -> None:
        self.output = output
        self.calls = 0
        self._manifest = AgentManifest(
            "daedalus", "test", (Capability("code.solve", "Solve a task"),)
        )

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def execute(self, task: Task) -> TaskResult:
        self.calls += 1
        return TaskResult(task.task_id, "daedalus", self.output)


def run(output: dict, **kwargs):
    audit = kwargs.pop("audit", None) or InMemoryAuditSink()
    executive = Executive(scheduler=offline_scheduler(), audit_sink=audit, **kwargs)
    adapter = OutputAdapter(output)
    executive.register(adapter)
    result = executive.execute(Task("code.solve", {"secret": "not logged"}))
    return executive, adapter, audit, result


# Outputs spanning the rule: success, ok:false, each rc key, bools-as-rc, both.
SAMPLES = [
    {"ok": True},
    {"reply": "hi"},
    {"ok": False},
    {"ok": False, "error": "job refused"},
    {"ok": False, "error": {"type": "X", "message": "boom"}},
    {"returncode": 0},
    {"returncode": 2},
    {"rc": 1},
    {"exit_code": -1},
    {"ok": True, "returncode": 3},
    {"ok": True, "flag": True, "returncode": False},
    {"ok": True, "rc": True},  # a bool is not a return code
    {"ok": False, "returncode": 2, "error": "denied"},
]


class OutcomeLedgerTests(unittest.TestCase):
    def test_ok_false_is_task_failed_with_the_error_text(self) -> None:
        _, _, audit, result = run({"ok": False, "error": "job refused:\n  tests red"})
        last = audit.events[-1]
        self.assertEqual(last.event_type, "task.failed")
        self.assertEqual(last.detail, "ok=false: job refused: tests red")
        self.assertFalse(result.output["ok"])  # the result is still returned
        self.assertNotIn("task.completed", [e.event_type for e in audit.events])
        self.assertNotIn("secret", repr(audit.events))

    def test_nonzero_returncode_is_task_failed_with_the_code(self) -> None:
        _, _, audit, _ = run({"returncode": 2, "stdout": "payload-ish"})
        self.assertEqual(audit.events[-1].event_type, "task.failed")
        self.assertEqual(audit.events[-1].detail, "returncode=2")
        self.assertNotIn("payload-ish", repr(audit.events))

    def test_ok_true_is_task_completed(self) -> None:
        executive, _, audit, _ = run({"ok": True, "returncode": 0})
        self.assertEqual(
            [e.event_type for e in audit.events], ["task.routed", "task.completed"]
        )
        self.assertIsNone(audit.events[-1].detail)
        self.assertEqual(executive.circuit("daedalus").snapshot().consecutive_failures, 0)

    def test_structured_error_uses_its_message(self) -> None:
        reason = outcome_failure_reason({"ok": False, "error": {"type": "X", "message": "boom"}})
        self.assertEqual(reason, "ok=false: boom")
        self.assertIsNone(outcome_failure_reason({"ok": True, "rc": True}))

    def test_reason_text_is_bounded(self) -> None:
        reason = outcome_failure_reason({"ok": False, "error": "x" * 5000})
        self.assertLessEqual(len(reason), len("ok=false: ") + 160)

    def test_server_and_ledger_share_one_rule(self) -> None:
        self.assertIs(server._outcome_ok, contracts.outcome_ok)
        for output in SAMPLES:
            with self.subTest(output=output):
                _, _, audit, _ = run(output)
                completed = audit.events[-1].event_type == "task.completed"
                self.assertEqual(server._outcome_ok(output), completed)
                self.assertEqual(
                    audit.events[-1].event_type,
                    "task.completed" if completed else "task.failed",
                )

    def test_server_response_and_ledger_agree_end_to_end(self) -> None:
        for output in SAMPLES:
            with self.subTest(output=output):
                executive, _, audit, _ = run({"ok": True})
                executive._adapters["daedalus"].output = output

                class Runtime:
                    cortex = None

                app = server.PionirApp.__new__(server.PionirApp)
                app.runtime = Runtime()
                app.runtime.executive = executive
                app._learn_from_failure = lambda *a: None
                response = app._execute_task("code.solve", {}, [])
                self.assertEqual(
                    response["ok"], audit.events[-1].event_type == "task.completed"
                )

    def test_specialist_failures_trip_the_circuit_and_record_a_lesson(self) -> None:
        lessons: list[str] = []
        audit = InMemoryAuditSink()
        executive = Executive(
            scheduler=offline_scheduler(),
            audit_sink=audit,
            circuit_factory=lambda: CircuitBreaker(failure_threshold=3, recovery_seconds=60),
            on_lesson=lessons.append,
        )
        adapter = OutputAdapter({"returncode": 2})
        executive.register(adapter)
        for count in range(1, 4):
            executive.execute(Task("code.solve", {}))
            self.assertEqual(
                executive.circuit("daedalus").snapshot().consecutive_failures, count
            )
        self.assertIs(executive.circuit("daedalus").snapshot().state, CircuitState.OPEN)
        self.assertEqual(len(lessons), 1)
        self.assertIn("specialist reported returncode=2", lessons[0])
        with self.assertRaises(CircuitOpen):
            executive.execute(Task("code.solve", {}))
        self.assertEqual(adapter.calls, 3)

    def test_a_success_resets_the_count(self) -> None:
        executive, adapter, _, _ = run({"ok": False})
        adapter.output = {"ok": True}
        executive.execute(Task("code.solve", {}))
        self.assertEqual(executive.circuit("daedalus").snapshot().consecutive_failures, 0)

    def test_audit_chain_verifies_with_failed_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            sink = JsonlAuditSink(path, fsync=False)
            executive, adapter, _, _ = run({"ok": True}, audit=sink)
            for output in ({"ok": False, "error": "no"}, {"returncode": 2}, {"ok": True}):
                adapter.output = output
                executive.execute(Task("code.solve", {}))
            sequence, _ = sink.verify()
            self.assertEqual(sequence, 8)
            text = path.read_text(encoding="utf-8")
            self.assertEqual(text.count('"task.failed"'), 2)
            self.assertEqual(text.count('"task.completed"'), 2)


if __name__ == "__main__":
    unittest.main()
