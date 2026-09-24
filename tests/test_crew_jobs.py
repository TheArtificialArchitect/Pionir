"""Jobs: the only way a crew member touches the world, and what gets recorded.

Pionir is a scripted fake in its real response shapes. Each test fails if the rule it
names is reverted: a job recorded as done when Pionir did not report it ran, a job
parked for Ian's approval recorded as done (or as nothing), a failed job relieving
purpose, or an answer of unknown shape assumed to be success.
"""

import unittest

from test_crew_kit import FAILED, PENDING, Crew, SendKind, World, done, member

from pionir.crew.hands import JobOutcome, PionirClient, PionirUnreachable, outcome_of


def kinds_of(agent) -> set:
    return {e["kind"] for e in agent.mem.recent(200)}


class JobTests(unittest.TestCase):
    def make(self, answer):
        self.world = World(target=10)
        self.kind = SendKind(self.world)
        crew = Crew([member("ada", ["outreach"])], kinds=[self.kind],
                    answers={"outreach.send": answer})
        self.addCleanup(crew.close)
        ada = crew["ada"]
        crew.give_project(ada, self.kind)
        ada.drives.value["purpose"] = 0.2
        return crew, ada

    def sends(self, result):
        """Pionir 'runs' it: the world really changes, then Pionir says so."""
        def answer(payload):
            self.world.sent += payload["n"]
            return done(result)
        return answer

    def test_a_job_pionir_reports_as_run_becomes_a_did_episode_and_a_seen_result(self) -> None:
        crew, ada = self.make(self.sends({"answer": "Sent 1 follow-up to Acme", "sent": 1}))
        crew.work_one_step(ada)
        self.assertEqual(len(crew.pionir.calls), 1)
        did = ada.mem.recent(10, kinds=("did",))
        self.assertEqual(len(did), 1)
        self.assertIn("send one follow-up email", did[0]["text"])
        self.assertEqual((did[0]["source"], did[0]["detail"]["status"]), ("did", "done"))
        result = ada.mem.recent(10, kinds=("result",))[0]
        self.assertEqual(result["source"], "seen")
        self.assertIn(1.0, result["detail"]["figures"])
        self.assertIn("Acme", result["detail"]["entities"])
        self.assertEqual(ada.mem.counter("jobs_done"), 1)
        self.assertEqual(ada.mem.counter("project_steps"), 1)   # the world really moved

    def test_pending_approval_is_recorded_as_pending_and_never_as_done(self) -> None:
        crew, ada = self.make(PENDING)
        crew.work_one_step(ada)
        self.assertNotIn("did", kinds_of(ada))
        self.assertNotIn("did_own", kinds_of(ada))
        pending = ada.mem.recent(10, kinds=("job_pending",))
        self.assertEqual(len(pending), 1)
        self.assertIn("has NOT run", pending[0]["text"])
        self.assertEqual(pending[0]["detail"]["approval_id"], "ap-1")
        self.assertEqual(ada.mem.counter("jobs_done"), 0)
        self.assertEqual(ada.mem.counter("jobs_pending"), 1)
        # and so she cannot say she did it
        self.assertIsNone(crew.sim.talk.critic(ada, "I sent the follow-up email."))

    def test_a_failed_job_relieves_no_purpose(self) -> None:
        crew, ada = self.make(FAILED)
        before = ada.drives.value["purpose"]
        crew.work_one_step(ada)
        self.assertLessEqual(ada.drives.value["purpose"], before)   # only decay, no relief
        self.assertNotIn("did", kinds_of(ada))
        tried = ada.mem.recent(10, kinds=("tried",))
        self.assertTrue(any("mail relay refused" in e["text"] for e in tried))
        self.assertEqual(ada.mem.counter("project_steps"), 0)
        self.assertEqual(ada.mem.counter("project_steps_empty"), 1)
        self.assertEqual(ada.mem.counter("jobs_failed"), 1)

    def test_a_job_that_ran_but_changed_nothing_relieves_no_purpose(self) -> None:
        # Pionir says ok, but the world the project measures did not move
        crew, ada = self.make(done({"answer": "nothing to send"}))
        before = ada.drives.value["purpose"]
        crew.work_one_step(ada)
        self.assertEqual(ada.mem.counter("jobs_done"), 1)
        self.assertLessEqual(ada.drives.value["purpose"], before)
        self.assertEqual(ada.mem.counter("project_steps"), 0)

    def test_a_running_job_is_followed_to_its_real_outcome(self) -> None:
        crew, ada = self.make({"status": "running", "running": True, "task_id": "t-9"})
        crew.pionir.records["t-9"] = {"task_id": "t-9", "status": "done",
                                      "result": done({"answer": "sent"}, "t-9")}
        crew.work_one_step(ada)
        self.assertEqual(crew.pionir.polls, ["t-9"])
        self.assertEqual(ada.mem.counter("jobs_done"), 1)

    def test_pionir_down_is_an_attempt_that_got_no_answer(self) -> None:
        crew, ada = self.make(PionirUnreachable("ConnectionRefusedError: refused"))
        crew.work_one_step(ada)
        self.assertNotIn("did", kinds_of(ada))
        self.assertTrue(any("did not answer" in e["text"]
                            for e in ada.mem.recent(10, kinds=("tried",))))

    def test_a_crew_without_hands_records_that_nothing_ran(self) -> None:
        crew, ada = self.make(done({}))
        crew.sim.hands = None
        crew.work_one_step(ada)
        self.assertEqual(crew.pionir.calls, [])
        self.assertNotIn("did", kinds_of(ada))
        self.assertEqual(ada.mem.counter("jobs_failed"), 1)


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


if __name__ == "__main__":
    unittest.main()
