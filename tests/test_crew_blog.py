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

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.calls: list = []

    def __call__(self, purpose, system, user, schema):
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
        brain = FakeBrain(bad(), bad())
        result = self.run_at(T0, brain)
        self.assertEqual(self.hands.jobs, [])
        blocked = [o for o in result.value if o.kind == "post.blocked"]
        self.assertEqual(len(blocked), 2)
        self.assertTrue(any("Jane Doe" in r for r in blocked[0].payload["reasons"]))
        self.assertTrue(all(o.derived for o in blocked))   # model words back no figure
        rec = self.record()
        self.assertEqual(len(rec["blocked"]), 2)
        self.assertTrue(any("Jane Doe" in r for r in rec["blocked"][0]["reasons"]))
        self.assertEqual(rec["posts"], [])
        t = self.tally(result)
        self.assertEqual((t["drafts written"], t["drafts blocked"],
                          t["posts submitted for approval"]), (2, 2, 0))

    def test_one_redraft_per_run_with_the_reasons_in_the_prompt(self) -> None:
        brain = FakeBrain(bad(), bad(), good())
        self.run_at(T0, brain)
        self.assertEqual(len(brain.calls), 2)                # never a third in one run
        self.assertNotIn("Jane Doe", brain.calls[0]["user"])
        self.assertIn("Jane Doe", brain.calls[1]["user"])

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
        brain = FakeBrain(bad(), bad(), good())
        self.run_at(T0, brain)
        self.assertEqual(self.record()["used_topics"], [])
        self.run_at(T0 + DAY, brain)
        self.assertIn(SEEDS[0].subject, brain.calls[0]["user"])
        self.assertIn(SEEDS[0].subject, brain.calls[2]["user"])      # retried, then passed
        self.assertEqual(self.record()["used_topics"], [SEEDS[0].key])

    def test_a_topic_blocked_on_three_days_is_retired(self) -> None:
        from pionir.crew.blog import MAX_TOPIC_BLOCKS
        brain = FakeBrain(*[bad()] * (2 * MAX_TOPIC_BLOCKS))
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
        self.assertIn("2 drafts blocked", user)

    def test_the_hands_say_unreachable_when_pionir_cannot_list_approvals(self) -> None:
        self.crew.hands.client = object()
        self.assertEqual(self.crew.hands.approval("ap-1")["status"], "unreachable")


if __name__ == "__main__":
    unittest.main()
