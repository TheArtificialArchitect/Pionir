"""``marketplaces.watcher``: what the published products did - read only - and the report.

One run, for what the packager's record says went out:

- **Apify**: ``Job("apify.stats", {actors})`` (READ_ONLY in Pionir): each Actor's users (all
  time, 30 days), runs (all time, last 7 days by status) and its Store rating and review count.
  Earnings are not in Apify's public API: reported as UNKNOWN, never as zero.
- **Chrome**: ``Job("chrome.status", {items})`` (READ_ONLY): the published and submitted
  revisions, taken down, warned; and the item's PUBLIC page (robots.txt obeyed) for its user
  count, rating and number of ratings.
- **Ratings moved**: when an item's rating count rises (or its rating falls) since the last
  look, ONE card tells the owner, with the item's page. Neither store has an API that gives a
  review's text or posts a reply, and the Chrome Web Store's robots.txt disallows its review
  pages: so no reply is drafted from text nobody may read - the owner reads and answers on the
  store himself.
- ``performance.json``: what sold, per store, for the scouts (a niche like one that already
  has users is worth a little more).

Rows: ``market.stats`` per item (typed figures) and ``market.report`` - the division's report
the Marketplaces leader reads. A store whose credential is missing is NOT CONFIGURED, said in
the report; nothing is guessed.
"""
from __future__ import annotations

import time

from ..blog import _clip, _Unreadable, read_record, record_path, save_record
from ..figures import Figure
from ..hands import Job
from ..log import log
from ..result import Ok, Result
from ..worker import ErrorKind, WorkContext, make_output, never_raises
from ..workers import _Base
from . import paths, providers
from .packager import APIFY_TOKEN, CHROME_CREDENTIALS, missing_credentials

STATS = "apify.stats"
STATUS = "chrome.status"
CARD = "builds.card"
CARD_KIND = "market_reviews"
PACKAGER = "marketplaces.packager"


class WatcherWorker(_Base):
    record_what = "the Marketplaces watcher's record of what the published products did"

    def __init__(self, spec, *, packager: str = PACKAGER, pause: float = 3.0) -> None:
        super().__init__(spec)
        self.packager = packager
        self.pause = float(pause)
        self.sleep = time.sleep                 # injectable: tests never wait

    def readiness(self, secrets_dir) -> str | None:
        missing = missing_credentials(secrets_dir)
        return ("NOT CONFIGURED: " + "; ".join(missing)) if missing else None

    @never_raises()
    def run(self, ctx: WorkContext) -> Result:
        root = paths.root_for(ctx.builds_dir)
        if root is None or ctx.state_dir is None:
            return self._err(ErrorKind.NOT_CONFIGURED, "no Builds folder or state dir",
                             retryable=False)
        try:
            pack = read_record(ctx.state_dir, self.packager) or {}
            rec = read_record(ctx.state_dir, self.worker_id) or {}
        except _Unreadable as exc:
            return self._err(ErrorKind.MALFORMED, f"a record it reads is unreadable ({exc})",
                             retryable=False)
        rec.setdefault("seen", {})
        rec.setdefault("cards", {})
        drafts = [d for d in (pack.get("drafts") or {}).values() if isinstance(d, dict)]
        actors = sorted(d["slug"] for d in drafts if d.get("market") == "apify"
                        and d.get("status") == "published")
        try:
            items_map = paths.chrome_items(root)
        except paths.Unreadable as exc:
            log.warning("%s: %s", self.worker_id, exc)
            items_map = {}
        chrome = sorted((d["slug"], items_map[d["slug"]]) for d in drafts
                        if d.get("market") == "chrome" and d["slug"] in items_map
                        and d.get("status") in ("submitted", "published"))
        events: list = []
        notes: list = []
        perf: dict = {"apify": {"items": []}, "chrome": {"items": []}}
        if actors:
            self._apify(ctx, rec, actors, events, notes, perf)
        if chrome:
            self._chrome(ctx, rec, chrome, events, notes, perf)
        try:
            paths.write_json(root / paths.PERFORMANCE, {**perf, "at": ctx.now})
        except OSError as exc:
            log.warning("%s: could not write %s: %s", self.worker_id, paths.PERFORMANCE, exc)
        save_record(record_path(ctx.state_dir, self.worker_id), rec)
        missing = missing_credentials(ctx.secrets_dir)
        if not actors and not chrome and len(missing) == 2:
            return self._err(ErrorKind.NOT_CONFIGURED, "nothing published to watch, and no "
                             "store credential yet: " + "; ".join(missing), retryable=False)
        return Ok((*events, self._report(ctx, drafts, perf, notes, missing)))

    # ---- Apify ------------------------------------------------------------------------------
    def _apify(self, ctx, rec, actors, events, notes, perf) -> None:
        if not (ctx.secrets_dir / APIFY_TOKEN).is_file():
            notes.append(r"Apify: NOT CONFIGURED (no token; run tools\setup-apify.ps1)")
            return
        if ctx.job is None:
            return
        out = ctx.job(Job(STATS, {"actors": actors[:20]}, what="read the Actors' figures"))
        result = out.result if isinstance(out.result, dict) else {}
        if out.status != "done" or result.get("ok") is not True:
            notes.append(f"Apify: UNAVAILABLE ({_clip(out.error or result.get('error'), 160)})")
            return
        for a in result.get("actors") or []:
            if not isinstance(a, dict) or not a.get("found"):
                continue
            figs = []
            for key, label, window in (("users_30d", "users", "last30"),
                                       ("users_total", "users", "all_time"),
                                       ("runs_total", "runs", "all_time"),
                                       ("reviews", "Store reviews", "all_time")):
                v = a.get(key)
                if isinstance(v, int) and not isinstance(v, bool):
                    figs.append(Figure(v, "count", label, stream=a["name"], window=window))
            runs = a.get("runs_7d") if isinstance(a.get("runs_7d"), dict) else None
            if runs is not None:
                figs.append(Figure(sum(v for v in runs.values() if isinstance(v, int)),
                                   "count", "runs", stream=a["name"], window="last7"))
            rating = a.get("rating")
            if isinstance(rating, (int, float)) and not isinstance(rating, bool):
                figs.append(Figure(round(float(rating) * 20, 1), "percent",
                                   "Store rating (of 5 stars, as percent)", stream=a["name"],
                                   window="now"))
            events.append(self._event(ctx, "market.stats", {
                "market": "apify", "item": a["name"], "url": a.get("url"),
                "runs_7d": runs, "earnings": "UNKNOWN (not in Apify's public API)"}, figs))
            perf["apify"]["items"].append({"slug": a["name"], "title": a["name"].replace("-", " "),
                                           "users": a.get("users_30d")})
            self._ratings(ctx, rec, "apify", a["name"], a.get("url"), a.get("reviews"),
                          rating, events)

    # ---- Chrome -----------------------------------------------------------------------------------
    def _chrome(self, ctx, rec, chrome, events, notes, perf) -> None:
        status = {}
        if not (ctx.secrets_dir / CHROME_CREDENTIALS).is_file():
            notes.append(r"Chrome: NOT CONFIGURED (no credentials; run "
                         r"tools\setup-chrome-webstore.ps1)")
        elif ctx.job is not None:
            out = ctx.job(Job(STATUS, {"items": [i for _s, i in chrome][:20]},
                              what="read the extensions' Chrome Web Store status"))
            result = out.result if isinstance(out.result, dict) else {}
            if out.status == "done" and result.get("ok") is True:
                status = {i.get("item_id"): i for i in result.get("items") or []
                          if isinstance(i, dict)}
            else:
                notes.append(f"Chrome: UNAVAILABLE ({_clip(out.error or result.get('error'), 160)})")
        reader = providers.Reader(ctx.http, pause=self.pause, sleep=self.sleep)
        for slug, item in chrome:
            url = f"{providers.CWS}/detail/{item}"
            public = {}
            try:
                public = providers.cws_detail(reader.get_text(url))
            except providers.ProviderError as exc:
                notes.append(f"Chrome {slug}: its public page could not be read ({_clip(exc, 120)})")
            st = status.get(item) or {}
            figs = []
            if isinstance(public.get("users"), int):
                figs.append(Figure(public["users"], "count", "users", stream=slug, window="now"))
            if isinstance(public.get("ratings"), int):
                figs.append(Figure(public["ratings"], "count", "ratings", stream=slug,
                                   window="all_time"))
            if isinstance(public.get("rating"), float):
                figs.append(Figure(round(public["rating"] * 20, 1), "percent",
                                   "rating (of 5 stars, as percent)", stream=slug, window="now"))
            events.append(self._event(ctx, "market.stats", {
                "market": "chrome", "item": slug, "url": url,
                "published": st.get("published"), "submitted": st.get("submitted"),
                "taken_down": st.get("taken_down"), "warned": st.get("warned")}, figs))
            perf["chrome"]["items"].append({"slug": slug, "title": public.get("title") or slug,
                                            "users": public.get("users")})
            if st.get("taken_down") or st.get("warned"):
                self._post(ctx, rec, f"market:chrome:{slug}:warned:{bool(st.get('taken_down'))}",
                           f"Chrome Web Store flagged {slug}",
                           f"The Chrome Web Store reports **{slug}** as "
                           f"{'TAKEN DOWN' if st.get('taken_down') else 'warned'}. Open the "
                           f"developer dashboard for the reason: {url}")
            self._ratings(ctx, rec, "chrome", slug, url, public.get("ratings"),
                          public.get("rating"), events)

    # ---- ratings ---------------------------------------------------------------------------------
    def _ratings(self, ctx, rec, market, item, url, count, rating, events) -> None:
        if not isinstance(count, int) or isinstance(count, bool):
            return
        key = f"{market}:{item}"
        before = rec["seen"].get(key) or {}
        rec["seen"][key] = {"count": count, "rating": rating, "at": ctx.now}
        old_count = before.get("count")
        if not isinstance(old_count, int) or count <= old_count:
            if not (isinstance(rating, (int, float)) and isinstance(before.get("rating"),
                                                                     (int, float))
                    and rating < before["rating"]):
                return
        new = count - old_count if isinstance(old_count, int) else 0
        events.append(self._event(ctx, "market.ratings_moved", {
            "market": market, "item": item, "new_ratings": new, "rating": rating,
            "rating_before": before.get("rating")}))
        self._post(ctx, rec, f"market:{market}:{item}:ratings:{count}",
                   f"New ratings on {item}",
                   f"**{item}** ({market}) has {count} ratings now ({new:+d} since the last "
                   f"look); its rating is {rating} (was {before.get('rating')}).\n\n"
                   "Neither store gives a review's text or a reply through its API (and the "
                   "Chrome Web Store's robots.txt disallows its review pages), so nothing was "
                   f"read or drafted: read and answer them on the store - {url}")

    def _post(self, ctx, rec, key: str, title: str, body: str) -> None:
        if rec["cards"].get(key) or ctx.job is None:
            return
        out = ctx.job(Job(CARD, {"key": key[:120], "kind": CARD_KIND, "title": title[:120],
                                 "body": body[:11000], "replies": False},
                          what="post a Marketplaces ratings card to the owner"))
        result = out.result if isinstance(out.result, dict) else {}
        if out.status == "done" and result.get("ok") is not False:
            rec["cards"][key] = ctx.now

    # ---- the report ---------------------------------------------------------------------------------
    def _event(self, ctx, kind: str, payload: dict, figures=()):
        return make_output(self, kind=kind, valid_at=ctx.now, observed_at=ctx.now,
                           payload=payload, figures=figures, entities=self.entities,
                           provenance={"source": "real", "provider": self.provider,
                                       "record": self.record_what})

    def _report(self, ctx, drafts: list, perf: dict, notes: list, missing: list):
        def n(market, *states) -> int:
            return sum(1 for d in drafts if d.get("market") == market
                       and d.get("status") in states)

        figures = []
        for market in paths.MARKETS:
            figures.append(Figure(n(market, "published", "submitted", "pack_sent"), "count",
                                  f"{market} products out of the door", window="all_time"))
        for market in ("apify", "chrome"):
            users = [i.get("users") for i in perf[market]["items"]
                     if isinstance(i.get("users"), int)]
            if users:
                figures.append(Figure(sum(users), "count", f"{market} users of our products",
                                      stream=market, window="now"))
        return make_output(self, kind="market.report", valid_at=ctx.now, observed_at=ctx.now,
                           payload={"notes": notes[:8], "missing_credentials": missing,
                                    "earnings": "UNKNOWN: neither store reports earnings "
                                                "through its public API; Apify pays monthly "
                                                "(PayPal, $20 minimum), ExtensionPay through "
                                                "Stripe"},
                           figures=figures, entities=self.entities,
                           provenance={"source": "real", "provider": self.provider,
                                       "record": self.record_what})
