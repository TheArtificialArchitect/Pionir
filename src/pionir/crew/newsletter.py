"""The newsletter worker: once a week, what was published, to the people who asked for it.

The list is Scrooge's (``api.dokaz.net``): double opt-in, a one-click unsubscribe in every
copy, nobody added who did not confirm. This worker writes nothing of its own and asks no
model: each newsletter is ASSEMBLED, deterministically, from what is already public -

- the blog posts published in the last seven days (the blog worker's record, ``payload``:
  the title and description exactly as published), each linked to its page;
- the products on sale (the product shelf's record: the owner's own listing name and the
  Gumroad URL Pionir published it at), a product new since the last newsletter marked so;
- the "we build it for you" page, ``/hire``;

every link tagged ``utm_source=newsletter&utm_medium=email&utm_campaign=<the newsletter's
id>`` (C:\\src\\Scrooge\\docs\\TRAFFIC.md). Its id is the ISO week: ``weekly-2026-w40``.

One run:

1. **Follow up** every newsletter waiting on the owner (``DailyPoster._follow_up``):
   approved and done WITH Scrooge's send id is SENT; denied or failed is not; anything else
   is still waiting. Pionir refusing because Scrooge already HAS this newsletter is an
   answer: recorded as sent with the send id it gives, or as failed without one.
2. **The audience**, from Scrooge's ``GET /dash/api.json`` (``newsletter``) with the read
   token: subscribers pending, confirmed and unsubscribed, the sends and the last one.
   Unreadable is said, never a zero, and never stops the run.
3. **This week's newsletter**, once: nothing new since the last one (no post published in
   the last seven days that a newsletter has not carried, no product newly on sale) is
   nothing to send - skipped, and no alarm. Scrooge KNOWN to have no confirmed subscriber
   is skipped too, and said: an approval that emails nobody wastes the owner's yes.
   Otherwise the newsletter is assembled, checked with the crew's content check
   (``contentcheck.check_newsletter``: Pionir's own newsletter check first, then the blog's
   rules with the newsletter's tags) on the exact text that would be sent, and submitted as
   ``Job("content.newsletter_send", {newsletter_id, subject, body_md})``. Pionir parks it
   for the owner on its own card. A blocked newsletter is recorded with its reasons and not
   retried that week (the check is deterministic).

Its pulse alarms (``no_output``) only when there WAS something new this week and no
newsletter reached the owner - blocked, or Pionir refused it. A quiet week is not an alarm.

Scrooge unable to send (``sender.ok`` false in the dash; its endpoint answers 503 until
POSTAL_ADDRESS and RESEND_API_KEY are set) is NOT CONFIGURED, typed: nothing is submitted
while the dash says so, an approved newsletter Scrooge answered 503 is settled
``not_configured`` (never retried that week, never counted as sent), and the run returns
``ErrorKind.NOT_CONFIGURED`` with its rows kept, so the vitals and the brief say it plainly.
"""
from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime
from urllib.parse import urlsplit

from . import contentcheck
from .blog import DailyPoster, _clip, _Unreadable, published_posts, read_record
from .figures import Figure
from .hands import Job
from .log import log
from .result import Err, Ok, Result
from .worker import ErrorKind, WorkContext, make_output, never_raises
from .workers import DashReader, _Base

CAPABILITY = "content.newsletter_send"
BLOG_WORKER = "posting.blog"
PRODUCTS_WORKER = "products.shelf"
SITE = "https://api.dokaz.net"
WEEK = 7 * 86400
RETRY_UNREACHABLE = 5        # runs a never-delivered submission is retried, that week
# A newsletter in one of these is out of the owner's hands or on its way: what it carried
# counts as featured.
FEATURED = frozenset({"pending_approval", "published", "unconfirmed", "unknown"})
NOTHING_NEW = "nothing new since the last newsletter"
NO_SUBSCRIBERS = ("Scrooge has no confirmed subscriber yet: a newsletter now would email "
                  "nobody, so none was submitted")
_ALREADY = ("already sent", "already queued")
# A send id quoted in a refusal's words (Pionir's own ledger refusal carries it only there).
_SEND_ID_TEXT = re.compile(r"\bnl_[0-9a-f]{8,64}\b")
_NOT_CONFIGURED_TEXT = re.compile(r"\bnot configured\b", re.IGNORECASE)
NOT_CONFIGURED = ("Scrooge cannot send newsletters yet ({why}): set its POSTAL_ADDRESS and "
                  "RESEND_API_KEY secrets; no newsletter is submitted until it can")


def reached(entry: dict) -> bool:
    """Did this newsletter reach the owner? Parked for him (an approval id: whatever he or
    Scrooge then said), or run and queued. Refused by Pionir before parking, blocked, or
    never delivered: no."""
    if entry.get("status") == "not_configured":
        return False        # approved or not, Scrooge could not send it: nothing went out
    return bool(entry.get("approval_id")) or entry.get("status") in ("published",
                                                                     "unconfirmed")


def _flagged(obj, key: str, depth: int = 0) -> bool:
    """Is ``key`` True anywhere shallow in Pionir's answer (the adapter's typed flags)?"""
    if depth > 4:
        return False
    if isinstance(obj, dict):
        return obj.get(key) is True or any(_flagged(v, key, depth + 1) for v in obj.values())
    if isinstance(obj, list):
        return any(_flagged(v, key, depth + 1) for v in obj[:20])
    return False


def sender_off(stats) -> str | None:
    """Why Scrooge says it cannot send newsletters (``sender.ok`` false), or None: it can,
    or the audience could not be read (then it is not known, and not assumed)."""
    if not isinstance(stats, dict) or not isinstance(stats.get("sender"), dict):
        return None
    sender = stats["sender"]
    if sender.get("ok") is not False:
        return None
    return NOT_CONFIGURED.format(why=sender.get("why") or "its sender is off")


def newsletter_id(now: float) -> str:
    """This week's id (UTC): ``weekly-<ISO year>-w<ISO week, two digits>``."""
    year, week, _day = datetime.fromtimestamp(float(now), UTC).isocalendar()
    return f"weekly-{year}-w{week:02d}"


def tagged(url: str, nid: str) -> str:
    """The link with the newsletter's three UTM tags (and nothing else added)."""
    joiner = "&" if urlsplit(url).query else "?"
    return f"{url}{joiner}{contentcheck_query(nid)}"


def contentcheck_query(nid: str) -> str:
    return (f"utm_source={contentcheck.UTM_SOURCE_NEWSLETTER}"
            f"&utm_medium={contentcheck.UTM_MEDIUM_EMAIL}"
            f"&utm_campaign={contentcheck.utm_campaign(nid)}")


def _plural(n: int, one: str, many: str) -> str:
    return f"a {one}" if n == 1 else f"{n} {many}"


def assemble(nid: str, posts: list, products: list) -> dict:
    """The exact newsletter that would be sent. ``posts``: ``{slug, title, description}``
    of the new blog posts, oldest first; ``products``: ``{slug, name, url, new}`` of the
    products on sale. Pure: the same week and records give the same email, byte for byte."""
    new_products = [p for p in products if p.get("new")]
    parts = []
    if posts:
        parts.append(f"{_plural(len(posts), 'new post', 'new posts')}")
    if new_products:
        parts.append(f"{_plural(len(new_products), 'new tool', 'new tools')}")
    subject = "Dokaz weekly: " + " and ".join(parts) if parts else "Dokaz weekly"
    lines = ["This week from Dokaz Industries: what we published, and the tools on sale.", ""]
    if posts:
        lines += ["## New on the blog", ""]
        for p in posts:
            url = tagged(f"{SITE}/blog/{p['slug']}", nid)
            lines.append(f"- [{p['title']}]({url}) - {p['description']}")
        lines.append("")
    if products:
        lines += ["## Tools on sale", ""]
        for p in products:
            mark = " (new this week)" if p.get("new") else ""
            lines.append(f"- [{p['name']}]({tagged(p['url'], nid)}){mark}")
        lines.append("")
    lines += ["## Built for you", "",
              ("We also build small custom tools and automations to a written brief: "
               "scripts, data clean-ups, spreadsheet and PDF workflows, and API integrations. "
               f"Tell us what you need on the [hire page]({tagged(SITE + '/hire', nid)})."),
              "",
              f"Every post is on the [blog]({tagged(SITE + '/blog', nid)}).", ""]
    return {"newsletter_id": nid, "subject": subject, "body_md": "\n".join(lines)}


def _send_id(result) -> str | None:
    """Scrooge's send id from Pionir's result, shallow first, or None."""
    seen = 0

    def walk(obj, depth: int):
        nonlocal seen
        if depth > 4 or seen > 200:
            return None
        seen += 1
        if isinstance(obj, dict):
            v = obj.get("send_id")
            if isinstance(v, str) and v.startswith("nl_") and 4 <= len(v) <= 80 \
                    and v[3:].isalnum():
                return v
            for x in obj.values():
                got = walk(x, depth + 1)
                if got:
                    return got
        elif isinstance(obj, list):
            for x in obj[:20]:
                got = walk(x, depth + 1)
                if got:
                    return got
        return None

    return walk(result, 0)


def _reason_of(raw) -> str:
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


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def read_stats(doc) -> tuple:
    """(stats, None) from ``/dash/api.json``'s ``newsletter`` object, or (None, why)."""
    if not isinstance(doc, dict):
        return None, "the dash answered with something that is not an object"
    nl = doc.get("newsletter")
    if nl is None:
        return None, "Scrooge's dash carries no newsletter figures (is the newsletter deployed?)"
    if not isinstance(nl, dict):
        return None, "Scrooge's newsletter figures are not an object"
    if isinstance(nl.get("error"), str):
        return None, f"Scrooge could not count the newsletter: {_clip(nl['error'], 200)}"
    subs = nl.get("subscribers")
    if not isinstance(subs, dict) or not all(_int(subs.get(k)) and subs[k] >= 0
                                             for k in ("pending", "confirmed", "unsubscribed")):
        return None, "Scrooge's newsletter subscribers are not three whole counts"
    for key in ("sends", "sent_today", "daily_cap"):
        if not _int(nl.get(key)) or nl[key] < 0:
            return None, f"Scrooge's newsletter {key} is not a whole count"
    last = nl.get("last_send")
    if last is not None and (
            not isinstance(last, dict) or not isinstance(last.get("newsletter_id"), str)
            or last.get("status") not in ("queued", "sending", "done")
            or not all(_int(last.get(k)) for k in ("recipients", "sent", "failed", "skipped"))):
        return None, "Scrooge's last newsletter send is malformed"
    sender = nl.get("sender")
    if not isinstance(sender, dict) or not isinstance(sender.get("ok"), bool):
        return None, "Scrooge's newsletter sender state is malformed"
    stats = {"subscribers": {k: int(subs[k]) for k in ("pending", "confirmed", "unsubscribed")},
             "sends": int(nl["sends"]), "sent_today": int(nl["sent_today"]),
             "daily_cap": int(nl["daily_cap"]),
             "sender": {"ok": sender["ok"],
                        "why": _clip(sender.get("why"), 200)
                        if isinstance(sender.get("why"), str) else None},
             "last_send": None if last is None else {
                 "newsletter_id": _clip(last["newsletter_id"], 64),
                 "status": last["status"],
                 **{k: int(last[k]) for k in ("recipients", "sent", "failed", "skipped")},
                 **{k: (last.get(k) if isinstance(last.get(k), str) else None)
                    for k in ("queued_at", "finished_at")}}}
    return stats, None


class NewsletterWorker(DailyPoster):
    """``posting.newsletter``: one checked newsletter a week, assembled from what was
    published, submitted for the owner's approval. Nothing new is nothing sent."""

    capability = CAPABILITY
    purpose = ""                    # asks the brain for nothing
    title_field = "subject"
    link_field = "send_id"
    link_missing = "gave no send id for the newsletter"
    record_what = "the newsletter worker's own event counts"
    unknown = "the newsletter audience"
    # the dash read, shared with the ledger and the traffic reader (workers.DashReader)
    _token_path = DashReader._token_path
    _read_dash = DashReader._read_dash
    _provenance = DashReader._provenance

    def __init__(self, spec, *, url: str, token_file: str, blog_worker: str = BLOG_WORKER,
                 products_worker: str = PRODUCTS_WORKER) -> None:
        _Base.__init__(self, spec)      # no draft cadence: one a week, by its id
        for name, value in (("url", url), ("token_file", token_file),
                            ("blog_worker", blog_worker), ("products_worker", products_worker)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be named")
        self.url = url
        self.token_file = token_file
        self.blog_worker = blog_worker
        self.products_worker = products_worker

    def _blank(self) -> dict:
        return {"posts": [], "counts": {}, "week": None, "audience": None}

    # ---- one run -------------------------------------------------------------------------
    @never_raises()
    def run(self, ctx: WorkContext) -> Result:
        if ctx.state_dir is None:
            return self._err(ErrorKind.NOT_CONFIGURED, "no state dir: the worker cannot keep "
                             "its record, and without it could send a newsletter twice",
                             retryable=False)
        try:
            rec = self.load(ctx.state_dir)
        except _Unreadable as exc:
            return self._err(ErrorKind.MALFORMED, f"its record is unreadable ({exc}); "
                             "refusing to send, since it could send one twice", retryable=False)
        rec["last_run_at"] = ctx.now
        events: list = self._follow_up(ctx, rec)
        audience = self._audience(ctx, rec)
        got = self._this_week(ctx, rec, events)
        self.save(ctx.state_dir, rec)
        outputs = (*events, self._tally(ctx, rec), audience)
        # Scrooge unable to send (its 503 until POSTAL_ADDRESS and RESEND_API_KEY are set) is
        # the worker NOT CONFIGURED - a typed state the vitals and the brief say plainly, not
        # a failure to retry - with this run's rows kept alongside it.
        off = sender_off((rec.get("audience") or {}).get("stats")) or next(
            (p.get("why") for p in rec["posts"] if p.get("status") == "not_configured"
             and p.get("settled_at") == ctx.now), None)
        if off:
            err = self._err(ErrorKind.NOT_CONFIGURED, _clip(off, 300), retryable=False)
            return Err(replace(err.error, partial=outputs))
        if isinstance(got, Err):
            if not events:
                return got
            log.warning("%s: no newsletter this run: %s", self.worker_id, got.error)
        return Ok(outputs)

    # ---- 2. the audience, as Scrooge counts it ---------------------------------------------
    def _audience(self, ctx: WorkContext, rec: dict):
        stats, why, resp = None, None, None
        if ctx.http is None:
            why = "no network handle: the audience was not read"
        else:
            got = self._read_dash(ctx)
            if isinstance(got, Err):
                why = got.error.message
            else:
                doc, resp = got.value
                stats, why = read_stats(doc)
        rec["audience"] = {"at": ctx.now, "stats": stats, "why": _clip(why, 300) if why else None}
        if stats is None:
            return make_output(self, kind="newsletter.audience_unavailable", valid_at=ctx.now,
                               observed_at=ctx.now, payload={"why": _clip(why, 300)},
                               entities=self.entities,
                               provenance={"source": "real", "provider": self.provider,
                                           "url": self.url})
        s, last = stats["subscribers"], stats["last_send"]
        figures = [
            Figure(s["confirmed"], "count", "newsletter subscribers confirmed", window="now"),
            Figure(s["pending"], "count", "newsletter subscribers waiting to confirm",
                   window="now"),
            Figure(s["unsubscribed"], "count", "newsletter subscribers unsubscribed",
                   window="now"),
            Figure(stats["sends"], "count", "newsletters sent", window="all_time"),
            Figure(stats["sent_today"], "count", "newsletter emails sent today", window="today"),
        ]
        if last is not None:
            figures += [Figure(last["recipients"], "count", "last newsletter recipients",
                               last["newsletter_id"], "last_send"),
                        Figure(last["sent"], "count", "last newsletter emails sent",
                               last["newsletter_id"], "last_send"),
                        Figure(last["failed"], "count", "last newsletter emails failed",
                               last["newsletter_id"], "last_send")]
        return make_output(self, kind="newsletter.audience", valid_at=ctx.now,
                           observed_at=ctx.now,
                           payload={"last_send": last, "sender": stats["sender"],
                                    "daily_cap": stats["daily_cap"]},
                           figures=figures, entities=self.entities,
                           provenance=self._provenance(resp) if resp is not None
                           else {"source": "real", "provider": self.provider})

    # ---- 3. this week's newsletter --------------------------------------------------------
    def _this_week(self, ctx: WorkContext, rec: dict, events: list) -> Result | None:
        nid = newsletter_id(ctx.now)
        mine = [p for p in rec["posts"] if p.get("draft_id") == nid]
        settled = [p for p in mine if p.get("status") != "unreachable"]
        if settled or len(mine) >= RETRY_UNREACHABLE:
            last = (settled or mine)[-1]
            week = rec.get("week") if isinstance(rec.get("week"), dict) \
                and rec["week"].get("newsletter_id") == nid else {}
            rec["week"] = {**week, "newsletter_id": nid, "at": ctx.now,
                           "submitted": reached(last),
                           "why": None if reached(last) else (week.get("why")
                                                               or last.get("why"))}
            if last.get("status") == "unreachable":
                rec["week"]["why"] = f"Pionir never answered ({RETRY_UNREACHABLE} tries)"
            return None
        try:
            blog = read_record(ctx.state_dir, self.blog_worker)
            shelf = read_record(ctx.state_dir, self.products_worker)
        except _Unreadable as exc:
            rec["week"] = {"newsletter_id": nid, "at": ctx.now, "new_content": None,
                           "submitted": False, "why": f"a record it reads is unreadable ({exc})"}
            return self._err(ErrorKind.MALFORMED, f"a record it reads is unreadable ({exc}); "
                             "no newsletter assembled", retryable=False)
        posts, products = self.new_content(rec, blog, shelf, ctx.now)
        new = len(posts) + sum(1 for p in products if p["new"])
        week = {"newsletter_id": nid, "at": ctx.now, "new_content": new,
                "new_posts": [p["draft_id"] for p in posts],
                "new_products": [p["slug"] for p in products if p["new"]],
                "submitted": False, "skipped": None, "why": None}
        rec["week"] = week
        if not new:
            week.update(skipped="nothing_new", why=NOTHING_NEW)
            return None
        stats = (rec.get("audience") or {}).get("stats")
        off = sender_off(stats)
        if off:
            # Scrooge would answer 503: asking the owner to approve it wastes his yes
            week.update(skipped="not_configured", why=off)
            log.warning("%s: %s not submitted: %s", self.worker_id, nid, off)
            return None
        if stats is not None and stats["subscribers"]["confirmed"] == 0:
            week.update(skipped="no_subscribers", why=NO_SUBSCRIBERS)
            log.info("%s: %s not submitted: %s", self.worker_id, nid, NO_SUBSCRIBERS)
            return None
        draft = assemble(nid, posts, products)
        self._count(rec, "drafts_written")
        reasons = self.check_draft(draft, names=frozenset(p["name"] for p in products))
        entry = {"draft_id": nid, "subject": draft["subject"],
                 "blog_posts": week["new_posts"],
                 "products": [p["slug"] for p in products]}
        if reasons:
            self._count(rec, "drafts_blocked")
            rec["posts"].append({**entry, "status": "blocked",
                                 "reasons": [_clip(r, 200) for r in reasons[:12]],
                                 "settled_at": ctx.now})
            week["why"] = "blocked by the content check: " + "; ".join(
                _clip(r, 120) for r in reasons[:3])
            log.warning("%s: newsletter %s BLOCKED by the content check and NOT submitted: %s",
                        self.worker_id, nid, "; ".join(reasons))
            events.append(self._event(ctx, "post.blocked", {
                "draft_id": nid, "attempt": 1, "reasons_total": len(reasons),
                "reasons": [_clip(r, 90) for r in reasons[:3]]}))
            return None
        if ctx.job is None:
            week["why"] = "no hands: a newsletter reaches the owner only through Pionir"
            return self._err(ErrorKind.NOT_CONFIGURED, week["why"], retryable=False)
        # the shared machinery names a draft by ``draft_id``; the payload is built from
        # NEWSLETTER_FIELDS alone (``_job``), so the alias never reaches Pionir
        self._submit_post(ctx, rec, entry, {**draft, "draft_id": nid}, events)
        week["submitted"] = reached(entry)
        if not week["submitted"]:
            week["why"] = _clip(entry.get("why") or f"Pionir said {entry.get('status')}", 200)
        return None

    def new_content(self, rec: dict, blog: dict | None, shelf: dict | None,
                    now: float) -> tuple:
        """(new posts, products on sale): the blog posts published in the last week that no
        newsletter carried, and every product on sale - ``new`` when none carried it."""
        carried_posts, carried_products = set(), set()
        for p in rec["posts"]:
            if p.get("status") in FEATURED:
                carried_posts.update(x for x in p.get("blog_posts") or [] if isinstance(x, str))
                carried_products.update(x for x in p.get("products") or []
                                        if isinstance(x, str))
        posts = []
        for p in published_posts(blog):
            payload = p.get("payload") if isinstance(p.get("payload"), dict) else {}
            did, slug = p.get("draft_id"), p.get("slug") or payload.get("slug")
            title, desc = payload.get("title"), payload.get("description")
            if not all(isinstance(v, str) and v.strip() for v in (did, slug, title, desc)):
                continue
            if did in carried_posts or now - float(p.get("settled_at") or 0) > WEEK:
                continue
            posts.append({"draft_id": did, "slug": slug, "title": " ".join(title.split()),
                          "description": " ".join(desc.split())})
        latest: dict = {}
        for e in (shelf or {}).get("submissions") or []:
            if not isinstance(e, dict) or e.get("status") != "published":
                continue
            slug, name, url = e.get("slug"), e.get("name"), e.get("url")
            if not all(isinstance(v, str) and v.strip() for v in (slug, name, url)):
                continue
            try:
                parts = urlsplit(url)
            except ValueError:
                continue
            if parts.scheme != "https" or (parts.hostname or "") not in contentcheck.LINK_HOSTS \
                    or parts.query or parts.fragment or "@" in parts.netloc:
                continue
            at = float(e.get("settled_at") or 0)
            if slug not in latest or at >= latest[slug][0]:
                latest[slug] = (at, {"slug": slug, "name": " ".join(name.split()), "url": url})
        products = [{**v, "new": slug not in carried_products}
                    for slug, (_at, v) in sorted(latest.items())]
        return posts, products

    # ---- what a newsletter is --------------------------------------------------------------
    def check_draft(self, draft: dict, names: frozenset = frozenset()) -> list:
        return contentcheck.check_newsletter(draft, names=names)

    def published_link(self, result, post: dict) -> str | None:
        return _send_id(result)

    def _describe(self, draft: dict) -> dict:
        return {"subject": draft.get("subject")}

    def _job(self, draft: dict) -> Job:
        payload = {k: draft[k] for k in contentcheck.NEWSLETTER_FIELDS}   # exactly what was checked
        return Job(CAPABILITY, payload,
                   what=f"email the newsletter {draft['subject']!r} to every confirmed subscriber")

    def _settle_failed(self, ctx: WorkContext, rec: dict, post: dict, why: str, raw,
                       events: list) -> None:
        """Pionir refusing because Scrooge ALREADY has this newsletter means it is sent:
        recorded so with the send id Pionir gives - without one, as failed with the reason."""
        reason = _reason_of(raw) or why
        if any(a in reason.lower() or a in why.lower() for a in _ALREADY):
            sid = _send_id(raw)
            if sid is None:
                m = _SEND_ID_TEXT.search(reason) or _SEND_ID_TEXT.search(why)
                sid = m.group(0) if m else None
            if sid is not None:
                post.update({"status": "published", "send_id": sid, "settled_at": ctx.now,
                             "why": _clip(reason, 200)})
                self._count(rec, "published")
                log.info("%s: %s was already with Scrooge as send %s", self.worker_id,
                         post["draft_id"], sid)
                events.append(self._event(ctx, "post.published", {
                    "draft_id": post["draft_id"], "send_id": sid,
                    "subject": _clip(post.get("subject"), 90)}))
                return
        if _flagged(raw, "not_configured") or any(
                _NOT_CONFIGURED_TEXT.search(t) for t in (reason, why)):
            # Scrooge's 503 (no postal address or mail key), or Pionir's missing publish
            # token: typed, so it is said as such and never mistaken for a one-off failure.
            # Never retried this week; its posts are offered again once it can send.
            self._settle(ctx, rec, post, "not_configured", f"NOT CONFIGURED: {reason}", events)
            return
        self._settle(ctx, rec, post, "failed", reason, events)

    # ---- its pulse -------------------------------------------------------------------------
    def pulse(self, state_dir, now: float, digest=None) -> dict:
        """What it has submitted and sent, this week's standing, and the audience as last
        read. ``alert`` only when this week had something new and no newsletter reached
        the owner: a quiet week, or a list with no confirmed subscriber, is not an alarm."""
        out: dict = {"worker": self.worker_id, "capability": self.capability}
        if state_dir is None:
            return {**out, "alert": "no state dir: it cannot keep its record"}
        try:
            rec = self.load(state_dir)
        except _Unreadable as exc:
            return {**out, "alert": f"its record is unreadable ({exc})"}
        c = rec.get("counts") or {}
        posts = [p for p in rec.get("posts") or [] if isinstance(p, dict)]
        handed = [float(p["submitted_at"]) for p in posts
                   if isinstance(p.get("submitted_at"), (int, float)) and reached(p)]
        sent = [float(p.get("settled_at") or 0) for p in posts if p.get("status") == "published"]
        week = rec.get("week") if isinstance(rec.get("week"), dict) else {}
        nid = newsletter_id(now)
        current = week.get("newsletter_id") == nid
        audience = rec.get("audience") if isinstance(rec.get("audience"), dict) else {}
        stats = audience.get("stats") if isinstance(audience.get("stats"), dict) else None
        out.update({
            "last_run_at": rec.get("last_run_at"),
            "this_week": nid,
            "new_content": week.get("new_content") if current else None,
            "skipped": week.get("skipped") if current else None,
            "submitted": int(c.get("submitted_for_approval", 0)),
            "pending": sum(1 for p in posts if p.get("status") == "pending_approval"),
            "published": int(c.get("published", 0)),
            "denied": int(c.get("denied", 0)),
            "failed": int(c.get("failed", 0)),
            "not_configured": int(c.get("not_configured", 0)),
            "drafts_blocked": int(c.get("drafts_blocked", 0)),
            "last_submitted_at": max(handed) if handed else None,
            "last_published_at": max(sent) if sent else None,
            "subscribers": stats["subscribers"] if stats else None,
            "sends": stats["sends"] if stats else None,
            "last_send": stats["last_send"] if stats else None,
            "audience_unavailable": audience.get("why") if stats is None else None,
            "alert": None,
        })
        if current and week.get("new_content") and not week.get("submitted") \
                and week.get("skipped") is None:
            out["alert"] = (f"there was new content for {nid} ({week['new_content']} new "
                            f"item(s)) but no newsletter reached the owner: "
                            f"{week.get('why') or 'nothing was submitted'}")
        return out

    # ---- what the leader reads ------------------------------------------------------------
    def _tally(self, ctx: WorkContext, rec: dict):
        c = rec["counts"]
        pending = [p["draft_id"] for p in rec["posts"] if p.get("status") == "pending_approval"]
        week = rec.get("week") if isinstance(rec.get("week"), dict) else {}
        figures = [
            Figure(int(c.get("submitted_for_approval", 0)), "count",
                   "newsletters submitted for approval", window="all_time"),
            Figure(len(pending), "count", "newsletters pending approval", window="now"),
            Figure(int(c.get("published", 0)), "count", "newsletters approved and queued",
                   window="all_time"),
            Figure(int(c.get("denied", 0)), "count", "newsletters denied", window="all_time"),
            Figure(int(c.get("failed", 0)), "count", "newsletters failed", window="all_time"),
            Figure(int(c.get("drafts_blocked", 0)), "count", "newsletters blocked",
                   window="all_time"),
        ]
        if isinstance(week.get("new_content"), int):
            figures.append(Figure(week["new_content"], "count",
                                  "new items for this week's newsletter", window="now"))
        return make_output(self, kind="post.tally", valid_at=ctx.now, observed_at=ctx.now,
                           payload={"pending_approval": pending[-3:],
                                    "this_week": {k: week.get(k) for k in
                                                  ("newsletter_id", "submitted", "skipped",
                                                   "why")}},
                           figures=figures, entities=self.entities,
                           provenance={"source": "real", "provider": self.provider,
                                       "record": self.record_what})
