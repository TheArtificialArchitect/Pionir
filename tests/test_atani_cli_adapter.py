import json
import unittest

from pionir.adapters.atani_cli import AtaniCliAdapter, AtaniCliSettings
from pionir.contracts import Task
from pionir.errors import AdapterProtocolError


class FakeRunner:
    def __init__(self, document) -> None:
        self.document = document
        self.calls: list[tuple[tuple[str, ...], int]] = []

    def run(self, arguments, *, timeout_seconds: int) -> str:
        self.calls.append((tuple(arguments), timeout_seconds))
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


if __name__ == "__main__":
    unittest.main()
