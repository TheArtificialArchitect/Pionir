"""The crew's one brain: its queue, its ceiling, and how it yields the card.

Every model is a fake - nothing here calls Ollama. Each test fails if the
behaviour it names is reverted: a thought served ahead of an owed reply, the
ceiling letting a call through, a callback that runs outside the crew's lock,
and above all the brain calling the model while someone else holds Pionir's
GPU lock, latching after the lock clears, or taking the lock itself (which
would make Moss stand down on every crew call).
"""

import threading
import time
import unittest
from pathlib import Path

from crew_support import FakeOllama, ScriptedProbe, settings, temp_dir

from pionir.config import PionirSettings
from pionir.crew import log as crewlog
from pionir.crew.brain import BACKGROUND, REPLY, Brain, OllamaError
from pionir.crew.budget import CardBusy, warm_brain
from pionir.crew.config import CrewSettings
from pionir.crew.gpu import CardWatch, who
from pionir.crew.sim import Sim
from pionir.shared_gpu import SharedGpuLock


class _Crew:
    """A real, unstarted Sim in a temp dir, with a brain on a fake model."""

    def __init__(self, root, *, probe=None, post=None, **cfg) -> None:
        self.cfg = settings(root, **cfg)
        self.sim = Sim(self.cfg)
        self.post = post or FakeOllama()
        card = CardWatch(self.cfg.gpu_lock_path, probe=probe,
                         poll_seconds=self.cfg.gpu_poll_seconds)
        self.brain = Brain(self.cfg, self.sim, card=card, post=self.post)
        self.sim.brain = self.brain
        self.got: list = []

    def ask(self, agent: str, purpose: str, priority: int = BACKGROUND):
        return self.brain.request(agent, purpose, "system", f"{agent}:{purpose}", {},
                                  lambda text, meta, err: self.got.append((agent, text, err)),
                                  priority=priority)

    def close(self) -> None:
        self.sim.stop()


class QueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = temp_dir()
        self.crew = _Crew(self._tmp.name, probe=lambda: None)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.crew.close)

    def test_an_owed_reply_goes_ahead_of_older_thoughts(self) -> None:
        self.crew.ask("a", "thought")
        self.crew.ask("b", "thought")
        self.crew.ask("c", "reply", priority=REPLY)
        for _ in range(3):
            self.crew.brain.step()
        self.assertEqual([g[0] for g in self.crew.got], ["c", "a", "b"])

    def test_the_hourly_ceiling_refuses_and_counts(self) -> None:
        self.crew.close()
        self.crew = _Crew(self._tmp.name + "/b", probe=lambda: None, calls_per_hour=2)
        for _ in range(2):
            self.crew.sim.store.add_call(self.crew.sim.clock.t, "x", "thought", 1, 1, 0.1, True)
        self.assertIsNone(self.crew.ask("a", "thought"))
        self.assertEqual(self.crew.brain.throttled, 1)
        self.assertEqual(self.crew.brain.waiting(), [])

    def test_below_the_ceiling_a_call_is_queued(self) -> None:
        self.assertIsNotNone(self.crew.ask("a", "thought"))
        self.assertEqual(len(self.crew.brain.waiting()), 1)

    def test_the_callback_gets_the_words_and_runs_under_the_crew_lock(self) -> None:
        held_elsewhere: list = []

        def callback(text, meta, err):
            # another thread cannot take the crew's lock while the callback runs
            def try_lock():
                got = self.crew.sim.lock.acquire(blocking=False)
                held_elsewhere.append(not got)
                if got:
                    self.crew.sim.lock.release()

            probe = threading.Thread(target=try_lock)
            probe.start()
            probe.join()
            self.crew.got.append(text)

        self.crew.brain.request("a", "thought", "s", "u", {"temperature": 0.2}, callback)
        self.crew.brain.step()
        self.assertEqual(self.crew.got, ["hello"])
        self.assertEqual(held_elsewhere, [True])
        _, body = self.crew.post.calls[0]
        self.assertEqual(body["model"], "gemma3:12b")
        self.assertEqual(body["options"], {"num_ctx": 8192, "temperature": 0.2})
        self.assertEqual(self.crew.sim.store.calls_last_hour(), 1)

    def test_a_failed_call_still_reaches_the_callback_and_is_counted(self) -> None:
        self.crew.post.error = OllamaError("connection refused")
        self.crew.ask("a", "thought")
        self.crew.brain.step()
        self.assertEqual(self.crew.got, [("a", None, "connection refused")])
        self.assertEqual(self.crew.brain.failures, 1)

    def test_a_callback_that_raises_is_a_lesion_not_a_dead_worker(self) -> None:
        before = crewlog.lesions["brain.callback.boom"]
        self.crew.brain.request("a", "boom", "s", "u", {}, lambda *_: 1 / 0)
        self.crew.ask("b", "thought")
        self.crew.brain.step()
        self.crew.brain.step()
        self.assertEqual(crewlog.lesions["brain.callback.boom"], before + 1)
        self.assertEqual([g[0] for g in self.crew.got], ["b"])


class GpuYieldTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = temp_dir()
        self.addCleanup(self._tmp.cleanup)

    def _crew(self, **kw) -> _Crew:
        crew = _Crew(self._tmp.name, **kw)
        self.addCleanup(crew.close)
        return crew

    def test_while_the_lock_is_held_no_model_call_is_made_and_the_yield_is_counted(self) -> None:
        seen: list = []
        crew: _Crew

        def on_probe(n):
            if crew.brain.card.yields:   # standing down: the model must not have been called
                seen.append((len(crew.post.calls), crew.sim.store.calls_last_hour()))

        probe = ScriptedProbe(held_for=5, on_probe=on_probe)
        crew = self._crew(probe=probe)
        crew.ask("a", "thought")
        crew.brain.step()
        self.assertEqual(probe.probes, 6)                    # five held, then free
        self.assertTrue(seen)
        self.assertTrue(all(calls == 0 and ledger == 0 for calls, ledger in seen))
        self.assertEqual(crew.brain.card.yields, 1)          # one spell, not one per poll
        self.assertEqual(len(crew.post.calls), 1)            # and then it went ahead

    def test_a_free_card_proceeds_at_once_with_no_yield(self) -> None:
        probe = ScriptedProbe(held_for=0)
        crew = self._crew(probe=probe)
        crew.ask("a", "thought")
        crew.brain.step()
        self.assertEqual(probe.probes, 1)
        self.assertEqual(crew.brain.card.yields, 0)
        self.assertEqual(len(crew.post.calls), 1)

    def test_when_the_lock_clears_it_resumes_with_no_latch(self) -> None:
        # held, free, held, free: each spell ends the moment the lock does
        states = iter([{"purpose": "daedalus: x"}, None, {"purpose": "daedalus: x"}, None, None])
        crew = self._crew(probe=lambda: next(states))
        crew.ask("a", "one")
        crew.brain.step()
        crew.ask("a", "two")
        crew.brain.step()
        crew.ask("a", "three")
        crew.brain.step()                                    # free straight away: no leftover pause
        self.assertEqual([g[0] for g in crew.got], ["a", "a", "a"])
        self.assertEqual(crew.brain.card.yields, 2)
        self.assertIsNone(crew.brain.card.yielding_to)

    def test_stopping_while_standing_down_never_calls_the_model(self) -> None:
        crew = self._crew(probe=lambda: {"purpose": "daedalus: qwen3-coder:30b"})
        crew.ask("a", "thought")
        result: list = []
        worker = threading.Thread(target=lambda: result.append(crew.brain.step()))
        worker.start()
        deadline = time.monotonic() + 5
        while crew.brain.card.yielding_to is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(who(crew.brain.card.yielding_to), "Daedalus")
        crew.brain._stop.set()
        worker.join(timeout=5)
        self.assertEqual(result, [False])
        self.assertEqual(crew.post.calls, [])
        self.assertEqual(len(crew.brain.waiting()), 1)       # still queued, not dropped

    def test_a_reply_that_arrives_during_the_yield_still_goes_first(self) -> None:
        crew: _Crew

        def on_probe(n):
            if n == 2:
                crew.ask("urgent", "reply", priority=REPLY)

        crew = self._crew(probe=ScriptedProbe(held_for=3, on_probe=on_probe))
        crew.ask("idle", "thought")
        crew.brain.step()
        self.assertEqual(crew.got[0][0], "urgent")

    def test_a_probe_that_breaks_is_counted_and_does_not_pause_for_ever(self) -> None:
        def broken():
            raise OSError("disk gone")

        crew = self._crew(probe=broken)
        crew.ask("a", "thought")
        crew.brain.step()
        self.assertEqual(crew.brain.card.probe_errors, 1)
        self.assertEqual(len(crew.post.calls), 1)


class RealLockTests(unittest.TestCase):
    """The default probe against Pionir's real lock, taken in this very process -
    which is where the crew may live, beside the scheduler."""

    def setUp(self) -> None:
        self._tmp = temp_dir()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = settings(self._tmp.name, gpu_poll_seconds=0.02)
        self.lock = SharedGpuLock(self.cfg.gpu_lock_path)

    def test_the_probe_sees_a_lease_held_in_this_process_and_names_the_holder(self) -> None:
        card = CardWatch(self.cfg.gpu_lock_path)
        self.assertIsNone(card.holder())                     # no file at all: free
        lease = self.lock.try_acquire(owner="pionir", purpose="daedalus: qwen3-coder:30b")
        self.assertIsNotNone(lease)
        try:
            holder = card.holder()
            self.assertIsNotNone(holder)
            self.assertEqual(who(holder), "Daedalus")
        finally:
            lease.release()
        self.assertIsNone(card.holder())                     # released: free, no latch

    def test_the_brain_waits_out_a_real_lease_then_calls(self) -> None:
        sim = Sim(self.cfg)
        post = FakeOllama()
        brain = Brain(self.cfg, sim, post=post)              # the default, real probe
        got: list = []
        brain.request("a", "thought", "s", "u", {}, lambda text, *_: got.append(text))
        lease = self.lock.try_acquire(owner="pionir", purpose="daedalus: qwen3-coder:30b")
        worker = threading.Thread(target=brain.step)
        try:
            worker.start()
            time.sleep(0.2)
            self.assertEqual(post.calls, [])
            self.assertEqual(brain.card.yields, 1)
        finally:
            lease.release()
        worker.join(timeout=5)
        self.assertEqual(got, ["hello"])
        sim.stop()

    def test_the_brain_never_takes_the_lock_itself(self) -> None:
        # If the crew held the lease around its own calls, the scheduler could not
        # take it for Daedalus mid-call - and Moss would stand down on every crew call.
        taken: list = []

        def during_call(url, body):
            lease = self.lock.try_acquire(owner="pionir", purpose="daedalus: probe")
            taken.append(lease is not None)
            if lease is not None:
                lease.release()

        sim = Sim(self.cfg)
        brain = Brain(self.cfg, sim, post=FakeOllama(on_call=during_call))
        brain.request("a", "thought", "s", "u", {}, lambda *_: None)
        brain.step()
        self.assertEqual(taken, [True])
        sim.stop()


class WarmTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = temp_dir()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = settings(self._tmp.name)

    def test_warming_waits_for_the_card_before_loading_the_model(self) -> None:
        post = FakeOllama()
        probe = ScriptedProbe(held_for=3, on_probe=lambda n: self.assertEqual(post.calls, []))
        card = CardWatch(self.cfg.gpu_lock_path, probe=probe, poll_seconds=0.01)
        warm_brain(self.cfg, card=card, post=post)
        self.assertEqual(card.yields, 1)
        self.assertEqual(len(post.calls), 1)
        self.assertTrue(post.calls[0][0].endswith("/api/generate"))

    def test_warming_stopped_while_the_card_is_held_never_loads(self) -> None:
        post = FakeOllama()
        card = CardWatch(self.cfg.gpu_lock_path, probe=lambda: {"purpose": "daedalus: x"},
                         poll_seconds=0.01)
        stop = threading.Event()
        threading.Timer(0.1, stop.set).start()
        with self.assertRaises(CardBusy):
            warm_brain(self.cfg, card=card, stop=stop, post=post)
        self.assertEqual(post.calls, [])


class SettingsTests(unittest.TestCase):
    def test_the_crew_lives_under_pionirs_state_root_and_reads_pionirs_own_lock(self) -> None:
        with temp_dir() as root:
            pionir = PionirSettings(state_root=Path(root))
            crew = CrewSettings.from_pionir(pionir)
            self.assertEqual(crew.state_dir, pionir.state_root / "crew")
            self.assertEqual(crew.gpu_lock_path, pionir.gpu_lock_path)
            self.assertEqual(crew.tick_seconds, 1.0)
            self.assertEqual(crew.model, "gemma3:12b")

    def test_a_non_positive_tick_is_refused(self) -> None:
        with temp_dir() as root, self.assertRaises(ValueError):
            settings(root, tick_seconds=0)


if __name__ == "__main__":
    unittest.main()
