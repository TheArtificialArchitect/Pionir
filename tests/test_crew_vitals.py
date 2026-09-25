"""Vitals: the inertness watch over workers and leaders, read from the runs table.

Each test fails if the watch is reverted to trusting verdicts: a worker that has never
succeeded, or keeps succeeding while producing nothing, going unreported - or a warning
that never clears once the worker recovers (a latch is the same bug in another hat).
"""

import time
import unittest

from crew_support import temp_dir
from test_crew_fakes import catalogue, make_crew

from pionir.crew.vitals import NEVER_AFTER


class VitalsTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = temp_dir()
        self.addCleanup(tmp.cleanup)
        self.crew = make_crew(tmp.name, cat=catalogue({
            "alpha": [{"name": "dead", "params": {"mode": "err"}},
                      {"name": "todo", "impl": "placeholder", "provider": "none"}]}))
        self.addCleanup(self.crew.stop)

    def checks(self) -> set:
        return {(v["who"], v["check"]) for v in self.crew.vitals.check(force=True)}

    def test_young_is_not_dead(self) -> None:
        self.crew.dispatcher.dispatch(only=["alpha.dead"], wait=True)
        self.assertNotIn(("alpha.dead", "never"), self.checks())

    def test_never_succeeded_is_reported_then_cleared(self) -> None:
        w = self.crew.registry.require("alpha.dead")
        for _ in range(NEVER_AFTER):
            self.crew.dispatcher.dispatch(only=["alpha.dead"], wait=True)
        self.assertIn(("alpha.dead", "never"), self.checks())
        w.mode = "ok"
        self.crew.dispatcher.dispatch(only=["alpha.dead"], wait=True)
        self.assertNotIn(("alpha.dead", "never"), self.checks())
        self.assertGreaterEqual(self.crew.vitals.cleared, 1)

    def test_a_placeholder_is_named_as_not_wired(self) -> None:
        self.crew.dispatcher.dispatch(only=["alpha.todo"], wait=True)
        self.assertIn(("alpha.todo", "not_wired"), self.checks())

    def test_a_leader_that_never_reports_is_watched_like_a_worker(self) -> None:
        cadences = self.crew.all_cadences()
        self.assertIn("leader.alpha", cadences)
        for _ in range(NEVER_AFTER):
            self.crew.store.record_attempt(worker_id="leader.alpha", division="alpha",
                                           started_at=time.time(), finished_at=time.time(),
                                           error=_err())
        self.assertIn(("leader.alpha", "never"), self.checks())


def _err():
    from pionir.crew.worker import ErrorKind, WorkerError
    return WorkerError("leader.alpha", ErrorKind.NO_WORDS, "refused")


if __name__ == "__main__":
    unittest.main()
