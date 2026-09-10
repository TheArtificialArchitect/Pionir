"""The executive records a lesson when a specialist's circuit opens - the shell
learning from its own repeated failures, recorded once per trip."""

import unittest

from pionir.contracts import AgentManifest, Capability, Task, TaskResult
from pionir.reliability import CircuitBreaker
from pionir.runtime import Executive


class _Boom:
    def __init__(self):
        self._manifest = AgentManifest(
            agent_id="flaky",
            version="1",
            capabilities=(Capability(name="do.thing", description="a flaky thing"),),
        )

    @property
    def manifest(self):
        return self._manifest

    def execute(self, task):
        raise RuntimeError("it broke again")


class ExecutiveLessonTests(unittest.TestCase):
    def _executive(self, lessons):
        ex = Executive(
            circuit_factory=lambda: CircuitBreaker(failure_threshold=3, recovery_seconds=60),
            on_lesson=lessons.append,
        )
        ex.register(_Boom())
        return ex

    def test_a_lesson_is_recorded_once_when_the_circuit_trips(self) -> None:
        lessons: list[str] = []
        ex = self._executive(lessons)
        for _ in range(5):   # threshold 3, then it stays open
            try:
                ex.execute(Task("do.thing", {}))
            except Exception:
                pass
        self.assertEqual(len(lessons), 1, "a tripped circuit should record exactly one lesson")
        self.assertIn("flaky", lessons[0])
        self.assertIn("circuit opened", lessons[0])

    def test_no_lesson_sink_is_fine(self) -> None:
        ex = Executive(
            circuit_factory=lambda: CircuitBreaker(failure_threshold=2, recovery_seconds=60),
        )
        ex.register(_Boom())
        for _ in range(4):
            try:
                ex.execute(Task("do.thing", {}))
            except Exception:
                pass  # no on_lesson: must not raise


if __name__ == "__main__":
    unittest.main()
