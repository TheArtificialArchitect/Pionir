"""Marketplaces: the scouts, the packager and the watcher, with Pionir and the stores faked.

Each test fails if its rule is reverted: a store that could not be read reported as an empty
market, a personal-data niche specified, a failed brain call counted as a try, two specs a run,
a second marketplace product queued while one waits, a publish submitted without its
credential (or more than one a run), anything called published before Pionir says so, a
Chrome item the owner never created targeted, two extensions for one purpose, a draft whose
manifest asks for more than its spec, or a rating card posted twice.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from marketplace_support import (
    APIFY_WORDS,
    CHROME_WORDS,
    SHOPIFY_WORDS,
    T0,
    FakeHttp,
    apify_spec,
    chrome_build,
    chrome_spec,
    core_build,
    isolate,
    shopify_spec,
)

from pionir.adapters.deliveries import SecretValues
from pionir.crew.builds import backlog as bl
from pionir.crew.hands import JobOutcome
from pionir.crew.marketplaces import paths, providers
from pionir.crew.marketplaces.staging import stage_build
from pionir.crew.registry import default_registry
from pionir.crew.result import Err, Ok
from pionir.crew.worker import ErrorKind, WorkContext, WorkerError

STORE = f"{providers.APIFY_STORE}?limit=500&offset=0&sortBy=popularity"


def store_items() -> list:
    def item(name, title, users, rating, count, desc="", cats=("SEO_TOOLS",)):
        return {"username": f"dev{name}", "name": name, "title": title, "description": desc,
                "categories": list(cats), "stats": {"totalUsers30Days": users},
                "actorReviewRating": rating, "actorReviewCount": count}
    return [item("a", "Sitemap Audit Tool", 3000, 3.1, 9),
            item("b", "Sitemap Audit Checker", 1500, 2.8, 4),
            item("c", "Sitemap Audit Lite", 700, None, 0),
            item("f", "Markdown Table Converter", 2500, 3.0, 6, cats=("DEVELOPER_TOOLS",)),
            item("g", "Markdown Table Formatter", 2000, 2.6, 5, cats=("DEVELOPER_TOOLS",)),
            item("d", "LinkedIn Profile Scraper", 90000, 3.0, 50, "emails and phones",
                 ("LEAD_GENERATION",)),
            item("e", "LinkedIn Profile Finder", 50000, 2.0, 40, "", ("LEAD_GENERATION",))]


class Pionir:
    """``ctx.job`` and ``ctx.approval``: publishes parked, cards recorded, reads answered."""

    def __init__(self) -> None:
        self.jobs: list = []
        self.cards: dict = {}
        self.approvals: dict = {}
        self.stats: dict = {"ok": True, "actors": []}

    def job(self, job):
        self.jobs.append(job)
        if job.capability == "builds.card":
            self.cards[job.payload["key"]] = job.payload
            return JobOutcome("done", job.capability, task_id="t-card", result={"ok": True})
        if job.capability in ("apify.publish", "chrome.publish_update"):
            n = len([j for j in self.jobs if j.capability == job.capability])
            return JobOutcome("pending_approval", job.capability, task_id=f"t-{n}",
                              approval_id=f"ap-{job.capability}-{n}")
        if job.capability == "apify.stats":
            return JobOutcome("done", job.capability, task_id="t-s", result=self.stats)
        if job.capability == "chrome.status":
            return JobOutcome("done", job.capability, task_id="t-c",
                              result={"ok": True, "items": []})
        raise AssertionError(job.capability)

    def approval(self, approval_id):
        return dict(self.approvals.get(approval_id, {"status": "pending"}))

    def of(self, capability) -> list:
        return [j for j in self.jobs if j.capability == capability]


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        isolate(self)
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.builds = self.dir / "builds"
        self.state = self.dir / "state"
        self.secrets = self.dir / "secrets"
        self.secrets.mkdir()
        self.root = self.dir / "marketplaces"
        self.pionir = Pionir()
        self.http = FakeHttp({STORE: (200, {"data": {"items": store_items()}})})
        self.words_asked: list = []
        self.answers: list = []
        self.now = T0

    def words(self, purpose, system, user, schema):
        self.words_asked.append(user)
        if not self.answers:
            return Err(WorkerError("w", ErrorKind.NO_WORDS, "the brain is busy"))
        return Ok(self.answers.pop(0))

    def ctx(self, **over) -> WorkContext:
        base = dict(now=self.now, http=self.http, secrets_dir=self.secrets,
                    words=self.words, job=self.pionir.job, approval=self.pionir.approval,
                    state_dir=self.state, builds_dir=self.builds)
        base.update(over)
        return WorkContext(**base)

    def step(self, worker_id, **over):
        worker = default_registry().require(worker_id)
        worker.sleep = lambda s: None
        return worker.run(self.ctx(**over))

    def candidates(self, market="apify") -> dict:
        return json.loads(paths.candidates_path(self.root, market).read_text("utf-8"))


class ScoutTests(_Case):
    def test_a_gap_is_specified_and_queued_for_the_night_builds(self) -> None:
        self.answers = [dict(APIFY_WORDS)]
        got = self.step("marketplaces.scout_apify")
        self.assertIsInstance(got, Ok, got)
        kinds = [o.kind for o in got.value]
        self.assertIn("market.spec_queued", kinds)
        written = next(o for o in got.value if o.kind == "market.spec_written")
        self.assertTrue(written.derived)                  # words came from the brain
        doc = bl.load(self.builds)
        entry = doc["products"][-1]
        self.assertEqual((entry["slug"], entry["product_type"]), ("sitemap-audit",
                                                                   "apify_actor"))
        spec = json.loads(paths.spec_path(self.root, "sitemap-audit").read_text("utf-8"))
        measured = self.candidates()["candidates"][f"apify:{spec['evidence']['niche']}"]
        self.assertEqual(spec["evidence"]["demand"], measured["demand"])   # the scout's, measured
        self.assertGreater(measured["demand"], 0)
        self.assertEqual(self.http.calls, [STORE])        # one page was enough

    def test_personal_data_niches_are_excluded_and_never_asked_about(self) -> None:
        self.answers = [dict(APIFY_WORDS)]
        self.step("marketplaces.scout_apify")
        cands = self.candidates()["candidates"]
        linked = [c for k, c in cands.items() if "linkedin" in k or "profile" in k]
        self.assertTrue(linked)
        self.assertTrue(all(c["status"] == "excluded" for c in linked))
        self.assertTrue(all("linkedin" not in u.lower() and "profile" not in u.lower()
                            for u in self.words_asked))

    def test_no_words_is_not_a_try_and_nothing_is_specified(self) -> None:
        got = self.step("marketplaces.scout_apify")
        self.assertIsInstance(got, Ok)
        self.assertTrue(all(c.get("tries", 0) == 0 for c in
                            self.candidates()["candidates"].values()))
        self.assertFalse((self.builds / "backlog.json").exists())

    def test_one_spec_a_run_and_one_marketplace_product_waits_at_a_time(self) -> None:
        second = {**APIFY_WORDS, "slug": "markdown-tables",
                  "name": "Markdown Tables: tidy tables in Markdown text"}
        self.answers = [dict(APIFY_WORDS), dict(second)]
        self.step("marketplaces.scout_apify")
        self.assertEqual(len(self.words_asked), 1)          # one spec a run
        self.now += 86400
        got = self.step("marketplaces.scout_apify")
        self.assertEqual(len(self.words_asked), 2)
        waiting = [o for o in got.value if o.kind == "market.spec_waiting"]
        self.assertEqual(len(waiting), 1)
        self.assertIn("already waiting", waiting[0].payload["why"])
        queued = [e["slug"] for e in bl.load(self.builds)["products"]
                  if bl.product_type(e) != bl.GUMROAD]
        self.assertEqual(len(queued), 1)

    def test_an_infeasible_gap_is_rejected(self) -> None:
        self.answers = [{"feasible": False, "why_not": "needs a headless browser"}]
        got = self.step("marketplaces.scout_apify")
        self.assertIn("market.spec_rejected", [o.kind for o in got.value])

    def test_a_store_that_cannot_be_read_is_never_an_empty_market(self) -> None:
        self.http.down = True
        got = self.step("marketplaces.scout_apify")
        self.assertIsInstance(got, Err)
        self.assertEqual(got.error.kind, ErrorKind.UNAVAILABLE)
        # with what an earlier run read, the scout works on and says the read failed
        self.http.down = False
        self.step("marketplaces.scout_apify")
        self.http.down = True
        self.now += 86400
        got = self.step("marketplaces.scout_apify")
        self.assertIsInstance(got, Ok)
        self.assertIn("market.read_failed", [o.kind for o in got.value])

    def test_the_chrome_scout_reads_only_what_robots_allows(self) -> None:
        cws = providers.CWS
        item = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        item2 = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        page = (f'<a href="./detail/word-count/{item}"><div>Word Count</div>'
                '<span>Average rating 2.9 out of 5 stars.</span></a>'
                f'<a href="./detail/word-counter/{item2}"><div>Word Counter</div>'
                '<span>Average rating 3.1 out of 5 stars.</span></a>'
                '<a href="./category/extensions/productivity/tools">t</a>')
        self.http.pages.update({
            f"{cws}/robots.txt": (200, "User-agent: *\nDisallow: /search\n"
                                       "Disallow: /detail/*/reviews\n"),
            f"{cws}/": (200, page),
            f"{cws}/category/extensions/productivity/tools": (200, page),
            f"{cws}/detail/word-count/{item}": (200, "<div>40,000 users</div><p>12 ratings</p>"),
            f"{cws}/detail/word-counter/{item2}": (200, "<div>9,000 users</div>")})
        self.answers = [dict(CHROME_WORDS)]
        got = self.step("marketplaces.scout_chrome")
        self.assertIsInstance(got, Ok, got)
        self.assertFalse(any("/search" in u or u.endswith("/reviews") for u in self.http.calls))
        listings = self.candidates("chrome")["listings"]
        self.assertEqual(listings[item]["users"], 40000)
        # specified, but JavaScript cannot be built yet: it waits, and says why
        cand = next(c for c in self.candidates("chrome")["candidates"].values()
                    if c.get("status") == "specified")
        self.assertIn("javascript", cand["waiting"])
        self.assertFalse((self.builds / "backlog.json").exists())

    def test_a_shopify_spec_goes_to_the_packager(self) -> None:
        shop = providers.SHOPIFY
        page = ''.join(
            f'<div data-app-card-handle-value="{h}" data-app-card-name-value="{n}">'
            f'<span>{r} out of 5 stars</span><span>{c} total reviews</span> • Free</div>'
            for h, n, r, c in (("alt-a", "Alt Text Fixer", 3.2, 400),
                               ("alt-b", "Alt Text Helper", 2.9, 120)))
        self.http.pages.update({f"{shop}/robots.txt": (200, "User-agent: *\nDisallow: *q=*\n"),
                                f"{shop}/": (200, page)})
        self.answers = [dict(SHOPIFY_WORDS)]
        got = self.step("marketplaces.scout_shopify")
        self.assertIn("market.spec_to_packager", [o.kind for o in got.value])
        self.assertTrue(paths.spec_path(self.root, "alt-text-check").is_file())


class PackagerTests(_Case):
    def stage_apify(self) -> None:
        spec = apify_spec()
        paths.write_json(paths.spec_path(self.root, spec["slug"]), spec)
        from pionir.crew.marketplaces.specs import to_backlog_entry
        stage_build(to_backlog_entry(spec), core_build(), self.builds, SecretValues())

    def stage_chrome(self, build=None, slug=None) -> None:
        spec = chrome_spec()
        if slug:
            spec["slug"] = slug
        paths.write_json(paths.spec_path(self.root, spec["slug"]), spec)
        entry = {"slug": spec["slug"], "name": spec["name"], "product_type": "chrome_extension"}
        stage_build(entry, build or chrome_build(), self.builds, SecretValues())

    def test_a_staged_actor_is_drafted_and_waits_for_its_token(self) -> None:
        self.stage_apify()
        got = self.step("marketplaces.packager")
        self.assertIsInstance(got, Ok, got)
        folder = paths.listing_dir(self.root, "sitemap-audit")
        self.assertTrue((folder / "actor-source.zip").is_file())
        self.assertTrue((folder / "icon.png").is_file())
        self.assertEqual(self.pionir.of("apify.publish"), [])     # no token: no wasted yes
        tally = got.value[-1]
        self.assertTrue(tally.payload["missing_credentials"])

    def test_with_the_token_it_parks_one_publish_and_follows_it(self) -> None:
        (self.secrets / "apify-token.txt").write_text("t", encoding="utf-8")
        self.stage_apify()
        self.step("marketplaces.packager")
        (job,) = self.pionir.of("apify.publish")
        listing = json.loads((paths.listing_dir(self.root, "sitemap-audit") /
                              "listing.json").read_text("utf-8"))
        self.assertEqual(job.payload, listing)
        self.step("marketplaces.packager")
        self.assertEqual(len(self.pionir.of("apify.publish")), 1)    # never twice
        rec = json.loads((self.state / "marketplaces.packager.json").read_text("utf-8"))
        self.assertEqual(rec["drafts"]["sitemap-audit"]["status"], "pending_approval")
        self.pionir.approvals["ap-apify.publish-1"] = {"status": "approved", "result": {
            "ok": True, "result": {"ok": True, "published": True,
                                   "url": "https://apify.com/dokaz/sitemap-audit"}}}
        got = self.step("marketplaces.packager")
        self.assertIn("market.published", [o.kind for o in got.value])

    def test_a_denied_publish_is_denied(self) -> None:
        (self.secrets / "apify-token.txt").write_text("t", encoding="utf-8")
        self.stage_apify()
        self.step("marketplaces.packager")
        self.pionir.approvals["ap-apify.publish-1"] = {"status": "denied", "reason": "no"}
        got = self.step("marketplaces.packager")
        self.assertIn("market.denied", [o.kind for o in got.value])
        self.assertNotIn("market.published", [o.kind for o in got.value])

    def test_a_chrome_item_is_created_by_hand_first(self) -> None:
        (self.secrets / "chrome-webstore.json").write_text("{}", encoding="utf-8")
        self.stage_chrome()
        self.step("marketplaces.packager")
        self.step("marketplaces.packager")
        self.assertEqual(self.pionir.of("chrome.publish_update"), [])
        self.assertEqual(list(self.pionir.cards), ["market:chrome:tab-word-count:create"])
        item = "abcdefghijklmnopabcdefghijklmnop"
        (self.root / "chrome-items.json").write_text(json.dumps({"tab-word-count": item}),
                                                     encoding="utf-8")
        self.step("marketplaces.packager")
        (job,) = self.pionir.of("chrome.publish_update")
        self.assertEqual((job.payload["item_id"], job.payload["version"],
                          job.payload["publish_type"]), (item, "1.0.0", "DEFAULT_PUBLISH"))

    def test_a_manifest_asking_for_more_than_its_spec_is_blocked(self) -> None:
        build = chrome_build(permissions=("activeTab", "scripting", "storage", "tabs"))
        self.stage_chrome(build)
        got = self.step("marketplaces.packager")
        blocked = [o for o in got.value if o.kind == "market.draft_blocked"]
        self.assertTrue(blocked)
        self.assertIn("tabs", json.dumps(blocked[0].payload))
        self.assertFalse(paths.listing_dir(self.root, "tab-word-count").exists())

    def test_two_extensions_for_one_purpose_are_refused(self) -> None:
        self.stage_chrome()
        self.stage_chrome(slug="tab-word-count-2")
        self.step("marketplaces.packager")
        rec = json.loads((self.state / "marketplaces.packager.json").read_text("utf-8"))
        states = sorted(d["status"] for d in rec["drafts"].values())
        self.assertEqual(states, ["blocked", "ready"])

    def test_a_shopify_pack_is_made_and_the_owner_told_once(self) -> None:
        spec = shopify_spec()
        paths.write_json(paths.spec_path(self.root, spec["slug"]), spec)
        self.step("marketplaces.packager")
        self.step("marketplaces.packager")
        folder = paths.listing_dir(self.root, spec["slug"])
        self.assertTrue((folder / "submission-pack.md").is_file())
        self.assertIn("NOT submitted", (folder / "submission-pack.md").read_text("utf-8"))
        self.assertEqual(list(self.pionir.cards), [f"market:shopify:{spec['slug']}:pack"])

    def test_a_buildable_spec_waits_for_its_build(self) -> None:
        spec = apify_spec()
        paths.write_json(paths.spec_path(self.root, spec["slug"]), spec)
        self.step("marketplaces.packager")
        self.assertFalse(paths.listing_dir(self.root, spec["slug"]).exists())


class WatcherTests(_Case):
    def publish_record(self) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "marketplaces.packager.json").write_text(json.dumps({"drafts": {
            "sitemap-audit": {"slug": "sitemap-audit", "market": "apify",
                              "status": "published"}}}), encoding="utf-8")

    def test_nothing_to_watch_and_no_credential_is_not_configured(self) -> None:
        got = self.step("marketplaces.watcher")
        self.assertIsInstance(got, Err)
        self.assertEqual(got.error.kind, ErrorKind.NOT_CONFIGURED)
        self.assertIn("setup-apify.ps1", got.error.message)

    def test_stats_rows_and_one_card_when_ratings_move(self) -> None:
        (self.secrets / "apify-token.txt").write_text("t", encoding="utf-8")
        self.publish_record()

        def actor(reviews):
            return {"ok": True, "actors": [{"name": "sitemap-audit", "found": True,
                                            "users_30d": 12, "runs_total": 80,
                                            "runs_7d": {"SUCCEEDED": 5}, "rating": 4.0,
                                            "reviews": reviews,
                                            "url": "https://apify.com/dokaz/sitemap-audit"}]}
        self.pionir.stats = actor(2)
        got = self.step("marketplaces.watcher")
        stats = next(o for o in got.value if o.kind == "market.stats")
        self.assertIn((12, "count", "users"), [(f.value, f.unit, f.measures)
                                               for f in stats.figures])
        report = got.value[-1]
        self.assertEqual(report.kind, "market.report")
        self.assertIn("UNKNOWN", report.payload["earnings"])
        self.assertEqual(self.pionir.cards, {})              # first look: nothing moved
        self.pionir.stats = actor(4)
        self.step("marketplaces.watcher")
        self.step("marketplaces.watcher")
        self.assertEqual(list(self.pionir.cards), ["market:apify:sitemap-audit:ratings:4"])
        perf = json.loads((self.root / "performance.json").read_text("utf-8"))
        self.assertEqual(perf["apify"]["items"][0]["users"], 12)


class ReadinessTests(unittest.TestCase):
    def test_each_worker_names_the_missing_credential(self) -> None:
        reg = default_registry()
        with tempfile.TemporaryDirectory() as d:
            secrets = Path(d)
            self.assertIsNone(reg.require("marketplaces.scout_apify").readiness(secrets))
            why = reg.require("marketplaces.packager").readiness(secrets)
            self.assertIn("apify-token.txt", why)
            self.assertIn("chrome-webstore.json", why)
            (secrets / "apify-token.txt").write_text("t")
            (secrets / "chrome-webstore.json").write_text("{}")
            self.assertIsNone(reg.require("marketplaces.watcher").readiness(secrets))


if __name__ == "__main__":
    unittest.main()
