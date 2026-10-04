"""``products.demand``: what to build and sell next, decided from measured demand only.

Once a day this worker reads the same ``GET /dash/api.json`` as the ledger (``DashReader``,
the read-only token) and scores every candidate the crew could put work into:

- **api** - each paid API product on api.dokaz.net (Scrooge ``worker/src/products``);
- **tool** - each free tool on dokazindustries.com/tools (doxa ``site/src/lib/free-tools.ts``),
  each tied to the API guide it links to (its score carries that API's usage too);
- **topic** - each blog seed topic (blog.SEEDS), with its published post's figures if it has one;
- **build** - each product WAITING in the Builds backlog (builds/backlog.py, read only);
- **apiidea** - each queued idea in the API builder's backlog (apibuild/backlog.py, read only).

**Measured signals only.** From ``usage`` (Scrooge ``usageLast(env, 14)``: per UTC day and API
product - calls, 5xx errors and distinct callers, internal keys excluded) and from ``traffic``
(``trafficReport()``, the last 30 days: page views by path, campaigns, sales by campaign; the
same object posting.results reads). A score is a weighted sum (``INPUTS``) of the inputs it
lists, every one of them a recorded number; the weights only order the candidates.

**UNKNOWN is never zero.** An input that could not be measured this time (a guide missing from
a capped ``top_paths`` list, a dash without ``usage``) has no value and says why; a candidate
with no measured input at all has no score and ranks after every scored one; a score with
any input unknown is marked partial (a lower bound). Signals Scrooge does not record at all
are listed in ``UNAVAILABLE``, never given a number:

- per-tool click-throughs: the tools tag their links ``utm_content=<slug>``, but Scrooge
  stores a visit's campaign as ``source/medium/campaign`` only (traffic.ts ``campaignOf``), so
  only the free tools' TOTAL is measured (campaign ``dokazindustries/referral/free-tools``);
- 429s: a rejected call is counted in ``usage.calls`` (``meter`` runs before the quota check)
  but nothing records that it was rejected; free and paid callers are not split either; and
  usage is metered per product (``/v1/<product>``), not per endpoint. What IS measured is how
  hard callers lean on a product: a day on which the average caller made at least
  ``FREE_PER_DAY`` calls (the anonymous daily cap) is a SATURATED day;
- search referrals per post: referrers are counted site-wide, never per page.

**What it writes.** ``<state_dir>/demand.json`` - the ranking with every input, for the two
hooks that read it (``build_preference`` for the Builds backlog pick, ``product_preference``
for the blog's topic choice; each ignores a file older than ``STALE_AFTER``) - and its own
record ``<state_dir>/products.demand.json`` (the cards it posted). It never writes a backlog:
the owner edits those.

**Owner cards.** When a product shows repeated saturation, or a free tool's guide draws many
views while no paid download covers it, it posts ONE card through Pionir's ``builds.card``
("demand seen: X - suggest Y"): at most one card per ``CARD_EVERY``, never the same suggestion
again within ``REPEAT_AFTER``. A suggestion only - nothing is added, built or sold from it.

It is stage 1 in the catalogue: in a dispatch it runs after the workers whose records it reads,
and never holds a slot on the dokaz host ahead of the site's health check.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .blog import SEEDS, _clip, _Unreadable, published_posts, read_record, record_path, save_record
from .builds import backlog as builds_backlog
from .figures import Figure
from .hands import Job
from .log import log
from .result import Err, Ok, Result
from .results import (
    LIST_CAP,
    SEARCH_HOSTS,
    _campaign_rows,
    _capped,
    _count,
    _post_campaign,
    _post_rows,
    _rows,
    split_campaign,
)
from .worker import ErrorKind, WorkContext, make_output, never_raises
from .workers import DashReader, _Malformed

DEMAND_FILE = "demand.json"
BLOG_WORKER = "posting.blog"
BUILDS_WORKER = "builds.daedalus"
CARD = "builds.card"
CARD_KIND = "demand"
FREE_PER_DAY = 100                 # Scrooge auth.ts FREE_PER_DAY: anonymous calls a day
USAGE_DAYS = 14                    # Scrooge dashboard.ts usageLast(env, 14)
SATURATED_DAYS = 2                 # saturated days in the window that make "repeated"
HIGH_GUIDE_VIEWS = 30              # guide views in 30 days that count as high for a tool
STALE_AFTER = 3 * 86400.0          # a demand.json older than this steers nothing
CARD_EVERY = 86400.0               # at most one card a day
REPEAT_AFTER = 14 * 86400.0        # never the same suggestion twice in 14 days
KEEP_CARDS = 100
TOP_N = 3
FREE_TOOLS_CAMPAIGN = ("dokazindustries", "referral", "free-tools")

# input -> (what it measures, window, weight, divisor): its share of a score is
# weight * (value // divisor). Only these are ever scored.
INPUTS = {
    "api_calls": ("API calls", "last14", 1, 10),
    "api_caller_days": ("API caller-days", "last14", 3, 1),
    "api_saturated_days": ("days the average API caller reached the free daily cap",
                           "last14", 10, 1),
    "guide_views": ("API guide page views", "last30", 1, 1),
    "post_views": ("blog post page views", "last30", 1, 1),
    "post_clicks": ("blog post clicks to product pages", "last30", 5, 1),
}
UNAVAILABLE = {
    "tool_click_through_per_tool": (
        "UNKNOWN: the free tools tag their links utm_content=<slug>, but Scrooge keeps only "
        "source/medium/campaign (traffic.ts campaignOf); only the free tools' total is measured"),
    "rejected_calls_429": (
        "UNKNOWN: a rejected call is counted in usage.calls, but nothing records that it was "
        "rejected; saturated days (average caller at the free daily cap) are measured instead"),
    "free_vs_paid_calls": "UNKNOWN: usage mixes free and paid callers (internal keys excluded)",
    "calls_per_endpoint": "UNKNOWN: Scrooge meters calls per API product, not per endpoint",
    "search_referrals_per_post": "UNKNOWN: referrers are counted site-wide, never per page",
}
KIND_ORDER = ("build", "topic", "tool", "api", "apiidea")

# Scrooge worker/src/products: id -> name. Ties in keyword matching go to the earlier entry,
# so the general converter is last.
APIS = {
    "invoice": "Invoice PDF", "email": "Email Verify", "qr": "QR Code", "barcode": "Barcode",
    "calendar": "Calendar", "image": "Image", "pdf": "Markdown to PDF", "site": "Site Intel",
    "text": "Text AI", "convert": "Convert",
}
# Distinctive words that tie a backlog entry to a product (its slug words count double).
KEYWORDS = {
    "invoice": {"invoice", "invoices", "receipt", "receipts", "estimate", "estimates",
                "billing"},
    "email": {"email", "emails", "mx", "bounce", "disposable"},
    "qr": {"qr", "wifi", "vcard"},
    "barcode": {"barcode", "barcodes", "ean", "ean-13", "upc", "code128"},
    "calendar": {"calendar", "ics", "ical"},
    "image": {"exif", "image", "images", "photo", "photos"},
    "pdf": {"markdown", "md"},
    "site": {"website", "seo", "tech", "stack"},
    "text": {"text", "summary", "summarize", "sentiment", "keywords"},
    "convert": {"xlsx", "excel", "json", "csv"},
}
# Scrooge worker/src/docs.ts: each API guide's product.
GUIDES = {
    "/docs/email-verification-api": "email", "/docs/disposable-email-detection-api": "email",
    "/docs/invoice-pdf-api": "invoice", "/docs/qr-code-api": "qr",
    "/docs/wifi-qr-code-api": "qr", "/docs/vcard-qr-code-api": "qr",
    "/docs/website-technology-detection-api": "site",
    "/docs/extract-contact-info-from-website-api": "site",
    "/docs/text-summarization-api": "text", "/docs/sentiment-analysis-api": "text",
    "/docs/keyword-extraction-api": "text", "/docs/zero-shot-text-classification-api": "text",
    "/docs/rewrite-text-tone-api": "text", "/docs/barcode-generator-api": "barcode",
    "/docs/ean-13-upc-a-barcode-api": "barcode", "/docs/markdown-to-pdf-api": "pdf",
    "/docs/csv-to-json-api": "convert", "/docs/json-to-csv-api": "convert",
    "/docs/json-to-excel-api": "convert", "/docs/ics-calendar-file-api": "calendar",
    "/docs/add-to-calendar-link-api": "calendar", "/docs/remove-exif-metadata-api": "image",
}
# doxa site/src/lib/free-tools.ts: slug -> (name, the API guide it links to).
TOOLS = {
    "qr-code-generator": ("QR code generator", "/docs/qr-code-api"),
    "wifi-qr-code-generator": ("Wi-Fi QR code generator", "/docs/wifi-qr-code-api"),
    "vcard-qr-code-generator": ("vCard QR code generator", "/docs/vcard-qr-code-api"),
    "barcode-generator": ("Barcode generator", "/docs/barcode-generator-api"),
    "ics-file-generator": ("ICS file generator", "/docs/add-to-calendar-link-api"),
    "csv-to-json": ("CSV to JSON", "/docs/csv-to-json-api"),
    "json-to-csv": ("JSON to CSV", "/docs/json-to-csv-api"),
    "json-to-excel": ("JSON to Excel", "/docs/json-to-excel-api"),
    "markdown-to-pdf": ("Markdown to PDF", "/docs/markdown-to-pdf-api"),
    "invoice-generator": ("Invoice generator", "/docs/invoice-pdf-api"),
    "remove-exif-metadata": ("Remove EXIF metadata", "/docs/remove-exif-metadata-api"),
    "email-verifier": ("Email verifier", "/docs/email-verification-api"),
    "website-technology-checker": ("Website technology checker",
                                   "/docs/website-technology-detection-api"),
}


def related_product(slug_words, other_words=()) -> tuple:
    """``(product, strength)``: the API product whose keywords a backlog entry's words hit
    hardest (slug words count twice) and by how much, or ``(None, 0)`` when none is hit."""
    score: dict = {}
    for words, weight in ((slug_words, 2), (other_words, 1)):
        for w in words:
            for pid, keys in KEYWORDS.items():
                if str(w).lower() in keys:
                    score[pid] = score.get(pid, 0) + weight
    if not score:
        return None, 0
    best = max(score.values())
    return next(pid for pid in KEYWORDS if score.get(pid) == best), best


def _words(text: str) -> list:
    return re.findall(r"[a-z0-9]+(?:-[0-9]+)?", str(text or "").lower())


def _inp(value, *, at_least: bool = False, why: str = "") -> dict:
    d = {"value": value}
    if at_least:
        d["at_least"] = True
    if why:
        d["why"] = why
    return d


def score_of(inputs: dict) -> tuple:
    """``(score or None, partial)``: None when no input was measured; partial when some were
    not (the score is then a lower bound)."""
    known = {k: v for k, v in inputs.items() if v.get("value") is not None}
    if not known:
        return None, bool(inputs)
    total = 0
    for k, v in known.items():
        _what, _window, weight, div = INPUTS[k]
        total += weight * (int(v["value"]) // div)
    partial = len(known) < len(inputs) or any(v.get("at_least") for v in known.values())
    return total, partial


def rank(candidates: list) -> list:
    """Scored candidates first, best first - on a tie by kind, then by how closely a backlog
    entry matched its product, then in the order given (a backlog's own order); then the
    unscored, in the order given."""
    scored = [c for c in candidates if c["score"] is not None]
    unscored = [c for c in candidates if c["score"] is None]
    scored.sort(key=lambda c: (-c["score"], KIND_ORDER.index(c["kind"]), -c.get("match", 0)))
    return scored + unscored


# ---- the hooks: what the Builds and blog workers read -----------------------------------------
def read_ranking(state_dir, now: float) -> list | None:
    """The ranked candidates of a fresh ``demand.json``; None when there is none, it is older
    than ``STALE_AFTER``, or it cannot be read (said in the log - the reader then falls back
    to its own order, never to a guess)."""
    if state_dir is None:
        return None
    path = Path(state_dir) / DEMAND_FILE
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("demand: %s cannot be read (%s: %s); nothing is steered by it", path,
                    type(exc).__name__, exc)
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("ranking"), list) \
            or not isinstance(doc.get("computed_at"), (int, float)):
        log.warning("demand: %s is not a demand ranking; nothing is steered by it", path)
        return None
    if now - float(doc["computed_at"]) > STALE_AFTER:
        return None
    return [c for c in doc["ranking"] if isinstance(c, dict)]


def _wanted(ranking) -> list:
    """The candidates with a measured, positive score, best first."""
    return [c for c in ranking or [] if isinstance(c.get("score"), (int, float))
            and not isinstance(c.get("score"), bool) and c["score"] > 0]


def build_preference(state_dir, now: float) -> list:
    """The Builds backlog's waiting slugs by measured demand, best first (only those with a
    positive score). Empty: no fresh ranking, or no backlog product has measured demand."""
    return [c["ref"] for c in _wanted(read_ranking(state_dir, now))
            if c.get("kind") == "build" and isinstance(c.get("ref"), str)]


def product_preference(state_dir, now: float) -> list:
    """The API products behind the candidates with measured demand, best first, each once."""
    out: list = []
    for c in _wanted(read_ranking(state_dir, now)):
        pid = c.get("product")
        if isinstance(pid, str) and pid not in out:
            out.append(pid)
    return out


def topic_product(topic) -> str | None:
    return GUIDES.get(topic.path)


def prefer_topic(free_topics, products) -> object | None:
    """The first free topic (in the rotation's order) about the highest-demand product that
    has one; None when no free topic is about any of them."""
    for pid in products or ():
        for t in free_topics:
            if topic_product(t) == pid:
                return t
    return None


# ---- the worker -------------------------------------------------------------------------------
class DemandWorker(DashReader):
    """``products.demand``: the ranked demand, its owner cards, and the digest's figures."""

    unknown = "demand"
    record_what = "the demand worker's ranking (demand.json) and its own record of cards"

    def __init__(self, spec, *, url: str, token_file: str, blog_worker: str = BLOG_WORKER,
                 builds_worker: str = BUILDS_WORKER) -> None:
        super().__init__(spec, url=url, token_file=token_file)
        self.blog_worker = blog_worker
        self.builds_worker = builds_worker

    @never_raises()
    def run(self, ctx: WorkContext) -> Result:
        if ctx.state_dir is None:
            return self._err(ErrorKind.NOT_CONFIGURED, "no state dir: the demand worker cannot "
                             "write its ranking or remember its cards", retryable=False)
        try:
            rec = read_record(ctx.state_dir, self.worker_id) or {}
        except _Unreadable as exc:
            return self._err(ErrorKind.MALFORMED, f"its record is unreadable ({exc}); refusing "
                             "to run, since it could repeat a card", retryable=False)
        rec = {"cards": [], **rec}
        got = self._read_dash(ctx)
        if isinstance(got, Err):
            return got
        doc, resp = got.value
        notes: list = []
        try:
            signals = Signals.read(doc)
        except (TypeError, ValueError, KeyError) as exc:
            return self._err(ErrorKind.MALFORMED, f"{type(exc).__name__}: {exc}")
        if signals.usage is None and signals.paths is None:
            return self._err(ErrorKind.NOT_CONFIGURED, "the dash has neither a usage nor a "
                             "traffic report; demand is UNKNOWN, not zero (the last ranking, "
                             "if any, is kept and goes stale)", retryable=False)
        try:
            candidates, counterparts = self._candidates(ctx, signals, notes)
        except (TypeError, ValueError, KeyError) as exc:
            return self._err(ErrorKind.MALFORMED, f"{type(exc).__name__}: {exc}")
        ranking = rank(candidates)
        out = {"computed_at": ctx.now, "source": self.url, "usage_days": USAGE_DAYS,
               "traffic_since": signals.since, "inputs": {k: {"measures": v[0], "window": v[1],
                                                              "weight": v[2], "per": v[3]}
                                                          for k, v in INPUTS.items()},
               "unavailable": UNAVAILABLE, "site": signals.site(), "notes": notes,
               "ranking": ranking}
        save_record(Path(ctx.state_dir) / DEMAND_FILE, out)
        events = self._card(ctx, rec, ranking, counterparts)
        rec["cards"] = rec["cards"][-KEEP_CARDS:]
        save_record(record_path(ctx.state_dir, self.worker_id), rec)
        return Ok((*events, self._ranking_row(ctx, resp, ranking, signals, notes)))

    # ---- the candidates ---------------------------------------------------------------------
    def _candidates(self, ctx, s: Signals, notes: list) -> tuple:
        out: list = []
        pids = list(APIS) + sorted(p for p in s.products() if p not in APIS)
        for pid in pids:
            out.append(self._candidate("api", pid, APIS.get(pid, pid), pid,
                                       {**s.api_inputs(pid),
                                        "guide_views": s.guide_views(pid=pid)}))
        for slug, (name, guide) in TOOLS.items():
            pid = GUIDES[guide]
            out.append(self._candidate("tool", slug, name, pid,
                                       {**s.api_inputs(pid),
                                        "guide_views": s.guide_views(paths=(guide,))},
                                       unavailable=["tool_click_through_per_tool"]))
        out += self._topics(ctx, s, notes)
        counterparts = set()
        out += self._builds(ctx, s, notes, counterparts)
        out += self._api_ideas(ctx, s, notes)
        for folder in self._shelf_slugs(ctx):
            pid, _strength = related_product(_words(folder))
            if pid:
                counterparts.add(pid)
        return out, counterparts

    @staticmethod
    def _candidate(kind, ref, name, pid, inputs, *, via=None, unavailable=(), note="") -> dict:
        score, partial = score_of(inputs)
        c = {"id": f"{kind}:{ref}", "kind": kind, "ref": ref, "name": name, "product": pid,
             "inputs": inputs, "score": score, "partial": partial,
             "unknown": sorted(k for k, v in inputs.items() if v.get("value") is None)}
        if via:
            c["via"] = via
        if unavailable:
            c["unavailable"] = list(unavailable)
        if score is None:
            c["why_unscored"] = note or "no input could be measured"
        return c

    def _topics(self, ctx, s: Signals, notes: list) -> list:
        posts: dict = {}
        try:
            for p in published_posts(read_record(ctx.state_dir, self.blog_worker)):
                if isinstance(p.get("topic"), str) and isinstance(p.get("slug"), str):
                    posts.setdefault(p["topic"], []).append(p)
        except _Unreadable as exc:
            log.warning("%s: the blog record is unreadable: %s", self.worker_id, exc)
            notes.append("the blog record is unreadable: its posts' figures are unknown")
        out = []
        for t in SEEDS:
            pid = topic_product(t)
            inputs = {"guide_views": s.guide_views(paths=(t.path,))}
            for p in posts.get(t.key, []):
                for k, v in s.post_inputs(p).items():
                    inputs[k] = _add(inputs.get(k), v)
            out.append(self._candidate("topic", t.key, t.subject, pid, inputs,
                                       unavailable=["search_referrals_per_post"]))
        return out

    def _builds(self, ctx, s: Signals, notes: list, counterparts: set) -> list:
        entries = self._builds_entries(ctx, notes)
        if entries is None:
            return []
        try:
            taken = set((read_record(ctx.state_dir, self.builds_worker) or {}).get("products")
                        or {})
        except _Unreadable as exc:
            log.warning("%s: the Builds record is unreadable: %s", self.worker_id, exc)
            notes.append("the Builds record is unreadable: which backlog products are taken "
                         "is unknown, so none is ranked")
            return []
        out = []
        for e in entries:
            pid, strength = related_product(_words(e["slug"]), e.get("tags") or ())
            if pid:
                counterparts.add(pid)       # a paid download exists or is planned for it
            if e["slug"] in taken:
                continue
            out.append(self._related("build", e["slug"], e["name"], pid, strength, s))
        return out

    def _builds_entries(self, ctx, notes: list) -> list | None:
        """The Builds backlog's usable entries, read WITHOUT seeding (the Builds worker writes
        that file, never this one): no file yet is the seed it would write."""
        if ctx.builds_dir is None:
            notes.append("no builds dir: the Builds backlog is not ranked")
            return None
        path = builds_backlog.backlog_path(ctx.builds_dir)
        if not path.exists():
            return builds_backlog.view({"products": [dict(e) for e in builds_backlog.SEED]})[
                "products"]
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("%s: the Builds backlog cannot be read: %s", self.worker_id, exc)
            notes.append("the Builds backlog cannot be read: it is not ranked")
            return None
        if not isinstance(raw, dict) or not isinstance(raw.get("products"), list):
            notes.append("the Builds backlog has no product list: it is not ranked")
            return None
        return builds_backlog.view(raw)["products"]

    def _api_ideas(self, ctx, s: Signals, notes: list) -> list:
        if ctx.apibuilds_dir is None:
            notes.append("no apibuilds dir: the API builder's backlog is not ranked")
            return []
        from .apibuild.backlog import load_backlog
        try:
            backlog = load_backlog(ctx.apibuilds_dir, write_seed=False)
        except ValueError as exc:
            log.warning("%s: the API builder's backlog cannot be read: %s", self.worker_id, exc)
            notes.append("the API builder's backlog cannot be read: it is not ranked")
            return []
        out = []
        for e in backlog.entries:
            words = [e["id"]] + [w for ep in e.get("endpoints") or []
                                 for w in _words(ep.get("path", ""))]
            pid, strength = related_product([e["id"]], words)
            out.append(self._related("apiidea", e["id"], e.get("name") or e["id"], pid,
                                     strength, s))
        return out

    def _related(self, kind, ref, name, pid, strength: int, s: Signals) -> dict:
        if pid is None:
            return self._candidate(kind, ref, name, None, {}, note="no measured signal relates "
                                   "to it: none of its words names an API product or guide")
        c = self._candidate(kind, ref, name, pid, {**s.api_inputs(pid),
                                                   "guide_views": s.guide_views(pid=pid)},
                            via=f"api:{pid}")
        c["match"] = strength
        return c

    @staticmethod
    def _shelf_slugs(ctx) -> list:
        if ctx.products_dir is None or not Path(ctx.products_dir).is_dir():
            return []
        return sorted(p.name for p in Path(ctx.products_dir).iterdir() if p.is_dir())

    # ---- the owner's card -------------------------------------------------------------------
    @staticmethod
    def suggestions(ranking: list, counterparts: set) -> list:
        """``[(id, title, body, figures)]``, strongest first: a product saturated on
        ``SATURATED_DAYS`` or more days, then a free tool whose guide drew
        ``HIGH_GUIDE_VIEWS`` or more views with no paid download for its product."""
        out = []
        sat = sorted((c for c in ranking if c["kind"] == "api"
                      and (c["inputs"].get("api_saturated_days") or {}).get("value") is not None
                      and c["inputs"]["api_saturated_days"]["value"] >= SATURATED_DAYS),
                     key=lambda c: (-c["inputs"]["api_saturated_days"]["value"], c["id"]))
        for c in sat:
            i = c["inputs"]
            n, calls, cd = (i["api_saturated_days"]["value"], i["api_calls"]["value"],
                            i["api_caller_days"]["value"])
            title = f"Demand seen: the {c['name']} API is at the free cap"
            body = (f"demand seen: on {n} of the last {USAGE_DAYS} days the average caller of the "
                    f"{c['name']} API made {FREE_PER_DAY} or more calls - the free daily cap "
                    f"({calls} calls over {cd} caller-days) - suggest a paid offline "
                    f"{c['name']} tool for bulk use with no daily cap, or a bulk API plan.")
            figs = [Figure(n, "count", INPUTS["api_saturated_days"][0], c["id"], "last14"),
                    Figure(calls, "count", "API calls", c["id"], "last14"),
                    Figure(cd, "count", "API caller-days", c["id"], "last14")]
            out.append((f"saturated-{c['ref']}", title, body, figs))
        tools = sorted((c for c in ranking if c["kind"] == "tool"
                        and c["product"] not in counterparts
                        and c["inputs"]["guide_views"].get("value") is not None
                        and c["inputs"]["guide_views"]["value"] >= HIGH_GUIDE_VIEWS),
                       key=lambda c: (-c["inputs"]["guide_views"]["value"], c["id"]))
        for c in tools:
            v = c["inputs"]["guide_views"]
            least = "at least " if v.get("at_least") else ""
            title = f"Demand seen: {c['name']}, and nothing paid to download"
            body = (f"demand seen: the API guide behind the free {c['name']} tool had {least}"
                    f"{v['value']} page views in the last 30 days, and no paid download covers "
                    f"it (nothing on the shelf or in the Builds backlog) - suggest a "
                    f"downloadable {c['name']} for Gumroad. Per-tool click-throughs are "
                    f"UNKNOWN (Scrooge does not keep utm_content).")
            figs = [Figure(v["value"], "count", INPUTS["guide_views"][0], c["id"], "last30")]
            out.append((f"no-paid-{c['ref']}", title, body, figs))
        return out

    def _card(self, ctx, rec: dict, ranking: list, counterparts: set) -> list:
        posted = [c for c in rec["cards"] if isinstance(c, dict)
                  and isinstance(c.get("posted_at"), (int, float))]
        if any(ctx.now - c["posted_at"] < CARD_EVERY for c in posted):
            return []                                       # one card a day at most
        recent = {c.get("suggestion") for c in posted if ctx.now - c["posted_at"] < REPEAT_AFTER}
        pick = next((s for s in self.suggestions(ranking, counterparts) if s[0] not in recent),
                    None)
        if pick is None:
            return []
        sid, title, body, figs = pick
        if ctx.job is None:
            return [self._event(ctx, "demand.card_unposted", {
                "suggestion": sid, "why": "no hands: the card goes only through Pionir"}, figs)]
        key = f"demand:{sid}:{int(ctx.now // 86400)}"
        body += ("\n\nA suggestion only: nothing was added to any backlog, built or put on "
                 "sale. Add it to a backlog yourself if you want it.")
        out = ctx.job(Job(CARD, {"key": key, "kind": CARD_KIND, "title": title[:120],
                                 "body": body, "replies": False},
                          what="post the demand card"))
        result = out.result if isinstance(out.result, dict) else {}
        if out.status == "done" and result.get("ok") is not False:
            rec["cards"].append({"suggestion": sid, "key": key, "posted_at": ctx.now,
                                 "title": title[:120]})
            return [self._event(ctx, "demand.card", {"suggestion": sid, "title": title[:120],
                                                     "said": _clip(body, 600)}, figs)]
        why = out.error or result.get("error") or result.get("unavailable") or out.status
        if out.status == "failed" and out.error_type == "AdapterProtocolError":
            log.error("%s: Pionir refused the demand card %s: %s", self.worker_id, key, why)
        return [self._event(ctx, "demand.card_unposted", {"suggestion": sid,
                                                          "why": _clip(why, 200)}, figs)]

    # ---- what the leader reads --------------------------------------------------------------
    def _event(self, ctx, kind: str, payload: dict, figures=()):
        return make_output(self, kind=kind, valid_at=ctx.now, observed_at=ctx.now,
                           payload=payload, figures=figures, entities=self.entities,
                           provenance={"source": "real", "provider": self.provider,
                                       "record": self.record_what})

    def _ranking_row(self, ctx, resp, ranking: list, s: Signals, notes: list):
        top = [c for c in ranking if c["score"] is not None][:TOP_N]
        figures, parts = [], []
        for c in top:
            figures.append(Figure(c["score"], "count", "demand score", c["id"], "now"))
            said = []
            for k, v in c["inputs"].items():
                if v.get("value") is None:
                    continue
                what, window = INPUTS[k][0], INPUTS[k][1]
                least = " (at least)" if v.get("at_least") else ""
                figures.append(Figure(v["value"], "count", what + least, c["id"], window))
                said.append(f"{v['value']}{'+' if least else ''} {what}")
            parts.append(f"{c['id']} (demand score {c['score']}"
                         + (", partial" if c["partial"] else "")
                         + (f": {', '.join(said)}" if said else "") + ")")
        ft = s.free_tool_clicks()
        if ft.get("value") is not None:
            figures.append(Figure(ft["value"], "count",
                                  "free tool click-throughs to the API, all tools"
                                  + (" (at least)" if ft.get("at_least") else ""),
                                  "free-tools", "last30"))
        summary = ("top demand: " + "; ".join(parts)) if parts else \
            "no candidate has a measured demand signal yet: every score is UNKNOWN"
        unscored = [c["id"] for c in ranking if c["score"] is None]
        return make_output(self, kind="demand.ranking", valid_at=ctx.now, observed_at=ctx.now,
                           payload={"summary": summary, "top": [c["id"] for c in top],
                                    "candidates": len(ranking),
                                    "scored": len(ranking) - len(unscored),
                                    "unknown": unscored[:12],
                                    "unavailable": sorted(UNAVAILABLE),
                                    "notes": notes[:5], "file": DEMAND_FILE},
                           figures=figures, entities=self.entities,
                           provenance={**self._provenance(resp), "record": self.record_what})


def _add(a: dict | None, b: dict) -> dict:
    """Two measurements of one input summed (two posts on one topic); unknown stays unknown."""
    if a is None:
        return b
    if a.get("value") is None or b.get("value") is None:
        return _inp(None, why=a.get("why") or b.get("why") or "not measured")
    return _inp(a["value"] + b["value"], at_least=bool(a.get("at_least") or b.get("at_least")))


class Signals:
    """The dash's measured signals, checked: ``usage`` (14 days, per day and product) and
    ``traffic`` (30 days). A part that is absent is UNKNOWN (None), never empty."""

    def __init__(self) -> None:
        self.usage: list | None = None        # [(day, product, calls, errors, callers)]
        self.since: str | None = None
        self.paths: dict | None = None
        self.paths_capped = False
        self.campaigns: list | None = None
        self.camps_capped = False
        self.referrers: list | None = None
        self.sales: list | None = None

    @classmethod
    def read(cls, doc: dict) -> Signals:
        s = cls()
        usage = doc.get("usage")
        if usage is not None:
            if not isinstance(usage, list):
                raise _Malformed("usage is not a list")
            s.usage = []
            for i, row in enumerate(usage[:5000]):
                where = f"usage[{i}]"
                if not isinstance(row, dict) or not isinstance(row.get("day"), str) \
                        or not isinstance(row.get("product"), str):
                    raise _Malformed(f"{where} is {row!r}")
                s.usage.append((row["day"], row["product"], _count(row, "calls", where),
                                _count(row, "errors", where), _count(row, "callers", where)))
        t = doc.get("traffic")
        if t is not None:
            if not isinstance(t, dict):
                raise _Malformed("traffic is not an object")
            if not isinstance(t.get("since"), str):
                raise _Malformed(f"traffic.since is {t.get('since')!r}")
            s.since = t["since"]
            paths = _rows(t, "top_paths", "path")
            s.paths = {p: c["n"] for p, c in paths}
            s.paths_capped = _capped(paths)
            s.campaigns = _rows(t, "top_campaigns", "campaign")
            s.camps_capped = _capped(s.campaigns)
            s.referrers = _rows(t, "top_referrers", "ref_host", required=False)
            s.sales = _rows(t, "sales_by_campaign", "campaign", required=False,
                            counts=("sales", "net"))
        return s

    def products(self) -> set:
        return {p for _d, p, _c, _e, _n in self.usage or []}

    def api_inputs(self, pid: str) -> dict:
        if self.usage is None:
            why = "the dash has no usage report"
            return {k: _inp(None, why=why)
                    for k in ("api_calls", "api_caller_days", "api_saturated_days")}
        rows = [r for r in self.usage if r[1] == pid]
        # every product with a call in the window has rows: none is a measured zero
        return {"api_calls": _inp(sum(r[2] for r in rows)),
                "api_caller_days": _inp(sum(r[4] for r in rows)),
                "api_saturated_days": _inp(sum(1 for r in rows
                                               if r[4] > 0 and r[2] >= FREE_PER_DAY * r[4]))}

    def guide_views(self, *, pid: str | None = None, paths=()) -> dict:
        paths = tuple(paths) or tuple(p for p, g in GUIDES.items() if g == pid)
        if self.paths is None:
            return _inp(None, why="the dash has no traffic report")
        if not paths:
            return _inp(None, why="no API guide is known for it")
        listed = [self.paths[p] for p in paths if p in self.paths]
        missing = len(listed) < len(paths)
        if missing and self.paths_capped:
            if not listed:
                return _inp(None, why=f"not in the top {LIST_CAP} pages (the list is capped)")
            return _inp(sum(listed), at_least=True, why=f"some of its guides are not in the "
                                                        f"top {LIST_CAP} pages")
        return _inp(sum(listed))

    def post_inputs(self, post: dict) -> dict:
        if self.paths is None:
            why = "the dash has no traffic report"
            return {"post_views": _inp(None, why=why), "post_clicks": _inp(None, why=why)}
        path = f"/blog/{post['slug']}"
        if path in self.paths:
            views = _inp(self.paths[path])
        elif self.paths_capped:
            views = _inp(None, why=f"not in the top {LIST_CAP} pages (the list is capped)")
        else:
            views = _inp(0)
        clicks = _post_rows(self.campaigns, self.camps_capped, source="blog",
                            campaign=_post_campaign(post))
        return {"post_views": views,
                "post_clicks": _inp(clicks) if clicks is not None else _inp(
                    None, why=f"not in the top {LIST_CAP} campaigns (the list is capped)")}

    def free_tool_clicks(self) -> dict:
        if self.campaigns is None:
            return _inp(None, why="the dash has no traffic report")
        src, med, camp = FREE_TOOLS_CAMPAIGN
        listed = any((p := split_campaign(n)) is not None and p == FREE_TOOLS_CAMPAIGN
                     for n, _c in self.campaigns)
        if not listed and self.camps_capped:
            return _inp(None, why=f"not in the top {LIST_CAP} campaigns (the list is capped)")
        return _inp(_campaign_rows(self.campaigns, source=src, medium=med, campaign=camp))

    def search_visits(self) -> dict:
        if self.referrers is None:
            return _inp(None, why="the dash reports no referrers")
        n = sum(c["n"] for h, c in self.referrers if SEARCH_HOSTS.search(h))
        return _inp(n, at_least=_capped(self.referrers))

    def site(self) -> dict:
        return {"free_tool_click_throughs_30d": self.free_tool_clicks(),
                "search_visits_30d": self.search_visits(),
                "usage_reported": self.usage is not None,
                "traffic_reported": self.paths is not None}
