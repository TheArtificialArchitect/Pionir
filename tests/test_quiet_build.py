"""The self-improvement worker's build (``coding.daedalus_build_quiet``) and Moss.

Like a night build it may sideline Moss's model under the GPU lease - but only while she is
idle (no turn with Ian for 30 minutes, nobody typing), asked under the lease BEFORE anything is
sidelined; while she is active it is refused with the reason and nothing of hers is touched; and
the lease ends with the job, so her model comes back. Every daemon hook is a fake: nothing loads
or unloads a real model, nothing reaches a real Galatea.
"""
from __future__ import annotations

import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

from pionir.adapters.daedalus import BUILD, BUILD_QUIET, DaedalusAdapter
from pionir.adapters.galatea import moss_busy_probe
from pionir.contracts import AgentManifest, Task, TaskResult
from pionir.errors import AdapterUnavailable, ResourceUnavailable
from pionir.runtime import Executive
from pionir.scheduler import ModelLeaseScheduler, ResourceBudget
from pionir.shared_gpu import SharedGpuLock

CODER = "qwen3-coder:30b"
VOICE = "gemma3:12b"
NOW = datetime(2026, 10, 13, 5, 50)


class Card:
    SIZES = {VOICE: 7_800, CODER: 10_000}

    def __init__(self) -> None:
        self.resident = [VOICE]
        self.unloaded: list = []
        self.warmed: list = []

    def free(self) -> int:
        return 10_450 - sum(self.SIZES.get(m, 0) for m in self.resident)

    def loaded(self) -> list:
        return list(self.resident)

    def unload(self, name: str) -> None:
        self.unloaded.append(name)
        if name in self.resident:
            self.resident.remove(name)

    def warm(self, name: str) -> None:
        self.warmed.append(name)
        if name not in self.resident:
            self.resident.append(name)


class Transport:
    """Galatea's /api/state and /api/messages, as she answers them."""

    def __init__(self, *, last_turn_minutes_ago=None, typing=False, situation="",
                 error=None) -> None:
        self.messages = [] if last_turn_minutes_ago is None else [
            {"id": 7, "role": "ian", "text": "hi",
             "ts": (NOW - timedelta(minutes=last_turn_minutes_ago)).isoformat()}]
        self.typing = typing
        self.situation = situation
        self.error = error

    def get(self, path: str):
        if self.error is not None:
            raise self.error
        if path.startswith("/api/state"):
            return {"now": NOW.isoformat(), "typing": self.typing, "situation": self.situation}
        after = int(path.split("after=", 1)[1]) if "after=" in path else 0
        # like Galatea: ascending, at most PAGE rows after ``after``
        return {"messages": [m for m in self.messages if m["id"] > after][:self.PAGE]}

    PAGE = 2

    @staticmethod
    def history(minutes_ago: list) -> list:
        """Rows with ids 1.. whose times are ``minutes_ago`` (oldest first)."""
        return [{"id": i + 1, "role": "ian" if i % 2 == 0 else "her", "text": "x",
                 "ts": (NOW - timedelta(minutes=m)).isoformat()}
                for i, m in enumerate(minutes_ago)]


class Moss:
    def __init__(self, transport) -> None:
        self._transport = transport


class Quiet:
    """The quiet build's capability and settle, from the real Daedalus adapter; the job itself
    only records whether the lease was held while it ran."""

    def __init__(self, daedalus: DaedalusAdapter, scheduler: ModelLeaseScheduler) -> None:
        self.daedalus = daedalus
        self.scheduler = scheduler
        caps = tuple(c for c in daedalus.manifest.capabilities if c.name in (BUILD, BUILD_QUIET))
        self._manifest = AgentManifest(agent_id="daedalus", version="test", capabilities=caps)
        self.ran_under_lease: list = []

    @property
    def manifest(self):
        return self._manifest

    def settle_for(self, task):
        return self.daedalus.settle_for(task)

    def execute(self, task):
        self.ran_under_lease.append(bool(self.scheduler._active))
        return TaskResult(task_id=task.task_id, agent_id="daedalus", output={"ok": True})


class MossProbeTests(unittest.TestCase):
    def probe(self, **kw):
        return moss_busy_probe(Moss(Transport(**kw)))()

    def test_quiet_for_half_an_hour_is_idle(self) -> None:
        self.assertIsNone(self.probe(last_turn_minutes_ago=40))
        self.assertIsNone(self.probe())                         # never talked

    def test_a_recent_turn_or_typing_is_not(self) -> None:
        self.assertIn("5 minute(s) ago", self.probe(last_turn_minutes_ago=5))
        self.assertIn("typing", self.probe(last_turn_minutes_ago=90, typing=True))
        self.assertIn("mid-conversation", self.probe(situation="night · you're mid-conversation"))

    def test_unreadable_is_not_idle(self) -> None:
        self.assertIn("could not be read", self.probe(error=AdapterUnavailable("401")))

    def test_her_last_turn_is_the_newest_row_not_the_first_page(self) -> None:
        transport = Transport()
        transport.messages = Transport.history([900, 800, 700, 600, 2])   # 3 pages
        self.assertIn("2 minute(s) ago", moss_busy_probe(Moss(transport))())

    def test_ids_going_backwards_are_not_idle_until_read_again(self) -> None:
        transport = Transport()
        transport.messages = Transport.history([900, 800, 700, 600, 500])
        probe = moss_busy_probe(Moss(transport))
        self.assertIsNone(probe())                                # quiet for hours
        transport.messages = Transport.history([1])              # her store was replaced
        self.assertIn("went backwards", probe())
        self.assertIn("1 minute(s) ago", probe())                # read again from the start

    def test_not_running_only_when_refused_twice_a_minute_apart(self) -> None:
        refused = AdapterUnavailable("down")
        refused.__cause__ = urllib.error.URLError(ConnectionRefusedError())
        clock = [1000.0]
        transport = Transport(error=refused)
        probe = moss_busy_probe(Moss(transport), clock=lambda: clock[0])
        self.assertIn("not answering", probe())                  # a restart blip?
        clock[0] += 30
        self.assertIn("not answering", probe())
        clock[0] += 31
        self.assertIsNone(probe())                               # down for over a minute
        transport.error = None                                    # she answers again...
        transport.messages = Transport.history([3])
        self.assertIn("3 minute(s) ago", probe())
        transport.error = refused                                 # ...so a new refusal starts over
        self.assertIn("not answering", probe())

    def test_unwired_is_never_idle(self) -> None:
        settle = DaedalusAdapter().settle_for(Task(BUILD_QUIET, {}))
        with self.assertRaises(ResourceUnavailable):
            settle()


class SidelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.card = Card()
        self.scheduler = ModelLeaseScheduler(
            ResourceBudget(), SharedGpuLock(Path(self._t.name) / "gpu.lock"),
            vram_probe=self.card.free, residency_probe=lambda m: False, evict_to_fit=True,
            evictor=self.card.unload, loaded_probe=self.card.loaded, protected_models=[VOICE],
            rewarmer=self.card.warm, background=lambda work: work(), sleep=lambda s: None)
        self.daedalus = DaedalusAdapter()
        self.adapter = Quiet(self.daedalus, self.scheduler)
        self.executive = Executive(scheduler=self.scheduler)
        self.executive.register(self.adapter)

    def run_quiet(self):
        return self.executive.execute(Task(BUILD_QUIET, {}, frozenset({"daedalus.build_sandbox"})))

    def test_idle_moss_is_sidelined_and_the_job_runs_and_she_comes_back(self) -> None:
        self.daedalus.moss_busy = moss_busy_probe(Moss(Transport(last_turn_minutes_ago=45)))
        self.run_quiet()
        self.assertEqual(self.adapter.ran_under_lease, [True])
        self.assertEqual(self.card.unloaded[0], VOICE)           # sidelined, under the lease
        self.assertEqual(self.scheduler._active, {})             # the lease ended with the job
        self.assertIn(VOICE, self.card.warmed)                   # and her model came back

    def test_active_moss_is_never_sidelined_and_the_reason_is_said(self) -> None:
        self.daedalus.moss_busy = moss_busy_probe(Moss(Transport(last_turn_minutes_ago=3)))
        with self.assertRaises(ResourceUnavailable) as caught:
            self.run_quiet()
        self.assertIn("Moss talked with Ian 3 minute(s) ago", str(caught.exception))
        self.assertEqual(self.card.unloaded, [])
        self.assertEqual(self.adapter.ran_under_lease, [])
        self.assertEqual(self.scheduler._active, {})
        self.assertIsNotNone(SharedGpuLock(Path(self._t.name) / "gpu.lock").try_acquire(
            owner="test", purpose="the lease was let go"))

    def test_the_night_build_is_unchanged(self) -> None:
        self.daedalus.moss_busy = lambda: "busy"
        self.assertIsNone(self.daedalus.settle_for(Task(BUILD, {})))


if __name__ == "__main__":
    unittest.main()
