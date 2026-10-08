"""``marketplaces.scout_<market>``: gaps in one store, from its public data, as specs.

One worker per store (``params.market``: apify, chrome or shopify). One run:

1. **Read** the store's public catalogue (providers.py) - at most once per ``fresh_hours``,
   at most ``pages`` catalogue pages and ``details`` item pages, robots.txt obeyed, a pause
   between reads. What was read is kept (``candidates-<market>.json``) and grows run by run;
   a listing not seen for ``FORGET_DAYS`` is forgotten. A store that cannot be read is said
   (an event, and an Err when nothing is cached) - never an empty market.
2. **Score** every niche the listings share (terms.py) and keep the best ``KEEP`` as
   candidates, de-duplicated (one niche per set of listings). A niche that touches people's
   personal data is recorded as EXCLUDED and never specified.
3. **Specify** at most ONE candidate a run: the best one not tried ``MAX_TRIES`` times. Its
   words come from the crew's shared brain (``ctx.words``, JSON schema, the division's
   budget) - the brain may also answer that it is not feasible as an offline core, or that it
   would touch personal data, and then it is REJECTED. The spec is checked fail closed
   (specs.spec_problems: lengths, honest claims, personal data, the store's own rules) and
   its evidence is the scout's measurement, never the model's.
4. **Hand over** the best specified candidate: an Apify Actor (or, once the night builds can
   test JavaScript, a Chrome extension) goes into the Builds backlog (specs.queue_for_build:
   one marketplace product waits there at a time); a Shopify app's spec goes straight to the
   packager for a submission pack. The spec is kept at ``specs/<slug>.json``.

No Job, no card: the packager and the Builds division tell the owner. Everything is in the
outputs the Marketplaces leader reads (``market.*``).
"""
from __future__ import annotations

import time

from ..blog import _clip, _Unreadable, read_record
from ..figures import Figure
from ..log import log
from ..result import Err, Ok, Result
from ..worker import ErrorKind, WorkContext, make_output, never_raises
from ..workers import _Base
from . import paths, providers, specs, terms

KEEP = 20
MAX_TRIES = 3
FORGET_DAYS = 30
BUILDS_WORKER = "builds.daedalus"
PURPOSE = "a marketplace product spec"


def _blank() -> dict:
    return {"listings": {}, "candidates": {}, "cursor": 0, "categories": [], "read_at": 0.0,
            "counts": {}, "last_error": None}


class ScoutWorker(_Base):
    """One store's scout. ``market`` names the store."""

    record_what = "the Marketplaces scout's record of what it read and scored"

    def __init__(self, spec, *, market: str, pages: int = 2, details: int = 8,
                 pause: float = 3.0, fresh_hours: float = 20.0) -> None:
        super().__init__(spec)
        if market not in paths.MARKETS:
            raise ValueError(f"market must be one of {', '.join(paths.MARKETS)}")
        self.market = market
        self.pages = max(1, int(pages))
        self.details = max(0, int(details))
        self.pause = float(pause)
        self.fresh = float(fresh_hours) * 3600
        self.sleep = time.sleep                 # injectable: tests never wait

    def readiness(self, secrets_dir) -> str | None:
        return None             # public catalogues only: no account, no credential

    # ---- one run ---------------------------------------------------------------------------
    @never_raises()
    def run(self, ctx: WorkContext) -> Result:
        root = paths.root_for(ctx.builds_dir)
        if root is None or ctx.state_dir is None:
            return self._err(ErrorKind.NOT_CONFIGURED, "no Builds folder or state dir: the "
                             "scout has nowhere to keep what it reads", retryable=False)
        path = paths.candidates_path(root, self.market)
        try:
            doc = {**_blank(), **paths.read_json(path, {})}
        except paths.Unreadable as exc:
            return self._err(ErrorKind.MALFORMED, f"its record is unreadable ({exc}); fix or "
                             "remove it - it is never replaced silently", retryable=False)
        events: list = []
        read_error = None
        if ctx.now - float(doc.get("read_at") or 0) >= self.fresh:
            try:
                self._read(ctx, doc)
                doc["read_at"] = ctx.now
                doc["last_error"] = None
            except providers.ProviderError as exc:
                read_error = str(exc)
                doc["last_error"] = _clip(exc, 300)
                log.warning("%s: could not read the store: %s", self.worker_id, exc)
                events.append(self._event(ctx, "market.read_failed",
                                          {"market": self.market, "why": _clip(exc, 200)}))
        self._forget(ctx, doc)
        if not doc["listings"]:
            paths.write_json(path, doc)
            if read_error:
                return self._err(ErrorKind.UNAVAILABLE, f"the {self.market} store could not "
                                 f"be read and nothing is cached: {read_error}")
            return Ok((*events, self._tally(ctx, doc, [])))
        found = terms.niches(list(doc["listings"].values()))
        boost = self._performance(root)
        for n in found:
            if boost and set(n["niche"].split()) & boost:
                n["score"] = round(n["score"] * 1.2, 4)
                n["sells_like_ours"] = True
        found.sort(key=lambda d: (-d["score"], d["niche"]))
        self._upsert(ctx, doc, found[:KEEP], events)
        self._specify(ctx, root, doc, events)
        self._hand_over(ctx, root, doc, events)
        paths.write_json(path, doc)
        return Ok((*events, self._tally(ctx, doc, found)))

    # ---- 1. reading the store ------------------------------------------------------------------
    def _read(self, ctx: WorkContext, doc: dict) -> None:
        reader = providers.Reader(ctx.http, pause=self.pause, sleep=self.sleep)
        if self.market == "apify":
            got = providers.read_apify(reader, pages=self.pages)
        elif self.market == "chrome":
            got = self._read_chrome(reader, doc, ctx.now)
        else:
            got = self._read_shopify(reader, doc)
        for item in got:
            old = doc["listings"].get(item["key"]) or {}
            merged = {**old, **{k: v for k, v in item.items() if v is not None or k not in old}}
            merged["seen_at"] = ctx.now
            doc["listings"][item["key"]] = merged
        doc["counts"]["reads"] = int(doc["counts"].get("reads") or 0) + reader.reads

    def _next_categories(self, doc: dict, n: int) -> list:
        cats = doc.get("categories") or []
        if not cats:
            return []
        start = int(doc.get("cursor") or 0) % len(cats)
        picked = [cats[(start + i) % len(cats)] for i in range(min(n, len(cats)))]
        doc["cursor"] = (start + len(picked)) % len(cats)
        return picked

    def _read_chrome(self, reader, doc: dict, now: float) -> list:
        home = reader.get_text(providers.CWS + "/")
        found = providers.cws_categories(home)
        doc["categories"] = sorted(set(doc.get("categories") or []) | set(found)) \
            or list(providers.CWS_SEED)
        out = providers.cws_list(home, "")
        for cat in self._next_categories(doc, self.pages):
            page = reader.get_text(f"{providers.CWS}/category/{cat}")
            doc["categories"] = sorted(set(doc["categories"]) | set(
                providers.cws_categories(page)))
            out += providers.cws_list(page, cat)
        # the item pages for user counts: the ones never read (or read longest ago) first
        known = {x["key"]: x for x in out}
        for key, old in doc["listings"].items():
            known.setdefault(key, old)
        todo = sorted(known.values(), key=lambda x: float(x.get("detail_at") or 0))
        for item in todo[:self.details]:
            got = providers.cws_detail(reader.get_text(item["url"]))
            item.update({k: v for k, v in got.items() if v not in (None, "")})
            item["detail_at"] = now
            if item not in out:
                out.append(item)
        return out

    def _read_shopify(self, reader, doc: dict) -> list:
        home = reader.get_text(providers.SHOPIFY + "/")
        doc["categories"] = sorted(set(doc.get("categories") or []) |
                                   set(providers.shopify_categories(home)))
        out = providers.shopify_list(home, "")
        for cat in self._next_categories(doc, self.pages):
            page = reader.get_text(f"{providers.SHOPIFY}/categories/{cat}")
            doc["categories"] = sorted(set(doc["categories"]) |
                                       set(providers.shopify_categories(page)))
            out += providers.shopify_list(page, cat)
        return out

    def _forget(self, ctx: WorkContext, doc: dict) -> None:
        cutoff = ctx.now - FORGET_DAYS * 86400
        doc["listings"] = {k: v for k, v in doc["listings"].items()
                           if float(v.get("seen_at") or 0) >= cutoff}

    def _performance(self, root) -> set:
        """Words from our own published products that people use (the watcher's record):
        a niche like one that already sells is worth a little more."""
        try:
            perf = paths.read_json(root / paths.PERFORMANCE, {})
        except paths.Unreadable as exc:
            log.warning("%s: %s", self.worker_id, exc)
            return set()
        out: set = set()
        for row in (perf.get(self.market) or {}).get("items") or []:
            if isinstance(row, dict) and (row.get("users") or 0) > 0:
                out.update(terms.phrases(str(row.get("title") or "")))
        return out

    # ---- 2. candidates --------------------------------------------------------------------------
    def _upsert(self, ctx: WorkContext, doc: dict, found: list, events: list) -> None:
        cands = doc["candidates"]
        for n in found:
            key = f"{self.market}:{n['niche']}"
            c = cands.get(key)
            if c is None:
                dup = next((k for k, o in cands.items() if o.get("status") in
                            ("specified", "queued", "built", "packed") and
                            terms._overlap(set(n["keys"]), set(o.get("keys") or [])) > 0.8),
                           None)
                c = {"niche": n["niche"], "market": self.market, "first_seen": ctx.now,
                     "status": "duplicate" if dup else "new", "tries": 0,
                     "duplicate_of": dup}
                if n["personal"]:
                    c.update(status="excluded", why=f"touches people's personal data "
                                                    f"({n['personal']!r})")
                cands[key] = c
                if c["status"] == "new":
                    events.append(self._event(ctx, "market.gap", {
                        "market": self.market, "niche": n["niche"], "score": n["score"],
                        "listings": n["listings"], "demand": n["demand"]},
                        figures=[Figure(n["demand"], "count", self._demand_label(),
                                        stream=n["niche"], window="now"),
                                 Figure(n["listings"], "count", "competing listings",
                                        stream=n["niche"], window="now")]))
            c.update({k: n[k] for k in ("listings", "demand", "quality", "score", "category",
                                        "keys", "sample", "preferred")})
            c["last_seen"] = ctx.now

    def _demand_label(self) -> str:
        return {"apify": "users of the niche's Actors in the last 30 days",
                "chrome": "users of the niche's extensions",
                "shopify": "reviews of the niche's apps (the store's only public demand "
                           "figure)"}[self.market]

    # ---- 3. one spec a run ------------------------------------------------------------------------
    def _specify(self, ctx: WorkContext, root, doc: dict, events: list) -> None:
        todo = sorted((c for c in doc["candidates"].values()
                       if c.get("status") == "new" and int(c.get("tries") or 0) < MAX_TRIES),
                      key=lambda c: (-float(c.get("score") or 0), c["niche"]))
        if not todo:
            return
        c = todo[0]
        if ctx.words is None:
            c["waiting"] = "no shared brain in this crew: nothing is specified without words"
            return
        got = ctx.words(PURPOSE, self._system(), self._prompt(c), specs.words_schema(
            self.market))
        if isinstance(got, Err):
            c["waiting"] = _clip(got.error, 200)
            return                  # no words came: not a try
        c["tries"] = int(c.get("tries") or 0) + 1
        c.pop("waiting", None)
        words = got.value
        if words.get("feasible") is False:
            c.update(status="rejected", why=_clip(f"the brain judged it not feasible: "
                                                  f"{words.get('why_not') or 'no reason'}", 300))
            events.append(self._event(ctx, "market.spec_rejected", {
                "market": self.market, "niche": c["niche"], "why": c["why"]}))
            return
        spec = specs.assemble(self.market, words, c, ctx.now)
        reasons = specs.spec_problems(spec)
        if not reasons and self._slug_taken(ctx, root, spec["slug"]):
            reasons = [f"the slug {spec['slug']} is already used"]
        if reasons:
            c["reasons"] = [_clip(r, 200) for r in reasons[:8]]
            if c["tries"] >= MAX_TRIES:
                c.update(status="rejected", why=f"its spec failed the check {MAX_TRIES} times")
            events.append(self._event(ctx, "market.spec_blocked", {
                "market": self.market, "niche": c["niche"], "try": c["tries"],
                "reasons": [_clip(r, 120) for r in reasons[:3]]}))
            return
        c.update(status="specified", spec=spec, slug=spec["slug"], specified_at=ctx.now)
        c.pop("reasons", None)
        events.append(self._event(ctx, "market.spec_written", {
            "market": self.market, "niche": c["niche"], "slug": spec["slug"],
            "name": spec["name"]}, derived=True))

    def _slug_taken(self, ctx: WorkContext, root, slug: str) -> bool:
        if paths.spec_path(root, slug).exists():
            return True
        try:
            rec = read_record(ctx.state_dir, BUILDS_WORKER) or {}
        except _Unreadable:
            return True             # cannot tell: treat as taken, never a second product
        return slug in (rec.get("products") or {})

    def _system(self) -> str:
        store = {"apify": "the Apify Store, as a pay-per-event Actor",
                 "chrome": "the Chrome Web Store, as a freemium extension",
                 "shopify": "the Shopify App Store, as an embedded app"}[self.market]
        return (f"You write the specification of ONE small software product sold on {store}. "
                "Answer one JSON object in the given schema and nothing else. Every sentence "
                "must be literally true of software that does exactly what the brief says: no "
                "superlatives (best, fastest, #1), no guarantees, no '100%', no 'unlimited', no "
                "user counts, no compliance badges, no links. The product never collects, "
                "scrapes or stores data about people (no emails, phone numbers, profiles, "
                "followers, contacts, leads or reviews). If the gap below cannot be served that "
                "way, answer feasible=false with why_not.")

    def _prompt(self, c: dict) -> str:
        sample = "\n".join(f"- {s.get('title')} (users: {s.get('users')}, rating: "
                           f"{s.get('rating')})" for s in (c.get("sample") or [])[:5])
        rules = {
            "apify": ("The Actor's CORE is a Python 3.11 package using the standard library "
                      "only, with no network access: a function process(record) turns ONE "
                      "input into ONE result dict. The Actor around it (written separately) "
                      "either fetches each URL the user gives and passes {url, status, "
                      "content_type, text} (io=fetch_urls), or passes each JSON item the user "
                      "gives (io=items). Pick io accordingly. It charges one event per "
                      "successful result: give the event a short title and description and a "
                      f"price between {specs.MIN_EVENT_USD} and {specs.MAX_EVENT_USD} USD per "
                      "result."),
            "chrome": ("A Manifest V3 extension with one single purpose, at most 4 of these "
                       "permissions: " + ", ".join(sorted(specs.CHROME_PERMISSIONS)) + " (no "
                       "host permissions), with a one-sentence justification for each. Its "
                       "logic must work offline. Free features and Pro features (via "
                       "ExtensionPay) and a monthly Pro price."),
            "shopify": ("An embedded Shopify admin app needing only read scopes from: "
                        + ", ".join(sorted(specs.SHOPIFY_SCOPES)) + ", each justified, and 1-4 "
                        "plans with a monthly price (one may be free)."),
        }[self.market]
        return (f"The gap: the niche '{c['niche']}' on this store. {c.get('listings')} listings "
                f"compete; their measured demand is {c.get('demand')} "
                f"({self._demand_label()}); the most used of them rate {c.get('quality')} of "
                f"1.0. The most used today:\n{sample}\n\nWrite a product that serves this "
                f"niche better and more simply.\n{rules}\n\nslug: 3-40 of a-z 0-9 and -; name "
                "5-80 characters; summary 20-200 characters, one line; brief one paragraph of "
                "60-1400 characters; 2-6 features and 3-10 acceptance tests, one line each "
                "(no < or >); limits: what it does NOT do, honestly; 1-5 lower-case tags.")

    # ---- 4. the hand-over ------------------------------------------------------------------------------
    def _hand_over(self, ctx: WorkContext, root, doc: dict, events: list) -> None:
        ready = sorted((c for c in doc["candidates"].values() if c.get("status") == "specified"),
                       key=lambda c: (-float(c.get("score") or 0), c["niche"]))
        if not ready:
            return
        c = ready[0]
        spec = c["spec"]
        if self.market == "shopify":
            paths.write_json(paths.spec_path(root, spec["slug"]), spec)
            c.update(status="packed", handed_at=ctx.now)
            events.append(self._event(ctx, "market.spec_to_packager", {
                "market": self.market, "slug": spec["slug"], "name": spec["name"]}))
            return
        if ctx.builds_dir is None:
            return
        try:
            rec = read_record(ctx.state_dir, BUILDS_WORKER) or {}
        except _Unreadable as exc:
            c["waiting"] = _clip(f"the Builds record is unreadable ({exc})", 200)
            return
        ok, note = specs.queue_for_build(ctx.builds_dir, spec, set(rec.get("products") or {}))
        if not ok:
            if c.get("waiting") != note:
                events.append(self._event(ctx, "market.spec_waiting", {
                    "market": self.market, "slug": spec["slug"], "why": _clip(note, 200)}))
            c["waiting"] = _clip(note, 300)
            return
        paths.write_json(paths.spec_path(root, spec["slug"]), spec)
        c.update(status="queued", queued_at=ctx.now)
        c.pop("waiting", None)
        events.append(self._event(ctx, "market.spec_queued", {
            "market": self.market, "slug": spec["slug"], "name": spec["name"],
            "note": _clip(note, 160)}))

    # ---- what the leader reads ------------------------------------------------------------------------
    def _event(self, ctx, kind: str, payload: dict, figures=(), *, derived: bool = False):
        prov = {"source": "real", "provider": self.provider, "record": self.record_what}
        if derived:
            prov["derived"] = True          # its words came from the brain
        return make_output(self, kind=kind, valid_at=ctx.now, observed_at=ctx.now,
                           payload=payload, figures=figures, entities=self.entities,
                           provenance=prov)

    def _tally(self, ctx, doc: dict, found: list):
        cands = list(doc["candidates"].values())

        def n(status: str) -> int:
            return sum(1 for c in cands if c.get("status") == status)

        top = [{"niche": d["niche"], "score": d["score"], "listings": d["listings"],
                "demand": d["demand"], "excluded": bool(d["personal"])} for d in found[:5]]
        figures = [
            Figure(len(doc["listings"]), "count", f"{self.market} listings known",
                   window="now"),
            Figure(n("new"), "count", "gaps not yet specified", window="now"),
            Figure(n("specified"), "count", "specs waiting to be handed over", window="now"),
            Figure(n("queued") + n("packed"), "count", "specs handed over",
                   window="all_time"),
            Figure(n("excluded"), "count", "gaps excluded for personal data",
                   window="all_time"),
        ]
        waiting = [{"slug": c.get("slug"), "why": c.get("waiting")} for c in cands
                   if c.get("status") == "specified" and c.get("waiting")][:2]
        return make_output(self, kind="market.scout", valid_at=ctx.now, observed_at=ctx.now,
                           payload={"market": self.market, "top_gaps": top,
                                    "read_at": doc.get("read_at"),
                                    "last_error": doc.get("last_error"),
                                    "waiting": waiting},
                           figures=figures, entities=self.entities,
                           provenance={"source": "real", "provider": self.provider,
                                       "record": self.record_what})
