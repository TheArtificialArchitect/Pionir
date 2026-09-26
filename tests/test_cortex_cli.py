"""The CLI path into the memory engine: remember, recall, and that the runtime
actually carries a store rather than the module floating unused (HEAD 3.1)."""

import tempfile
import unittest
from pathlib import Path

from pionir.bootstrap import build_runtime
from pionir.cli import _execute, _parser


class CortexCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_root = Path(self._tmp.name) / ".pionir"
        self._runtimes: list = []

    def tearDown(self) -> None:
        # Close every store first: Windows will not remove a directory while a
        # SQLite file in it is still held open.
        for runtime in self._runtimes:
            runtime.cortex.close()
        self._tmp.cleanup()

    def _runtime(self):
        from pionir.config import PionirSettings

        # Hermetic: nothing live - no Moss /health probe, no Ollama embedder.
        settings = PionirSettings(
            state_root=self.state_root,
            atani_command=("pionir-test-no-such-binary",),
            daedalus_url=None, melete_url=None, galatea_url=None, crew_url=None,
            bryo_status_command=None, nyx_status_command=None, voodoo_status_command=None,
            embed_model=None, evict_to_fit=False,
        )
        runtime = build_runtime(settings)
        self._runtimes.append(runtime)
        return runtime

    def test_the_runtime_carries_a_real_store(self) -> None:
        runtime = self._runtime()
        self.assertEqual(runtime.cortex.stats()["total"], 0)
        self.assertEqual(runtime.cortex.path, self.state_root / "cortex" / "memory.db")

    def test_remember_then_recall_round_trips_through_the_cli(self) -> None:
        runtime = self._runtime()
        parser = _parser()

        _execute(
            parser.parse_args(
                ["remember", "--kind", "fact", "--namespace", "ian",
                 "his", "sister", "Jana", "has", "an", "interview", "Tuesday"]
            ),
            runtime,
        )
        self.assertEqual(runtime.cortex.stats()["total"], 1)

        # A fresh runtime on the same state root: the memory persisted to disk,
        # not just to the process that wrote it.
        reopened = self._runtime()
        hits = reopened.cortex.recall("when is Jana's interview")
        self.assertTrue(hits)
        self.assertIn("Jana", hits[0].text)

    def test_recall_scopes_to_a_namespace(self) -> None:
        runtime = self._runtime()
        runtime.cortex.remember("fact", "the deploy key is in the vault", namespace="ops")
        runtime.cortex.remember("fact", "the deploy shipped Friday", namespace="chat")
        parser = _parser()
        # Namespace scoping is exercised directly on the store the CLI drives;
        # the command wiring is the thin part and is covered by the round-trip.
        hits = runtime.cortex.recall("deploy", namespace="ops")
        self.assertTrue(all(memory.namespace == "ops" for memory in hits))
        self.assertTrue(hits)
        _ = parser  # parser construction must not raise with the new subcommands


if __name__ == "__main__":
    unittest.main()
