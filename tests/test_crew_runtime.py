"""The crew's runtime rules: pause holds the brain, stale requests expire out loud, and
``python -m pionir.crew`` starts, says what it started, and stops cleanly.

Each test fails if the rule is reverted: a model call made while the crew is paused, a
stale request served late or dropped without a word, an agent left believing a thought
it never had is still on its way, or a foreground run that does not stop cleanly.
"""

import threading
import unittest

from crew_support import FakeOllama, settings, temp_dir
from test_crew_kit import Crew, FakePionir, member

from pionir.crew.__main__ import run
from pionir.crew.brain import EXPIRED
from pionir.crew.crew import build
from pionir.crew.gpu import CardWatch


class PauseHoldsTheBrainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.crew = Crew([member("ada", ["outreach"])])
        self.addCleanup(self.crew.close)
        self.brain = self.crew.sim.brain
        self.got: list = []

    def ask(self) -> None:
        self.brain.request("ada", "thought", "system", "user", {},
                           lambda text, meta, err: self.got.append((text, meta, err)))

    def test_while_paused_no_model_call_is_made_and_the_work_waits(self) -> None:
        self.ask()
        self.crew.sim.pause("operator")
        for _ in range(3):
            self.assertTrue(self.brain.step())
        self.assertEqual(self.crew.ollama.calls, [])
        self.assertEqual(len(self.brain.waiting()), 1)        # still queued, not dropped
        self.assertEqual(self.got, [])
        self.assertGreaterEqual(self.brain.held_for_pause, 3)
        self.assertTrue(self.brain.snapshot()["held"])
        self.crew.sim.resume()
        self.brain.step()
        self.assertEqual(len(self.crew.ollama.calls), 1)
        self.assertEqual(self.got[0][0], "hello")

    def test_a_paused_crew_starts_no_job_either(self) -> None:
        crew = Crew([member("ada", ["outreach"])], answers={"x.y": {"ok": True}})
        self.addCleanup(crew.close)
        crew.sim.hands.submit("ada", _job(), lambda out: None)
        crew.sim.pause("operator")
        crew.sim.hands.step()
        self.assertEqual(crew.pionir.calls, [])
        crew.sim.resume()
        crew.sim.hands.step()
        self.assertEqual(len(crew.pionir.calls), 1)


def _job():
    from pionir.crew.actions import Job
    return Job("x.y", {}, what="do the thing")


class ExpiryTests(unittest.TestCase):
    def test_a_stale_request_expires_with_the_explicit_signal_and_is_counted(self) -> None:
        crew = Crew([member("ada", ["outreach"])])
        self.addCleanup(crew.close)
        brain = crew.sim.brain
        self.assertEqual(brain.ttl, 600.0)                    # the default
        got: list = []
        brain.request("ada", "thought", "s", "u", {}, lambda t, m, e: got.append((t, m, e)))
        crew.time.advance(599)
        self.assertEqual(brain.expire_stale(), 0)             # not yet
        crew.time.advance(2)
        brain.step()
        self.assertEqual(brain.expired, 1)
        self.assertEqual(crew.ollama.calls, [])               # never served late
        text, meta, err = got[0]
        self.assertIsNone(text)
        self.assertEqual(err, EXPIRED)
        self.assertTrue(meta["expired"])
        self.assertEqual(brain.waiting(), [])
        self.assertEqual(crew.sim.store.calls_since(0)["ada"]["calls"], 1)   # in the ledger

    def test_the_ttl_is_configurable(self) -> None:
        crew = Crew([member("ada", ["outreach"])], request_ttl_seconds=5)
        self.addCleanup(crew.close)
        self.assertEqual(crew.sim.brain.ttl, 5.0)

    def test_an_expired_intention_tells_the_agent_it_did_not_happen(self) -> None:
        crew = Crew([member("ada", ["outreach"])])
        self.addCleanup(crew.close)
        ada = crew["ada"]
        ada.intend_pending = True
        crew.sim.brain.request("ada", "intend", "s", "u", {},
                               lambda t, m, e: ada._on_intend(crew.sim, 1, t, e))
        crew.time.advance(601)
        crew.sim.brain.step()
        self.assertFalse(ada.intend_pending)                  # free to form one again
        self.assertEqual(ada.mem.counter("intend_no_words"), 1)
        self.assertEqual(ada.mem.counter("intentions_formed"), 0)

    def test_an_expired_line_closes_the_conversation_and_says_why(self) -> None:
        crew = Crew([member("ada", ["outreach"]), member("bram", ["outreach"])])
        self.addCleanup(crew.close)
        talk = crew.sim.talk
        talk.start(crew["ada"], crew["bram"], "test", "Speak to Bram.")
        crew.time.advance(601)
        crew.sim.brain.step()
        self.assertEqual(talk.active, {})
        self.assertIsNone(crew["ada"].conversation)
        self.assertEqual(crew.sim.store.count_utterances(), 0)


class ForegroundRunTests(unittest.TestCase):
    def test_run_starts_says_what_it_started_and_stops_cleanly(self) -> None:
        with temp_dir() as root:
            cfg = settings(root, tick_seconds=0.05)
            sim = build(cfg, cast=[member("ada", ["outreach"]), member("bram", ["ops"])],
                        post=FakeOllama(), client=FakePionir(),
                        card=CardWatch(cfg.gpu_lock_path, probe=lambda: None,
                                       poll_seconds=0.01))
            stop = threading.Event()
            lines: list = []
            timer = threading.Timer(0.4, stop.set)
            timer.start()
            try:
                self.assertEqual(run(sim, stop=stop, out=lines.append, monitor=False), 0)
            finally:
                timer.cancel()
            text = "\n".join(lines)
            self.assertIn("Ada", text)
            self.assertIn("#outreach: Ada", text)
            self.assertIn("NO project kinds", text)            # an idle crew says so up front
            self.assertIn("stopped cleanly", lines[-1])
            self.assertTrue(sim.stopping)
            self.assertGreater(sim.ticks, 0)
            self.assertFalse(sim._thread.is_alive())
            self.assertFalse(sim.brain._thread.is_alive())
            self.assertFalse(sim.hands._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
