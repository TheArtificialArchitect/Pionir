import unittest

from pionir.adapters.autogenesis_status import (
    AutogenesisStatusAdapter,
    AutogenesisStatusSettings,
)
from pionir.contracts import Task
from pionir.errors import AdapterProtocolError


class FakeRunner:
    def __init__(self, output: str) -> None:
        self.output = output

    def run(self, *, timeout_seconds: int) -> str:
        return self.output


class AutogenesisStatusAdapterTests(unittest.TestCase):
    def test_returns_existing_read_only_status(self) -> None:
        adapter = AutogenesisStatusAdapter(
            AutogenesisStatusSettings(command=("python", "-m", "autogenesis")),
            runner=FakeRunner("{'controls': {'paused': False}, 'events': []}"),
        )
        result = adapter.execute(Task("organism.autogenesis_status", {}))
        self.assertIn("controls", result.output["snapshot"])
        self.assertEqual(result.evidence, ("autogenesis:read-only-status",))
        self.assertIsNone(adapter.manifest.capabilities[0].model)

    def test_empty_status_fails_closed(self) -> None:
        adapter = AutogenesisStatusAdapter(
            AutogenesisStatusSettings(command=("python",)),
            runner=FakeRunner(""),
        )
        with self.assertRaises(AdapterProtocolError):
            adapter.execute(Task("organism.autogenesis_status", {}))


if __name__ == "__main__":
    unittest.main()
