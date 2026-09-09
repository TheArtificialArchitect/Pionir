import unittest
from types import SimpleNamespace

from pionir.contracts import TaskResult
from pionir.desktop import DesktopController


class FakeExecutive:
    def __init__(self) -> None:
        self.tasks = []

    def execute(self, task):
        self.tasks.append(task)
        return TaskResult(task.task_id, "fake", {"answer": "ready"})


class DesktopControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.executive = FakeExecutive()
        self.runtime = SimpleNamespace(
            executive=self.executive,
            adapters={
                "atani": object(),
                "theo": object(),
                "bryo": object(),
            },
        )
        self.controller = DesktopController(self.runtime)

    def test_default_route_calls_atani_with_permission(self) -> None:
        result = self.controller.ask("Atani", "Hello")
        task = self.executive.tasks[0]
        self.assertEqual(result["answer"], "ready")
        self.assertEqual(task.capability, "reasoning.atani_answer")
        self.assertEqual(task.granted_permissions, frozenset({"atani.chat"}))

    def test_depth_and_theo_are_explicit_routes(self) -> None:
        self.controller.ask("Atani · depth", "Think")
        self.controller.ask("Theo", "Reflect")
        self.assertEqual(
            [task.capability for task in self.executive.tasks],
            ["reasoning.atani_depth", "conversation.theo_reply"],
        )

    def test_missing_theo_fails_before_dispatch(self) -> None:
        self.runtime.adapters.pop("theo")
        with self.assertRaisesRegex(ValueError, "not configured"):
            self.controller.ask("Theo", "Hello")
        self.assertEqual(self.executive.tasks, [])


if __name__ == "__main__":
    unittest.main()
