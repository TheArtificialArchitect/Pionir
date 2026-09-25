"""The results loop: what the posts did, from the site's own visit counter.

Every network is a fake (``FakeHttp`` answering ``/dash/api.json``); the blog record is a
file in a temporary state dir, shaped exactly as the blog worker writes it. Each test
fails if the rule it names is reverted: a figure for a post computed from the wrong row,
a campaign matched to the wrong post, an empty counter reported as zeros (or as a trend),
a missing traffic report reported as zero traffic, or a post missing from a capped list
given a zero it was never measured to have.
"""

import ast
import time
import unittest
from pathlib import Path

from crew_support import temp_dir
from test_crew_fakes import FakeHttp, make_crew
from test_crew_leader import FakeAsk
from test_crew_leader import reply as leader_reply

from pionir.crew import devto as devto_module
from pionir.crew import results as results_module
from pionir.crew.blog import save_record
from pionir.crew.leader import Leader
from pionir.crew.registry import build_registry, default_registry, load_catalogue
from pionir.crew.result import Err, Ok
from pionir.crew.results import NO_DATA, TrafficWorker, split_campaign, utm_part
from pionir.crew.worker import ErrorKind, WorkContext

URL = "https://api.dokaz.net/dash/api.json"

A = ("2026-09-01-verify-email-before-sending", "verify-email-before-sending")
B = ("2026-09-02-qr-codes-from-an-api", "qr-codes-from-an-api")
C = ("2026-09-03-wifi-qr-codes", "wifi-qr-codes")
D = ("2026-09-04-vcard-qr-codes", "vcard-qr-codes")


def post(ids, status="published", settled=1.0):
    did, slug = ids
    p = {"draft_id": did, "slug": slug, "title": "A title", "topic": slug, "status": status,
         "submitted_at": settled - 1, "approval_id": "ap"}
    if status == "published":
        p |= {"url": f"https://api.dokaz.net/blog/{slug}", "settled_at": settled}
    return p


BLOG = {"posts": [post(A, settled=1.0), post(B, settled=2.0), post(C, settled=3.0),
                  post(D, status="pending_approval")],
        "counts": {}, "used_slugs": [], "used_topics": [], "blocked": [],
        "last_drafted_at": None}

# A realistic trafficReport() (Scrooge worker/src/traffic.ts), last 30 days.
TRAFFIC = {
    "since": "2026-08-27",
    "views": 412,
    "by_day": [{"day": "2026-09-20", "n": 200}, {"day": "2026-09-21", "n": 212}],
    "top_paths": [
        {"path": "/", "n": 150}, {"path": "/docs/email-verification-api", "n": 80},
        {"path": f"/blog/{A[1]}", "n": 64}, {"path": f"/blog/{B[1]}", "n": 41},
        {"path": "/docs/qr-code-api", "n": 40}, {"path": "/blog", "n": 30},
        {"path": f"/blog/{C[1]}", "n": 7}],
    "top_referrers": [
        {"ref_host": "direct", "n": 200}, {"ref_host": "www.google.com", "n": 90},
        {"ref_host": "internal", "n": 60}, {"ref_host": "dev.to", "n": 12},
        {"ref_host": "l.instagram.com", "n": 9}, {"ref_host": "bing.com", "n": 5}],
    "top_campaigns": [
        {"campaign": "none", "n": 300},
        {"campaign": f"blog/referral/{A[0]}", "n": 11},
        {"campaign": "instagram/bio/", "n": 9},
        {"campaign": f"devto/referral/{A[0]}", "n": 8},
        {"campaign": "instagram/social/2026-09-05-ig-qr", "n": 4},
        {"campaign": f"blog/referral/{B[0]}", "n": 3},
        {"campaign": "instagram/bio/2026-09-10-launch", "n": 2}],
    "sales_by_campaign": [
        {"campaign": "none", "sales": 5, "net": 4500},
        {"campaign": f"blog/referral/{A[0]}", "sales": 2, "net": 1800}],
    "sales_by_referrer": [{"ref_host": "internal", "sales": 7, "net": 6300}],
}

EMPTY = {"since": "2026-08-27", "views": 0, "by_day": [], "top_paths": [],
         "top_referrers": [], "top_campaigns": [], "sales_by_campaign": [],
         "sales_by_referrer": []}


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = temp_dir()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.secrets = root / "secrets"
        self.state = root / "workers"
        self.worker = default_registry().require("posting.results")

    def token(self, value="abc123") -> None:
        self.secrets.mkdir(parents=True, exist_ok=True)
        (self.secrets / "scrooge-read-token.txt").write_text(value, encoding="utf-8")

    def blog(self, rec=BLOG) -> None:
        save_record(self.state / "posting.blog.json", rec)

    def run_with(self, doc, *, blog=True):
        self.token()
        if blog:
            self.blog()
        self.http = FakeHttp({URL: (200, doc)})
        return self.worker.run(WorkContext(now=time.time(), http=self.http,
                                           secrets_dir=self.secrets, state_dir=self.state))

    @staticmethod
    def by_kind(result) -> dict:
        out: dict = {}
        for o in result.value:
            out.setdefault(o.kind, []).append(o)
        return out

    @staticmethod
    def figs(output) -> dict:
        return {(f.measures, f.stream): f for f in output.figures}


class CatalogueTests(unittest.TestCase):
    def test_results_reads_the_same_dash_with_the_same_token_as_the_ledger(self) -> None:
        reg = default_registry()
        w, ledger = reg.require("posting.results"), reg.require("treasury.ledger")
        self.assertIsInstance(w, TrafficWorker)
        self.assertEqual((w.division, w.cadence_seconds), ("posting", 21600))
        self.assertEqual((w.url, w.token_file), (ledger.url, ledger.token_file))
        self.assertEqual(w.blog_worker, "posting.blog")

    def test_the_posting_leader_is_told_how_to_read_results(self) -> None:
        notes = default_registry().division("posting").leader_notes
        for words in ("posting.results", "too early to tell", "fewer than 20 page views",
                      "never claim cause from one data point", "no traffic data yet",
                      "never write it as zero"):
            self.assertIn(words, notes)


class FigureTests(_Case):
    def test_per_post_views_clicks_devto_and_sales_from_a_realistic_report(self) -> None:
        result = self.run_with({"summary": {}, "traffic": TRAFFIC})
        self.assertIsInstance(result, Ok)
        self.assertEqual(self.http.calls[0][1], {"x-dash-token": "abc123"})
        kinds = self.by_kind(result)
        self.assertEqual(sorted(kinds), ["traffic.posts", "traffic.sales", "traffic.site",
                                         "traffic.working"])
        (posts,) = kinds["traffic.posts"]
        f = self.figs(posts)
        self.assertEqual(f[("post page views", A[1])].value, 64)
        self.assertEqual(f[("post clicks to product pages", A[1])].value, 11)
        self.assertEqual(f[("post dev.to visits", A[1])].value, 8)
        self.assertEqual(f[("post sales", A[1])].value, 2)
        net = f[("post net revenue", A[1])]
        self.assertEqual((net.value, net.unit), (1800, "usd_cents"))
        self.assertEqual(f[("post page views", B[1])].value, 41)
        self.assertEqual(f[("post clicks to product pages", B[1])].value, 3)
        self.assertNotIn(("post sales", B[1]), f)            # nothing attributed: no figure
        # C is listed with 7 views and, the campaign list being complete, no clicks
        self.assertEqual(f[("post page views", C[1])].value, 7)
        self.assertEqual(f[("post clicks to product pages", C[1])].value, 0)
        # the post still waiting for approval is not a published post
        self.assertFalse(any(fig.stream == D[1] for fig in posts.figures))
        self.assertEqual(posts.payload["published_posts"], 3)
        self.assertEqual(posts.payload["too_early_to_tell"], [C[1]])
        for o in result.value:
            self.assertFalse(o.derived)
            self.assertEqual(o.provenance["source"], "real")
            self.assertIn("since 2026-08-27", o.provenance["source_detail"])
            if o.kind != "traffic.working":
                self.assertIn("since 2026-08-27", o.payload["source"])
            for fig in o.figures:
                self.assertEqual(fig.window, "last30", fig)
                self.assertIn(fig.unit, ("count", "usd_cents"))

    def test_site_and_channel_figures(self) -> None:
        result = self.run_with({"traffic": TRAFFIC})
        (site,) = self.by_kind(result)["traffic.site"]
        f = self.figs(site)
        self.assertEqual(f[("site page views", "all")].value, 412)
        self.assertEqual(f[("blog index page views", "/blog")].value, 30)
        self.assertEqual(f[("blog post page views", "/blog/*")].value, 64 + 41 + 7)
        self.assertEqual(f[("clicks from blog posts to product pages", "blog")].value, 14)
        self.assertEqual(f[("instagram bio clicks", "instagram/bio")].value, 9 + 2)
        self.assertEqual(f[("dev.to visits", "devto")].value, 8)
        self.assertEqual(f[("visits from search engines", "search")].value, 95)
        self.assertEqual(f[("visits referred", "dev.to")].value, 12)
        self.assertNotIn(("visits referred", "direct"), f)
        (sales,) = self.by_kind(result)["traffic.sales"]
        s = self.figs(sales)
        self.assertEqual(s[("sales", f"blog/referral/{A[0]}")].value, 2)
        self.assertEqual(s[("net revenue", f"blog/referral/{A[0]}")].value, 1800)
        self.assertEqual(s[("sales in the window", "all")].value, 7)

    def test_whats_working_names_the_top_posts_and_channels_from_real_numbers(self) -> None:
        result = self.run_with({"traffic": TRAFFIC})
        (w,) = self.by_kind(result)["traffic.working"]
        self.assertEqual(w.payload["top_by_views"], [A[1], B[1], C[1]])
        self.assertEqual(w.payload["top_by_clicks"], [A[1], B[1]])     # C sent none
        self.assertEqual(w.payload["visitors_from"], ["blog links", "instagram bio", "dev.to",
                                                      "search"])
        self.assertEqual(next(iter(sorted(w.payload))), "summary")    # the brief shows it first
        s = w.payload["summary"]
        self.assertIn(f"most viewed posts: {A[1]} (64), {B[1]} (41), {C[1]} (7)", s)
        self.assertIn(f"most clicks to product pages: {A[1]} (11), {B[1]} (3)", s)
        self.assertIn("instagram bio (11)", s)
        self.assertNotIn("too early", s)       # A and B have 20+ views

    def test_a_campaign_is_matched_to_its_own_post_only(self) -> None:
        self.assertEqual(split_campaign(f"blog/referral/{A[0]}"), ("blog", "referral", A[0]))
        self.assertIsNone(split_campaign("none"))
        self.assertEqual(utm_part("My Post!"), "mypost")         # Scrooge's sanitiser
        # a campaign on another source, or another post's campaign, is not this post's click
        traffic = dict(TRAFFIC, top_campaigns=[
            {"campaign": f"instagram/social/{A[0]}", "n": 50},
            {"campaign": f"blog/referral/{A[0]}-2", "n": 40}])
        (posts,) = self.by_kind(self.run_with({"traffic": traffic}))["traffic.posts"]
        self.assertEqual(self.figs(posts)[("post clicks to product pages", A[1])].value, 0)


class HonestyTests(_Case):
    def test_no_traffic_yet_says_so_and_invents_no_figure(self) -> None:
        result = self.run_with({"traffic": EMPTY})
        self.assertIsInstance(result, Ok)
        (only,) = result.value
        self.assertEqual(only.kind, "traffic.working")
        self.assertEqual(only.payload["summary"], NO_DATA)
        self.assertEqual(only.figures, ())

    def test_a_missing_traffic_key_is_not_configured_never_zero(self) -> None:
        result = self.run_with({"summary": {"all_time_net": 0}})
        self.assertIsInstance(result, Err)
        self.assertEqual(result.error.kind, ErrorKind.NOT_CONFIGURED)
        self.assertIn("UNKNOWN, not zero", result.error.message)

    def test_no_token_is_not_configured_and_nothing_is_asked(self) -> None:
        http = FakeHttp({URL: (200, {"traffic": TRAFFIC})})
        result = self.worker.run(WorkContext(now=time.time(), http=http,
                                             secrets_dir=self.secrets, state_dir=self.state))
        self.assertEqual(result.error.kind, ErrorKind.NOT_CONFIGURED)
        self.assertIn("traffic is UNKNOWN, not zero", result.error.message)
        self.assertEqual(http.calls, [])

    def test_a_missing_list_is_malformed_not_an_empty_one(self) -> None:
        broken = {k: v for k, v in TRAFFIC.items() if k != "top_paths"}
        result = self.run_with({"traffic": broken})
        self.assertEqual(result.error.kind, ErrorKind.MALFORMED)
        bad_count = dict(TRAFFIC, views="lots")
        self.assertEqual(self.run_with({"traffic": bad_count}).error.kind, ErrorKind.MALFORMED)

    def test_a_post_missing_from_a_capped_list_has_unknown_views_not_zero(self) -> None:
        full = [{"path": f"/docs/page-{i}", "n": 500 - i} for i in range(20)]
        camps = [{"campaign": f"x/social/c{i}", "n": 100 - i} for i in range(20)]
        traffic = dict(TRAFFIC, top_paths=full, top_campaigns=camps)
        result = self.run_with({"traffic": traffic})
        kinds = self.by_kind(result)
        (posts,) = kinds["traffic.posts"]
        self.assertFalse(any(f.measures == "post page views" for f in posts.figures))
        self.assertFalse(any(f.measures == "post clicks to product pages"
                             for f in posts.figures))
        self.assertEqual(sorted(posts.payload["views_unknown_not_in_top_paths"]),
                         sorted([A[1], B[1], C[1]]))
        self.assertEqual(posts.payload["too_early_to_tell"], [])
        (site,) = kinds["traffic.site"]
        f = {x.measures: x for x in site.figures}
        self.assertIn("blog post page views (at least; the list is capped at 20)", f)
        self.assertNotIn("blog index page views", f)       # /blog is not listed: unknown
        (w,) = kinds["traffic.working"]
        self.assertEqual(w.payload["top_by_views"], [])

    def test_every_post_under_twenty_views_is_too_early_to_tell(self) -> None:
        paths = [{"path": f"/blog/{A[1]}", "n": 12}, {"path": f"/blog/{B[1]}", "n": 3}]
        result = self.run_with({"traffic": dict(TRAFFIC, top_paths=paths)})
        (w,) = self.by_kind(result)["traffic.working"]
        self.assertIn("every post is too early to tell (under 20 views)", w.payload["summary"])
        (posts,) = self.by_kind(result)["traffic.posts"]
        self.assertEqual(posts.payload["too_early_to_tell"], [A[1], B[1], C[1]])

    def test_no_blog_record_yet_still_reports_the_site(self) -> None:
        result = self.run_with({"traffic": TRAFFIC}, blog=False)
        kinds = self.by_kind(result)
        self.assertNotIn("traffic.posts", kinds)
        self.assertIn("traffic.site", kinds)
        (w,) = kinds["traffic.working"]
        self.assertIn("no blog posts yet", w.payload["summary"])
        self.assertEqual(w.payload["top_by_views"], [])


class LeaderTests(unittest.TestCase):
    """The worker inside a real crew, and the posting leader reading what it recorded."""

    def setUp(self) -> None:
        tmp = temp_dir()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "secrets").mkdir()
        (root / "secrets" / "scrooge-read-token.txt").write_text("abc", encoding="utf-8")
        cat = load_catalogue()
        cat["divisions"] = [d for d in cat["divisions"] if d["id"] == "posting"]
        self.crew = make_crew(root, registry=build_registry(cat),
                              http=FakeHttp({URL: (200, {"traffic": TRAFFIC})}))
        self.addCleanup(self.crew.stop)
        save_record(self.crew.cfg.state_dir / "workers" / "posting.blog.json", BLOG)
        self.crew.dispatcher.dispatch(only=("posting.results",), wait=True)

    def lead(self, summary: str, figures: list):
        ask = FakeAsk(leader_reply(summary=summary, figures=figures))
        lead = Leader("posting", self.crew.registry, self.crew.store, ask=ask, model="m",
                      clock=time.time)
        return lead.run(), ask

    def test_the_leader_reads_the_summary_and_a_report_from_its_figures_is_grounded(
            self) -> None:
        result, ask = self.lead(
            f"The {A[1]} post had 64 views and sent 11 clicks to product pages.",
            [{"value": 64, "unit": "count", "measures": "post page views", "stream": A[1],
              "window": "last30"}])
        system, user = (m["content"] for m in ask.calls[0]["messages"])
        self.assertIn("too early to tell", system)
        self.assertIn("traffic.working", user)
        self.assertIn("most viewed posts", user)
        self.assertIsInstance(result, Ok, result)

    def test_a_report_inventing_a_traffic_figure_is_rejected(self) -> None:
        result, _ask = self.lead(f"The {A[1]} post had 640 views.", [])
        self.assertIsInstance(result, Err)
        self.assertEqual(result.error.kind, ErrorKind.UNGROUNDED)


class NoModelTests(unittest.TestCase):
    def test_the_results_and_devto_modules_import_nothing_that_can_call_a_model(self) -> None:
        for mod in (results_module, devto_module):
            tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
            names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    names.add(node.module or "")
                elif isinstance(node, ast.Import):
                    names |= {a.name for a in node.names}
            for bad in ("brain", "budget", "escalation", "leader", "urllib.request",
                        "subprocess"):
                self.assertFalse(any(bad in n for n in names), (mod.__name__, bad, names))


if __name__ == "__main__":
    unittest.main()
