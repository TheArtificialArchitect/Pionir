"""Namespaced memory primitives; persistence adapters can implement the same boundary."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable

from .contracts import MemoryNamespace
from .errors import NamespaceAccessDenied


class InMemoryNamespaceStore:
    """Test/reference store with explicit namespace grants and defensive copies."""

    def __init__(self, grants: Iterable[MemoryNamespace]) -> None:
        self._grants = frozenset(grants)
        self._data: dict[tuple[str, str], Any] = {}

    def _authorize(self, namespace: MemoryNamespace) -> None:
        if not any(grant.contains(namespace) for grant in self._grants):
            raise NamespaceAccessDenied(namespace.value)

    def put(self, namespace: MemoryNamespace, key: str, value: Any) -> None:
        self._authorize(namespace)
        if not key:
            raise ValueError("memory key is required")
        self._data[(namespace.value, key)] = deepcopy(value)

    def get(self, namespace: MemoryNamespace, key: str) -> Any | None:
        self._authorize(namespace)
        value = self._data.get((namespace.value, key))
        return deepcopy(value)

    def delete(self, namespace: MemoryNamespace, key: str) -> bool:
        self._authorize(namespace)
        return self._data.pop((namespace.value, key), None) is not None
