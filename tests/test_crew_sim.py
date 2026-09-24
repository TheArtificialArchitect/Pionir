"""The crew's tick loop on the wall clock, and its honesty about time not worked.

A fake hand drives both clocks, so nothing here sleeps on the tick. Each test
fails if the behaviour it names is reverted: game time creeping back (a tick
advancing a clock of its own), missed ticks replayed in a burst, a stall or an
operator pause worked through or left unrecorded, or a restart pretending the
gap never happened.
"""

import time
import unittest

from crew_support import FakeTime, settings, temp_dir

from pionir.crew.clock import WallClock
from pionir.crew.log import check_source
from pionir.crew.sim import FIRST_CHECKPOINT_SECONDS, Sim


class _Agent:
    def __init__(self, aid: str) -> None:
        self.id = aid
        self.acted: list = []
        self.perceived = 0
        self.checkpoints = 0
        self.closed = False

    def act(self, sim) -> None:
        self.acted.append((sim.clock.t, sim.dt))

    def perceive(self, sim) -> None:
        self.perceived += 1

    def checkpoint(self) -> None:
        self.checkpoints += 1

    def close(self) -> None:
        self.closed = True


class _SimCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = temp_dir()
        self.addCleanup(self._tmp.cleanup)
        self.clock = FakeTime()
        self.cfg = settings(self._tmp.name)
        self.sim = self._sim()
        self.addCleanup(self.sim.stop)
        self.agent = _Agent("ada")
        self.sim.add_agent(self.agent)
        self.sim.step()                               # the first look only starts the cadence

    def _sim(self) -> Sim:
        return Sim(self.cfg, clock=WallClock(self.clock.now), monotonic=self.clock.monotonic)

    def advance(self, seconds: float) -> int:
        self.clock.advance(seconds)
        return self.sim.step()


class WallClockTickTests(_SimCase):
    def test_a_tick_is_one_real_second_on_the_wall_clock(self) -> None:
        self.assertEqual(self.advance(0.5), 0)        # not due yet
        self.assertEqual(self.advance(0.5), 1)
        self.assertEqual(self.agent.acted[-1][0], int(self.clock.wall))
        self.assertEqual(self.advance(1.0), 1)
        self.assertAlmostEqual(self.agent.acted[-1][1], 1.0)   # dt is real seconds
        self.assertEqual(self.sim.ticks, 2)
        self.assertEqual(self.agent.perceived, 2)

    def test_the_clock_is_read_not_ticked(self) -> None:
        before = self.sim.clock.t
        self.sim.tick()
        self.sim.tick()
        self.assertEqual(self.sim.clock.t, before)    # ticking does not move time
        self.clock.advance(3600)
        self.assertEqual(self.sim.clock.t, before + 3600)

    def test_lag_is_skipped_not_replayed(self) -> None:
        self.assertEqual(self.advance(4.2), 1)        # four due: one runs, three skipped
        self.assertEqual(len(self.agent.acted), 1)
        self.assertEqual(self.sim.skipped_ticks, 3)
        self.assertEqual(self.advance(0.9), 1)        # the 0.2 remainder carried: cadence kept

    def test_the_first_checkpoint_comes_a_minute_in_on_the_wall_clock(self) -> None:
        for _ in range(FIRST_CHECKPOINT_SECONDS - 1):
            self.advance(1.0)
        self.assertEqual(self.sim.checkpoints, 0)
        self.advance(1.0)
        self.assertEqual(self.sim.checkpoints, 1)
        self.assertEqual(self.agent.checkpoints, 1)


class HonestPauseTests(_SimCase):
    def test_a_stall_is_recorded_as_a_pause_and_not_worked(self) -> None:
        self.assertEqual(self.advance(30.0), 0)
        self.assertEqual(self.agent.acted, [])
        pause = self.sim.last_pause
        self.assertEqual(pause["reason"], "process was suspended")
        self.assertAlmostEqual(pause["seconds"], 30.0)
        self.assertAlmostEqual(self.sim.paused_seconds_total, 30.0)
        self.assertEqual(self.advance(1.0), 1)        # and then on as normal

    def test_an_operator_pause_stops_ticks_and_is_recorded_when_it_ends(self) -> None:
        self.advance(1.0)
        self.sim.pause("Ian is on a call")
        for _ in range(45):
            self.assertEqual(self.advance(1.0), 0)
        self.assertEqual(len(self.agent.acted), 1)
        self.assertEqual(self.sim.snapshot()["paused"], "Ian is on a call")
        self.sim.resume()
        pause = self.sim.last_pause
        self.assertEqual(pause["reason"], "Ian is on a call")
        self.assertAlmostEqual(pause["seconds"], 45.0)
        # no catch-up after it, and no second "suspended" pause for the same stretch
        self.assertEqual(self.advance(0.5), 0)
        self.assertEqual(self.advance(0.5), 1)
        self.assertEqual(len(self.agent.acted), 2)
        self.assertEqual(len(self.sim.store.pauses()), 1)

    def test_a_restart_records_the_time_the_process_was_not_running(self) -> None:
        born = self.sim.born_real
        self.sim.stop()
        self.assertTrue(self.agent.closed)
        self.clock.advance(600)
        self.sim = self._sim()
        self.addCleanup(self.sim.stop)
        pause = self.sim.last_pause
        self.assertEqual(pause["reason"], "process was not running")
        self.assertAlmostEqual(pause["seconds"], 600.0)
        self.assertEqual(self.sim.born_real, born)    # the crew's age survives a restart
        self.assertAlmostEqual(self.sim.age_seconds(), 600.0)


class ThreadTests(unittest.TestCase):
    def test_nothing_ticks_until_started_and_it_ticks_once_started(self) -> None:
        with temp_dir() as root:
            sim = Sim(settings(root, tick_seconds=0.02))
            agent = _Agent("ada")
            sim.add_agent(agent)
            time.sleep(0.1)
            self.assertEqual(agent.acted, [])         # nothing starts itself
            sim.start()
            deadline = time.monotonic() + 5
            while len(agent.acted) < 3 and time.monotonic() < deadline:
                time.sleep(0.02)
            sim.stop()
            self.assertGreaterEqual(len(agent.acted), 3)


class SourceTests(unittest.TestCase):
    def test_the_crew_source_has_no_mangled_escapes(self) -> None:
        self.assertEqual(check_source(), [])


if __name__ == "__main__":
    unittest.main()
