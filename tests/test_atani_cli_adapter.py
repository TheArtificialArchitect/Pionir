import json
import unittest

from pionir.adapters.atani_cli import AtaniCliAdapter, AtaniCliSettings
from pionir.contracts import Task
from pionir.errors import AdapterProtocolError


class FakeRunner:
    def __init__(self, document) -> None:
        self.document = document
        self.calls: list[tuple[tuple[str, ...], int]] = []

    def run(self, arguments, *, timeout_seconds: int, input_text=None) -> str:
        self.calls.append((tuple(arguments), timeout_seconds))
        self.input_text = input_text
        return json.dumps(self.document)


class AtaniCliAdapterTests(unittest.TestCase):
    def test_default_chat_uses_json_cli_and_preserves_diagnostics(self) -> None:
        runner = FakeRunner(
            {"answer": "A grounded answer", "cycle_id": "cycle-1", "uncertainty": []}
        )
        adapter = AtaniCliAdapter(runner=runner)
        task = Task(
            "reasoning.atani_chat",
            {"content": "Think about this"},
            frozenset({"atani.chat"}),
        )
        result = adapter.execute(task)
        self.assertEqual(result.output["answer"], "A grounded answer")
        self.assertEqual(result.evidence, ("atani:reasoning-cycle:cycle-1",))
        self.assertEqual(runner.calls[0][0], ("chat", "--json", "Think about this"))

    def test_depth_uses_explicit_depth_flag(self) -> None:
        runner = FakeRunner({"answer": "Deep answer", "cycle_id": "cycle-2"})
        adapter = AtaniCliAdapter(runner=runner)
        adapter.execute(
            Task(
                "reasoning.atani_depth",
                {"content": "Go deep"},
                frozenset({"atani.chat"}),
            )
        )
        self.assertEqual(
            runner.calls[0][0],
            ("chat", "--json", "--depth", "Go deep"),
        )

    def test_requires_answer_in_response(self) -> None:
        adapter = AtaniCliAdapter(runner=FakeRunner({"cycle_id": "cycle-3"}))
        with self.assertRaises(AdapterProtocolError):
            adapter.execute(Task("reasoning.atani_chat", {"content": "hello"}))

    def test_manifest_separates_default_and_depth_models(self) -> None:
        adapter = AtaniCliAdapter(AtaniCliSettings(command=("atani",)))
        models = {
            capability.name: capability.model.model_id
            for capability in adapter.manifest.capabilities
            if capability.model is not None
        }
        self.assertEqual(models["reasoning.atani_chat"], "qwen2.5:7b-instruct")
        self.assertIn("nemotron", models["reasoning.atani_depth"])

    def test_executive_plan_uses_versioned_stdin_contract(self) -> None:
        runner = FakeRunner(
            {"status": "completed", "goal_id": "goal-1", "steps": 1}
        )
        adapter = AtaniCliAdapter(runner=runner)
        request = {
            "protocol": "atani.executive.v1",
            "goal_id": "goal-1",
            "steps": [],
        }
        result = adapter.execute(
            Task(
                "executive.atani_run",
                request,
                frozenset({"atani.executive"}),
            )
        )
        self.assertEqual(runner.calls[0][0], ("executive",))
        self.assertEqual(json.loads(runner.input_text), request)
        self.assertEqual(result.evidence, ("atani:goal:goal-1",))

    def test_executive_plan_rejects_wrong_protocol(self) -> None:
        adapter = AtaniCliAdapter(runner=FakeRunner({}))
        with self.assertRaises(AdapterProtocolError):
            adapter.execute(
                Task("executive.atani_run", {"protocol": "unknown"})
            )


if __name__ == "__main__":
    unittest.main()
