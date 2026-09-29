"""Moss's window on the work log: aggregates only, read-only, and registered like the other
read-only capabilities. Everything runs on a temporary state root."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from standins import down_url

from pionir import cli
from pionir.adapters.work_summary import CAPABILITY, WorkSummaryAdapter, WorkSummarySettings
from pionir.bootstrap import build_runtime
from pionir.config import PionirSettings
from pionir.contracts import RiskLevel, Task
from pionir.errors import AdapterProtocolError
from pionir.router import IntentRouter
from pionir.worklog import WorkLog, Zone

CANARIES = ("CANARY-note-zebra", "CANARY-rubric-walrus", "CANARY-payout-heron", "CANARY-session-otter")


def _runtime(tmp: str):
    return build_runtime(PionirSettings(
        state_root=Path(tmp),
        atani_command=("pionir-test-no-such-binary",),
        galatea_url=down_url(),
        galatea_model_id="stub-model",
        embed_model=None,
        daedalus_url=down_url(),
        melete_url=down_url(),
        crew_url=None,
        bryo_status_command=None,
        nyx_status_command=None,
        voodoo_status_command=None,
        evict_to_fit=False,
    ))


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 3, 11, 15, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.t


def _plant(log: WorkLog) -> None:
    job = log.create_job("Data Annotation", "freelance", hourly_rate_cents=2500)
    log.create_job("Day job", "employment")
    log.add_session(job["id"], "2026-03-11T09:00:00Z", "2026-03-11T11:00:00Z", CANARIES[3])
    log.add_log(job["id"], "note", CANARIES[0])
    log.add_log(job["id"], "rubric", CANARIES[1])
    log.add_log(job["id"], "payout", CANARIES[2], amount_cents=123457)
    log.start_timer("Day job", note=CANARIES[3])


class WorkCapability(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.runtime = _runtime(self._tmp.name)
        self.path = self.runtime.settings.worklog_path
        self.log = WorkLog(self.path, clock=self.clock, zone=Zone("UTC"))
        _plant(self.log)

    def tearDown(self) -> None:
        self.runtime.cortex.close()
        self._tmp.cleanup()

    def adapter(self) -> WorkSummaryAdapter:
        return WorkSummaryAdapter(WorkSummarySettings(path=self.path, zone="UTC"), clock=self.clock)

    def test_the_answer_is_numbers_and_job_names_with_no_note_text(self) -> None:
        result = self.adapter().execute(Task(CAPABILITY, {}))
        text = json.dumps(result.output["work"])
        for canary in CANARIES:
            self.assertNotIn(canary, text)
        self.assertNotIn("123457", text)             # a payout is not an aggregate
        work = result.output["work"]
        self.assertTrue(work["timer_running"])
        jobs = {j["job"]: j for j in work["jobs"]}
        self.assertEqual(jobs["Data Annotation"]["today"], {"hours": 2.0, "earned_cents": 5000})
        self.assertTrue(jobs["Day job"]["timer_running"])
        self.assertFalse(jobs["Day job"]["rate_set"])
        self.assertIsNone(jobs["Day job"]["week"]["earned_cents"])
        allowed = {"job", "kind", "currency", "rate_set", "timer_running", "today", "week", "month"}
        self.assertEqual(set(jobs["Data Annotation"]), allowed)

    def test_it_is_read_only_and_has_one_capability(self) -> None:
        manifest = self.adapter().manifest
        self.assertEqual([c.name for c in manifest.capabilities], ["work.summary"])
        self.assertEqual(manifest.capabilities[0].risk, RiskLevel.READ_ONLY)
        self.assertFalse(manifest.capabilities[0].requires_approval)
        for name in ("work.start", "work.stop", "work.write", "work.log", "work.timer"):
            with self.assertRaises(AdapterProtocolError):
                self.adapter().execute(Task(name, {"job": "Day job", "text": "x"}))
        rows = self._rows()
        self.adapter().execute(Task(CAPABILITY, {"job": "Day job", "start": "now", "note": "x"}))
        self.assertEqual(self._rows(), rows)          # a payload cannot make it write

    def _rows(self) -> tuple:
        with closing(sqlite3.connect(self.path)) as con:
            return (con.execute("SELECT COUNT(*) FROM sessions").fetchone(),
                    con.execute("SELECT COUNT(*) FROM log").fetchone(),
                    con.execute("SELECT COUNT(*) FROM changes").fetchone())

    def test_it_runs_through_the_executive_and_is_audited_without_content(self) -> None:
        result = self.runtime.executive.execute(Task(CAPABILITY, {}))
        self.assertEqual(result.agent_id, "work")
        ledger = self.runtime.settings.audit_path.read_text(encoding="utf-8")
        for canary in CANARIES:
            self.assertNotIn(canary, ledger)

    def test_it_is_registered_and_routable_like_the_other_read_only_capabilities(self) -> None:
        self.assertIn("work", self.runtime.adapters)
        decision = IntentRouter(self.runtime.executive).classify("how many hours did I work this week")
        self.assertEqual(decision.capability, CAPABILITY, decision)
        decision = IntentRouter(self.runtime.executive).classify("did I leave my timer running")
        self.assertEqual(decision.capability, CAPABILITY, decision)

    def test_doctor_reports_the_db_open_timers_and_last_write(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        try:
            runtime = build_runtime(PionirSettings(
                state_root=Path(tmp.name), atani_command=("pionir-test-no-such-binary",),
                galatea_url=down_url(), galatea_model_id="stub-model", embed_model=None,
                daedalus_url=None, melete_url=None, crew_url=None, bryo_status_command=None,
                nyx_status_command=None, voodoo_status_command=None, evict_to_fit=False))
            _plant(WorkLog(runtime.settings.worklog_path, clock=self.clock, zone=Zone("UTC")))
            report = cli._doctor(runtime)
            work = report["specialists"]["work"]["details"]
            self.assertTrue(work["present"])
            self.assertEqual(work["open_timers"], 1)
            self.assertEqual(work["jobs"], 2)
            self.assertIsNotNone(work["last_write"])
            self.assertNotIn("CANARY", json.dumps(report, default=str))
            runtime.cortex.close()
        finally:
            tmp.cleanup()

    def test_doctor_before_any_work_creates_no_database(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        try:
            runtime = build_runtime(PionirSettings(
                state_root=Path(tmp.name), atani_command=("pionir-test-no-such-binary",),
                galatea_url=down_url(), galatea_model_id="stub-model", embed_model=None,
                daedalus_url=None, melete_url=None, crew_url=None, bryo_status_command=None,
                nyx_status_command=None, voodoo_status_command=None, evict_to_fit=False))
            work = cli._doctor(runtime)["specialists"]["work"]["details"]
            self.assertFalse(work["present"])
            self.assertEqual((work["open_timers"], work["last_write"]), (0, None))
            runtime.executive.execute(Task(CAPABILITY, {}))
            self.assertFalse(runtime.settings.worklog_path.exists())
            runtime.cortex.close()
        finally:
            tmp.cleanup()

    def test_a_forgotten_timer_shows_as_stale_in_the_aggregate(self) -> None:
        self.clock.t += timedelta(hours=13)
        work = self.adapter().execute(Task(CAPABILITY, {})).output["work"]
        self.assertTrue(work["stale_timer"])
        details = self.adapter().status()
        self.assertEqual((details["open_timers"], details["flagged_timers"]), (1, 1))


if __name__ == "__main__":
    unittest.main()
