"""The blog worker: one checked draft a day, submitted for the owner's approval.

The brain is a fake (``FakeBrain`` as ``ctx.words``, or ``FakeOllama`` behind the real
brain), Pionir is a fake (``FakeHands`` as ``ctx.job`` and ``ctx.approval``, or
``FakeClient`` behind the real hands). Each test fails if the rule it names is reverted:
more than one draft a day, a blocked draft submitted (or recorded without its reasons),
a submitted post counted as published before Pionir says it is done with a URL, a slug
used twice, or words reaching the worker any way but the shared brain.
"""

import ast
import json
import tempfile
import time
import unittest
from pathlib import Path

from crew_support import FakeOllama, temp_dir
from test_crew_fakes import FakeHttp, make_crew
from test_crew_leader import FakeAsk
from test_crew_leader import reply as leader_reply

from pionir.crew import blog as blog_module
from pionir.crew import contentcheck
from pionir.crew.blog import CAPABILITY, DRAFT_SCHEMA, SEEDS, BlogWorker
from pionir.crew.hands import JobOutcome
from pionir.crew.leader import Leader
from pionir.crew.registry import build_registry, default_registry, load_catalogue
from pionir.crew.result import Err, Ok
from pionir.crew.worker import ErrorKind, WorkContext

T0 = 1_790_000_000.0
DAY = 86400.0

BODY = """Checking an email address before you send to it saves bounced messages and a \
damaged sender reputation.

## Why addresses go bad

People mistype their address when they sign up. A missing letter in the domain or a \
throwaway domain looks fine in a form, but the message never arrives.

## What a good check looks at

- **Syntax:** is the address shaped like an address at all?
- **MX records:** does the domain accept mail?
- **Disposable domains:** is it a throwaway inbox?

The Email Verify API runs these checks in one request and returns a score, so you can \
decide whether to accept the sign-up or ask the person to fix a typo.
"""


def good(slug="verify-email-before-sending", title="Verify email addresses before you send"):
    return {"title": title, "slug": slug,
            "description": "Why checking syntax, MX records and typos before you send keeps "
                           "your messages out of the void.",
            "body_md": BODY, "tags": ["email", "Deliverability"]}


def bad():
    d = good()
    d["body_md"] = BODY + "\nA friend, Jane Doe, swears by it.\n"
    return d


class FakeBrain:
    """``ctx.words``: answers each call with the next scripted draft (or Err)."""

    def __init__(self, *answers, rewrites=None) -> None:
        self.answers = list(answers)
        self.calls: list = []
        # the sentence repairs (purpose "<draft purpose>_repair", postfix.py): kept apart
        # from the drafts, answered by ``rewrites`` (a callable of the user prompt, or a
        # fixed answer) - by default with no rewrite, so a scripted draft stays as it was
        self.repairs: list = []
        self.rewrites = rewrites

    def __call__(self, purpose, system, user, schema):
        if purpose.endswith("_repair"):
            self.repairs.append({"purpose": purpose, "user": user, "schema": schema})
            ans = self.rewrites(user) if callable(self.rewrites) else self.rewrites
            ans = {"rewrites": []} if ans is None else ans
            return ans if isinstance(ans, (Ok, Err)) else Ok(ans)
        self.calls.append({"purpose": purpose, "system": system, "user": user,
                           "schema": schema})
        ans = self.answers.pop(0) if self.answers else good()
        return ans if isinstance(ans, (Ok, Err)) else Ok(ans)


PARKED = JobOutcome("pending_approval", CAPABILITY, task_id="t-1", approval_id="ap-1",
                    error="held for Ian's approval - it has not run")


class FakeHands:
    """``ctx.job`` and ``ctx.approval``: Pionir parks every job; approvals are scripted."""

    def __init__(self, outcome: JobOutcome = PARKED) -> None:
        self.outcome = outcome
        self.jobs: list = []
        self.approvals: dict = {}

    def job(self, job):
        self.jobs.append(job)
        return self.outcome

    def approval(self, approval_id):
        return dict(self.approvals.get(approval_id, {"status": "pending"}))


def published(url):
    """Pionir's approval record once the owner said yes and the publish ran."""
    return {"id": "ap-1", "status": "approved",
            "result": {"ok": True, "agent_id": "content", "result": {"url": url},
                       "task_id": "t-2"}}


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.worker = default_registry().require("posting.blog")
        self.hands = FakeHands()

    def run_at(self, now, brain, goal=None, approval=True):
        ctx = WorkContext(now=now, http=None, secrets_dir=self.state, words=brain,
                          job=self.hands.job,
                          approval=self.hands.approval if approval else None,
                          goal=goal, state_dir=self.state)
        return self.worker.run(ctx)

    def record(self) -> dict:
        return json.loads(self.worker.record_path(self.state).read_text(encoding="utf-8"))

    @staticmethod
    def kinds(result) -> list:
        return [o.kind for o in result.value]

    @staticmethod
    def tally(result) -> dict:
        (t,) = [o for o in result.value if o.kind == "post.tally"]
        return {f.measures: f.value for f in t.figures}


class CatalogueTests(unittest.TestCase):
    def test_the_blog_placeholder_is_replaced_by_the_real_worker(self) -> None:
        w = default_registry().require("posting.blog")
        self.assertIsInstance(w, BlogWorker)
        self.assertTrue(w.live)
        self.assertEqual(w.draft_every_seconds, 86400)

    def test_every_seed_topic_links_to_a_page_and_its_links_pass_the_check(self) -> None:
        w = default_registry().require("posting.blog")
        rec = {"used_slugs": [], "used_topics": []}
        for topic in SEEDS:
            d = w.assemble(good(slug=topic.key), topic, rec, T0)
            self.assertEqual(contentcheck.check(d), [], topic.key)
            self.assertIn(f"https://api.dokaz.net{topic.path}?utm_source=blog", d["body_md"])


class SubmitTests(_Case):
    def test_a_passing_draft_is_submitted_as_content_publish_and_is_only_pending(self) -> None:
        result = self.run_at(T0, FakeBrain(good()))
        self.assertIsInstance(result, Ok)
        (job,) = self.hands.jobs
        self.assertEqual(job.capability, "content.publish")
        self.assertEqual(set(job.payload), set(contentcheck.FIELDS))
        # the exact text submitted is text the check passed, links and UTM tags included
        self.assertEqual(contentcheck.check(job.payload), [])
        self.assertIn("utm_source=blog&utm_medium=referral&utm_campaign=", job.payload["body_md"])
        self.assertEqual(job.permissions, ())            # nothing granted: Pionir parks it
        self.assertIn("post.pending_approval", self.kinds(result))
        self.assertNotIn("post.published", self.kinds(result))
        t = self.tally(result)
        self.assertEqual((t["drafts written"], t["posts submitted for approval"],
                          t["posts pending approval"], t["posts published"]), (1, 1, 1, 0))
        (post,) = self.record()["posts"]
        self.assertEqual((post["status"], post["approval_id"]), ("pending_approval", "ap-1"))

    def test_a_done_publish_records_the_url(self) -> None:
        self.run_at(T0, FakeBrain(good()))
        slug = self.hands.jobs[0].payload["slug"]
        url = f"https://api.dokaz.net/blog/{slug}"
        self.hands.approvals["ap-1"] = published(url)
        result = self.run_at(T0 + 3600, FakeBrain())
        (out,) = [o for o in result.value if o.kind == "post.published"]
        self.assertEqual(out.payload["url"], url)
        self.assertEqual(self.tally(result)["posts published"], 1)
        self.assertEqual(self.tally(result)["posts pending approval"], 0)
        self.assertEqual(self.record()["posts"][0]["status"], "published")

    def test_still_waiting_is_still_pending_never_published(self) -> None:
        self.run_at(T0, FakeBrain(good()))
        result = self.run_at(T0 + 3600, FakeBrain())    # the approval says "pending"
        self.assertEqual(self.tally(result)["posts published"], 0)
        self.assertEqual(self.tally(result)["posts pending approval"], 1)

    def test_done_without_a_url_is_not_published(self) -> None:
        self.run_at(T0, FakeBrain(good()))
        self.hands.approvals["ap-1"] = published(None) | {"result": {"ok": True,
                                                                    "result": {"note": "ok"}}}
        result = self.run_at(T0 + 3600, FakeBrain())
        self.assertNotIn("post.published", self.kinds(result))
        self.assertEqual(self.tally(result)["posts published"], 0)
        self.assertEqual(self.record()["posts"][0]["status"], "unconfirmed")

    def test_a_url_for_another_post_or_host_is_not_this_posts_url(self) -> None:
        self.run_at(T0, FakeBrain(good()))
        self.hands.approvals["ap-1"] = published("https://example.com/blog/something-else")
        result = self.run_at(T0 + 3600, FakeBrain())
        self.assertEqual(self.tally(result)["posts published"], 0)

    def test_a_denied_post_is_not_published(self) -> None:
        self.run_at(T0, FakeBrain(good()))
        self.hands.approvals["ap-1"] = {"id": "ap-1", "status": "denied", "reason": "expired"}
        result = self.run_at(T0 + 3600, FakeBrain())
        self.assertIn("post.not_published", self.kinds(result))
        self.assertEqual(self.tally(result)["posts denied"], 1)
        self.assertEqual(self.tally(result)["posts published"], 0)

    def test_a_publish_pionir_ran_without_parking_is_recorded_as_not_owner_approved(self) -> None:
        self.hands.outcome = JobOutcome(
            "done", CAPABILITY, task_id="t-3",
            result={"url": "https://api.dokaz.net/blog/verify-email-before-sending"})
        result = self.run_at(T0, FakeBrain(good()))
        self.assertEqual(self.tally(result)["posts published"], 1)
        self.assertIs(self.record()["posts"][0]["approved_by_owner"], False)

    def test_an_unreachable_submission_is_neither_pending_nor_published(self) -> None:
        self.hands.outcome = JobOutcome("unreachable", CAPABILITY, error="no route")
        result = self.run_at(T0, FakeBrain(good()))
        t = self.tally(result)
        self.assertEqual((t["posts submitted for approval"], t["posts published"]), (0, 0))
        self.assertEqual(self.record()["posts"][0]["status"], "unreachable")


class CheckTests(_Case):
    def test_a_failing_draft_is_never_submitted_and_is_recorded_with_its_reasons(self) -> None:
        brain = FakeBrain(bad(), bad(), bad())
        result = self.run_at(T0, brain)
        self.assertEqual(self.hands.jobs, [])
        blocked = [o for o in result.value if o.kind == "post.blocked"]
        self.assertEqual(len(blocked), 3)
        self.assertTrue(any("Jane Doe" in r for r in blocked[0].payload["reasons"]))
        self.assertTrue(all(o.derived for o in blocked))   # model words back no figure
        rec = self.record()
        self.assertEqual(len(rec["blocked"]), 3)
        self.assertTrue(any("Jane Doe" in r for r in rec["blocked"][0]["reasons"]))
        self.assertEqual(rec["posts"], [])
        t = self.tally(result)
        self.assertEqual((t["drafts written"], t["drafts blocked"],
                          t["posts submitted for approval"]), (3, 3, 0))
        # the flagged sentence was sent for repair after each draft, and nothing else
        self.assertEqual(len(brain.repairs), 3)
        self.assertIn("A friend, Jane Doe, swears by it.", brain.repairs[0]["user"])
        self.assertNotIn("Why addresses go bad", brain.repairs[0]["user"])

    def test_redrafts_per_run_with_the_reasons_in_the_prompt(self) -> None:
        brain = FakeBrain(bad(), bad(), bad(), good())
        self.run_at(T0, brain)
        self.assertEqual(len(brain.calls), 3)                # drafts_per_run, never a fourth
        self.assertNotIn("Jane Doe", brain.calls[0]["user"])
        self.assertIn("Jane Doe", brain.calls[1]["user"])
        self.assertIn("Jane Doe", brain.calls[2]["user"])
        self.assertEqual(self.hands.jobs, [])

    def test_a_blocked_run_with_budget_left_drafts_again_on_the_next_run(self) -> None:
        brain = FakeBrain(bad(), bad(), bad(), good())
        self.run_at(T0, brain)
        self.assertEqual(self.hands.jobs, [])
        self.assertIsNone(self.record()["last_drafted_at"])      # the day is not settled
        self.run_at(T0 + 6 * 3600, brain)
        self.assertEqual(len(brain.calls), 4)
        self.assertEqual(len(self.hands.jobs), 1)
        self.run_at(T0 + 12 * 3600, brain)                    # settled: nothing more today
        self.assertEqual(len(brain.calls), 4)

    def test_the_days_local_budget_is_a_hard_cap(self) -> None:
        brain = FakeBrain(*[bad()] * 20)
        for hours in (0, 6, 12, 18):
            self.run_at(T0 + hours * 3600, brain)
        self.assertEqual(len(brain.calls), self.worker.local_drafts_per_day)
        self.assertEqual(self.hands.jobs, [])
        rec = self.record()
        self.assertEqual(rec["today"]["local"], self.worker.local_drafts_per_day)
        self.assertEqual(rec["topic_blocks"][SEEDS[0].key], 1)    # one blocked DAY

    def test_a_model_repair_of_the_flagged_sentence_is_checked_and_submitted(self) -> None:
        def fix(user):
            n = int(user.split("\n")[-1].split(".", 1)[0])
            return {"rewrites": [{"id": n, "text": "A friend swears by it."}]}
        brain = FakeBrain(bad(), rewrites=fix)
        self.run_at(T0, brain)
        self.assertEqual(len(brain.calls), 1)                 # mended, not redrafted
        (job,) = self.hands.jobs
        self.assertNotIn("Jane Doe", job.payload["body_md"])
        self.assertIn("A friend swears by it.", job.payload["body_md"])
        self.assertEqual(contentcheck.check(job.payload), [])
        (post,) = self.record()["posts"]
        self.assertTrue(any("rewrote a sentence" in m for m in post["mended"]))

    def test_a_repair_that_keeps_the_name_is_still_blocked(self) -> None:
        def stubborn(user):
            n = int(user.split("\n")[-1].split(".", 1)[0])
            return {"rewrites": [{"id": n, "text": "My friend Jane Doe loves it."}]}
        brain = FakeBrain(bad(), bad(), bad(), rewrites=stubborn)
        self.run_at(T0, brain)
        self.assertEqual(self.hands.jobs, [])                 # the check decides, always
        self.assertTrue(all(any("Jane Doe" in r for r in b["reasons"])
                            for b in self.record()["blocked"]))

    def test_a_blocked_draft_then_a_clean_redraft_is_submitted(self) -> None:
        result = self.run_at(T0, FakeBrain(bad(), good()))
        self.assertEqual(len(self.hands.jobs), 1)
        self.assertEqual(contentcheck.check(self.hands.jobs[0].payload), [])
        self.assertEqual(self.kinds(result).count("post.blocked"), 1)


class CadenceTests(_Case):
    def test_it_drafts_at_most_once_a_day(self) -> None:
        brain = FakeBrain()
        self.run_at(T0, brain)
        for hours in (6, 12, 18, 23):
            result = self.run_at(T0 + hours * 3600, brain)
            self.assertIsInstance(result, Ok)
        self.assertEqual(len(brain.calls), 1)
        self.assertEqual(len(self.hands.jobs), 1)
        self.run_at(T0 + DAY, brain)
        self.assertEqual(len(brain.calls), 2)

    def test_a_brain_that_gave_no_words_does_not_use_up_the_day(self) -> None:
        brain = FakeBrain(Err("refused"))
        self.assertIsInstance(self.run_at(T0, brain), Err)
        self.run_at(T0 + 6 * 3600, brain)
        self.assertEqual(len(brain.calls), 2)
        self.assertEqual(len(self.hands.jobs), 1)


class TopicAndSlugTests(_Case):
    def test_it_never_reuses_a_slug_or_a_topic(self) -> None:
        brain = FakeBrain(good(), good(), good())      # the model repeats itself
        for day in range(3):
            self.run_at(T0 + day * DAY, brain)
        slugs = [j.payload["slug"] for j in self.hands.jobs]
        self.assertEqual(len(slugs), 3)
        self.assertEqual(len(set(slugs)), 3, slugs)
        topics = [p["topic"] for p in self.record()["posts"]]
        self.assertEqual(len(set(topics)), 3, topics)

    def test_a_blocked_day_does_not_use_up_the_topic(self) -> None:
        # the first real run blocked a topic and moved on: 14 seeds would be gone in a
        # fortnight with nothing published
        brain = FakeBrain(bad(), bad(), bad(), good())
        self.run_at(T0, brain)
        self.assertEqual(self.record()["used_topics"], [])
        self.run_at(T0 + DAY, brain)
        self.assertIn(SEEDS[0].subject, brain.calls[0]["user"])
        self.assertIn(SEEDS[0].subject, brain.calls[3]["user"])      # retried, then passed
        self.assertEqual(self.record()["used_topics"], [SEEDS[0].key])

    def test_a_topic_blocked_on_three_days_is_retired(self) -> None:
        from pionir.crew.blog import MAX_TOPIC_BLOCKS
        brain = FakeBrain(*[bad()] * (3 * MAX_TOPIC_BLOCKS))
        for day in range(MAX_TOPIC_BLOCKS):
            if day:
                self.assertEqual(self.record()["used_topics"], [], day)
            self.run_at(T0 + day * DAY, brain)
        self.assertEqual(self.record()["used_topics"], [SEEDS[0].key])

    def test_the_divisions_goal_steers_the_topic(self) -> None:
        brain = FakeBrain()
        self.run_at(T0, brain, goal="more search traffic to the QR code pages")
        self.assertIn("QR codes", brain.calls[0]["user"])
        self.assertIn("more search traffic", brain.calls[0]["user"])

    def test_a_topic_blocked_yesterday_starts_today_knowing_why(self) -> None:
        # temperature 0: without yesterday's reasons the first draft is yesterday's draft,
        # blocked again, and the topic burns its MAX_TOPIC_BLOCKS days
        brain = FakeBrain(bad(), bad(), bad(), good())
        self.run_at(T0, brain)
        self.assertNotIn("thrown away", brain.calls[0]["user"])     # a fresh topic: none
        self.run_at(T0 + DAY, brain)
        self.assertEqual(len(brain.calls), 4)
        today = brain.calls[3]["user"]
        self.assertIn(f"Topic: {SEEDS[0].subject}.", today)
        self.assertIn("thrown away", today)
        self.assertIn("'Jane Doe'", today)
        (job,) = self.hands.jobs
        self.assertEqual(self.record()["used_topics"], [SEEDS[0].key])

    def test_another_topics_block_is_not_fed_to_this_one(self) -> None:
        rec = self.worker.load(self.state)
        rec["blocked"] = [{"draft_id": "x", "topic": SEEDS[1].key, "reasons": ["other topic"],
                           "attempt": 1, "at": T0 - DAY}]
        self.worker.save(self.state, rec)
        brain = FakeBrain(good())
        self.run_at(T0, brain)
        self.assertNotIn("other topic", brain.calls[0]["user"])

    def test_a_goal_that_matches_no_seed_is_never_the_posts_subject(self) -> None:
        # live: Moss's instruction to the division would have become a post about itself,
        # linking to "/"; it only steers among the seeds, so the rotation goes on
        goal = ("report only what the workers actually measured, so the report passes the "
                "checks - then publish one good post a day")
        brain = FakeBrain(good(), good(slug="second-post", title="A second post to publish"))
        self.run_at(T0, brain, goal=goal)
        self.run_at(T0 + DAY, brain, goal=goal)
        self.assertTrue(brain.calls[0]["user"].startswith(f"Topic: {SEEDS[0].subject}."))
        self.assertTrue(brain.calls[1]["user"].startswith(f"Topic: {SEEDS[1].subject}."))
        self.assertIn(SEEDS[0].path, self.hands.jobs[0].payload["body_md"])
        self.assertEqual(self.record()["used_topics"], [SEEDS[0].key, SEEDS[1].key])

    def test_without_a_goal_it_takes_the_next_evergreen_seed(self) -> None:
        brain = FakeBrain()
        self.run_at(T0, brain)
        self.assertIn(SEEDS[0].subject, brain.calls[0]["user"])

    def test_an_unreadable_record_stops_it_before_any_draft(self) -> None:
        path = self.worker.record_path(self.state)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        brain = FakeBrain()
        result = self.run_at(T0, brain)
        self.assertIsInstance(result, Err)
        self.assertEqual(result.error.kind, ErrorKind.MALFORMED)
        self.assertEqual(brain.calls, [])


INVOICE = next(t for t in SEEDS if t.key == "invoice-pdf-from-json")
QR = next(t for t in SEEDS if t.key == "qr-codes-from-an-api")


def body_plus(extra: str) -> dict:
    d = good()
    d["body_md"] = BODY + "\n" + extra + "\n"
    return d


class RepairTests(unittest.TestCase):
    """The raw model words are repaired by a fixed rule before assembly - a Markdown link
    loses its target, an RFC 2606 documentation host becomes ``your-site`` - and nothing
    else: every other URL still blocks, and the full check runs on the final post."""

    def setUp(self) -> None:
        self.worker = default_registry().require("posting.blog")
        self.rec = {"used_slugs": [], "used_topics": []}

    def assemble(self, raw, topic=None) -> dict:
        return self.worker.assemble(raw, topic or SEEDS[0], self.rec, T0)

    def test_a_model_link_is_unlinked_and_its_anchor_text_kept(self) -> None:
        # live: the model linked api.dokaz.net itself, without the post's UTM tags
        d = self.assemble(body_plus(
            "Read the [Email Verify guide](https://api.dokaz.net/docs/email-verification-api) "
            "first."))
        self.assertIn("Read the Email Verify guide first.", d["body_md"])
        self.assertNotIn("](https://api.dokaz.net/docs/email-verification-api)", d["body_md"])
        self.assertEqual(self.worker.check_draft(d), [])
        self.assertEqual(d["repaired"], ["body_md: unlinked 'Email Verify guide'"])

    def test_a_documentation_host_in_a_code_fence_and_in_prose_becomes_a_placeholder(self) -> None:
        # live: the QR post encoded https://www.example.com and was blocked twice for it
        d = self.assemble(body_plus(
            'Point the code at www.example.com or shop.example.\n\n```json\n'
            '{"data": "https://www.example.com/menu?table=4", "format": "svg"}\n```'), QR)
        self.assertNotIn("example", d["body_md"].replace("examples", ""))
        # the JSON block's example values are placeholders now (postfix.scrub_code_blocks)
        self.assertIn('"data": "{data}"', d["body_md"])
        self.assertIn("Point the code at your-site or your-site.", d["body_md"])
        self.assertEqual(self.worker.check_draft(d), [])
        self.assertEqual(len(d["repaired"]), 4)
        self.assertIn("body_md: example values in a JSON block made placeholders",
                      d["repaired"])

    def test_a_reserved_email_address_is_left_alone(self) -> None:
        # user@example.com is an address the check already allows; "user@your-site" would
        # be an @-handle
        d = self.assemble(body_plus("A test address such as user@example.com never bounces."))
        self.assertIn("user@example.com", d["body_md"])
        self.assertNotIn("repaired", d)
        self.assertEqual(self.worker.check_draft(d), [])

    def test_a_real_off_site_url_still_blocks(self) -> None:
        for extra in ("See https://mailcheck-tools.com/docs for more.",
                      "See [https://mailcheck-tools.com](https://mailcheck-tools.com).",
                      "See www.mailcheck-tools.com for more.",
                      "See https://api.dokaz.net/docs/qr-code-api for more."):
            d = self.assemble(body_plus(extra))
            reasons = self.worker.check_draft(d)
            self.assertTrue(reasons, extra)
            self.assertTrue(any("mailcheck-tools.com" in r or "utm_source" in r
                                for r in reasons), (extra, reasons))

    def test_the_workers_own_utm_links_are_untouched(self) -> None:
        d = self.assemble(body_plus("Read the [guide](https://www.example.com/guide)."))
        footer = BlogWorker.links(SEEDS[0], d["draft_id"])
        self.assertTrue(d["body_md"].endswith(footer))
        self.assertEqual(d["body_md"].count("utm_source=blog"), footer.count("utm_source=blog"))
        self.assertEqual(self.worker.check_draft(d), [])

    def test_a_heading_deeper_than_three_is_made_three_and_its_words_still_checked(self) -> None:
        # live 2026-10-06 and 10-07: the Wi-Fi post's second draft was blocked only for a ####
        d = self.assemble(body_plus("#### Scan the code\n\nPoint the camera at it."))
        self.assertIn("\n### Scan the code\n", d["body_md"])
        self.assertNotIn("####", d["body_md"])
        self.assertEqual(self.worker.check_draft(d), [])
        self.assertIn("body_md: 1 heading(s) deeper than ### made ###", d["repaired"])
        d = self.assemble(body_plus("#### Ask Jane Doe"))
        self.assertTrue(any("Jane Doe" in r for r in self.worker.check_draft(d)))

    def test_a_hash_line_inside_a_code_fence_is_not_made_a_heading(self) -> None:
        # a fence that is not JSON is removed whole (postfix.py); its lines never become
        # headings on the way out
        d = self.assemble(body_plus("```\n#### not a heading\n```"))
        self.assertNotIn("not a heading", d["body_md"])
        self.assertEqual(d["repaired"],
                         ["body_md: removed a code block that is not JSON ('#### not a heading')"])

    def test_repaired_text_still_fails_on_every_other_reason(self) -> None:
        d = self.assemble(body_plus("Ask [Jane Doe](https://example.com/jane) at "
                                    "jane@realmail.com about invoice INV-2024-001."))
        self.assertIn("Ask Jane Doe at", d["body_md"])
        reasons = self.worker.check_draft(d)
        self.assertTrue(any("Jane Doe" in r for r in reasons), reasons)
        self.assertTrue(any("email address" in r for r in reasons), reasons)
        self.assertTrue(any("INV-2024-001" in r for r in reasons), reasons)

    def test_the_record_keeps_what_was_repaired(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = Path(tmp.name)
        hands = FakeHands()
        raw = body_plus("Ask [Jane Doe](https://example.com/jane).")
        ctx = WorkContext(now=T0, http=None, secrets_dir=state, words=FakeBrain(raw, raw, raw),
                          job=hands.job, approval=hands.approval, state_dir=state)
        self.worker.run(ctx)
        rec = json.loads(self.worker.record_path(state).read_text(encoding="utf-8"))
        self.assertEqual(rec["blocked"][0]["repaired"], ["body_md: unlinked 'Jane Doe'"])
        self.assertEqual(hands.jobs, [])
        # and a passing repaired draft is submitted without the note: it is never published
        hands2, raw2 = FakeHands(), body_plus("Read the [guide](https://www.example.com).")
        state2 = state / "two"
        ctx = WorkContext(now=T0, http=None, secrets_dir=state2, words=FakeBrain(raw2),
                          job=hands2.job, approval=hands2.approval, state_dir=state2)
        self.worker.run(ctx)
        (job,) = hands2.jobs
        self.assertEqual(set(job.payload), set(contentcheck.FIELDS))
        rec = json.loads(self.worker.record_path(state2).read_text(encoding="utf-8"))
        self.assertEqual(rec["posts"][0]["repaired"], ["body_md: unlinked 'guide'"])


class OfferTests(unittest.TestCase):
    """Every post's footer links to a paid offer: the live Gumroad product its topic fits,
    or the /hire page - with the post's UTM tags, so the visit or the sale is tied to it."""

    def setUp(self) -> None:
        self.worker = default_registry().require("posting.blog")

    def test_every_post_links_a_paid_offer_with_its_utm_tags_and_passes_both_checks(self) -> None:
        from pionir.adapters.content import check_draft as pionir_check
        for topic in SEEDS:
            d = self.worker.assemble(good(slug=topic.key), topic, {"used_slugs": []}, T0)
            want = (blog_module.OFFERS[topic.key].url if topic.key in blog_module.OFFERS
                    else "https://api.dokaz.net/hire")
            q = contentcheck.utm_query(d["draft_id"])
            self.assertIn(f"]({want}?{q})", d["body_md"], topic.key)
            self.assertEqual(self.worker.check_draft(d), [], topic.key)
            pionir_check(d)                        # Pionir's own validator: no exception

    def test_the_invoice_post_links_the_live_invoice_product(self) -> None:
        d = self.worker.assemble(good(), INVOICE, {"used_slugs": []}, T0)
        self.assertIn("https://dokaz.gumroad.com/l/obol-pro?utm_source=blog&utm_medium="
                      "referral&utm_campaign=", d["body_md"])
        self.assertNotIn("/hire", d["body_md"])
        q = BlogWorker.links(QR, "2026-10-04-qr")
        self.assertIn("https://api.dokaz.net/hire?utm_source=blog", q)
        self.assertNotIn("gumroad", q)

    def test_the_results_loop_reads_the_offers_campaign_as_the_posts(self) -> None:
        from urllib.parse import parse_qs, urlsplit

        from pionir.crew import devto, results
        d = self.worker.assemble(good(), INVOICE, {"used_slugs": []}, T0)
        (url,) = [u for u in contentcheck.links_in(d["body_md"]) if "gumroad" in u]
        q = parse_qs(urlsplit(url).query)
        stored = "/".join(results.utm_part(q[k][0])
                          for k in ("utm_source", "utm_medium", "utm_campaign"))
        self.assertEqual(results.split_campaign(stored),
                         ("blog", "referral", results._post_campaign(d)))
        # the dev.to copy of the post retags it like every other Dokaz link
        self.assertIn("obol-pro?utm_source=devto&", devto.swap_source(d["body_md"]))

    def test_only_live_products_are_offered(self) -> None:
        live = {"obol-pro", "metron", "approval-gate", "card-press", "post-guard"}
        for key, offer in blog_module.OFFERS.items():
            self.assertIn(key, {t.key for t in SEEDS})
            self.assertTrue(offer.url.startswith("https://dokaz.gumroad.com/l/"), key)
            self.assertIn(offer.url.rsplit("/", 1)[1], live, key)


class PromptTests(unittest.TestCase):
    PLACEHOLDERS = ("{invoice-number}", "{company-name}", "{customer-email}",
                    "{customer-name}", "{street-address}", "{customer-handle}", "{order-id}")

    def test_the_placeholders_the_prompt_asks_for_pass_both_validators(self) -> None:
        from pionir.adapters.content import check_draft as pionir_check
        w = default_registry().require("posting.blog")
        lines = [f"The field holds {p} until you fill it in." for p in self.PLACEHOLDERS]
        code = ",\n".join(f'  "{p[1:-1].replace("-", "_")}": "{p}"' for p in self.PLACEHOLDERS)
        d = w.assemble(body_plus("\n".join(lines) + "\n\n```json\n{\n" + code + "\n}\n```"),
                       INVOICE, {"used_slugs": []}, T0)
        self.assertEqual(w.check_draft(d), [])
        pionir_check(d)
        # the form the prompt does NOT ask for: an angle bracket reads as raw HTML to both
        # checks, and the worker's assembly takes the brackets off (postfix.unbracket_tags)
        d = w.assemble(body_plus("The field holds <invoice-number> until then."), INVOICE,
                       {"used_slugs": []}, T0)
        self.assertIn("The field holds invoice-number until then.", d["body_md"])
        self.assertEqual(w.check_draft(d), [])
        raw = dict(d, body_md=d["body_md"].replace("holds invoice-number", "holds <invoice-number>"))
        raw.pop("repaired", None)
        self.assertTrue(any("raw HTML" in r for r in w.check_draft(raw)))
        with self.assertRaises(ValueError):
            pionir_check(raw)

    def test_the_system_prompt_offers_those_placeholders_and_the_new_rules(self) -> None:
        system = default_registry().require("posting.blog")._system()
        for p in ("{invoice-number}", "{company-name}", "{customer-email}"):
            self.assertIn(p, system)
        self.assertIn("never spell it out", system)
        self.assertIn("no #### headings", system)

    def test_a_link_reason_is_fed_back_as_write_no_links_without_the_host_list(self) -> None:
        hosts = "api.dokaz.net, dokaz.gumroad.com, dokazindustries.com, www.dokazindustries.com"
        reasons = [
            (f"Pionir's publish check refuses it: body_md: links may only go to {hosts}; "
             "not 'www.example.com'"),
            (f"body_md: link 'https://www.example.com' goes to www.example.com; posts link "
             f"only to {hosts}"),
            ("body_md: link 'https://api.dokaz.net' does not carry utm_source=blog (this "
             "post's UTM tags, TRAFFIC.md)"),
            ("body_md: 'www.foo.com' is a bare www address; links are full https URLs with "
             "UTM tags"),
            "body_md: names the website 'foo.com'",
            ("names 'Scalable', which is not on the allowlist of names a post may use (a "
             "person, place or company blocks the post)"),
            ("Pionir's publish check refuses it: body_md: raw HTML tags are not allowed "
             "(Markdown only; write links as [text](url))"),
        ]
        prompt = BlogWorker._prompt(QR, None, reasons)
        self.assertIn("remove every URL and link; write none", prompt)
        self.assertEqual(prompt.count("write none"), 1)
        self.assertNotIn("dokaz.gumroad.com", prompt)
        self.assertNotIn("api.dokaz.net", prompt)
        self.assertNotIn("utm_source", prompt)
        self.assertIn("'Scalable'", prompt)             # every other reason, as it was
        self.assertIn("raw HTML tags are not allowed", prompt)


class WordsOnlyThroughTheBrainTests(unittest.TestCase):
    def test_the_blog_and_check_modules_import_nothing_that_can_call_a_model(self) -> None:
        for mod in (blog_module, contentcheck):
            tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
            names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    names.add(node.module or "")
                elif isinstance(node, ast.Import):
                    names |= {a.name for a in node.names}
            # urllib.parse (reading a URL) is fine; anything that can open a connection is not
            for bad_name in ("brain", "budget", "escalation", "leader", "urllib.request",
                             "subprocess", "http.client", "socket", "runtime", "net"):
                self.assertFalse(any(n == bad_name or n.endswith("." + bad_name)
                                     or n.startswith(bad_name) for n in names),
                                 (mod.__name__, bad_name, names))

    def test_without_the_brain_there_is_no_draft(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            w = default_registry().require("posting.blog")
            hands = FakeHands()
            result = w.run(WorkContext(now=T0, http=None, secrets_dir=Path(d), words=None,
                                       job=hands.job, state_dir=Path(d)))
            self.assertIsInstance(result, Err)
            self.assertEqual(result.error.kind, ErrorKind.NOT_CONFIGURED)
            self.assertEqual(hands.jobs, [])


class FakeClient:
    """Pionir behind the real hands: parks content.publish; lists its approval queue."""

    def __init__(self) -> None:
        self.calls: list = []
        self.queue: dict = {"pending": [], "recent": []}

    def run_task(self, capability, payload, *, permissions=(), wait=30.0):
        self.calls.append((capability, dict(payload), tuple(permissions)))
        self.queue["pending"].append({"id": "ap-9", "status": "pending",
                                      "capability": capability})
        return {"ok": False, "status": "pending_approval", "approval_id": "ap-9",
                "summary": "content . content.publish", "task_id": "t-9",
                "note": "held for Ian's approval - it has not run"}

    def task(self, task_id, *, wait=0.0):
        raise AssertionError("no test job keeps running")

    def approvals(self):
        return self.queue


class CrewTests(unittest.TestCase):
    """The worker inside a real crew: words via the real brain, the job via the real hands,
    and the posting leader reading what it recorded."""

    def setUp(self) -> None:
        tmp = temp_dir()
        self.addCleanup(tmp.cleanup)
        cat = load_catalogue()
        cat["divisions"] = [d for d in cat["divisions"] if d["id"] == "posting"]
        self.post = FakeOllama(reply=json.dumps(good()))
        self.crew = make_crew(tmp.name, registry=build_registry(cat), post=self.post,
                              http=FakeHttp())
        self.addCleanup(self.crew.stop)
        self.client = FakeClient()
        self.crew.hands.client = self.client
        self.crew.brain.start()
        self.crew.hands.start()

    def test_the_model_is_reached_only_through_the_shared_brain(self) -> None:
        self.crew.dispatcher.dispatch(only=("posting.blog",), wait=True)
        self.assertEqual(len(self.post.calls), 1)
        _url, body = self.post.calls[0]
        self.assertEqual((body["format"], body["options"]["temperature"]), (DRAFT_SCHEMA, 0))
        self.assertEqual(self.crew.store.calls_last_hour("posting"), 1)   # charged to posting
        (call,) = self.client.calls
        self.assertEqual((call[0], call[2]), ("content.publish", ()))
        self.assertEqual(self.crew.hands.approval("ap-9")["status"], "pending")

    def test_the_posting_leader_reads_blocked_reasons_pending_and_the_rule(self) -> None:
        self.post.reply = json.dumps(bad())
        self.crew.dispatcher.dispatch(only=("posting.blog",), wait=True)
        ask = FakeAsk(leader_reply(summary="Two drafts were blocked; nothing is waiting.",
                                   figures=[]))
        lead = Leader("posting", self.crew.registry, self.crew.store, ask=ask, model="m",
                      clock=time.time)
        lead.run()
        system, user = (m["content"] for m in ask.calls[0]["messages"])
        self.assertIn("PENDING APPROVAL has NOT been published", system)
        self.assertIn("post.blocked", user)
        self.assertIn("Jane Doe", user)
        self.assertIn("3 drafts blocked", user)

    def test_the_hands_say_unreachable_when_pionir_cannot_list_approvals(self) -> None:
        self.crew.hands.client = object()
        self.assertEqual(self.crew.hands.approval("ap-1")["status"], "unreachable")


if __name__ == "__main__":
    unittest.main()
