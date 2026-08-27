import unittest

from pionir.contracts import AgentManifest, Capability, MemoryNamespace, ModelRequirement, Task


class ContractTests(unittest.TestCase):
    def test_namespace_requires_hierarchy(self) -> None:
        with self.assertRaises(ValueError):
            MemoryNamespace("theo")
        self.assertEqual(MemoryNamespace("identity/theo").value, "identity/theo")

    def test_namespace_containment_stops_prefix_confusion(self) -> None:
        root = MemoryNamespace("identity/theo")
        self.assertTrue(root.contains(MemoryNamespace("identity/theo/lessons")))
        self.assertFalse(root.contains(MemoryNamespace("identity/theodore")))

    def test_task_payload_is_immutable_snapshot(self) -> None:
        payload = {"question": "hello"}
        task = Task("conversation.reply", payload)
        payload["question"] = "changed"
        self.assertEqual(task.payload["question"], "hello")
        with self.assertRaises(TypeError):
            task.payload["question"] = "nope"  # type: ignore[index]

    def test_manifest_rejects_duplicate_capability(self) -> None:
        capability = Capability("conversation.reply", "Reply to the user")
        with self.assertRaises(ValueError):
            AgentManifest("theo", "1", (capability, capability))

    def test_model_requirement_includes_context(self) -> None:
        model = ModelRequirement("qwen", 4_700, 1_500)
        self.assertEqual(model.total_vram_mb, 6_200)


if __name__ == "__main__":
    unittest.main()
