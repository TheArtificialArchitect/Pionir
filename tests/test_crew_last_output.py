"""Each worker's "last output": what it last produced and when, or an honest "never".

The desktop's worker panel shows this, so it must be true and must not leak: counts,
timestamps and the KIND of the newest output only - never a payload, a buyer's word or an
error message. Each test fails if its rule is reverted: a worker that produced nothing read
as blank or as fine, a green poster whose counter says nothing reached the owner read as
producing, or a customer's text carried out through the panel.
"""
from __future__ import annotations

import json
import tempfile
import unittest

from test_crew_fakes import make_crew

from pionir.crew.blog import record_path, save_record
from pionir.crew.registry import build_registry, load_catalogue
from pionir.crew.worker import ErrorKind, WorkerError, make_output

NOW = 1_790_000_000.0
SECRET = "Hi, I am Jane Roe, my file is at https://jane.example/private and my card is 4111"


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cat = load_catalogue()
        cat["divisions"] = [d for d in cat["divisions"] if d["id"] in ("posting", "fiverr")]
        self.now = NOW
        self.crew = make_crew(tmp.name, registry=build_registry(cat), now=lambda: self.now)
        self.addCleanup(self.crew.stop)
        self.state = self.crew.cfg.state_dir / "workers"

    def attempt(self, worker_id: str, *, at: float, kind: str | None = None, payload=None,
                error=None) -> None:
        w = self.crew.registry.require(worker_id)
        outs = [] if kind is None else [make_output(w, valid_at=at, observed_at=at,
                                                   payload=payload or {"n": 1}, kind=kind)]
        self.crew.store.record_attempt(worker_id=worker_id, division=w.division,
                                       started_at=at - 1, finished_at=at, error=error,
                                       outputs=outs)

    def facts(self) -> dict:
        return self.crew.output_facts()

    def worker_entry(self, worker_id: str) -> dict:
        for d in self.crew.direction.divisions():
            for w in d["workers"]:
                if w["id"] == worker_id:
                    return w
        raise AssertionError(worker_id)


class LastOutputTests(_Case):
    def test_a_worker_that_never_produced_says_never_not_blank(self) -> None:
        fact = self.facts()["fiverr.gigs"]
        self.assertEqual((fact["state"], fact["last_at"], fact["kind"]), ("never", None, None))
        self.assertIsNone(fact["last_attempt_at"])           # it has not even run
        self.assertEqual(self.worker_entry("fiverr.gigs")["output"]["state"], "never")

    def test_it_reports_what_it_last_produced_and_when(self) -> None:
        self.attempt("fiverr.gigs", at=NOW - 7200, kind="fiverr.gig_tally")
        self.attempt("fiverr.gigs", at=NOW - 3600, kind="fiverr.gig_tally")
        self.attempt("fiverr.gigs", at=NOW - 60, kind=None)          # a green run that wrote nothing
        fact = self.worker_entry("fiverr.gigs")["output"]
        self.assertEqual((fact["state"], fact["kind"]), ("produced", "fiverr.gig_tally"))
        self.assertEqual(fact["last_at"], NOW - 3600)                # the run since produced nothing
        self.assertEqual(fact["last_attempt_at"], NOW - 60)
        self.assertEqual(fact["last_success_at"], NOW - 60)

    def test_a_green_poster_whose_counter_says_nothing_reached_the_owner_is_no_output(self) -> None:
        blog = self.crew.registry.require("posting.blog")
        rec = blog.load(self.state)
        rec.update(first_run_at=NOW, last_drafted_at=NOW + 2 * 86400,
                   counts={"drafts_written": 6, "drafts_blocked": 6})
        blog.save(self.state, rec)
        self.now = NOW + 3 * 86400
        self.attempt("posting.blog", at=self.now - 120, kind="post.tally")     # green, and a tally
        fact = self.facts()["posting.blog"]
        self.assertEqual(fact["state"], "no_output")
        self.assertTrue(fact["alert"])
        self.assertEqual(fact["kind"], "post.tally")                 # what it did write is still said
        self.assertIn("posting", fact)
        self.assertEqual(fact["posting"].get("submitted", 0), 0)
        # nothing ever reached the owner: the time is absent (unknown), not a fake value
        self.assertNotIn("last_submitted_at", fact["posting"])
        self.assertNotIn("last_published_at", fact["posting"])

    def test_a_poster_that_reached_the_owner_reports_its_counts_and_times(self) -> None:
        blog = self.crew.registry.require("posting.blog")
        rec = blog.load(self.state)
        rec.update(first_run_at=NOW - 3600, last_run_at=NOW - 60, last_drafted_at=NOW - 3000,
                   counts={"drafts_written": 4, "drafts_blocked": 1, "submitted_for_approval": 3,
                           "published": 2},
                   posts=[{"draft_id": "d1", "status": "published", "submitted_at": NOW - 3000,
                           "settled_at": NOW - 1000, "title": SECRET},
                          {"draft_id": "d2", "status": "pending_approval", "submitted_at": NOW - 900}])
        blog.save(self.state, rec)
        self.attempt("posting.blog", at=NOW - 60, kind="post.tally")
        fact = self.facts()["posting.blog"]
        self.assertEqual(fact["state"], "produced")
        self.assertEqual(fact["posting"], {"submitted": 3, "pending": 1, "published": 2,
                                           "drafts_written": 4, "drafts_blocked": 1,
                                           "last_submitted_at": NOW - 900,
                                           "last_published_at": NOW - 1000})
        self.assertNotIn("Jane", json.dumps(fact))

    def test_the_fiverr_desk_shows_its_ack_counters_and_no_customer_text(self) -> None:
        desk = self.crew.registry.require("fiverr.desk")
        rec = desk.load(self.state)
        rec["orders"] = {"FO123": {"buyer": "Jane Roe", "brief": SECRET, "state": "working"},
                         "FO124": {"buyer": "Sam", "brief": SECRET, "state": "new"}}
        rec["acks_pending"] = ["evt-1", "evt-2", "evt-3"]
        rec["counts"].update(events=9, unknown_events=1)
        save_record(record_path(self.state, "fiverr.desk"), rec)
        self.attempt("fiverr.desk", at=NOW - 30, kind="fiverr.tally", payload={"note": SECRET})
        fact = self.facts()["fiverr.desk"]
        self.assertEqual(fact["desk"]["orders"], 2)
        self.assertEqual(fact["desk"]["acks_pending"], 3)
        self.assertEqual((fact["desk"]["events"], fact["desk"]["unknown_events"]), (9, 1))
        blob = json.dumps(self.crew.direction.divisions())
        for word in ("Jane", "jane.example", "4111", "Roe", "private", SECRET[:20]):
            self.assertNotIn(word, blob)

    def test_an_error_message_never_reaches_the_panel(self) -> None:
        self.attempt("fiverr.gigs", at=NOW - 5, error=WorkerError("fiverr.gigs", list(ErrorKind)[0], SECRET))
        blob = json.dumps(self.crew.direction.divisions())
        self.assertNotIn("Jane", blob)
        fact = self.facts()["fiverr.gigs"]
        self.assertEqual(fact["state"], "never")                     # a failure produced nothing
        self.assertEqual(fact["last_attempt_at"], NOW - 5)
        self.assertIsNone(fact["last_success_at"])

    def test_an_unreadable_records_path_and_parser_words_never_reach_the_panel(self) -> None:
        path = record_path(self.state, "posting.blog")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ nope", encoding="utf-8")
        fact = self.facts()["posting.blog"]
        self.assertEqual(fact["state"], "no_output")
        self.assertEqual(fact["alert"], "its record is unreadable")     # the sentence, not the exception
        blob = json.dumps(self.crew.direction.divisions())
        self.assertNotIn(str(self.state), blob)
        self.assertNotIn("Expecting", blob)

    def test_an_odd_kind_is_not_shown(self) -> None:
        self.attempt("fiverr.gigs", at=NOW - 5, kind="has spaces and <b>markup</b>")
        fact = self.facts()["fiverr.gigs"]
        self.assertEqual(fact["state"], "produced")
        self.assertIsNone(fact["kind"])

    def test_one_workers_unreadable_record_does_not_blank_the_rest(self) -> None:
        path = record_path(self.state, "fiverr.desk")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ nope", encoding="utf-8")
        self.attempt("fiverr.gigs", at=NOW - 5, kind="fiverr.gig_tally")
        facts = self.facts()
        self.assertEqual(facts["fiverr.gigs"]["state"], "produced")
        self.assertEqual(facts["fiverr.desk"]["facts_error"], "_Unreadable")

    def test_direction_survives_a_broken_reader_and_says_nothing_rather_than_never(self) -> None:
        json.dumps(self.crew.direction.divisions())

        def broken():
            raise RuntimeError("x")

        self.crew.direction.facts = broken
        for d in self.crew.direction.divisions():
            for w in d["workers"]:
                self.assertNotIn("output", w)                        # absent, never a made-up "nothing"


if __name__ == "__main__":
    unittest.main()
