import unittest

from pionir.adapters.bryo_status import BryoStatusAdapter, BryoStatusSettings
from pionir.contracts import Task
from pionir.errors import AdapterProtocolError


class FakeRunner:
    def __init__(self, output: str, *, json_output: str | None = None) -> None:
        self.output = output
        self.json_output = json_output
        self.calls: list[tuple[str, ...]] = []

    def run(self, *, timeout_seconds: int, arguments=()) -> str:
        self.calls.append(tuple(arguments))
        if arguments and self.json_output is not None:
            return self.json_output
        return self.output


class BryoStatusAdapterTests(unittest.TestCase):
    def test_returns_read_only_snapshot(self) -> None:
        runner = FakeRunner("BRYO — tick 42")
        adapter = BryoStatusAdapter(
            BryoStatusSettings(command=("python", "-m", "bryo.status")),
            runner=runner,
        )
        result = adapter.execute(Task("organism.bryo_status", {}))
        self.assertEqual(result.output["snapshot"], "BRYO — tick 42")
        self.assertEqual(result.output["format"], "text")
        self.assertEqual(runner.calls, [("--json",), ()])
        self.assertEqual(result.evidence, ("bryo:read-only-status",))
        self.assertIsNone(adapter.manifest.capabilities[0].model)

    def test_prefers_machine_readable_vitals(self) -> None:
        runner = FakeRunner(
            "fallback",
            json_output='{"schema":"bryo.vitals/1","alive":true,"pressure":0.25}',
        )
        result = BryoStatusAdapter(
            BryoStatusSettings(command=("python",)), runner=runner
        ).execute(Task("organism.bryo_status", {}))
        self.assertEqual(result.output["format"], "json")
        self.assertEqual(result.output["vitals"]["pressure"], 0.25)
        self.assertEqual(runner.calls, [("--json",)])

    def test_empty_snapshot_fails_closed(self) -> None:
        adapter = BryoStatusAdapter(
            BryoStatusSettings(command=("python",)),
            runner=FakeRunner(""),
        )
        with self.assertRaises(AdapterProtocolError):
            adapter.execute(Task("organism.bryo_status", {}))


if __name__ == "__main__":
    unittest.main()
