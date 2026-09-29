"""Review fixes on the live memory engine (2026-09-28, review of 21be736).

Each test fails with its fix reverted:

* HIGH - a lesson written from a failure holds structural facts only (who, which
  capability, error class/code): the caller's request - the voice's words - can
  never reach the shared lessons namespace, on /api/task or the intent seam, nor
  through the executive's refusal lesson; and any lesson is scrubbed and capped.
* MED - raw turns are scrubbed before storage; folded turns are DELETED after the
  retention window; a distilled fact carries its provenance and expires.
* MED - a fold sends a bounded chunk, backs off exponentially after a failure,
  and poisons a chunk after POISON_AFTER failures (logged, alarmed); the distil
  request pins the model to the CPU (options.num_gpu = 0).
* LOW - Bryo advising deferral holds a fold back.
* The pre-migration backup is not stacked: identical content is reused, and only
  the newest few are kept.
"""

from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from pionir.bootstrap import build_runtime
from pionir.config import PionirSettings
from pionir.consolidate import (
    BACKOFF_FIRST_SECONDS,
    BACKOFF_MAX_SECONDS,
    DistillUnavailable,
    POISON_AFTER,
    Consolidator,
    Distilled,
    OllamaDistiller,
)
from pionir.contracts import AgentManifest, Capability, Task, TaskResult
from pionir.cortex import LESSON_MAX_CHARS, LESSONS_NAMESPACE, Cortex
from pionir.memory_health import memory_health
from pionir.reliability import CircuitBreaker
from pionir.runtime import Executive
from pionir.server import PionirApp

SECRET = "ghp_" + "c" * 36
REQUEST = "please tell Ian that the tortoise password is under the blue mat"


class _Clock:
    def __init__(self, t: float = 1_790_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class _EchoAdapter:
    """A doer whose failures echo the request back - the leak path."""

    def __init__(self) -> None:
        self._manifest = AgentManifest(
            "echo", "test",
            (Capability("test.echo_fail", "fails, quoting the request back",
                        routing_hints=frozenset({"tortoise"})),
             Capability("test.echo_raise", "raises, quoting the request back"),
             Capability("test.echo_refuse", "refuses, quoting the request back")),
        )

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def execute(self, task: Task) -> TaskResult:
        said = str(task.payload.get("content"))
        if task.capability == "test.echo_raise":
            raise RuntimeError(f"could not do '{said}' (HTTP 502)")
        if task.capability == "test.echo_refuse":
            return TaskResult(task.task_id, "echo", {"ok": False, "refused": True, "error": f"no: {said}"}, ())
        return TaskResult(task.task_id, "echo",
                          {"ok": False, "error": f"HTTP 400: bad request '{said}' {SECRET}"}, ())


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
    runtime.register(_EchoAdapter())
    return PionirApp(runtime)


def _all_lesson_text(cortex: Cortex) -> str:
    return "\n".join(m.text for m in cortex.memories(LESSONS_NAMESPACE, limit=None))


# ------------------------------------------------------------------ HIGH
class LessonPrivacyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.app = _app(self._tmp.name)

    def tearDown(self) -> None:
        self.app.runtime.cortex.close()
        self._tmp.cleanup()

    def test_a_task_failure_never_puts_the_request_into_a_lesson(self) -> None:
        self.app.run_task("test.echo_fail", {"content": REQUEST}, wait=30)
        self.app.run_task("test.echo_raise", {"content": REQUEST}, wait=30)
        text = _all_lesson_text(self.app.runtime.cortex)
        self.assertIn("test.echo_fail failed: reported failure, HTTP 400", text)
        self.assertIn("test.echo_raise failed: RuntimeError, HTTP 502", text)
        for word in ("tortoise", "password", "blue mat", SECRET):
            self.assertNotIn(word, text)

    def test_an_intent_failure_never_puts_the_voices_words_into_a_lesson(self) -> None:
        decision = self.app.router.classify("tortoise")
        out = self.app._run_intent("test.echo_raise", {"content": REQUEST}, frozenset(), decision)
        self.assertEqual(out["status"], "error")
        text = _all_lesson_text(self.app.runtime.cortex)
        self.assertIn("test.echo_raise", text)
        self.assertNotIn("tortoise", text)
        self.assertNotIn("blue mat", text)

    def test_a_refusal_lesson_holds_no_request_text(self) -> None:
        lessons: list[str] = []
        ex = Executive(circuit_factory=lambda: CircuitBreaker(failure_threshold=3,
                                                              recovery_seconds=60),
                       on_lesson=lessons.append)
        ex.register(_EchoAdapter())
        ex.execute(Task("test.echo_refuse", {"content": REQUEST}, frozenset()))
        self.assertTrue(lessons)
        self.assertIn("refused a task test.echo_refuse", lessons[0])
        self.assertNotIn("tortoise", "\n".join(lessons))

    def test_any_lesson_is_scrubbed_and_capped_at_the_store(self) -> None:
        c = Cortex(":memory:")
        mid = c.record_lesson(f"the key {SECRET} leaked " + "x" * 1000)
        text = c.get(mid).text
        self.assertNotIn(SECRET, text)
        self.assertLessEqual(len(text), LESSON_MAX_CHARS)


# ------------------------------------------------------------ MED: turns
class TurnScrubAndRetentionTests(unittest.TestCase):
    def test_raw_turns_are_scrubbed_before_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = _app(tmp)
            try:
                app.intent(f"photosynthesis note, my token is {SECRET}")
                stored = "\n".join(m.text for m in app.runtime.cortex.memories(
                    "voice", kind="message"))
                self.assertIn("photosynthesis", stored)
                self.assertNotIn(SECRET, stored)
            finally:
                app.runtime.cortex.close()

    def test_folded_turns_are_deleted_after_retention_and_facts_expire(self) -> None:
        clock = _Clock()
        c = Cortex(":memory:", now=clock, turn_retention_days=14, fact_retention_days=180)
        for i in range(6):
            c.remember("message", f"turn {i}", namespace="voice")
        waiting = c.remember("message", "still waiting", namespace="other")

        class D:
            model = "fake"

            def distill(self, turns):
                return Distilled("they talked", ("the sky is blue",))

        outcome = Consolidator(c, D(), clock=clock).consolidate("voice")
        fact = c.get(outcome.fact_ids[0])
        # provenance: where the fact came from, and when it goes
        self.assertEqual(fact.meta["source"], "consolidation")
        self.assertEqual(fact.meta["episode_id"], outcome.episode_id)
        self.assertEqual(fact.meta["namespace"], "voice")
        self.assertEqual(fact.meta["expires_ts"], clock() + 180 * 86400)
        # (each step writes something, as live traffic does: retention only
        # advances as far as the store's own writes corroborate the clock)
        clock.t += 13 * 86400
        c.remember("note", "day 13")
        self.assertEqual(c.purge(), {"turns": 0, "facts": 0})
        clock.t += 2 * 86400
        c.remember("note", "day 15")
        self.assertEqual(c.purge()["turns"], 6)
        rows = c._db.execute("SELECT COUNT(*) FROM memories WHERE kind='message'").fetchone()[0]
        self.assertEqual(rows, 1)                     # deleted, not retired
        self.assertIsNotNone(c.get(waiting))          # an unfolded turn is kept
        clock.t += 170 * 86400
        c.remember("note", "day 185")
        self.assertEqual(c.purge()["facts"], 1)
        self.assertIsNone(c.get(outcome.fact_ids[0]))
        self.assertIsNotNone(c.get(outcome.episode_id))


    def test_a_clock_jumping_forward_cannot_purge_early(self) -> None:
        clock = _Clock()
        c = Cortex(":memory:", now=clock, fact_retention_days=180)
        for i in range(6):
            c.remember("message", f"turn {i}", namespace="voice")

        class D:
            def distill(self, turns):
                return Distilled("they talked", ("the sky is blue",))

        outcome = Consolidator(c, D(), clock=clock).consolidate("voice")
        clock.t += 400 * 86400                         # the wall clock leaps a year
        with self.assertLogs("pionir.cortex", level="WARNING"):
            self.assertEqual(c.purge(), {"turns": 0, "facts": 0})
        self.assertIsNotNone(c.get(outcome.fact_ids[0]))
        self.assertEqual(len(c._db.execute(
            "SELECT id FROM memories WHERE kind='message'").fetchall()), 6)


# -------------------------------------------------------------- MED: fold
class _Recorder:
    model = "fake"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[list[str]] = []

    def distill(self, turns):
        self.calls.append(list(turns))
        if self.fail:
            raise RuntimeError("context overflow")
        return Distilled("a chunk")


class FoldBoundsTests(unittest.TestCase):
    def test_a_fold_sends_a_bounded_chunk_oldest_first(self) -> None:
        c = Cortex(":memory:")
        for i in range(20):
            c.remember("message", f"turn {i:02d} " + "y" * 400, namespace="voice")
        rec = _Recorder()
        con = Consolidator(c, rec, chunk_tokens=500)   # ~2000 chars: 4 turns of ~400
        outcome = con.consolidate("voice")
        self.assertEqual(outcome.folded_turns, 4)
        self.assertTrue(rec.calls[0][0].startswith("turn 00"))
        self.assertLessEqual(sum(len(t) for t in rec.calls[0]), 500 * 4)
        self.assertEqual(len(c.memories("voice", kind="message")), 16)

    def test_a_failing_chunk_backs_off_then_is_poisoned(self) -> None:
        clock = _Clock()
        c = Cortex(":memory:", now=clock)
        for i in range(6):
            c.remember("message", f"turn {i}", namespace="voice")
        rec = _Recorder(fail=True)
        con = Consolidator(c, rec, clock=clock)
        with self.assertLogs("pionir.consolidate", level="WARNING"):
            con.consolidate("voice")
        self.assertEqual(len(rec.calls), 1)
        # straight away: backing off, the model is not asked again
        self.assertIsNone(con.consolidate("voice"))
        self.assertFalse(con.due("voice"))
        self.assertEqual(len(rec.calls), 1)
        # the backoff doubles: 60 s, then 120 s, ...
        waits = [BACKOFF_FIRST_SECONDS * 2 ** n for n in range(POISON_AFTER - 1)]
        with self.assertLogs("pionir.consolidate", level="WARNING") as caught:
            for wait in waits:
                clock.t += wait - 1
                asked = len(rec.calls)
                self.assertIsNone(con.consolidate("voice"))   # not yet: model not asked
                self.assertEqual(len(rec.calls), asked)
                clock.t += 1
                con.consolidate("voice")
        self.assertEqual(len(rec.calls), POISON_AFTER)
        self.assertTrue(any("poisoned" in line for line in caught.output))
        # poisoned: out of the backlog, not retried, and doctor shouts
        self.assertEqual(c.memories("voice", kind="message"), [])
        alerts = memory_health(c, [], now=clock())["alerts"]
        self.assertTrue(any("poisoned" in a for a in alerts), alerts)
        clock.t += 10 ** 6
        self.assertIsNone(con.consolidate("voice"))
        self.assertEqual(len(rec.calls), POISON_AFTER)

    def test_nothing_worth_keeping_is_a_fold_not_a_failure(self) -> None:
        clock = _Clock()
        c = Cortex(":memory:", now=clock)
        for i in range(12):
            c.remember("message", f"ok {i}", namespace="voice")

        class Empty:
            calls = 0

            def distill(self, turns):
                Empty.calls += 1
                return Distilled("")

        con = Consolidator(c, Empty(), chunk_tokens=1, clock=clock)   # a turn a chunk
        for _ in range(POISON_AFTER + 2):
            self.assertIsNotNone(con.consolidate("voice", min_turns=1))
        # every attempt folded a chunk straight away: no backoff, nothing poisoned
        self.assertEqual(Empty.calls, POISON_AFTER + 2)
        self.assertEqual(c.memories("voice", kind="episode"), [])
        self.assertEqual(c.output()["poisoned_chunks"], 0)
        self.assertIsNotNone(c.output()["last_consolidation"])   # it counts as a fold
        folded = c._db.execute("SELECT COUNT(*) FROM memories WHERE kind='message' AND "
                               "json_extract(meta, '$.folded_into') = 0").fetchone()[0]
        self.assertEqual(folded, POISON_AFTER + 2)
        self.assertEqual(memory_health(c, [], now=clock())["alerts"], [])

    def test_an_unavailable_model_backs_off_with_a_cap_and_never_poisons(self) -> None:
        clock = _Clock()
        c = Cortex(":memory:", now=clock)
        for i in range(6):
            c.remember("message", f"turn {i}", namespace="voice")

        class Away:
            calls = 0

            def distill(self, turns):
                Away.calls += 1
                raise DistillUnavailable("qwen3: timed out")

        con = Consolidator(c, Away(), clock=clock)
        with self.assertLogs("pionir.consolidate", level="WARNING"):
            for _ in range(POISON_AFTER * 3):
                clock.t += BACKOFF_MAX_SECONDS + 1     # the cap: never longer than this
                self.assertIsNone(con.consolidate("voice"))
        self.assertEqual(Away.calls, POISON_AFTER * 3)
        self.assertEqual(len(c.memories("voice", kind="message")), 6)   # never poisoned
        self.assertEqual(c.output()["poisoned_chunks"], 0)
        alerts = memory_health(c, [], now=clock())["alerts"]
        self.assertTrue(any("unavailable" in a for a in alerts), alerts)

    def test_only_non_transient_failures_count_toward_poisoning(self) -> None:
        clock = _Clock()
        c = Cortex(":memory:", now=clock)
        for i in range(6):
            c.remember("message", f"turn {i}", namespace="voice")

        class Flaky:
            calls = 0

            def distill(self, turns):
                Flaky.calls += 1
                if Flaky.calls < POISON_AFTER:
                    raise DistillUnavailable("timed out")
                raise RuntimeError("bad output")

        con = Consolidator(c, Flaky(), clock=clock)
        with self.assertLogs("pionir.consolidate", level="WARNING"):
            for _ in range(POISON_AFTER):
                clock.t += BACKOFF_MAX_SECONDS + 1
                con.consolidate("voice")
        # POISON_AFTER failures in all, but only one was the turns' fault
        self.assertEqual(Flaky.calls, POISON_AFTER)
        self.assertEqual(len(c.memories("voice", kind="message")), 6)

    def test_the_ollama_distiller_calls_a_timeout_transient(self) -> None:
        class Opener:
            def open(self, request, timeout=None):
                raise TimeoutError("timed out")

        with self.assertRaises(DistillUnavailable):
            OllamaDistiller("m", opener=Opener()).distill(["a"])

    def test_the_distil_request_keeps_the_model_off_the_gpu(self) -> None:
        sent: list[dict] = []

        class Opener:
            def open(self, request, timeout=None):
                sent.append(json.loads(request.data.decode("utf-8")))
                reply = {"message": {"content": json.dumps({"summary": "s", "facts": []})}}
                return io.BytesIO(json.dumps(reply).encode("utf-8"))

        distilled = OllamaDistiller("qwen3:4b-instruct-2507-q4_K_M",
                                    opener=Opener()).distill(["a", "b"])
        self.assertEqual(distilled.summary, "s")
        self.assertEqual(sent[0]["options"]["num_gpu"], 0)


# ------------------------------------------------------------ LOW: Bryo
class BryoDeferralTests(unittest.TestCase):
    def test_a_fold_waits_when_bryo_advises_deferring(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = _app(tmp)
            rec = _Recorder()
            app.distiller = rec
            app.runtime.executive._pressure_probe = lambda: SimpleNamespace(
                alive=True, defer_heavy_work=True, detail="bryo: hot")
            try:
                for i in range(app.CONSOLIDATE_AT // 2):
                    app.intent(f"photosynthesis in tomato plants number {i}")
                app._consolidation_thread.join(30)
                self.assertEqual(rec.calls, [])
                attempt = app.runtime.cortex.output()["last_consolidation_attempt"]
                self.assertEqual(attempt["outcome"], "deferred")
            finally:
                app.runtime.cortex.close()


# ---------------------------------------------------------- the backups
class BackupTests(unittest.TestCase):
    def test_backups_are_reused_when_unchanged_and_capped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = _Clock()
            c = Cortex(Path(tmp) / "memory.db", now=clock)
            try:
                first = c._backup("pre-x")
                clock.t += 5
                self.assertEqual(c._backup("pre-x"), first)       # same content: reused
                for i in range(5):
                    clock.t += 5
                    c.remember("fact", f"change {i}")
                    c._backup("pre-x")
                kept = list(Path(tmp).glob("memory.pre-x-*.db"))
                self.assertEqual(len(kept), 3)
            finally:
                c.close()


if __name__ == "__main__":
    unittest.main()
