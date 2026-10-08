"""How long the crew was down is measured, not guessed (2026-10-07 outage review).

The live ``pauses`` table held 43 "process was not running" rows, 13.45 h in all. Both
launchers stop the crew with a hard kill, so every gap was counted from the last 5-minute
checkpoint (up to 5 minutes early), and an instance that died before its first checkpoint
made the next start record the SAME gap a second time (four pairs shared one stopped_real).
Each test fails if its fix is reverted. Temp dirs and a fake clock only.
"""
from __future__ import annotations

import threading
import unittest

from crew_support import FakeTime, temp_dir
from test_crew_fakes import catalogue, make_crew

from pionir.crew import __main__ as crew_main


def _kill(crew) -> None:
    """A hard kill: no stop(), so no final checkpoint - only what is already on disk."""
    crew.store.close()


class HeartbeatTests(unittest.TestCase):
    def test_a_hard_kill_is_measured_from_the_last_heartbeat_not_the_last_checkpoint(
            self) -> None:
        with temp_dir() as root:
            t = FakeTime()
            first = make_crew(root, cat=catalogue(), now=t.now)
            t.advance(70)
            first._beat(t.now())               # the first checkpoint (at +60 s)
            t.advance(180)
            first._beat(t.now())               # +250 s: a heartbeat, no checkpoint yet
            _kill(first)
            t.advance(600)                     # killed at +250, back at +850
            second = make_crew(root, cat=catalogue(), now=t.now)
            try:
                self.assertAlmostEqual(second.store.last_pause()["seconds"], 600, delta=1)
            finally:
                second.stop()

    def test_a_gap_is_recorded_once_even_if_the_next_instance_dies_young(self) -> None:
        with temp_dir() as root:
            t = FakeTime()
            start = t.now()
            a = make_crew(root, cat=catalogue(), now=t.now)
            t.advance(70)
            a._beat(t.now())                   # checkpoint at start+70
            _kill(a)
            t.advance(930)                     # back at start+1000 ...
            b = make_crew(root, cat=catalogue(), now=t.now)
            t.advance(20)
            _kill(b)                           # ... and killed again 20 s later, unsaved
            t.advance(980)                     # back at start+2000
            c = make_crew(root, cat=catalogue(), now=t.now)
            try:
                last = c.store.last_pause()
                self.assertAlmostEqual(last["stopped_real"], start + 1000, delta=1)
                # 930 s + 1000 s, not 930 s + 1930 s
                self.assertAlmostEqual(c.paused_seconds_total, 1930, delta=1)
            finally:
                c.stop()


class StartFailureTests(unittest.TestCase):
    def test_a_start_that_fails_still_stops_cleanly(self) -> None:
        with temp_dir() as root:
            crew = make_crew(root, cat=catalogue(), tick_seconds=0.05)

            def refuse():
                raise OSError("[WinError 10048] the crew API port is taken")
            crew.start = refuse
            with self.assertRaises(OSError):
                crew_main.run(crew, stop=threading.Event(), out=lambda _l: None,
                              monitor=False)
            self.assertTrue(crew.stopping)     # stop() ran: its final checkpoint was taken


if __name__ == "__main__":
    unittest.main()
