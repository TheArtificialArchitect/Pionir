"""The dev.to cross-poster: each published blog post, once, checked, for the owner's yes.

Pionir is a fake (``FakeHands`` from the blog's tests). The blog record the cross-poster
reads is written by the REAL blog worker (drafted with a fake brain, published through
the fake hands), so the text copied is exactly what the blog worker keeps. Each test fails
if the rule it names is reverted: the utm swap, the exact footer, the tag mapping, a post
that is not published being cross-posted, the same post submitted twice, more than one
submission a run, or a check that does not fail closed.
"""

import json
import tempfile
import unittest
from pathlib import Path

from test_crew_blog import FakeBrain, FakeHands, good, published

from pionir.crew import contentcheck
from pionir.crew.blog import SEEDS, save_record
from pionir.crew.devto import CAPABILITY, DevtoWorker, crosspost, devto_tags, swap_source
from pionir.crew.hands import JobOutcome
from pionir.crew.registry import default_registry
from pionir.crew.result import Err, Ok
from pionir.crew.worker import ErrorKind, WorkContext

T0 = 1_790_000_000.0
DAY = 86400.0
DEVTO_URL = "https://dev.to/dokaz/verify-email-addresses-before-you-send-4k2j"


def parked(n: int = 1) -> JobOutcome:
    return JobOutcome("pending_approval", CAPABILITY, task_id=f"t-{n}", approval_id=f"dv-{n}")


class SequencedHands(FakeHands):
    """Parks each job under its own approval id (dv-1, dv-2, ...)."""

    def job(self, job):
        self.jobs.append(job)
        return parked(len(self.jobs))


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        reg = default_registry()
        self.blog = reg.require("posting.blog")
        self.worker = reg.require("posting.devto")
        self.hands = SequencedHands()

    # ---- the blog's record, written by the real blog worker --------------------------------
    def publish_blog_post(self, now: float, raw: dict | None = None, *, approve: bool = True):
        """Draft, submit and (if ``approve``) publish one blog post through the real worker."""
        hands = FakeHands()
        ctx = WorkContext(now=now, http=None, secrets_dir=self.state, words=FakeBrain(
            raw or good()), job=hands.job, approval=hands.approval, state_dir=self.state)
        self.assertIsInstance(self.blog.run(ctx), Ok)
        post = self.blog_record()["posts"][-1]
        if approve:
            hands.approvals["ap-1"] = published(f"https://api.dokaz.net/blog/{post['slug']}")
        else:
            hands.approvals["ap-1"] = {"id": "ap-1", "status": "denied", "reason": "no"}
        ctx.now = now + 60
        self.blog.run(ctx)
        return self.blog_record()["posts"][-1]

    def blog_record(self) -> dict:
        return json.loads(self.blog.record_path(self.state).read_text(encoding="utf-8"))

    def edit_blog_record(self, fn) -> None:
        rec = self.blog_record()
        fn(rec)
        save_record(self.blog.record_path(self.state), rec)

    # ---- the cross-poster -------------------------------------------------------------------
    def run_at(self, now: float):
        ctx = WorkContext(now=now, http=None, secrets_dir=self.state, job=self.hands.job,
                          approval=self.hands.approval, state_dir=self.state)
        return self.worker.run(ctx)

    def record(self) -> dict:
        return json.loads(self.worker.record_path(self.state).read_text(encoding="utf-8"))

    @staticmethod
    def tally(result) -> dict:
        (t,) = [o for o in result.value if o.kind == "post.tally"]
        return {f.measures: f.value for f in t.figures}


class CatalogueTests(unittest.TestCase):
    def test_the_cross_poster_is_in_the_posting_division_every_six_hours(self) -> None:
        w = default_registry().require("posting.devto")
        self.assertIsInstance(w, DevtoWorker)
        self.assertEqual((w.division, w.cadence_seconds, w.blog_worker),
                         ("posting", 21600, "posting.blog"))
        notes = default_registry().division("posting").leader_notes
        self.assertIn("posting.devto", notes)


class BlogRecordTests(_Case):
    def test_the_blog_record_keeps_the_exact_payload_it_submitted(self) -> None:
        hands = FakeHands()
        ctx = WorkContext(now=T0, http=None, secrets_dir=self.state, words=FakeBrain(good()),
                          job=hands.job, approval=hands.approval, state_dir=self.state)
        self.blog.run(ctx)
        (job,) = hands.jobs
        (post,) = self.blog_record()["posts"]
        self.assertEqual(post["payload"], job.payload)


class TransformTests(_Case):
    def test_a_published_post_becomes_one_crosspost_with_the_utm_swapped_and_the_footer(
            self) -> None:
        raw = good() | {"tags": ["site-checks", "Email-Verify", "qr", "API", "python"]}
        post = self.publish_blog_post(T0, raw)
        self.assertEqual(post["status"], "published")
        original = post["payload"]
        result = self.run_at(T0 + 3600)
        self.assertIsInstance(result, Ok)
        (job,) = self.hands.jobs
        self.assertEqual(job.capability, "content.crosspost_devto")
        self.assertEqual(job.permissions, ())               # nothing granted: Pionir parks it
        p = job.payload
        self.assertEqual(set(p), {"draft_id", "slug", "title", "description", "body_md", "tags"})
        for key in ("draft_id", "slug", "title", "description"):
            self.assertEqual(p[key], original[key], key)
        slug, did = original["slug"], original["draft_id"]
        campaign = contentcheck.utm_campaign(did)
        footer = (f"*Originally published at [api.dokaz.net](https://api.dokaz.net/blog/{slug}"
                  f"?utm_source=devto&utm_medium=referral&utm_campaign={campaign})*")
        self.assertTrue(p["body_md"].endswith("\n\n" + footer), p["body_md"][-300:])
        # every Dokaz link now says devto; nothing else about any link changed
        self.assertIn("utm_source=blog", original["body_md"])
        self.assertNotIn("utm_source=blog", p["body_md"])
        body = p["body_md"][:-len("\n\n" + footer)]
        self.assertEqual(body.replace("utm_source=devto", "utm_source=blog"),
                         original["body_md"].rstrip())
        self.assertEqual(body.count("utm_source=devto"), original["body_md"].count(
            "utm_source=blog"))
        self.assertIn(f"utm_medium=referral&utm_campaign={campaign}", body)
        self.assertEqual(p["tags"], ["sitechecks", "emailverify", "qr", "api"])
        self.assertEqual(contentcheck.check(p, utm_source="devto"), [])
        (entry,) = self.record()["posts"]
        self.assertEqual((entry["draft_id"], entry["status"], entry["approval_id"]),
                         (did, "pending_approval", "dv-1"))
        self.assertIn("post.pending_approval", [o.kind for o in result.value])
        self.assertEqual(self.tally(result)["cross-posts pending approval"], 1)

    def test_tags_map_to_devto_form_and_never_to_none(self) -> None:
        self.assertEqual(devto_tags(["site-checks", "email"]), ["sitechecks", "email"])
        self.assertEqual(devto_tags(["a", "b", "c", "d", "e"]), ["a", "b", "c", "d"])
        self.assertEqual(devto_tags(["qr-code", "qrcode"]), ["qrcode"])
        self.assertEqual(devto_tags([]), ["webdev"])
        self.assertEqual(devto_tags(["---"]), ["webdev"])
        self.assertEqual(devto_tags(None), ["webdev"])

    def test_only_a_dokaz_links_blog_source_is_swapped(self) -> None:
        q = "utm_source=blog&utm_medium=referral&utm_campaign=x"
        text = (f"[a](https://api.dokaz.net/docs/qr-code-api?{q}) and "
                f"[b](https://dokaz.gumroad.com/l/obol?{q}). See https://example.com/?{q}.")
        out = swap_source(text)
        self.assertIn("https://api.dokaz.net/docs/qr-code-api?utm_source=devto&utm_medium="
                      "referral&utm_campaign=x)", out)
        self.assertIn("https://dokaz.gumroad.com/l/obol?utm_source=devto&", out)
        self.assertIn(f"https://example.com/?{q}.", out)          # not ours: left alone

    def test_crosspost_is_pure_and_keeps_the_same_draft_id(self) -> None:
        payload = {"draft_id": "2026-09-25-a-post", "slug": "a-post", "title": "t" * 20,
                   "description": "d" * 60, "body_md": "b" * 400, "tags": ["x-y"]}
        before = json.dumps(payload, sort_keys=True)
        d = crosspost(payload)
        self.assertEqual(json.dumps(payload, sort_keys=True), before)
        self.assertEqual((d["draft_id"], d["tags"]), ("2026-09-25-a-post", ["xy"]))


class WhichPostsTests(_Case):
    def test_a_post_pending_approval_is_not_crossposted(self) -> None:
        hands = FakeHands()
        ctx = WorkContext(now=T0, http=None, secrets_dir=self.state, words=FakeBrain(good()),
                          job=hands.job, approval=hands.approval, state_dir=self.state)
        self.blog.run(ctx)
        self.assertEqual(self.blog_record()["posts"][0]["status"], "pending_approval")
        result = self.run_at(T0 + 3600)
        self.assertIsInstance(result, Ok)
        self.assertEqual(self.hands.jobs, [])
        self.assertEqual(self.tally(result)["published blog posts waiting to be cross-posted"],
                         0)

    def test_a_denied_post_is_not_crossposted(self) -> None:
        post = self.publish_blog_post(T0, approve=False)
        self.assertEqual(post["status"], "denied")
        self.run_at(T0 + 3600)
        self.assertEqual(self.hands.jobs, [])

    def test_no_blog_record_yet_is_nothing_to_do(self) -> None:
        result = self.run_at(T0)
        self.assertIsInstance(result, Ok)
        self.assertEqual(self.hands.jobs, [])

    def test_a_published_post_without_its_text_is_skipped_not_guessed(self) -> None:
        self.publish_blog_post(T0)
        self.edit_blog_record(lambda rec: rec["posts"][0].pop("payload"))
        result = self.run_at(T0 + 3600)
        self.assertEqual(self.hands.jobs, [])
        (entry,) = self.record()["posts"]
        self.assertEqual(entry["status"], "skipped")
        self.assertEqual(result.value[-1].payload["skipped_without_text"], 1)


class OnceTests(_Case):
    def test_never_submitted_twice_while_waiting_or_after_a_denial(self) -> None:
        self.publish_blog_post(T0)
        self.run_at(T0 + 3600)
        self.run_at(T0 + 2 * 3600)                  # still pending: not sent again
        self.assertEqual(len(self.hands.jobs), 1)
        self.hands.approvals["dv-1"] = {"id": "dv-1", "status": "denied", "reason": "no"}
        result = self.run_at(T0 + 3 * 3600)
        self.assertEqual(len(self.hands.jobs), 1)   # denied: never resubmitted
        self.assertEqual(self.record()["posts"][0]["status"], "denied")
        self.assertEqual(self.tally(result)["cross-posts denied"], 1)
        self.run_at(T0 + 4 * 3600)
        self.assertEqual(len(self.hands.jobs), 1)

    def test_a_submission_that_never_reached_pionir_is_retried_a_bounded_number_of_times(
            self) -> None:
        from pionir.crew.devto import RETRY_UNREACHABLE
        self.publish_blog_post(T0)
        down = JobOutcome("unreachable", CAPABILITY, error="connection refused")
        self.hands.job = lambda job: (self.hands.jobs.append(job), down)[1]
        for i in range(RETRY_UNREACHABLE):
            self.run_at(T0 + (i + 1) * 3600)
        self.assertEqual(len(self.hands.jobs), RETRY_UNREACHABLE)   # retried, not dropped
        self.run_at(T0 + (RETRY_UNREACHABLE + 1) * 3600)
        self.assertEqual(len(self.hands.jobs), RETRY_UNREACHABLE)   # and then it stops

    def test_pionir_back_up_gets_the_cross_post(self) -> None:
        self.publish_blog_post(T0)
        real = self.hands.job
        self.hands.job = lambda job: (self.hands.jobs.append(job),
                                      JobOutcome("unreachable", CAPABILITY, error="down"))[1]
        self.run_at(T0 + 3600)
        self.hands.job = real
        self.run_at(T0 + 7200)
        self.assertEqual(len(self.hands.jobs), 2)
        self.assertEqual(self.record()["posts"][-1]["status"], "pending_approval")

    def test_one_per_run_oldest_published_first(self) -> None:
        a = self.publish_blog_post(T0, good(slug="verify-email-before-sending"))
        b = self.publish_blog_post(T0 + DAY, good(slug="email-checks-at-sign-up",
                                                  title="Check email addresses at sign-up"))
        self.assertEqual((a["status"], b["status"]), ("published", "published"))
        first = self.run_at(T0 + 2 * DAY)
        self.assertEqual([j.payload["draft_id"] for j in self.hands.jobs], [a["draft_id"]])
        self.assertEqual(self.tally(first)["published blog posts waiting to be cross-posted"],
                         1)
        self.run_at(T0 + 2 * DAY + 3600)
        self.assertEqual([j.payload["draft_id"] for j in self.hands.jobs],
                         [a["draft_id"], b["draft_id"]])
        self.run_at(T0 + 2 * DAY + 7200)
        self.assertEqual(len(self.hands.jobs), 2)


class OutcomeTests(_Case):
    def setUp(self) -> None:
        super().setUp()
        self.post = self.publish_blog_post(T0)
        self.run_at(T0 + 3600)

    def test_approved_and_done_with_a_devto_url_is_published(self) -> None:
        self.hands.approvals["dv-1"] = {"id": "dv-1", "status": "approved", "result": {
            "ok": True, "agent_id": "content", "result": {
                "ok": True, "id": 123, "url": DEVTO_URL,
                "canonical_url": f"https://api.dokaz.net/blog/{self.post['slug']}"}}}
        result = self.run_at(T0 + 7200)
        (out,) = [o for o in result.value if o.kind == "post.published"]
        self.assertEqual(out.payload["url"], DEVTO_URL)
        self.assertEqual(self.record()["posts"][0]["url"], DEVTO_URL)
        self.assertEqual(self.tally(result)["cross-posts published"], 1)

    def test_done_with_a_url_that_is_not_devto_is_not_published(self) -> None:
        self.hands.approvals["dv-1"] = {"id": "dv-1", "status": "approved", "result": {
            "ok": True, "result": {"ok": True, "url": "https://example.com/x"}}}
        result = self.run_at(T0 + 7200)
        self.assertEqual(self.record()["posts"][0]["status"], "unconfirmed")
        self.assertEqual(self.tally(result)["cross-posts published"], 0)

    def refused(self, reason: str, **extra) -> None:
        self.hands.approvals["dv-1"] = {"id": "dv-1", "status": "approved_failed", "result": {
            "ok": False, "agent_id": "content",
            "result": {"ok": False, "refused": reason, "error": reason, **extra}}}

    def test_already_crossposted_with_its_url_is_recorded_as_published(self) -> None:
        self.refused("this draft was already crossposted to dev.to", url=DEVTO_URL)
        result = self.run_at(T0 + 7200)
        entry = self.record()["posts"][0]
        self.assertEqual((entry["status"], entry["url"]), ("published", DEVTO_URL))
        self.assertEqual(self.tally(result)["cross-posts published"], 1)

    def test_already_crossposted_without_a_url_is_failed_with_the_reason(self) -> None:
        self.refused("already cross-posted")
        result = self.run_at(T0 + 7200)
        entry = self.record()["posts"][0]
        self.assertEqual(entry["status"], "failed")
        self.assertIn("already cross-posted", entry["why"])
        self.assertEqual(self.tally(result)["cross-posts published"], 0)
        self.assertEqual(self.tally(result)["cross-posts failed"], 1)

    def test_any_other_refusal_is_failed_with_its_reason(self) -> None:
        self.refused("the original is not live", url=DEVTO_URL)
        self.run_at(T0 + 7200)
        entry = self.record()["posts"][0]
        self.assertEqual(entry["status"], "failed")
        self.assertIn("not live", entry["why"])


class FailClosedTests(_Case):
    def test_a_draft_the_check_refuses_is_blocked_recorded_and_never_retried(self) -> None:
        self.publish_blog_post(T0)

        def taint(rec):
            p = rec["posts"][0]["payload"]
            p["body_md"] = p["body_md"] + "\nA friend, Jane Doe, swears by it.\n"
        self.edit_blog_record(taint)
        result = self.run_at(T0 + 3600)
        self.assertEqual(self.hands.jobs, [])
        (entry,) = self.record()["posts"]
        self.assertEqual(entry["status"], "blocked")
        self.assertTrue(any("Jane Doe" in r for r in entry["reasons"]), entry["reasons"])
        self.assertIn("post.blocked", [o.kind for o in result.value])
        self.run_at(T0 + 7200)
        self.assertEqual(self.hands.jobs, [])
        self.assertEqual(len(self.record()["posts"]), 1)

    def test_the_devto_check_refuses_a_link_still_saying_blog(self) -> None:
        post = self.publish_blog_post(T0)
        draft = crosspost(post["payload"])
        self.assertEqual(self.worker.check_draft(draft), [])
        still_blog = dict(draft, body_md=draft["body_md"].replace("utm_source=devto",
                                                                  "utm_source=blog", 1))
        self.assertTrue(any("utm_source=devto" in r for r in self.worker.check_draft(
            still_blog)))
        no_footer = dict(draft, body_md=draft["body_md"].rsplit("\n\n", 1)[0])
        self.assertTrue(any("Originally published" in r for r in self.worker.check_draft(
            no_footer)))
        too_many = dict(draft, tags=["a", "b", "c", "d", "e"])
        self.assertTrue(any("tags" in r for r in self.worker.check_draft(too_many)))

    def test_the_blog_check_is_not_weakened_by_the_devto_source(self) -> None:
        post = self.publish_blog_post(T0)
        blog_payload = post["payload"]
        self.assertEqual(contentcheck.check(blog_payload), [])
        self.assertTrue(any("utm_source=devto" in r for r in contentcheck.check(
            blog_payload, utm_source="devto")))
        devto_draft = crosspost(blog_payload)
        self.assertTrue(any("utm_source=blog" in r for r in contentcheck.check(devto_draft)))
        self.assertTrue(any("not one of" in r for r in contentcheck.check(
            blog_payload, utm_source="twitter")))

    def test_no_state_dir_or_no_hands_is_not_configured(self) -> None:
        r = self.worker.run(WorkContext(now=T0, http=None, secrets_dir=self.state))
        self.assertEqual(r.error.kind, ErrorKind.NOT_CONFIGURED)
        self.publish_blog_post(T0)
        r = self.worker.run(WorkContext(now=T0 + 3600, http=None, secrets_dir=self.state,
                                        state_dir=self.state))
        self.assertIsInstance(r, Err)
        self.assertEqual(r.error.kind, ErrorKind.NOT_CONFIGURED)

    def test_every_seed_topic_crossposts_cleanly(self) -> None:
        blog = self.blog
        rec = {"used_slugs": [], "used_topics": []}
        for topic in SEEDS:
            payload = blog._job(blog.assemble(good(slug=topic.key), topic, rec, T0)).payload
            self.assertEqual(self.worker.check_draft(crosspost(payload)), [], topic.key)


if __name__ == "__main__":
    unittest.main()
