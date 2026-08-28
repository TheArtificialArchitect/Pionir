import unittest

from pionir.contracts import AgentManifest, Capability, ModelRequirement, Task, TaskResult
from pionir.errors import CircuitOpen, ResourceUnavailable
from pionir.reliability import CircuitBreaker
from pionir.runtime import Executive, InMemoryAuditSink
from pionir.scheduler import ModelLeaseScheduler


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class RefusingScheduler(ModelLeaseScheduler):
    """Stands in for a GPU already leased by Bryo or another Pionir process."""

    def acquire(self, requirement):  # type: ignore[override]
        raise ResourceUnavailable("GPU is leased by another Pionir-compatible process")


class FakeAdapter:
    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.calls = 0
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
        self.calls += 1
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

    def test_repeated_adapter_failure_opens_circuit_without_another_call(self) -> None:
        adapter = FakeAdapter(fail=True)
        executive = Executive(
            circuit_factory=lambda: CircuitBreaker(
                failure_threshold=2,
                recovery_seconds=60,
            )
        )
        executive.register(adapter)
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                executive.execute(Task("conversation.reply", {}))
        with self.assertRaises(CircuitOpen):
            executive.execute(Task("conversation.reply", {}))
        self.assertEqual(adapter.calls, 2)

    def test_a_refused_lease_does_not_wedge_the_recovery_probe(self) -> None:
        """A busy GPU must not permanently open a healthy specialist's circuit."""

        clock = FakeClock()
        adapter = FakeAdapter()
        circuit = CircuitBreaker(
            failure_threshold=1,
            recovery_seconds=10,
            clock=clock,
        )
        executive = Executive(
            scheduler=RefusingScheduler(),
            circuit_factory=lambda: circuit,
        )
        executive.register(adapter)

        # One genuine adapter failure opens the circuit.
        circuit.record_failure()
        clock.now = 10

        # The recovery probe is admitted, then dies on the shared GPU lease.
        with self.assertRaises(ResourceUnavailable):
            executive.execute(Task("conversation.reply", {}))
        self.assertEqual(adapter.calls, 0)

        # Once the GPU frees up the specialist must be reachable again.
        executive.scheduler = ModelLeaseScheduler()
        clock.now = 20
        result = executive.execute(Task("conversation.reply", {}))
        self.assertEqual(result.output["reply"], "hello")


if __name__ == "__main__":
    unittest.main()
