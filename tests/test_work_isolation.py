"""The work log is its own file: nothing that indexes, recalls, consolidates, posts or
shows Moss's memory can see it. Canary text planted in every text column of the work log
must never come back from any of those doors; and the work log opens no other database."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from standins import down_url

from pionir import library
from pionir.bootstrap import build_runtime
from pionir.config import PionirSettings
from pionir.worklog import WorkLog

CANARY = "CANARY-isolation-quokka"


class WorkLogIsolation(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.runtime = build_runtime(PionirSettings(
            state_root=Path(self._tmp.name),
            atani_command=("pionir-test-no-such-binary",),
            galatea_url=down_url(), galatea_model_id="stub-model", embed_model=None,
            daedalus_url=down_url(), melete_url=down_url(), crew_url=None,
            bryo_status_command=None, nyx_status_command=None, voodoo_status_command=None,
            evict_to_fit=False,
        ))
        self.settings = self.runtime.settings
        log = WorkLog(self.settings.worklog_path)
        job = log.create_job("Data Annotation", "freelance", hourly_rate_cents=2500)
        log.add_session(job["id"], "2026-03-11T09:00:00Z", "2026-03-11T10:00:00Z", f"{CANARY} session")
        log.add_log(job["id"], "note", f"{CANARY} note")
        log.add_log(job["id"], "rubric", f"{CANARY} rubric")
        log.add_log(job["id"], "payout", f"{CANARY} payout", amount_cents=5000)
        self.runtime.cortex.record_lesson("a harmless ordinary lesson about tomatoes")

    def tearDown(self) -> None:
        self.runtime.cortex.close()
        self._tmp.cleanup()

    def test_no_other_state_file_holds_the_text(self) -> None:
        self.assertTrue(self.settings.worklog_path.exists())
        for db in self.settings.state_root.rglob("*"):
            if db.is_file() and db != self.settings.worklog_path \
                    and not db.name.startswith("worklog.db"):
                self.assertNotIn(CANARY.encode(), db.read_bytes(), str(db))

    def test_recall_cannot_return_work_log_text(self) -> None:
        for query in (CANARY, "isolation quokka", "Data Annotation note rubric payout session"):
            for namespace in (None, "lessons", "default"):
                got = self.runtime.cortex.recall(query, k=20, namespace=namespace)
                self.assertNotIn(CANARY, repr(got))

    def test_the_library_cannot_read_work_log_text(self) -> None:
        memory_db = next(p for p in self.settings.state_root.rglob("*.db")
                         if p != self.settings.worklog_path and "memory" in str(p))
        self.assertNotIn(CANARY, json.dumps(library.overview(memory_db), default=str))
        self.assertNotIn(CANARY, json.dumps(library.entries(memory_db, {"q": [CANARY]}), default=str))

    def test_the_audit_ledger_and_the_doctor_status_hold_no_work_text(self) -> None:
        self.assertNotIn(CANARY, json.dumps(self.runtime.adapters["work"].status(), default=str))
        audit = self.settings.audit_path
        if audit.exists():
            self.assertNotIn(CANARY, audit.read_text(encoding="utf-8"))

    def test_the_work_log_holds_only_its_own_tables(self) -> None:
        with closing(sqlite3.connect(self.settings.worklog_path)) as con:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"jobs", "sessions", "log", "changes"} <= tables)
        self.assertFalse({"memories", "lessons", "memory"} & tables)


if __name__ == "__main__":
    unittest.main()
