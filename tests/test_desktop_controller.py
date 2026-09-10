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

    def test_depth_is_an_explicit_route(self) -> None:
        self.controller.ask("Atani · depth", "Think")
        self.assertEqual(
            [task.capability for task in self.executive.tasks],
            ["reasoning.atani_depth"],
        )



if __name__ == "__main__":
    unittest.main()
