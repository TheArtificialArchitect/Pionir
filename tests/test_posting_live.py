"""The posting path must produce, and say so when it does not (2026-09-28).

Ian: "our posting bots are all idle ... No instagram posts today, nothing in the discord."
Every piece had reported "ok" on its own. What the output counters showed:

- the Instagram draft was made 24 hours after the last one, so its hour drifted a run later
  every day (23:04, 04:45, 10:26); the 10:26 draft missed the 09:00 digest and waited a day,
  and one day had no draft at all. The drafting is now timed for the digest the server
  stamps each batched approval with (``DigestSettings.next_digest``): the AGREEMENT between
  the producer's schedule and the consumer's is tested here, day by day;
- the blog's six drafts in three days were all blocked by the content check and not one
  reached the owner, while every run succeeded (each wrote a tally), so the vitals, the
  division health and the digest to Moss all read "ok". The posters now keep an output
  counter (``pulse``), a no-output alarm (vitals ``no_output``), and doctor shows it;
- two of those blocks named "JSON-to-PDF" and "JSON-Based": compounds of the very names the
  drafting prompt offers ("JSON", "PDF"), refused as unknown names;
- nothing Pionir-side said when the digest last reached Discord or whether the gate ran.

Each test here fails if its fix is reverted. No network, model, Pionir or Discord is real.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, time, timedelta
from pathlib import Path

from test_crew_blog import FakeBrain, FakeHands, bad, good
from test_crew_fakes import make_crew
from test_approval_batching import BatchCase

from pionir.batching import DigestSettings
from pionir.crew import contentcheck
from pionir.crew.blog import SEEDS, _allowlist_display
from pionir.crew.pool import jitter
from pionir.crew.registry import build_registry, default_registry, load_catalogue
from pionir.crew.result import Ok
from pionir.crew.vitals import Vitals
from pionir.crew.worker import WorkContext
from pionir.posting_health import digest_capabilities, posting_health

DIGEST = DigestSettings(enabled=True, at=time(9, 0), expire_days=7)
# a Monday 00:00 local, well clear of a daylight-saving change
START = datetime(2026, 9, 21, 0, 0).astimezone().timestamp()
DAY = 86400.0


def local(t: float) -> datetime:
    return datetime.fromtimestamp(t).astimezone()


def digest_date(t: float) -> str:
    """What Pionir's server stamps a batched approval parked at ``t`` with (server.py:
    ``self.digest.next_digest(local_now()).date()``)."""
    return DIGEST.next_digest(local(t)).date().isoformat()


class _Poster(unittest.TestCase):
    worker_id = "posting.blog"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.worker = default_registry().require(self.worker_id)
        self.hands = FakeHands()
        self.parked_at: list = []

    def run_at(self, now: float, brain, digest=DIGEST):
        before = len(self.hands.jobs)
        ctx = WorkContext(now=now, http=None, secrets_dir=self.state, words=brain,
                          job=self.hands.job, approval=self.hands.approval,
                          state_dir=self.state, digest=digest)
        result = self.worker.run(ctx)
        if len(self.hands.jobs) > before:
            self.parked_at.append(now)
        return result

    def days_of_runs(self, phase: float, days: int, brain, digest=DIGEST) -> dict:
        """Run the worker as the dispatcher does (every cadence x its jitter) for ``days``
        days from START + ``phase``; -> {digest date: [times a post was parked]}."""
        step = self.worker.cadence_seconds * jitter(self.worker_id)
        parked: dict = {}
        t = START + phase
        while t < START + days * DAY:
            before = len(self.hands.jobs)
            self.run_at(t, brain, digest)
            if len(self.hands.jobs) > before:
                parked.setdefault(digest_date(t), []).append(t)
            t += step
        return parked


class DraftTimingAgreementTests(_Poster):
    """The producer (the poster's schedule) and the consumer (the digest the server puts
    each parked post in) must agree: one post in EVERY morning's digest, none late."""

    def test_every_digest_gets_exactly_one_post_whatever_hour_the_crew_started(self) -> None:
        for phase_h in (0, 3.5, 7.9, 10.4, 13, 17.2, 22.9):
            with self.subTest(started_at_hour=phase_h):
                self.setUp()
                parked = self.days_of_runs(phase_h * 3600, 8, FakeBrain(*[good()] * 20))
                days = sorted(parked)
                first = datetime.fromisoformat(days[0]).date()
                # from the first digest it could reach, every day's digest has one post
                expect = [(first + timedelta(days=i)).isoformat() for i in range(len(days))]
                self.assertEqual(days, expect, parked)
                self.assertTrue(all(len(v) == 1 for v in parked.values()), parked)
                self.assertGreaterEqual(len(days), 7, parked)

    def test_the_live_sequence_no_longer_skips_a_digest(self) -> None:
        # posting.instagram, 2026-09-27/28: a draft at 04:45 (in that morning's digest),
        # then runs at 10:42, 16:38, 22:34, 04:30 and 10:26. The 24-hour rule drafted at
        # 10:26 the next day - after the 09:00 digest - so the 28th's digest had nothing.
        day0 = START + DAY
        brain = FakeBrain(good(), good(), good())
        self.run_at(day0 + 4 * 3600 + 45 * 60, brain)
        for hm in ((10, 42), (16, 38), (22, 34), (28, 30), (34, 26)):
            self.run_at(day0 + hm[0] * 3600 + hm[1] * 60, brain)
        parked = [digest_date(t) for t in self.parked_at]
        next_day = local(day0 + DAY).date().isoformat()
        self.assertIn(next_day, parked, parked)
        self.assertEqual(len(parked), len(set(parked)), parked)

    def test_without_the_digest_it_is_still_one_draft_a_day(self) -> None:
        brain = FakeBrain(*[good()] * 20)
        parked = self.days_of_runs(0, 5, brain, digest=None)
        self.assertLessEqual(sum(len(v) for v in parked.values()), 5)
        times = sorted(t for v in parked.values() for t in v)
        self.assertTrue(all(b - a >= DAY for a, b in zip(times, times[1:])), times)

    def test_the_crew_hands_its_workers_the_servers_digest_settings(self) -> None:
        from pionir.config import PionirSettings
        from pionir.crew.config import CrewSettings
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        (Path(root) / "discord").mkdir()
        (Path(root) / "discord" / "config.json").write_text(
            json.dumps({"digest_time": "07:30"}), encoding="utf-8")
        pionir = PionirSettings(state_root=Path(root))
        cfg = CrewSettings.from_pionir(pionir, secrets_dir=Path(root) / "s")
        server_side = DigestSettings.from_environment(Path(root))
        self.assertEqual(cfg.digest, server_side)
        self.assertEqual(cfg.public()["digest"],
                         {"enabled": server_side.enabled, "time": server_side.time_text})
        crew = make_crew(root, digest=cfg.digest)
        self.addCleanup(crew.stop)
        w = crew.registry.all()[0]
        self.assertIs(crew.context_for(w).digest, cfg.digest)


class PulseTests(_Poster):
    def test_a_blog_whose_every_draft_is_blocked_raises_no_output_though_its_runs_succeed(
            self) -> None:
        brain = FakeBrain(*[bad()] * 20)
        t = START
        while t < START + 3 * DAY:
            result = self.run_at(t, brain)
            self.assertIsInstance(result, Ok, result)    # green, every run
            t += 6 * 3600
        pulse = self.worker.pulse(self.state, t, DIGEST)
        written = pulse["drafts_written"]
        self.assertGreaterEqual(written, 6)
        self.assertEqual((pulse["drafts_blocked"], pulse["submitted"]), (written, 0))
        self.assertIsNotNone(pulse["alert"])
        self.assertIn("EVER", pulse["alert"])
        self.assertIn(f"{written} blocked", pulse["alert"])
        self.assertTrue(pulse["last_block_reasons"])

    def test_a_poster_that_reached_the_owner_in_its_window_is_quiet(self) -> None:
        self.run_at(START + 3 * 3600, FakeBrain(good()))
        pulse = self.worker.pulse(self.state, START + DAY, DIGEST)
        self.assertIsNone(pulse["alert"])
        self.assertEqual((pulse["submitted"], pulse["pending"]), (1, 1))
        self.assertIsNotNone(pulse["last_submitted_at"])
        late = START + 3 * 3600 + self.worker.output_window_seconds() + 60
        self.assertIn("last post reached the owner", self.worker.pulse(self.state, late,
                                                                        DIGEST)["alert"])

    def test_a_young_poster_is_not_accused(self) -> None:
        self.run_at(START, FakeBrain(bad(), bad()))
        self.assertIsNone(self.worker.pulse(self.state, START + 6 * 3600, DIGEST)["alert"])

    def test_a_record_from_before_first_run_at_still_dates_its_first_day(self) -> None:
        # the live records predate first_run_at: their oldest entry is the first day
        rec = self.worker.load(self.state)
        rec["blocked"] = [{"draft_id": "d", "topic": SEEDS[0].key, "reasons": ["x"],
                           "attempt": 1, "at": START}]
        rec["last_drafted_at"] = START + 2 * DAY
        rec["counts"] = {"drafts_written": 6, "drafts_blocked": 6}
        self.worker.save(self.state, rec)
        self.assertIsNotNone(self.worker.pulse(self.state, START + 2.5 * DAY, DIGEST)["alert"])

    def test_a_poster_with_no_topic_left_says_so(self) -> None:
        rec = self.worker.load(self.state)
        rec["used_topics"] = [t.key for t in SEEDS]
        self.worker.save(self.state, rec)
        self.assertIn("every topic", self.worker.pulse(self.state, START, DIGEST)["alert"])

    def test_devto_waits_on_the_blog_and_says_so_without_an_alarm_of_its_own(self) -> None:
        devto = default_registry().require("posting.devto")
        pulse = devto.pulse(self.state, START, DIGEST)
        self.assertEqual((pulse["blog_posts_published"], pulse["waits_on"], pulse["alert"]),
                         (0, "posting.blog", None))


class VitalsNoOutputTests(unittest.TestCase):
    class _Store:
        def health(self, cadences, now):
            return []

    def test_a_no_output_alarm_is_raised_and_cleared(self) -> None:
        quiet = {"posting.blog": "no post has EVER reached the owner"}
        v = Vitals(self._Store(), dict, clock=lambda: 0.0, outputs=lambda: dict(quiet))
        with self.assertLogs("pionir.crew", level="WARNING") as logs:
            report = v.check(force=True)
        self.assertEqual([(r["who"], r["check"]) for r in report],
                         [("posting.blog", "no_output")])
        self.assertIn("NO OUTPUT", "\n".join(logs.output))
        quiet.clear()
        self.assertEqual(v.check(force=True), [])
        self.assertEqual(v.cleared, 1)


class CrewHealthTests(unittest.TestCase):
    """The crew's /api/health (doctor's specialists.crew) and Moss's digest both say it."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cat = load_catalogue()
        cat["divisions"] = [d for d in cat["divisions"] if d["id"] == "posting"]
        self.now = START + 3 * DAY
        self.crew = make_crew(tmp.name, registry=build_registry(cat), digest=DIGEST,
                              now=lambda: self.now)
        self.addCleanup(self.crew.stop)
        blog = self.crew.registry.require("posting.blog")
        state = self.crew.cfg.state_dir / "workers"
        rec = blog.load(state)
        rec.update(first_run_at=START, last_drafted_at=START + 2 * DAY,
                   counts={"drafts_written": 6, "drafts_blocked": 6})
        blog.save(state, rec)

    def test_health_carries_the_posters_pulses_and_the_open_alarms(self) -> None:
        self.crew.vitals.check(force=True)
        doc = self.crew.api_health()
        pulses = {p["worker"]: p for p in doc["posting"]}
        self.assertEqual(set(pulses), {"posting.blog", "posting.instagram", "posting.devto"})
        self.assertIsNotNone(pulses["posting.blog"]["alert"])
        self.assertIn(("posting.blog", "no_output"),
                      {(a["who"], a["check"]) for a in doc["alerts"]})
        json.dumps(doc)                                   # it goes out as JSON

    def test_the_division_health_and_moss_digest_do_not_call_it_ok(self) -> None:
        (div,) = [d for d in self.crew.direction.divisions() if d["division"] == "posting"]
        self.assertIn("posting.blog", div["health"]["no_output"])
        self.assertNotIn("posting.blog", div["health"]["ok"])
        (entry,) = [e for e in self.crew.direction.digest()["divisions"]
                    if e["division"] == "posting"]
        self.assertIn("posting.blog", entry["no_output"])
        self.assertEqual(entry["attention"], "watch")


class CompoundNameTests(unittest.TestCase):
    """The drafting prompt offers the allowlisted names; the check must accept what the
    model builds from them, and still refuse invented ids and names."""

    @staticmethod
    def names(text: str) -> list:
        return contentcheck._names_in([("body_md", text)])

    def test_compounds_of_offered_names_pass(self) -> None:
        for text in ("Use a JSON-to-PDF step here.", "A JSON-Based layout works.",
                     "It is a JSON-based layout.", "The PDF-Ready output is fine."):
            with self.subTest(text=text):
                self.assertEqual(self.names(text), [])

    def test_every_offered_name_can_be_joined_to_another(self) -> None:
        offered = [n for n in _allowlist_display() if " " not in n]
        for name in offered:
            with self.subTest(name=name):
                self.assertEqual(self.names(f"Use the {name}-to-JSON step."), [], name)
                self.assertEqual(self.names(f"A {name}-based way."), [], name)

    def test_invented_ids_and_names_still_block(self) -> None:
        for text, name in (("See INV-2024-001 here.", "INV-2024-001"),
                           ("See REF-12345 here.", "REF-12345"),
                           ("Ask Jean-Luc about it.", "Jean-Luc"),
                           ("It is JSON-Corp work.", "JSON-Corp"),
                           ("Use JSON-001 here.", "JSON-001")):
            with self.subTest(text=text):
                self.assertTrue(any(name in r for r in self.names(text)), self.names(text))


class PostingHealthTests(unittest.TestCase):
    NOW = datetime(2026, 9, 28, 17, 50).astimezone()
    CAPS = [{"name": "social.instagram_post", "approval": "digest"},
            {"name": "content.publish", "approval": "digest"},
            {"name": "client.email", "approval": "card"}]

    def rows(self) -> list:
        return [
            {"id": "a1", "capability": "social.instagram_post", "status": "approved",
             "created_at": "2026-09-27T11:45:56+00:00", "resolved_at": "2026-09-27T18:28:53+00:00",
             "result": {"ok": True, "result": {"ok": True, "permalink": "p"}}},
            {"id": "a2", "capability": "social.instagram_post", "status": "pending",
             "batch": True, "digest_date": "2026-09-29", "created_at": "2026-09-28T17:26:22+00:00"},
        ]

    def gate(self, **over) -> dict:
        state = {"running": True, "configured": True, "enabled": True, "last_error": None,
                 "digest": {"last_run_at": self.NOW.replace(hour=9, minute=0).isoformat(),
                            "last_sent_at": "2026-09-27T16:00:04+00:00",
                            "last_run": {"date": "2026-09-28", "items": 0, "complete": True}}}
        state.update(over)
        return state

    def test_it_reports_each_step_and_is_quiet_when_all_is_well(self) -> None:
        view = posting_health(rows=self.rows(), capabilities=self.CAPS, digest=DIGEST,
                              gate=self.gate(), now_local=self.NOW)
        ig = view["capabilities"]["social.instagram_post"]
        self.assertEqual((ig["pending"], ig["last_published_at"], ig["last_parked_at"]),
                         (1, "2026-09-27T18:28:53+00:00", "2026-09-28T17:26:22+00:00"))
        self.assertEqual(view["capabilities"]["content.publish"]["last_parked_at"], None)
        self.assertNotIn("client.email", view["capabilities"])
        self.assertEqual(view["digest"]["batched_pending"], 1)
        self.assertEqual(view["digest"]["last_run"]["items"], 0)
        self.assertEqual(view["alerts"], [])

    def test_a_gate_that_is_not_running_is_an_alarm(self) -> None:
        for gate in (None, self.gate(running=False, last_error="token rejected")):
            view = posting_health(rows=self.rows(), capabilities=self.CAPS, digest=DIGEST,
                                  gate=gate, now_local=self.NOW)
            self.assertTrue(any("NOT running" in a for a in view["alerts"]), view["alerts"])

    def test_a_missed_digest_is_an_alarm(self) -> None:
        gate = self.gate()
        gate["digest"]["last_run_at"] = "2026-09-27T09:00:02-07:00"
        view = posting_health(rows=self.rows(), capabilities=self.CAPS, digest=DIGEST,
                              gate=gate, now_local=self.NOW)
        self.assertTrue(any("has not run" in a for a in view["alerts"]), view["alerts"])

    def test_the_digest_capabilities_are_the_servers_own_approval_levels(self) -> None:
        self.assertEqual(digest_capabilities(self.CAPS),
                         ["content.publish", "social.instagram_post"])


class GateAndDoctorTests(BatchCase):
    """Pionir's half: the gate says when a digest last reached Discord, and doctor says
    whether the gate runs at all."""

    def test_an_empty_digest_run_is_not_a_digest_sent(self) -> None:
        self.at(9, 1)
        gate = self.dgate()
        self.assertTrue(gate.run_once())
        digest = gate.state()["digest"]
        self.assertIsNotNone(digest["last_run_at"])
        self.assertEqual(digest["last_run"]["items"], 0)
        self.assertIsNone(digest["last_sent_at"])          # nothing reached Discord

    def test_a_digest_with_an_item_records_when_it_reached_discord(self) -> None:
        self.page()
        self.at(9, 1)
        gate = self.dgate()
        self.assertTrue(gate.run_once())
        digest = gate.state()["digest"]
        self.assertEqual((digest["last_run"]["items"], digest["last_run"]["complete"]),
                         (1, True))
        self.assertIsNotNone(digest["last_sent_at"])
        self.assertTrue(self.digest_posts())

    def test_doctor_says_whether_the_gate_runs_and_what_waits(self) -> None:
        self.page()
        view = self.app.doctor()["posting"]
        self.assertTrue(any("NOT running" in a for a in view["alerts"]), view["alerts"])
        self.assertEqual(view["digest"]["batched_pending"], 1)
        self.assertIn("site.publish_page", view["capabilities"])
        gate = self.dgate()
        self.app.gate = gate
        gate.running = True                   # as start() leaves it, without a thread
        view = self.app.doctor()["posting"]
        self.assertFalse(any("NOT running" in a for a in view["alerts"]), view["alerts"])
        self.assertTrue(view["discord_gate"]["running"])

    def test_a_dead_gate_loop_is_not_left_reading_running(self) -> None:
        gate = self.dgate()
        gate.running = True

        def boom() -> bool:
            raise SystemExit("gone")

        gate.run_once = boom                  # type: ignore[method-assign]
        with self.assertRaises(SystemExit):
            gate._loop()
        self.assertFalse(gate.running)
        self.assertIn("loop died", gate.state()["last_error"])


if __name__ == "__main__":
    unittest.main()
