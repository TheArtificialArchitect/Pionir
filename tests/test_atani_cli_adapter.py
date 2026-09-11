import json
import unittest

from pionir.adapters.atani_cli import AtaniCliAdapter, AtaniCliSettings, CommandResult
from pionir.contracts import Task
from pionir.errors import AdapterProtocolError, AdapterUnavailable


class FakeRunner:
    def __init__(
        self,
        document=None,
        *,
        stdout: str | None = None,
        returncode: int = 0,
        stderr: str = "",
    ) -> None:
        if stdout is not None:
            self.stdout = stdout
        elif document is not None:
            self.stdout = json.dumps(document)
        else:
            self.stdout = ""
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[tuple[tuple[str, ...], int]] = []
        self.input_text = None

    def run(self, arguments, *, timeout_seconds: int, input_text=None) -> CommandResult:
        self.calls.append((tuple(arguments), timeout_seconds))
        self.input_text = input_text
        return CommandResult(self.returncode, self.stdout, self.stderr)


class AtaniCliAdapterTests(unittest.TestCase):
    def test_default_chat_uses_json_cli_and_preserves_diagnostics(self) -> None:
        runner = FakeRunner(
            {"answer": "A grounded answer", "cycle_id": "cycle-1", "uncertainty": []}
        )
        adapter = AtaniCliAdapter(runner=runner)
        task = Task(
            "reasoning.atani_answer",
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
            adapter.execute(Task("reasoning.atani_answer", {"content": "hello"}))

    def test_manifest_separates_default_and_depth_models(self) -> None:
        adapter = AtaniCliAdapter(AtaniCliSettings(command=("atani",)))
        models = {
            capability.name: capability.model.model_id
            for capability in adapter.manifest.capabilities
            if capability.model is not None
        }
        self.assertEqual(models["reasoning.atani_answer"], "qwen2.5:7b-instruct")
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

    def test_a_failed_outcome_survives_a_nonzero_exit(self) -> None:
        # Verified against the real CLI (2026-09-10): a legitimately failed goal
        # is written to stdout while the process exits 1. Reading the exit code
        # first turned that real outcome - its goal_id, its reason - into a bare
        # "Atani is unavailable". The outcome on stdout is authoritative.
        runner = FakeRunner(
            {
                "status": "failed",
                "goal_id": "goal-9",
                "reason": "bounded executive limit reached",
                "steps": 1,
            },
            returncode=1,
        )
        adapter = AtaniCliAdapter(runner=runner)
        result = adapter.execute(
            Task(
                "executive.atani_run",
                {"protocol": "atani.executive.v1", "goal_id": "goal-9", "steps": []},
                frozenset({"atani.executive"}),
            )
        )
        self.assertEqual(result.output["status"], "failed")
        self.assertEqual(result.output["reason"], "bounded executive limit reached")
        self.assertEqual(result.evidence, ("atani:goal:goal-9",))

    def test_an_executive_error_surfaces_atanis_own_reason(self) -> None:
        # Empty stdout with a non-zero exit is a genuine failure - and Atani's
        # stderr says why. Reporting "run atani doctor" instead would throw the
        # actual reason away (a swallowed diagnosis, HEAD 3.18).
        runner = FakeRunner(
            stdout="",
            returncode=1,
            stderr="Atani error: protocol must be atani.executive.v1",
        )
        adapter = AtaniCliAdapter(runner=runner)
        with self.assertRaises(AdapterUnavailable) as caught:
            adapter.execute(
                Task(
                    "executive.atani_run",
                    {"protocol": "atani.executive.v1", "goal_id": "g", "steps": []},
                    frozenset({"atani.executive"}),
                )
            )
        self.assertIn("protocol must be", str(caught.exception))

    def test_a_chat_failure_surfaces_atanis_own_reason(self) -> None:
        runner = FakeRunner(stdout="", returncode=1, stderr="Atani error: model not pulled")
        adapter = AtaniCliAdapter(runner=runner)
        with self.assertRaises(AdapterUnavailable) as caught:
            adapter.execute(Task("reasoning.atani_answer", {"content": "hi"}))
        self.assertIn("model not pulled", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
