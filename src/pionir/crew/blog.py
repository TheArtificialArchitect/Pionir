"""The blog worker: drafts one post for api.dokaz.net and asks the owner to approve it.

The business goal is search traffic to the paid APIs. The owner's rule for AI-written
public content is the whole shape of this file: **the model drafts; a fail-closed check
runs on the exact final text; the owner approves every post.**

One run:

1. **Follow up** every post already waiting on the owner (``ctx.approval``): approved and
   done WITH a URL is published; denied, failed or done-without-a-URL is not; anything
   else is still waiting. Nothing is ever assumed published.
2. **At most one draft per day** (``draft_every_seconds``): the owner reads each one by
   hand, and a flood of them would only teach him to stop reading.
3. **A topic**: from the division's goal as Moss set it, if there is one - the seed topic
   that matches it best, else the goal itself once - otherwise the next evergreen seed tied
   to a real product. A topic or slug already used is never used again (the record).
4. **Words from the shared brain only** (``ctx.words``: JSON schema, temperature 0,
   charged to this division). This module imports no model.
5. **The worker inserts the links itself**, each carrying the blog's UTM tags, and only
   THEN runs ``contentcheck.check`` - on the exact dict that would be submitted.
6. **Blocked**: recorded with its reasons, loudly, and NOT submitted. One fresh draft is
   allowed per run, with the reasons in the prompt. **Passed**: submitted as
   ``Job("content.publish", {draft_id, slug, title, description, body_md, tags})``; Pionir
   parks it for the owner, and it is recorded as PENDING APPROVAL - never as published.

Every count this worker reports is a count of real events it recorded: drafts written,
drafts blocked, posts submitted for approval, posts published.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

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
- Write NO links, NO URLs and NO website or domain names. The links are added for you.
- Name no person, place, city, country, company, customer or website. The only names you \
may write with a capital letter are: {names}. Every other word is lower case unless it \
starts a sentence.
- Title and headings in sentence case: only the first word capitalised.
- No numbers about the business: no counts of customers, users or sales, no revenue, no \
prices, no money amounts. Never write "we made", "we earned", "we sold" or "our revenue".
- No email addresses, phone numbers, IP addresses or street addresses, not even made-up \
examples.
- Code is optional. If you show any, show only a JSON request body in a fenced block, \
with lower-case keys and no keys, tokens, headers or URLs.
- title: 10 to 120 characters. description: one or two sentences, 60 to 280 characters. \
body_md: 400 to 1200 words. slug: a few lower-case words joined by hyphens. tags: up to \
five lower-case words, hyphens instead of spaces."""


def slugify(text: str, limit: int = MAX_SLUG) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return s[:limit].strip("-")


def _day(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now))


def _clip(s, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 3] + "..."


def published_url(result, slug: str) -> str | None:
    """The post's public URL from Pionir's result, or None. Only an https URL on a Dokaz
    host that carries the post's slug counts: a done job with no such URL is not a
    published post."""
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
    for url in found:
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


class BlogWorker(_Base):
    """``posting.blog``: one checked draft a day, submitted for the owner's approval."""

    def __init__(self, spec, *, draft_every_seconds: int = 86400) -> None:
        super().__init__(spec)
        if isinstance(draft_every_seconds, bool) or not isinstance(draft_every_seconds, int) \
                or draft_every_seconds <= 0:
            raise ValueError("draft_every_seconds is a positive whole number of seconds")
        self.draft_every_seconds = draft_every_seconds

    # ---- the record: what it drafted, what it used, what became of each post ----------
    def record_path(self, state_dir: Path) -> Path:
        return Path(state_dir) / f"{self.worker_id}.json"

    def load(self, state_dir: Path) -> dict:
        path = self.record_path(state_dir)
        blank = {"last_drafted_at": None, "used_topics": [], "used_slugs": [], "posts": [],
                 "blocked": [], "counts": {}}
        if not path.exists():
            return blank
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise _Unreadable(f"{path}: {exc}") from exc
        if not isinstance(doc, dict):
            raise _Unreadable(f"{path} is not an object")
        return {**blank, **doc}

    def save(self, state_dir: Path, rec: dict) -> None:
        path = self.record_path(state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(path)       # atomic: a half-written record is never read back

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
        events += self._follow_up(ctx, rec)
        drafted: Result | None = None
        last = rec.get("last_drafted_at")
        if last is None or ctx.now - float(last) >= self.draft_every_seconds:
            drafted = self._draft_and_submit(ctx, rec, events)
        self.save(ctx.state_dir, rec)
        if isinstance(drafted, Err):
            if not events:
                return drafted
            log.warning("%s: no draft this run: %s", self.worker_id, drafted.error)
        return Ok((*events, self._tally(ctx, rec)))

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
                self._settle_done(ctx, rec, post, outcome_of(CAPABILITY, got.get("result")),
                                  events)
            elif state == "approved_failed":
                out = outcome_of(CAPABILITY, got.get("result"))
                self._settle(ctx, rec, post, "failed", out.error or "the publish failed",
                             events)
            else:
                log.warning("%s: approval %s has a status nobody knows (%r); still waiting",
                            self.worker_id, post["approval_id"], state)
        return events

    def _settle_done(self, ctx: WorkContext, rec: dict, post: dict, out, events: list) -> None:
        if not out.ran:
            self._settle(ctx, rec, post, "failed", out.error or f"Pionir said {out.status}",
                         events)
            return
        url = published_url(out.result, post["slug"])
        if url is None:
            self._settle(ctx, rec, post, "unconfirmed", "Pionir said done but gave no URL for "
                         "the post; NOT counted as published", events)
            return
        post.update(status="published", url=url, settled_at=ctx.now)
        self._count(rec, "published")
        log.info("%s: published %s", self.worker_id, url)
        events.append(self._event(ctx, "post.published", {
            "draft_id": post["draft_id"], "url": url, "title": _clip(post.get("title"), 90)}))

    def _settle(self, ctx: WorkContext, rec: dict, post: dict, status: str, why: str,
                events: list) -> None:
        post.update(status=status, why=why, settled_at=ctx.now)
        self._count(rec, status)
        log.warning("%s: %s is %s: %s", self.worker_id, post["draft_id"], status, why)
        events.append(self._event(ctx, "post.not_published", {
            "draft_id": post["draft_id"], "status": status, "why": _clip(why, 160)}))

    # ---- 2-6. one draft, checked, and submitted if it passed ------------------------------
    def choose_topic(self, rec: dict, goal: str | None) -> Topic | None:
        used = set(rec["used_topics"])
        free = [t for t in SEEDS if t.key not in used]
        if goal:
            words = set(re.findall(r"[a-z0-9-]+", goal.lower()))
            scored = sorted(((len(words & set(t.words)), i, t) for i, t in enumerate(free)),
                            key=lambda x: (-x[0], x[1]))
            if scored and scored[0][0] > 0:
                return scored[0][2]
            key = "goal-" + hashlib.sha256(goal.strip().lower().encode()).hexdigest()[:10]
            if key not in used:
                return Topic(key, f"a post that serves this goal: {goal.strip()}", "Dokaz",
                             "sells small paid web APIs: invoice PDFs, email verification, QR "
                             "codes, site checks and text utilities", (), "/",
                             "all Dokaz APIs")
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
        reasons: list = []
        for attempt in (1, 2):      # one fresh draft per run, at most, after a block
            got = ctx.words("blog_draft", SYSTEM.format(names=self._names()),
                            self._prompt(topic, ctx.goal, reasons), DRAFT_SCHEMA)
            if isinstance(got, Err):
                if attempt == 1:
                    return got          # no words came: nothing was drafted, the day is not used
                break
            rec["last_drafted_at"] = ctx.now
            self._count(rec, "drafts_written")
            draft = self.assemble(got.value, topic, rec, ctx.now)
            reasons = contentcheck.check(draft)
            if reasons:
                self._blocked(ctx, rec, topic, draft, reasons, attempt, events)
                continue
            self._submit(ctx, rec, topic, draft, events)
            break
        if topic.key not in rec["used_topics"]:
            rec["used_topics"].append(topic.key)
        return None

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
            lines += [f"- {r}" for r in reasons[:12]]
        return "\n".join(lines)

    def assemble(self, raw, topic: Topic, rec: dict, now: float) -> dict:
        """The exact post that would be published: the model's words, a slug of its own
        that was never used, and the links the worker inserts - with UTM tags."""
        raw = raw if isinstance(raw, dict) else {}
        title = raw.get("title")
        base = slugify(raw.get("slug") or title or topic.key) or slugify(topic.key)
        slug, n, used = base, 2, set(rec["used_slugs"])
        while slug in used:
            suffix = f"-{n}"
            slug = base[:MAX_SLUG - len(suffix)].strip("-") + suffix
            n += 1
        draft_id = f"{_day(now)}-{slug}"
        body = raw.get("body_md")
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
        return {"draft_id": draft_id, "slug": slug,
                "title": title.strip() if isinstance(title, str) else title,
                "description": (raw.get("description").strip()
                                if isinstance(raw.get("description"), str)
                                else raw.get("description")),
                "body_md": body, "tags": tags}

    @staticmethod
    def links(topic: Topic, draft_id: str) -> str:
        q = contentcheck.utm_query(draft_id)
        lines = ["## Try it", "", f"- [{topic.label}]({SITE}{topic.path}?{q})"]
        if topic.path != "/":
            lines.append(f"- [all Dokaz APIs]({SITE}/?{q})")
        return "\n".join(lines) + "\n"

    def _blocked(self, ctx: WorkContext, rec: dict, topic: Topic, draft: dict, reasons: list,
                 attempt: int, events: list) -> None:
        self._count(rec, "drafts_blocked")
        rec["blocked"] = (rec["blocked"] + [{
            "draft_id": draft["draft_id"], "slug": draft["slug"], "topic": topic.key,
            "title": draft.get("title"), "reasons": reasons, "attempt": attempt,
            "at": ctx.now}])[-100:]
        log.warning("%s: draft %s BLOCKED by the content check and NOT submitted (attempt %d): "
                    "%s", self.worker_id, draft["draft_id"], attempt, "; ".join(reasons))
        events.append(self._event(ctx, "post.blocked", {
            "draft_id": draft["draft_id"], "attempt": attempt, "reasons_total": len(reasons),
            "reasons": [_clip(r, 90) for r in reasons[:3]]}))

    def _submit(self, ctx: WorkContext, rec: dict, topic: Topic, draft: dict,
                events: list) -> None:
        payload = {k: draft[k] for k in contentcheck.FIELDS}      # exactly what was checked
        out = ctx.job(Job(CAPABILITY, payload,
                          what=f"publish the blog post {draft['title']!r} on api.dokaz.net"))
        rec["used_slugs"].append(draft["slug"])
        post = {"draft_id": draft["draft_id"], "slug": draft["slug"], "topic": topic.key,
                "title": draft["title"], "submitted_at": ctx.now, "status": out.status,
                "task_id": out.task_id, "approval_id": out.approval_id}
        rec["posts"].append(post)
        if out.status == "pending_approval":
            self._count(rec, "submitted_for_approval")
            log.info("%s: %s submitted; PENDING the owner's approval (approval %s)",
                     self.worker_id, draft["draft_id"], out.approval_id)
            events.append(self._event(ctx, "post.pending_approval", {
                "draft_id": draft["draft_id"], "title": _clip(draft["title"], 90),
                "approval_id": out.approval_id}))
        elif out.status == "done":
            # Pionir ran it without parking it: the owner did NOT approve this post. That
            # breaks his rule, and it is Pionir's gate that must hold it - say so loudly.
            log.error("%s: content.publish ran WITHOUT the owner's approval for %s; the "
                      "capability must be approval-gated in Pionir", self.worker_id,
                      draft["draft_id"])
            post["approved_by_owner"] = False
            self._settle_done(ctx, rec, post, out, events)
        else:
            # failed, unreachable or still running: not published, and never assumed to be
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
        last = rec.get("last_drafted_at")
        wait = 0 if last is None else max(0, round(float(last) + self.draft_every_seconds
                                                   - ctx.now))
        return make_output(self, kind="post.tally", valid_at=ctx.now, observed_at=ctx.now,
                           payload={"pending_approval": pending[-3:],
                                    "next_draft_in_hours": round(wait / 3600, 1),
                                    "topics_left": sum(1 for t in SEEDS
                                                       if t.key not in rec["used_topics"])},
                           figures=figures, entities=self.entities,
                           provenance={"source": "real", "provider": self.provider,
                                       "record": "the blog worker's own event counts"})


def _allowlist_display() -> list:
    """The names the prompt offers the model, as written (not lower-cased)."""
    doc = json.loads(contentcheck.ALLOWLIST_PATH.read_text(encoding="utf-8"))
    return [n for key in ("brand", "products", "technology") for n in doc.get(key, [])]
