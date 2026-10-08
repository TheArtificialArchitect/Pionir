"""Where the posting workers' topics come from (topics.py): what we sell, by measured demand.

The failure this exists for, measured 2026-10-07: the blog and Instagram drew from 14 static
seeds; Instagram had used 12 and would have stopped within two days, its only response an
alert. Each test fails if its rule is reverted: a worker running dry, a topic that points at
nothing we sell, demand ignored, the seeds anything but a fallback, a worker's ledger shared.
"""

import json
import tempfile
import time
import unittest
from pathlib import Path

from test_crew_blog import FakeBrain, FakeHands
from test_crew_instagram import good as ig_good

from pionir.crew import contentcheck, demand, topics
from pionir.crew.blog import SEEDS, BlogWorker, offer_for
from pionir.crew.registry import default_registry
from pionir.crew.worker import WorkContext

T0 = 1_790_000_000.0
DAY = 86400.0


def demand_doc(now=T0, *, product=None, guides=None) -> dict:
    """A fresh demand.json: ``product`` {pid: score}, ``guides`` {path: views}."""
    ranking = [{"id": f"api:{pid}", "kind": "api", "ref": pid, "product": pid, "score": s,
                "inputs": {}} for pid, s in (product or {}).items()]
    g = {path: {"product": pid, "views": {"value": (guides or {}).get(path)},
                "tool_clicks": {"value": None}} for path, pid in demand.GUIDES.items()}
    return {"computed_at": now, "ranking": ranking, "guides": g}


class InventoryTests(unittest.TestCase):
    def test_every_paid_guide_is_a_topic_source_and_nothing_else_is(self) -> None:
        self.assertEqual({g.path for g in topics.GUIDES}, set(demand.GUIDES))
        keys = [t.key for t in topics.all_topics()]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertGreater(len(keys), 140)

    def test_every_topic_points_at_something_we_sell_and_its_footer_passes(self) -> None:
        for t in topics.all_topics():
            footer = BlogWorker.links(t, "2026-10-07-x")
            self.assertIn(f"https://api.dokaz.net{t.path}?utm_source=blog", footer)
            offer = offer_for(t).url
            self.assertTrue(offer.startswith(("https://dokaz.gumroad.com/l/",
                                              "https://api.dokaz.net/hire")), t.key)
            self.assertEqual(contentcheck.unknown_names([("body_md", footer)]), [], t.key)
        # the invoice guide sells Obol, whatever the angle
        inv = topics.make_topic(topics.GUIDE_BY_PATH["/docs/invoice-pdf-api"],
                                topics.ANGLES[2])
        self.assertIn("obol-pro", offer_for(inv).url)

    def test_every_product_named_to_the_model_is_a_name_a_post_may_use(self) -> None:
        for g in topics.GUIDES:
            sentence = f"Try the {g.product} for this."
            self.assertEqual(contentcheck.unknown_names([("body_md", sentence)]), [],
                             g.product)


class ChoiceTests(unittest.TestCase):
    def test_without_demand_the_seeds_come_first_then_the_generated_topics(self) -> None:
        rec = {"used_topics": []}
        self.assertEqual(topics.choose(rec, None, None, T0).key, SEEDS[0].key)
        rec = {"used_topics": [t.key for t in SEEDS]}
        nxt = topics.choose(rec, None, None, T0)
        self.assertNotIn(nxt.key, {t.key for t in SEEDS})
        self.assertEqual(nxt.path, topics.GUIDES[0].path)

    def test_measured_demand_picks_the_guide_and_spreads_the_posts(self) -> None:
        doc = demand_doc(product={"barcode": 80, "email": 60},
                         guides={"/docs/ean-13-upc-a-barcode-api": 9})
        rec = {"used_topics": []}
        first = topics.choose(rec, None, doc, T0)
        self.assertEqual(first.path, "/docs/ean-13-upc-a-barcode-api")     # 80 + 9 views
        rec["used_topics"].append(first.key)
        # the same guide again is worth (80 + 9) / 2 now; the other barcode guide 80
        second = topics.choose(rec, None, doc, T0)
        self.assertEqual(second.path, "/docs/barcode-generator-api")
        rec["used_topics"].append(second.key)
        third = topics.choose(rec, None, doc, T0)
        self.assertEqual(GUIDE_PRODUCT(third), "email")                # 60 beats 44.5, 40

    def test_unknown_demand_ranks_after_measured_and_is_never_zero(self) -> None:
        doc = demand_doc(product={"text": 1})
        ranked = topics.rank(topics.free_topics({"used_topics": []}), {}, doc)
        self.assertEqual(GUIDE_PRODUCT(ranked[0]), "text")
        self.assertNotIn("/docs/qr-code-api", topics.guide_scores(doc))

    def test_the_goal_still_wins(self) -> None:
        doc = demand_doc(product={"barcode": 80})
        t = topics.choose({"used_topics": []}, "posts about removing exif data from photos",
                          doc, T0)
        self.assertEqual(t.path, "/docs/remove-exif-metadata-api")


def GUIDE_PRODUCT(topic) -> str:
    return demand.GUIDES[topic.path]


class NeverRunsDryTests(unittest.TestCase):
    def test_instagram_with_every_seed_used_still_has_a_topic(self) -> None:
        # the live record: 12 of 14 seeds used; then all 14
        rec = {"used_topics": [t.key for t in SEEDS]}
        self.assertIsNotNone(topics.choose(rec, None, None, T0))
        self.assertGreater(topics.topics_left(rec), 100)

    def test_when_every_topic_is_used_the_oldest_comes_back_only_after_reuse_after(self) -> None:
        every = topics.all_topics()
        rec = {"used_topics": [t.key for t in every],
               "topic_used_at": {t.key: T0 + i for i, t in enumerate(every)}}
        self.assertIsNone(topics.choose(rec, None, None, T0 + DAY))
        back = topics.choose(rec, None, None, T0 + topics.REUSE_AFTER)
        self.assertEqual(back.key, every[0].key)


class WorkerTests(unittest.TestCase):
    """The real workers, a real demand.json on disk, fakes at the brain and at Pionir."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)

    def run_worker(self, wid, brain, now=T0):
        w = default_registry().require(wid)
        hands = FakeHands()
        w.run(WorkContext(now=now, http=None, secrets_dir=self.state, words=brain,
                          job=hands.job, approval=hands.approval, state_dir=self.state))
        return w, hands

    def test_instagram_is_steered_by_the_same_demand_and_keeps_its_own_ledger(self) -> None:
        (self.state / demand.DEMAND_FILE).write_text(
            json.dumps(demand_doc(product={"image": 50})), encoding="utf-8")
        blog_brain = FakeBrain()
        self.run_worker("posting.blog", blog_brain)
        self.assertIn("removing location and camera data from photos",
                      blog_brain.calls[0]["user"])
        ig_brain = FakeBrain(ig_good())
        ig, hands = self.run_worker("posting.instagram", ig_brain)
        # the blog used that topic; Instagram's own ledger still has it
        self.assertIn("removing location and camera data from photos", ig_brain.calls[0]["user"])
        self.assertEqual(len(hands.jobs), 1)
        rec = json.loads(ig.record_path(self.state).read_text(encoding="utf-8"))
        self.assertEqual(rec["used_topics"], ["remove-exif-metadata-api.how-to"])

    def test_a_stale_demand_file_steers_nothing(self) -> None:
        (self.state / demand.DEMAND_FILE).write_text(
            json.dumps(demand_doc(T0 - demand.STALE_AFTER - 1, product={"image": 50})),
            encoding="utf-8")
        brain = FakeBrain()
        self.run_worker("posting.blog", brain)
        self.assertTrue(brain.calls[0]["user"].startswith(f"Topic: {SEEDS[0].subject}."))

    def test_the_live_instagram_record_would_draft_for_months(self) -> None:
        live = {"used_topics": [t.key for t in SEEDS[:12]]}
        days, rec, now = 0, dict(live), time.time()
        while topics.choose(rec, None, None, now) is not None and days < 400:
            rec["used_topics"].append(topics.choose(rec, None, None, now).key)
            days += 1
        self.assertGreater(days, 120)


if __name__ == "__main__":
    unittest.main()
