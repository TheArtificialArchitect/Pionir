"""The blog worker: drafts one post for api.dokaz.net and asks the owner to approve it.

The business goal is search traffic to the paid APIs. The owner's rule for AI-written
public content is the whole shape of this file: **the model drafts; a fail-closed check
runs on the exact final text; the owner approves every post.**

One run:

1. **Follow up** every post already waiting on the owner (``ctx.approval``): approved and
   done WITH a URL is published; denied, failed or done-without-a-URL is not; anything
   else is still waiting. Nothing is ever assumed published.
2. **One draft for each of the owner's daily digests** (``draft_due``): the owner reads
   each one by hand, and a flood of them would only teach him to stop reading. A draft is
   due once the digest the last one waited for is out, so every morning's digest holds
   exactly one post. (A bare 24 hours after the last draft drifted a run later every day -
   23:04, 04:45, 10:26 - so some digests got none: the 10:26 draft came an hour after the
   09:00 digest.) With no digest known, it is every ``draft_every_seconds``.
3. **A topic**: the evergreen seed, tied to a real product, that the division's goal as
   Moss set it matches best - otherwise, or with no goal, the next seed in the rotation.
   The goal steers among the seeds; it is never a post's subject itself. A topic or slug
   already used is never used again (the record).
4. **Words from the shared brain only** (``ctx.words``: JSON schema, temperature 0,
   charged to this division). This module imports no model.
5. **The worker repairs, then inserts the links itself.** ``repair`` mends two things the
   model writes against its instructions by a fixed rule: a Markdown link loses its target
   (the anchor text stays, and is checked), and an address on an RFC 2606 documentation
   host (example.com, *.example) becomes ``your-site``; any other URL is left to block.
   What was repaired is recorded (``repaired``). The footer links - the docs page, every
   API, and the paid offer that fits the topic (a live Gumroad product, else /hire) - each
   carry the blog's UTM tags, and only THEN does ``contentcheck.check`` run - on the exact
   dict that would be submitted.
6. **Blocked**: recorded with its reasons, loudly, and NOT submitted. One fresh draft is
   allowed per run, with the reasons in the prompt; a topic blocked on an earlier day
   starts its first draft with its last block's reasons (the model runs at temperature 0,
   so without them it writes the same draft again). **Passed**: submitted as
   ``Job("content.publish", {draft_id, slug, title, description, body_md, tags})``; Pionir
   parks it for the owner, and it is recorded as PENDING APPROVAL - never as published.
   The record keeps that exact payload with the post (``payload``): once published it is
   the post's text, which the dev.to cross-poster (devto.py) copies.

Every count this worker reports is a count of real events it recorded: drafts written,
drafts blocked, posts submitted for approval, posts published.

The machinery of steps 1-6 is ``DailyPoster``, shared with the Instagram worker
(instagram.py); ``BlogWorker`` says only what a blog post is, how it is checked and where it
is published.
"""
from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlsplit

from pionir import atomic

from . import contentcheck
from .figures import Figure
from .hands import Job, outcome_of
from .log import log
from .result import Err, Ok, Result
from .worker import ErrorKind, WorkContext, make_output, never_raises
from .workers import _Base

CAPABILITY = "content.publish"
SITE = "https://api.dokaz.net"
MAX_SLUG = 50              # "YYYY-MM-DD-" + slug stays inside draft_id's 64
MAX_TOPIC_BLOCKS = 3       # days a topic may be blocked before it is retired
DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "slug": {"type": "string"},
        "description": {"type": "string"},
        "body_md": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "slug", "description", "body_md", "tags"],
}
# An approval Pionir no longer lists after this long is no longer counted as waiting:
# Pionir auto-denies a parked action after a day and forgets resolved ones after a week.
FORGOTTEN_AFTER = 8 * 86400


@dataclass(frozen=True)
class Topic:
    key: str
    subject: str           # what the post is about, in words
    product: str           # the real product it leads to (an allowlisted name)
    does: str              # what that product really does (Scrooge's own product summary)
    words: tuple           # for matching a goal
    path: str              # the docs page the post links to
    label: str             # that link's text


# Evergreen topics, each tied to a real product and to a docs page that exists
# (C:\src\Scrooge\worker\src\products\*.ts and docs.ts).
SEEDS = (
    Topic("invoice-pdf-from-json", "generating invoice, estimate and receipt PDFs from JSON "
          "without a PDF library", "Invoice PDF", "turns a JSON description of an invoice, "
          "estimate or receipt into a clean multi-page PDF", ("invoice", "invoices", "pdf",
          "receipt", "estimate", "billing"), "/docs/invoice-pdf-api", "Invoice PDF API guide"),
    Topic("verify-email-before-sending", "checking an email address before you send to it: "
          "syntax, MX records and typos", "Email Verify", "checks syntax, MX records, "
          "disposable domains, role accounts and typos, and scores an address from 0 to 100",
          ("email", "emails", "verify", "verification", "mx", "bounce", "typo"),
          "/docs/email-verification-api", "Email Verify API guide"),
    Topic("disposable-email-detection", "spotting disposable and throwaway email addresses at "
          "sign-up", "Email Verify", "flags disposable domains and role accounts",
          ("email", "disposable", "throwaway", "signup", "spam"),
          "/docs/disposable-email-detection-api", "Email Verify guide to disposable addresses"),
    Topic("qr-codes-from-an-api", "generating QR codes as SVG or PNG from an API", "QR Code API",
          "generates QR codes as SVG or PNG, with custom colours and error-correction levels",
          ("qr", "code", "codes", "svg", "png"), "/docs/qr-code-api", "QR Code API guide"),
    Topic("wifi-qr-codes", "making a QR code that joins a Wi-Fi network", "QR Code API",
          "encodes Wi-Fi credentials as a QR code a phone camera can join",
          ("qr", "wifi", "wi-fi", "network"), "/docs/wifi-qr-code-api",
          "QR Code API guide to Wi-Fi codes"),
    Topic("vcard-qr-codes", "putting contact details in a QR code with vCard", "QR Code API",
          "encodes a vCard as a QR code", ("qr", "vcard", "contact", "card"),
          "/docs/vcard-qr-code-api", "QR Code API guide to vCard codes"),
    Topic("detect-website-technology", "finding out what technology a website is built with",
          "Site Intel", "reads a page's title, meta tags, technology stack and analytics tags",
          ("site", "website", "tech", "technology", "stack", "seo"),
          "/docs/website-technology-detection-api", "Site Intel guide to technology detection"),
    Topic("site-meta-and-open-graph", "checking a page's title, description and social preview "
          "tags", "Site Intel", "reads a page's title, meta description and preview tags",
          ("site", "website", "meta", "preview", "seo"),
          "/docs/website-technology-detection-api", "Site Intel API guide"),
    Topic("summarize-text-api", "summarising long text into a few sentences or bullets",
          "Text AI", "summarises text in a set number of sentences, as a paragraph or bullets",
          ("text", "summary", "summarize", "summarise"), "/docs/text-summarization-api",
          "Text AI guide to summaries"),
    Topic("sentiment-analysis-api", "scoring the sentiment of reviews and messages",
          "Text AI", "scores text as positive or negative", ("text", "sentiment", "review",
          "reviews"), "/docs/sentiment-analysis-api", "Text AI guide to sentiment"),
    Topic("keyword-extraction-api", "pulling keywords out of a piece of text", "Text AI",
          "extracts the keywords from a text", ("text", "keyword", "keywords", "tags"),
          "/docs/keyword-extraction-api", "Text AI guide to keywords"),
    Topic("classify-text-without-training", "sorting text into your own categories without "
          "training a model", "Text AI", "classifies text into labels you choose",
          ("text", "classify", "classification", "labels", "categories"),
          "/docs/zero-shot-text-classification-api", "Text AI guide to classification"),
    Topic("rewrite-text-tone", "rewriting text in a different tone", "Text AI",
          "rewrites text as professional, casual, concise, friendly or formal",
          ("text", "rewrite", "tone"), "/docs/rewrite-text-tone-api",
          "Text AI guide to rewriting"),
    Topic("call-web-apis-from-python", "calling small web APIs from Python", "Dokaz",
          "sells small paid web APIs: invoice PDFs, email verification, QR codes, site checks "
          "and text utilities", ("python", "code", "developer"), "/docs/quickstart-python",
          "Python quickstart"),
)

SYSTEM = """You write one blog post for the Dokaz website, which sells small paid web APIs. \
The post helps a developer or a small business owner with a real task and, where it fits, \
shows how the named product does it. Answer with ONE JSON object: title, slug, \
description, body_md, tags. Nothing else.

Hard rules. A draft that breaks any one of them is thrown away unread:
- Markdown only: no HTML tags, no HTML comments, no angle brackets at all.
- Only ## and ### headings, paragraphs, - or 1. lists, **bold**, *italic*, `code` and ```
  fences. No # heading and nothing deeper than ###: no #### headings. No tables, no > \
quotes, no --- rules, no images.
- Write NO links, NO URLs and NO website or domain names. The links are added for you.
- Name no person, place, city, country, company, customer or website. The only names you \
may write with a capital letter are: {names}. Every other word is lower case unless it \
starts a sentence.
- Write an acronym as the acronym (SVG, JSON, PDF) and never spell it out: write SVG, \
never the words it stands for. Use only acronyms from the names above.
- Title and headings in sentence case: only the first word capitalised.
- No numbers about the business: no counts of customers, users or sales, no revenue, no \
prices, no money amounts. Never write "we made", "we earned", "we sold" or "our revenue".
- No email addresses, phone numbers, IP addresses or street addresses, not even made-up \
examples.
- Never write the @ character. Say "an email address", never an example of one.
- Invent NO example data at all: no sample people, companies, addresses, invoice or order \
numbers, dates or IDs. Describe a field by what it holds ("the customer's name", "the \
invoice number"), never by an example value.
- When an example needs an id, a name, a company, an address, an email address or a \
handle, write a placeholder in curly braces, lower case with hyphens, such as \
{{invoice-number}}, {{company-name}} or {{customer-email}}, instead of inventing one.
- Code is optional. If you show any, show only a JSON request body in a fenced block, \
with lower-case keys, placeholders as values, and no keys, tokens, headers or URLs.
- title: 10 to 120 characters. description: one or two sentences, 60 to 280 characters. \
body_md: 400 to 1200 words. slug: a few lower-case words joined by hyphens. tags: up to \
five lower-case words, hyphens instead of spaces."""


@dataclass(frozen=True)
class Offer:
    """A paid offer a post's footer points to: a page where money really changes hands."""
    url: str               # without a query: the worker adds the post's UTM tags
    label: str             # the link's text (checked like every other word)


# "I build it for you": the contract-work page on api.dokaz.net (Scrooge worker/src/hire.ts).
HIRE = Offer(f"{SITE}/hire", "have us build it for you")
# The most relevant paid product for a topic, where one exists. Only products that are
# really on sale: the Gumroad listing (product.gumroad_list, 2026-10-04) has obol-pro,
# metron, approval-gate, card-press and post-guard live. A topic none of them serves gets
# HIRE. A product taken off sale must be taken out of here in the same change.
OFFERS = {
    "invoice-pdf-from-json": Offer("https://dokaz.gumroad.com/l/obol-pro",
                                   "Obol on Gumroad: invoices and estimates with no "
                                   "subscription"),
}


def offer_for(topic: Topic) -> Offer:
    """The paid offer a post on this topic points to: its product, or else /hire."""
    return OFFERS.get(topic.key, HIRE)


# ---- repairing the model's words before they are assembled ----------------------------
# Two things the model writes against its instructions that a fixed rule can mend without
# loosening the check: a Markdown link (its anchor text stays and is checked like any other
# words; only the target goes), and an address on a host RFC 2606 / 6761 reserves for
# documentation (example.com/.org/.net and their subdomains, the .example TLD), which can
# never be anyone's site. Every other URL, domain or address is left exactly as written,
# so it still blocks. The full check then runs on the assembled post.
_MD_LINK = re.compile(r"(?<!!)\[([^\[\]\n]+)\]\(\s*<?[^)\s>]*>?(?:\s+\"[^\"\n]*\")?\s*\)")
_RESERVED_HOST = (r"(?:(?:[a-z0-9-]+\.)*example\.(?:com|org|net)|(?:[a-z0-9-]+\.)+example)"
                  r"(?![\w-]|\.[a-z0-9])")
_RESERVED_URL = re.compile(rf"(?i)\b[a-z][a-z0-9+.-]*://{_RESERVED_HOST}(?::\d+)?"
                           r"(?:[/?#][^\s<>()\[\]{}\"'`]*)?")
# not after "@" (user@example.com is a reserved address the check already allows) nor
# inside a longer host or path
_RESERVED_BARE = re.compile(rf"(?i)(?<![\w@./:-]){_RESERVED_HOST}")
PLACEHOLDER_SITE = "your-site"
REPAIRED = "repaired"      # the draft's (and the record's) note of what was repaired


def repair(field: str, text: str) -> tuple:
    """(text, what was repaired) - see above. Deterministic; never adds anything."""
    done: list = []

    def unlink(m: re.Match) -> str:
        done.append(f"{field}: unlinked {_clip(m.group(1), 60)!r}")
        return m.group(1)

    def reserved(m: re.Match) -> str:
        found = m.group(0)
        tail = len(found) - len(found.rstrip(".,;:!?"))
        found, rest = found[:len(found) - tail], found[len(found) - tail:]
        done.append(f"{field}: {_clip(found, 60)!r} -> {PLACEHOLDER_SITE!r}")
        return PLACEHOLDER_SITE + rest

    text = _MD_LINK.sub(unlink, text)
    text = _RESERVED_URL.sub(reserved, text)
    text = _RESERVED_BARE.sub(reserved, text)
    return text, done


# A reason about a link names the hosts a post may link to; echoed back, it reads as an
# invitation to link to them. Every link in a post is the worker's, so the model is told
# plainly to write none. Only reasons ABOUT a link (contentcheck's "link '...'", Pionir's
# "links may only go to ..."): Pionir's raw-HTML reason also mentions links, and is kept.
_LINK_REASON = re.compile(r"(?i)\blink '|\blinks (?:may|must|cannot)\b|bare www|"
                          r"names the website|bare address")
NO_LINKS = ("you wrote a URL, a link or a website name: remove every URL and link; "
            "write none")


def slugify(text: str, limit: int = MAX_SLUG) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return s[:limit].strip("-")


def _day(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now))


def _clip(s, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 3] + "..."


def result_links(result) -> list:
    """Every ``url`` / ``published_url`` / ``permalink`` string in Pionir's result, shallow
    first. A caller decides which of them, if any, is really the post's address."""
    found: list = []

    def walk(obj, depth: int) -> None:
        if depth > 4:
            return
        if isinstance(obj, dict):
            for key in ("url", "published_url", "permalink"):
                if isinstance(obj.get(key), str):
                    found.append(obj[key])
            for v in obj.values():
                walk(v, depth + 1)
        elif isinstance(obj, list):
            for v in obj[:20]:
                walk(v, depth + 1)

    walk(result, 0)
    return found


def published_url(result, slug: str) -> str | None:
    """The post's public URL from Pionir's result, or None. Only an https URL on a Dokaz
    host that carries the post's slug counts: a done job with no such URL is not a
    published post."""
    for url in result_links(result):
        try:
            parts = urlsplit(url)
        except ValueError:
            continue
        if parts.scheme == "https" and (parts.hostname or "") in contentcheck.LINK_HOSTS \
                and slug and slug in parts.path:
            return url
    return None


class _Unreadable(ValueError):
    pass


def record_path(state_dir: Path, worker_id: str) -> Path:
    """Where a posting worker keeps its record: ``<state_dir>/<worker_id>.json``."""
    return Path(state_dir) / f"{worker_id}.json"


def read_record(state_dir: Path, worker_id: str) -> dict | None:
    """Another worker's record, read-only: None when there is none yet (it has not run),
    ``_Unreadable`` when it cannot be read. Never written from here: only its own worker
    writes it."""
    path = record_path(state_dir, worker_id)
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _Unreadable(f"{path}: {exc}") from exc
    if not isinstance(doc, dict):
        raise _Unreadable(f"{path} is not an object")
    return doc


def save_record(path: Path, rec: dict) -> None:
    """Atomic: a half-written record is never read back. On Windows the swap fails while
    another thread has the old file open for a moment (a sibling worker reading it), so it
    is retried briefly rather than losing a record of something already submitted."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, indent=1, sort_keys=True), encoding="utf-8")
    atomic.replace(tmp, path)


def published_posts(rec: dict | None) -> list:
    """The posts a posting record says are published, oldest first (by when that was
    settled). Only ``status == "published"`` counts: pending, denied or failed is not."""
    posts = [p for p in ((rec or {}).get("posts") or [])
             if isinstance(p, dict) and p.get("status") == "published"]
    return sorted(posts, key=lambda p: (float(p.get("settled_at") or 0),
                                        float(p.get("submitted_at") or 0),
                                        str(p.get("draft_id") or "")))


class DailyPoster(_Base):
    """What every posting worker shares: one draft a day at most, words from the shared
    brain only, a fail-closed check on the exact payload, submission to Pionir (which parks
    it for the owner), and an honest record of what became of each post.

    A subclass says what it drafts and how: ``capability``, ``purpose``, ``schema``, and the
    hooks ``_system``, ``_prompt``, ``assemble``, ``check_draft``, ``published_link``,
    ``_describe`` and ``_job`` (plus ``_before_submit`` / ``_after_submit`` if it keeps
    anything of its own). Everything else - the cadence, the record, the follow-up, the
    one redraft, the topic rule and the counts - is here, once."""

    capability = ""                 # the Pionir capability a passed draft is submitted as
    purpose = ""                    # what ctx.words is asked for (the brain's ledger)
    schema: ClassVar[dict] = {}     # the JSON schema the words must fill
    title_field = "title"           # the draft's one-line name, for the record and events
    link_field = "url"              # where a published post's public address is recorded
    link_missing = "gave no URL for the post"
    record_what = "this worker's own event counts"

    def __init__(self, spec, *, draft_every_seconds: int = 86400) -> None:
        super().__init__(spec)
        if isinstance(draft_every_seconds, bool) or not isinstance(draft_every_seconds, int) \
                or draft_every_seconds <= 0:
            raise ValueError("draft_every_seconds is a positive whole number of seconds")
        self.draft_every_seconds = draft_every_seconds

    # ---- the record: what it drafted, what it used, what became of each post ----------
    def record_path(self, state_dir: Path) -> Path:
        return record_path(state_dir, self.worker_id)

    def _blank(self) -> dict:
        return {"last_drafted_at": None, "used_topics": [], "posts": [], "blocked": [],
                "counts": {}}

    def load(self, state_dir: Path) -> dict:
        doc = read_record(state_dir, self.worker_id)
        blank = self._blank()
        return blank if doc is None else {**blank, **doc}

    def save(self, state_dir: Path, rec: dict) -> None:
        save_record(self.record_path(state_dir), rec)

    @staticmethod
    def _count(rec: dict, what: str, n: int = 1) -> None:
        rec["counts"][what] = int(rec["counts"].get(what, 0)) + n

    # ---- one run -------------------------------------------------------------------------
    @never_raises()
    def run(self, ctx: WorkContext) -> Result:
        if ctx.state_dir is None:
            return self._err(ErrorKind.NOT_CONFIGURED, "no state dir: the worker cannot keep "
                             "its record, and without it could repeat a slug", retryable=False)
        try:
            rec = self.load(ctx.state_dir)
        except _Unreadable as exc:
            return self._err(ErrorKind.MALFORMED, f"its record is unreadable ({exc}); "
                             "refusing to draft, since it could repeat a topic or slug",
                             retryable=False)
        events: list = []
        if rec.get("first_run_at") is None:
            rec["first_run_at"] = _first_seen(rec, ctx.now)
        rec["last_run_at"] = ctx.now
        events += self._follow_up(ctx, rec)
        drafted: Result | None = None
        if self.draft_due(ctx.now, rec.get("last_drafted_at"), ctx.digest):
            drafted = self._draft_and_submit(ctx, rec, events)
        self.save(ctx.state_dir, rec)
        if isinstance(drafted, Err):
            if not events:
                return drafted
            log.warning("%s: no draft this run: %s", self.worker_id, drafted.error)
        return Ok((*events, self._tally(ctx, rec)))

    # ---- 2. when: one draft for each of the owner's daily digests ---------------------------
    @staticmethod
    def _digest_on(digest) -> bool:
        return digest is not None and bool(getattr(digest, "enabled", False))             and callable(getattr(digest, "next_digest", None))

    @staticmethod
    def _digest_of(t: float, digest) -> datetime:
        """The digest a post parked at ``t`` waits for - Pionir's server stamps each batched
        approval with exactly this (``DigestSettings.next_digest`` of its local time)."""
        return digest.next_digest(datetime.fromtimestamp(float(t)).astimezone())

    def draft_due(self, now: float, last, digest=None) -> bool:
        """Is a draft due? Never drafted: yes. With the owner's digest on: when the digest a
        draft made now would wait for has none yet - one post in every digest, drafted as
        early as it can be, so a brain that is busy for a run or two still makes it. Without
        one: ``draft_every_seconds`` after the last."""
        if last is None:
            return True
        if self._digest_on(digest):
            return self._digest_of(last, digest) < self._digest_of(now, digest)
        return now - float(last) >= self.draft_every_seconds

    def next_draft_at(self, now: float, last, digest=None) -> float:
        """When the next draft is due (``now`` when it already is)."""
        if self.draft_due(now, last, digest):
            return now
        if self._digest_on(digest):
            return self._digest_of(last, digest).timestamp()     # once that digest is out
        return float(last) + self.draft_every_seconds

    def output_window_seconds(self) -> int:
        """How long this worker may go without submitting a post before that is an alarm:
        a day's post, plus the widest gap between its runs and a second chance at it."""
        return int(self.draft_every_seconds + 2 * math.ceil(self.cadence_seconds * 1.15))

    # ---- its pulse: what it has REALLY produced, for doctor and the vitals ----------------
    def pulse(self, state_dir: Path | None, now: float, digest=None) -> dict:
        """This worker's output counter, from its own record - never from whether its runs
        succeeded: a run that drafts, is blocked and returns a tally is a success that
        produced nothing for the owner. ``alert`` is set when no post has reached the owner
        within ``output_window_seconds`` (or it has nothing left to draft)."""
        out: dict = {"worker": self.worker_id, "capability": self.capability}
        if state_dir is None:
            return {**out, "alert": "no state dir: it cannot keep its record"}
        try:
            rec = self.load(state_dir)
        except _Unreadable as exc:
            return {**out, "alert": f"its record is unreadable ({exc})"}
        c = rec.get("counts") or {}
        posts = [p for p in rec.get("posts") or [] if isinstance(p, dict)]
        reached = [float(p["submitted_at"]) for p in posts
                   if isinstance(p.get("submitted_at"), (int, float))
                   and p.get("status") != "unreachable"]
        published = [float(p.get("settled_at") or 0) for p in posts
                     if p.get("status") == "published"]
        blocked = [b for b in rec.get("blocked") or [] if isinstance(b, dict)]
        last = rec.get("last_drafted_at")
        window = self.output_window_seconds()
        topics_left = sum(1 for t in SEEDS if t.key not in (rec.get("used_topics") or []))
        out.update({
            "last_run_at": rec.get("last_run_at"),
            "last_drafted_at": last,
            "next_draft_at": self.next_draft_at(now, last, digest),
            "drafts_written": int(c.get("drafts_written", 0)),
            "drafts_blocked": int(c.get("drafts_blocked", 0)),
            "submitted": int(c.get("submitted_for_approval", 0)),
            "pending": sum(1 for p in posts if p.get("status") == "pending_approval"),
            "published": int(c.get("published", 0)),
            "denied": int(c.get("denied", 0)),
            "last_submitted_at": max(reached) if reached else None,
            "last_published_at": max(published) if published else None,
            "last_block_reasons": [_clip(r, 120) for r in (blocked[-1].get("reasons") or [])[:3]]
            if blocked else [],
            "topics_left": topics_left,
            "window_s": window,
            "alert": None,
        })
        since = out["last_submitted_at"]
        start = since if since is not None else float(
            rec.get("first_run_at") or _first_seen(rec, now))
        if topics_left == 0 and not out["pending"]:
            out["alert"] = ("has used every topic and will draft nothing more until a new "
                            "seed topic is added")
        elif now - start > window:
            hours = round((now - start) / 3600)
            what = (f"its last post reached the owner {hours} h ago" if since is not None
                    else f"no post has EVER reached the owner ({hours} h since it first ran)")
            out["alert"] = (f"{what}: {out['drafts_written']} drafts written, "
                            f"{out['drafts_blocked']} blocked by the content check, "
                            f"{out['submitted']} submitted for approval. Its runs succeed, "
                            "so nothing else says so")
        return out

    # ---- 1. what became of the posts already waiting -------------------------------------
    def _follow_up(self, ctx: WorkContext, rec: dict) -> list:
        events = []
        for post in rec["posts"]:
            if post.get("status") != "pending_approval" or not post.get("approval_id"):
                continue
            if ctx.approval is None:
                break
            got = ctx.approval(post["approval_id"])
            state = (got or {}).get("status")
            post["checked_at"] = ctx.now
            if state in ("pending", "running", "unreachable"):
                continue
            if state == "unknown":
                if ctx.now - float(post.get("submitted_at") or ctx.now) > FORGOTTEN_AFTER:
                    self._settle(ctx, rec, post, "unknown", "Pionir no longer lists this "
                                 "approval; it was never seen published", events)
                continue
            if state == "denied":
                self._settle(ctx, rec, post, "denied",
                             f"the owner did not approve it ({got.get('reason') or 'denied'})",
                             events)
            elif state == "approved":
                self._settle_done(ctx, rec, post,
                                  outcome_of(self.capability, got.get("result")), events)
            elif state == "approved_failed":
                out = outcome_of(self.capability, got.get("result"))
                self._settle_failed(ctx, rec, post, out.error or "the publish failed",
                                    got.get("result"), events)
            else:
                log.warning("%s: approval %s has a status nobody knows (%r); still waiting",
                            self.worker_id, post["approval_id"], state)
        return events

    def _settle_done(self, ctx: WorkContext, rec: dict, post: dict, out, events: list) -> None:
        if not out.ran:
            self._settle(ctx, rec, post, "failed", out.error or f"Pionir said {out.status}",
                         events)
            return
        link = self.published_link(out.result, post)
        if link is None:
            self._settle(ctx, rec, post, "unconfirmed", f"Pionir said done but "
                         f"{self.link_missing}; NOT counted as published", events)
            return
        post.update({"status": "published", self.link_field: link, "settled_at": ctx.now})
        self._count(rec, "published")
        log.info("%s: published %s", self.worker_id, link)
        events.append(self._event(ctx, "post.published", {
            "draft_id": post["draft_id"], self.link_field: link,
            self.title_field: _clip(post.get(self.title_field), 90)}))

    def _settle_failed(self, ctx: WorkContext, rec: dict, post: dict, why: str, raw,
                       events: list) -> None:
        """Pionir said it did not work (``raw`` is its answer, when there is one). A
        subclass may know a refusal that is really an answer (``DevtoWorker``)."""
        self._settle(ctx, rec, post, "failed", why, events)

    def _settle(self, ctx: WorkContext, rec: dict, post: dict, status: str, why: str,
                events: list) -> None:
        post.update(status=status, why=why, settled_at=ctx.now)
        self._count(rec, status)
        log.warning("%s: %s is %s: %s", self.worker_id, post["draft_id"], status, why)
        events.append(self._event(ctx, "post.not_published", {
            "draft_id": post["draft_id"], "status": status, "why": _clip(why, 160)}))

    # ---- 2-6. one draft, checked, and submitted if it passed ------------------------------
    def choose_topic(self, rec: dict, goal: str | None) -> Topic | None:
        """The free seed the goal's words match best; with no goal, or a goal that matches
        no seed, the next seed in the rotation. The goal only steers among the seeds: it is
        an instruction to the division, never a post's subject (Moss's "report only what the
        workers measured ... publish one good post a day" once would have been one)."""
        used = set(rec["used_topics"])
        free = [t for t in SEEDS if t.key not in used]
        if goal:
            words = set(re.findall(r"[a-z0-9-]+", goal.lower()))
            scored = sorted(((len(words & set(t.words)), i, t) for i, t in enumerate(free)),
                            key=lambda x: (-x[0], x[1]))
            if scored and scored[0][0] > 0:
                return scored[0][2]
        return free[0] if free else None

    def _draft_and_submit(self, ctx: WorkContext, rec: dict, events: list) -> Result | None:
        if ctx.words is None:
            return self._err(ErrorKind.NOT_CONFIGURED, "no brain: a post's words come only "
                             "from the shared brain", retryable=False)
        if ctx.job is None:
            return self._err(ErrorKind.NOT_CONFIGURED, "no hands: a post reaches the owner "
                             "only through Pionir", retryable=False)
        topic = self.choose_topic(rec, ctx.goal)
        if topic is None:
            log.warning("%s: every topic has been used; nothing to draft until a goal or a "
                        "new seed topic is given", self.worker_id)
            return None
        # The model runs at temperature 0: a topic blocked on an earlier day, drafted again
        # with no reasons, comes back as the same draft and the same block. Its first draft
        # today already knows why its last one was thrown away.
        reasons: list = self._last_block_reasons(rec, topic)
        submitted = False
        for attempt in (1, 2):      # one fresh draft per run, at most, after a block
            got = ctx.words(self.purpose, self._system(),
                            self._prompt(topic, ctx.goal, reasons), self.schema)
            if isinstance(got, Err):
                if attempt == 1:
                    return got          # no words came: nothing was drafted, the day is not used
                break
            rec["last_drafted_at"] = ctx.now
            self._count(rec, "drafts_written")
            draft = self.assemble(got.value, topic, rec, ctx.now)
            reasons = self.check_draft(draft)
            if reasons:
                self._blocked(ctx, rec, topic, draft, reasons, attempt, events)
                continue
            self._submit(ctx, rec, topic, draft, events)
            submitted = True
            break
        # A topic is used up by a submitted post, not by a blocked day: with a strict check,
        # burning a topic per block would exhaust the seeds having published nothing. A topic
        # blocked on MAX_TOPIC_BLOCKS days is retired, loudly - it is not going to work.
        blocks = rec.setdefault("topic_blocks", {})
        if not submitted and reasons:
            blocks[topic.key] = int(blocks.get(topic.key, 0)) + 1
            if blocks[topic.key] >= MAX_TOPIC_BLOCKS:
                log.warning("%s: topic %r blocked on %d days; retiring it", self.worker_id,
                            topic.key, blocks[topic.key])
        if (submitted or blocks.get(topic.key, 0) >= MAX_TOPIC_BLOCKS) \
                and topic.key not in rec["used_topics"]:
            rec["used_topics"].append(topic.key)
        return None

    @staticmethod
    def _last_block_reasons(rec: dict, topic: Topic) -> list:
        """The reasons this topic's most recent blocked draft was thrown away, or none."""
        for entry in reversed(rec.get("blocked") or []):
            if isinstance(entry, dict) and entry.get("topic") == topic.key:
                return [r for r in entry.get("reasons") or [] if isinstance(r, str)]
        return []

    # ---- what a subclass says about its own kind of post ----------------------------------
    def _system(self) -> str:
        raise NotImplementedError

    def _prompt(self, topic: Topic, goal: str | None, reasons: list) -> str:
        raise NotImplementedError

    def assemble(self, raw, topic: Topic, rec: dict, now: float) -> dict:
        """The exact payload that would be submitted, built from the model's words."""
        raise NotImplementedError

    def check_draft(self, draft: dict) -> list:
        """Every reason the draft may not go out; empty is the only pass."""
        raise NotImplementedError

    def published_link(self, result, post: dict) -> str | None:
        """The post's public address from Pionir's result, or None: not published."""
        raise NotImplementedError

    def _describe(self, draft: dict) -> dict:
        """The fields the record keeps about a draft, beside its id and topic."""
        raise NotImplementedError

    def _job(self, draft: dict) -> Job:
        raise NotImplementedError

    def _before_submit(self, ctx: WorkContext, rec: dict, draft: dict, post: dict) -> None:
        """Anything kept locally about a passed draft before it goes to Pionir."""

    def _after_submit(self, ctx: WorkContext, rec: dict, draft: dict) -> None:
        """Anything the record must remember once a draft went to Pionir."""

    # ---- blocked, or submitted -----------------------------------------------------------
    def _blocked(self, ctx: WorkContext, rec: dict, topic: Topic, draft: dict, reasons: list,
                 attempt: int, events: list) -> None:
        self._count(rec, "drafts_blocked")
        rec["blocked"] = (rec["blocked"] + [{
            "draft_id": draft["draft_id"], **self._describe(draft), "topic": topic.key,
            "reasons": reasons, "attempt": attempt, "at": ctx.now}])[-100:]
        log.warning("%s: draft %s BLOCKED by the content check and NOT submitted (attempt %d): "
                    "%s", self.worker_id, draft["draft_id"], attempt, "; ".join(reasons))
        events.append(self._event(ctx, "post.blocked", {
            "draft_id": draft["draft_id"], "attempt": attempt, "reasons_total": len(reasons),
            "reasons": [_clip(r, 90) for r in reasons[:3]]}))

    def _submit(self, ctx: WorkContext, rec: dict, topic: Topic, draft: dict,
                events: list) -> None:
        post = {"draft_id": draft["draft_id"], **self._describe(draft), "topic": topic.key}
        self._submit_post(ctx, rec, post, draft, events)

    def _submit_post(self, ctx: WorkContext, rec: dict, post: dict, draft: dict,
                     events: list) -> None:
        """Send one checked draft to Pionir and record what it said. ``post`` is the
        record's entry for it, already describing the draft."""
        self._before_submit(ctx, rec, draft, post)
        out = ctx.job(self._job(draft))
        self._after_submit(ctx, rec, draft)
        post.update(submitted_at=ctx.now, status=out.status, task_id=out.task_id,
                    approval_id=out.approval_id)
        rec["posts"].append(post)
        if out.status == "pending_approval":
            self._count(rec, "submitted_for_approval")
            log.info("%s: %s submitted; PENDING the owner's approval (approval %s)",
                     self.worker_id, draft["draft_id"], out.approval_id)
            events.append(self._event(ctx, "post.pending_approval", {
                "draft_id": draft["draft_id"],
                self.title_field: _clip(draft[self.title_field], 90),
                "approval_id": out.approval_id}))
        elif out.status == "done":
            # Pionir ran it without parking it: the owner did NOT approve this post. That
            # breaks his rule, and it is Pionir's gate that must hold it - say so loudly.
            log.error("%s: %s ran WITHOUT the owner's approval for %s; the capability must "
                      "be approval-gated in Pionir", self.worker_id, self.capability,
                      draft["draft_id"])
            post["approved_by_owner"] = False
            self._settle_done(ctx, rec, post, out, events)
        elif out.status == "failed":
            self._settle_failed(ctx, rec, post, out.error or "Pionir said failed", None,
                                events)
        else:
            # unreachable or still running: not published, and never assumed to be
            self._settle(ctx, rec, post, out.status if out.status != "running" else "unknown",
                         out.error or f"Pionir said {out.status}", events)

    # ---- what the leader reads --------------------------------------------------------------
    def _event(self, ctx: WorkContext, kind: str, payload: dict):
        # it carries model-written words (a title, a slug), so it backs no figure and vouches
        # for no name (grounding.py)
        return make_output(self, kind=kind, valid_at=ctx.now, observed_at=ctx.now,
                           payload=payload, entities=self.entities,
                           provenance={"source": "model", "derived": True,
                                       "checked_by": "contentcheck"})

    def _tally(self, ctx: WorkContext, rec: dict):
        c = rec["counts"]
        pending = [p["draft_id"] for p in rec["posts"] if p.get("status") == "pending_approval"]
        figures = [
            Figure(int(c.get("drafts_written", 0)), "count", "drafts written", window="all_time"),
            Figure(int(c.get("drafts_blocked", 0)), "count", "drafts blocked", window="all_time"),
            Figure(int(c.get("submitted_for_approval", 0)), "count",
                   "posts submitted for approval", window="all_time"),
            Figure(len(pending), "count", "posts pending approval", window="now"),
            Figure(int(c.get("published", 0)), "count", "posts published", window="all_time"),
            Figure(int(c.get("denied", 0)), "count", "posts denied", window="all_time"),
        ]
        wait = max(0, round(self.next_draft_at(ctx.now, rec.get("last_drafted_at"),
                                               ctx.digest) - ctx.now))
        return make_output(self, kind="post.tally", valid_at=ctx.now, observed_at=ctx.now,
                           payload={"pending_approval": pending[-3:],
                                    "next_draft_in_hours": round(wait / 3600, 1),
                                    "topics_left": sum(1 for t in SEEDS
                                                       if t.key not in rec["used_topics"])},
                           figures=figures, entities=self.entities,
                           provenance={"source": "real", "provider": self.provider,
                                       "record": self.record_what})


def _first_seen(rec: dict, now: float) -> float:
    """The earliest moment a record shows its worker at work (``now`` for a new one): a
    record written before ``first_run_at`` existed still dates the worker's first day."""
    seen = [now]
    for key in ("last_drafted_at", "last_run_at"):
        if isinstance(rec.get(key), (int, float)):
            seen.append(float(rec[key]))
    for entry in list(rec.get("posts") or []) + list(rec.get("blocked") or []):
        if isinstance(entry, dict):
            for key in ("submitted_at", "at"):
                if isinstance(entry.get(key), (int, float)):
                    seen.append(float(entry[key]))
    return min(seen)


class BlogWorker(DailyPoster):
    """``posting.blog``: one checked draft a day, submitted for the owner's approval."""

    capability = CAPABILITY
    purpose = "blog_draft"
    schema = DRAFT_SCHEMA
    record_what = "the blog worker's own event counts"

    def _blank(self) -> dict:
        return {**super()._blank(), "used_slugs": []}

    def _system(self) -> str:
        return SYSTEM.format(names=self._names())

    @staticmethod
    def _names() -> str:
        return ", ".join(sorted({n for n in _allowlist_display()}))

    @staticmethod
    def _prompt(topic: Topic, goal: str | None, reasons: list) -> str:
        lines = [f"Topic: {topic.subject}.",
                 f"Product to mention where it fits: {topic.product}, which {topic.does}."]
        if goal:
            lines.append(f"The goal for this work, for context only: {goal.strip()}")
        if reasons:
            lines.append("Your previous draft was thrown away for these reasons. Write a new "
                         "draft that breaks none of them:")
            # every link reason becomes one plain instruction: the allowed hosts it names
            # are for the worker's own links, never an invitation to the model
            said = [NO_LINKS if _LINK_REASON.search(r) else r for r in reasons]
            lines += [f"- {r}" for r in list(dict.fromkeys(said))[:12]]
        return "\n".join(lines)

    def assemble(self, raw, topic: Topic, rec: dict, now: float) -> dict:
        """The exact post that would be published: the model's words (repaired, see
        ``repair``), a slug of its own that was never used, and the links the worker inserts
        - with UTM tags. What was repaired is kept as ``repaired`` (only when anything was):
        it is never published, and ``check_draft`` checks everything else."""
        raw = raw if isinstance(raw, dict) else {}
        repaired: list = []
        words = {}
        for key in ("title", "description", "body_md"):
            v = raw.get(key)
            if isinstance(v, str):
                v, done = repair(key, v)
                repaired += done
            words[key] = v
        title = words["title"]
        base = slugify(raw.get("slug") or title or topic.key) or slugify(topic.key)
        slug, n, used = base, 2, set(rec["used_slugs"])
        while slug in used:
            suffix = f"-{n}"
            slug = base[:MAX_SLUG - len(suffix)].strip("-") + suffix
            n += 1
        draft_id = f"{_day(now)}-{slug}"
        body = words["body_md"]
        if isinstance(body, str):
            body = body.strip() + "\n\n" + self.links(topic, draft_id)
        tags = raw.get("tags")
        if isinstance(tags, list) and all(isinstance(t, str) for t in tags):
            clean: list = []
            for t in tags:
                s = slugify(t, 24)
                if s and s not in clean:
                    clean.append(s)
            tags = clean[:5]
        description = words["description"]
        draft = {"draft_id": draft_id, "slug": slug,
                 "title": title.strip() if isinstance(title, str) else title,
                 "description": (description.strip() if isinstance(description, str)
                                 else description),
                 "body_md": body, "tags": tags}
        if repaired:
            draft[REPAIRED] = repaired
        return draft

    @staticmethod
    def links(topic: Topic, draft_id: str) -> str:
        """The worker's own footer: the topic's docs page, every API, and the paid offer
        that fits the topic (``offer_for``) - each with the post's UTM tags, so a visit,
        and a Gumroad sale, is tied to the post that sent it (TRAFFIC.md)."""
        q = contentcheck.utm_query(draft_id)
        lines = ["## Try it", "", f"- [{topic.label}]({SITE}{topic.path}?{q})"]
        if topic.path != "/":
            lines.append(f"- [all Dokaz APIs]({SITE}/?{q})")
        offer = offer_for(topic)
        lines.append(f"- [{offer.label}]({offer.url}?{q})")
        return "\n".join(lines) + "\n"

    def check_draft(self, draft: dict) -> list:
        # everything but the record's own note of what was repaired: exactly the payload
        # ``_job`` submits once it passes (an unknown field still blocks)
        return contentcheck.check({k: v for k, v in draft.items() if k != REPAIRED})

    def published_link(self, result, post: dict) -> str | None:
        return published_url(result, post["slug"])

    def _describe(self, draft: dict) -> dict:
        out = {"slug": draft["slug"], "title": draft.get("title")}
        if draft.get(REPAIRED):
            out[REPAIRED] = list(draft[REPAIRED])
        return out

    def _job(self, draft: dict) -> Job:
        payload = {k: draft[k] for k in contentcheck.FIELDS}      # exactly what was checked
        return Job(CAPABILITY, payload,
                   what=f"publish the blog post {draft['title']!r} on api.dokaz.net")

    def _before_submit(self, ctx: WorkContext, rec: dict, draft: dict, post: dict) -> None:
        """Keep the exact payload submitted beside the post: once it is published, that is
        the post's text, and the dev.to cross-poster (devto.py) copies it from here rather
        than from anywhere a word could have changed."""
        post["payload"] = json.loads(json.dumps(self._job(draft).payload))

    def _after_submit(self, ctx: WorkContext, rec: dict, draft: dict) -> None:
        rec["used_slugs"].append(draft["slug"])


def _allowlist_display() -> list:
    """The names the prompt offers the model, as written (not lower-cased)."""
    doc = json.loads(contentcheck.ALLOWLIST_PATH.read_text(encoding="utf-8"))
    return [n for key in ("brand", "products", "technology") for n in doc.get(key, [])]
