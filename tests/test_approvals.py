"""The approval gate: a privileged action Moss/Atani requests without its
permission is parked, not run, until Ian approves it - then it runs with exactly
that permission. This is what lets an offensive tool be reachable at all while
never firing on the voice's own initiative.
"""
import tempfile
import unittest
from pathlib import Path

from standins import down_url

from pionir.approvals import ApprovalQueue
from pionir.bootstrap import build_runtime
from pionir.config import PionirSettings
from pionir.server import PionirApp


def _app(tmp: str) -> PionirApp:
    return PionirApp(
        build_runtime(
            PionirSettings(
                state_root=Path(tmp),
                atani_command=("pionir-test-no-such-binary",),
                galatea_url=down_url(),
                galatea_model_id="stub-model",
                embed_model=None,  # never reach the live Ollama embedder from a test
                daedalus_url=down_url(),
                melete_url=down_url(),
                bryo_status_command=None,
                nyx_status_command=None,
                voodoo_status_command=None,
                evict_to_fit=False,
            )
        )
    )


class QueueTests(unittest.TestCase):
    def test_enqueue_pending_resolve_and_no_double_resolve(self):
        with tempfile.TemporaryDirectory() as tmp:
            q = ApprovalQueue(Path(tmp) / "q.json")
            aid = q.enqueue("coding.daedalus_solve", {"content": "fix"},
                            ["daedalus.solve"], "Daedalus · fix")
            self.assertEqual(len(q.pending()), 1)
            self.assertEqual(q.get(aid)["status"], "pending")
            self.assertTrue(q.resolve(aid, "approved", {"ok": True}))
            self.assertFalse(q.resolve(aid, "denied"))     # already resolved: no re-run
            self.assertEqual(q.pending(), [])
            self.assertEqual(q.recent()[0]["status"], "approved")

    def test_queue_is_durable_across_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "q.json"
            aid = ApprovalQueue(path).enqueue("x.y", {}, ["p"], "s")
            self.assertEqual(ApprovalQueue(path).get(aid)["status"], "pending")


class GateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._t = tempfile.TemporaryDirectory()
        self.app = _app(self._t.name)

    def tearDown(self) -> None:
        self.app.runtime.cortex.close()
        self._t.cleanup()

    def test_privileged_without_permission_is_parked_not_run(self):
        out = self.app.run_task("coding.daedalus_solve", {"content": "refactor X"})
        self.assertEqual(out["status"], "pending_approval")
        self.assertIn("approval_id", out)
        self.assertIn("daedalus", out["summary"])
        self.assertEqual(len(self.app.approvals.pending()), 1)

    def test_granted_permission_bypasses_the_gate(self):
        # with the permission, it runs (daedalus is a 503 stand-in here, so it errors -
        # but it is NOT parked); the gate only holds the UN-permitted.
        out = self.app.run_task("coding.daedalus_solve", {"content": "x"},
                                permissions=["daedalus.solve"])
        self.assertNotEqual(out.get("status"), "pending_approval")

    def test_approve_runs_it_then_cannot_run_twice(self):
        aid = self.app.run_task("coding.daedalus_solve", {"content": "x"})["approval_id"]
        res = self.app.approve(aid)
        # Claimed and running as a job: the answer comes back at once with the
        # job's id; the approval record settles when the job ends.
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], "running")
        self.assertFalse(self.app.approve(aid)["ok"])       # already claimed: no re-run
        self.assertTrue(self.app.jobs.wait(res["task_id"], 30))
        # daedalus answers 503 here, so it ran and failed: approved_failed,
        # with the outcome on the record - never silently "approved".
        record = self.app.approvals.get(aid)
        self.assertEqual(record["status"], "approved_failed")
        self.assertEqual(record["task_id"], res["task_id"])
        self.assertFalse(record["result"]["ok"])
        self.assertFalse(self.app.approve(aid)["ok"])       # already resolved

    def test_deny_never_runs_it(self):
        aid = self.app.run_task("coding.daedalus_solve", {"content": "x"})["approval_id"]
        self.assertTrue(self.app.deny(aid)["ok"])
        self.assertEqual(self.app.approvals.get(aid)["status"], "denied")

    def test_view_lists_pending_and_recent(self):
        self.app.run_task("coding.daedalus_solve", {"content": "a"})
        view = self.app.approvals_view()
        self.assertEqual(len(view["pending"]), 1)
        self.assertGreaterEqual(len(view["recent"]), 1)


if __name__ == "__main__":
    unittest.main()
