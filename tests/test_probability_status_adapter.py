import unittest

from pionir.adapters.probability_status import (
    ProbabilityStatusAdapter,
    ProbabilityStatusSettings,
)
from pionir.contracts import Task
from pionir.errors import AdapterProtocolError


class FakeReader:
    def __init__(self, document):
        self.document = document
        self.paths = []

    def get(self, path):
        self.paths.append(path)
        return self.document


class ProbabilityStatusAdapterTests(unittest.TestCase):
    def test_redacts_private_runtime_collections(self) -> None:
        reader = FakeReader(
            {
                "ok": True,
                "name": "Probability",
                "mode": "awake",
                "age": "1d 2h",
                "tick": 9,
                "busy": False,
                "auto_evolve": True,
                "mentor": {"available": True, "model": "mentor"},
                "discord": {
                    "configured": True,
                    "controls": True,
                    "notifications": True,
                    "channel": 123456,
                },
                "resources": {"pressure": "open"},
                "self_model": {"files": 42},
                "last_evolution": "recent",
                "gene_count": 3,
                "memories": [{"text": "private"}],
                "events": [{"details": "private"}],
                "goals": [{"goal": "private"}],
                "experiments": [{"diff": "private"}],
            }
        )
        adapter = ProbabilityStatusAdapter(
            ProbabilityStatusSettings(), reader=reader
        )

        result = adapter.execute(Task("organism.probability_status", {}))

        self.assertEqual(reader.paths, ["/api/state"])
        self.assertEqual(result.output["mode"], "awake")
        self.assertNotIn("channel", result.output["discord"])
        for private in ("memories", "events", "goals", "experiments"):
            self.assertNotIn(private, result.output)
        self.assertEqual(
            result.evidence,
            (
                "probability:loopback-read-only",
                "probability:private-state-redacted",
            ),
        )

    def test_rejects_malformed_state(self) -> None:
        adapter = ProbabilityStatusAdapter(
            ProbabilityStatusSettings(), reader=FakeReader({"ok": True})
        )
        with self.assertRaises(AdapterProtocolError):
            adapter.status()

    def test_rejects_non_loopback_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback"):
            ProbabilityStatusSettings(base_url="http://example.com:8791")


if __name__ == "__main__":
    unittest.main()
