"""The dev.to cross-poster: each published blog post, once, on dev.to, for the owner's yes.

No words and no model: the post is the blog post exactly as it was published - the blog
worker's record keeps the payload it submitted (blog.py, ``payload``) - with two changes
only, both made here and nowhere else:

- every Dokaz link's ``utm_source=blog`` becomes ``utm_source=devto`` (medium and campaign
  unchanged), so a visit from dev.to is counted as dev.to's and still tied to the same
  post's campaign (C:\\src\\Scrooge\\docs\\TRAFFIC.md);
- after a blank line, the line ``FOOTER`` naming the original, with the same tags.

Tags become dev.to's form: lower-case letters and digits only (``site-checks`` ->
``sitechecks``), at most four, at least one (``webdev`` if none survive).

One run:

1. **Follow up** every cross-post waiting on the owner (``DailyPoster._follow_up``):
   approved and done WITH a dev.to URL is published; denied or failed is not; anything
   else is still waiting. Pionir refusing because the draft WAS already cross-posted is an
   answer, not a failure: recorded as published with the URL it gives, or as failed with
   its reason when it gives none.
2. **The oldest published blog post not in this worker's own record** is transformed,
   checked with ``contentcheck.check(..., utm_source="devto")`` plus the contract's own
   rules (``check_draft``), and submitted as ``Job("content.crosspost_devto", {draft_id,
   slug, title, description, body_md, tags})``. Pionir parks it for the owner.
3. **At most one submission per run.** A post whose check fails is recorded as blocked
   (the check is deterministic, so it is not retried), and a post the blog's record holds
   no text for is recorded as skipped; neither counts as the run's one.

Every post this worker ever acted on has exactly one entry in its record, keyed by the
blog post's ``draft_id`` (the cross-post keeps it, so its links' campaign is unchanged):
nothing in the record is ever submitted again.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

from . import contentcheck
from .blog import DailyPoster, _clip, _Unreadable, published_posts, read_record, result_links
from .figures import Figure
from .hands import Job
from .log import log
from .result import Err, Ok, Result
from .worker import ErrorKind, WorkContext, make_output, never_raises
from .workers import _Base

CAPABILITY = "content.crosspost_devto"
BLOG_WORKER = "posting.blog"
SITE = "https://api.dokaz.net"
DEVTO_HOSTS = frozenset({"dev.to"})
MAX_TAGS = 4
FALLBACK_TAG = "webdev"
_DEVTO_TAG = re.compile(r"[a-z0-9]{1,30}")
FOOTER = "*Originally published at [api.dokaz.net]({url})*"
# Pionir's refusal when the draft is already on dev.to. Matched loosely on purpose: the
# adapter's exact wording is its own ("already crossposted", "already cross-posted").
_ALREADY = re.compile(r"(?i)\balready\s+(?:been\s+)?cross-?posted\b")
_TRAILING = ".,;:!?"
NO_TEXT = ("the blog record holds no text for this post (it was published before the blog "
           "worker kept each post's text); it cannot be copied")


def devto_tags(tags) -> list:
    """The blog's tags in dev.to's form: ``[a-z0-9]`` only, at most ``MAX_TAGS``, never
    none (``FALLBACK_TAG``)."""
    out: list = []
    for t in tags if isinstance(tags, list) else []:
        if not isinstance(t, str):
            continue
        s = re.sub(r"[^a-z0-9]", "", t.lower())[:30]
        if s and s not in out:
            out.append(s)
    return out[:MAX_TAGS] or [FALLBACK_TAG]


def _swap_one(url: str) -> str:
    core = url.rstrip(_TRAILING)
    tail = url[len(core):]
    try:
        parts = urlsplit(core)
        host = (parts.hostname or "").lower()
    except ValueError:
        return url
    if host not in contentcheck.LINK_HOSTS or not parts.query:
        return url
    pairs = [f"utm_source={contentcheck.UTM_SOURCE_DEVTO}"
             if p == f"utm_source={contentcheck.UTM_SOURCE}" else p
             for p in parts.query.split("&")]
    return urlunsplit(parts._replace(query="&".join(pairs))) + tail


def swap_source(text: str) -> str:
    """Every Dokaz link's ``utm_source=blog`` -> ``utm_source=devto``; nothing else in
    the text or the link changes. A link elsewhere is left alone (the check refuses it)."""
    return contentcheck._URL.sub(lambda m: _swap_one(m.group(0)), text)


def footer(slug: str, draft_id: str) -> str:
    """The exact last line of every cross-post: where the post was first published."""
    q = contentcheck.utm_query(draft_id, contentcheck.UTM_SOURCE_DEVTO)
    return FOOTER.format(url=f"{SITE}/blog/{slug}?{q}")


def crosspost(payload: dict) -> dict:
    """The dev.to draft from the blog post as published: the same draft_id, slug, title and
    description; the body with its links' source swapped and the footer appended after a
    blank line; the tags in dev.to's form. A field that is not text is passed through for
    the check to refuse."""
    did, slug, body = payload.get("draft_id"), payload.get("slug"), payload.get("body_md")
    if isinstance(body, str) and isinstance(slug, str) and isinstance(did, str):
        body = swap_source(body).rstrip() + "\n\n" + footer(slug, did)
    return {"draft_id": did, "slug": slug, "title": payload.get("title"),
            "description": payload.get("description"), "body_md": body,
            "tags": devto_tags(payload.get("tags"))}


def devto_url(result) -> str | None:
    """The cross-post's dev.to address from Pionir's result, or None: an https URL on
    dev.to with a path. A done job without one is not a published cross-post."""
    for url in result_links(result):
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            continue
        if parts.scheme == "https" and (parts.hostname or "").lower() in DEVTO_HOSTS \
                and port is None and "@" not in parts.netloc and parts.path.strip("/"):
            return url
    return None


def _reason_of(raw) -> str:
    """Pionir's words for why it did not run the cross-post, from its raw answer."""
    for doc in (raw, raw.get("result") if isinstance(raw, dict) else None):
        if not isinstance(doc, dict):
            continue
        for key in ("refused", "error", "reason"):
            v = doc.get(key)
            if isinstance(v, dict):
                v = v.get("message") or v.get("type")
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


class DevtoWorker(DailyPoster):
    """``posting.devto``: each published blog post cross-posted to dev.to, once, checked,
    submitted for the owner's approval. At most one submission a run."""

    capability = CAPABILITY
    purpose = ""                    # asks the brain for nothing
    link_field = "url"
    link_missing = "gave no dev.to URL for the cross-post"
    record_what = "the dev.to cross-poster's own event counts"

    def __init__(self, spec, *, blog_worker: str = BLOG_WORKER) -> None:
        _Base.__init__(self, spec)      # no draft cadence: it drafts nothing
        if not isinstance(blog_worker, str) or not blog_worker.strip():
            raise ValueError("blog_worker names the blog worker whose record is read")
        self.blog_worker = blog_worker

    def _blank(self) -> dict:
        return {"posts": [], "counts": {}}

    # ---- one run -------------------------------------------------------------------------
    @never_raises()
    def run(self, ctx: WorkContext) -> Result:
        if ctx.state_dir is None:
            return self._err(ErrorKind.NOT_CONFIGURED, "no state dir: the worker cannot keep "
                             "its record, and without it could cross-post a post twice",
                             retryable=False)
        try:
            rec = self.load(ctx.state_dir)
        except _Unreadable as exc:
            return self._err(ErrorKind.MALFORMED, f"its record is unreadable ({exc}); "
                             "refusing to cross-post, since it could post one twice",
                             retryable=False)
        events: list = self._follow_up(ctx, rec)
        failed: Err | None = None
        waiting = 0
        try:
            blog = read_record(ctx.state_dir, self.blog_worker)
        except _Unreadable as exc:
            blog = None
            failed = self._err(ErrorKind.MALFORMED, f"the blog worker's record is unreadable "
                               f"({exc}); nothing cross-posted", retryable=False)
        else:
            got = self._crosspost_next(ctx, rec, blog, events)
            if isinstance(got, Err):
                failed = got
            else:
                waiting = got
        self.save(ctx.state_dir, rec)
        if failed is not None:
            if not events:
                return failed
            log.warning("%s: no cross-post this run: %s", self.worker_id, failed.error)
        return Ok((*events, self._tally(ctx, rec, waiting)))

    def _crosspost_next(self, ctx: WorkContext, rec: dict, blog: dict | None,
                        events: list) -> Result | int:
        """Submit the oldest published blog post not yet in this record; record any post
        passed over (blocked, or no text) on the way. Returns how many published posts are
        still waiting for a cross-post after this run, or the Err that stopped it."""
        seen = {p.get("draft_id") for p in rec["posts"]}
        todo = [p for p in published_posts(blog)
                if isinstance(p.get("draft_id"), str) and p["draft_id"] not in seen]
        submitted = False
        for i, post in enumerate(todo):
            if submitted:
                return len(todo) - i
            payload = post.get("payload")
            if not isinstance(payload, dict) or payload.get("draft_id") != post["draft_id"]:
                self._pass_over(ctx, rec, post, "skipped", [NO_TEXT], events)
                continue
            draft = crosspost(payload)
            reasons = self.check_draft(draft)
            if reasons:
                self._pass_over(ctx, rec, post, "blocked", reasons, events)
                continue
            if ctx.job is None:
                return self._err(ErrorKind.NOT_CONFIGURED, "no hands: a cross-post reaches "
                                 "the owner only through Pionir", retryable=False)
            entry = {"draft_id": draft["draft_id"], **self._describe(draft),
                     "original_url": post.get("url")}
            self._submit_post(ctx, rec, entry, draft, events)
            submitted = True
        return 0

    def _pass_over(self, ctx: WorkContext, rec: dict, post: dict, status: str, reasons: list,
                   events: list) -> None:
        """A published post this worker will not submit: recorded once, never retried."""
        did = post["draft_id"]
        rec["posts"].append({"draft_id": did, "slug": post.get("slug"), "status": status,
                             "reasons": [_clip(r, 200) for r in reasons[:12]],
                             "settled_at": ctx.now})
        self._count(rec, "drafts_blocked" if status == "blocked" else "skipped")
        log.warning("%s: %s %s and NOT submitted: %s", self.worker_id, did,
                    "BLOCKED by the content check" if status == "blocked" else "skipped",
                    "; ".join(reasons))
        if status == "blocked":
            events.append(self._event(ctx, "post.blocked", {
                "draft_id": did, "attempt": 1, "reasons_total": len(reasons),
                "reasons": [_clip(r, 90) for r in reasons[:3]]}))

    # ---- what a dev.to cross-post is -------------------------------------------------------
    def check_draft(self, draft: dict) -> list:
        """The crew's content check with dev.to's source, then the contract's own rules:
        1-4 tags of ``[a-z0-9]{1,30}`` and the exact footer as the body's last line."""
        reasons = contentcheck.check(draft, utm_source=contentcheck.UTM_SOURCE_DEVTO)
        tags = draft.get("tags")
        if not isinstance(tags, list) or not 1 <= len(tags) <= MAX_TAGS \
                or not all(isinstance(t, str) and _DEVTO_TAG.fullmatch(t) for t in tags):
            reasons.append(f"tags must be 1-{MAX_TAGS} of a-z 0-9 (dev.to's form)")
        body, slug, did = draft.get("body_md"), draft.get("slug"), draft.get("draft_id")
        if not (isinstance(body, str) and isinstance(slug, str) and isinstance(did, str)
                and body.endswith("\n\n" + footer(slug, did))):
            reasons.append("body_md does not end with the exact 'Originally published at' "
                           "line after a blank line")
        return reasons

    def published_link(self, result, post: dict) -> str | None:
        return devto_url(result)

    def _describe(self, draft: dict) -> dict:
        return {"slug": draft["slug"], "title": draft.get("title")}

    def _job(self, draft: dict) -> Job:
        payload = {k: draft[k] for k in contentcheck.FIELDS}      # exactly what was checked
        return Job(CAPABILITY, payload,
                   what=f"cross-post the blog post {draft['title']!r} to dev.to")

    def _settle_failed(self, ctx: WorkContext, rec: dict, post: dict, why: str, raw,
                       events: list) -> None:
        """Pionir refusing because this draft is ALREADY on dev.to means it is published
        there: recorded so, with the URL Pionir gives - and without one, as failed with the
        reason, since a cross-post nobody can point to is not counted as published."""
        reason = _reason_of(raw) or why
        if _ALREADY.search(reason) or _ALREADY.search(why):
            link = devto_url(raw)
            if link is not None:
                post.update({"status": "published", "url": link, "settled_at": ctx.now,
                             "why": _clip(reason, 200)})
                self._count(rec, "published")
                log.info("%s: %s was already on dev.to: %s", self.worker_id,
                         post["draft_id"], link)
                events.append(self._event(ctx, "post.published", {
                    "draft_id": post["draft_id"], "url": link,
                    "title": _clip(post.get("title"), 90)}))
                return
        self._settle(ctx, rec, post, "failed", reason, events)

    # ---- what the leader reads ------------------------------------------------------------
    def _tally(self, ctx: WorkContext, rec: dict, waiting: int = 0):
        c = rec["counts"]
        pending = [p["draft_id"] for p in rec["posts"] if p.get("status") == "pending_approval"]
        figures = [
            Figure(int(c.get("submitted_for_approval", 0)), "count",
                   "cross-posts submitted for approval", window="all_time"),
            Figure(len(pending), "count", "cross-posts pending approval", window="now"),
            Figure(int(c.get("published", 0)), "count", "cross-posts published",
                   window="all_time"),
            Figure(int(c.get("denied", 0)), "count", "cross-posts denied", window="all_time"),
            Figure(int(c.get("failed", 0)), "count", "cross-posts failed", window="all_time"),
            Figure(int(c.get("drafts_blocked", 0)), "count", "cross-posts blocked",
                   window="all_time"),
            Figure(waiting, "count", "published blog posts waiting to be cross-posted",
                   window="now"),
        ]
        return make_output(self, kind="post.tally", valid_at=ctx.now, observed_at=ctx.now,
                           payload={"pending_approval": pending[-3:],
                                    "skipped_without_text": int(c.get("skipped", 0))},
                           figures=figures, entities=self.entities,
                           provenance={"source": "real", "provider": self.provider,
                                       "record": self.record_what})
