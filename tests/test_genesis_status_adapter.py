import unittest

from pionir.adapters.genesis_status import GenesisStatusAdapter, GenesisStatusSettings
from pionir.contracts import Task
from pionir.errors import AdapterProtocolError


class FakeReader:
    def __init__(self, documents):
        self.documents = documents
        self.paths = []

    def get(self, path):
        self.paths.append(path)
        return self.documents[path]


class GenesisStatusAdapterTests(unittest.TestCase):
    def test_redacts_vault_thought_and_action_result(self) -> None:
        reader = FakeReader(
            {
                "/api/health": {
                    "booted": True,
                    "boot_error": None,
                    "model": "qwen2.5-coder:7b",
                    "status": "Active",
                    "subsystems": {
                        "llm": {
                            "name": "Local Model",
                            "ok": True,
                            "detail": "private detail",
                        },
                        "vault": {
                            "name": "Obsidian Vault",
                            "ok": True,
                            "detail": r"C:\Users\Ian\genesis-agent\vault",
                        },
                    },
                },
                "/api/state": {
                    "emotions": {"trust": 0.7},
                    "last_monologue": "private thought",
                    "last_action": "none",
                    "last_action_result": "private result",
                    "tick_count": 12,
                    "cognitive_errors": 1,
                    "running": True,
                },
            }
        )
        adapter = GenesisStatusAdapter(GenesisStatusSettings(), reader=reader)

        result = adapter.execute(Task("organism.genesis_status", {}))

        self.assertEqual(reader.paths, ["/api/health", "/api/state"])
        self.assertTrue(result.output["ok"])
        self.assertEqual(result.output["life_loop"]["tick_count"], 12)
        self.assertNotIn("detail", result.output["subsystems"]["vault"])
        self.assertNotIn("last_monologue", result.output)
        self.assertNotIn("last_action_result", result.output["life_loop"])

    def test_rejects_malformed_state(self) -> None:
        adapter = GenesisStatusAdapter(
            GenesisStatusSettings(),
            reader=FakeReader(
                {
                    "/api/health": {"status": "Active"},
                    "/api/state": {"tick_count": "twelve"},
                }
            ),
        )
        with self.assertRaises(AdapterProtocolError):
            adapter.status()

    def test_rejects_non_loopback_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback"):
            GenesisStatusSettings(base_url="https://genesis.example.com")


if __name__ == "__main__":
    unittest.main()
