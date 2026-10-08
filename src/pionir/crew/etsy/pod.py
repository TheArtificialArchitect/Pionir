"""``etsy.pod``: one print-on-demand product at a time, through Printify to the Etsy shop.

The same shape as the digital maker (``maker.py``), with two cards per product:

1. ``printify.create_product`` - the design and the listing text, priced cost-plus. Nothing
   is public and nothing is charged: Printify keeps it private. The adapter learns the print
   costs from Printify's answer and holds every variant to ``min_margin_cents`` after Etsy's
   fees, repricing up to ``max_price_cents`` or disabling a variant that cannot.
2. ``printify.publish`` - the product, with each variant's final price, print cost and what
   the shop keeps, pushed to the linked Etsy shop (Etsy's listing fee: ``spends_money``).

What it makes: the best-ranked keyword in the scout's fresh ranking that names a product
Printify's catalog has (``scout.pod_product``), never made before. The product's blueprint
and print provider are chosen from Printify's catalog as read now (``printify.catalog``):
the blueprint whose title names the product, the provider with the most variants sharing one
front print area, light colours only (the design's ink is dark - a dark garment would show
nothing, and a mockup must not misrepresent the product). The design is the checked phrase,
drawn at that print area's exact pixel size (``render.design_png``).

Low volume on purpose (``daily_cap``, default 1): the same Etsy shop carries the digital
listings, and a suspension would end both streams.
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from ...adapters import printify as printify_adapter
from ...adapters.etsy import credentials_problem as etsy_problem
from ...adapters.printify import CATALOG, LISTING_FEE
from ...adapters.printify import CREATE as P_CREATE
from ...adapters.printify import PUBLISH as P_PUBLISH
from ...adapters.printify import credentials_problem as printify_problem
from ..figures import Figure
from ..hands import Job
from ..log import log
from ..result import Err, Ok, Result
from ..worker import ErrorKind, WorkContext, make_output, never_raises
from ..workers import _Base
from . import render, rules
from .common import (
    Record,
    Unreadable,
    etsy_dir,
    follow,
    ninety_nine,
    read_scout,
    sha256,
    slug_for,
    write_staged,
)

PURPOSE = "etsy print-on-demand listing"
INTRO = (60, 500)
BLOCK_DAYS = 14
MAX_VARIANTS = 10
LIGHT = ("white", "natural", "ivory", "cream", "bone")
GENERIC_TAGS = ("gift idea", "typography", "quote", "minimalist", "black text",
                "word art", "simple design", "statement", "everyday", "text design",
                "modern", "lettering", "gift")


def _light(variant: dict) -> bool:
    opts = variant.get("options") if isinstance(variant.get("options"), dict) else {}
    colour = opts.get("color") or opts.get("colour")
    if colour is None:
        title = str(variant.get("title") or "").lower()
        # no colour option: a poster or a white mug; a title naming a dark colour is not
        return not any(d in title for d in ("black", "navy", "dark", "charcoal"))
    return any(word in str(colour).lower() for word in LIGHT)


def choose(catalog: dict) -> dict | None:
    """The blueprint, provider and variants to make, from Printify's catalog as read now.
    None when nothing in it can carry a front design in a light colour."""
    best = None
    for b in catalog.get("blueprints") or []:
        for p in b.get("providers") or []:
            groups: dict = {}
            for v in p.get("variants") or []:
                if not isinstance(v, dict) or not isinstance(v.get("id"), int) or not _light(v):
                    continue
                for ph in v.get("placeholders") or []:
                    w, h = ph.get("width"), ph.get("height")
                    if ph.get("position") == "front" and isinstance(w, int) and \
                            isinstance(h, int) and 500 <= w <= 20000 and 500 <= h <= 20000:
                        groups.setdefault((w, h), []).append(v["id"])
            if not groups:
                continue
            size = Counter({k: len(v) for k, v in groups.items()}).most_common(1)[0][0]
            ids = groups[size][:MAX_VARIANTS]
            cand = {"blueprint_id": b["blueprint_id"], "blueprint": b.get("title"),
                    "print_provider_id": p["print_provider_id"], "variants": ids,
                    "width": size[0], "height": size[1]}
            if best is None or len(ids) > len(best["variants"]):
                best = cand
    return best


def describe(intro: str, phrase: str) -> str:
    return "\n\n".join((
        intro.strip(),
        f"Design: the words \"{phrase}\" in bold black lettering on a light background, as "
        "in the photos.",
        "Sizes and colours are the ones listed in the options.",
        rules.POD_NOTE,
        rules.AI_DISCLOSURE,
    ))


class PodMaker(_Base):
    """``etsy.pod``: Printify products for the Etsy shop, each through two approvals."""

    schema = {
        "type": "object",
        "properties": {"phrase": {"type": "string"}, "title": {"type": "string"},
                       "intro": {"type": "string"},
                       "tags": {"type": "array", "items": {"type": "string"}}},
        "required": ["phrase", "title", "intro", "tags"],
    }

    def __init__(self, spec, *, daily_cap: int = 1, stage_dir: str | None = None,
                 min_margin_cents: int = 400, price_floor_cents: int = 1499,
                 price_ceiling_cents: int = 4999, default_price_cents: int = 2299) -> None:
        super().__init__(spec)
        for name, v, low, high in (("daily_cap", daily_cap, 0, 10),
                                   ("min_margin_cents", min_margin_cents,
                                    printify_adapter.MIN_MARGIN_CENTS, 10_000),
                                   ("price_floor_cents", price_floor_cents, 500, 20_000),
                                   ("price_ceiling_cents", price_ceiling_cents, 500, 20_000),
                                   ("default_price_cents", default_price_cents, 500, 20_000)):
            if isinstance(v, bool) or not isinstance(v, int) or not low <= v <= high:
                raise ValueError(f"{name}: a whole number {low}-{high}")
        if price_floor_cents > price_ceiling_cents:
            raise ValueError("price_floor_cents is above price_ceiling_cents")
        self.daily_cap = daily_cap
        self.stage_param = stage_dir
        self.min_margin = min_margin_cents
        self.price_floor = price_floor_cents
        self.price_ceiling = price_ceiling_cents
        self.default_price = default_price_cents

    def stage_root(self) -> Path:
        return etsy_dir(self.stage_param) / "pod"

    def readiness(self, secrets_dir) -> str | None:
        # Printify publishes to the Etsy shop: both must be set up (the Etsy app's key also
        # feeds the scout this worker picks from).
        return (printify_problem(Path(secrets_dir) / "printify.json")
                or etsy_problem(Path(secrets_dir) / "etsy.json", ("keystring",
                                                                  "shared_secret")))

    @never_raises()
    def run(self, ctx: WorkContext) -> Result:
        if ctx.state_dir is None:
            return self._err(ErrorKind.NOT_CONFIGURED, "no state dir", retryable=False)
        try:
            record = Record(ctx.state_dir, self.worker_id,
                            {"items": [], "counts": {}, "made": {}, "blocked": {}})
        except Unreadable as exc:
            return self._err(ErrorKind.MALFORMED, f"its record is unreadable ({exc})",
                             retryable=False)
        events: list = []
        self._follow_up(ctx, record, events)
        made = self._make_one(ctx, record, events)
        record.save()
        if isinstance(made, Err):
            if not events:
                return made
            log.warning("%s: nothing new this run: %s", self.worker_id, made.error)
        return Ok((*events, self._tally(ctx, record, made if isinstance(made, str) else None)))

    # ---- following up ---------------------------------------------------------------------
    def _follow_up(self, ctx: WorkContext, record: Record, events: list) -> None:
        for item in record.doc["items"]:
            status = item.get("status")
            if status == "create_pending":
                state, got = follow(ctx, item, P_CREATE)
                if state == "waiting":
                    continue
                if state != "approved":
                    self._settle(ctx, record, item, state, str(got), events)
                    continue
                res = got.result if isinstance(got.result, dict) else {}
                pid, variants = res.get("product_id"), res.get("variants")
                if not isinstance(pid, str) or not isinstance(variants, list) or not variants:
                    self._settle(ctx, record, item, "failed", "Pionir said done but gave no "
                                 "product id or variants", events)
                    continue
                item.update(status="created", product_id=pid,
                            variants=[{k: v[k] for k in ("id", "price_cents", "cost_cents")}
                                      for v in variants if isinstance(v, dict)])
                record.count("products_created")
                self._submit_publish(ctx, record, item, events)
            elif status in ("created", "publish_unreachable"):
                self._submit_publish(ctx, record, item, events)
            elif status == "publish_pending":
                state, got = follow(ctx, item, P_PUBLISH)
                if state == "waiting":
                    continue
                if state != "approved":
                    self._settle(ctx, record, item, state, str(got), events)
                    continue
                item.update(status="published", settled_at=ctx.now)
                record.count("published")
                events.append(self._event(ctx, "etsy.pod_published", {
                    "slug": item["slug"], "product_id": item.get("product_id"),
                    "title": item.get("title")}))

    def _submit_publish(self, ctx: WorkContext, record: Record, item: dict,
                        events: list) -> None:
        if ctx.job is None:
            return
        out = ctx.job(Job(P_PUBLISH, {"slug": item["slug"], "product_id": item["product_id"],
                                      "title": item["title"], "variants": item["variants"],
                                      "min_margin_cents": self.min_margin,
                                      "spend": dict(LISTING_FEE), "spends_money": True},
                          what=f"publish {item['title']!r} to the Etsy shop via Printify"))
        if out.status == "pending_approval":
            item.update(status="publish_pending", approval_id=out.approval_id)
        elif out.status == "unreachable":
            item["status"] = "publish_unreachable"
        else:
            self._settle(ctx, record, item, "failed", out.error or out.status, events)

    def _settle(self, ctx: WorkContext, record: Record, item: dict, status: str, why: str,
                events: list) -> None:
        item.update(status=status, why=why[:300], settled_at=ctx.now)
        record.count(status)
        log.warning("%s: %s is %s: %s", self.worker_id, item.get("slug"), status, why)
        events.append(self._event(ctx, "etsy.not_listed", {
            "slug": item.get("slug"), "status": status, "why": why[:200]}))

    # ---- making one ------------------------------------------------------------------------
    def _make_one(self, ctx: WorkContext, record: Record, events: list):
        rec = record.doc
        if record.made_today(ctx.now) >= self.daily_cap:
            return f"the daily cap of {self.daily_cap} is reached"
        problem = self.readiness(ctx.secrets_dir)
        if problem:
            return self._err(ErrorKind.NOT_CONFIGURED, problem, retryable=False)
        if ctx.job is None or ctx.words is None:
            return self._err(ErrorKind.NOT_CONFIGURED, "no hands or no words", retryable=False)
        ranking = read_scout(ctx.state_dir, ctx.now)
        if ranking is None:
            return "no fresh demand ranking from etsy.scout yet"
        pick = next((r for r in ranking if r.get("track") == "pod"
                     and isinstance(r.get("keyword"), str) and r["keyword"] not in rec["made"]
                     and ctx.now >= float(rec["blocked"].get(r["keyword"]) or 0)
                     and r.get("score") is not None), None)
        if pick is None:
            return "nothing in the demand ranking for print on demand, not made before"
        cat = ctx.job(Job(CATALOG, {"product": pick["product"]},
                          what=f"read Printify's catalog for {pick['product']!r}"))
        if not cat.ran or not isinstance(cat.result, dict):
            return self._err(ErrorKind.UNAVAILABLE, f"Printify's catalog: {cat.status} "
                             f"{cat.error}")
        choice = choose(cat.result)
        if choice is None:
            rec["blocked"][pick["keyword"]] = ctx.now + BLOCK_DAYS * 86400
            return f"Printify's catalog has no light {pick['product']} with a front print area"
        draft, reasons = None, []
        for _attempt in (1, 2):
            got = ctx.words(PURPOSE, self._system(), self._prompt(pick["keyword"], reasons),
                            self.schema)
            if isinstance(got, Err):
                return got
            draft, reasons = self._assemble(got.value, pick["keyword"])
            if not reasons:
                break
            record.count("drafts_blocked")
        if reasons:
            rec["blocked"][pick["keyword"]] = ctx.now + BLOCK_DAYS * 86400
            events.append(self._event(ctx, "etsy.blocked", {
                "keyword": pick["keyword"], "reasons": [r[:120] for r in reasons[:4]]}))
            return f"the draft for {pick['keyword']!r} did not pass the checks"
        price = pick.get("median_price_cents")
        price = ninety_nine(price) if isinstance(price, int) else self.default_price
        price = min(max(price, self.price_floor), self.price_ceiling)
        ceiling = min(self.price_ceiling, max(price, ninety_nine(int(price * 1.35))))
        slug = slug_for("pod", pick["keyword"], ctx.now)
        design = render.design_png(draft["phrase"], choice["width"], choice["height"])
        name = re.sub(r"[^a-z0-9]+", "-", draft["phrase"].lower()).strip("-")[:50] + ".png"
        write_staged(self.stage_root() / slug, {name: design})
        payload = {"slug": slug, "title": draft["title"], "description": draft["description"],
                   "tags": draft["tags"], "blueprint_id": choice["blueprint_id"],
                   "print_provider_id": choice["print_provider_id"], "position": "front",
                   "variants": [{"id": v, "price_cents": price} for v in choice["variants"]],
                   "design": {"name": name, "sha256": sha256(design),
                              "width": choice["width"], "height": choice["height"]},
                   "min_margin_cents": self.min_margin, "max_price_cents": ceiling,
                   "ai_disclosure": rules.AI_DISCLOSURE}
        try:
            printify_adapter.check_create(payload)
        except ValueError as exc:
            rec["blocked"][pick["keyword"]] = ctx.now + BLOCK_DAYS * 86400
            return self._err(ErrorKind.MALFORMED, f"the product failed Pionir's check: {exc}",
                             retryable=False)
        rec["made"][pick["keyword"]] = slug
        item = {"slug": slug, "keyword": pick["keyword"], "title": draft["title"],
                "phrase": draft["phrase"], "submitted_at": ctx.now,
                "blueprint": choice["blueprint"]}
        rec["items"].append(item)
        out = ctx.job(Job(P_CREATE, payload, what=f"make {draft['title']!r} in Printify"))
        if out.status == "pending_approval":
            item.update(status="create_pending", approval_id=out.approval_id)
            record.count("submitted_for_approval")
            events.append(self._event(ctx, "etsy.pending_approval", {
                "slug": slug, "title": draft["title"], "keyword": pick["keyword"]}))
            return f"submitted {slug} for the owner's approval"
        self._settle(ctx, record, item, "failed" if out.status != "unreachable"
                     else "unreachable", out.error or out.status, events)
        return f"{slug}: {out.status}"

    # ---- the words -----------------------------------------------------------------------
    @staticmethod
    def _system() -> str:
        return ("You write short, original phrases for typographic print designs and the Etsy "
                "listing that sells them. Never quote a song, film, book, slogan or anyone's "
                "words; never name a person, place, company, brand, team, character or "
                "celebrity; no claims or promises, no links, no emoji, no hashtags. Answer "
                "only with the JSON object.")

    @staticmethod
    def _prompt(keyword: str, reasons: list) -> str:
        text = (f"Buyers on Etsy search for: {keyword!r}.\n"
                f"- phrase: an original phrase for the design, {rules.PHRASE[0]}-"
                f"{rules.PHRASE[1]} characters, at most {rules.PHRASE_WORDS} words.\n"
                "- title: an Etsy listing title, 60-140 characters, starting with the words "
                "buyers search for, phrases separated by ' | '.\n"
                f"- intro: 1-2 sentences, {INTRO[0]}-{INTRO[1]} characters.\n"
                "- tags: 13 search phrases, lower case, each at most 20 characters.")
        if reasons:
            text += "\nYour last answer was refused for: " + "; ".join(reasons[:6])
        return text

    def _assemble(self, words: dict, keyword: str) -> tuple[dict, list]:
        reasons: list = []
        phrase = " ".join(str(words.get("phrase") or "").split())
        reasons += rules.phrase_problems(phrase)
        intro = " ".join(str(words.get("intro") or "").split())
        if not INTRO[0] <= len(intro) <= INTRO[1]:
            reasons.append(f"intro: {INTRO[0]}-{INTRO[1]} characters")
        title = " ".join(str(words.get("title") or "").split())
        tags: list = []
        for t in [*(words.get("tags") if isinstance(words.get("tags"), list) else []),
                  keyword, *GENERIC_TAGS]:
            if not isinstance(t, str):
                continue
            t = " ".join(re.sub(r"[^a-z0-9 ]+", " ", t.lower()).split())
            if t and len(t) <= rules.TAG_MAX and t not in tags \
                    and not rules.tag_problems([t], exactly=1):
                tags.append(t)
        tags = tags[:rules.TAGS_EXACTLY]
        description = describe(intro, phrase)
        reasons += rules.listing_problems(title, description, tags, kind="pod")
        return ({"phrase": phrase, "title": title, "intro": intro, "tags": tags,
                 "description": description}, reasons)

    def _event(self, ctx: WorkContext, kind: str, payload: dict):
        return make_output(self, kind=kind, valid_at=ctx.now, observed_at=ctx.now,
                           payload=payload, entities=self.entities,
                           provenance={"source": "model", "derived": True,
                                       "checked_by": "etsy.rules"})

    def _tally(self, ctx: WorkContext, record: Record, note: str | None):
        items = record.doc.get("items") or []
        c = record.doc.get("counts") or {}

        def n(*states):
            return sum(1 for i in items if i.get("status") in states)

        figures = [
            Figure(int(c.get("submitted_for_approval", 0)), "count",
                   "print-on-demand products submitted for approval", window="all_time"),
            Figure(n("create_pending", "publish_pending"), "count",
                   "print-on-demand products waiting for the owner's approval", window="now"),
            Figure(n("published"), "count", "print-on-demand products published to Etsy",
                   window="all_time"),
            Figure(n("denied"), "count", "print-on-demand products denied", window="all_time"),
            Figure(n("failed"), "count", "print-on-demand products failed", window="all_time"),
        ]
        return make_output(self, kind="etsy.pod_tally", valid_at=ctx.now, observed_at=ctx.now,
                           payload={"note": note, "daily_cap": self.daily_cap,
                                    "min_margin_cents": self.min_margin},
                           figures=figures, entities=self.entities,
                           provenance={"source": "real", "provider": self.provider,
                                       "record": "the print-on-demand maker's own record"})
