"""Jobs: the only way a worker acts in the world, and what an outcome may be taken to mean.

Pionir is a scripted fake in its real response shapes. Each test fails if the rule it
names is reverted: a job parked for the owner's approval read as done, an answer of
unknown shape assumed to be success, a job with no outcome read as done, or the client
reaching anywhere but loopback.
"""

import threading
import unittest

from crew_support import temp_dir
from test_crew_fakes import FAILED, PENDING, FakePionir, done, make_crew

from pionir.crew.hands import Job, JobOutcome, PionirClient, outcome_of


class OutcomeTests(unittest.TestCase):
    def test_pionirs_shapes_map_to_outcomes(self) -> None:
        self.assertEqual(outcome_of("x", done({"a": 1})).status, "done")
        self.assertEqual(outcome_of("x", PENDING).status, "pending_approval")
        self.assertEqual(outcome_of("x", FAILED).status, "failed")
        self.assertEqual(outcome_of("x", {"status": "error", "error": "bad"}).status, "failed")
        self.assertEqual(outcome_of("x", {"status": "running", "task_id": "t"}).status,
                         "running")

    def test_an_answer_of_unknown_shape_is_never_assumed_done(self) -> None:
        for doc in ({}, {"task_id": "t"}, {"error": "forbidden"}, "yes", None):
            self.assertFalse(outcome_of("x", doc).ran, doc)

    def test_the_figures_of_a_result_are_its_numbers_not_its_booleans(self) -> None:
        out = outcome_of("x", done({"revenue": 470, "paid": True, "note": "12 replies"}))
        self.assertEqual(sorted(out.figures), [12.0, 470.0])

    def test_an_unknown_status_cannot_be_made(self) -> None:
        with self.assertRaises(ValueError):
            JobOutcome("probably_done")

    def test_the_client_reaches_pionir_over_loopback_only(self) -> None:
        PionirClient("http://127.0.0.1:8780")
        for url in ("http://example.com:8780", "https://127.0.0.1:8780", "http://10.0.0.2"):
            with self.assertRaises(ValueError):
                PionirClient(url)


class WorkerJobTests(unittest.TestCase):
    """A worker's ``ctx.job`` goes through the hands to Pionir and waits for the outcome."""

    def setUp(self) -> None:
        tmp = temp_dir()
        self.addCleanup(tmp.cleanup)
        self.crew = make_crew(tmp.name)
        self.addCleanup(self.crew.stop)
        self.worker = self.crew.registry.require("alpha.a1")

    def _job_from_a_worker(self, answer) -> JobOutcome:
        self.crew.hands.client = FakePionir({"outreach.send": answer})
        self.crew.hands.start()
        ctx = self.crew.context_for(self.worker)
        return ctx.job(Job("outreach.send", {"n": 1}, permissions=()))

    def test_a_privileged_job_comes_back_parked_not_done(self) -> None:
        out = self._job_from_a_worker(PENDING)
        self.assertEqual(out.status, "pending_approval")
        self.assertFalse(out.ran)
        self.assertEqual(self.crew.hands.client.calls[0][0], "outreach.send")

    def test_a_job_pionir_ran_comes_back_done(self) -> None:
        out = self._job_from_a_worker(done({"sent": 1}))
        self.assertTrue(out.ran)

    def test_a_job_with_no_outcome_is_unreachable_never_done(self) -> None:
        # the hands never start: nothing serves the queue, and the wait runs out
        self.crew.hands.client = FakePionir({"x.y": done({})})
        out: list = []
        t = threading.Thread(target=lambda: out.append(
            self.crew.hands.run_sync("alpha.a1", Job("x.y"), timeout=0.3)))
        t.start()
        t.join(5)
        self.assertEqual(out[0].status, "unreachable")
        self.assertFalse(out[0].ran)


if __name__ == "__main__":
    unittest.main()
