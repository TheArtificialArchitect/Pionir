"""The work log: hours and pay for the day job and freelance work.

Real SQLite in a temp dir (never ~/.pionir), a fake clock, and a fixed IANA zone so the DST
and midnight cases are the same on every machine.
"""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pionir import worklog
from pionir.worklog import WorkError, WorkLog, Zone


class Clock:
    def __init__(self, at: str) -> None:
        self.at = datetime.fromisoformat(at)

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **kw) -> None:
        self.at += timedelta(**kw)


class WorkTestCase(unittest.TestCase):
    NOW = "2026-03-11T15:00:00Z"        # a Wednesday, well after the DST change
    ZONE = "America/New_York"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "worklog" / "worklog.db"
        self.clock = Clock(self.NOW)
        self.log = WorkLog(self.path, clock=self.clock, zone=Zone(self.ZONE))
        self.da = self.log.create_job("Data Annotation", "freelance", hourly_rate_cents=2500)
        self.day = self.log.create_job("Day job", "employment", hourly_rate_cents=2000)

    def refused(self, code: str, fn, *a, **kw) -> WorkError:
        with self.assertRaises(WorkError) as ctx:
            fn(*a, **kw)
        self.assertEqual(ctx.exception.code, code, str(ctx.exception))
        return ctx.exception

    def week(self, job: str = "Data Annotation") -> dict:
        return next(j for j in self.log.summary()["jobs"] if j["name"] == job)["periods"]


class MoneyTests(unittest.TestCase):
    def test_cents_are_integers_and_round_half_up(self) -> None:
        self.assertEqual(worklog.earned_cents(3600, 2500), 2500)
        self.assertEqual(worklog.earned_cents(1800, 2500), 1250)
        self.assertEqual(worklog.earned_cents(1, 2500), 1)        # 0.69 cents rounds up
        self.assertEqual(worklog.earned_cents(0, 2500), 0)
        self.assertEqual(worklog.earned_cents(3, 1201), 1)        # 1.0028
        self.assertIsInstance(worklog.earned_cents(5400, 1999), int)
        # three-thirds do not drift: one total, rounded once
        self.assertEqual(worklog.earned_cents(3 * 1200, 1999), worklog.earned_cents(3600, 1999))

    def test_dollars_parse_without_floats(self) -> None:
        self.assertEqual(worklog.parse_cents("25"), 2500)
        self.assertEqual(worklog.parse_cents("$1,234.5"), 123450)
        self.assertEqual(worklog.parse_cents("0.07"), 7)
        self.assertEqual(worklog.parse_cents("19.99"), 1999)          # 19.99 * 100 is 1998.999.. as a float
        for bad in ("", "abc", "1.234", "-5", "1e3", "12.", None, True, 1.5):
            with self.assertRaises(WorkError, msg=repr(bad)):
                worklog.parse_cents(bad)

    def test_hours_display_is_from_integers(self) -> None:
        self.assertEqual(worklog.hours(3600), 1.0)
        self.assertEqual(worklog.hours(5400), 1.5)
        self.assertEqual(worklog.hours(2), 0.0)


class JobTests(WorkTestCase):
    def test_no_rate_is_no_earnings_not_a_guess(self) -> None:
        self.log.create_job("Unrated", "freelance")
        self.log.add_session("Unrated", "2026-03-11T13:00:00Z", "2026-03-11T14:00:00Z")
        p = self.week("Unrated")
        self.assertEqual(p["today"]["seconds"], 3600)
        self.assertIsNone(p["today"]["earned_cents"])
        job = next(j for j in self.log.summary()["jobs"] if j["name"] == "Unrated")
        self.assertFalse(job["rate_set"])
        self.assertEqual(self.log.summary()["totals"]["today"]["unrated_seconds"], 3600)

    def test_names_are_unique_bounded_and_not_ids(self) -> None:
        self.refused("conflict", self.log.create_job, "data annotation")
        self.refused("bad_input", self.log.create_job, "12345")
        self.refused("bad_input", self.log.create_job, "x" * 61)
        self.refused("bad_input", self.log.create_job, "   ")
        self.refused("bad_input", self.log.create_job, "a\x00b")
        self.refused("bad_input", self.log.create_job, "Job", "gig")
        self.refused("bad_input", self.log.create_job, "Job", hourly_rate_cents=2.5)
        self.refused("bad_input", self.log.create_job, "Job", hourly_rate_cents=-1)
        self.refused("bad_input", self.log.create_job, "Job", hourly_rate_cents=worklog.MAX_RATE_CENTS + 1)
        self.refused("bad_input", self.log.create_job, "Job", hourly_rate_cents=True)
        self.refused("bad_input", self.log.create_job, "Job", set_aside_pct=101)
        self.refused("bad_input", self.log.create_job, "Job", currency="DOLLARS")

    def test_job_count_is_capped(self) -> None:
        for i in range(worklog.MAX_JOBS - 2):
            self.log.create_job(f"Job {i}x")
        self.refused("limit", self.log.create_job, "One too many")

    def test_update_sets_and_clears_the_rate(self) -> None:
        job = self.log.update_job("Day job", hourly_rate_cents=None)
        self.assertFalse(job["rate_set"])
        job = self.log.update_job(self.day["id"], hourly_rate_cents=2200, set_aside_pct=25)
        self.assertEqual((job["hourly_rate_cents"], job["set_aside_pct"]), (2200, 25))
        self.refused("conflict", self.log.update_job, "Day job", name="Data Annotation")
        self.refused("not_found", self.log.update_job, "nothing")
        self.assertEqual(self.log.update_job("Day job", name="Day job")["name"], "Day job")

    def test_an_inactive_job_cannot_start_a_timer(self) -> None:
        self.log.update_job("Day job", active=False)
        self.refused("conflict", self.log.start_timer, "Day job")
        self.refused("bad_input", self.log.update_job, "Day job", active="no")


class SessionValidationTests(WorkTestCase):
    def test_end_must_follow_start(self) -> None:
        self.refused("bad_input", self.log.add_session, "Day job", "2026-03-11T13:00:00Z", "2026-03-11T13:00:00Z")
        self.refused("bad_input", self.log.add_session, "Day job", "2026-03-11T14:00:00Z", "2026-03-11T13:00:00Z")

    def test_a_session_over_sixteen_hours_is_refused(self) -> None:
        s = self.log.add_session("Day job", "2026-03-10T14:00:00Z", "2026-03-11T06:00:00Z")   # exactly 16h
        self.assertEqual(s["seconds"], 16 * 3600)
        self.refused("too_long", self.log.add_session, "Data Annotation", "2026-03-10T13:59:59Z", "2026-03-11T06:00:00Z")

    def test_the_future_and_the_distant_past_are_refused(self) -> None:
        self.refused("bad_input", self.log.add_session, "Day job", "2026-03-11T16:00:00Z", "2026-03-11T17:00:00Z")
        self.refused("bad_input", self.log.add_session, "Day job", "2026-03-11T14:00:00Z", "2026-03-11T15:05:00Z")
        self.refused("bad_input", self.log.add_session, "Day job", "1999-01-01T00:00:00Z", "1999-01-01T01:00:00Z")

    def test_overlap_within_a_job_is_refused_and_touching_is_not(self) -> None:
        self.log.add_session("Day job", "2026-03-11T10:00:00Z", "2026-03-11T12:00:00Z")
        for a, b in (("09:00", "10:30"), ("11:00", "13:00"), ("10:30", "11:30"), ("09:00", "13:00")):
            self.refused("overlap", self.log.add_session, "Day job", f"2026-03-11T{a}:00Z", f"2026-03-11T{b}:00Z")
        self.log.add_session("Day job", "2026-03-11T12:00:00Z", "2026-03-11T13:00:00Z")   # touches the end
        self.log.add_session("Day job", "2026-03-11T09:00:00Z", "2026-03-11T10:00:00Z")   # touches the start

    def test_two_jobs_may_overlap_each_other(self) -> None:
        self.log.add_session("Day job", "2026-03-11T10:00:00Z", "2026-03-11T12:00:00Z")
        self.log.add_session("Data Annotation", "2026-03-11T11:00:00Z", "2026-03-11T13:00:00Z")

    def test_a_manual_session_cannot_land_inside_an_open_timer(self) -> None:
        self.clock.advance(hours=-3)
        self.log.start_timer("Day job")                  # 12:00Z
        self.clock.advance(hours=3)
        self.refused("overlap", self.log.add_session, "Day job", "2026-03-11T13:00:00Z", "2026-03-11T14:00:00Z")
        self.log.add_session("Day job", "2026-03-11T10:00:00Z", "2026-03-11T11:00:00Z")   # before it: fine

    def test_naive_text_is_local_time_and_a_dst_gap_is_refused(self) -> None:
        s = self.log.add_session("Day job", "2026-03-11T09:00", "2026-03-11T10:00")          # EDT: 13:00Z
        self.assertEqual(s["start_ts"], "2026-03-11T13:00:00Z")
        e = self.refused("bad_input", self.log.add_session, "Day job", "2026-03-08T02:30", "2026-03-08T03:30")
        self.assertIn("does not exist", str(e))
        self.refused("bad_input", self.log.add_session, "Day job", "yesterday", "today")
        self.refused("bad_input", self.log.add_session, "Day job", 5, 6)
        self.refused("bad_input", self.log.add_session, "Day job", "2026-03-11T09:00:00" + "0" * 40, "x")

    def test_the_http_way_refuses_a_time_without_a_zone(self) -> None:
        self.refused("bad_input", self.log.add_session, "Day job", "2026-03-11T09:00", "2026-03-11T10:00",
                     allow_naive=False)

    def test_edit_revalidates_and_soft_delete_frees_the_slot(self) -> None:
        a = self.log.add_session("Day job", "2026-03-11T10:00:00Z", "2026-03-11T11:00:00Z")
        b = self.log.add_session("Day job", "2026-03-11T12:00:00Z", "2026-03-11T13:00:00Z")
        self.refused("overlap", self.log.edit_session, b["id"], start="2026-03-11T10:30:00Z")
        self.refused("bad_input", self.log.edit_session, b["id"], end="2026-03-11T11:00:00Z")
        self.assertEqual(self.log.edit_session(b["id"], end="2026-03-11T13:30:00Z", note="ok")["seconds"], 5400)
        self.log.edit_session(a["id"], start="2026-03-11T09:00:00Z")           # editing itself is not overlap
        self.log.delete_session(a["id"])
        self.log.add_session("Day job", "2026-03-11T09:30:00Z", "2026-03-11T10:30:00Z")
        self.refused("not_found", self.log.edit_session, a["id"], note="x")
        self.refused("not_found", self.log.delete_session, 9999)
        self.refused("bad_input", self.log.delete_session, "1; drop table sessions")


class TimerTests(WorkTestCase):
    def test_start_and_stop_makes_one_session(self) -> None:
        s = self.log.start_timer("Data Annotation")
        self.assertTrue(s["open"])
        self.refused("already_open", self.log.start_timer, "Data Annotation")
        self.clock.advance(hours=2, minutes=30)
        self.assertEqual(self.log.summary()["jobs"][0]["periods"]["today"]["seconds"], 9000)   # counts while running
        done = self.log.stop_timer("Data Annotation", note="rated a batch")
        self.assertEqual((done["seconds"], done["source"], done["note"]), (9000, "timer", "rated a batch"))
        self.refused("not_open", self.log.stop_timer, "Data Annotation")

    def test_only_one_open_session_per_job_even_around_the_api(self) -> None:
        self.log.start_timer("Day job")
        with closing(sqlite3.connect(self.path)) as raw, self.assertRaises(sqlite3.IntegrityError):
            raw.execute("INSERT INTO sessions(job_id, start_ts, source, tz, created_ts, updated_ts)"
                        " VALUES(?, '2026-03-11T14:00:00Z', 'timer', 'x', 'x', 'x')", (self.day["id"],))

    def test_a_second_jobs_timer_needs_saying_so(self) -> None:
        self.log.start_timer("Day job")
        e = self.refused("other_timer_open", self.log.start_timer, "Data Annotation")
        self.assertIn("Day job", str(e))
        self.log.start_timer("Data Annotation", allow_concurrent=True)
        self.assertEqual(len(self.log.summary()["timers"]), 2)

    def test_a_forgotten_timer_is_flagged_and_not_counted(self) -> None:
        self.log.start_timer("Day job")
        self.clock.advance(hours=13)
        s = self.log.summary()
        self.assertEqual(s["totals"]["all"]["seconds"], 0)                      # not silently counted
        self.assertEqual(s["jobs"][1]["periods"]["today"]["earned_cents"], 0)
        self.assertEqual(len(s["flags"]), 1)
        self.assertEqual(s["flags"][0]["job"], "Day job")
        self.assertTrue(s["timers"][0]["flagged"])
        self.assertEqual(self.log.status()["flagged_timers"], 1)
        self.refused("stale_timer", self.log.stop_timer, "Day job")              # a plain stop will not guess
        self.clock.advance(hours=-13)
        s = self.log.summary()
        self.assertEqual(s["flags"], [])

    def test_a_stale_timer_closes_with_an_explicit_end(self) -> None:
        self.log.start_timer("Day job")                                          # 15:00Z
        self.clock.advance(hours=14)
        done = self.log.stop_timer("Day job", end="2026-03-11T19:00:00Z")
        self.assertEqual(done["seconds"], 4 * 3600)
        self.assertEqual(self.log.summary()["totals"]["all"]["seconds"], 4 * 3600)

    def test_an_explicit_end_is_still_checked(self) -> None:
        self.log.start_timer("Day job")
        self.clock.advance(hours=1)
        self.refused("bad_input", self.log.stop_timer, "Day job", end="2026-03-11T14:00:00Z")   # before start
        self.refused("bad_input", self.log.stop_timer, "Day job", end="2026-03-11T18:00:00Z")   # future
        self.assertTrue(self.log.list_sessions("Day job")[0]["open"])                          # nothing changed

    def test_an_open_timer_past_sixteen_hours_cannot_be_closed_at_now(self) -> None:
        self.log.start_timer("Day job")
        self.clock.advance(hours=17)
        self.refused("stale_timer", self.log.stop_timer, "Day job")
        self.refused("too_long", self.log.stop_timer, "Day job", end="2026-03-12T08:00:00Z")     # 17h
        self.log.stop_timer("Day job", end="2026-03-12T06:00:00Z")                               # 15h

    def test_the_timer_can_be_deleted_and_started_again(self) -> None:
        s = self.log.start_timer("Day job")
        self.log.delete_session(s["id"])
        self.log.start_timer("Day job")


class SummaryTests(WorkTestCase):
    def test_a_session_across_local_midnight_splits_between_the_days(self) -> None:
        # 23:00 to 01:00 New York (EDT) on the 10th to the 11th = 03:00Z to 05:00Z on the 11th.
        self.clock.at = datetime(2026, 3, 11, 12, 0, tzinfo=UTC)
        self.log.add_session("Day job", "2026-03-10T23:00", "2026-03-11T01:00")
        today = self.week("Day job")["today"]
        self.assertEqual(today["seconds"], 3600)                                  # only the hour after midnight
        self.assertEqual(self.week("Day job")["week"]["seconds"], 7200)
        self.assertEqual(self.week("Day job")["month"]["seconds"], 7200)
        self.clock.advance(days=-1)
        self.assertEqual(self.log.summary()["jobs"][1]["periods"]["today"]["seconds"], 3600)   # the other half

    def test_a_session_across_the_week_and_month_edges(self) -> None:
        # Sunday 2026-03-01 23:00 -> Monday 2026-03-02 (also the 1st of a week, not month) 01:00 local.
        self.clock.at = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
        self.log.add_session("Day job", "2026-03-01T23:00", "2026-03-02T01:00")
        p = self.week("Day job")
        self.assertEqual((p["week"]["seconds"], p["month"]["seconds"]), (3600, 7200))
        self.clock.at = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
        self.assertEqual(self.week("Day job")["month"]["seconds"], 0)
        self.assertEqual(self.week("Day job")["year"]["seconds"], 7200)

    def test_a_dst_day_is_23_or_25_hours_long(self) -> None:
        spring = self.log.period_bounds(datetime(2026, 3, 8, 17, 0, tzinfo=UTC))["today"]
        self.assertEqual((spring.hi - spring.lo).total_seconds(), 23 * 3600)
        fall = self.log.period_bounds(datetime(2026, 11, 1, 17, 0, tzinfo=UTC))["today"]
        self.assertEqual((fall.hi - fall.lo).total_seconds(), 25 * 3600)
        week = self.log.period_bounds(datetime(2026, 3, 9, 17, 0, tzinfo=UTC))["week"]
        self.assertEqual(week.lo, datetime(2026, 3, 9, 4, 0, tzinfo=UTC))       # Monday midnight EDT
        spring_week = self.log.period_bounds(datetime(2026, 3, 8, 17, 0, tzinfo=UTC))["week"]
        self.assertEqual((spring_week.hi - spring_week.lo).total_seconds(), 7 * 24 * 3600 - 3600)

    def test_a_session_over_the_spring_gap_counts_real_elapsed_time(self) -> None:
        # 01:30 EST to 03:30 EDT is one hour of clock time, not two.
        self.clock.at = datetime(2026, 3, 8, 18, 0, tzinfo=UTC)
        s = self.log.add_session("Day job", "2026-03-08T06:30:00Z", "2026-03-08T07:30:00Z")
        self.assertEqual(s["seconds"], 3600)
        self.assertEqual(self.week("Day job")["today"]["seconds"], 3600)

    def test_a_session_over_the_fall_repeat_counts_real_elapsed_time(self) -> None:
        # 01:30 EDT to 01:30 EST: the same wall time an hour apart.
        self.clock.at = datetime(2026, 11, 1, 18, 0, tzinfo=UTC)
        s = self.log.add_session("Day job", "2026-11-01T05:30:00Z", "2026-11-01T06:30:00Z")
        self.assertEqual(s["seconds"], 3600)
        # a session from midnight to the end of the 25h day is bounded by the cap, but a 10h
        # shift through the repeat is 10 real hours
        self.log.add_session("Day job", "2026-11-01T04:00:00Z", "2026-11-01T05:00:00Z")
        self.assertEqual(self.week("Day job")["today"]["seconds"], 7200)

    def test_earnings_round_once_per_period_not_per_session(self) -> None:
        self.log.update_job("Day job", hourly_rate_cents=1999)
        for i in range(3):
            self.log.add_session("Day job", f"2026-03-11T0{i + 1}:00:00Z", f"2026-03-11T0{i + 1}:20:00Z")
        # 3600s total: exactly one hour
        self.assertEqual(self.week("Day job")["month"]["earned_cents"], 1999)

    def test_effective_rate_is_payouts_over_hours_for_freelance_only(self) -> None:
        self.log.add_session("Data Annotation", "2026-03-11T10:00:00Z", "2026-03-11T14:00:00Z")   # 4h
        self.assertIsNone(self.week()["today"]["effective_hourly_cents"])                         # no payout yet
        self.log.add_log("Data Annotation", "payout", amount_cents=10500)
        p = self.week()
        self.assertEqual(p["today"]["payout_cents"], 10500)
        self.assertEqual(p["today"]["effective_hourly_cents"], 2625)
        self.assertNotIn("effective_hourly_cents", self.week("Day job")["today"])

    def test_set_aside_is_informational_and_unset_by_default(self) -> None:
        self.log.add_session("Data Annotation", "2026-03-11T10:00:00Z", "2026-03-11T14:00:00Z")
        self.log.add_log("Data Annotation", "payout", amount_cents=10000)
        self.assertNotIn("set_aside_cents", self.week()["today"])
        self.log.update_job("Data Annotation", set_aside_pct=30)
        self.assertEqual(self.week()["today"]["set_aside_cents"], 3000)

    def test_totals_never_add_across_currencies_and_flag_unrated_time(self) -> None:
        self.log.create_job("Abroad", "freelance", hourly_rate_cents=1000, currency="cad")
        self.log.add_session("Abroad", "2026-03-11T10:00:00Z", "2026-03-11T11:00:00Z")
        self.log.add_session("Day job", "2026-03-11T10:00:00Z", "2026-03-11T11:00:00Z")
        t = self.log.summary()["totals"]["today"]
        self.assertEqual(t["earned_cents_by_currency"], {"CAD": 1000, "USD": 2000})
        self.assertEqual(t["seconds"], 7200)

    def test_the_weekly_rollup_has_twelve_weeks_oldest_first(self) -> None:
        self.log.add_session("Day job", "2026-03-02T10:00:00Z", "2026-03-02T12:00:00Z")
        weekly = next(j for j in self.log.summary()["jobs"] if j["name"] == "Day job")["weekly"]
        self.assertEqual(len(weekly), worklog.WEEKLY_ROLLUP_WEEKS)
        self.assertEqual(weekly[-1]["week_start"], "2026-03-09")
        self.assertEqual(weekly[-2], {"week_start": "2026-03-02", "seconds": 7200, "hours": 2.0,
                                      "earned_cents": 4000, "payout_cents": 0})

    def test_no_streaks_no_gamification(self) -> None:
        blob = json.dumps(self.log.summary()).lower()
        for word in ("streak", "badge", "goal", "level"):
            self.assertNotIn(word, blob)

    def test_deleted_sessions_and_payouts_leave_every_total(self) -> None:
        s = self.log.add_session("Data Annotation", "2026-03-11T10:00:00Z", "2026-03-11T12:00:00Z")
        p = self.log.add_log("Data Annotation", "payout", amount_cents=5000)
        self.log.delete_session(s["id"])
        self.log.delete_log(p["id"])
        today = self.week()["today"]
        self.assertEqual((today["seconds"], today["payout_cents"]), (0, 0))
        self.assertEqual(self.log.list_sessions("Data Annotation"), [])
        self.assertEqual(len(self.log.list_sessions("Data Annotation", include_deleted=True)), 1)
        self.assertEqual(self.log.list_log("Data Annotation", "payout"), [])


class SoftDeleteAuditTests(WorkTestCase):
    def test_every_change_leaves_a_row_and_nothing_is_erased(self) -> None:
        s = self.log.add_session("Day job", "2026-03-11T10:00:00Z", "2026-03-11T11:00:00Z", "first")
        self.log.edit_session(s["id"], end="2026-03-11T11:30:00Z")
        self.log.delete_session(s["id"])
        raw = sqlite3.connect(self.path)
        self.addCleanup(raw.close)
        actions = [r[0] for r in raw.execute("SELECT action FROM changes WHERE entity='session' ORDER BY id")]
        self.assertEqual(actions, ["create", "edit", "delete"])
        before, = raw.execute("SELECT before FROM changes WHERE action='delete'").fetchone()
        self.assertEqual(json.loads(before)["end_ts"], "2026-03-11T11:30:00Z")
        self.assertEqual(raw.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)
        self.assertIsNotNone(raw.execute("SELECT deleted_ts FROM sessions").fetchone()[0])

    def test_a_refused_write_changes_nothing(self) -> None:
        self.log.add_session("Day job", "2026-03-11T10:00:00Z", "2026-03-11T11:00:00Z")
        raw = sqlite3.connect(self.path)
        self.addCleanup(raw.close)
        before = raw.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
        self.refused("overlap", self.log.add_session, "Day job", "2026-03-11T10:30:00Z", "2026-03-11T11:30:00Z")
        self.assertEqual(raw.execute("SELECT COUNT(*) FROM changes").fetchone()[0], before)

    def test_the_zone_name_is_stored_beside_the_utc_time(self) -> None:
        self.log.add_session("Day job", "2026-03-11T10:00:00Z", "2026-03-11T11:00:00Z")
        s = self.log.list_sessions("Day job")[0]
        self.assertEqual(s["tz"], self.ZONE)
        self.assertTrue(s["start_ts"].endswith("Z"))
        n = self.log.add_log("Day job", "note", "a generic note")
        self.assertEqual(n["tz"], self.ZONE)


class TextTests(WorkTestCase):
    def test_a_note_over_the_cap_is_refused_with_the_reminder(self) -> None:
        e = self.refused("too_long", self.log.add_log, "Data Annotation", "note", "x" * (worklog.NOTE_CAP + 1))
        self.assertIn("NDA", str(e))
        self.log.add_log("Data Annotation", "note", "x" * worklog.NOTE_CAP)
        self.refused("too_long", self.log.add_session, "Day job", "2026-03-11T10:00:00Z",
                     "2026-03-11T11:00:00Z", "y" * (worklog.NOTE_CAP + 1))
        self.refused("too_long", self.log.add_log, "Data Annotation", "rubric", "z" * (worklog.NOTE_CAP + 1))

    def test_text_that_looks_like_pasted_task_content_is_refused(self) -> None:
        for pasted in ("Prompt: write me a poem about the sea",
                       "Response A: The sea is wide.\nResponse B: The sea is blue.",
                       "User: hi\nAssistant: hello",
                       "\n".join(["line"] * 9)):
            self.refused("looks_like_task", self.log.add_log, "Data Annotation", "note", pasted)
            self.refused("looks_like_task", self.log.add_log, "Data Annotation", "rubric", pasted)
            self.refused("looks_like_task", self.log.add_session, "Day job", "2026-03-11T10:00:00Z",
                         "2026-03-11T11:00:00Z", pasted)
        self.refused("looks_like_task", self.log.create_job, "Prompt: secret task")

    def test_generic_tips_are_accepted(self) -> None:
        self.log.add_log("Data Annotation", "rubric", "Check factual claims before style; say why in one line.")
        self.log.add_log("Data Annotation", "note", "Batch of 12, slower than usual.")

    def test_secrets_are_scrubbed_before_they_are_stored(self) -> None:
        secret = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2"
        n = self.log.add_log("Data Annotation", "note", f"remember {secret} for later")
        self.assertNotIn(secret, n["text"])
        raw = sqlite3.connect(self.path)
        self.addCleanup(raw.close)
        self.assertNotIn(secret, json.dumps(list(raw.iterdump())))
        s = self.log.add_session("Day job", "2026-03-11T10:00:00Z", "2026-03-11T11:00:00Z", f"key {secret}")
        self.assertNotIn(secret, s["note"])
        self.refused("bad_input", self.log.create_job, secret)

    def test_empty_and_wrong_typed_text_is_refused(self) -> None:
        for bad in ("", "   ", None, 5, ["x"]):
            self.refused("bad_input", self.log.add_log, "Data Annotation", "note", bad)
        self.refused("bad_input", self.log.add_log, "Data Annotation", "note", "a\x00b")

    def test_a_payout_is_whole_cents_and_bounded(self) -> None:
        for bad in (None, 0, -5, 12.5, True, "100", worklog.MAX_PAYOUT_CENTS + 1):
            self.refused("bad_input", self.log.add_log, "Data Annotation", "payout", amount_cents=bad)
        self.refused("bad_input", self.log.add_log, "Data Annotation", "note", "hi", amount_cents=100)
        self.refused("bad_input", self.log.add_log, "Data Annotation", "tip", "hi")
        self.log.add_log("Data Annotation", "payout", "March batch", amount_cents=worklog.MAX_PAYOUT_CENTS)

    def test_a_log_time_in_the_future_is_refused(self) -> None:
        self.refused("bad_input", self.log.add_log, "Data Annotation", "note", "hi", ts="2026-03-12T00:00:00Z")


class ListAndBoundsTests(WorkTestCase):
    def test_ranges_and_limits_are_bounded(self) -> None:
        self.refused("bad_input", self.log.list_sessions, None, "2020-01-01T00:00:00Z", "2026-03-11T00:00:00Z")
        self.refused("bad_input", self.log.list_sessions, None, "2026-03-11T00:00:00Z", "2026-03-10T00:00:00Z")
        self.refused("bad_input", self.log.list_sessions, None, None, None, limit=worklog.MAX_LIST + 1)
        self.refused("bad_input", self.log.list_sessions, None, None, None, limit=0)
        self.refused("bad_input", self.log.list_sessions, None, None, None, limit=True)
        self.refused("bad_input", self.log.list_log, None, "chat")
        self.refused("not_found", self.log.list_sessions, "nobody")

    def test_sessions_list_newest_first_within_the_range(self) -> None:
        self.log.add_session("Day job", "2026-03-09T10:00:00Z", "2026-03-09T11:00:00Z")
        self.log.add_session("Day job", "2026-03-10T10:00:00Z", "2026-03-10T11:00:00Z")
        got = self.log.list_sessions("Day job", "2026-03-10T00:00:00Z", "2026-03-12T00:00:00Z")
        self.assertEqual([s["start_ts"] for s in got], ["2026-03-10T10:00:00Z"])
        both = self.log.list_sessions()
        self.assertEqual([s["start_ts"] for s in both], ["2026-03-10T10:00:00Z", "2026-03-09T10:00:00Z"])
        self.assertEqual(len(self.log.list_sessions(limit=1)), 1)


class StatusAndIsolationTests(unittest.TestCase):
    def test_reading_never_creates_the_file_and_status_says_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = WorkLog(Path(tmp) / "worklog" / "worklog.db", clock=Clock("2026-03-11T15:00:00Z"),
                          zone=Zone("America/New_York"))
            self.assertEqual(log.status(), {"present": False, "jobs": 0, "open_timers": 0,
                                            "flagged_timers": 0, "last_write": None})
            self.assertEqual((log.list_jobs(), log.list_sessions(), log.list_log()), ([], [], []))
            self.assertEqual(log.summary()["jobs"], [])
            self.assertEqual(log.aggregates()["jobs"], [])
            self.assertFalse((Path(tmp) / "worklog").exists())

    def test_status_counts_timers_and_reports_the_last_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock("2026-03-11T15:00:00Z")
            log = WorkLog(Path(tmp) / "w.db", clock=clock, zone=Zone("America/New_York"))
            log.create_job("Day job", "employment")
            log.start_timer("Day job")
            st = log.status()
            self.assertEqual((st["present"], st["jobs"], st["open_timers"], st["last_write"]),
                             (True, 1, 1, "2026-03-11T15:00:00Z"))
            self.assertEqual(set(st), {"present", "jobs", "open_timers", "flagged_timers", "last_write"})

    def test_an_unknown_zone_is_refused(self) -> None:
        with self.assertRaises(WorkError):
            Zone("Mars/Olympus")

    def test_the_machine_zone_works_too(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = WorkLog(Path(tmp) / "w.db")
            log.create_job("Day job", "employment", hourly_rate_cents=1000)
            log.start_timer("Day job")
            self.assertEqual(len(log.summary()["timers"]), 1)
            self.assertTrue(log.summary()["tz"])
            self.assertEqual(list(log.summary()["periods"]), ["today", "week", "month", "year"])

    def test_the_module_imports_nothing_of_memory(self) -> None:
        import ast
        tree = ast.parse(Path(worklog.__file__).read_text(encoding="utf-8"))
        ours = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level}
        ours |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
                 if a.name.startswith("pionir")}
        self.assertEqual(ours, {""}, "worklog.py may import only secretscrub from this package")


class AggregatesTests(WorkTestCase):
    CANARY = "CANARY-secret-task-words-zebra"

    def test_moss_view_has_numbers_and_names_but_no_text(self) -> None:
        self.log.add_log("Data Annotation", "note", f"{self.CANARY} note")
        self.log.add_log("Data Annotation", "rubric", f"{self.CANARY} rubric")
        self.log.add_log("Data Annotation", "payout", f"{self.CANARY} payout", amount_cents=4200)
        self.log.add_session("Data Annotation", "2026-03-11T10:00:00Z", "2026-03-11T12:00:00Z", f"{self.CANARY} s")
        self.log.start_timer("Day job")
        agg = self.log.aggregates()
        blob = json.dumps(agg)
        self.assertNotIn(self.CANARY, blob)
        self.assertNotIn("zebra", blob)
        self.assertTrue(agg["timer_running"])
        da = next(j for j in agg["jobs"] if j["job"] == "Data Annotation")
        self.assertEqual(da["today"], {"hours": 2.0, "earned_cents": 5000})
        self.assertFalse(da["timer_running"])
        self.assertTrue(next(j for j in agg["jobs"] if j["job"] == "Day job")["timer_running"])
        for j in agg["jobs"]:
            self.assertEqual(set(j), {"job", "kind", "currency", "rate_set", "timer_running",
                                      "today", "week", "month"})

    def test_the_stale_flag_reaches_moss_as_a_bool(self) -> None:
        self.log.start_timer("Day job")
        self.clock.advance(hours=13)
        self.assertTrue(self.log.aggregates()["stale_timer"])


if __name__ == "__main__":
    unittest.main()
