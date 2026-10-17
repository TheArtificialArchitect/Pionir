"""The Techne watch: a preview is ready, so the owner is alerted - once, never twice.

Pionir is a fake (``FakePionir``): it lists the Techne jobs and takes the owner.notify
alerts. Each test fails if the rule it names is reverted: a preview_ready job pinged more
than once, a job that was never preview_ready pinged, the watch firing techne.run, or jobs
that could not be read reported as none.
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from pionir.crew.hands import JobOutcome
from pionir.crew.registry import default_registry
from pionir.crew.result import Err, Ok
from pionir.crew.techne import OWNER_NOTIFY, TECHNE_JOBS
from pionir.crew.worker import ErrorKind, WorkContext

T0 = 1_790_000_000.0


def a_job(jid="job_1", status="preview_ready", slug="acme", domain="acme.example"):
    return {"id": jid, "status": status, "slug": slug, "domain": domain,
            "preview_url": "https://acme.pages.dev", "updated_at": "2026-10-02T10:00:00Z"}


class FakePionir:
    """``ctx.job``: Techne jobs listed, owner.notify alerts taken."""

    def __init__(self, *jobs) -> None:
        self.jobs = list(jobs)
        self.calls: list = []
        self.jobs_outcome = None
        self.notify_outcome = None

    def job(self, job):
        self.calls.append(job)
        if job.capability == TECHNE_JOBS:
            if self.jobs_outcome is not None:
                return self.jobs_outcome
            return JobOutcome("done", TECHNE_JOBS, task_id="t-j",
                              result={"ok": True, "jobs": copy.deepcopy(self.jobs)})
        if job.capability == OWNER_NOTIFY:
            if self.notify_outcome is not None:
                return self.notify_outcome
            return JobOutcome("done", OWNER_NOTIFY, result={"ok": True, "sent": True})
        raise AssertionError(f"the watch must never call {job.capability}")

    def alerts(self) -> list:
        return [j for j in self.calls if j.capability == OWNER_NOTIFY]


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.worker = default_registry().require("delivery.techne")

    def run_with(self, pionir, now=T0):
        ctx = WorkContext(now=now, http=None, secrets_dir=self.state, job=pionir.job,
                          state_dir=self.state)
        return self.worker.run(ctx)

    def record(self) -> dict:
        return json.loads(self.worker.record_path(self.state).read_text(encoding="utf-8"))


class WatchTests(_Case):
    def test_one_preview_ready_job_pings_exactly_once(self) -> None:
        pionir = FakePionir(a_job(), a_job("job_live", status="live"))
        result = self.run_with(pionir)
        self.assertIsInstance(result, Ok)
        self.assertEqual(len(pionir.alerts()), 1)
        alert = pionir.alerts()[0]
        self.assertEqual(alert.payload["kind"], "alert")
        self.assertIn("preview is ready", alert.payload["body_text"])
        self.assertIn("job_1", self.record()["pinged"])

    def test_the_same_job_is_not_pinged_on_a_second_run(self) -> None:
        pionir = FakePionir(a_job())
        self.run_with(pionir)
        self.run_with(pionir, now=T0 + 300)
        self.assertEqual(len(pionir.alerts()), 1)   # still just the one

    def test_a_new_preview_ready_job_pings(self) -> None:
        pionir = FakePionir(a_job("job_1"))
        self.run_with(pionir)
        pionir.jobs.append(a_job("job_2", slug="beta"))
        self.run_with(pionir, now=T0 + 300)
        pinged = {a.payload["body_text"] for a in pionir.alerts()}
        self.assertEqual(len(pionir.alerts()), 2)
        self.assertEqual(sorted(self.record()["pinged"]), ["job_1", "job_2"])
        self.assertTrue(any("job_1" in t or "acme" in t for t in pinged))

    def test_a_job_that_is_never_preview_ready_is_never_pinged(self) -> None:
        pionir = FakePionir(a_job("j_b", status="building"),
                            a_job("j_l", status="live"),
                            a_job("j_d", status="down"))
        self.run_with(pionir)
        self.assertEqual(pionir.alerts(), [])
        self.assertEqual(self.record()["pinged"], [])

    def test_the_watch_never_fires_techne_run(self) -> None:
        pionir = FakePionir(a_job())
        self.run_with(pionir)
        for call in pionir.calls:
            self.assertIn(call.capability, (TECHNE_JOBS, OWNER_NOTIFY))
            self.assertNotEqual(call.capability, "techne.run")

    def test_a_failed_alert_is_retried_next_run(self) -> None:
        pionir = FakePionir(a_job())
        pionir.notify_outcome = JobOutcome("unreachable", OWNER_NOTIFY, error="Discord down")
        self.run_with(pionir)
        self.assertEqual(self.record()["pinged"], [])   # not recorded, so not lost
        pionir.notify_outcome = None
        self.run_with(pionir, now=T0 + 300)
        self.assertEqual(len(pionir.alerts()), 2)        # one failed try + one that sent
        self.assertIn("job_1", self.record()["pinged"])

    def test_jobs_that_cannot_be_read_are_unknown_not_none(self) -> None:
        pionir = FakePionir()
        pionir.jobs_outcome = JobOutcome("unreachable", TECHNE_JOBS, error="Pionir down")
        result = self.run_with(pionir)
        self.assertIsInstance(result, Err)
        self.assertEqual(result.error.kind, ErrorKind.UNAVAILABLE)
        self.assertEqual(pionir.alerts(), [])


if __name__ == "__main__":
    unittest.main()
