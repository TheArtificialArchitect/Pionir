"""The feedback desk: one feedback request per delivered order, and each consented testimonial
proposed to the owner - every step for his yes.

``contracts.feedback``. **No model, no words.** The feedback request is the one fixed template
below (``FEEDBACK_SUBJECT`` / ``FEEDBACK_BODY``) with the order's id and the client's name filled
in; ``{feedback_link}`` and ``{referral_code}`` stay as they are for the owner to read on the card
and are filled in by Scrooge, which mints both (worker/src/feedback.ts). A testimonial is the
client's own words, proposed exactly as stored - never edited, never summarised.

One run:

1. **The orders**: ``Job("client.orders", {})``. Unavailable or not set up is an honest
   ``UNAVAILABLE`` / ``NOT_CONFIGURED``, never "nothing to do", and the run does nothing else.
2. **Follow up** every request and proposal waiting on the owner (``ctx.approval``): approved
   and done is SENT / LIVE; denied, failed or unknown is not. Nothing is assumed.
3. **Feedback requests**, oldest delivery first, at most ``MAX_REQUESTS_PER_RUN`` a run: an
   order that is ``delivered``, whose Scrooge listing says it has never been asked
   (``feedback.requested_at`` null) and may be from a time that has come (``feedback.eligible_from``
   - delivered at least ``FEEDBACK_AFTER_SECONDS`` ago, no refund or lost dispute; Scrooge refuses
   the rest anyway). The email is built, checked fail-closed (``check_feedback_email``) on the
   exact payload, and submitted as ``client.email`` with ``kind: "feedback_request"``; Pionir parks
   it for the owner.
4. **Testimonials**: ``Job("client.testimonials", {})`` lists the consented ones waiting. Each new
   one is checked by Pionir's content rules (``adapters/testimonials.check_testimonial``: the
   blog's text checks and stricter ones - no links, no contact details, a name that is a name)
   and, if it passes, submitted as ``client.testimonial_publish`` for the owner's yes, at most
   ``MAX_PROPOSALS_PER_RUN`` a run. One that fails is recorded as BLOCKED with its reasons and
   never proposed (the owner may hide it on Scrooge).

**Never twice.** Every request and proposal has an entry in the record
(``contracts.feedback.json``); an order is asked once and a testimonial proposed once, except a
submission that never reached Pionir (``unreachable``), retried on up to ``RETRY_UNREACHABLE``
runs. Scrooge refuses a second request whatever this record says.

**Money.** A referral credit ($25 off the referrer's next order) is a record on Scrooge; this
desk only counts the credits earned, for the owner to apply by hand. It never discounts,
refunds or moves anything.
"""
from __future__ import annotations

import math
import re

from pionir.adapters.testimonials import check_testimonial

from .blog import FORGOTTEN_AFTER, _clip, _Unreadable, read_record, record_path, save_record
from .figures import Figure
from .hands import Job, outcome_of
from .log import log
from .orders import HIRE_URL, _already_sent, _epoch, _orders_of, check_email, greeting_name
from .result import Err, Ok, Result
from .worker import ErrorKind, WorkContext, make_output, never_raises
from .workers import _Base

ORDERS = "client.orders"
EMAIL = "client.email"
TESTIMONIALS = "client.testimonials"
PUBLISH = "client.testimonial_publish"
# The kind and placeholders Scrooge fills in (worker/src/feedback.ts; adapters/clients.py has
# the same - a contract test pins all three).
FEEDBACK_KIND = "feedback_request"
FEEDBACK_LINK = "{feedback_link}"
REFERRAL_CODE = "{referral_code}"
REFERRAL_CREDIT_CENTS = 2500
FEEDBACK_AFTER_SECONDS = 3 * 86400
MAX_REQUESTS_PER_RUN = 2
MAX_PROPOSALS_PER_RUN = 2
RETRY_UNREACHABLE = 5
_ORDER_ID = re.compile(r"[0-9a-f]{12}")
_NOT_SET_UP = re.compile(r"(?i)not[ _-]?configured|not set up|unknown capability|"
                         r"no such capability|not wired|no capability")

# ---- the template: every word a client gets from this desk ---------------------------------
# {name} is the name the client gave (or "there"), {order_id} the order's id. {feedback_link}
# and {referral_code} are NOT filled in here: Scrooge mints them when the owner's yes sends it.
FEEDBACK_SUBJECT = "How did your Dokaz order {order_id} go?"
FEEDBACK_BODY = """Hello {name},

It's been a few days since we delivered your order {order_id}. We hope it's doing its job.

Would you tell us how it went? It takes two minutes:
{feedback_link}

That link is private to you and works for 60 days. If you're happy for us to, you can let us \
show your words on our website - only if you tick the box, and only after we've read them.

Your referral code is {referral_code}. If someone you know needs a script, a data clean-up or \
a small tool, send them to https://api.dokaz.net/hire?ref={referral_code} - when they order, you \
get $25 off your next order.

Thank you,
Dokaz"""
_REFERRAL_LINK = f"{HIRE_URL}?ref={REFERRAL_CODE}"


def build_feedback_email(order: dict) -> dict:
    """The exact ``client.email`` payload of this order's feedback request. Pure."""
    oid = order.get("id")
    body = FEEDBACK_BODY.replace("{name}", greeting_name(order.get("name"))) \
        .replace("{order_id}", str(oid))
    return {"order_id": oid, "to": order.get("email"), "kind": FEEDBACK_KIND,
            "subject": FEEDBACK_SUBJECT.format(order_id=oid), "body_text": body}


def check_feedback_email(payload: dict, order: dict) -> list:
    """Every reason this feedback request may not go out; empty is the only pass. The order
    desk's own checks (orders.check_email) on the text with its placeholders taken out, plus:
    exactly the template, the kind, and the placeholders where Scrooge expects them."""
    reasons: list = []
    if payload.get("kind") != FEEDBACK_KIND:
        reasons.append(f"the kind is not {FEEDBACK_KIND!r}")
    if payload != build_feedback_email(order):
        reasons.append("the email is not exactly the feedback template for this order")
    body = payload.get("body_text")
    if not isinstance(body, str):
        return [*reasons, "the body is not text"]
    if body.count(FEEDBACK_LINK) != 1:
        reasons.append(f"the body must carry {FEEDBACK_LINK} exactly once")
    if not 1 <= body.count(REFERRAL_CODE) <= 3:
        reasons.append(f"the body must carry {REFERRAL_CODE} one to three times")
    plain = body.replace(_REFERRAL_LINK, HIRE_URL).replace(FEEDBACK_LINK, "") \
        .replace(REFERRAL_CODE, "ABCDEFGH")
    reasons += check_email({**{k: payload.get(k) for k in ("order_id", "to", "subject")},
                            "body_text": plain}, order, REFERRAL_CREDIT_CENTS)
    return reasons


def _feedback_of(order: dict) -> dict | None:
    f = order.get("feedback") if isinstance(order, dict) else None
    return f if isinstance(f, dict) else None


def request_due(order: dict, now: float) -> bool:
    """Delivered, never asked (by Scrooge's listing), and the day has come."""
    if order.get("status") != "delivered" or not _ORDER_ID.fullmatch(str(order.get("id"))):
        return False
    f = _feedback_of(order)
    if f is None or f.get("requested_at"):
        return False
    at = _epoch(f.get("eligible_from"))
    return at is not None and at <= now


def _stats_of(result) -> tuple:
    """(testimonials, stats) from Pionir's answer, or (None, None)."""
    for doc in (result, result.get("result") if isinstance(result, dict) else None):
        if isinstance(doc, dict) and isinstance(doc.get("testimonials"), list):
            if doc.get("ok") is False:
                return None, None
            stats = doc.get("stats") if isinstance(doc.get("stats"), dict) else {}
            return doc["testimonials"], stats
    return None, None


def _safe_reason(error) -> str:
    """A check's refusal without any of the client's text in it (a quoted match is cut), for the
    rows the leader reads."""
    return _clip(re.sub(r"'[^']*'|\"[^\"]*\"", "(...)", str(error)), 120)


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) \
        else None


class FeedbackDesk(_Base):
    """``contracts.feedback``: one feedback request per delivered order, and each consented
    testimonial proposed, both for the owner's yes."""

    record_what = "the feedback desk's own record of every request and proposal it submitted"

    def record_path(self, state_dir):
        return record_path(state_dir, self.worker_id)

    @staticmethod
    def _blank() -> dict:
        return {"requests": [], "testimonials": []}

    def load(self, state_dir) -> dict:
        doc = read_record(state_dir, self.worker_id)
        return self._blank() if doc is None else {**self._blank(), **doc}

    @never_raises()
    def run(self, ctx: WorkContext) -> Result:
        if ctx.state_dir is None:
            return self._err(ErrorKind.NOT_CONFIGURED, "no state dir: the desk cannot keep its "
                             "record, and without it could ask a client twice", retryable=False)
        if ctx.job is None:
            return self._err(ErrorKind.NOT_CONFIGURED, "no hands: orders are read and clients "
                             "emailed only through Pionir", retryable=False)
        try:
            rec = self.load(ctx.state_dir)
        except _Unreadable as exc:
            return self._err(ErrorKind.MALFORMED, f"its record is unreadable ({exc}); refusing "
                             "to ask anyone, since it could ask a client twice", retryable=False)
        out = ctx.job(Job(ORDERS, {}, what="read the client orders"))
        orders = _orders_of(out.result) if out.status == "done" else None
        if orders is None:
            why = out.error or f"Pionir said {out.status}"
            if out.status == "done":
                return self._err(ErrorKind.MALFORMED, "Pionir answered client.orders without an "
                                 "order list; the orders are UNKNOWN, not zero")
            if out.status == "failed" and _NOT_SET_UP.search(why):
                return self._err(ErrorKind.NOT_CONFIGURED, f"client.orders is not set up "
                                 f"({_clip(why, 160)})", retryable=False)
            return self._err(ErrorKind.UNAVAILABLE, f"could not read the orders ({out.status}: "
                             f"{_clip(why, 160)}); they are UNKNOWN, not zero")
        orders = [o for o in orders if isinstance(o, dict) and isinstance(o.get("id"), str)]
        by_id = {o["id"]: o for o in orders}
        events: list = []
        self._follow_up(ctx, rec, by_id, events)
        ready = self._ask(ctx, rec, orders, events)
        listed, stats = self._propose(ctx, rec, events)
        save_record(self.record_path(ctx.state_dir), rec)
        return Ok((*events, self._tally(ctx, rec, orders, ready, listed, stats)))

    # ---- what became of the ones waiting --------------------------------------------------
    def _follow_up(self, ctx: WorkContext, rec: dict, by_id: dict, events: list) -> None:
        for kind, capability in (("requests", EMAIL), ("testimonials", PUBLISH)):
            for e in rec[kind]:
                if e.get("status") != "pending_approval" or not e.get("approval_id") \
                        or ctx.approval is None:
                    continue
                got = ctx.approval(e["approval_id"]) or {}
                state = got.get("status")
                if state in ("pending", "running", "unreachable"):
                    continue
                if state == "denied":
                    self._settle(ctx, e, kind, "denied", "the owner did not approve it", events)
                elif state in ("approved", "approved_failed"):
                    res = outcome_of(capability, got.get("result"))
                    order = by_id.get(e.get("order_id"))
                    if res.ran or (kind == "requests" and order is not None
                                   and _already_sent(order, e.get("subject"))):
                        self._settle(ctx, e, kind, "sent" if kind == "requests" else "live",
                                     "approved by the owner", events)
                    else:
                        self._settle(ctx, e, kind, "failed", res.error or "it did not run",
                                     events)
                elif state == "unknown" and ctx.now - float(e.get("submitted_at") or ctx.now) \
                        > FORGOTTEN_AFTER:
                    self._settle(ctx, e, kind, "unknown", "Pionir no longer lists this approval",
                                 events)

    def _settle(self, ctx: WorkContext, e: dict, kind: str, status: str, why: str,
                events: list) -> None:
        e.update(status=status, why=_clip(why, 300), settled_at=ctx.now)
        what = "feedback.request" if kind == "requests" else "feedback.testimonial"
        log.info("%s: %s for %s is %s", self.worker_id, what, e.get("order_id"), status)
        events.append(self._event(ctx, f"{what}_{status}", {
            k: e[k] for k in ("order_id", "testimonial_id") if e.get(k)}))

    # ---- 3. the one request per delivered order ---------------------------------------------
    def _ask(self, ctx: WorkContext, rec: dict, orders: list, events: list) -> int:
        submitted = ready = 0
        for order in sorted(orders, key=lambda o: (_epoch((_feedback_of(o) or {})
                                                          .get("eligible_from")) or math.inf,
                                                   o["id"])):
            if not request_due(order, ctx.now):
                continue
            tries = [e for e in rec["requests"] if e.get("order_id") == order["id"]]
            if any(e.get("status") != "unreachable" for e in tries) \
                    or len(tries) >= RETRY_UNREACHABLE:
                continue
            email = build_feedback_email(order)
            entry = {"order_id": order["id"], "to": email["to"], "subject": email["subject"]}
            if _already_sent(order, email["subject"]):
                rec["requests"].append({**entry, "status": "sent", "settled_at": ctx.now,
                                        "why": "the order's messages already show it sent"})
                continue
            reasons = check_feedback_email(email, order)
            if reasons:
                rec["requests"].append({**entry, "status": "blocked", "settled_at": ctx.now,
                                        "reasons": [_clip(r, 200) for r in reasons[:8]]})
                log.warning("%s: the feedback request for %s was BLOCKED: %s", self.worker_id,
                            order["id"], "; ".join(reasons))
                events.append(self._event(ctx, "feedback.request_blocked", {
                    "order_id": order["id"], "reasons": [_clip(r, 90) for r in reasons[:3]]}))
                continue
            if submitted >= MAX_REQUESTS_PER_RUN:
                ready += 1
                continue
            out = ctx.job(Job(EMAIL, dict(email),
                              what=f"ask the client of order {order['id']} for feedback"))
            submitted += 1
            entry.update(submitted_at=ctx.now, status=out.status, approval_id=out.approval_id)
            rec["requests"].append(entry)
            if out.status == "pending_approval":
                events.append(self._event(ctx, "feedback.request_pending", {
                    "order_id": order["id"], "approval_id": out.approval_id}))
            elif out.status == "done":
                log.error("%s: client.email ran WITHOUT the owner's approval for %s; it must be "
                          "approval-gated in Pionir", self.worker_id, order["id"])
                entry["approved_by_owner"] = False
                self._settle(ctx, entry, "requests", "sent", "sent without being parked", events)
            elif out.status == "unreachable":
                self._settle(ctx, entry, "requests", "unreachable",
                             out.error or "never reached Pionir", events)
            else:
                self._settle(ctx, entry, "requests", "failed",
                             out.error or f"Pionir said {out.status}", events)
        return ready

    # ---- 4. each consented testimonial, proposed once ---------------------------------------
    def _propose(self, ctx: WorkContext, rec: dict, events: list) -> tuple:
        out = ctx.job(Job(TESTIMONIALS, {}, what="read the testimonials waiting for the owner"))
        listed, stats = _stats_of(out.result) if out.status == "done" else (None, None)
        if listed is None:
            log.warning("%s: the testimonials could not be read (%s); they are UNKNOWN",
                        self.worker_id, out.error or out.status)
            return None, None
        proposed = 0
        for t in listed:
            if not isinstance(t, dict) or not isinstance(t.get("id"), str):
                continue
            tries = [e for e in rec["testimonials"] if e.get("testimonial_id") == t["id"]]
            if any(e.get("status") != "unreachable" for e in tries) \
                    or len(tries) >= RETRY_UNREACHABLE:
                continue
            payload = {"testimonial_id": t["id"], "order_id": t.get("order_id"),
                       "rating": t.get("rating"), "display_name": t.get("display_name"),
                       "body": t.get("body")}
            entry = {"testimonial_id": t["id"], "order_id": t.get("order_id"),
                     "rating": t.get("rating")}
            try:
                check_testimonial(payload)
            except ValueError as error:
                rec["testimonials"].append({**entry, "status": "blocked", "settled_at": ctx.now,
                                            "reasons": [_safe_reason(error)]})
                log.warning("%s: testimonial %s was BLOCKED by the content checks: %s",
                            self.worker_id, t["id"], error)
                events.append(self._event(ctx, "feedback.testimonial_blocked", {
                    "testimonial_id": t["id"], "reason": _safe_reason(error)}))
                continue
            if proposed >= MAX_PROPOSALS_PER_RUN:
                continue
            res = ctx.job(Job(PUBLISH, payload,
                              what=f"show testimonial {t['id']} on the hire page"))
            proposed += 1
            entry.update(submitted_at=ctx.now, status=res.status, approval_id=res.approval_id)
            rec["testimonials"].append(entry)
            if res.status == "pending_approval":
                events.append(self._event(ctx, "feedback.testimonial_pending", {
                    "testimonial_id": t["id"], "approval_id": res.approval_id}))
            elif res.status == "done":
                log.error("%s: client.testimonial_publish ran WITHOUT the owner's approval",
                          self.worker_id)
                self._settle(ctx, entry, "testimonials", "live", "published without being "
                             "parked", events)
            elif res.status == "unreachable":
                self._settle(ctx, entry, "testimonials", "unreachable",
                             res.error or "never reached Pionir", events)
            else:
                self._settle(ctx, entry, "testimonials", "failed",
                             res.error or f"Pionir said {res.status}", events)
        return listed, stats

    # ---- what the leader reads --------------------------------------------------------------
    def _event(self, ctx: WorkContext, kind: str, payload: dict, figures=()):
        # never the client's name, address or words: ids and statuses only
        return make_output(self, kind=kind, valid_at=ctx.now, observed_at=ctx.now,
                           payload=payload, figures=figures, entities=self.entities,
                           provenance={"source": "real", "provider": self.provider,
                                       "record": self.record_what})

    def _tally(self, ctx: WorkContext, rec: dict, orders: list, ready: int, listed, stats):
        def n(kind: str, *statuses) -> int:
            return sum(1 for e in rec[kind] if e.get("status") in statuses)

        due = sum(1 for o in orders if request_due(o, ctx.now))
        figures = [
            Figure(due, "count", "delivered orders due a feedback request", window="now"),
            Figure(n("requests", "pending_approval"), "count",
                   "feedback requests pending the owner's approval", window="now"),
            Figure(n("requests", "sent"), "count", "feedback requests sent by this desk",
                   window="all_time"),
            Figure(n("requests", "denied"), "count", "feedback requests denied",
                   window="all_time"),
            Figure(n("requests", "failed", "unknown"), "count", "feedback requests failed",
                   window="all_time"),
            Figure(n("requests", "blocked"), "count",
                   "feedback requests blocked by the template check", window="all_time"),
            Figure(n("testimonials", "pending_approval"), "count",
                   "testimonials pending the owner's approval", window="now"),
            Figure(n("testimonials", "blocked"), "count",
                   "testimonials blocked by the content checks", window="all_time"),
            Figure(n("testimonials", "denied"), "count", "testimonials the owner declined",
                   window="all_time"),
        ]
        payload: dict = {"requests_ready_next_run": ready,
                         "blocked_testimonials": [
                             {"testimonial_id": e.get("testimonial_id"),
                              "reasons": e.get("reasons", [])[:1]}
                             for e in rec["testimonials"] if e.get("status") == "blocked"][-5:]}
        if stats is None:
            payload["scrooge_counts"] = "UNKNOWN: the testimonials could not be read"
        else:
            for key, measures in (("feedback_requested", "feedback requests sent"),
                                  ("feedback_received", "feedback answers received"),
                                  ("testimonials_live", "testimonials live on the hire page"),
                                  ("testimonials_pending", "testimonials waiting on Scrooge"),
                                  ("referral_orders", "orders placed through a referral code"),
                                  ("referral_orders_paid", "referral orders paid")):
                v = _num(stats.get(key))
                if v is not None:
                    figures.append(Figure(int(v), "count", measures, window="all_time"))
            avg = _num(stats.get("avg_rating"))
            if avg is not None:
                figures.append(Figure(round(float(avg), 2), "count",
                                      "average client rating, in stars out of 5",
                                      window="all_time"))
            earned = _num(stats.get("credits_earned_cents"))
            if earned is not None and float(earned).is_integer():
                figures.append(Figure(int(earned), "usd_cents",
                                      "referral credits to apply by hand", window="now"))
        return make_output(self, kind="feedback.tally", valid_at=ctx.now, observed_at=ctx.now,
                           payload=payload, figures=figures, entities=self.entities,
                           provenance={"source": "real", "provider": self.provider,
                                       "record": self.record_what})
