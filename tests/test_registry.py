import unittest

from pionir.contracts import AgentManifest, Capability, Task
from pionir.errors import CapabilityNotFound, PermissionDenied
from pionir.registry import CapabilityRegistry


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = CapabilityRegistry()

    def test_routes_by_priority_then_agent_id(self) -> None:
        self.registry.register(
            AgentManifest("theo", "1", (Capability("conversation.reply", "Reply", priority=10),))
        )
        self.registry.register(
            AgentManifest("atani", "1", (Capability("conversation.reply", "Reply", priority=5),))
        )
        self.assertEqual(self.registry.resolve(Task("conversation.reply", {})).agent_id, "theo")

    def test_requires_declared_permission(self) -> None:
        self.registry.register(
            AgentManifest(
                "support",
                "1",
                (
                    Capability(
                        "device.change",
                        "Change a device",
                        required_permissions=frozenset({"device.write"}),
                    ),
                ),
            )
        )
        with self.assertRaises(PermissionDenied):
            self.registry.resolve(Task("device.change", {}))
        route = self.registry.resolve(
            Task("device.change", {}, frozenset({"device.write"}))
        )
        self.assertEqual(route.agent_id, "support")

    def test_unknown_capability_fails_closed(self) -> None:
        with self.assertRaises(CapabilityNotFound):
            self.registry.resolve(Task("unknown", {}))


if __name__ == "__main__":
    unittest.main()
