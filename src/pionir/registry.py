"""Deterministic specialist registration and capability routing."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import AgentManifest, Capability, Task
from .errors import CapabilityNotFound, PermissionDenied


@dataclass(frozen=True, slots=True)
class Route:
    agent_id: str
    capability: Capability


class CapabilityRegistry:
    """Registers manifests and resolves one task to one explicit specialist."""

    def __init__(self) -> None:
        self._manifests: dict[str, AgentManifest] = {}

    def register(self, manifest: AgentManifest) -> None:
        if manifest.agent_id in self._manifests:
            raise ValueError(f"agent already registered: {manifest.agent_id}")
        self._manifests[manifest.agent_id] = manifest

    def unregister(self, agent_id: str) -> None:
        self._manifests.pop(agent_id, None)

    def resolve(self, task: Task) -> Route:
        candidates = [
            Route(manifest.agent_id, capability)
            for manifest in self._manifests.values()
            for capability in manifest.capabilities
            if capability.name == task.capability
        ]
        if not candidates:
            raise CapabilityNotFound(task.capability)

        authorized = [
            route
            for route in candidates
            if route.capability.required_permissions <= task.granted_permissions
        ]
        if not authorized:
            missing_options = sorted(
                {
                    tuple(sorted(route.capability.required_permissions - task.granted_permissions))
                    for route in candidates
                }
            )
            raise PermissionDenied(
                f"capability '{task.capability}' requires one of these permission sets; "
                f"missing: {missing_options}"
            )

        return sorted(
            authorized,
            key=lambda route: (-route.capability.priority, route.agent_id),
        )[0]

    def manifests(self) -> tuple[AgentManifest, ...]:
        return tuple(self._manifests[key] for key in sorted(self._manifests))
