"""The approval gate: a privileged action Moss/Atani requests without its
permission is parked, not run, until Ian approves it - then it runs with exactly
that permission. This is what lets an offensive tool be reachable at all while
never firing on the voice's own initiative.
"""
import tempfile
import unittest
from pathlib import Path

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
                galatea_url="http://127.0.0.1:8799",
                galatea_model_id="stub-model",
                daedalus_url="http://127.0.0.1:9998",
                melete_url="http://127.0.0.1:9999",
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
        # with the permission, it runs (daedalus is a dead port here, so it errors -
        # but it is NOT parked); the gate only holds the UN-permitted.
        out = self.app.run_task("coding.daedalus_solve", {"content": "x"},
                                permissions=["daedalus.solve"])
        self.assertNotEqual(out.get("status"), "pending_approval")

    def test_approve_runs_it_then_cannot_run_twice(self):
        aid = self.app.run_task("coding.daedalus_solve", {"content": "x"})["approval_id"]
        res = self.app.approve(aid)
        self.assertEqual(res["status"], "approved")
        self.assertEqual(self.app.approvals.get(aid)["status"], "approved")
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
