"""Bryo's felt pressure as the spine's advisory signal.

The body advises; it never decides and it can never be in the critical path. Every
guarantee that follows from that is pinned here:

  * the reader never blocks and fails open on anything odd;
  * old advice is dropped rather than acted on;
  * the executive records the advice on GPU work but holds back only work that was
    explicitly marked deferrable, and only when a LIVING Bryo advises it;
  * a silent, dead or broken organism changes nothing at all.
"""

from __future__ import annotations

import json
import unittest

from support import offline_scheduler

from pionir.bryo_pressure import (
    SCHEMA,
    BryoPressureReader,
    PressureReading,
    neutral,
    parse_vitals,
)
from pionir.contracts import AgentManifest, Capability, ModelRequirement, Task, TaskResult
from pionir.errors import BodyDeferred, ResourceUnavailable
from pionir.runtime import Executive, InMemoryAuditSink


def vitals(**over) -> str:
    doc = {
        "schema": SCHEMA, "alive": True, "pressure": 0.4,
        "advisory": {"defer_heavy_work": False, "note": "comfortable"},
    }
    doc.update(over)
    return json.dumps(doc)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FakeRunner:
    def __init__(self, output: str = "", error: Exception | None = None) -> None:
        self.output, self.error, self.calls = output, error, 0

    def run(self, *, timeout_seconds: int) -> str:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.output


def reader(runner, clock=None, **kw) -> BryoPressureReader:
    # spawn synchronously so tests are deterministic; production spawns a daemon thread
    return BryoPressureReader(("python", "-m", "bryo.status"), runner=runner,
                              clock=clock or FakeClock(), spawn=lambda fn: fn(), **kw)


class ParseVitalsTests(unittest.TestCase):
    def test_a_living_stressed_organism_advises_deferring(self) -> None:
        r = parse_vitals(vitals(pressure=0.8, advisory={"defer_heavy_work": True, "note": "stressed"}))
        self.assertEqual(r, PressureReading(True, 0.8, True, "stressed"))

    def test_a_dead_organism_never_advises_deferring(self) -> None:
        r = parse_vitals(vitals(alive=False, advisory={"defer_heavy_work": True, "note": "stale"}))
        self.assertFalse(r.alive)
        self.assertFalse(r.defer_heavy_work, "stale advice is not evidence about the machine now")

    def test_anything_odd_fails_open_to_neutral(self) -> None:
        for text in ("", "not json", "[]", json.dumps({"schema": "bryo.vitals/99"}),
                     vitals(pressure="high"), vitals(pressure=1.7), vitals(pressure=float("nan"))):
            with self.subTest(text=text[:40]):
                r = parse_vitals(text)
                self.assertFalse(r.alive)
                self.assertFalse(r.defer_heavy_work)
                self.assertEqual(r.pressure, 0.0)


class ReaderTests(unittest.TestCase):
    def test_first_peek_is_neutral_and_starts_a_refresh(self) -> None:
        spawned = []
        rd = BryoPressureReader(("python", "-m", "bryo.status"), runner=FakeRunner(vitals()),
                                clock=FakeClock(), spawn=spawned.append)
        self.assertFalse(rd.peek().alive, "nothing is known until a read completes")
        self.assertEqual(len(spawned), 1)

    def test_read_now_reads_synchronously_for_a_one_shot_cli(self) -> None:
        # peek() is neutral until a background read completes - fine for the
        # long-running server, useless for a one-shot process that exits first.
        # read_now() does the read on the calling thread (item 4).
        runner = FakeRunner(vitals(pressure=0.7, advisory={"defer_heavy_work": True, "note": "busy"}))
        rd = BryoPressureReader(("python", "-m", "bryo.status"), runner=runner,
                                clock=FakeClock(), spawn=lambda fn: None)  # no background thread
        reading = rd.read_now()
        self.assertTrue(reading.alive)
        self.assertEqual(reading.pressure, 0.7)
        self.assertTrue(reading.defer_heavy_work)
        self.assertGreaterEqual(runner.calls, 1)

    def test_it_appends_json_to_the_configured_command(self) -> None:
        rd = BryoPressureReader(("python", "-m", "bryo.status"), cwd=r"C:\src\terrarium")
        self.assertEqual(rd._runner._command, ("python", "-m", "bryo.status", "--json"))

    def test_cached_within_ttl_then_refreshed(self) -> None:
        clock, runner = FakeClock(), FakeRunner(vitals(pressure=0.3))
        rd = reader(runner, clock, ttl_seconds=30, max_age_seconds=120)
        rd.peek()                      # kicks the first read (synchronous here)
        self.assertEqual(rd.peek().pressure, 0.3)
        calls = runner.calls
        clock.now = 10
        rd.peek()
        self.assertEqual(runner.calls, calls, "no re-read inside the TTL")
        clock.now = 31
        rd.peek()
        self.assertEqual(runner.calls, calls + 1, "re-read once the TTL lapses")

    def test_single_flight_while_a_refresh_is_in_progress(self) -> None:
        spawned = []
        rd = BryoPressureReader(("python", "-m", "bryo.status"), runner=FakeRunner(vitals()),
                                clock=FakeClock(), spawn=spawned.append)
        rd.peek(); rd.peek(); rd.peek()
        self.assertEqual(len(spawned), 1, "concurrent peeks must not stack subprocesses")

    def test_advice_older_than_max_age_is_dropped(self) -> None:
        clock = FakeClock()
        stressed = vitals(pressure=0.9, advisory={"defer_heavy_work": True, "note": "stressed"})
        rd = BryoPressureReader(("python", "-m", "bryo.status"), runner=FakeRunner(stressed),
                                clock=clock, ttl_seconds=30, max_age_seconds=120,
                                spawn=lambda fn: fn())
        rd.peek()
        self.assertTrue(rd.peek().defer_heavy_work)
        # the refresh machinery wedges (spawn now does nothing) and time passes
        rd._spawn = lambda fn: None
        clock.now = 500
        r = rd.peek()
        self.assertFalse(r.defer_heavy_work, "a dead organism's last stress must not hold work")
        self.assertFalse(r.alive)

    def test_a_failing_read_is_neutral_not_an_exception(self) -> None:
        rd = reader(FakeRunner(error=OSError("no terrarium")))
        rd.peek()
        r = rd.peek()
        self.assertFalse(r.alive)
        self.assertIn("unreadable", r.note)

    def test_a_failed_spawn_does_not_wedge_future_refreshes(self) -> None:
        def broken(fn):
            raise RuntimeError("can't start new thread")
        runner = FakeRunner(vitals())
        rd = BryoPressureReader(("python", "-m", "bryo.status"), runner=runner,
                                clock=FakeClock(), spawn=broken)
        rd.peek()
        rd._spawn = lambda fn: fn()
        rd.peek()
        self.assertEqual(runner.calls, 1, "the refreshing flag must have been released")


# --- the executive consulting the body ------------------------------------------

class GpuAdapter:
    def __init__(self) -> None:
        self.calls = 0
        self._manifest = AgentManifest("melete", "test", (
            Capability("research.deep", "heavy", model=ModelRequirement("m", 4_500)),
            Capability("research.cpu", "light",
                       model=ModelRequirement("c", 0, requires_gpu=False)),
            Capability("research.none", "no model"),
        ))

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def execute(self, task: Task) -> TaskResult:
        self.calls += 1
        return TaskResult(task.task_id, "melete", {"ok": True})


STRESSED = PressureReading(True, 0.9, True, "stressed")
CALM = PressureReading(True, 0.2, False, "comfortable")


def executive(probe):
    sink = InMemoryAuditSink()
    ex = Executive(scheduler=offline_scheduler(), audit_sink=sink, pressure_probe=probe)
    adapter = GpuAdapter()
    ex.register(adapter)
    return ex, sink, adapter


def kinds(sink) -> list[str]:
    return [e.event_type for e in sink.events]


class ExecutiveBodyTests(unittest.TestCase):
    def test_deferrable_gpu_work_is_held_when_a_living_bryo_is_stressed(self) -> None:
        ex, sink, adapter = executive(lambda: STRESSED)
        with self.assertRaises(BodyDeferred):
            ex.execute(Task("research.deep", {}), deferrable=True)
        self.assertEqual(adapter.calls, 0, "the specialist must never be called")
        self.assertIn("task.deferred", kinds(sink))
        self.assertTrue(issubclass(BodyDeferred, ResourceUnavailable))

    def test_a_deferral_never_counts_against_the_specialist(self) -> None:
        ex, _sink, _adapter = executive(lambda: STRESSED)
        for _ in range(10):
            with self.assertRaises(BodyDeferred):
                ex.execute(Task("research.deep", {}), deferrable=True)
        self.assertEqual(ex.circuit("melete").snapshot().consecutive_failures, 0)

    def test_non_deferrable_work_is_never_held_but_the_advice_is_recorded(self) -> None:
        """The voice and every existing caller: nothing interactive is ever held."""
        ex, sink, adapter = executive(lambda: STRESSED)
        ex.execute(Task("research.deep", {}))
        self.assertEqual(adapter.calls, 1)
        paced = [e for e in sink.events if e.event_type == "task.paced"]
        self.assertEqual(len(paced), 1, "the body's advice must be visible in the ledger")
        self.assertIn("pressure=0.90", paced[0].detail)

    def test_calm_body_records_and_proceeds(self) -> None:
        ex, sink, adapter = executive(lambda: CALM)
        ex.execute(Task("research.deep", {}), deferrable=True)
        self.assertEqual(adapter.calls, 1)
        self.assertIn("task.paced", kinds(sink))
        self.assertNotIn("task.deferred", kinds(sink))

    def test_a_silent_or_dead_bryo_changes_nothing(self) -> None:
        for probe in (None, lambda: neutral("no reading yet"),
                      lambda: PressureReading(False, 0.95, True, "stale")):
            with self.subTest(probe=probe):
                ex, sink, adapter = executive(probe)
                ex.execute(Task("research.deep", {}), deferrable=True)
                self.assertEqual(adapter.calls, 1)
                self.assertNotIn("task.deferred", kinds(sink))
                self.assertNotIn("task.paced", kinds(sink))

    def test_a_broken_probe_changes_nothing(self) -> None:
        def boom():
            raise RuntimeError("reader exploded")
        ex, _sink, adapter = executive(boom)
        ex.execute(Task("research.deep", {}), deferrable=True)
        self.assertEqual(adapter.calls, 1)

    def test_any_model_backed_work_consults_the_body_model_less_does_not(self) -> None:
        # Fixed 2026-09-14 (audit item 4): a CPU model still loads and competes
        # for the machine, so it is consulted too; only a model-less capability
        # (a pure status read) is never paced.
        consulted = []
        ex, _sink, adapter = executive(lambda: consulted.append(1) or CALM)
        ex.execute(Task("research.cpu", {}), deferrable=True)
        self.assertEqual(consulted, [1], "CPU model work now consults the body")
        ex.execute(Task("research.none", {}), deferrable=True)
        self.assertEqual(consulted, [1], "model-less work still never waits on the body")
        self.assertEqual(adapter.calls, 2)

    def test_deferrable_cpu_model_work_is_held_when_stressed(self) -> None:
        ex, sink, adapter = executive(lambda: STRESSED)
        with self.assertRaises(BodyDeferred):
            ex.execute(Task("research.cpu", {}), deferrable=True)
        self.assertEqual(adapter.calls, 0, "a stressed body defers CPU model work too")
        self.assertIn("task.deferred", kinds(sink))


if __name__ == "__main__":
    unittest.main()


class SupervisionVisibilityTests(unittest.TestCase):
    """A dead supervisor must reach a human surface.

    Bryo's own beacon logged supervisor_missing 55 times across 2.3 days in September
    2026 and nothing surfaced it: the log was the only witness, and nobody reads a log.
    The reading therefore carries `supervised` so the dashboard can shout (HEAD 3.19 -
    test the alarm path; it is the thing least likely to have run).
    """

    def test_supervised_is_carried_through_for_a_living_organism(self) -> None:
        self.assertIs(parse_vitals(vitals(supervised=True)).supervised, True)
        self.assertIs(parse_vitals(vitals(supervised=False)).supervised, False)

    def test_unsupervised_is_reported_even_though_he_is_alive(self) -> None:
        r = parse_vitals(vitals(supervised=False))
        self.assertTrue(r.alive, "he is running; that is exactly why it matters")
        self.assertIs(r.supervised, False)

    def test_supervision_is_unknown_when_absent_or_unreadable(self) -> None:
        self.assertIsNone(parse_vitals(vitals()).supervised, "absent means unknown")
        self.assertIsNone(neutral("no reading yet").supervised)
        self.assertIsNone(parse_vitals("not json").supervised)

    def test_supervision_does_not_change_pacing(self) -> None:
        """It is an alarm for a human, not an input to scheduling: an unsupervised
        but comfortable organism must not start holding the spine's work back."""
        ex, sink, adapter = executive(lambda: PressureReading(True, 0.2, False, "ok", supervised=False))
        ex.execute(Task("research.deep", {}), deferrable=True)
        self.assertEqual(adapter.calls, 1)
        self.assertNotIn("task.deferred", kinds(sink))
