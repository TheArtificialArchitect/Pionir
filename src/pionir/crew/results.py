"""The results loop: what the posts actually did, from the site's own counter.

``TrafficWorker`` (``posting.results``) reads the same ``GET /dash/api.json`` as the ledger,
with the same read-only token (``DashReader``), and takes its ``traffic`` object - Scrooge's
``trafficReport()`` (worker/src/traffic.ts): the last 30 days, UTC, today included.

Honest numbers only, the ledger's rule:

- **No ``traffic`` key** (a Scrooge from before it counted visits) is ``NOT_CONFIGURED``,
  loudly: the traffic is UNKNOWN, never zero.
- **No traffic at all** (no views, no rows) is one row saying "no traffic data yet" and no
  figures: an empty counter is not a trend, and not a zero to compare anything against.
- **A capped list is not a complete one.** Scrooge returns the top 20 rows of each list.
  When a list is full, a post missing from it has UNKNOWN views (listed in the payload,
  never given a figure), and a sum over it is a lower bound and says so in what it
  measures ("at least").
- Every figure is typed (``count`` / ``usd_cents``), names its window (``last30``), and
  the row it sits in names its source (the ``traffic`` field it came from).

How a campaign is matched to a post: the blog worker tags every link in a post with
``utm_source=blog&utm_medium=referral&utm_campaign=<utm_campaign(draft_id)>``, and Scrooge
stores a visit's campaign as ``source/medium/campaign``, each part sanitised by
``utm_part`` (``campaignOf``). So the clicks a post sent to the product pages are the
``top_campaigns`` rows whose source is ``blog`` and whose campaign part is that post's
``utm_campaign`` (``_post_campaign``/``_campaign_rows``); its dev.to visits are the rows with
source ``devto`` and the same campaign (devto.py keeps it); its page views are the
``top_paths`` row ``/blog/<slug>``.

The rows, per run: ``traffic.site`` (site-wide and per-channel figures), ``traffic.posts``
(each published post's figures), ``traffic.sales`` (sales and net revenue per campaign,
where Scrooge attributed any), and ``traffic.working`` - the plain "what's working"
summary the posting leader reads first, built only from the figures above.
"""
from __future__ import annotations

import re

from .blog import _Unreadable, published_posts, read_record
from .contentcheck import utm_campaign
from .figures import Figure
from .log import log
from .result import Err, Ok, Result
from .worker import ErrorKind, WorkContext, make_output, never_raises
from .workers import DashReader, _Malformed

BLOG_WORKER = "posting.blog"
WINDOW = "last30"
LIST_CAP = 20              # Scrooge's LIMIT on every top_* and sales_* list (traffic.ts)
TOO_EARLY_VIEWS = 20       # a post with fewer views than this is too early to tell
TOP_N = 3                  # posts named in the "what's working" summary
MAX_POSTS = 12             # posts given figures in one traffic.posts row
NO_DATA = "no traffic data yet"
SEARCH_HOSTS = re.compile(r"(?:^|\.)(?:google\.[a-z.]+|bing\.com|duckduckgo\.com|"
                          r"search\.yahoo\.com|yandex\.[a-z.]+|baidu\.com|ecosia\.org|"
                          r"search\.brave\.com|startpage\.com|qwant\.com)$")
_NOT_REFERRERS = frozenset({"direct", "internal"})


def utm_part(value) -> str:
    """Scrooge's ``utmPart``: lower case, only ``a-z 0-9 . _ -``, at most 40 characters."""
    return re.sub(r"[^a-z0-9._-]", "", str(value or "").lower())[:40]


def split_campaign(campaign: str) -> tuple | None:
    """``source/medium/campaign`` -> the three parts, or None (``none``, or not that
    shape)."""
    parts = campaign.split("/")
    return tuple(parts) if len(parts) == 3 else None


def _post_campaign(post: dict) -> str:
    """The campaign part a post's links carry: ``utm_campaign`` of its draft id, as
    Scrooge stores it."""
    return utm_part(utm_campaign(post.get("draft_id") or ""))


def _count(row: dict, key: str, where: str) -> int:
    """A whole number the report must carry: missing or anything else is MALFORMED, never
    a default zero. Only ``net`` (cents, after refunds) may be negative."""
    v = row.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not float(v).is_integer() \
            or (v < 0 and key != "net"):
        raise _Malformed(f"{where}.{key} is {v!r}, not a whole count")
    return int(v)


def _rows(traffic: dict, key: str, label: str, *, required: bool = True,
          counts: tuple = ("n",)) -> list | None:
    """One of the report's lists, each row checked: ``[(label value, {count: int})]``.
    A required list that is missing is MALFORMED; an optional one is None (unknown)."""
    rows = traffic.get(key)
    if rows is None and not required:
        return None
    if not isinstance(rows, list):
        raise _Malformed(f"traffic.{key} is not a list")
    out = []
    for i, row in enumerate(rows[:200]):
        where = f"traffic.{key}[{i}]"
        if not isinstance(row, dict) or not isinstance(row.get(label), str):
            raise _Malformed(f"{where} is {row!r}")
        out.append((row[label], {c: _count(row, c, where) for c in counts}))
    return out


def _campaign_rows(campaigns: list, *, source: str, campaign: str | None = None,
                   medium: str | None = None) -> int:
    """The sum of the campaign rows with this source (and medium, and campaign part)."""
    total = 0
    for name, c in campaigns:
        parts = split_campaign(name)
        if parts is None or parts[0] != source:
            continue
        if medium is not None and parts[1] != medium:
            continue
        if campaign is not None and parts[2] != campaign:
            continue
        total += c["n"]
    return total


def _post_rows(campaigns: list, capped: bool, *, source: str, campaign: str) -> int | None:
    """One post's count on one source: exact when its row is listed, 0 when the list is
    complete and it is not, and None (UNKNOWN) when the list is capped and it is not."""
    matched = any((parts := split_campaign(name)) is not None and parts[0] == source
                  and parts[2] == campaign for name, _c in campaigns)
    if not matched:
        return None if capped else 0
    return _campaign_rows(campaigns, source=source, campaign=campaign)


def _capped(rows: list | None) -> bool:
    return rows is not None and len(rows) >= LIST_CAP


def _at_least(measures: str, capped: bool) -> str:
    return f"{measures} (at least; the list is capped at {LIST_CAP})" if capped else measures


class TrafficWorker(DashReader):
    """``posting.results``: the site's traffic, per post and per channel, last 30 days."""

    unknown = "traffic"

    def __init__(self, spec, *, url: str, token_file: str,
                 blog_worker: str = BLOG_WORKER) -> None:
        super().__init__(spec, url=url, token_file=token_file)
        self.blog_worker = blog_worker

    @never_raises()
    def run(self, ctx: WorkContext) -> Result:
        got = self._read_dash(ctx)
        if isinstance(got, Err):
            return got
        doc, resp = got.value
        if "traffic" not in doc or doc["traffic"] is None:
            return self._err(ErrorKind.NOT_CONFIGURED,
                             "the dash has no traffic report (a Scrooge from before it "
                             "counted page views); traffic is UNKNOWN, not zero. Deploy "
                             "Scrooge with worker/src/traffic.ts", retryable=False)
        traffic = doc["traffic"]
        posts, record_note = self._posts(ctx)
        try:
            if not isinstance(traffic, dict):
                raise _Malformed("traffic is not an object")
            outputs = self._outputs(ctx, traffic, posts, record_note, resp)
        except (TypeError, ValueError, KeyError) as exc:
            return self._err(ErrorKind.MALFORMED, f"{type(exc).__name__}: {exc}")
        return Ok(tuple(outputs))

    def _posts(self, ctx: WorkContext) -> tuple:
        """(the blog's published posts, oldest first, a note about the record or "")."""
        if ctx.state_dir is None:
            return [], "no state dir: the blog's posts are unknown"
        try:
            rec = read_record(ctx.state_dir, self.blog_worker)
        except _Unreadable as exc:
            log.warning("%s: the blog record is unreadable: %s", self.worker_id, exc)
            return [], "the blog record is unreadable: its posts are unknown"
        if rec is None:
            return [], "no blog posts yet"
        posts = [p for p in published_posts(rec)
                 if isinstance(p.get("slug"), str) and isinstance(p.get("draft_id"), str)]
        return posts, "" if posts else "no published blog posts yet"

    # ---- the figures -------------------------------------------------------------------------
    def _outputs(self, ctx: WorkContext, t: dict, posts: list, record_note: str,
                 resp) -> list:
        since = t.get("since")
        if not isinstance(since, str):
            raise _Malformed(f"traffic.since is {since!r}")
        views = _count(t, "views", "traffic")
        paths = _rows(t, "top_paths", "path")
        campaigns = _rows(t, "top_campaigns", "campaign")
        referrers = _rows(t, "top_referrers", "ref_host", required=False)
        sales = _rows(t, "sales_by_campaign", "campaign", required=False,
                      counts=("sales", "net"))
        source = f"Scrooge /dash/api.json traffic, last 30 days since {since}"
        prov = {**self._provenance(resp), "window": WINDOW, "since": since}

        def out(kind: str, payload: dict, figures: list, *, sourced: bool = True):
            # the source travels in provenance always, and in the details a leader reads
            # except on the summary row, whose every number is already a sourced figure
            return make_output(self, kind=kind, valid_at=ctx.now, observed_at=ctx.now,
                               payload={"source": source, **payload} if sourced else payload,
                               figures=figures, entities=self.entities,
                               provenance={**prov, "source_detail": source})

        if views == 0 and not paths and not campaigns and not referrers and not sales:
            return [out("traffic.working", {"summary": NO_DATA, "posts": record_note or
                                            f"{len(posts)} published blog posts"}, [])]

        path_views = {p: c["n"] for p, c in paths}
        paths_capped = _capped(paths)
        camps_capped = _capped(campaigns)

        # -- site-wide and per channel --
        blog_posts_views = sum(n for p, n in path_views.items() if p.startswith("/blog/"))
        blog_clicks = _campaign_rows(campaigns, source="blog")
        ig_bio = _campaign_rows(campaigns, source="instagram", medium="bio")
        devto = _campaign_rows(campaigns, source="devto")
        site = [Figure(views, "count", "site page views", "all", WINDOW)]
        if "/blog" in path_views or not paths_capped:
            site.append(Figure(path_views.get("/blog", 0), "count", "blog index page views",
                               "/blog", WINDOW))
        site += [
            Figure(blog_posts_views, "count", _at_least("blog post page views", paths_capped),
                   "/blog/*", WINDOW),
            Figure(blog_clicks, "count",
                   _at_least("clicks from blog posts to product pages", camps_capped),
                   "blog", WINDOW),
            Figure(ig_bio, "count", _at_least("instagram bio clicks", camps_capped),
                   "instagram/bio", WINDOW),
            Figure(devto, "count", _at_least("dev.to visits", camps_capped), "devto", WINDOW),
        ]
        channels = {"blog links": blog_clicks, "instagram bio": ig_bio, "dev.to": devto}
        ref_list: list = []
        if referrers is not None:
            refs_capped = _capped(referrers)
            search = sum(c["n"] for h, c in referrers if SEARCH_HOSTS.search(h))
            site.append(Figure(search, "count", _at_least("visits from search engines",
                                                          refs_capped), "search", WINDOW))
            channels["search"] = search
            for host, c in referrers:
                if host in _NOT_REFERRERS or SEARCH_HOSTS.search(host) or len(ref_list) >= 5:
                    continue
                site.append(Figure(c["n"], "count", "visits referred", host, WINDOW))
                ref_list.append(host)
        rows = [out("traffic.site", {
            "lists_capped": [k for k, r in (("top_paths", paths), ("top_campaigns", campaigns),
                                            ("top_referrers", referrers)) if _capped(r)],
            "referrers": ref_list,
            "referrers_reported": referrers is not None}, site)]

        # -- each published post --
        stats = []
        for p in posts:
            path = f"/blog/{p['slug']}"
            camp = _post_campaign(p)
            s = {"slug": p["slug"], "campaign": camp,
                 "views": path_views.get(path, None if paths_capped else 0),
                 "clicks": _post_rows(campaigns, camps_capped, source="blog", campaign=camp),
                 "devto": _post_rows(campaigns, camps_capped, source="devto", campaign=camp),
                 "sales": None, "net": None}
            for name, c in sales or []:
                parts = split_campaign(name)
                if parts is not None and parts[2] == camp and camp:
                    s["sales"] = (s["sales"] or 0) + c["sales"]
                    s["net"] = (s["net"] or 0) + c["net"]
            stats.append(s)
        ranked = sorted(stats, key=lambda s: (-(s["views"] or 0), -(s["clicks"] or 0),
                                              s["slug"]))
        post_figs = []
        for s in ranked[:MAX_POSTS]:
            if s["views"] is not None:
                post_figs.append(Figure(s["views"], "count", "post page views", s["slug"],
                                        WINDOW))
            if s["clicks"] is not None:
                post_figs.append(Figure(s["clicks"], "count", "post clicks to product pages",
                                        s["slug"], WINDOW))
            if s["devto"]:
                post_figs.append(Figure(s["devto"], "count", "post dev.to visits", s["slug"],
                                        WINDOW))
            if s["sales"] is not None:
                post_figs.append(Figure(s["sales"], "count", "post sales", s["slug"], WINDOW))
                post_figs.append(Figure(s["net"], "usd_cents", "post net revenue", s["slug"],
                                        WINDOW))
        if posts:
            rows.append(out("traffic.posts", {
                "published_posts": len(posts),
                "listed": len(ranked[:MAX_POSTS]),
                "too_early_to_tell": [s["slug"] for s in ranked
                                      if s["views"] is not None and s["views"] < TOO_EARLY_VIEWS],
                "views_unknown_not_in_top_paths": [s["slug"] for s in ranked
                                                   if s["views"] is None],
                "clicks_unknown_not_in_top_campaigns": [s["slug"] for s in ranked
                                                        if s["clicks"] is None],
            }, post_figs))

        # -- sales per campaign, where Scrooge attributed any --
        if sales is not None:
            sale_figs = [Figure(sum(c["sales"] for _n, c in sales), "count",
                                _at_least("sales in the window", _capped(sales)), "all",
                                WINDOW)]
            for name, c in sales:
                if name == "none":
                    continue
                sale_figs.append(Figure(c["sales"], "count", "sales", name, WINDOW))
                sale_figs.append(Figure(c["net"], "usd_cents", "net revenue", name, WINDOW))
            rows.append(out("traffic.sales", {"campaigns": len(sales)}, sale_figs))

        rows.append(self._working(out, views, ranked, channels, record_note))
        return rows

    @staticmethod
    def _working(out, views: int, ranked: list, channels: dict, record_note: str):
        """The plain summary: the top posts by views and by clicks, and the channels that
        sent anyone - each only from a real, non-zero number, and nothing more."""
        by_views = [s for s in ranked if s["views"]][:TOP_N]
        by_clicks = sorted((s for s in ranked if s["clicks"]),
                           key=lambda s: (-s["clicks"], s["slug"]))[:TOP_N]
        sent = [(k, n) for k, n in channels.items() if n > 0]
        parts = [f"{views} page views in the last 30 days"]
        if record_note:
            parts.append(record_note)
        if by_views:
            parts.append("most viewed posts: " + ", ".join(
                f"{s['slug']} ({s['views']})" for s in by_views))
        elif ranked:
            parts.append("no published post has a recorded view yet")
        if by_clicks:
            parts.append("most clicks to product pages: " + ", ".join(
                f"{s['slug']} ({s['clicks']})" for s in by_clicks))
        elif ranked:
            parts.append("no post has sent a click to a product page yet")
        if ranked and all(s["views"] is None or s["views"] < TOO_EARLY_VIEWS for s in ranked):
            parts.append(f"every post is too early to tell (under {TOO_EARLY_VIEWS} views)")
        parts.append("visitors came from: " + ", ".join(f"{k} ({n})" for k, n in sent)
                     if sent else "no tracked channel sent a visitor yet")
        figures = [Figure(s["views"], "count", "post page views", s["slug"], WINDOW)
                   for s in by_views]
        figures += [Figure(s["clicks"], "count", "post clicks to product pages", s["slug"],
                           WINDOW) for s in by_clicks]
        figures += [Figure(n, "count", f"visits from {k}", k, WINDOW) for k, n in sent]
        # "summary" sorts first, so the brief (which clips a row's details) shows it first;
        # every number in it is also a figure below, which the brief shows in full
        return out("traffic.working", {
            "summary": "; ".join(parts),
            "top_by_views": [s["slug"] for s in by_views],
            "top_by_clicks": [s["slug"] for s in by_clicks],
            "visitors_from": [k for k, _n in sent]}, figures, sourced=False)
