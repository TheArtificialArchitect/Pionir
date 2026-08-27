import unittest

from pionir.contracts import MemoryNamespace
from pionir.errors import NamespaceAccessDenied
from pionir.memory import InMemoryNamespaceStore


class MemoryTests(unittest.TestCase):
    def test_grant_allows_descendant_namespace(self) -> None:
        store = InMemoryNamespaceStore([MemoryNamespace("identity/theo")])
        namespace = MemoryNamespace("identity/theo/lessons")
        original = {"rule": ["be precise"]}
        store.put(namespace, "lesson-1", original)
        original["rule"].append("mutated")
        self.assertEqual(store.get(namespace, "lesson-1"), {"rule": ["be precise"]})

    def test_grant_denies_sibling_namespace(self) -> None:
        store = InMemoryNamespaceStore([MemoryNamespace("identity/theo")])
        with self.assertRaises(NamespaceAccessDenied):
            store.get(MemoryNamespace("identity/atani"), "state")


if __name__ == "__main__":
    unittest.main()
