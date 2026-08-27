import unittest

from pionir.adapters.bryo_status import BryoStatusAdapter, BryoStatusSettings
from pionir.contracts import Task
from pionir.errors import AdapterProtocolError


class FakeRunner:
    def __init__(self, output: str) -> None:
        self.output = output
        self.calls = 0

    def run(self, *, timeout_seconds: int) -> str:
        self.calls += 1
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
        self.assertEqual(result.evidence, ("bryo:read-only-status",))
        self.assertIsNone(adapter.manifest.capabilities[0].model)

    def test_empty_snapshot_fails_closed(self) -> None:
        adapter = BryoStatusAdapter(
            BryoStatusSettings(command=("python",)),
            runner=FakeRunner(""),
        )
        with self.assertRaises(AdapterProtocolError):
            adapter.execute(Task("organism.bryo_status", {}))


if __name__ == "__main__":
    unittest.main()
