"""The Instagram worker: drafts one card post a day and asks the owner to approve it.

The business goal is traffic to the paid APIs, through the link in the account's bio. The
owner's rule for AI-written public content is the whole shape of this file, as it is of the
blog's (blog.py, whose machinery this worker shares - ``DailyPoster``): **the model drafts;
a fail-closed check runs on the exact final payload; the owner approves every post** - on
Discord, with the rendered card attached.

One run:

1. **Follow up** every post already waiting on the owner (``ctx.approval``): approved and
   done WITH an https instagram.com permalink is published; denied, failed or
   done-without-a-permalink is not; anything else is still waiting. Nothing is ever
   assumed published.
2. **At most one draft per day** (``draft_every_seconds``).
3. **A topic**: the blog seed that the division's goal as Moss set it matches best -
   otherwise, or with no goal, the next blog seed (``blog.SEEDS``) this worker has not
   used. The goal steers among the seeds; it is never a post's subject itself. Its own
   record: a topic the blog used is fine here too. A blocked day does not use a topic up
   (``MAX_TOPIC_BLOCKS``).
4. **Words from the shared brain only** (``ctx.words``: JSON schema, temperature 0,
   charged to this division). This module imports no model.
5. **The worker assembles the payload** - ``{draft_id, headline, points, caption, hashtags,
   card_sha}``, where ``card_sha`` pins the exact JPEG ``pionir.social.card`` renders from
   the headline and points - and only THEN runs ``contentcheck.check_social`` on it.
6. **Blocked**: recorded with its reasons, loudly, and NOT submitted; one fresh draft per
   run, with the reasons in the prompt. **Passed**: the card is saved next to the record
   (the last ``KEEP_CARDS``), and the payload is submitted unchanged as
   ``Job("social.instagram_post", payload)`` with no permissions. Pionir parks it for the
   owner, and it is recorded as PENDING APPROVAL - never as published.

Every count this worker reports is a count of real events it recorded.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlsplit

from pionir import atomic
from pionir.social.card import CardTooLong, card_sha, render_card

from . import contentcheck
from .blog import DailyPoster, Topic, _allowlist_display, _day, result_links, slugify
from .hands import Job
from .log import log
from .worker import WorkContext

CAPABILITY = "social.instagram_post"
MAX_SLUG = 50              # "YYYY-MM-DD-ig-" + slug stays inside draft_id's 64
MAX_HASHTAGS = 8
KEEP_CARDS = 30            # rendered cards kept on disk for the owner and the leader
PERMALINK_HOSTS = frozenset({"instagram.com", "www.instagram.com"})
DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "points": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                   "maxItems": 3},
        "caption": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_HASHTAGS},
    },
    "required": ["headline", "points", "caption", "hashtags"],
}

SYSTEM = """You write one Instagram post for Dokaz, which sells small paid web APIs. The \
post is a picture card - a headline and one to three short points - and a caption under \
it. It helps a developer or a small business owner with a real task and, where it fits, \
shows how the named product does it. Answer with ONE JSON object: headline, points, \
caption, hashtags. Nothing else.

Hard rules. A draft that breaks any one of them is thrown away unread:
- headline: 10 to 90 characters, one line. It goes on the card in large type.
- points: one to three, each 10 to 110 characters, one line each. Each is one short, \
plain statement.
- caption: 50 to 1800 characters. Its last sentence points the reader to "the link in \
bio", in those words.
- Write NO links, NO URLs and NO website or domain names anywhere. Instagram captions do \
not link; the link in bio does that.
- Never write the @ character or the # character in the headline, the points or the \
caption. Hashtags go ONLY in the hashtags list.
- hashtags: at most {max_tags}, each one or more plain English words run together, lower \
case, without the # sign (for example "emaildeliverability"). No names in them.
- Name no person, place, city, country, company, customer, website or platform. The only \
names you may write with a capital letter are: {names}. Every other word is lower case \
unless it starts a sentence or is in the headline.
- No numbers about the business: no counts of customers, users or sales, no revenue, no \
prices, no money amounts. Never write "we made", "we earned", "we sold" or "our revenue".
- No email addresses, phone numbers, IP addresses or street addresses, not even made-up \
examples.
- Invent NO example data at all: no sample people, companies, addresses, invoice or order \
numbers, dates or IDs. Describe a field by what it holds ("the customer's name"), never \
by an example value.
- Only plain ASCII letters, digits and punctuation, plus these: ’ “ ” \
– — …. No emoji, no other symbols, no angle brackets."""


def permalink(result) -> str | None:
    """The post's Instagram permalink from Pionir's result, or None. Only an https
    instagram.com address with a path counts: a done job without one is not a published
    post."""
    for url in result_links(result):
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            continue
        if parts.scheme == "https" and (parts.hostname or "") in PERMALINK_HOSTS \
                and port is None and "@" not in parts.netloc and parts.path.strip("/"):
            return url
    return None


def media_id(result) -> str | None:
    """The published post's Instagram media id from Pionir's result (the adapter's
    ``media_id``), or None. Only an all-digit id counts: it goes into a Graph path."""
    raw = result.get("media_id") if isinstance(result, dict) else None
    if isinstance(raw, int) and not isinstance(raw, bool):
        raw = str(raw)
    ok = isinstance(raw, str) and raw.isascii() and raw.isdigit() and len(raw) <= 40
    return raw if ok else None


def _clean_tags(tags) -> object:
    """Hashtags as Instagram wants them - lower case, no #, no spaces, no repeats, at most
    ``MAX_HASHTAGS``. Anything that is not a list of strings is left for the check to
    refuse; a tag that cannot be a hashtag at all (empty, or only symbols) is dropped."""
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        return tags
    out: list = []
    for t in tags:
        s = "".join(t.strip().lstrip("#").lower().split()).replace("-", "")
        if s and any(c.isalnum() for c in s) and s not in out:
            out.append(s)
    return out[:MAX_HASHTAGS]


class InstagramWorker(DailyPoster):
    """``posting.instagram``: one checked card post a day, submitted for the owner's
    approval."""

    capability = CAPABILITY
    purpose = "instagram_draft"
    schema: ClassVar[dict] = DRAFT_SCHEMA
    title_field = "headline"
    link_field = "permalink"
    link_missing = "gave no instagram.com permalink for the post"
    record_what = "the Instagram worker's own event counts"

    # ---- the card kept beside the record ---------------------------------------------------
    def cards_dir(self, state_dir: Path) -> Path:
        return Path(state_dir) / self.worker_id

    def _before_submit(self, ctx: WorkContext, rec: dict, draft: dict, post: dict) -> None:
        """Save the card the owner will be shown, before it goes to Pionir, and keep only the
        newest ``KEEP_CARDS`` (draft ids start with the day, so their names sort by age).
        A failure to save is logged and does not stop the post: the owner sees the card on
        the approval itself."""
        folder = self.cards_dir(ctx.state_dir)
        try:
            jpeg = render_card(draft["headline"], draft["points"])
            if hashlib.sha256(jpeg).hexdigest() != draft["card_sha"]:
                log.error("%s: the card for %s rendered differently from the one checked; "
                          "not saved", self.worker_id, draft["draft_id"])
                return
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{draft['draft_id']}.jpg"
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(jpeg)
            atomic.replace(tmp, path)
            post["card"] = f"{folder.name}/{path.name}"
            for old in sorted(folder.glob("*.jpg"))[:-KEEP_CARDS]:
                old.unlink()
        except Exception as exc:  # noqa: BLE001 - a convenience copy never stops the post
            log.warning("%s: could not save the card for %s: %s: %s", self.worker_id,
                        draft["draft_id"], type(exc).__name__, exc)

    # ---- what an Instagram post is -------------------------------------------------------
    def _system(self) -> str:
        names = ", ".join(sorted(set(_allowlist_display())))
        return SYSTEM.format(names=names, max_tags=MAX_HASHTAGS)

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
        """The exact payload that would be submitted: the model's words (trimmed), an id of
        the worker's own, and the card's hash. A card the text does not fit gets no hash;
        the check then blocks it and says why."""
        raw = raw if isinstance(raw, dict) else {}
        headline = raw.get("headline")
        headline = headline.strip() if isinstance(headline, str) else headline
        points = raw.get("points")
        if isinstance(points, list):
            points = [p.strip() if isinstance(p, str) else p for p in points]
        caption = raw.get("caption")
        caption = caption.strip() if isinstance(caption, str) else caption
        slug = slugify(headline if isinstance(headline, str) else "", MAX_SLUG) \
            or slugify(topic.key, MAX_SLUG)
        draft = {"draft_id": f"{_day(now)}-ig-{slug}", "headline": headline, "points": points,
                 "caption": caption, "hashtags": _clean_tags(raw.get("hashtags"))}
        if isinstance(headline, str) and isinstance(points, list) \
                and all(isinstance(p, str) for p in points):
            try:
                draft["card_sha"] = card_sha(headline, points)
            except CardTooLong as exc:
                log.warning("%s: the text does not fit the card (%s); it will be blocked",
                            self.worker_id, exc)
            except Exception as exc:  # noqa: BLE001 - no card, no hash: the check blocks it
                log.warning("%s: the card could not be rendered: %s: %s", self.worker_id,
                            type(exc).__name__, exc)
        return draft

    def check_draft(self, draft: dict) -> list:
        reasons = contentcheck.check_social(draft)
        caption = draft.get("caption")
        if isinstance(caption, str) and "link in bio" not in " ".join(caption.lower().split()):
            reasons.append('the caption does not point the reader to "the link in bio"')
        return reasons

    def published_link(self, result, post: dict) -> str | None:
        return permalink(result)

    def _settle_done(self, ctx: WorkContext, rec: dict, post: dict, out, events: list) -> None:
        """As every poster, and a published post also keeps Instagram's media id: the
        results worker reads its insights by it (``social.instagram_insights``)."""
        super()._settle_done(ctx, rec, post, out, events)
        if post.get("status") == "published":
            mid = media_id(out.result)
            if mid is not None:
                post["media_id"] = mid

    def _describe(self, draft: dict) -> dict:
        return {"headline": draft.get("headline")}

    def _job(self, draft: dict) -> Job:
        payload = {k: draft[k] for k in ("draft_id", "headline", "points", "caption",
                                         "hashtags", "card_sha")}      # exactly what was checked
        return Job(CAPABILITY, payload, what=f"post {draft['headline']!r} to Instagram")
