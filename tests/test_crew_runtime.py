"""The crew's runtime rules: pause holds the brain, the hands and the dispatcher; stale
model requests expire out loud; ``python -m pionir.crew`` starts, says which workers are
live and which are placeholders, and stops cleanly.

Each test fails if the rule is reverted: a model call, a job or a worker run made while
the crew is paused, a stale request served late or dropped without a word, or a
foreground run that does not stop cleanly.
"""

import threading
import unittest

from crew_support import FakeTime, temp_dir
from test_crew_fakes import FakeHttp, FakePionir, catalogue, done, make_crew

from pionir.crew.__main__ import run
from pionir.crew.brain import EXPIRED
from pionir.crew.hands import Job
from pionir.crew.log import check_source
from pionir.crew.registry import default_registry
from pionir.crew.workers import CONTROL_URL


class _Case(unittest.TestCase):
    def crew(self, **kw):
        tmp = temp_dir()
        self.addCleanup(tmp.cleanup)
        crew = make_crew(tmp.name, **kw)
        self.addCleanup(crew.stop)
        return crew


class PauseHoldsTests(_Case):
    def test_while_paused_no_model_call_is_made_and_the_work_waits(self) -> None:
        crew = self.crew()
        got: list = []
        crew.brain.request("leader.alpha", "distil", "s", "u", {},
                           lambda text, meta, err: got.append(text))
        crew.pause("operator")
        for _ in range(3):
            self.assertTrue(crew.brain.step())
        self.assertEqual(crew.brain._post.calls, [])
        self.assertEqual(len(crew.brain.waiting()), 1)         # still queued, not dropped
        self.assertTrue(crew.brain.snapshot()["held"])
        crew.resume()
        crew.brain.step()
        self.assertEqual(got, ["hello"])

    def test_a_paused_crew_starts_no_job_either(self) -> None:
        crew = self.crew()
        crew.hands.client = FakePionir({"x.y": done({})})
        crew.hands.submit("alpha.a1", Job("x.y", {}), lambda out: None)
        crew.pause("operator")
        crew.hands.step()
        self.assertEqual(crew.hands.client.calls, [])
        crew.resume()
        crew.hands.step()
        self.assertEqual(len(crew.hands.client.calls), 1)

    def test_a_paused_crew_dispatches_no_worker(self) -> None:
        crew = self.crew()
        crew.pause("operator")
        crew.step()
        crew.dispatcher.shutdown(wait=True)
        self.assertEqual(crew.store.last_attempts(), {})
        crew.resume()
        crew.dispatcher = type(crew.dispatcher)(crew.registry, crew.store,
                                                context=crew.context_for, gate=crew.gate)
        crew.step()
        crew.dispatcher.shutdown(wait=True)
        self.assertIn("alpha.a1", crew.store.last_attempts())   # and resumes after


class ExpiryTests(_Case):
    def test_a_stale_request_expires_with_the_explicit_signal_and_is_counted(self) -> None:
        t = FakeTime()
        crew = self.crew(now=t.now)
        brain = crew.brain
        self.assertEqual(brain.ttl, 600.0)                     # the default
        got: list = []
        brain.request("leader.alpha", "distil", "s", "u", {},
                      lambda text, meta, err: got.append((text, meta, err)),
                      division="alpha")
        t.advance(599)
        self.assertEqual(brain.expire_stale(), 0)              # not yet
        t.advance(2)
        brain.step()
        self.assertEqual(brain.expired, 1)
        self.assertEqual(brain._post.calls, [])                # never served late
        text, meta, err = got[0]
        self.assertIsNone(text)
        self.assertEqual(err, EXPIRED)
        self.assertTrue(meta["expired"])
        self.assertEqual(crew.store.calls_since(0)["leader.alpha"]["calls"], 1)  # in the ledger

    def test_the_ttl_is_configurable(self) -> None:
        crew = self.crew(request_ttl_seconds=5)
        self.assertEqual(crew.brain.ttl, 5.0)

    def test_ask_returns_at_once_when_the_brain_is_stopped(self) -> None:
        crew = self.crew()
        crew.brain.stop()
        text, _meta, err = crew.brain.ask("leader.alpha", "distil", [], {}, division="alpha")
        self.assertIsNone(text)
        self.assertIn("stopped", err)


class ForegroundRunTests(unittest.TestCase):
    def test_run_says_what_is_live_and_what_is_placeholder_and_stops_cleanly(self) -> None:
        with temp_dir() as root:
            http = FakeHttp()                  # every URL unreachable: nothing leaves the box
            crew = make_crew(root, registry=default_registry(), http=http, tick_seconds=0.05)
            stop = threading.Event()
            lines: list = []
            timer = threading.Timer(0.5, stop.set)
            timer.start()
            try:
                self.assertEqual(run(crew, stop=stop, out=lines.append, monitor=False), 0)
            finally:
                timer.cancel()
            text = "\n".join(lines)
            self.assertIn("treasury.ledger", text)
            self.assertIn("LIVE but NOT CONFIGURED", text)        # no token file in the temp dir
            self.assertIn("posting.blog", text)
            self.assertIn("PLACEHOLDER", text)
            self.assertIn("8 live, 3 placeholder(s)", text)
            self.assertIn("stopped cleanly", lines[-1])
            self.assertTrue(crew.stopping)
            self.assertGreater(crew.steps, 0)
            self.assertFalse(crew._thread.is_alive())
            self.assertFalse(crew.brain._thread.is_alive())
            self.assertFalse(crew.hands._thread.is_alive())
            urls = {u for u, _h, _t in http.calls}
            # Strict on purpose - no surprise network calls. The ledger is never called (no
            # token). Health got no answer here, so it asked the control site before daring to
            # call the site down; that second call is the one it is supposed to make.
            self.assertEqual(urls, {"https://api.dokaz.net/health", CONTROL_URL})

    def test_a_restart_records_the_gap_it_was_not_running(self) -> None:
        with temp_dir() as root:
            t = FakeTime()
            first = make_crew(root, cat=catalogue(), now=t.now)
            first.stop()
            t.advance(3600)
            second = make_crew(root, cat=catalogue(), now=t.now)
            try:
                pause = second.store.last_pause()
                self.assertEqual(pause["reason"], "process was not running")
                self.assertAlmostEqual(pause["seconds"], 3600, delta=1)
            finally:
                second.stop()


class SourceTests(unittest.TestCase):
    def test_no_mangled_escapes_in_the_crew_source(self) -> None:
        # a regex word boundary arriving as a literal 0x08 matches nothing, silently
        self.assertEqual(check_source(), [])


if __name__ == "__main__":
    unittest.main()
