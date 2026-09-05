"""Tests for the CLI's own rendering and its view of declared models.

Both of these were wrong on the first live Theo turn while the whole suite was
green, which is what they are here to stop happening again.
"""

import json
import unittest
from types import MappingProxyType
from unittest import mock

from pionir.benchmark import LoadedModel
from pionir.cli import _declared_models, _jsonable
from pionir.contracts import AgentManifest, Capability, ModelRequirement


class RenderingTests(unittest.TestCase):
    def test_renders_a_task_output_as_json_not_a_python_repr(self) -> None:
        # TaskResult.output is a MappingProxyType, which is not a dict subclass.
        # Missing that made every reply print as a quoted repr that nothing
        # downstream could parse.
        rendered = json.dumps(_jsonable(MappingProxyType({"reply": "hi", "n": 1})))
        self.assertEqual(json.loads(rendered), {"reply": "hi", "n": 1})

    def test_renders_nested_mappings_too(self) -> None:
        value = MappingProxyType({"outer": MappingProxyType({"inner": "x"})})
        self.assertEqual(json.loads(json.dumps(_jsonable(value))), {"outer": {"inner": "x"}})


class _Runtime:
    def __init__(self, model_id: str) -> None:
        capability = Capability(
            name="conversation.theo_reply",
            description="d",
            model=ModelRequirement(model_id, 4_500, 690),
        )

        class _Registry:
            def manifests(self_inner):
                return (AgentManifest("theo", "1", (capability,)),)

        class _Executive:
            registry = _Registry()

        self.executive = _Executive()


def _loaded(name: str) -> LoadedModel:
    return LoadedModel(name=name, size_bytes=1, size_vram_bytes=1, context_length=16384)


class DeclaredModelTests(unittest.TestCase):
    def test_sees_a_resident_model_across_the_latest_tag_difference(self) -> None:
        # Theo reports his build untagged; the daemon tags it `:latest`.
        # Compared verbatim this read False for a model that was resident, so
        # the view built to show declaration drift was inventing it while
        # admission underneath was correct. Observed live 2026-09-04.
        with mock.patch(
            "pionir.benchmark.read_loaded_models",
            return_value=[_loaded("theo-local-v25-q4:latest")],
        ):
            rows = _declared_models(_Runtime("theo-local-v25-q4"))
        self.assertEqual(rows, [{"model_id": "theo-local-v25-q4", "resident": True}])

    def test_a_genuinely_different_build_still_reads_as_absent(self) -> None:
        with mock.patch(
            "pionir.benchmark.read_loaded_models",
            return_value=[_loaded("theo-local-v25-q4:latest")],
        ):
            rows = _declared_models(_Runtime("theo-local-v17-q4"))
        self.assertFalse(rows[0]["resident"])

    def test_an_unreachable_daemon_is_not_reported_as_absent(self) -> None:
        # None means "could not look", which is not the same as "not loaded".
        from pionir.benchmark import BenchmarkError

        with mock.patch(
            "pionir.benchmark.read_loaded_models", side_effect=BenchmarkError("no daemon")
        ):
            rows = _declared_models(_Runtime("theo-local-v25-q4"))
        self.assertIsNone(rows[0]["resident"])


if __name__ == "__main__":
    unittest.main()
