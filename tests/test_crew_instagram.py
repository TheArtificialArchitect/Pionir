"""The Instagram worker: one checked card post a day, submitted for the owner's approval.

The brain is a fake (``FakeBrain`` as ``ctx.words``, or ``FakeOllama`` behind the real
brain), Pionir is a fake (``FakeHands`` as ``ctx.job`` and ``ctx.approval``, or
``FakeClient`` behind the real hands). Each test fails if the rule it names is reverted:
more than one draft a day, a blocked draft submitted (or recorded without its reasons), a
payload without the card's hash, a submitted post counted as published before Pionir says
it is done with an instagram.com permalink, a card that does not fit crashing the run, a
blocked day using up a topic, or the card not kept beside the record.
"""

import ast
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path

from crew_support import FakeOllama, temp_dir
from test_crew_blog import FakeBrain, FakeHands
from test_crew_fakes import FakeHttp, make_crew
from test_crew_leader import FakeAsk
from test_crew_leader import reply as leader_reply

from pionir.crew import contentcheck
from pionir.crew import instagram as instagram_module
from pionir.crew.blog import MAX_TOPIC_BLOCKS, SEEDS
from pionir.crew.hands import JobOutcome
from pionir.crew.instagram import CAPABILITY, DRAFT_SCHEMA, KEEP_CARDS, InstagramWorker
from pionir.crew.leader import Leader
from pionir.crew.registry import build_registry, default_registry, load_catalogue
from pionir.crew.result import Err, Ok
from pionir.crew.worker import ErrorKind, WorkContext
from pionir.social.card import card_sha

T0 = 1_790_000_000.0          # 2026-09-21
DAY = 86400.0
PERMALINK = "https://www.instagram.com/p/C0ffee123/"


def good(headline="Check an email address before you hit send"):
    """What the model answers: the words only, no id and no hash."""
    return {"headline": headline,
            "points": ["A typo in the domain bounces, and bounces hurt everything after.",
                       "Syntax, domain and mail server: three checks, one call.",
                       "Catch throwaway addresses at sign-up, not after."],
            "caption": "Every bounced message chips away at how inboxes treat your mail. "
                       "Checking the address first is cheap, fast, and keeps your sender "
                       "reputation clean. The Email Verify API does it in one request. The "
                       "full guide is at the link in bio.",
            "hashtags": ["#EmailDeliverability", "webdev"]}


def bad():
    d = good()
    d["caption"] += " A friend, Jane Doe, swears by it."
    return d


def too_wide():
    d = good()
    d["points"] = ["w" * 60]            # one word the card cannot hold
    return d


def published(link):
    """Pionir's approval record once the owner said yes and the post ran."""
    return {"id": "ap-1", "status": "approved",
            "result": {"ok": True, "agent_id": "instagram",
                       "result": {"permalink": link, "media_id": "1790"}, "task_id": "t-2"}}


PARKED = JobOutcome("pending_approval", CAPABILITY, task_id="t-1", approval_id="ap-1",
                    error="held for Ian's approval - it has not run")


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.worker = default_registry().require("posting.instagram")
        self.hands = FakeHands(PARKED)

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
    def test_the_instagram_placeholder_is_replaced_by_the_real_worker(self) -> None:
        w = default_registry().require("posting.instagram")
        self.assertIsInstance(w, InstagramWorker)
        self.assertTrue(w.live)
        self.assertEqual(w.draft_every_seconds, 86400)

    def test_the_posting_leader_is_told_about_instagram(self) -> None:
        notes = default_registry().division("posting").leader_notes
        self.assertIn("posting.instagram", notes)
        self.assertIn("PENDING APPROVAL has NOT been published", notes)
        self.assertIn("permalink", notes)


class SubmitTests(_Case):
    def test_a_passing_draft_is_submitted_with_its_card_and_is_only_pending(self) -> None:
        result = self.run_at(T0, FakeBrain(good()))
        self.assertIsInstance(result, Ok)
        (job,) = self.hands.jobs
        self.assertEqual(job.capability, "social.instagram_post")
        p = job.payload
        self.assertEqual(set(p), {"draft_id", "headline", "points", "caption", "hashtags",
                                  "card_sha"})
        # the card's hash pins the exact image rendered from the text submitted
        self.assertEqual(p["card_sha"], card_sha(p["headline"], p["points"]))
        self.assertEqual(contentcheck.check_social(p), [])      # exactly what was checked
        self.assertEqual(p["draft_id"], "2026-09-21-ig-check-an-email-address-before-you-hit"
                                        "-send")
        self.assertEqual(p["hashtags"], ["emaildeliverability", "webdev"])
        self.assertEqual(job.permissions, ())            # nothing granted: Pionir parks it
        self.assertIn("post.pending_approval", self.kinds(result))
        self.assertNotIn("post.published", self.kinds(result))
        t = self.tally(result)
        self.assertEqual((t["drafts written"], t["posts submitted for approval"],
                          t["posts pending approval"], t["posts published"]), (1, 1, 1, 0))
        (post,) = self.record()["posts"]
        self.assertEqual((post["status"], post["approval_id"]), ("pending_approval", "ap-1"))
        self.assertNotIn("permalink", post)

    def test_a_done_post_with_a_permalink_is_published(self) -> None:
        self.run_at(T0, FakeBrain(good()))
        self.hands.approvals["ap-1"] = published(PERMALINK)
        result = self.run_at(T0 + 3600, FakeBrain())
        (out,) = [o for o in result.value if o.kind == "post.published"]
        self.assertEqual(out.payload["permalink"], PERMALINK)
        t = self.tally(result)
        self.assertEqual((t["posts published"], t["posts pending approval"]), (1, 0))
        (post,) = self.record()["posts"]
        self.assertEqual((post["status"], post["permalink"]), ("published", PERMALINK))

    def test_still_waiting_is_still_pending_never_published(self) -> None:
        self.run_at(T0, FakeBrain(good()))
        result = self.run_at(T0 + 3600, FakeBrain())
        t = self.tally(result)
        self.assertEqual((t["posts published"], t["posts pending approval"]), (0, 1))

    def test_done_without_an_instagram_permalink_is_not_published(self) -> None:
        for link in (None, "http://www.instagram.com/p/C0ffee123/",
                     "https://example.com/p/C0ffee123/", "https://www.instagram.com/",
                     "https://instagram.com.evil.example/p/x/"):
            with self.subTest(link):
                self.setUp()
                self.run_at(T0, FakeBrain(good()))
                self.hands.approvals["ap-1"] = published(link)
                result = self.run_at(T0 + 3600, FakeBrain())
                self.assertNotIn("post.published", self.kinds(result))
                self.assertEqual(self.tally(result)["posts published"], 0)
                self.assertEqual(self.record()["posts"][0]["status"], "unconfirmed")

    def test_a_denied_post_is_recorded_denied(self) -> None:
        self.run_at(T0, FakeBrain(good()))
        self.hands.approvals["ap-1"] = {"id": "ap-1", "status": "denied", "reason": "no"}
        result = self.run_at(T0 + 3600, FakeBrain())
        self.assertIn("post.not_published", self.kinds(result))
        t = self.tally(result)
        self.assertEqual((t["posts denied"], t["posts published"],
                          t["posts pending approval"]), (1, 0, 0))
        self.assertEqual(self.record()["posts"][0]["status"], "denied")

    def test_a_failed_post_is_recorded_failed(self) -> None:
        self.run_at(T0, FakeBrain(good()))
        self.hands.approvals["ap-1"] = {"id": "ap-1", "status": "approved_failed",
                                        "result": {"ok": False, "error": "token expired"}}
        self.run_at(T0 + 3600, FakeBrain())
        post = self.record()["posts"][0]
        self.assertEqual((post["status"], post["why"]), ("failed", "token expired"))

    def test_a_post_pionir_ran_without_parking_is_recorded_as_not_owner_approved(self) -> None:
        self.hands.outcome = JobOutcome("done", CAPABILITY, task_id="t-3",
                                        result={"permalink": PERMALINK})
        result = self.run_at(T0, FakeBrain(good()))
        self.assertEqual(self.tally(result)["posts published"], 1)
        self.assertIs(self.record()["posts"][0]["approved_by_owner"], False)


class CheckTests(_Case):
    def test_a_failing_draft_is_never_submitted_and_is_recorded_with_its_reasons(self) -> None:
        result = self.run_at(T0, FakeBrain(bad(), bad()))
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
        self.assertEqual(brain.calls[0]["schema"], DRAFT_SCHEMA)
        self.assertIn("link in bio", brain.calls[0]["system"])

    def test_a_blocked_draft_then_a_clean_redraft_is_submitted(self) -> None:
        result = self.run_at(T0, FakeBrain(bad(), good()))
        self.assertEqual(len(self.hands.jobs), 1)
        self.assertEqual(contentcheck.check_social(self.hands.jobs[0].payload), [])
        self.assertEqual(self.kinds(result).count("post.blocked"), 1)

    def test_text_that_does_not_fit_the_card_is_a_block_not_a_crash(self) -> None:
        result = self.run_at(T0, FakeBrain(too_wide(), too_wide()))
        self.assertIsInstance(result, Ok)
        self.assertEqual(self.hands.jobs, [])
        reasons = self.record()["blocked"][0]["reasons"]
        self.assertTrue(any(r.startswith("card:") and "too wide" in r for r in reasons),
                        reasons)
        self.assertEqual(self.tally(result)["drafts blocked"], 2)

    def test_a_caption_that_does_not_point_to_the_bio_is_blocked(self) -> None:
        d = good()
        d["caption"] = d["caption"].replace(" The full guide is at the link in bio.", "")
        self.run_at(T0, FakeBrain(d, d))
        self.assertEqual(self.hands.jobs, [])
        self.assertTrue(any("link in bio" in r for r in self.record()["blocked"][0]["reasons"]))

    def test_a_brain_answer_that_is_not_a_post_is_blocked(self) -> None:
        result = self.run_at(T0, FakeBrain({"headline": 7}, "nonsense"))
        self.assertIsInstance(result, Ok)
        self.assertEqual(self.hands.jobs, [])
        self.assertEqual(self.tally(result)["drafts blocked"], 2)


class CardFileTests(_Case):
    def test_the_submitted_card_is_saved_beside_the_record(self) -> None:
        self.run_at(T0, FakeBrain(good()))
        (job,) = self.hands.jobs
        (post,) = self.record()["posts"]
        path = self.state / post["card"]
        self.assertEqual(path, self.worker.cards_dir(self.state) / f"{job.payload['draft_id']}"
                                                                   ".jpg")
        self.assertEqual(path.parent.parent, self.worker.record_path(self.state).parent)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), job.payload["card_sha"])

    def test_a_blocked_draft_saves_no_card(self) -> None:
        self.run_at(T0, FakeBrain(bad(), bad()))
        folder = self.worker.cards_dir(self.state)
        self.assertEqual(list(folder.glob("*.jpg")) if folder.exists() else [], [])

    def test_only_the_newest_cards_are_kept(self) -> None:
        folder = self.worker.cards_dir(self.state)
        folder.mkdir(parents=True)
        for n in range(KEEP_CARDS + 5):
            (folder / f"2025-01-{n + 1:02d}-ig-old.jpg").write_bytes(b"old")
        self.run_at(T0, FakeBrain(good()))
        kept = sorted(p.name for p in folder.glob("*.jpg"))
        self.assertEqual(len(kept), KEEP_CARDS)
        self.assertEqual(kept[-1], f"{self.hands.jobs[0].payload['draft_id']}.jpg")
        self.assertNotIn("2025-01-01-ig-old.jpg", kept)

    def test_a_card_that_cannot_be_saved_does_not_stop_the_post(self) -> None:
        self.worker.cards_dir(self.state).write_text("a file where the folder should be")
        result = self.run_at(T0, FakeBrain(good()))
        self.assertIsInstance(result, Ok)
        self.assertEqual(len(self.hands.jobs), 1)
        self.assertNotIn("card", self.record()["posts"][0])


class CadenceTests(_Case):
    def test_it_drafts_at_most_once_a_day(self) -> None:
        brain = FakeBrain(*[good()] * 3)
        self.run_at(T0, brain)
        for hours in (6, 12, 18, 23):
            self.assertIsInstance(self.run_at(T0 + hours * 3600, brain), Ok)
        self.assertEqual(len(brain.calls), 1)
        self.assertEqual(len(self.hands.jobs), 1)
        self.run_at(T0 + DAY, brain)
        self.assertEqual(len(brain.calls), 2)

    def test_a_brain_that_gave_no_words_does_not_use_up_the_day(self) -> None:
        brain = FakeBrain(Err("refused"), good())
        self.assertIsInstance(self.run_at(T0, brain), Err)
        self.run_at(T0 + 6 * 3600, brain)
        self.assertEqual(len(brain.calls), 2)
        self.assertEqual(len(self.hands.jobs), 1)


class TopicTests(_Case):
    def test_without_a_goal_it_takes_the_next_blog_seed_on_its_own_record(self) -> None:
        # the blog having used a topic does not stop Instagram from using it
        blog = default_registry().require("posting.blog")
        blog.save(self.state, {**blog.load(self.state), "used_topics": [SEEDS[0].key]})
        brain = FakeBrain(good(), good())
        self.run_at(T0, brain)
        self.assertIn(SEEDS[0].subject, brain.calls[0]["user"])
        self.run_at(T0 + DAY, brain)
        self.assertIn(SEEDS[1].subject, brain.calls[1]["user"])
        self.assertEqual(self.record()["used_topics"], [SEEDS[0].key, SEEDS[1].key])

    def test_the_divisions_goal_steers_the_topic(self) -> None:
        brain = FakeBrain(good())
        self.run_at(T0, brain, goal="more traffic to the QR code pages")
        self.assertIn("QR codes", brain.calls[0]["user"])
        self.assertIn("more traffic", brain.calls[0]["user"])

    def test_a_blocked_day_does_not_use_up_the_topic(self) -> None:
        brain = FakeBrain(bad(), bad(), good())
        self.run_at(T0, brain)
        self.assertEqual(self.record()["used_topics"], [])
        self.run_at(T0 + DAY, brain)
        self.assertIn(SEEDS[0].subject, brain.calls[0]["user"])
        self.assertIn(SEEDS[0].subject, brain.calls[2]["user"])      # retried, then passed
        self.assertEqual(self.record()["used_topics"], [SEEDS[0].key])

    def test_a_topic_blocked_on_three_days_is_retired(self) -> None:
        brain = FakeBrain(*[bad()] * (2 * MAX_TOPIC_BLOCKS))
        for day in range(MAX_TOPIC_BLOCKS):
            if day:
                self.assertEqual(self.record()["used_topics"], [], day)
            self.run_at(T0 + day * DAY, brain)
        self.assertEqual(self.record()["used_topics"], [SEEDS[0].key])

    def test_an_unreadable_record_stops_it_before_any_draft(self) -> None:
        path = self.worker.record_path(self.state)
        path.write_text("{not json", encoding="utf-8")
        brain = FakeBrain()
        result = self.run_at(T0, brain)
        self.assertIsInstance(result, Err)
        self.assertEqual(result.error.kind, ErrorKind.MALFORMED)
        self.assertEqual(brain.calls, [])


class WordsOnlyThroughTheBrainTests(unittest.TestCase):
    def test_the_instagram_module_imports_nothing_that_can_call_a_model(self) -> None:
        tree = ast.parse(Path(instagram_module.__file__).read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names.add(node.module or "")
            elif isinstance(node, ast.Import):
                names |= {a.name for a in node.names}
        for bad_name in ("brain", "budget", "escalation", "leader", "urllib.request",
                         "subprocess", "http.client", "socket", "runtime", "net"):
            self.assertFalse(any(n == bad_name or n.endswith("." + bad_name)
                                 or n.startswith(bad_name) for n in names), (bad_name, names))

    def test_without_the_brain_there_is_no_draft(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            w = default_registry().require("posting.instagram")
            hands = FakeHands(PARKED)
            result = w.run(WorkContext(now=T0, http=None, secrets_dir=Path(d), words=None,
                                       job=hands.job, state_dir=Path(d)))
            self.assertIsInstance(result, Err)
            self.assertEqual(result.error.kind, ErrorKind.NOT_CONFIGURED)
            self.assertEqual(hands.jobs, [])


class FakeClient:
    """Pionir behind the real hands: parks social.instagram_post; lists its approvals."""

    def __init__(self) -> None:
        self.calls: list = []
        self.queue: dict = {"pending": [], "recent": []}

    def run_task(self, capability, payload, *, permissions=(), wait=30.0):
        self.calls.append((capability, dict(payload), tuple(permissions)))
        self.queue["pending"].append({"id": "ap-9", "status": "pending",
                                      "capability": capability})
        return {"ok": False, "status": "pending_approval", "approval_id": "ap-9",
                "summary": "instagram . social.instagram_post", "task_id": "t-9",
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
        self.crew.dispatcher.dispatch(only=("posting.instagram",), wait=True)
        self.assertEqual(len(self.post.calls), 1)
        _url, body = self.post.calls[0]
        self.assertEqual((body["format"], body["options"]["temperature"]), (DRAFT_SCHEMA, 0))
        self.assertEqual(self.crew.store.calls_last_hour("posting"), 1)   # charged to posting
        (call,) = self.client.calls
        self.assertEqual((call[0], call[2]), ("social.instagram_post", ()))
        self.assertEqual(call[1]["card_sha"], card_sha(call[1]["headline"], call[1]["points"]))

    def test_the_posting_leader_reads_blocked_reasons_and_the_rule(self) -> None:
        self.post.reply = json.dumps(bad())
        self.crew.dispatcher.dispatch(only=("posting.instagram",), wait=True)
        ask = FakeAsk(leader_reply(summary="Two Instagram drafts were blocked.", figures=[]))
        lead = Leader("posting", self.crew.registry, self.crew.store, ask=ask, model="m",
                      clock=time.time)
        lead.run()
        system, user = (m["content"] for m in ask.calls[0]["messages"])
        self.assertIn("PENDING APPROVAL has NOT been published", system)
        self.assertIn("posting.instagram", system)
        self.assertIn("post.blocked", user)
        self.assertIn("posting.instagram", user)
        self.assertIn("2 drafts blocked", user)


if __name__ == "__main__":
    unittest.main()
