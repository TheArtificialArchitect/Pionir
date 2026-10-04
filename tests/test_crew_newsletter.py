"""The newsletter worker: once a week, what was published, for the owner's yes.

Pionir is a fake (``FakeHands`` from the blog's tests) and Scrooge's dash a ``FakeHttp``. The
blog record it reads is written by the REAL blog worker in one test and by hand in the rest;
the product shelf's record by hand, in the shelf's own shape. Each test fails if the rule it
names is reverted: the deterministic assembly and its UTM tags, one newsletter a week, a
quiet week sending nothing and raising nothing, a post or product carried twice, the content
check failing closed, the zero-subscriber skip, and the alarm when there WAS something new
and nothing reached the owner.
"""

import json
import tempfile
import unittest
from pathlib import Path

from test_crew_blog import FakeBrain, FakeHands, good, published
from test_crew_fakes import FakeHttp

from pionir.crew import contentcheck
from pionir.crew.blog import save_record
from pionir.crew.hands import JobOutcome
from pionir.crew.newsletter import (
    CAPABILITY,
    NO_SUBSCRIBERS,
    NewsletterWorker,
    assemble,
    newsletter_id,
)
from pionir.crew.registry import default_registry
from pionir.crew.result import Err, Ok
from pionir.crew.worker import ErrorKind, WorkContext

T0 = 1_790_000_000.0          # Mon 2026-09-21 14:13 UTC, ISO week 39
DAY = 86400.0
WEEK = 7 * DAY
DASH = "https://api.dokaz.net/dash/api.json"
READ_TOKEN = "readTOKEN0123456789"


def stats(confirmed=2, pending=1, unsubscribed=0, sends=0, last=None) -> dict:
    return {"subscribers": {"pending": pending, "confirmed": confirmed,
                            "unsubscribed": unsubscribed},
            "sends": sends, "last_send": last, "sender": {"ok": True, "why": None},
            "sent_today": 0, "daily_cap": 50}


def blog_post(n: int, settled_at: float, *, status="published") -> dict:
    slug = f"post-number-{n}"
    did = f"2026-09-2{n % 10}-{slug}"
    return {"draft_id": did, "slug": slug, "title": f"How to check an address, part {n}",
            "status": status, "settled_at": settled_at, "submitted_at": settled_at - 60,
            "url": f"https://api.dokaz.net/blog/{slug}",
            "payload": {"draft_id": did, "slug": slug,
                        "title": f"How to check an address, part {n}",
                        "description": "Why checking syntax and mail servers before you send "
                                       "keeps your messages out of the void.",
                        "body_md": "x" * 400, "tags": ["email"]}}


def product(slug="obol", name="Obol invoice kit", url="https://dokaz.gumroad.com/l/obol",
            settled_at=T0, status="published") -> dict:
    return {"slug": slug, "name": name, "url": url, "status": status, "version": "1.0.0",
            "settled_at": settled_at, "key": slug + "-1"}


def parked(n: int = 1) -> JobOutcome:
    return JobOutcome("pending_approval", CAPABILITY, task_id=f"t-{n}", approval_id=f"nl-{n}")


class SequencedHands(FakeHands):
    def job(self, job):
        self.jobs.append(job)
        return self.outcome if self.outcome.status != "pending_approval" \
            else parked(len(self.jobs))


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        (self.state / "scrooge-read-token.txt").write_text(READ_TOKEN, encoding="utf-8")
        self.worker = default_registry().require("posting.newsletter")
        self.hands = SequencedHands()
        self.http = FakeHttp({DASH: (200, {"summary": {}, "newsletter": stats()})})

    def set_blog(self, posts: list) -> None:
        save_record(self.state / "posting.blog.json", {"posts": posts, "counts": {}})

    def set_products(self, subs: list) -> None:
        save_record(self.state / "products.shelf.json", {"submissions": subs})

    def run_at(self, now: float, http="default"):
        ctx = WorkContext(now=now, http=self.http if http == "default" else http,
                          secrets_dir=self.state, job=self.hands.job,
                          approval=self.hands.approval, state_dir=self.state)
        return self.worker.run(ctx)

    def record(self) -> dict:
        return json.loads(self.worker.record_path(self.state).read_text(encoding="utf-8"))

    def pulse(self, now: float) -> dict:
        return self.worker.pulse(self.state, now)

    @staticmethod
    def kinds(result) -> list:
        return [o.kind for o in result.value]

    @staticmethod
    def tally(result) -> dict:
        (t,) = [o for o in result.value if o.kind == "post.tally"]
        return {f.measures: f.value for f in t.figures}


class CatalogueTests(unittest.TestCase):
    def test_the_newsletter_is_in_the_posting_division_and_reads_the_dash(self) -> None:
        reg = default_registry()
        w = reg.require("posting.newsletter")
        self.assertIsInstance(w, NewsletterWorker)
        self.assertEqual((w.division, w.cadence_seconds, w.blog_worker, w.products_worker,
                          w.url, w.token_file),
                         ("posting", 21600, "posting.blog", "products.shelf", DASH,
                          "scrooge-read-token.txt"))
        self.assertEqual(w.purpose, "")                     # asks no model
        self.assertIn("posting.newsletter", reg.division("posting").leader_notes)

    def test_the_week_id_is_the_iso_week(self) -> None:
        self.assertEqual(newsletter_id(T0), "weekly-2026-w39")
        self.assertEqual(newsletter_id(T0 + WEEK), "weekly-2026-w40")
        # 2027-01-01 is a Friday: ISO week 53 of 2026
        self.assertEqual(newsletter_id(1_798_761_600.0), "weekly-2026-w53")


class AssemblyTests(_Case):
    def test_it_assembles_the_weeks_posts_products_and_hire_and_submits_them(self) -> None:
        self.set_blog([blog_post(1, T0 - DAY), blog_post(2, T0 - 2 * WEEK)])
        self.set_products([product()])
        result = self.run_at(T0)
        self.assertIsInstance(result, Ok)
        (job,) = self.hands.jobs
        self.assertEqual(job.capability, "content.newsletter_send")
        self.assertEqual(job.permissions, ())               # nothing granted: Pionir parks it
        p = job.payload
        self.assertEqual(set(p), {"newsletter_id", "subject", "body_md"})
        self.assertEqual(p["newsletter_id"], "weekly-2026-w39")
        self.assertEqual(p["subject"], "Dokaz weekly: a new post and a new tool")
        q = "utm_source=newsletter&utm_medium=email&utm_campaign=weekly-2026-w39"
        body = p["body_md"]
        self.assertIn(f"- [How to check an address, part 1](https://api.dokaz.net/blog/"
                      f"post-number-1?{q}) - Why checking syntax", body)
        self.assertNotIn("part 2", body)                    # published three weeks ago
        self.assertIn(f"- [Obol invoice kit](https://dokaz.gumroad.com/l/obol?{q}) "
                      "(new this week)", body)
        self.assertIn(f"[hire page](https://api.dokaz.net/hire?{q})", body)
        for url in contentcheck.links_in(body):
            self.assertTrue(url.endswith(q), url)
        # deterministic: exactly what assemble() makes of the same records
        self.assertEqual(p, assemble("weekly-2026-w39", [
            {"draft_id": "2026-09-21-post-number-1", "slug": "post-number-1",
             "title": "How to check an address, part 1",
             "description": blog_post(1, 0)["payload"]["description"]}],
            [{"slug": "obol", "name": "Obol invoice kit",
              "url": "https://dokaz.gumroad.com/l/obol", "new": True}]))
        self.assertEqual(contentcheck.check_newsletter(p, names=frozenset({"Obol invoice kit"})),
                         [])
        (entry,) = self.record()["posts"]
        self.assertEqual((entry["draft_id"], entry["status"], entry["approval_id"],
                          entry["blog_posts"], entry["products"]),
                         ("weekly-2026-w39", "pending_approval", "nl-1",
                          ["2026-09-21-post-number-1"], ["obol"]))
        self.assertIn("post.pending_approval", self.kinds(result))
        self.assertEqual(self.tally(result)["newsletters pending approval"], 1)

    def test_from_the_real_blog_workers_record(self) -> None:
        blog = default_registry().require("posting.blog")
        hands = FakeHands()
        ctx = WorkContext(now=T0, http=None, secrets_dir=self.state, words=FakeBrain(good()),
                          job=hands.job, approval=hands.approval, state_dir=self.state)
        blog.run(ctx)
        post = json.loads(blog.record_path(self.state).read_text(encoding="utf-8"))["posts"][0]
        hands.approvals["ap-1"] = published(f"https://api.dokaz.net/blog/{post['slug']}")
        ctx.now = T0 + 60
        blog.run(ctx)
        self.run_at(T0 + 3600)
        (job,) = self.hands.jobs
        self.assertIn(f"[{good()['title']}](https://api.dokaz.net/blog/{good()['slug']}?",
                      job.payload["body_md"])
        self.assertIn(good()["description"], job.payload["body_md"])
        self.assertEqual(job.payload["subject"], "Dokaz weekly: a new post")

    def test_a_product_off_the_allowed_hosts_is_left_out(self) -> None:
        self.set_blog([blog_post(1, T0 - DAY)])
        self.set_products([product(url="https://gum.co/obol"),
                           product(slug="kleos", name="Kleos kit",
                                   url="https://dokaz.gumroad.com/l/kleos", status="denied")])
        self.run_at(T0)
        (job,) = self.hands.jobs
        self.assertNotIn("Tools on sale", job.payload["body_md"])
        self.assertNotIn("gum.co", job.payload["body_md"])


class OncePerWeekTests(_Case):
    def test_one_newsletter_a_week_and_nothing_carried_twice(self) -> None:
        self.set_blog([blog_post(1, T0 - DAY)])
        self.set_products([product()])
        self.run_at(T0)
        self.run_at(T0 + 3 * 3600)                          # same week: nothing more
        self.assertEqual(len(self.hands.jobs), 1)
        # next week: one new post; the old post and the known product are not new
        self.set_blog([blog_post(1, T0 - DAY), blog_post(3, T0 + WEEK - DAY)])
        self.run_at(T0 + WEEK)
        self.assertEqual(len(self.hands.jobs), 2)
        p = self.hands.jobs[1].payload
        self.assertEqual(p["newsletter_id"], "weekly-2026-w40")
        self.assertEqual(p["subject"], "Dokaz weekly: a new post")
        self.assertIn("part 3", p["body_md"])
        self.assertNotIn("part 1", p["body_md"])
        self.assertIn("[Obol invoice kit](", p["body_md"])   # still on sale, listed
        self.assertNotIn("(new this week)", p["body_md"])

    def test_a_post_last_weeks_newsletter_carried_is_not_carried_again(self) -> None:
        self.set_blog([blog_post(1, T0 + 5 * DAY)])        # a Saturday post, week 39
        self.run_at(T0 + 5 * DAY + 60)
        self.assertEqual(len(self.hands.jobs), 1)
        self.run_at(T0 + WEEK)                             # Monday: still under a week old
        self.assertEqual(len(self.hands.jobs), 1)          # nothing new: nothing sent
        self.assertEqual(self.record()["week"]["skipped"], "nothing_new")
        self.set_blog([blog_post(1, T0 + 5 * DAY), blog_post(4, T0 + WEEK + DAY)])
        self.run_at(T0 + WEEK + DAY + 60)
        body = self.hands.jobs[1].payload["body_md"]
        self.assertIn("part 4", body)
        self.assertNotIn("part 1", body)

    def test_a_quiet_week_sends_nothing_and_raises_no_alarm(self) -> None:
        self.set_blog([blog_post(1, T0 - 3 * WEEK)])
        self.set_products([])
        result = self.run_at(T0)
        self.assertIsInstance(result, Ok)
        self.assertEqual(self.hands.jobs, [])
        week = self.record()["week"]
        self.assertEqual((week["new_content"], week["skipped"]), (0, "nothing_new"))
        self.assertIsNone(self.pulse(T0 + 60)["alert"])
        self.assertEqual(self.pulse(T0 + 60)["new_content"], 0)

    def test_no_records_at_all_is_a_quiet_week(self) -> None:
        self.assertIsInstance(self.run_at(T0), Ok)
        self.assertEqual(self.hands.jobs, [])
        self.assertIsNone(self.pulse(T0)["alert"])

    def test_a_denied_newsletter_lets_its_posts_go_again_inside_the_week_window(self) -> None:
        self.set_blog([blog_post(1, T0 + 3 * DAY)])
        self.run_at(T0 + 3 * DAY)
        self.hands.approvals["nl-1"] = {"id": "nl-1", "status": "denied", "reason": "no"}
        self.run_at(T0 + 3 * DAY + 60)
        self.assertEqual(self.record()["posts"][0]["status"], "denied")
        self.run_at(T0 + WEEK)                             # next ISO week, post still fresh
        self.assertEqual(len(self.hands.jobs), 2)
        self.assertIn("part 1", self.hands.jobs[1].payload["body_md"])


class AlarmTests(_Case):
    def test_new_content_blocked_by_the_check_is_an_alarm(self) -> None:
        self.set_blog([])
        self.set_products([product(name="Acme Corp invoice kit")])
        result = self.run_at(T0)
        self.assertIsInstance(result, Ok)
        self.assertEqual(self.hands.jobs, [])
        self.assertIn("post.blocked", self.kinds(result))
        (entry,) = self.record()["posts"]
        self.assertEqual(entry["status"], "blocked")
        self.assertTrue(any("Acme Corp" in r for r in entry["reasons"]))
        alert = self.pulse(T0 + 60)["alert"]
        self.assertIn("weekly-2026-w39", alert)
        self.assertIn("blocked by the content check", alert)
        self.run_at(T0 + 3600)                              # deterministic: not retried
        self.assertEqual(len(self.record()["posts"]), 1)
        self.assertIsNone(self.pulse(T0 + WEEK)["alert"])   # a new week starts clean

    def test_new_content_pionir_refused_is_an_alarm(self) -> None:
        self.set_blog([blog_post(1, T0 - DAY)])
        self.hands.outcome = JobOutcome("failed", CAPABILITY, error="publish token missing",
                                        error_type="AdapterUnavailable")
        self.run_at(T0)
        self.assertEqual(len(self.hands.jobs), 1)
        self.assertIn("publish token missing", self.pulse(T0 + 60)["alert"])

    def test_a_submitted_newsletter_is_no_alarm(self) -> None:
        self.set_blog([blog_post(1, T0 - DAY)])
        self.run_at(T0)
        p = self.pulse(T0 + 60)
        self.assertIsNone(p["alert"])
        self.assertEqual((p["submitted"], p["pending"]), (1, 1))


class AudienceTests(_Case):
    def test_no_confirmed_subscriber_skips_and_says_so_without_an_alarm(self) -> None:
        self.http.routes[DASH] = (200, {"newsletter": stats(confirmed=0, pending=3)})
        self.set_blog([blog_post(1, T0 - DAY)])
        result = self.run_at(T0)
        self.assertEqual(self.hands.jobs, [])
        week = self.record()["week"]
        self.assertEqual((week["skipped"], week["why"]), ("no_subscribers", NO_SUBSCRIBERS))
        p = self.pulse(T0 + 60)
        self.assertIsNone(p["alert"])
        self.assertEqual(p["skipped"], "no_subscribers")
        self.assertEqual(p["subscribers"], {"pending": 3, "confirmed": 0, "unsubscribed": 0})
        (aud,) = [o for o in result.value if o.kind == "newsletter.audience"]
        figs = {f.measures: f.value for f in aud.figures}
        self.assertEqual(figs["newsletter subscribers confirmed"], 0)
        self.assertEqual(figs["newsletter subscribers waiting to confirm"], 3)
        # the first subscriber confirms: the same week's newsletter goes
        self.http.routes[DASH] = (200, {"newsletter": stats(confirmed=1)})
        self.run_at(T0 + 3600)
        self.assertEqual(len(self.hands.jobs), 1)

    def test_the_read_token_goes_only_in_the_header_and_counts_are_reported(self) -> None:
        last = {"newsletter_id": "weekly-2026-w38", "send_id": "nl_" + "1" * 24,
                "subject": "Dokaz weekly: a new post", "status": "done", "recipients": 5,
                "sent": 4, "failed": 1, "skipped": 0,
                "queued_at": "2026-09-15T12:00:00.000Z",
                "finished_at": "2026-09-15T12:05:00.000Z"}
        self.http.routes[DASH] = (200, {"newsletter": stats(confirmed=5, sends=1, last=last)})
        result = self.run_at(T0)
        ((url, headers, _t),) = self.http.calls
        self.assertEqual((url, headers), (DASH, {"x-dash-token": READ_TOKEN}))
        (aud,) = [o for o in result.value if o.kind == "newsletter.audience"]
        figs = {f.measures: f.value for f in aud.figures}
        self.assertEqual((figs["newsletters sent"], figs["last newsletter recipients"],
                          figs["last newsletter emails sent"],
                          figs["last newsletter emails failed"]), (1, 5, 4, 1))
        self.assertEqual(aud.payload["last_send"]["finished_at"], "2026-09-15T12:05:00.000Z")
        self.assertEqual(self.pulse(T0)["last_send"]["newsletter_id"], "weekly-2026-w38")

    def test_an_unreadable_audience_is_said_and_never_stops_the_newsletter(self) -> None:
        self.set_blog([blog_post(1, T0 - DAY)])
        for routes, said in (({}, "no route"),
                             ({DASH: (200, {"summary": {}})}, "no newsletter figures"),
                             ({DASH: (200, {"newsletter": {"error": "stats failed (no such "
                                                                    "table)"}})},
                              "stats failed"),
                             ({DASH: (403, {"error": "no"})}, "HTTP 403")):
            with self.subTest(said):
                self.http.routes = dict(routes)
                result = self.run_at(T0)
                (out,) = [o for o in result.value if o.kind == "newsletter.audience_unavailable"]
                self.assertIn(said, out.payload["why"])
                self.assertIsNone(self.pulse(T0)["subscribers"])
        self.assertEqual(len(self.hands.jobs), 1)            # submitted once, counts unknown


class NotConfiguredTests(_Case):
    """Scrooge answers 503 until POSTAL_ADDRESS and RESEND_API_KEY are set: a typed NOT
    CONFIGURED state, never a generic failure, never an approval asked for in vain."""

    def off(self) -> dict:
        s = stats()
        s["sender"] = {"ok": False, "why": "POSTAL_ADDRESS is not set"}
        return s

    def test_scrooge_unable_to_send_submits_nothing_and_is_not_configured(self) -> None:
        self.http.routes[DASH] = (200, {"newsletter": self.off()})
        self.set_blog([blog_post(1, T0 - DAY)])
        result = self.run_at(T0)
        self.assertEqual(self.hands.jobs, [])
        self.assertIsInstance(result, Err)
        self.assertEqual(result.error.kind, ErrorKind.NOT_CONFIGURED)
        self.assertFalse(result.error.retryable)
        self.assertIn("POSTAL_ADDRESS is not set", result.error.message)
        kinds = [o.kind for o in result.error.partial]
        self.assertIn("post.tally", kinds)                  # its rows are kept
        self.assertIn("newsletter.audience", kinds)
        week = self.record()["week"]
        self.assertEqual(week["skipped"], "not_configured")
        self.assertIn("POSTAL_ADDRESS", week["why"])
        # configured: the same week's newsletter goes, and the run is Ok again
        self.http.routes[DASH] = (200, {"newsletter": stats()})
        self.assertIsInstance(self.run_at(T0 + 3600), Ok)
        self.assertEqual(len(self.hands.jobs), 1)

    def test_an_approved_newsletter_scrooge_answered_503_is_typed_and_alarms(self) -> None:
        t = T0 + 3 * DAY                                     # a Thursday post
        self.set_blog([blog_post(1, t)])
        self.run_at(t)
        self.hands.approvals["nl-1"] = {
            "id": "nl-1", "status": "approved_failed",
            "result": {"ok": False, "agent_id": "newsletter", "result": {
                "ok": False, "status": 503, "not_configured": True,
                "unavailable": "Scrooge cannot send newsletters yet: newsletter sending is "
                               "not configured: POSTAL_ADDRESS is not set",
                "error": "Scrooge cannot send newsletters yet: newsletter sending is not "
                         "configured: POSTAL_ADDRESS is not set"}}}
        result = self.run_at(t + 600)
        self.assertIsInstance(result, Err)
        self.assertEqual(result.error.kind, ErrorKind.NOT_CONFIGURED)
        (entry,) = self.record()["posts"]
        self.assertEqual(entry["status"], "not_configured")
        self.assertNotIn("send_id", entry)
        p = self.pulse(t + 700)
        self.assertEqual((p["published"], p["not_configured"]), (0, 1))
        self.assertIn("NOT CONFIGURED", p["alert"])         # approved, and nothing went out
        self.run_at(t + 3600)                              # not retried that week
        self.assertEqual(len(self.hands.jobs), 1)
        # next week, configured: the still-fresh post goes again
        self.run_at(T0 + WEEK)
        self.assertEqual(len(self.hands.jobs), 2)
        self.assertIn("part 1", self.hands.jobs[1].payload["body_md"])


class FollowUpTests(_Case):
    def test_approved_with_a_send_id_is_sent(self) -> None:
        self.set_blog([blog_post(1, T0 - DAY)])
        self.run_at(T0)
        self.hands.approvals["nl-1"] = {
            "id": "nl-1", "status": "approved",
            "result": {"ok": True, "agent_id": "newsletter", "task_id": "t-9",
                       "result": {"ok": True, "newsletter_id": "weekly-2026-w39",
                                  "send_id": "nl_" + "c" * 24, "status": "queued",
                                  "recipients": 2, "queued_at": "2026-09-22T12:00:00.000Z"}}}
        result = self.run_at(T0 + 600)
        (entry,) = self.record()["posts"]
        self.assertEqual((entry["status"], entry["send_id"]), ("published", "nl_" + "c" * 24))
        self.assertIn("post.published", self.kinds(result))
        self.assertEqual(self.pulse(T0 + 700)["published"], 1)

    def test_scrooge_already_having_it_counts_as_sent(self) -> None:
        self.set_blog([blog_post(1, T0 - DAY)])
        self.run_at(T0)
        self.hands.approvals["nl-1"] = {
            "id": "nl-1", "status": "approved_failed",
            "result": {"ok": False, "status": "error", "result": {
                "ok": False, "refused": "newsletter_id: already queued at x (send nl_dd)",
                "send_id": "nl_" + "d" * 24}}}
        self.run_at(T0 + 600)
        (entry,) = self.record()["posts"]
        self.assertEqual((entry["status"], entry["send_id"]), ("published", "nl_" + "d" * 24))

    def test_pionirs_own_ledger_refusal_quoting_the_send_id_counts_as_sent(self) -> None:
        self.set_blog([blog_post(1, T0 - DAY)])
        sid = "nl_" + "e" * 24
        self.hands.outcome = JobOutcome(
            "failed", CAPABILITY, error=f"content.newsletter_send refused by Pionir - "
            f"newsletter_id: already sent (send {sid}, queued at x)",
            error_type="AdapterProtocolError")
        self.run_at(T0)
        (entry,) = self.record()["posts"]
        self.assertEqual((entry["status"], entry["send_id"]), ("published", sid))

    def test_approved_without_a_send_id_is_not_counted_sent(self) -> None:
        self.set_blog([blog_post(1, T0 - DAY)])
        self.run_at(T0)
        self.hands.approvals["nl-1"] = {"id": "nl-1", "status": "approved",
                                        "result": {"ok": True, "result": {"ok": True}}}
        self.run_at(T0 + 600)
        self.assertEqual(self.record()["posts"][0]["status"], "unconfirmed")


if __name__ == "__main__":
    unittest.main()
