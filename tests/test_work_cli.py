"""``pionir work ...``: plain output, exit 0/1, its own database on a temp state root, and
the confidentiality reminder in the help. Driven through cli.main with PIONIR_STATE_ROOT
pointed at a temp dir - never the real ~/.pionir."""

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pionir import cli, workcli


class WorkCli(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {"PIONIR_STATE_ROOT": self._tmp.name,
                                               "PIONIR_WORK_TZ": "UTC"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "worklog" / "worklog.db"

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(["work", *argv])
            except SystemExit as exit_:      # argparse
                code = int(exit_.code or 0)
        return code, out.getvalue(), err.getvalue()

    def ok(self, *argv: str) -> str:
        code, out, err = self.run_cli(*argv)
        self.assertEqual(code, 0, err + out)
        return out

    def fails(self, *argv: str) -> str:
        code, out, err = self.run_cli(*argv)
        self.assertNotEqual(code, 0, out)
        self.assertNotIn("Traceback", err + out)
        return err

    def test_the_help_carries_the_confidentiality_reminder(self) -> None:
        for argv in ([], ["log"], ["add"], ["start"], ["payout"]):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as raised:
                cli.main(["work", *argv, "--help"])
            self.assertEqual(raised.exception.code, 0)
            self.assertIn("NDA", " ".join(out.getvalue().split()), argv)

    def test_a_day_of_work(self) -> None:
        self.ok("jobs", "add", "Data Annotation", "--rate", "25")
        self.ok("jobs", "add", "Day job", "--kind", "employment")
        self.assertIn("Data Annotation", self.ok("jobs"))
        self.assertIn("rate not set", self.ok("jobs", "list"))
        self.assertIn("Started", self.ok("start", "Data Annotation", "--note", "batch one"))
        self.assertIn("Running: Data Annotation", self.ok("status"))
        self.fails("start", "Data Annotation")                       # already running
        self.fails("start", "Day job")                               # another timer is open
        self.ok("start", "Day job", "--allow-concurrent")
        self.fails("stop")                                           # several: name the job
        self.assertIn("Stopped Day job", self._stop_now("Day job"))
        self.assertIn("Stopped Data Annotation", self.ok("stop"))     # exactly one left
        self.assertIn("Added Day job: 8:30:00",
                      self.ok("add", "Day job", "2026-03-09T09:00", "2026-03-09T17:30", "--note", "office"))
        self.fails("add", "Day job", "2026-03-09T12:00", "2026-03-09T13:00")   # overlap
        self.fails("add", "Day job", "2026-03-10T09:00", "2026-03-10T08:00")   # backwards
        self.fails("add", "Day job", "2026-03-10T00:00", "2026-03-10T23:00")   # over 16 hours
        self.assertIn("Logged rubric", self.ok("log", "Data Annotation", "explain the why", "--rubric"))
        self.assertIn("Logged note", self.ok("log", "Data Annotation", "batch felt long"))
        self.assertIn("Recorded payout 480.00 USD", self.ok("payout", "Data Annotation", "480"))
        self.assertIn("1,200.50 USD", self.ok("payout", "Data Annotation", "1,200.50"))
        summary = self.ok("summary")
        self.assertIn("Data Annotation (freelance)", summary)
        self.assertIn("rate not set", summary)                       # Day job has no rate
        self.assertIn("Total", summary)
        self.assertIn("year", summary)
        week = self.ok("summary", "--week")
        self.assertNotIn(" year ", week)
        self.assertIn("week", week)
        self.assertIn("month", self.ok("summary", "--month"))
        self.fails("summary", "--week", "--month")

    def _stop_now(self, job: str) -> str:
        import time
        time.sleep(1.1)
        return self.ok("stop", job)

    def test_job_edit_sets_and_clears_the_rate_and_never_guesses(self) -> None:
        self.ok("jobs", "add", "Contract")
        self.assertIn("rate not set", self.ok("jobs", "list"))
        self.assertIn("22.50 USD", self.ok("jobs", "edit", "Contract", "--rate", "22.50"))
        self.assertIn("rate not set", self.ok("jobs", "edit", "Contract", "--clear-rate"))
        self.ok("jobs", "edit", "Contract", "--set-aside", "25", "--currency", "eur")
        self.ok("jobs", "edit", "Contract", "--active", "no")
        self.fails("start", "Contract")                              # inactive
        self.fails("jobs", "edit", "Contract")                       # nothing to change
        self.fails("jobs", "edit", "Contract", "--rate", "1", "--clear-rate")
        self.fails("jobs", "edit", "Nobody", "--rate", "5")

    def test_money_is_parsed_from_decimal_text_never_a_float(self) -> None:
        self.ok("jobs", "add", "Job", "--rate", "0.10")
        self.assertIn("0.10 USD", self.ok("jobs", "edit", "Job", "--rate", "0.10"))
        for bad in ("abc", "-5", "1.234", "1e3", "", "0", "9999999999"):
            self.fails("payout", "Job", bad)
        self.fails("jobs", "add", "Bad", "--rate", "12.345")
        self.assertEqual(workcli.money(1), "0.01 USD")
        self.assertEqual(workcli.money(123456789, "EUR"), "1,234,567.89 EUR")
        self.assertEqual(workcli.money(None), "rate not set")

    def test_a_task_shaped_or_oversized_note_is_refused_and_not_stored(self) -> None:
        self.ok("jobs", "add", "Data Annotation")
        for text in ("Prompt: write a poem about the sea", "x" * 601, "Response A: the sea is blue"):
            err = self.fails("log", "Data Annotation", text)
            self.assertIn("NDA", err)
        self.assertEqual(self.ok("summary").count("Logged"), 0)
        self.fails("start", "Data Annotation", "--note", "Prompt: something")
        self.assertNotIn("Running", self.ok("status"))

    def test_status_with_nothing_and_with_no_database(self) -> None:
        self.assertIn("No timer running", self.ok("status"))
        self.assertFalse(self.db.exists(), "reading must not create the database")
        self.assertIn("No jobs yet", self.ok("summary"))
        self.assertFalse(self.db.exists())

    def test_errors_exit_nonzero_with_one_plain_line(self) -> None:
        self.assertIn("no such job", self.fails("start", "Ghost"))
        self.assertIn("no timer is running", self.fails("stop"))
        self.fails("add", "Ghost", "2026-03-09T09:00", "2026-03-09T10:00")
        self.fails("payout", "Ghost", "5")
        self.fails("log", "Ghost", "hello")
        self.fails("add", "Ghost", "yesterday", "today")
        self.fails("nonsense")

    def test_a_broken_database_is_one_line_not_a_traceback(self) -> None:
        self.ok("jobs", "add", "A")
        self.db.write_bytes(b"not a database at all" * 100)
        err = self.fails("summary")
        self.assertIn("error:", err)

    def test_a_forgotten_timer_is_flagged_and_closed_with_an_explicit_end(self) -> None:
        from datetime import UTC, datetime, timedelta

        from pionir.config import PionirSettings
        from pionir.worklog import WorkLog, worklog_for

        self.ok("jobs", "add", "Day job", "--rate", "20")
        settings = PionirSettings.from_environment()
        old = datetime.now(UTC) - timedelta(hours=14)
        worklog_for(settings, clock=lambda: old).start_timer("Day job")
        self.assertIn("FLAGGED", self.ok("status"))
        self.assertIn("FORGOTTEN", self.ok("summary"))
        self.fails("stop", "Day job")                                # over 12h: needs an explicit end
        end = (old + timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertIn("8:00:00", self.ok("stop", "Day job", "--end", end))
        self.assertIsInstance(worklog_for(settings), WorkLog)


if __name__ == "__main__":
    unittest.main()
