"""The demand loop: what to build and sell next, from measured demand only.

The dash is a fake (``FakeHttp`` answering ``/dash/api.json`` with a document shaped exactly
like Scrooge's: ``summary``, ``usage`` from ``usageLast(env, 14)``, ``keys``, ``crons``,
``outreach``, ``paid_calls``, ``traffic`` from ``trafficReport()`` and ``fiverr``); the blog
and Builds records are files in a temporary state dir; Pionir is a fake that records each
card. Each test fails if the rule it names is reverted: a score from the wrong rows or
without its inputs, an unmeasured input counted as zero, a backlog file written, a card
posted twice in a day or the same suggestion again within 14 days, the Builds pick or the
blog's topic ignoring demand - or demand overruling Moss's goal.
"""

import json
import tempfile
import unittest
from pathlib import Path

from test_crew_fakes import FakeHttp

from pionir.crew import demand
from pionir.crew.blog import SEEDS, save_record
from pionir.crew.builds import backlog
from pionir.crew.contentcheck import utm_campaign
from pionir.crew.hands import JobOutcome
from pionir.crew.registry import default_registry, load_catalogue
from pionir.crew.result import Err, Ok
from pionir.crew.worker import ErrorKind, WorkContext

URL = "https://api.dokaz.net/dash/api.json"
T0 = 1_791_000_000.0
DAY = 86400.0
POST = ("2026-09-29-email-address-validation-syntax-mx-typos",
        "email-address-validation-syntax-mx-typos")
CAMPAIGN = utm_campaign(POST[0])           # what the blog tags the post's links with (40 max)

# usageLast(env, 14): one row per UTC day and product, internal keys excluded.
USAGE = [
    {"day": "2026-09-25", "product": "convert", "calls": 450, "errors": 0, "callers": 3},
    {"day": "2026-09-26", "product": "convert", "calls": 260, "errors": 1, "callers": 2},
    {"day": "2026-09-26", "product": "qr", "calls": 30, "errors": 0, "callers": 5},
    {"day": "2026-09-27", "product": "convert", "calls": 40, "errors": 0, "callers": 4},
    {"day": "2026-09-27", "product": "email", "calls": 12, "errors": 2, "callers": 3},
]
# trafficReport(): the last 30 days; every list under its 20-row cap (complete).
TRAFFIC = {
    "since": "2026-09-05",
    "views": 383,
    "by_day": [{"day": "2026-09-26", "n": 190}, {"day": "2026-09-27", "n": 193}],
    "top_paths": [
        {"path": "/", "n": 120}, {"path": "/docs/json-to-excel-api", "n": 44},
        {"path": "/docs/qr-code-api", "n": 35}, {"path": "/blog", "n": 33},
        {"path": "/docs/email-verification-api", "n": 18}, {"path": f"/blog/{POST[1]}", "n": 8},
        {"path": "/docs", "n": 6}],
    "top_referrers": [
        {"ref_host": "direct", "n": 200}, {"ref_host": "dokazindustries.com", "n": 60},
        {"ref_host": "www.google.com", "n": 35}, {"ref_host": "www.facebook.com", "n": 6}],
    "top_campaigns": [
        {"campaign": "none", "n": 290}, {"campaign": "instagram/bio/", "n": 23},
        {"campaign": "dokazindustries/referral/free-tools", "n": 9},
        {"campaign": "dokazindustries/referral/products", "n": 7},
        {"campaign": f"blog/referral/{CAMPAIGN}", "n": 2}],
    "sales_by_campaign": [{"campaign": "none", "sales": 1, "net": 1900}],
    "sales_by_referrer": [{"ref_host": "direct", "sales": 1, "net": 1900}],
}
DASH = {
    "summary": {"all_time_net": 1900, "mtd_net": 0, "last30_net": 1900, "last30_gross": 1900,
                "by_stream_30": [{"stream": "gumroad", "net": 1900, "sales": 1}],
                "by_stream_all": [{"stream": "gumroad", "net": 1900, "sales": 1}],
                "recent": [], "run_rate_month": 1900, "target_month": 400000},
    "usage": USAGE,
    "keys": [{"plan": "starter", "n": 1}],
    "crons": [{"k": "cron:daily", "v": "ok", "updated_at": "2026-09-27T00:00:00.000Z"}],
    "outreach": {"sent": 0},
    "paid_calls": {"used": 3, "cap": 1000, "remaining": 997},
    "traffic": TRAFFIC,
    "fiverr": {"day": "2026-09-27", "today": {"by_kind": {}, "events": 0, "unparsed": 0,
                                              "dropped": 0}, "pending": 0},
}
BLOG = {"posts": [{"draft_id": POST[0], "slug": POST[1], "title": "A title",
                   "topic": "verify-email-before-sending", "status": "published",
                   "submitted_at": 1.0, "settled_at": 2.0, "approval_id": "ap",
                   "url": f"https://api.dokaz.net/blog/{POST[1]}"}],
        "counts": {}, "used_slugs": [POST[1]], "used_topics": ["verify-email-before-sending"],
        "blocked": [], "last_drafted_at": None}


def with_traffic(**over) -> dict:
    return {**DASH, "traffic": {**TRAFFIC, **over}}


class FakePionir:
    """``ctx.job``: each demand card recorded, or refused/unavailable as scripted."""

    def __init__(self) -> None:
        self.cards: list = []
        self.outcome = None

    def job(self, job):
        assert job.capability == demand.CARD, job.capability
        if self.outcome is not None:
            return self.outcome
        self.cards.append(dict(job.payload))
        return JobOutcome("done", demand.CARD, task_id="t-card",
                          result={"ok": True, "message_id": "m1"})


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.root = root
        self.secrets = root / "secrets"
        self.secrets.mkdir()
        (self.secrets / "scrooge-read-token.txt").write_text("read-token", encoding="utf-8")
        self.state = root / "workers"
        self.builds = root / "builds"
        self.apibuilds = root / "apibuilds"
        self.shelf = root / "products"
        self.worker = default_registry().require("products.demand")
        self.pionir = FakePionir()

    def run_at(self, now=T0, doc=DASH):
        self.http = FakeHttp({URL: (200, doc)})
        return self.worker.run(WorkContext(
            now=now, http=self.http, secrets_dir=self.secrets, job=self.pionir.job,
            state_dir=self.state, builds_dir=self.builds, apibuilds_dir=self.apibuilds,
            products_dir=self.shelf))

    def ok(self, now=T0, doc=DASH):
        result = self.run_at(now, doc)
        self.assertIsInstance(result, Ok, result)
        return result

    def ranking(self) -> dict:
        doc = json.loads((self.state / demand.DEMAND_FILE).read_text(encoding="utf-8"))
        return {c["id"]: c for c in doc["ranking"]}, doc

    @staticmethod
    def row(result, kind):
        return [o for o in result.value if o.kind == kind]


class ScoreTests(_Case):
    def test_each_score_is_its_measured_inputs_and_shows_them(self) -> None:
        save_record(self.state / "posting.blog.json", BLOG)
        self.ok()
        by_id, doc = self.ranking()
        conv = by_id["api:convert"]
        # 750 calls over 9 caller-days; 450 >= 3*100 and 260 >= 2*100: 2 saturated days;
        # its guides: json-to-excel 44 (csv-to-json, json-to-csv not listed in a complete list)
        self.assertEqual({k: v["value"] for k, v in conv["inputs"].items()},
                         {"api_calls": 750, "api_caller_days": 9, "api_saturated_days": 2,
                          "guide_views": 44})
        self.assertEqual(conv["score"], 75 + 27 + 20 + 44)
        self.assertFalse(conv["partial"])
        email = by_id["api:email"]
        self.assertEqual(email["score"], 1 + 9 + 0 + 18)
        topic = by_id["topic:verify-email-before-sending"]
        self.assertEqual({k: v["value"] for k, v in topic["inputs"].items()},
                         {"guide_views": 18, "post_views": 8, "post_clicks": 2})
        self.assertEqual(topic["score"], 18 + 8 + 10)
        self.assertEqual(topic["unavailable"], ["search_referrals_per_post"])
        tool = by_id["tool:qr-code-generator"]
        self.assertEqual(tool["inputs"]["guide_views"]["value"], 35)
        self.assertEqual(tool["unavailable"], ["tool_click_through_per_tool"])
        self.assertEqual(doc["site"]["free_tool_click_throughs_30d"], {"value": 9})
        self.assertIn("tool_click_through_per_tool", doc["unavailable"])
        self.assertIn("rejected_calls_429", doc["unavailable"])

    def test_a_backlog_product_takes_its_closest_products_demand(self) -> None:
        self.ok()
        by_id, doc = self.ranking()
        x = by_id["build:json-to-xlsx"]
        self.assertEqual((x["product"], x["via"], x["score"]),
                         ("convert", "api:convert", by_id["api:convert"]["score"]))
        self.assertEqual(by_id["build:email-list-clean"]["product"], "email")
        self.assertEqual(by_id["build:csv-to-ics"]["product"], "calendar")
        self.assertEqual(by_id["build:exif-strip"]["product"], "image")
        # tied with csv-to-sqlite on the score; the closer match ranks first
        ids = [c["id"] for c in doc["ranking"]]
        self.assertLess(ids.index("build:json-to-xlsx"), ids.index("build:csv-to-sqlite"))
        self.assertEqual(ids[0], "build:json-to-xlsx")

    def test_nothing_measured_is_unknown_never_zero(self) -> None:
        self.ok()
        by_id, doc = self.ranking()
        cron = by_id["build:cron-explain"]
        self.assertIsNone(cron["score"])
        self.assertIn("no measured signal", cron["why_unscored"])
        self.assertEqual(doc["ranking"][-1]["score"], None)
        # a product with no usage row in a reported usage list is a measured zero
        self.assertEqual(by_id["api:barcode"]["inputs"]["api_calls"], {"value": 0})

    def test_a_guide_missing_from_a_capped_list_is_unknown(self) -> None:
        full = [{"path": f"/p{i}", "n": 100 - i} for i in range(19)] + [
            {"path": "/docs/json-to-excel-api", "n": 44}]
        self.ok(doc=with_traffic(top_paths=full))
        by_id, _doc = self.ranking()
        qr = by_id["tool:qr-code-generator"]["inputs"]["guide_views"]
        self.assertIsNone(qr["value"])
        self.assertIn("capped", qr["why"])
        conv = by_id["api:convert"]
        self.assertEqual(conv["inputs"]["guide_views"]["value"], 44)
        self.assertTrue(conv["inputs"]["guide_views"]["at_least"])
        self.assertTrue(conv["partial"])

    def test_a_dash_without_usage_leaves_the_api_inputs_unknown(self) -> None:
        doc = {k: v for k, v in DASH.items() if k != "usage"}
        self.ok(doc=doc)
        by_id, _doc = self.ranking()
        conv = by_id["api:convert"]
        self.assertIsNone(conv["inputs"]["api_calls"]["value"])
        self.assertEqual(conv["score"], 44)            # the guide views alone, partial
        self.assertTrue(conv["partial"])

    def test_a_dash_with_no_signal_at_all_is_an_error_and_keeps_the_last_ranking(self) -> None:
        self.ok()
        before = (self.state / demand.DEMAND_FILE).read_text(encoding="utf-8")
        doc = {k: v for k, v in DASH.items() if k not in ("usage", "traffic")}
        result = self.run_at(T0 + DAY, doc)
        self.assertIsInstance(result, Err)
        self.assertEqual(result.error.kind, ErrorKind.NOT_CONFIGURED)
        self.assertIn("UNKNOWN", result.error.message)
        self.assertEqual((self.state / demand.DEMAND_FILE).read_text(encoding="utf-8"), before)

    def test_no_token_is_not_configured(self) -> None:
        (self.secrets / "scrooge-read-token.txt").unlink()
        result = self.run_at()
        self.assertIsInstance(result, Err)
        self.assertEqual(result.error.kind, ErrorKind.NOT_CONFIGURED)
        self.assertFalse((self.state / demand.DEMAND_FILE).exists())

    def test_a_malformed_usage_row_is_malformed(self) -> None:
        bad = {**DASH, "usage": USAGE + [{"day": "2026-09-27", "product": "qr", "calls": "x",
                                          "errors": 0, "callers": 1}]}
        result = self.run_at(doc=bad)
        self.assertIsInstance(result, Err)
        self.assertEqual(result.error.kind, ErrorKind.MALFORMED)

    def test_the_digest_row_names_the_top_three_with_their_figures(self) -> None:
        result = self.ok()
        (row,) = self.row(result, "demand.ranking")
        self.assertEqual(row.payload["top"], ["build:json-to-xlsx", "build:csv-to-sqlite",
                                              "tool:json-to-excel"])
        figs = {(f.measures, f.stream): f.value for f in row.figures}
        self.assertEqual(figs[("demand score", "build:json-to-xlsx")], 166)
        self.assertEqual(figs[("API calls", "build:json-to-xlsx")], 750)
        self.assertEqual(figs[("API guide page views", "tool:json-to-excel")], 44)
        self.assertEqual(figs[("free tool click-throughs to the API, all tools",
                               "free-tools")], 9)
        self.assertIn("build:json-to-xlsx (demand score 166", row.payload["summary"])
        self.assertIn("build:cron-explain", row.payload["unknown"])
        self.assertIn("tool_click_through_per_tool", row.payload["unavailable"])
        self.assertFalse(row.derived)

    def test_a_leader_can_quote_the_top_three_and_nothing_else(self) -> None:
        from pionir.crew.grounding import unbacked_claims
        result = self.ok()
        (row,) = self.row(result, "demand.ranking")
        said = ("Top demand: json-to-xlsx with a demand score of 166, from 750 API calls, "
                "9 API caller-days and 44 API guide page views; 9 free tool click-throughs.")
        self.assertEqual(unbacked_claims(said, row.figures), [])
        self.assertNotEqual(unbacked_claims("json-to-xlsx drew 990 API calls.", row.figures),
                            [])


class BacklogFilesTests(_Case):
    def test_it_never_writes_a_backlog(self) -> None:
        self.ok()
        self.assertFalse(backlog.backlog_path(self.builds).exists())   # never seeded here
        self.assertFalse((self.apibuilds / "backlog.json").exists())
        self.builds.mkdir()
        raw = {"products": [dict(e) for e in backlog.SEED]}
        text = json.dumps(raw, indent=2) + "\n"
        backlog.backlog_path(self.builds).write_text(text, encoding="utf-8")
        self.ok(T0 + 2 * DAY)
        self.assertEqual(backlog.backlog_path(self.builds).read_text(encoding="utf-8"), text)

    def test_a_product_already_taken_by_the_builds_is_not_ranked(self) -> None:
        save_record(self.state / "builds.daedalus.json",
                    {"products": {"json-to-xlsx": {"state": "staged"}}})
        self.ok()
        by_id, _doc = self.ranking()
        self.assertNotIn("build:json-to-xlsx", by_id)


class CardTests(_Case):
    def test_saturation_posts_one_suggestion_card(self) -> None:
        result = self.ok()
        (card,) = self.pionir.cards
        self.assertEqual(card["kind"], "demand")
        self.assertFalse(card["replies"])
        self.assertTrue(card["body"].startswith("demand seen: on 2 of the last 14 days"))
        self.assertIn("suggest a paid offline Convert tool", card["body"])
        self.assertIn("nothing was added to any backlog", card["body"])
        (row,) = self.row(result, "demand.card")
        self.assertEqual(row.payload["suggestion"], "saturated-convert")
        self.assertIn(2, [f.value for f in row.figures])

    def test_at_most_one_card_a_day_and_no_repeat_within_14_days(self) -> None:
        self.ok()
        self.ok(T0 + 3600)
        self.assertEqual(len(self.pionir.cards), 1)                   # same day: none
        self.ok(T0 + DAY + 60)
        self.assertEqual(len(self.pionir.cards), 2)
        # the convert suggestion is not repeated; the next one is the QR tool, whose guide
        # drew 35 views and no backlog product or shelf product covers QR codes
        self.assertTrue(self.pionir.cards[1]["key"].startswith("demand:no-paid-qr-code-"))
        self.assertIn("UNKNOWN", self.pionir.cards[1]["body"])
        self.ok(T0 + 2 * DAY + 120)
        self.assertEqual(len(self.pionir.cards), 2)                   # nothing new to say
        self.ok(T0 + 15 * DAY)
        self.assertEqual(len(self.pionir.cards), 3)
        self.assertTrue(self.pionir.cards[2]["key"].startswith("demand:saturated-convert:"))

    def test_a_tool_with_a_paid_counterpart_is_not_suggested(self) -> None:
        sugg = demand.DemandWorker.suggestions
        self.ok()
        _by_id, doc = self.ranking()
        ids = [s[0] for s in sugg(doc["ranking"], {"convert"})]
        self.assertNotIn("no-paid-json-to-excel", ids)          # json-to-xlsx covers it
        self.assertIn("no-paid-qr-code-generator", ids)
        self.assertNotIn("no-paid-qr-code-generator", [s[0] for s in sugg(doc["ranking"],
                                                                          {"qr"})])

    def test_a_refused_card_is_not_counted_and_is_offered_again(self) -> None:
        self.pionir.outcome = JobOutcome("failed", demand.CARD, error="kind: one of ...",
                                         error_type="AdapterProtocolError")
        result = self.ok()
        (row,) = self.row(result, "demand.card_unposted")
        self.assertIn("kind", row.payload["why"])
        self.pionir.outcome = None
        self.ok(T0 + 3600)
        self.assertEqual(len(self.pionir.cards), 1)


class HookTests(_Case):
    def test_the_builds_pick_prefers_demand_but_the_goal_wins(self) -> None:
        self.ok()
        prefer = demand.build_preference(self.state, T0 + 60)
        self.assertEqual(prefer[:2], ["json-to-xlsx", "csv-to-sqlite"])
        self.assertNotIn("cron-explain", prefer)                 # no score: never preferred
        self.assertNotIn("barcode-svg", prefer)                  # a measured zero: neither
        entries = [dict(e) for e in backlog.SEED]
        self.assertEqual(backlog.choose(entries, set(), None, prefer)["slug"], "json-to-xlsx")
        self.assertEqual(backlog.choose(entries, set(), "Build cron-explain next",
                                        prefer)["slug"], "cron-explain")
        self.assertEqual(backlog.choose(entries, {"json-to-xlsx"}, None, prefer)["slug"],
                         "csv-to-sqlite")
        self.assertEqual(backlog.choose(entries, set(), None)["slug"], "exif-strip")

    def test_a_stale_or_unreadable_ranking_steers_nothing(self) -> None:
        self.ok()
        self.assertEqual(demand.build_preference(self.state, T0 + demand.STALE_AFTER + 1), [])
        (self.state / demand.DEMAND_FILE).write_text("{not json", encoding="utf-8")
        with self.assertLogs("pionir.crew", "WARNING"):
            self.assertEqual(demand.product_preference(self.state, T0), [])

    def test_the_blog_writes_about_the_highest_demand_product_unless_the_goal_says(self) -> None:
        from test_crew_blog import FakeBrain, FakeHands
        self.ok()
        # convert 166; qr 53 (3 + 15 + 35 guide views); email 28
        self.assertEqual(demand.product_preference(self.state, T0 + 60)[:3],
                         ["convert", "qr", "email"])
        blog = default_registry().require("posting.blog")

        def draft(goal=None, state=None):
            brain = FakeBrain()
            hands = FakeHands()
            blog.run(WorkContext(now=T0 + 60, http=None, secrets_dir=self.secrets,
                                 words=brain, job=hands.job, approval=hands.approval,
                                 goal=goal, state_dir=state or self.state))
            return brain.calls[0]["user"]

        # no seed is about Convert; the first about QR codes is SEEDS[3], not SEEDS[0]
        self.assertEqual(SEEDS[3].key, "qr-codes-from-an-api")
        self.assertTrue(draft().startswith(f"Topic: {SEEDS[3].subject}."))
        other = self.root / "other"
        other.mkdir()
        (other / demand.DEMAND_FILE).write_text(
            (self.state / demand.DEMAND_FILE).read_text(encoding="utf-8"), encoding="utf-8")
        topic = next(t for t in SEEDS if t.key == "sentiment-analysis-api")
        self.assertTrue(draft(goal="posts on the sentiment of reviews",
                              state=other).startswith(f"Topic: {topic.subject}."))
        empty = self.root / "empty"
        empty.mkdir()
        self.assertTrue(draft(state=empty).startswith(f"Topic: {SEEDS[0].subject}."))

    def test_prefer_topic_follows_the_products_in_order(self) -> None:
        free = list(SEEDS)
        self.assertEqual(demand.prefer_topic(free, ["convert", "qr"]).key,
                         "qr-codes-from-an-api")
        self.assertIsNone(demand.prefer_topic(free, ["convert"]))


class WiringTests(unittest.TestCase):
    def test_it_is_a_products_worker_in_the_digest_like_the_shelf(self) -> None:
        reg = default_registry()
        w = reg.require("products.demand")
        self.assertEqual((w.division, w.provider, w.cadence_seconds),
                         ("products", "dokaz", 86400))
        # it reads what the other workers recorded, so it runs after them in a dispatch -
        # and never holds a dokaz slot ahead of the site's health check
        self.assertEqual(w.stage, 1)
        (div,) = [d for d in load_catalogue()["divisions"] if d["id"] == "products"]
        for kind in ("demand.ranking", "demand.card", "demand.card_unposted"):
            self.assertIn(kind, div["brief_quota"])
        self.assertIn("products.demand", div["leader_notes"])
        (entry,) = [x for x in div["workers"] if x["name"] == "demand"]
        self.assertEqual(entry["uses"], ["builds.card"])


if __name__ == "__main__":
    unittest.main()
