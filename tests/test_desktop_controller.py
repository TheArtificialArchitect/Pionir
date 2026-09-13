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

    def test_the_retired_depth_route_is_rejected(self) -> None:
        # The "Atani · depth" route went with the depth tier (2026-09-13).
        with self.assertRaises(ValueError):
            self.controller.ask("Atani · depth", "Think")



if __name__ == "__main__":
    unittest.main()
