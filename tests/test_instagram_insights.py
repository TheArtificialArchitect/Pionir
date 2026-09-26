"""What the Instagram posts did, from Instagram's own Media Insights - honest numbers only.

``social.instagram_insights`` (the adapter) is tested against a fake Graph API at the HTTP
opener - the real opener's signature, nothing touches the network - and once with the real
urllib opener against a loopback server. ``posting.results`` (the crew) is tested with a
fake Pionir (``ctx.job`` answering like the adapter). Each test fails if the rule it names
is reverted: a metric parsed into the wrong post, one rejected metric failing the whole
post (or the whole call), a rejected token or a missing permission reported as anything
but itself, a missing number reported as zero, a figure without its unit, window or post,
or insights read more often than once per 6 h.
"""

from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from test_crew_blog import FakeHands
from test_crew_fakes import FakeHttp
from test_instagram_post import USER_ID, _settings, serve, write_ig_token

from pionir.adapters.content import ContentSettings
from pionir.adapters.instagram import (
    IMAGE_METRICS,
    INSIGHTS,
    INSIGHTS_SCOPE,
    TOKEN_REJECTED,
    InstagramAdapter,
    InstagramSettings,
)
from pionir.bootstrap import build_runtime
from pionir.contracts import RiskLevel, Task
from pionir.crew import insights as insights_module
from pionir.crew.blog import save_record
from pionir.crew.hands import JobOutcome
from pionir.crew.instagram import InstagramWorker
from pionir.crew.registry import default_registry
from pionir.crew.result import Ok
from pionir.crew.worker import WorkContext
from pionir.errors import AdapterProtocolError
from pionir.server import PionirApp

# Fake secrets, built by concatenation so no scanner mistakes them for real ones.
IG_TOKEN = "IGAA" + "fake" + "Insights" + "Token" + "0123456789abcdef" * 2
PUBLISH_TOKEN = "scrooge-" + "fake-" + "publish-" + "token-5e1d"
GRAPH = "https://graph.instagram.test/v25.0"
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
M1, M2, M3 = "18000000000000001", "18000000000000002", "18000000000000003"
LINK = {M1: "https://www.instagram.com/p/AAA1/", M2: "https://www.instagram.com/p/BBB2/",
        M3: "https://www.instagram.com/p/CCC3/"}


def metric(name: str, value: Any, *, total: bool = False) -> dict[str, Any]:
    """One row of Meta's /insights answer: ``values`` (the documented lifetime shape) or
    ``total_value``."""
    row: dict[str, Any] = {"name": name, "period": "lifetime", "title": name,
                           "description": "", "id": f"x/insights/{name}/lifetime"}
    if total:
        row["total_value"] = {"value": value}
    else:
        row["values"] = [{"value": value}]
    return row


def graph_error(code: int, message: str, **extra: Any) -> dict[str, Any]:
    return {"error": {"message": message, "type": "OAuthException", "code": code,
                      "fbtrace_id": "t", **extra}}


class _Response:
    def __init__(self, payload: Any) -> None:
        self.status = 200
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self, _n: int = -1) -> bytes:
        return self._raw

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class FakeGraph:
    """The Instagram Graph API's media, media list and insights routes, in memory. Checks
    the token like the real one. A metric in ``rejected[media]`` makes any insights call
    naming it fail with Graph code 100, as Meta does for a metric a media does not
    support; a metric with no value in ``values[media]`` is answered with no row (Meta's
    empty data set)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.values: dict[str, dict[str, int]] = {
            M1: {"reach": 120, "likes": 14, "comments": 3, "saved": 5, "shares": 2,
                 "total_interactions": 24, "views": 310},
            M2: {"reach": 480, "likes": 40, "comments": 6, "saved": 11, "shares": 9,
                 "total_interactions": 66, "views": 900},
            M3: {"reach": 55, "likes": 2, "comments": 0, "saved": 0, "shares": 0,
                 "total_interactions": 2, "views": 70},
        }
        self.rejected: dict[str, set[str]] = {}
        self.unreadable: set[str] = set()                 # media the media call refuses
        self.forced: tuple[int, Any] | None = None        # every call answers this
        self.total_value: set[str] = set()                # media answered as total_value
        self._lock = threading.Lock()

    def steps(self) -> list[str]:
        return [c["step"] for c in self.calls]

    def __call__(self, request: Any, data: Any = None, timeout: float | None = None) -> Any:
        if data is not None:
            raise TypeError(f"opener got a positional data argument: {data!r}")
        with self._lock:
            return self._handle(request)

    @staticmethod
    def _error(url: str, status: int, payload: Any) -> None:
        raise urllib.error.HTTPError(url, status, "error", {},  # type: ignore[arg-type]
                                     io.BytesIO(json.dumps(payload).encode("utf-8")))

    def _handle(self, request: Any) -> _Response:
        url = request.full_url
        parsed = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        parts = parsed.path.split("/")[2:]              # after /v25.0
        if parts[-1] == "insights":
            step, media = "insights", parts[0]
        elif parts[-1] == "media":
            step, media = "list", None
        else:
            step, media = "media", parts[0]
        self.calls.append({"step": step, "media": media, "path": parsed.path, "query": query,
                           "method": request.get_method()})
        if self.forced is not None:
            status, payload = self.forced
            if status != 200:
                self._error(url, status, payload)
            return _Response(payload)
        if query.get("access_token") != IG_TOKEN:
            self._error(url, 400, graph_error(190, "Invalid OAuth access token - Cannot "
                                                   "parse access token"))
        if step == "list":
            n = int(query.get("limit", "25"))
            return _Response({"data": [
                {"id": m, "permalink": LINK[m], "timestamp": f"2026-09-2{i}T10:00:00+0000",
                 "caption": "A caption with 99 numbers and words"}
                for i, m in enumerate((M3, M2, M1))][:n]})
        if media not in self.values or media in self.unreadable:
            self._error(url, 400, graph_error(100, "Unsupported get request. Object with ID "
                                                   f"'{media}' does not exist"))
        if step == "media":
            return _Response({"id": media, "permalink": LINK[media],
                              "timestamp": "2026-09-20T10:00:00+0000"})
        asked = query["metric"].split(",")
        bad = [m for m in asked if m in self.rejected.get(media, set())]
        if bad:
            self._error(url, 400, graph_error(
                100, f"(#100) metric[{asked.index(bad[0])}] must be one of the following "
                     "values: reach, likes, comments"))
        have = self.values[media]
        return _Response({"data": [metric(m, have[m], total=media in self.total_value)
                                   for m in asked if m in have]})


class _AdapterCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ig_file = self.root / "secrets" / "instagram.json"
        write_ig_token(self.ig_file, token=IG_TOKEN, refreshed_at="2026-09-25T12:00:00Z")
        self.graph = FakeGraph()
        self.adapter = InstagramAdapter(
            InstagramSettings(graph_url=GRAPH, token_file=self.ig_file,
                              content=ContentSettings(base_url="https://api.dokaz.test",
                                                      token_file=self.root / "none.txt")),
            opener=self.graph, sleep=lambda _s: None, clock=lambda: NOW)

    def read(self, payload: dict[str, Any]) -> dict[str, Any]:
        out = dict(self.adapter.execute(Task(INSIGHTS, payload)).output)
        self.assertNotIn(IG_TOKEN, json.dumps(out))            # never the token
        return out

    @staticmethod
    def by_id(out: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {m["media_id"]: m for m in out["media"]}


class InsightsAdapterTests(_AdapterCase):
    def test_it_is_read_only_never_routed_and_never_parked(self) -> None:
        caps = {c.name: c for c in self.adapter.manifest.capabilities}
        cap = caps[INSIGHTS]
        self.assertIs(cap.risk, RiskLevel.READ_ONLY)
        self.assertFalse(cap.requires_approval)
        self.assertFalse(cap.routable)
        runtime = build_runtime(_settings(self.root, content_url=None))
        self.addCleanup(runtime.cortex.close)
        runtime.register(self.adapter)
        app = PionirApp(runtime)
        answer = app.run_task(INSIGHTS, {"media_ids": [M1]}, permissions=[])
        self.assertIs(answer["ok"], True, answer)              # ran, with no permission
        self.assertNotEqual(answer.get("status"), "pending_approval")
        self.assertEqual(app.approvals.pending(), [])
        self.assertEqual(answer["result"]["media"][0]["metrics"]["reach"], 120)

    def test_metrics_are_parsed_per_post(self) -> None:
        self.graph.total_value = {M2}                          # the other documented shape
        out = self.read({"media_ids": [M1, M2]})
        self.assertIs(out["ok"], True, out)
        self.assertEqual(out["period"], "lifetime")
        self.assertEqual(out["measured_at"], "2026-09-26T12:00:00Z")
        self.assertEqual(out["metrics_requested"], list(IMAGE_METRICS))
        one, two = self.by_id(out)[M1], self.by_id(out)[M2]
        self.assertEqual(one, {"media_id": M1, "permalink": LINK[M1],
                               "timestamp": "2026-09-20T10:00:00+0000",
                               "metrics": self.graph.values[M1], "unavailable": []})
        self.assertEqual(two["metrics"], self.graph.values[M2])
        # one media call and ONE insights call per media, all the image metrics at once
        self.assertEqual(self.graph.steps(), ["media", "media", "insights", "insights"])
        insight = self.graph.calls[2]
        self.assertEqual(insight["path"], f"/v25.0/{M1}/insights")
        self.assertEqual(insight["query"]["metric"],
                         "reach,likes,comments,saved,shares,total_interactions,views")
        self.assertEqual(self.graph.calls[0]["query"]["fields"], "id,permalink,timestamp")

    def test_a_rejected_metric_degrades_only_that_metric_of_that_post(self) -> None:
        self.graph.rejected = {M1: {"views"}}
        out = self.read({"media_ids": [M1, M2]})
        self.assertIs(out["ok"], True, out)
        one, two = self.by_id(out)[M1], self.by_id(out)[M2]
        self.assertEqual(one["unavailable"], ["views"])
        self.assertNotIn("views", one["metrics"])               # no number, not a zero
        expected = {k: v for k, v in self.graph.values[M1].items() if k != "views"}
        self.assertEqual(one["metrics"], expected)
        self.assertEqual(two["unavailable"], [])                # the other post untouched
        self.assertEqual(two["metrics"], self.graph.values[M2])
        # M1: the combined call, then one call per metric; M2: the combined call only
        m1 = [c for c in self.graph.calls if c["media"] == M1 and c["step"] == "insights"]
        self.assertEqual([c["query"]["metric"] for c in m1[1:]], list(IMAGE_METRICS))
        m2 = [c for c in self.graph.calls if c["media"] == M2 and c["step"] == "insights"]
        self.assertEqual(len(m2), 1)

    def test_a_metric_with_no_data_is_unavailable_never_zero(self) -> None:
        del self.graph.values[M1]["saved"]
        del self.graph.values[M1]["shares"]
        out = self.read({"media_ids": [M1]})
        (one,) = out["media"]
        self.assertEqual(one["unavailable"], ["saved", "shares"])
        self.assertNotIn("saved", one["metrics"])
        self.assertNotIn("shares", one["metrics"])
        self.graph.values[M1] = {}                              # Meta's empty data set
        (one,) = self.read({"media_ids": [M1]})["media"]
        self.assertEqual((one["metrics"], one["unavailable"]), ({}, list(IMAGE_METRICS)))

    def test_a_value_that_is_not_a_count_is_unavailable(self) -> None:
        self.graph.values[M1]["reach"] = "lots"                 # type: ignore[assignment]
        self.graph.values[M1]["likes"] = -3
        (one,) = self.read({"media_ids": [M1]})["media"]
        self.assertEqual(one["unavailable"], ["reach", "likes"])

    def test_a_media_instagram_will_not_describe_keeps_its_row_and_says_why(self) -> None:
        self.graph.unreadable = {M1}
        out = self.read({"media_ids": [M1, M2]})
        self.assertIs(out["ok"], True, out)
        one = self.by_id(out)[M1]
        self.assertEqual((one["metrics"], one["unavailable"]), ({}, list(IMAGE_METRICS)))
        self.assertIn("does not exist", one["error"])
        self.assertEqual(self.by_id(out)[M2]["metrics"], self.graph.values[M2])

    def test_a_rejected_token_is_the_token_hint_for_the_whole_call(self) -> None:
        write_ig_token(self.ig_file, token="IGAA" + "revoked" + "x" * 30,
                       refreshed_at="2026-09-25T12:00:00Z")
        out = self.read({"media_ids": [M1, M2]})
        self.assertIs(out["ok"], False)
        self.assertEqual(out["unavailable"], TOKEN_REJECTED)
        self.assertIs(out["token_rejected"], True)
        self.assertNotIn("media", out)
        self.assertEqual(len(self.graph.calls), 1)              # stopped at the first 190

    def test_a_missing_insights_permission_names_the_scope(self) -> None:
        for code, message in ((10, ("(#10) Application does not have permission for "
                                    "this action")),
                              (200, ("(#200) Requires instagram_business_manage_insights "
                                     "permission"))):
            with self.subTest(code=code):
                self.graph.calls.clear()
                self.graph.forced = (400, graph_error(code, message))
                out = self.read({"media_ids": [M1, M2]})
                self.assertIs(out["ok"], False)
                self.assertIs(out["permission_missing"], True)
                self.assertIn(INSIGHTS_SCOPE, out["unavailable"])
                self.assertEqual(out["scope"], "instagram_business_manage_insights")
                self.assertEqual(len(self.graph.calls), 1)

    def test_code_10_that_is_not_about_permission_is_about_that_media_only(self) -> None:
        # Meta's code 10 is also "not enough viewers": one media's trouble, not the token's
        self.graph.rejected = {}
        original = self.graph._handle

        def handle(request: Any) -> _Response:
            if f"/{M1}/insights" in request.full_url:
                self.graph.calls.append({"step": "insights", "media": M1, "query": {}})
                self.graph._error(request.full_url, 400, graph_error(
                    10, "(#10) Not enough viewers for the media to show insights"))
            return original(request)

        self.graph._handle = handle  # type: ignore[method-assign]
        out = self.read({"media_ids": [M1, M2]})
        self.assertIs(out["ok"], True, out)
        self.assertEqual(self.by_id(out)[M1]["unavailable"], list(IMAGE_METRICS))
        self.assertEqual(self.by_id(out)[M2]["metrics"], self.graph.values[M2])

    def test_rate_limits_and_a_down_graph_are_unavailable(self) -> None:
        for forced, words in (((400, graph_error(4, "Application request limit reached")),
                               "asked to wait"),
                              ((500, graph_error(2, "Service temporarily unavailable",
                                                 is_transient=True)), "asked to wait"),
                              ((503, {"nothing": True}), "HTTP 503")):
            with self.subTest(forced=forced):
                self.graph.forced = forced
                out = self.read({"media_ids": [M1]})
                self.assertIs(out["ok"], False)
                self.assertIn(words, out["unavailable"])

    def test_recent_lists_the_accounts_latest_media_and_drops_the_caption(self) -> None:
        out = self.read({"recent": 2})
        self.assertIs(out["ok"], True, out)
        listing = self.graph.calls[0]
        self.assertEqual(listing["path"], f"/v25.0/{USER_ID}/media")
        self.assertEqual(listing["query"]["fields"], "id,permalink,timestamp,caption")
        self.assertEqual(listing["query"]["limit"], "2")
        self.assertEqual([m["media_id"] for m in out["media"]], [M3, M2])
        self.assertEqual(out["media"][0]["permalink"], LINK[M3])
        self.assertNotIn("caption", json.dumps(out))            # words, not a figure
        self.assertEqual(self.graph.steps(), ["list", "insights", "insights"])

    def test_bad_payloads_are_refused_before_any_call(self) -> None:
        for payload in ({}, {"media_ids": []}, {"media_ids": [M1], "recent": 3},
                        {"recent": 0}, {"recent": 26}, {"recent": True},
                        {"media_ids": ["../me"]}, {"media_ids": ["1" * 41]},
                        {"media_ids": [str(10**15 + i) for i in range(26)]},
                        {"media_ids": [M1], "extra": 1}):
            with self.subTest(payload=payload):
                with self.assertRaises(AdapterProtocolError):
                    self.adapter.validate(Task(INSIGHTS, payload))
                with self.assertRaises(AdapterProtocolError):
                    self.adapter.execute(Task(INSIGHTS, payload))
        self.assertEqual(self.graph.calls, [])

    def test_no_token_is_not_configured(self) -> None:
        self.ig_file.unlink()
        out = self.read({"media_ids": [M1]})
        self.assertIs(out["not_configured"], True)
        self.assertEqual(self.graph.calls, [])


class InsightsRealOpenerTests(unittest.TestCase):
    """The default urllib opener against a real loopback Graph server."""

    def test_insights_through_the_real_opener(self) -> None:
        def graph(record: dict[str, Any]) -> tuple[int, Any]:
            parsed = urllib.parse.urlsplit(record["path"])
            query = dict(urllib.parse.parse_qsl(parsed.query))
            if query.get("access_token") != IG_TOKEN:
                return 400, graph_error(190, "Invalid OAuth access token")
            if parsed.path.endswith("/insights"):
                if query["metric"] != ",".join(IMAGE_METRICS):
                    return 400, graph_error(100, "(#100) metric[0] must be one of ...")
                return 200, {"data": [metric("reach", 77), metric("likes", 5)]}
            return 200, {"id": M1, "permalink": LINK[M1], "timestamp": "2026-09-20T10:00:00Z"}

        url, seen = serve(self, graph)
        with tempfile.TemporaryDirectory() as tmp:
            ig = Path(tmp) / "instagram.json"
            write_ig_token(ig, token=IG_TOKEN, refreshed_at="2026-09-25T12:00:00Z")
            adapter = InstagramAdapter(InstagramSettings(
                graph_url=f"{url}/v25.0", token_file=ig,
                content=ContentSettings(base_url="https://api.dokaz.test",
                                        token_file=Path(tmp) / "none.txt")),
                clock=lambda: NOW)                              # the default, real opener
            out = dict(adapter.execute(Task(INSIGHTS, {"media_ids": [M1]})).output)
        self.assertIs(out["ok"], True, out)
        (one,) = out["media"]
        self.assertEqual(one["metrics"], {"reach": 77, "likes": 5})
        self.assertEqual(one["unavailable"], ["comments", "saved", "shares",
                                              "total_interactions", "views"])
        self.assertEqual([(r["method"], urllib.parse.urlsplit(r["path"]).path) for r in seen],
                         [("GET", f"/v25.0/{M1}"), ("GET", f"/v25.0/{M1}/insights")])
        self.assertNotIn(IG_TOKEN, json.dumps(out))


# ---- the crew: posting.results reads it ---------------------------------------------------
DASH = "https://api.dokaz.net/dash/api.json"
EMPTY_TRAFFIC = {"since": "2026-08-27", "views": 0, "by_day": [], "top_paths": [],
                 "top_referrers": [], "top_campaigns": [], "sales_by_campaign": [],
                 "sales_by_referrer": []}
TRAFFIC = dict(EMPTY_TRAFFIC, views=40, top_paths=[{"path": "/", "n": 40}])
T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC).timestamp()
P1, P2 = "2026-09-20-ig-check-an-email", "2026-09-21-ig-qr-codes-for-menus"


def ig_post(draft_id: str, mid: str | None, settled: float) -> dict[str, Any]:
    p: dict[str, Any] = {"draft_id": draft_id, "headline": "A headline", "topic": "t",
                         "status": "published", "submitted_at": settled - 1,
                         "approval_id": "ap", "settled_at": settled,
                         "permalink": LINK[mid or M2]}
    if mid is not None:
        p["media_id"] = mid
    return p


def answer(media: list[dict[str, Any]], measured: str = "2026-09-26T11:00:00Z") -> JobOutcome:
    """What the fake Pionir says: the adapter's result, done."""
    result = {"ok": True, "period": "lifetime", "measured_at": measured,
              "metrics_requested": list(IMAGE_METRICS), "media": media}
    return JobOutcome("done", INSIGHTS, task_id="t-ig", agent_id="instagram", result=result)


def row(mid: str, metrics: dict[str, int]) -> dict[str, Any]:
    return {"media_id": mid, "permalink": LINK[mid], "timestamp": "2026-09-20T10:00:00+0000",
            "metrics": metrics, "unavailable": [m for m in IMAGE_METRICS if m not in metrics]}


FULL1 = {"reach": 120, "likes": 14, "comments": 3, "saved": 5, "shares": 2,
         "total_interactions": 24, "views": 310}
FULL2 = {"reach": 480, "likes": 40, "comments": 6, "saved": 11, "shares": 9,
         "total_interactions": 66, "views": 900}


class _CrewCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.secrets = root / "secrets"
        self.secrets.mkdir()
        (self.secrets / "scrooge-read-token.txt").write_text("r" + "ead", encoding="utf-8")
        self.state = root / "workers"
        self.worker = default_registry().require("posting.results")
        self.hands = FakeHands(answer([row(M1, FULL1), row(M2, FULL2)]))

    def instagram(self, posts: list[dict[str, Any]]) -> None:
        save_record(self.state / "posting.instagram.json",
                    {"posts": posts, "counts": {}, "used_slugs": [], "used_topics": [],
                     "blocked": [], "last_drafted_at": None})

    def run_at(self, now: float = T0, traffic: dict[str, Any] = TRAFFIC, job: bool = True):
        result = self.worker.run(WorkContext(
            now=now, http=FakeHttp({DASH: (200, {"traffic": traffic})}),
            secrets_dir=self.secrets, state_dir=self.state,
            job=self.hands.job if job else None))
        self.assertIsInstance(result, Ok, result)
        return {o.kind: o for o in result.value}

    @staticmethod
    def figs(output) -> dict:
        return {(f.measures, f.stream): f for f in output.figures}


class ResultsInsightsTests(_CrewCase):
    def test_figures_are_typed_windowed_per_post_and_totalled(self) -> None:
        self.instagram([ig_post(P1, M1, 1.0), ig_post(P2, M2, 2.0)])
        kinds = self.run_at()
        (job,) = self.hands.jobs
        self.assertEqual((job.capability, job.payload, job.permissions),
                         (INSIGHTS, {"media_ids": [M1, M2]}, ()))
        ig = kinds["traffic.instagram"]
        f = self.figs(ig)
        for label, key in (("reach", "reach"), ("likes", "likes"), ("comments", "comments"),
                           ("saves", "saved"), ("shares", "shares"),
                           ("interactions", "total_interactions"), ("views", "views")):
            for did, full in ((P1, FULL1), (P2, FULL2)):
                fig = f[(f"instagram {label}", did)]
                self.assertEqual((fig.value, fig.unit, fig.window), (full[key], "count",
                                                                     "lifetime"))
            total = f[(f"instagram {label}, total of 2 posts", "instagram")]
            self.assertEqual((total.value, total.unit, total.window),
                             (FULL1[key] + FULL2[key], "count", "lifetime"))
        # valid at the measurement time Instagram's reading gives, and says it
        self.assertEqual(ig.valid_at, datetime(2026, 9, 26, 11, tzinfo=UTC).timestamp())
        self.assertEqual(ig.payload["measured_at"], "2026-09-26T11:00:00Z")
        self.assertEqual(ig.payload["window"], "lifetime")
        w = kinds["traffic.working"]
        self.assertIn(f"best Instagram post by reach: {P2} (480)", w.payload["summary"])
        best = self.figs(w)[("instagram reach", P2)]
        self.assertEqual((best.value, best.unit, best.window), (480, "count", "lifetime"))

    def test_an_unavailable_metric_has_no_figure_and_totals_say_how_many(self) -> None:
        self.instagram([ig_post(P1, M1, 1.0), ig_post(P2, M2, 2.0)])
        partial = {k: v for k, v in FULL1.items() if k not in ("saved", "views")}
        self.hands.outcome = answer([row(M1, partial), row(M2, FULL2)])
        ig = self.run_at()["traffic.instagram"]
        f = self.figs(ig)
        self.assertNotIn(("instagram saves", P1), f)            # never a zero
        self.assertNotIn(("instagram views", P1), f)
        self.assertEqual(f[("instagram saves", P2)].value, 11)
        self.assertEqual(ig.payload["metrics_unavailable"], {P1: ["saved", "views"]})
        saves = ("instagram saves, total of 1 posts (of 2; the others have no saves "
                 "figure)")
        self.assertEqual(f[(saves, "instagram")].value, 11)
        self.assertEqual(f[("instagram reach, total of 2 posts", "instagram")].value, 600)
        self.assertFalse(any(x.value == 0 for x in ig.figures))

    def test_no_data_is_no_instagram_insights_yet_never_zero(self) -> None:
        self.instagram([ig_post(P1, M1, 1.0), ig_post(P2, M2, 2.0)])
        self.hands.outcome = answer([row(M1, {}), row(M2, {})])
        kinds = self.run_at()
        ig = kinds["traffic.instagram"]
        self.assertEqual(ig.figures, ())
        self.assertEqual(ig.payload["summary"], "no Instagram insights yet")
        self.assertIn("no Instagram insights yet", kinds["traffic.working"].payload["summary"])
        self.assertFalse(any("instagram" in x.measures
                             for x in kinds["traffic.working"].figures))

    def test_no_published_post_is_no_insights_yet_and_asks_nothing(self) -> None:
        self.instagram([])
        kinds = self.run_at(traffic=EMPTY_TRAFFIC)
        self.assertEqual(self.hands.jobs, [])
        self.assertEqual(kinds["traffic.instagram"].payload["summary"],
                         "no Instagram insights yet: no published Instagram posts yet")
        self.assertEqual(kinds["traffic.working"].payload["summary"],
                         "no traffic data yet; no Instagram insights yet: no published "
                         "Instagram posts yet")

    def test_a_rejected_token_is_said_with_its_hint_and_no_figure(self) -> None:
        self.instagram([ig_post(P1, M1, 1.0)])
        self.hands.outcome = JobOutcome("failed", INSIGHTS, task_id="t", error=TOKEN_REJECTED,
                                        error_type="")
        kinds = self.run_at()
        ig = kinds["traffic.instagram"]
        self.assertEqual(ig.figures, ())
        self.assertIn("setup-instagram.ps1", ig.payload["why_no_figures"])
        self.assertIn("Instagram insights unavailable: the last read failed: the Instagram "
                      "token was rejected", kinds["traffic.working"].payload["summary"])

    def test_insights_are_read_at_most_once_per_6_hours(self) -> None:
        self.instagram([ig_post(P1, M1, 1.0), ig_post(P2, M2, 2.0)])
        first = self.run_at(T0)["traffic.instagram"]
        again = self.run_at(T0 + 6 * 3600 - 1)["traffic.instagram"]
        self.assertEqual(len(self.hands.jobs), 1)               # the cached reading
        self.assertEqual(again.figures, first.figures)
        self.assertEqual(again.valid_at, first.valid_at)        # still its measurement time
        self.assertIs(again.provenance["asked_this_run"], False)
        self.run_at(T0 + 6 * 3600)
        self.assertEqual(len(self.hands.jobs), 2)

    def test_a_failed_refresh_keeps_the_last_reading_and_says_so(self) -> None:
        self.instagram([ig_post(P1, M1, 1.0)])
        first = self.run_at(T0)["traffic.instagram"]
        self.hands.outcome = JobOutcome("unreachable", INSIGHTS, error="Pionir is down")
        later = self.run_at(T0 + 7 * 3600)["traffic.instagram"]
        self.assertEqual(later.figures, first.figures)
        self.assertEqual(later.payload["measured_at"], "2026-09-26T11:00:00Z")
        self.assertIn("the last read failed: Pionir is down", later.payload["note"])

    def test_a_post_without_a_media_id_is_matched_by_permalink_from_recent(self) -> None:
        self.instagram([ig_post(P1, None, 1.0), ig_post(P2, M1, 2.0)])
        self.hands.outcome = answer([row(M2, FULL2), row(M1, FULL1), row(M3, {"reach": 1})])
        ig = self.run_at()["traffic.instagram"]
        (job,) = self.hands.jobs
        self.assertEqual(job.payload, {"recent": 25})
        f = self.figs(ig)
        self.assertEqual(f[("instagram reach", P1)].value, 480)   # by permalink (M2's)
        self.assertEqual(f[("instagram reach", P2)].value, 120)   # by media id
        self.assertEqual(ig.payload["not_in_reading"], [])

    def test_a_post_not_in_the_reading_is_unknown_not_zero(self) -> None:
        self.instagram([ig_post(P1, M1, 1.0), ig_post(P2, M3, 2.0)])
        self.hands.outcome = answer([row(M1, FULL1)])
        ig = self.run_at()["traffic.instagram"]
        self.assertEqual(ig.payload["not_in_reading"], [P2])
        self.assertFalse(any(x.stream == P2 for x in ig.figures))

    def test_no_pionir_in_the_run_asks_nothing_and_says_so(self) -> None:
        self.instagram([ig_post(P1, M1, 1.0)])
        kinds = self.run_at(job=False)
        self.assertEqual(self.hands.jobs, [])
        self.assertEqual(kinds["traffic.instagram"].figures, ())
        self.assertIn("Pionir is not reachable", kinds["traffic.instagram"].payload[
            "why_no_figures"])

    def test_no_instagram_record_adds_no_row(self) -> None:
        kinds = self.run_at()
        self.assertNotIn("traffic.instagram", kinds)
        self.assertEqual(self.hands.jobs, [])

    def test_the_catalogue_wires_it(self) -> None:
        self.assertEqual(self.worker.instagram_worker, "posting.instagram")
        division = default_registry().division("posting")
        self.assertEqual(division.brief_quota["traffic.instagram"], 1)
        self.assertIn("traffic.instagram", division.leader_notes)
        self.assertEqual(insights_module.REFRESH_SECONDS, 21600)


class InstagramWorkerKeepsTheMediaIdTests(unittest.TestCase):
    def test_a_published_post_records_its_media_id(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            worker = default_registry().require("posting.instagram")
            self.assertIsInstance(worker, InstagramWorker)
            rec = worker._blank()
            rec["posts"].append({"draft_id": P1, "headline": "h", "topic": "t",
                                 "status": "pending_approval", "approval_id": "ap-1",
                                 "submitted_at": T0 - 10})
            worker.save(state, rec)
            hands = FakeHands()
            hands.approvals["ap-1"] = {
                "id": "ap-1", "status": "approved",
                "result": {"ok": True, "agent_id": "instagram", "task_id": "t-2",
                           "result": {"ok": True, "media_id": M1, "permalink": LINK[M1]}}}
            worker.run(WorkContext(now=T0, http=None, secrets_dir=state, words=None,
                                   job=hands.job, approval=hands.approval, state_dir=state))
            (post,) = [p for p in worker.load(state)["posts"] if p["draft_id"] == P1]
        self.assertEqual((post["status"], post["media_id"]), ("published", M1))

    def test_only_an_all_digit_media_id_is_kept(self) -> None:
        from pionir.crew.instagram import media_id
        self.assertEqual(media_id({"media_id": M1}), M1)
        self.assertEqual(media_id({"media_id": 17}), "17")
        for bad in ("../x", "", "１２", True, None, "9" * 41):
            self.assertIsNone(media_id({"media_id": bad}))


if __name__ == "__main__":
    unittest.main()
