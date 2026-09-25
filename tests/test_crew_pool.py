"""The pool: several workers at once, polite per provider, one writer, fixed jitter.

Each test fails if the behaviour it names is reverted: two starts against one provider
closer than its interval (even from different threads), a write executed on any thread
but the store's one writer, workers run one at a time, or a worker's cadence offset
changing between restarts.
"""

import threading
import time
import unittest
from itertools import pairwise

from crew_support import temp_dir
from test_crew_fakes import ScriptedWorker, catalogue, make_crew

from pionir.crew.pool import ProviderGate, jitter


class _Case(unittest.TestCase):
    def crew(self, divisions, providers=None, **kw):
        tmp = temp_dir()
        self.addCleanup(tmp.cleanup)
        crew = make_crew(tmp.name, cat=catalogue(divisions, providers), **kw)
        self.addCleanup(crew.stop)
        return crew


class ConcurrencyTests(_Case):
    def test_workers_run_at_once_in_a_bounded_pool(self) -> None:
        crew = self.crew({"alpha": [{"name": f"w{i}", "provider": f"p{i}",
                                     "params": {"sleep": 0.3}} for i in range(4)]},
                         providers={f"p{i}": 0.0 for i in range(4)}, pool_size=4)
        t0 = time.monotonic()
        report = crew.dispatcher.dispatch(wait=True)
        took = time.monotonic() - t0
        self.assertEqual(report.succeeded, 4)
        self.assertLess(took, 0.9)              # one at a time would take 1.2 s

    def test_the_per_provider_interval_holds_under_concurrency(self) -> None:
        ScriptedWorker.starts.clear()
        crew = self.crew({"alpha": [{"name": f"s{i}", "provider": "slow"} for i in range(4)]
                          + [{"name": f"f{i}", "provider": "fast"} for i in range(2)]},
                         providers={"slow": 0.15, "fast": 0.0}, pool_size=6)
        t0 = time.monotonic()
        report = crew.dispatcher.dispatch(wait=True)
        self.assertEqual(report.succeeded, 6)
        slow = sorted(t for w, t in ScriptedWorker.starts if ".s" in w)
        gaps = [b - a for a, b in pairwise(slow)]
        self.assertEqual(len(slow), 4)
        self.assertTrue(all(g >= 0.14 for g in gaps), gaps)
        fast = [t - t0 for w, t in ScriptedWorker.starts if ".f" in w]
        self.assertTrue(all(d < 0.1 for d in fast), fast)   # another host is not held up

    def test_the_gate_reserves_before_it_sleeps(self) -> None:
        gate = ProviderGate({"h": 0.1})
        starts: list = []
        lock = threading.Lock()

        def go():
            gate.wait_turn("h")
            with lock:
                starts.append(time.monotonic())

        threads = [threading.Thread(target=go) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        starts.sort()
        self.assertTrue(all(b - a >= 0.09 for a, b in pairwise(starts)), starts)

    def test_a_worker_is_never_run_twice_at_once(self) -> None:
        crew = self.crew({"alpha": [{"name": "slow", "params": {"sleep": 0.3}}]})
        first = crew.dispatcher.dispatch(only=["alpha.slow"])
        second = crew.dispatcher.dispatch(only=["alpha.slow"])
        self.assertEqual((first.attempted, second.attempted, second.busy), (1, 0, 1))


class SingleWriterTests(_Case):
    def test_every_write_happens_on_the_one_writer_thread(self) -> None:
        crew = self.crew({"alpha": [{"name": f"w{i}", "provider": f"p{i}"} for i in range(8)]},
                         providers={f"p{i}": 0.0 for i in range(8)}, pool_size=8)
        crew.store.write_threads.clear()
        for _ in range(5):
            report = crew.dispatcher.dispatch(only=crew.registry.ids(), wait=True)
            self.assertEqual(report.succeeded, 8)
        # and a burst of writes straight from many threads at once
        barrier = threading.Barrier(8)

        def hammer(i):
            barrier.wait()
            for _ in range(10):
                crew.store.add_call(0, f"t{i}", "x", 1, 1, 0.1, True, division="alpha")

        threads = [threading.Thread(target=hammer, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(crew.store.write_threads, {crew.store.writer_ident})
        self.assertEqual(crew.store.calls_last_hour("alpha"), 80)
        runs = sum(len(crew.store.runs(w, limit=100)) for w in crew.registry.ids())
        self.assertEqual(runs, 40)

    def test_a_read_after_a_write_sees_it(self) -> None:
        crew = self.crew({"alpha": [{"name": "a1"}]})
        crew.store.set("k", 1)
        self.assertEqual(crew.store.get("k"), 1)


class CadenceTests(_Case):
    def test_jitter_is_fixed_per_id_and_bounded(self) -> None:
        self.assertEqual(jitter("treasury.ledger"), jitter("treasury.ledger"))
        self.assertNotEqual(jitter("treasury.ledger"), jitter("watch.health"))
        for wid in ("a", "b", "treasury.ledger", "watch.health", "x" * 50):
            self.assertTrue(0.85 <= jitter(wid) <= 1.15)

    def test_a_worker_is_due_by_its_cadence_scaled_by_its_jitter(self) -> None:
        crew = self.crew({"alpha": [{"name": "a1", "cadence_seconds": 100}]})
        crew.dispatcher.dispatch(only=["alpha.a1"], wait=True)
        last = crew.store.last_attempts()["alpha.a1"]
        due_at = last + 100 * jitter("alpha.a1")
        self.assertEqual(crew.dispatcher.due(due_at - 1)[0], [])
        self.assertEqual([w.worker_id for w in crew.dispatcher.due(due_at + 1)[0]],
                         ["alpha.a1"])


if __name__ == "__main__":
    unittest.main()
