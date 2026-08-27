import unittest

from pionir.contracts import AgentManifest, Capability, ModelRequirement, Task, TaskResult
from pionir.runtime import Executive, InMemoryAuditSink


class FakeAdapter:
    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self._manifest = AgentManifest(
            "theo",
            "test",
            (
                Capability(
                    "conversation.reply",
                    "Reply to a user",
                    model=ModelRequirement("theo-test", 4_700),
                ),
            ),
        )

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def execute(self, task: Task) -> TaskResult:
        if self._fail:
            raise RuntimeError("adapter offline")
        return TaskResult(task.task_id, "theo", {"reply": "hello"}, ("fake:test",))


class RuntimeTests(unittest.TestCase):
    def test_routes_executes_and_audits_without_payload(self) -> None:
        audit = InMemoryAuditSink()
        executive = Executive(audit_sink=audit)
        executive.register(FakeAdapter())
        result = executive.execute(Task("conversation.reply", {"secret": "not logged"}))
        self.assertEqual(result.output["reply"], "hello")
        self.assertEqual([event.event_type for event in audit.events], [
            "task.routed",
            "task.completed",
        ])
        self.assertNotIn("secret", repr(audit.events))
        self.assertEqual(executive.scheduler.active_requirements, ())

    def test_adapter_failure_releases_lease_and_is_audited(self) -> None:
        audit = InMemoryAuditSink()
        executive = Executive(audit_sink=audit)
        executive.register(FakeAdapter(fail=True))
        with self.assertRaises(RuntimeError):
            executive.execute(Task("conversation.reply", {}))
        self.assertEqual(executive.scheduler.active_requirements, ())
        self.assertEqual(audit.events[-1].event_type, "task.failed")
        self.assertEqual(audit.events[-1].detail, "RuntimeError")


if __name__ == "__main__":
    unittest.main()
