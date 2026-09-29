"""The memory engine must be live, not wired-but-inert (audit 2026-09-28).

What the live store showed: 800 memories, all lessons, 6 distinct texts (398
copies each of two fiverr.ack failures); nothing but lessons ever written;
consolidation never run; every task handed three lessons however unrelated,
which were then thrown away. Each test here fails with its fix reverted:

* a known lesson recurs (count goes up) instead of being inserted again;
* migration 1 collapses an existing flood - backup first, soft-retire, undoable;
* lessons_for is relevance-floored, so unrelated lessons are not handed out;
* recalled lessons reach the caller in the task response, and a failed recall
  is logged, not swallowed;
* a swallowed embedding failure is counted and logged; rows written while the
  embedder was down are backfilled on its next success;
* the voice's intents are written as raw turns in her namespace, and her own
  traffic folds them into an episode (consolidation logged either way);
* doctor's memory output counts writes/consolidation/coverage and alarms on a
  stall.
"""

from __future__ import annotations

import json
import math
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from pionir.bootstrap import build_runtime
from pionir.config import PionirSettings
from pionir.consolidate import Consolidator, Distilled
from pionir.contracts import AgentManifest, Capability, Task, TaskResult
from pionir.cortex import LESSONS_NAMESPACE, Cortex, lesson_key
from pionir.memory_health import memory_health
from pionir.server import PionirApp


class _Clock:
    def __init__(self, t: float = 1_790_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class _KeywordEmbedder:
    """Deterministic vectors: one axis per keyword group, so cosines are known."""

    model = "fake-embed"

    def __init__(self, axes: dict[str, list[float]]) -> None:
        self.axes = axes
        self.down = False
        self.raise_error = False

    def embed(self, texts):
        if self.raise_error:
            raise RuntimeError("ollama fell over")
        if self.down:
            return None
        out = []
        for text in texts:
            vec = [0.0, 0.0, 0.0]
            for word, axis in self.axes.items():
                if word in text.lower():
                    vec = [a + b for a, b in zip(vec, axis)]
            out.append(vec if any(vec) else [0.01, 0.01, 0.01])
        return out


# --------------------------------------------------------------- lessons
class LessonDedupeTests(unittest.TestCase):
    def test_the_same_lesson_recurs_instead_of_being_inserted_again(self) -> None:
        c = Cortex(":memory:")
        first = c.record_lesson("fiverr · fiverr.ack failed: Scrooge answered HTTP 400")
        again = c.record_lesson("fiverr  ·  fiverr.ack FAILED: Scrooge answered HTTP 400 ")
        self.assertEqual(first, again)
        self.assertEqual(c.stats()["by_namespace"], {LESSONS_NAMESPACE: 1})
        self.assertEqual(c.get(first).meta["seen"], 2)

    def test_long_ids_do_not_make_a_new_lesson_but_short_numbers_do(self) -> None:
        c = Cortex(":memory:")
        a = c.record_lesson("ack failed for event 1790402658 (task 3f2a9c1b-77aa-4e1d)")
        b = c.record_lesson("ack failed for event 1790409999 (task 9e8d7c6b-1234-4abc)")
        self.assertEqual(a, b)
        c.record_lesson("gumroad answered HTTP 500")
        c.record_lesson("gumroad answered HTTP 401")
        self.assertEqual(c.stats()["total"], 3)
        self.assertNotEqual(lesson_key("HTTP 500"), lesson_key("HTTP 401"))


class LessonMigrationTests(unittest.TestCase):
    def _flooded(self, path: Path) -> None:
        # A store as the live one was: plain inserts, no dedupe, user_version 0.
        c = Cortex(path)
        for _ in range(5):
            c.remember("lesson", "fiverr.ack failed: must be an event id",
                       namespace=LESSONS_NAMESPACE)
        c.remember("lesson", "crew is not running", namespace=LESSONS_NAMESPACE)
        c.remember("fact", "fiverr.ack failed: must be an event id", namespace="other")
        c._db.execute("PRAGMA user_version=0")
        c._db.commit()
        c.close()

    def test_opening_a_flooded_store_backs_up_then_collapses_it_reversibly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memory.db"
            self._flooded(path)
            c = Cortex(path)
            try:
                self.assertIsNone(c.migration_error)
                self.assertEqual(c.stats()["by_namespace"], {LESSONS_NAMESPACE: 2, "other": 1})
                kept = c.memories(LESSONS_NAMESPACE)[0]
                self.assertEqual(kept.meta["seen"], 5)
                # six lesson writes happened, whether counted as rows or recurrences
                self.assertEqual(c.output()["writes"][LESSONS_NAMESPACE], 6)
                # the backup was taken first and holds every original row, all active
                backups = list(Path(tmp).glob("memory.pre-lesson-dedupe-*.db"))
                self.assertEqual(len(backups), 1)
                raw = sqlite3.connect(backups[0])
                self.assertEqual(raw.execute(
                    "SELECT COUNT(*) FROM memories WHERE active=1").fetchone()[0], 7)
                raw.close()
                # retired, not deleted - and undoable
                self.assertEqual(c.undo_lesson_dedupe(), 4)
                self.assertEqual(c.stats()["by_namespace"], {LESSONS_NAMESPACE: 6, "other": 1})
            finally:
                c.close()
            # it runs once: reopening does not collapse them again
            c = Cortex(path)
            try:
                self.assertEqual(c.stats()["by_namespace"][LESSONS_NAMESPACE], 6)
            finally:
                c.close()


class LessonRelevanceTests(unittest.TestCase):
    def test_an_unrelated_lesson_is_not_handed_out_before_acting(self) -> None:
        emb = _KeywordEmbedder({"ack": [1.0, 0.0, 0.0], "orders": [0.5, 0.0, 0.866],
                                "refund": [0.0, 1.0, 0.1], "reimburse": [0.0, 1.0, 0.1]})
        c = Cortex(":memory:", embedder=emb)
        c.record_lesson("fiverr ack failed: Scrooge wants an event id")
        c.record_lesson("a refund must be approved by the owner first")
        # 'orders' shares no word with either lesson; its cosine to the ack lesson
        # is 0.5 - below the floor - so nothing comes back
        self.assertEqual(c.lessons_for("client orders"), [])
        # a strong semantic match with no shared word still qualifies (cos ~0.74)
        hits = c.lessons_for("reimburse the orders")
        self.assertIn("refund", " ".join(m.text for m in hits))
        self.assertNotIn("ack", " ".join(m.text for m in hits))


# ------------------------------------------------------------- embedding
class EmbeddingSurfacedTests(unittest.TestCase):
    def test_a_swallowed_embedding_failure_is_counted_and_logged(self) -> None:
        emb = _KeywordEmbedder({})
        emb.raise_error = True
        c = Cortex(":memory:", embedder=emb)
        with self.assertLogs("pionir.cortex", level="WARNING") as caught:
            c.remember("fact", "the sky is blue")
        self.assertIn("ollama fell over", "\n".join(caught.output))
        stats = c.stats()
        self.assertEqual(stats["embed_failures"], 1)
        self.assertIn("ollama fell over", stats["last_embed_error"])

    def test_rows_written_while_the_embedder_was_down_are_backfilled(self) -> None:
        emb = _KeywordEmbedder({"sky": [1.0, 0.0, 0.0]})
        c = Cortex(":memory:", embedder=emb)
        emb.down = True
        with self.assertLogs("pionir.cortex", level="WARNING"):
            c.remember("fact", "the sky is blue")
        self.assertEqual(c.stats()["embedded"], 0)
        emb.down = False
        c.remember("fact", "the sky is wide")
        self.assertEqual(c.stats()["embedded"], 2)
        self.assertEqual(c.output()["embed_coverage_pct"], 100.0)


# ------------------------------------------------------------ the server
class _FlakeAdapter:
    def __init__(self) -> None:
        self._manifest = AgentManifest(
            "flake", "test",
            (Capability("test.fail_read", "returns ok:false",
                        routing_hints=frozenset({"flakey"})),),
        )

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def execute(self, task: Task) -> TaskResult:
        return TaskResult(task.task_id, "flake", {"ok": False, "error": "widget manifest bad"}, ())


class _FakeDistiller:
    def __init__(self) -> None:
        self.calls = 0

    def distill(self, turns):
        self.calls += 1
        return Distilled(summary=f"the voice asked {len(turns) // 2} unclear things",
                         facts=("she likes tomatoes",))


def _app(tmp: str) -> PionirApp:
    runtime = build_runtime(
        PionirSettings(
            state_root=Path(tmp),
            atani_command=("pionir-test-no-such-binary",),
            bryo_status_command=None, nyx_status_command=None,
            voodoo_status_command=None, daedalus_url=None, melete_url=None,
            galatea_url=None, evict_to_fit=False,
            embed_model=None,  # lexical-only cortex: no network, no embedder
        )
    )
    runtime.register(_FlakeAdapter())
    return PionirApp(runtime)


class RecallBeforeActTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.app = _app(self._tmp.name)

    def tearDown(self) -> None:
        self.app.runtime.cortex.close()
        self._tmp.cleanup()

    def test_the_recalled_lessons_reach_the_caller(self) -> None:
        self.app.run_task("test.fail_read", {"content": "parse the widget manifest"}, wait=30)
        out = self.app.run_task("test.fail_read", {"content": "parse the widget manifest"},
                                wait=30)
        self.assertIn("lessons", out)
        self.assertIn("widget manifest", out["lessons"][0]["text"])
        # and the repeat was counted on the one lesson, not written as another
        self.assertGreaterEqual(out["lessons"][0]["seen"], 1)
        lessons = self.app.runtime.cortex.memories(LESSONS_NAMESPACE)
        self.assertEqual(len({lesson_key(m.text) for m in lessons}), len(lessons))

    def test_a_failed_recall_is_logged_not_swallowed(self) -> None:
        cortex = self.app.runtime.cortex

        def broken(*_a, **_k):
            raise sqlite3.OperationalError("database is locked")

        cortex.lessons_for = broken  # type: ignore[method-assign]
        with self.assertLogs("pionir.server", level="WARNING") as caught:
            out = self.app.run_task("test.fail_read", {"content": "x"}, wait=30)
        self.assertIn("recalling lessons", "\n".join(caught.output))
        self.assertNotIn("lessons", out)


class VoiceTurnsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.app = _app(self._tmp.name)
        self.app.distiller = _FakeDistiller()

    def tearDown(self) -> None:
        self.app.runtime.cortex.close()
        self._tmp.cleanup()

    def test_an_intent_is_written_as_raw_turns_in_the_voices_namespace(self) -> None:
        self.app.intent("photosynthesis in tomato plants")
        turns = self.app.runtime.cortex.memories("voice", kind="message")
        self.assertEqual([m.text.split(":")[0] for m in turns], ["asked", "told"])
        self.assertIn("photosynthesis", turns[0].text)
        self.assertIn("unclear", turns[1].text)

    def test_her_own_traffic_folds_the_turns_into_an_episode(self) -> None:
        cortex = self.app.runtime.cortex
        for i in range(self.app.CONSOLIDATE_AT // 2):
            self.app.intent(f"photosynthesis in tomato plants number {i}")
        thread = self.app._consolidation_thread
        self.assertIsNotNone(thread)
        thread.join(30)
        self.assertEqual(self.app.distiller.calls, 1)
        self.assertEqual(cortex.memories("voice", kind="message"), [])
        episodes = cortex.memories("voice", kind="episode")
        self.assertEqual(len(episodes), 1)
        last = cortex.output()["last_consolidation"]
        self.assertEqual((last["namespace"], last["folded"]), ("voice", self.app.CONSOLIDATE_AT))

    def test_a_failing_fold_is_logged_and_recorded(self) -> None:
        class Broken:
            def distill(self, turns):
                raise RuntimeError("model fell over")

        self.app.distiller = Broken()
        with self.assertLogs("pionir.consolidate", level="WARNING"):
            for i in range(self.app.CONSOLIDATE_AT // 2):
                self.app.intent(f"photosynthesis in tomato plants number {i}")
            self.app._consolidation_thread.join(30)
        attempt = self.app.runtime.cortex.output()["last_consolidation_attempt"]
        self.assertEqual(attempt["outcome"], "failed")
        self.assertIn("model fell over", attempt["detail"])
        # the turns were kept for a later pass
        self.assertEqual(len(self.app.runtime.cortex.memories("voice", kind="message")),
                         self.app.CONSOLIDATE_AT)


# ---------------------------------------------------------------- doctor
def _failed(at: float) -> dict:
    from datetime import UTC, datetime
    return {"event_type": "task.failed",
            "occurred_at": datetime.fromtimestamp(at, UTC).isoformat()}


class MemoryHealthTests(unittest.TestCase):
    def test_writes_per_namespace_and_recurrences_are_counted(self) -> None:
        clock = _Clock()
        c = Cortex(":memory:", now=clock)
        c.remember("message", "hi", namespace="voice")
        c.record_lesson("ack failed")
        c.record_lesson("ack failed")
        c.record_lesson("ack failed")
        view = memory_health(c, [], now=clock())
        self.assertEqual(view["writes_24h"], {"voice": 1, LESSONS_NAMESPACE: 3})
        self.assertEqual(view["alerts"], [])

    def test_failing_tasks_with_no_lesson_written_is_a_loud_stall(self) -> None:
        clock = _Clock()
        c = Cortex(":memory:", now=clock)
        events = [_failed(clock() - 60) for _ in range(5)]
        alerts = memory_health(c, events, now=clock())["alerts"]
        self.assertTrue(any("lesson writer has stalled" in a for a in alerts), alerts)
        c.record_lesson("the thing failed")
        alerts = memory_health(c, events, now=clock())["alerts"]
        self.assertFalse(any("stalled" in a for a in alerts), alerts)

    def test_a_lesson_repeating_all_day_is_an_alarm(self) -> None:
        clock = _Clock()
        c = Cortex(":memory:", now=clock)
        for _ in range(25):
            clock.t += 60
            c.record_lesson("fiverr.ack failed: must be an event id")
        alerts = memory_health(c, [], now=clock())["alerts"]
        self.assertTrue(any("recurred 24 times" in a for a in alerts), alerts)

    def test_turns_waiting_unfolded_for_hours_is_a_stall(self) -> None:
        clock = _Clock()
        c = Cortex(":memory:", now=clock)
        for i in range(12):
            c.remember("message", f"turn {i}", namespace="voice")
        c.note_consolidation("voice", "declined", detail="qwen3: not JSON")
        self.assertEqual(memory_health(c, [], now=clock())["alerts"], [])
        clock.t += 7 * 3600
        alerts = memory_health(c, [], now=clock())["alerts"]
        self.assertTrue(any("consolidation stalled" in a and "declined" in a
                            for a in alerts), alerts)

    def test_low_embedding_coverage_is_an_alarm(self) -> None:
        emb = _KeywordEmbedder({})
        clock = _Clock()
        c = Cortex(":memory:", embedder=emb, now=clock)
        emb.down = True
        with self.assertLogs("pionir.cortex", level="WARNING"):
            c.remember("fact", "one")
        alerts = memory_health(c, [], now=clock())["alerts"]
        self.assertTrue(any("have a vector" in a for a in alerts), alerts)

    def test_doctor_carries_the_memory_output(self) -> None:
        from pionir.cli import _doctor

        with tempfile.TemporaryDirectory() as tmp:
            runtime = build_runtime(
                PionirSettings(
                    state_root=Path(tmp),
                    atani_command=("pionir-test-no-such-binary",),
                    bryo_status_command=None, nyx_status_command=None,
                    voodoo_status_command=None, daedalus_url=None,
                    melete_url=None, galatea_url=None, evict_to_fit=False,
                    embed_model=None,
                )
            )
            try:
                runtime.cortex.record_lesson("a lesson")
                memory = _doctor(runtime)["memory"]
                self.assertEqual(memory["output"]["writes_24h"], {LESSONS_NAMESPACE: 1})
                self.assertIn("alerts", memory["output"])
            finally:
                runtime.cortex.close()


if __name__ == "__main__":
    unittest.main()
