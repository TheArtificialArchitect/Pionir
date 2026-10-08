"""``etsy.digital``: one Etsy digital download at a time, from measured demand to a parked draft.

One run:

1. **Follow up** what it submitted. A draft Pionir created (``etsy.create_draft_listing``,
   approved and done with a ``listing_id``) is submitted at once for activation
   (``etsy.activate_listing``) - a second card, because going live is what Etsy charges for.
   An activation approved and done is recorded ``active`` with its URL. Denied, failed and
   forgotten are recorded as such; nothing is ever resubmitted after a no.
2. **At most one new listing per run, at most ``daily_cap`` per UTC day**, and only when the
   shop is set up (``readiness`` names the exact missing credential, so the worker idles
   cleanly until Ian runs ``tools\\setup-etsy.ps1``).
3. **What to make**: the best-ranked keyword in the scout's fresh ranking that a workbook
   kind can make (``sheets.kind_for``), that names an Etsy category, and that this worker
   never made before (its record is the dedupe ledger: keyword and slug, for ever).
4. **Words**: the shared brain fills a JSON schema (a short headline, the title, a short
   intro, 13 tags, the row labels). Tags are normalised and topped up from fixed, generic
   ones; then every word is checked (``rules``) - a draft that fails gets ONE redraft with the
   reasons; a second failure blocks that keyword. Everything else in the description is fixed
   text written here, ending with the required notes: ``DIGITAL_NOTE`` and the AI disclosure.
5. **Files**: the workbook (``sheets.build_workbook``), the printable PDF and three photos
   (``render``), all drawn from the workbook itself, staged in ``<etsy_dir>/digital/<slug>/``
   and pinned by SHA-256 in the payload.
6. **Price**: the median price of the keyword's top results as measured, as an x.99 price,
   kept inside ``[price_floor_cents, price_ceiling_cents]``.
"""
from __future__ import annotations

import re
from pathlib import Path

from ...adapters import etsy as etsy_adapter
from ...adapters.etsy import ACTIVATE, CREATE, LISTING_FEE, credentials_problem
from ..figures import Figure
from ..hands import Job
from ..log import log
from ..result import Err, Ok, Result
from ..worker import ErrorKind, WorkContext, make_output, never_raises
from ..workers import _Base
from . import render, rules, sheets
from .common import (
    RETRY_UNREACHABLE,
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

PURPOSE = "etsy digital listing"
HEADLINE = (8, 40)
INTRO = (60, 600)
BLOCK_DAYS = 14
GENERIC_TAGS = ("printable", "digital download", "spreadsheet template", "printable planner",
                "instant download", "planner printable", "tracker template",
                "monthly planner", "organizer", "fillable tracker", "spreadsheet",
                "planner", "template", "tracker")


def normalise_tags(raw, kind: sheets.Kind, keyword: str) -> list[str]:
    """Up to 13 tags in Etsy's form - lower case, a-z 0-9 and single spaces, at most 20
    characters, no repeats - the model's first, then the keyword and the kind's noun, then
    fixed generic ones. Every one is still checked by ``rules`` afterwards."""
    out: list[str] = []

    def add(t) -> None:
        if not isinstance(t, str):
            return
        t = " ".join(re.sub(r"[^a-z0-9 ]+", " ", t.lower()).split())
        if 1 <= len(t) <= rules.TAG_MAX and t not in out and not rules.tag_problems(
                [t], exactly=1):
            out.append(t)

    for t in raw if isinstance(raw, list) else []:
        add(t)
    for t in (keyword, kind.noun, *kind.noun.split(), *GENERIC_TAGS):
        if len(out) >= rules.TAGS_EXACTLY:
            break
        add(t)
    return out[:rules.TAGS_EXACTLY]


def describe(intro: str, kind: sheets.Kind, labels: list[str]) -> str:
    """The whole description: the model's intro, then fixed text, then the required notes."""
    return "\n\n".join((
        intro.strip(),
        "What you get:\n"
        "- A spreadsheet (.xlsx) with three sheets: Tracker (yours to fill in), Example "
        "(sample numbers, so you can see it work) and How to use.\n"
        "- A printable PDF page (US Letter) to fill in by hand.",
        "Ready-made rows:\n" + "\n".join(f"- {label}" for label in labels)
        + "\nRename any of them by typing over it, or use the blank lines on the printable "
        f"page (the {kind.noun} has room for your own).",
        "The spreadsheet uses ordinary formulas (sums, counts and percentages), so the shaded "
        "columns and the totals work themselves out as you type. Open it in a spreadsheet app "
        "that reads .xlsx files.",
        rules.DIGITAL_NOTE,
        rules.AI_DISCLOSURE,
    ))


class DigitalMaker(_Base):
    """``etsy.digital``: Etsy digital-download listings, drafted and submitted for approval."""

    schema = {
        "type": "object",
        "properties": {
            "headline": {"type": "string"},
            "title": {"type": "string"},
            "intro": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "labels": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["headline", "title", "intro", "tags", "labels"],
    }

    def __init__(self, spec, *, daily_cap: int = 2, stage_dir: str | None = None,
                 price_floor_cents: int = 299, price_ceiling_cents: int = 1299,
                 default_price_cents: int = 499) -> None:
        super().__init__(spec)
        for name, v, low, high in (("daily_cap", daily_cap, 0, 10),
                                   ("price_floor_cents", price_floor_cents, 100, 50_000),
                                   ("price_ceiling_cents", price_ceiling_cents, 100, 50_000),
                                   ("default_price_cents", default_price_cents, 100, 50_000)):
            if isinstance(v, bool) or not isinstance(v, int) or not low <= v <= high:
                raise ValueError(f"{name}: a whole number {low}-{high}")
        if price_floor_cents > price_ceiling_cents:
            raise ValueError("price_floor_cents is above price_ceiling_cents")
        self.daily_cap = daily_cap
        self.stage_param = stage_dir
        self.price_floor = price_floor_cents
        self.price_ceiling = price_ceiling_cents
        self.default_price = default_price_cents

    def stage_root(self) -> Path:
        return etsy_dir(self.stage_param) / "digital"

    def readiness(self, secrets_dir) -> str | None:
        return (credentials_problem(Path(secrets_dir) / "etsy.json")
                or sheets.openpyxl_problem())

    # ---- one run -------------------------------------------------------------------------
    @never_raises()
    def run(self, ctx: WorkContext) -> Result:
        if ctx.state_dir is None:
            return self._err(ErrorKind.NOT_CONFIGURED, "no state dir: without its record it "
                             "could list something twice", retryable=False)
        try:
            record = Record(ctx.state_dir, self.worker_id,
                            {"items": [], "counts": {}, "made": {}, "blocked": {}})
        except Unreadable as exc:
            return self._err(ErrorKind.MALFORMED, f"its record is unreadable ({exc}); refusing "
                             "to list anything", retryable=False)
        events: list = []
        self._follow_up(ctx, record, events)
        made = self._make_one(ctx, record, events)
        record.save()
        if isinstance(made, Err):
            if not events:
                return made
            log.warning("%s: nothing new this run: %s", self.worker_id, made.error)
        return Ok((*events, self._tally(ctx, record, made if isinstance(made, str) else None)))

    # ---- following what it submitted -------------------------------------------------------
    def _follow_up(self, ctx: WorkContext, record: Record, events: list) -> None:
        for item in record.doc["items"]:
            status = item.get("status")
            if status == "draft_pending":
                state, got = follow(ctx, item, CREATE)
                if state == "waiting":
                    continue
                if state != "approved":
                    self._settle(ctx, record, item, state, str(got), events)
                    continue
                lid = (got.result or {}).get("listing_id") if isinstance(got.result, dict) \
                    else None
                if not isinstance(lid, int):
                    self._settle(ctx, record, item, "failed", "Pionir said done but gave no "
                                 "listing_id", events)
                    continue
                item.update(status="draft_created", listing_id=lid)
                record.count("drafts_created")
                self._submit_activation(ctx, record, item, events)
            elif status in ("draft_created", "activate_unreachable"):
                self._submit_activation(ctx, record, item, events)
            elif status == "activate_pending":
                state, got = follow(ctx, item, ACTIVATE)
                if state == "waiting":
                    continue
                if state != "approved":
                    self._settle(ctx, record, item, state, str(got), events)
                    continue
                url = (got.result or {}).get("url") if isinstance(got.result, dict) else None
                item.update(status="active", url=url, settled_at=ctx.now)
                record.count("listed")
                events.append(self._event(ctx, "etsy.listed", {
                    "slug": item["slug"], "listing_id": item.get("listing_id"), "url": url,
                    "title": item.get("title")}))

    def _submit_activation(self, ctx: WorkContext, record: Record, item: dict,
                           events: list) -> None:
        if ctx.job is None:
            return
        out = ctx.job(Job(ACTIVATE, {"slug": item["slug"], "listing_id": item["listing_id"],
                                     "title": item["title"], "spend": dict(LISTING_FEE),
                                     "spends_money": True},
                          what=f"make the Etsy listing {item['title']!r} live"))
        if out.status == "pending_approval":
            item.update(status="activate_pending", approval_id=out.approval_id)
        elif out.status == "unreachable":
            item["status"] = "activate_unreachable"
        else:
            self._settle(ctx, record, item, "failed", out.error or out.status, events)

    def _settle(self, ctx: WorkContext, record: Record, item: dict, status: str, why: str,
                events: list) -> None:
        item.update(status=status, why=why[:300], settled_at=ctx.now)
        record.count(status)
        log.warning("%s: %s is %s: %s", self.worker_id, item.get("slug"), status, why)
        events.append(self._event(ctx, "etsy.not_listed", {
            "slug": item.get("slug"), "status": status, "why": why[:200]}))

    # ---- making one new listing ---------------------------------------------------------
    def _make_one(self, ctx: WorkContext, record: Record, events: list):
        rec = record.doc
        # a submission that never reached Pionir is retried before anything new is made
        for item in rec["items"]:
            if item.get("status") == "unreachable" and item.get("tries", 0) < RETRY_UNREACHABLE:
                return self._submit(ctx, record, item, item["payload"], events)
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
        pick = None
        for row in ranking:
            kw = row.get("keyword")
            if row.get("track") != "digital" or not isinstance(kw, str) or kw in rec["made"]:
                continue
            if ctx.now < float(rec["blocked"].get(kw) or 0):
                continue
            if not isinstance(row.get("taxonomy_id"), int) or row.get("score") is None:
                continue
            pick = row
            break
        if pick is None:
            return "nothing in the demand ranking that a workbook can make, not made before"
        kind = sheets.KINDS[pick["kind"]]
        draft, reasons = None, []
        for _attempt in (1, 2):
            got = ctx.words(PURPOSE, self._system(), self._prompt(pick["keyword"], kind,
                                                                  reasons), self.schema)
            if isinstance(got, Err):
                return got
            draft, reasons = self._assemble(got.value, kind, pick["keyword"])
            if not reasons:
                break
            record.count("drafts_blocked")
            log.warning("%s: draft for %r blocked: %s", self.worker_id, pick["keyword"],
                        "; ".join(reasons[:4]))
        if reasons:
            rec["blocked"][pick["keyword"]] = ctx.now + BLOCK_DAYS * 86400
            events.append(self._event(ctx, "etsy.blocked", {
                "keyword": pick["keyword"], "reasons": [r[:120] for r in reasons[:4]]}))
            return f"the draft for {pick['keyword']!r} did not pass the checks"
        price = pick.get("median_price_cents")
        price = ninety_nine(price) if isinstance(price, int) else self.default_price
        price = min(max(price, self.price_floor), self.price_ceiling)
        slug = slug_for(kind.name, pick["keyword"], ctx.now)
        payload = self._stage(slug, kind, draft, price, pick["taxonomy_id"])
        try:
            etsy_adapter.check_create(payload)      # the adapter's own rules, before Pionir
        except ValueError as exc:
            rec["blocked"][pick["keyword"]] = ctx.now + BLOCK_DAYS * 86400
            return self._err(ErrorKind.MALFORMED, f"the listing failed Pionir's check: {exc}",
                             retryable=False)
        rec["made"][pick["keyword"]] = slug
        item = {"slug": slug, "keyword": pick["keyword"], "kind": kind.name,
                "title": draft["title"], "price_cents": price, "submitted_at": ctx.now,
                "payload": payload, "tries": 0}
        rec["items"].append(item)
        return self._submit(ctx, record, item, payload, events)

    def _submit(self, ctx: WorkContext, record: Record, item: dict, payload: dict,
                events: list):
        out = ctx.job(Job(CREATE, payload, what=f"list {item['title']!r} on Etsy as a draft"))
        item["tries"] = int(item.get("tries", 0)) + 1
        if out.status == "pending_approval":
            item.update(status="draft_pending", approval_id=out.approval_id)
            record.count("submitted_for_approval")
            events.append(self._event(ctx, "etsy.pending_approval", {
                "slug": item["slug"], "title": item["title"],
                "price_cents": item["price_cents"], "keyword": item["keyword"]}))
            return f"submitted {item['slug']} for the owner's approval"
        if out.status == "unreachable":
            item["status"] = "unreachable"
            return self._err(ErrorKind.UNAVAILABLE, f"Pionir did not answer: {out.error}")
        self._settle(ctx, record, item, "failed", out.error or out.status, events)
        return f"{item['slug']} was refused: {out.error or out.status}"

    # ---- the words -----------------------------------------------------------------------
    @staticmethod
    def _system() -> str:
        return ("You write Etsy listings for simple, useful spreadsheet templates. Plain, "
                "honest English. Never name a person, place, company, brand, app, product, "
                "character or celebrity, never make a promise or a claim (no guarantees, no "
                "'best', no health, money or results claims), no links, no emoji, no hashtags. "
                "Answer only with the JSON object.")

    @staticmethod
    def _prompt(keyword: str, kind: sheets.Kind, reasons: list) -> str:
        lo, hi = kind.labels
        text = (f"Buyers on Etsy search for: {keyword!r}. We sell a {kind.noun} as a "
                f"spreadsheet with formulas plus a printable PDF.\n"
                f"- headline: {HEADLINE[0]}-{HEADLINE[1]} characters, Title Case, the "
                f"product's name (e.g. 'Monthly Budget Tracker').\n"
                f"- title: an Etsy listing title, 60-140 characters, starting with the "
                f"words buyers search for, phrases separated by ' | '.\n"
                f"- intro: 2-3 sentences, {INTRO[0]}-{INTRO[1]} characters, saying who it is "
                f"for and what it helps them keep track of.\n"
                f"- tags: 13 search phrases, lower case, each at most 20 characters.\n"
                f"- labels: {lo}-{hi} short {kind.label_word} names (1-3 words each) that fit "
                f"{keyword!r}.")
        if reasons:
            text += ("\nYour last answer was refused for: " + "; ".join(reasons[:6])
                     + ". Fix exactly those.")
        return text

    def _assemble(self, words: dict, kind: sheets.Kind, keyword: str) -> tuple[dict, list]:
        """The model's words -> the draft, and every reason it may not be submitted."""
        reasons: list = []
        headline = words.get("headline") if isinstance(words.get("headline"), str) else ""
        headline = " ".join(headline.split())
        if not HEADLINE[0] <= len(headline) <= HEADLINE[1]:
            reasons.append(f"headline: {HEADLINE[0]}-{HEADLINE[1]} characters")
        reasons += rules.text_problems("headline", headline)
        intro = words.get("intro") if isinstance(words.get("intro"), str) else ""
        intro = " ".join(intro.split())
        if not INTRO[0] <= len(intro) <= INTRO[1]:
            reasons.append(f"intro: {INTRO[0]}-{INTRO[1]} characters")
        labels = [" ".join(str(x).split()) for x in (words.get("labels") or [])
                  if isinstance(x, str)]
        labels = list(dict.fromkeys(labels))
        lo, hi = kind.labels
        if not lo <= len(labels) <= hi:
            reasons.append(f"labels: {lo}-{hi} distinct names")
        for i, lab in enumerate(labels):
            reasons += rules.label_problems(f"labels[{i}]", lab)
        title = " ".join(str(words.get("title") or "").split())
        tags = normalise_tags(words.get("tags"), kind, keyword)
        description = describe(intro, kind, labels)
        reasons += rules.listing_problems(title, description, tags, kind="digital")
        return ({"headline": headline, "title": title, "intro": intro, "labels": labels,
                 "tags": tags, "description": description}, reasons)

    # ---- the files -------------------------------------------------------------------------
    def _stage(self, slug: str, kind: sheets.Kind, draft: dict, price: int,
               taxonomy_id: int) -> dict:
        base = re.sub(r"[^a-z0-9]+", "-", draft["headline"].lower()).strip("-")[:40] or kind.name
        xlsx = sheets.build_workbook(kind.name, draft["headline"], draft["labels"])
        pages = render.printable_pages(xlsx, kind.name)
        pdf = render.pdf_bytes(pages)
        files = {f"{base}.xlsx": xlsx, f"{base}-printable.pdf": pdf}
        photos = render.preview_images(xlsx, pages, kind.name, draft["headline"],
                                       [(n, len(b)) for n, b in files.items()])
        write_staged(self.stage_root() / slug, {**files, **{n: b for n, b, _ in photos}})
        return {
            "slug": slug, "title": draft["title"], "description": draft["description"],
            "tags": draft["tags"], "price_cents": price, "taxonomy_id": taxonomy_id,
            "files": [{"name": n, "sha256": sha256(b)} for n, b in files.items()],
            "images": [{"name": n, "sha256": sha256(b), "alt_text": alt}
                       for n, b, alt in photos],
            "ai_disclosure": rules.AI_DISCLOSURE,
            "spend": dict(LISTING_FEE), "spends_money": True,
        }

    # ---- what the leader reads ------------------------------------------------------------
    def _event(self, ctx: WorkContext, kind: str, payload: dict):
        # it carries model-written words (a title), so it backs no figure (grounding.py)
        return make_output(self, kind=kind, valid_at=ctx.now, observed_at=ctx.now,
                           payload=payload, entities=self.entities,
                           provenance={"source": "model", "derived": True,
                                       "checked_by": "etsy.rules"})

    def _tally(self, ctx: WorkContext, record: Record, note: str | None):
        rec = record.doc
        c = rec.get("counts") or {}
        items = rec.get("items") or []

        def n(*states):
            return sum(1 for i in items if i.get("status") in states)

        figures = [
            Figure(int(c.get("submitted_for_approval", 0)), "count",
                   "Etsy drafts submitted for approval", window="all_time"),
            Figure(n("draft_pending", "activate_pending"), "count",
                   "Etsy listings waiting for the owner's approval", window="now"),
            Figure(n("active"), "count", "Etsy listings live", window="all_time"),
            Figure(n("denied"), "count", "Etsy listings denied", window="all_time"),
            Figure(n("failed"), "count", "Etsy listings failed", window="all_time"),
            Figure(int(c.get("drafts_blocked", 0)), "count", "Etsy drafts blocked by the checks",
                   window="all_time"),
            Figure(record.made_today(ctx.now), "count", "Etsy drafts made today",
                   window="today"),
        ]
        return make_output(self, kind="etsy.tally", valid_at=ctx.now, observed_at=ctx.now,
                           payload={"note": note, "daily_cap": self.daily_cap},
                           figures=figures, entities=self.entities,
                           provenance={"source": "real", "provider": self.provider,
                                       "record": "the digital maker's own record"})
