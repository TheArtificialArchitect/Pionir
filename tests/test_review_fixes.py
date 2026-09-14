"""Regression tests for the 2026-09-14 audit fixes.

Each test fails if its fix is reverted:

* item 1 - a still-running job carries an explicit ``running: True`` a client
  cannot misread as failure.
* item 2 - a malformed run action is refused BEFORE it is parked for approval.
* item 3 - a specialist that returns a failing verdict is reported ok:false and
  an approved-but-failed action is recorded ``approved_failed``, not ``approved``.
* item 5 - a failed task writes a lesson that ``lessons_for`` recalls.
* item 7 - the pulse thread logs recurring errors (rate-limited) rather than
  swallowing them.
"""

import logging
import tempfile
import threading
import time
import unittest
from pathlib import Path

from pionir.bootstrap import build_runtime
from pionir.config import PionirSettings
from pionir.contracts import AgentManifest, Capability, RiskLevel, Task, TaskResult
from pionir.server import PionirApp, _outcome_ok, _RateLimitedErrors


class _SleepAdapter:
    """A read-only capability that takes long enough to outlive a short wait."""

    def __init__(self) -> None:
        self._manifest = AgentManifest(
            "sleeper", "test",
            (Capability("test.sleep", "sleeps a while",
                        routing_hints=frozenset({"snooze"})),),
        )

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def execute(self, task: Task) -> TaskResult:
        time.sleep(0.5)
        return TaskResult(task.task_id, "sleeper", {"ok": True}, ())


class _FailAdapter:
    """Capabilities whose specialist returns normally with a FAILING verdict."""

    def __init__(self) -> None:
        self._manifest = AgentManifest(
            "flake", "test",
            (
                Capability("test.fail_read", "returns ok:false",
                           routing_hints=frozenset({"flakey"})),
                Capability("test.fail_privileged", "privileged and fails",
                           risk=RiskLevel.PRIVILEGED,
                           required_permissions=frozenset({"flake.run"})),
                Capability("test.bad_rc", "non-zero returncode",
                           routing_hints=frozenset({"rcish"})),
            ),
        )

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def execute(self, task: Task) -> TaskResult:
        if task.capability == "test.bad_rc":
            return TaskResult(task.task_id, "flake", {"returncode": 2, "output": "boom"}, ())
        return TaskResult(task.task_id, "flake", {"ok": False, "error": "it did not work"}, ())


def _app(tmp: str, *, nyx: bool = False) -> PionirApp:
    runtime = build_runtime(
        PionirSettings(
            state_root=Path(tmp),
            atani_command=("pionir-test-no-such-binary",),
            bryo_status_command=None,
            nyx_status_command=("python", "-c", "print('{}')") if nyx else None,
            voodoo_status_command=None,
            daedalus_url=None,
            melete_url=None,
            galatea_url=None,
            evict_to_fit=False,
            embed_model=None,  # lexical-only cortex: no network, no embedder
        )
    )
    runtime.register(_SleepAdapter())
    runtime.register(_FailAdapter())
    return PionirApp(runtime)


class RunningFieldTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.app = _app(self._tmp.name)

    def tearDown(self) -> None:
        self.app.runtime.cortex.close()
        self._tmp.cleanup()

    def test_a_still_running_job_says_so_explicitly(self) -> None:
        out = self.app.run_task("test.sleep", {}, wait=0.05)
        self.assertEqual(out["status"], "running")
        self.assertIs(out["running"], True)      # the field a client cannot misread
        self.assertNotIn("ok", out)              # no ok:false to be read as failure
        self.assertTrue(self.app.jobs.wait(out["task_id"], 30))


class HonestOkTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.app = _app(self._tmp.name)

    def tearDown(self) -> None:
        self.app.runtime.cortex.close()
        self._tmp.cleanup()

    def test_outcome_ok_reads_the_inner_verdict(self) -> None:
        self.assertTrue(_outcome_ok({"ok": True}))
        self.assertFalse(_outcome_ok({"ok": False}))
        self.assertFalse(_outcome_ok({"returncode": 1}))
        self.assertTrue(_outcome_ok({"returncode": 0}))
        self.assertTrue(_outcome_ok({"ok": True, "flag": True}))  # bools are not rcs

    def test_a_failing_specialist_is_reported_not_ok(self) -> None:
        out = self.app.run_task("test.fail_read", {}, wait=30)
        self.assertFalse(out["ok"])
        self.assertEqual(self.app.job(out["task_id"])["status"], "error")

    def test_a_nonzero_returncode_is_reported_not_ok(self) -> None:
        out = self.app.run_task("test.bad_rc", {}, wait=30)
        self.assertFalse(out["ok"])

    def test_an_approved_but_failed_action_is_recorded_approved_failed(self) -> None:
        parked = self.app.run_task("test.fail_privileged", {"content": "go"})
        self.assertEqual(parked["status"], "pending_approval")
        first = self.app.approve(parked["approval_id"])
        self.assertTrue(self.app.jobs.wait(first["task_id"], 30))
        record = self.app.approvals.get(parked["approval_id"])
        self.assertEqual(record["status"], "approved_failed")  # not "approved"


class ValidationBeforeApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.app = _app(self._tmp.name, nyx=True)

    def tearDown(self) -> None:
        self.app.runtime.cortex.close()
        self._tmp.cleanup()

    def test_a_malformed_action_is_refused_before_it_is_parked(self) -> None:
        out = self.app.run_task(
            "security.nyx_run",
            {"action": "research", "args": ["not a url with spaces"]},
            permissions=[],
        )
        self.assertEqual(out["status"], "error")
        self.assertFalse(out["ok"])
        self.assertEqual(self.app.approvals.pending(), [])  # nothing queued for Ian

    def test_a_well_formed_action_still_reaches_the_approval_gate(self) -> None:
        out = self.app.run_task(
            "security.nyx_run",
            {"action": "research", "args": ["https://example.com"]},
            permissions=[],
        )
        self.assertEqual(out["status"], "pending_approval")
        self.assertEqual(len(self.app.approvals.pending()), 1)


class FailureLessonTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.app = _app(self._tmp.name)

    def tearDown(self) -> None:
        self.app.runtime.cortex.close()
        self._tmp.cleanup()

    def test_a_failed_task_leaves_a_lesson_that_lessons_for_recalls(self) -> None:
        self.app.run_task("test.fail_read", {"content": "parse the widget manifest"}, wait=30)
        lessons = self.app.runtime.cortex.lessons_for("parse the widget manifest")
        self.assertTrue(lessons)
        self.assertIn("fail", lessons[0].text.lower())


class RateLimitedErrorTests(unittest.TestCase):
    def test_logs_the_first_then_rate_limits_with_a_suppressed_count(self) -> None:
        clock = [0.0]
        limiter = _RateLimitedErrors(
            logging.getLogger("pionir.test.pulse"), interval=60.0, clock=lambda: clock[0]
        )
        with self.assertLogs("pionir.test.pulse", level="WARNING") as caught:
            limiter.note(ValueError("x"), "poll failed")  # logs
            limiter.note(ValueError("x"), "poll failed")  # suppressed
            limiter.note(ValueError("x"), "poll failed")  # suppressed
            clock[0] = 120.0
            limiter.note(ValueError("x"), "poll failed")  # logs again
        self.assertEqual(len(caught.records), 2)
        self.assertIn("2 similar suppressed", caught.output[1])


if __name__ == "__main__":
    unittest.main()
