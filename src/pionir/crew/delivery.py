"""The delivery desk: each paid order's finished work shipped to its client, by fixed
template, for the owner's yes.

The owner builds an order's work and drops it as a zip in the order's own folder:
``<deliveries_dir>/<order_id>/<anything>.zip`` (``CrewSettings.deliveries_dir``, by default
``~/.pionir/deliveries`` - the folder Pionir reads too). This worker finds it and asks Pionir
to ship it: ``Job("client.deliver", {order_id, to, zip_name, zip_sha256, subject,
body_text})``. Pionir checks the zip BEFORE anything is parked (missing or changed, too big,
not a zip, unsafe paths, executables, no README or HOWTO, secrets) and refuses it with the
reason; a zip that passes is parked for the owner, and on his yes Pionir uploads it, puts
the private link where the email says ``{link}``, and emails the client.

**No model, no words.** The email is one of the two templates below (``DELIVERY_*``, or
``REVISION_*`` for a revised build) with the order's id and the client's plain name filled
in, and ``{link}`` left for Pionir. It is checked (``check_delivery``, fail closed) on the
exact payload before it is submitted.

One run:

1. **The orders**: ``Job("client.orders", {})``, read exactly as the order desk reads them.
   Unreadable is an honest ``UNAVAILABLE`` / ``NOT_CONFIGURED``, never "nothing to deliver",
   and the run then does nothing else.
2. **Follow up** every delivery waiting on the owner: approved and done (Pionir says it was
   emailed) is DELIVERED; denied is DENIED; refused on approval is FAILED; an approved one
   that failed only for an outage (the mail or the upload down, or uploaded but "NOT
   emailed") is UNDELIVERED and offered to him again, up to ``RETRY_UNDELIVERED`` times.
   Nothing is ever assumed delivered.
3. **New deliveries**, oldest order first, at most ``MAX_DELIVERIES_PER_RUN`` a run. An order
   is considered when it is ``in_progress`` (the first delivery) or has been delivered by this
   desk and has a NEWER zip than the last one delivered (a revision). Only the newest zip in
   its folder is looked at - an older one the owner has superseded is never sent - and only
   once it has sat unchanged for ``SETTLE_SECONDS`` (not half-copied).

**Half the price still owed.** A quoted order paid 50% up front (pionir/quotes.py) owes the
other 50% on delivery, and the client never gets the work before it is paid. So for an
order that owes its balance (``balance_owed``: its quote's deposit is paid, its balance is
not) the zip is submitted with ``hold_for_balance: true`` and the ``BALANCE_*`` template:
Scrooge stores it with NO link, and ``{link}`` becomes the link to PAY the balance. Once the
webhook has marked the balance paid (``balance_paid``), this desk asks the owner to release
the held delivery: ``Job("client.release", {order_id, to, delivery_id, subject,
body_text})`` with the ``RELEASE_*`` template, whose ``{link}`` becomes the download link.

**Never twice.** Every zip is known by its sha256. A zip submitted for an order is never
submitted for it again - delivered, denied, refused by Pionir's checks, blocked by the
template check or failed - except a submission that never reached Pionir (``unreachable``,
up to ``RETRY_UNREACHABLE`` runs) or an approved one that hit an outage (``undelivered``,
up to ``RETRY_UNDELIVERED``). A zip Pionir's checks REFUSED (a secret in it, say) is
recorded with the reason and counted as blocked until the owner drops a fixed zip: a
different zip is a new attempt. Only one delivery per order waits on the owner at a time.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import re
from pathlib import Path

from .blog import FORGOTTEN_AFTER, _clip, _Unreadable, read_record, record_path, save_record
from .figures import Figure
from .hands import Job, outcome_of
from .log import log
from .orders import (
    _CONTROL,
    _DOMAIN,
    _EMAIL,
    _MONEY,
    _NOT_SET_UP,
    _ORDER_ID,
    _URL,
    FIND,
    PACKAGES,
    RETRY_UNDELIVERED,
    RETRY_UNREACHABLE,
    OrderDesk,
    _cents_of,
    _epoch,
    _oldest_first,
    balance_owed,
    greeting_name,
    package_of,
    price_text,
)
from .result import Err, Ok, Result
from .worker import ErrorKind, WorkContext, make_output, never_raises
from .workers import _Base

DELIVER = "client.deliver"
RELEASE_CAP = "client.release"
LINK = "{link}"                     # Pionir puts the private download link here
MAX_DELIVERIES_PER_RUN = 1
RETRYABLE = {"unreachable": RETRY_UNREACHABLE, "undelivered": RETRY_UNDELIVERED}
SETTLE_SECONDS = 120                # a zip changed more recently may still be being copied
PAYLOAD_KEYS = frozenset({"order_id", "to", "zip_name", "zip_sha256", "subject", "body_text"})
RELEASE_KEYS = frozenset({"order_id", "to", "delivery_id", "subject", "body_text"})
FIRST, REVISION = "delivery", "revision"
HELD, RELEASE = "held_delivery", "release"      # the balance request; the files, once paid
_DELIVERY_ID = re.compile(r"dl_[0-9a-f]{24}")

# ---- the templates: every word a client gets with the work -------------------------------
# {name} is the name the client gave (or "there" when it is not a plain name), {order_id} the
# order's id. {link} is NOT filled in here: it is left for Pionir, which puts the private
# download link there when it sends the email.

DELIVERY_SUBJECT = "Your Dokaz delivery for order {order_id}"
DELIVERY_BODY = """Hello {name},

Your order {order_id} is ready.

Download it here:
{link}

The link works for 30 days. The zip file contains the build, plus a README or HOWTO that \
explains how to set it up and use it.

One round of revisions is included free: if anything needs changing, reply to this email \
and tell us what.

Thank you,
Dokaz"""

REVISION_SUBJECT = "Your revised Dokaz delivery for order {order_id}"
REVISION_BODY = """Hello {name},

Your order {order_id} is ready again: this is the revised version.

Download it here:
{link}

The link works for 30 days. The zip file contains the build, plus a README or HOWTO that \
explains how to set it up and use it.

If you have a question about it, reply to this email.

Thank you,
Dokaz"""

_ZIP_NAME = re.compile(r"[^\\/:*?\"<>|\x00-\x1f\x7f]{1,200}\.zip", re.IGNORECASE)
_SHA256 = re.compile(r"[0-9a-f]{64}")
# Pionir's typed errors that are a passing problem, not a verdict on the zip (the type comes
# through JobOutcome.error_type; the wording regex below is only for an untyped failure)
PASSING_TYPES = frozenset({"AdapterUnavailable", "AdapterAuthenticationError", "CircuitOpen",
                           "ResourceUnavailable", "BodyDeferred"})
# a failure that is Pionir or a service passing through a bad moment, not a verdict on the zip
_PASSING = re.compile(r"(?i)\b(?:unavailable|timed out|timeout|circuit (?:is )?open|"
                      r"try again later|not answering|temporarily)\b")


# The work is ready but half the price is still owed: {link} is the balance pay link, never
# the files. {balance} is the balance Scrooge recorded for the owner's quote.
BALANCE_SUBJECT = "Your Dokaz order {order_id} is ready - balance due"
BALANCE_BODY = """Hello {name},

Your order {order_id} is ready.

The balance of {balance} is now due. Pay it securely here:
{link}

As soon as it is paid, we'll email you the private link to download your files. This \
payment link is valid for 14 days.

Thank you,
Dokaz"""

# The balance is paid: {link} is the private download link of the files held until now.
RELEASE_SUBJECT = "Your files for Dokaz order {order_id}"
RELEASE_BODY = """Hello {name},

Thank you - your balance is paid. Your order {order_id} is ready to download:
{link}

The link works for 30 days. The zip file contains the build, plus a README or HOWTO that \
explains how to set it up and use it.

One round of revisions is included free: if anything needs changing, reply to this email \
and tell us what.

Thank you,
Dokaz"""


# ---- the check: fail closed, on the exact payload that would be submitted ------------------
def check_delivery(payload: dict, order: dict, balance_cents: int | None = None) -> list:
    """Every reason this delivery may not be submitted; empty is the only pass. With
    ``balance_cents`` it is a held delivery's balance request: it must say so
    (``hold_for_balance``) and may state that one amount."""
    reasons: list = []
    keys = PAYLOAD_KEYS | ({"hold_for_balance"} if balance_cents is not None else set())
    if set(payload) != keys:
        reasons.append("the payload is not exactly order_id, to, zip_name, zip_sha256, "
                       "subject, body_text" + (", hold_for_balance" if balance_cents is not None
                                               else ""))
    if balance_cents is not None and payload.get("hold_for_balance") is not True:
        reasons.append("a delivery for an order that owes its balance must be held")
    oid, to = payload.get("order_id"), payload.get("to")
    subject, body = payload.get("subject"), payload.get("body_text")
    name, sha = payload.get("zip_name"), payload.get("zip_sha256")
    if oid != order.get("id") or not isinstance(oid, str) or not _ORDER_ID.fullmatch(oid):
        reasons.append("order_id is not this order's plain id")
    if not isinstance(to, str) or to != order.get("email") or not _EMAIL.fullmatch(to):
        reasons.append("the recipient is not exactly the order's email address")
    if not isinstance(name, str) or not _ZIP_NAME.fullmatch(name) or name.startswith("."):
        reasons.append("zip_name is not a plain .zip file name")
    if not isinstance(sha, str) or not _SHA256.fullmatch(sha):
        reasons.append("zip_sha256 is not a sha256 in lowercase hex")
    if not isinstance(subject, str) or not 5 <= len(subject) <= 120:
        reasons.append("the subject must be 5 to 120 characters")
    elif "\n" in subject or "\r" in subject or _CONTROL.search(subject):
        reasons.append("the subject must be a single line")
    if not isinstance(body, str) or not 20 <= len(body) <= 5000:
        reasons.append("the body must be 20 to 5000 characters")
    elif _CONTROL.search(body):
        reasons.append("the body has control characters")
    if isinstance(body, str) and body.count(LINK) != 1:
        reasons.append(f"the body must hold {LINK} exactly once, not {body.count(LINK)} times")
    for field, text in (("subject", subject), ("body", body)):
        if not isinstance(text, str):
            continue
        rest = text.replace(LINK, " ") if field == "body" else text
        if field == "subject" and LINK in text:
            reasons.append(f"the subject holds {LINK}")
        if "<" in text or ">" in text:
            reasons.append(f"the {field} is not plain text (it has < or >)")
        if "{" in rest or "}" in rest:
            reasons.append(f"the {field} has a brace outside {LINK} (a template left unfilled)")
        for m in _URL.finditer(rest):
            reasons.append(f"the {field} has a link of its own: {m.group(0)}")
            rest = rest.replace(m.group(0), " ")
        for m in _DOMAIN.finditer(rest):
            reasons.append(f"the {field} names an address or a domain: {m.group(0)}")
        for m in _MONEY.finditer(text):
            if balance_cents is not None and _cents_of(m.group(0)) == balance_cents:
                continue
            reasons.append(f"the {field} states the amount {m.group(0).strip()}, which a "
                           "delivery email may not")
    return reasons


def check_release(payload: dict, order: dict) -> list:
    """Every reason this release may not be submitted; empty is the only pass. The email is
    checked by the delivery rules (one {link}, no link or amount of its own)."""
    reasons: list = []
    if set(payload) != RELEASE_KEYS:
        reasons.append("the payload is not exactly order_id, to, delivery_id, subject, "
                       "body_text")
    held = order.get("held_delivery") if isinstance(order.get("held_delivery"), dict) else {}
    did = payload.get("delivery_id")
    if not isinstance(did, str) or not _DELIVERY_ID.fullmatch(did) or did != held.get("id"):
        reasons.append("delivery_id is not the delivery Scrooge holds for this order")
    as_delivery = {k: v for k, v in payload.items() if k != "delivery_id"}
    as_delivery.update(zip_name="held.zip", zip_sha256="0" * 64)
    reasons += [r for r in check_delivery(as_delivery, order) if not r.startswith("the payload")]
    return reasons


def build_delivery(kind: str, order: dict, zip_name: str, zip_sha256: str) -> dict:
    """The exact ``client.deliver`` payload for this order and zip. Pure."""
    oid = order.get("id")
    extra: dict = {}
    fill: dict = {}
    if kind == FIRST:
        subject_t, body_t = DELIVERY_SUBJECT, DELIVERY_BODY
    elif kind == REVISION:
        subject_t, body_t = REVISION_SUBJECT, REVISION_BODY
    elif kind == HELD:
        subject_t, body_t = BALANCE_SUBJECT, BALANCE_BODY
        fill["balance"] = price_text(balance_owed(order))
        extra["hold_for_balance"] = True
    else:
        raise ValueError(f"no template for {kind!r}")
    return {"order_id": oid, "to": order.get("email"), "zip_name": zip_name,
            "zip_sha256": zip_sha256, "subject": subject_t.format(order_id=oid),
            "body_text": body_t.format(name=greeting_name(order.get("name")), order_id=oid,
                                       link=LINK, **fill), **extra}


def build_release(order: dict) -> dict:
    """The exact ``client.release`` payload for this order's held delivery. Pure."""
    oid = order.get("id")
    return {"order_id": oid, "to": order.get("email"),
            "delivery_id": (order.get("held_delivery") or {}).get("id"),
            "subject": RELEASE_SUBJECT.format(order_id=oid),
            "body_text": RELEASE_BODY.format(name=greeting_name(order.get("name")),
                                             order_id=oid, link=LINK)}


# ---- the turnaround ------------------------------------------------------------------------
def turnaround_due(order: dict) -> float | None:
    """When this order's work is due: its package's business days (Monday to Friday) after
    ``paid_at``, as epoch seconds. None for a custom order or one with no readable paid_at -
    no date is ever made up."""
    pkg = PACKAGES.get(package_of(order))
    paid = _epoch(order.get("paid_at"))
    if pkg is None or paid is None:
        return None
    day = _dt.datetime.fromtimestamp(paid, _dt.UTC)
    left = pkg.days
    while left:
        day += _dt.timedelta(days=1)
        if day.weekday() < 5:
            left -= 1
    return day.timestamp()


# ---- the folder ------------------------------------------------------------------------------
def newest_zip(folder: Path):
    """``(path, mtime, size)`` of the newest ``.zip`` directly in ``folder`` (a real file, not
    a link), or None. The newest is the owner's latest word on the order."""
    best = None
    try:
        entries = list(folder.iterdir()) if folder.is_dir() else []
    except OSError as exc:
        log.warning("delivery desk: cannot list %s: %s", folder, exc)
        return None
    for p in entries:
        try:
            if p.suffix.lower() != ".zip" or p.is_symlink() or not p.is_file():
                continue
            st = p.stat()
        except OSError:
            continue
        key = (st.st_mtime, p.name)
        if best is None or key > best[0]:
            best = (key, p, st.st_mtime, st.st_size)
    return None if best is None else best[1:]


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class DeliveryDesk(_Base):
    """``contracts.delivery``: each order's finished zip shipped by fixed template, for the
    owner's yes."""

    record_what = "the delivery desk's own record of every zip it submitted"
    # The orders are read exactly as the order desk reads them (and are unknown, never zero,
    # when they cannot be).
    _read_orders = OrderDesk._read_orders

    def record_path(self, state_dir):
        return record_path(state_dir, self.worker_id)

    def load(self, state_dir) -> dict:
        doc = read_record(state_dir, self.worker_id)
        blank = {"deliveries": []}
        return blank if doc is None else {**blank, **doc}

    def save(self, state_dir, rec: dict) -> None:
        save_record(self.record_path(state_dir), rec)

    # ---- one run -------------------------------------------------------------------------
    @never_raises()
    def run(self, ctx: WorkContext) -> Result:
        if ctx.state_dir is None:
            return self._err(ErrorKind.NOT_CONFIGURED, "no state dir: the desk cannot keep its "
                             "record, and without it could deliver twice", retryable=False)
        if ctx.job is None:
            return self._err(ErrorKind.NOT_CONFIGURED, "no hands: orders are read and work "
                             "delivered only through Pionir", retryable=False)
        if ctx.deliveries_dir is None:
            return self._err(ErrorKind.NOT_CONFIGURED, "no deliveries folder is set "
                             "(deliveries_dir): the desk cannot see the finished work",
                             retryable=False)
        try:
            rec = self.load(ctx.state_dir)
        except _Unreadable as exc:
            return self._err(ErrorKind.MALFORMED, f"its record is unreadable ({exc}); "
                             "refusing to deliver anything, since it could deliver twice",
                             retryable=False)
        got = self._read_orders(ctx)
        if isinstance(got, Err):
            return got          # nothing seen, nothing done: never "nothing to deliver"
        orders, malformed = got.value
        by_id = {o["id"]: o for o in orders}
        events: list = []
        self._follow_up(ctx, rec, by_id, events)
        looked = self._deliver_new(ctx, rec, orders, events)
        self._release_new(ctx, rec, orders, events)
        self.save(ctx.state_dir, rec)
        return Ok((*events, self._tally(ctx, rec, orders, looked, malformed=malformed)))

    # ---- the record ------------------------------------------------------------------------
    @staticmethod
    def _of(rec: dict, oid: str) -> list:
        return [e for e in rec["deliveries"] if e.get("order_id") == oid]

    def _sha_closed(self, rec: dict, oid: str, sha: str) -> bool:
        """True when this zip must never be submitted for this order (again)."""
        tries = [e for e in self._of(rec, oid) if e.get("zip_sha256") == sha]
        if any(e.get("status") not in RETRYABLE for e in tries):
            return True
        return any(sum(1 for e in tries if e.get("status") == status) >= cap
                   for status, cap in RETRYABLE.items())

    def _last_delivered(self, rec: dict, oid: str) -> dict | None:
        done = [e for e in self._of(rec, oid) if e.get("status") == "delivered"]
        return max(done, key=lambda e: float(e.get("zip_mtime") or 0)) if done else None

    # ---- 2. what became of the deliveries already waiting ------------------------------------
    def _follow_up(self, ctx: WorkContext, rec: dict, by_id: dict, events: list) -> None:
        for e in rec["deliveries"]:
            if e.get("status") != "pending_approval" or not e.get("approval_id"):
                continue
            if ctx.approval is None:
                return
            got = ctx.approval(e["approval_id"]) or {}
            state = got.get("status")
            e["checked_at"] = ctx.now
            if state in ("pending", "running", "unreachable"):
                continue
            order = by_id.get(e.get("order_id"))
            if state == "unknown":
                if self._shown_sent(rec, order, e):
                    self._settle(ctx, e, "delivered", "Pionir no longer lists the approval, "
                                 "and the order's messages show the email sent", events)
                elif ctx.now - float(e.get("submitted_at") or ctx.now) > FORGOTTEN_AFTER:
                    self._settle(ctx, e, "unknown", "Pionir no longer lists this approval; it "
                                 "was never seen delivered", events)
                continue
            if state == "denied":
                self._settle(ctx, e, "denied", f"the owner did not approve it "
                             f"({got.get('reason') or 'denied'}); left to him", events)
            elif state in ("approved", "approved_failed"):
                self._approved(ctx, rec, e, order, got.get("result"), events)
            else:
                log.warning("%s: approval %s has a status nobody knows (%r); still waiting",
                            self.worker_id, e["approval_id"], state)

    def _approved(self, ctx: WorkContext, rec: dict, e: dict, order, result,
                  events: list) -> None:
        out = outcome_of(e.get("capability") or DELIVER, result)
        if not out.ran:
            out.error = _inner_why(result) or out.error
        done = out.result if isinstance(out.result, dict) else {}
        if out.ran and done.get("emailed") is True and done.get("held") is True:
            # the balance request went; the files wait for the balance (a release, later)
            e["delivery_id"] = done.get("delivery_id")
            self._settle(ctx, e, "held", "approved by the owner: stored WITHOUT a link, and the "
                         "balance pay link emailed", events)
        elif out.ran and done.get("emailed") is True:
            for key in ("delivery_id", "expires_at"):
                if isinstance(done.get(key), (str, int, float)) and not isinstance(
                        done.get(key), bool):
                    e[key] = done[key]
            self._settle(ctx, e, "delivered", "approved by the owner, uploaded and emailed",
                         events)
        elif self._shown_sent(rec, order, e):
            self._settle(ctx, e, "delivered", "Pionir reported a failure, but the order's "
                         "messages show the email sent", events)
        elif not out.ran and _refused(result):
            self._settle(ctx, e, "failed", out.error or "refused when it was approved", events)
        else:
            why = out.error if not out.ran else "Pionir did not say the client was emailed"
            self._settle(ctx, e, "undelivered", (why or "it failed") + "; it will be offered "
                         "to the owner again", events)

    def _shown_sent(self, rec: dict, order, e: dict) -> bool:
        """Pionir's order lists this email's subject among the messages sent to the client
        more times than this desk has recorded it delivered - so this one went too."""
        msgs = order.get("messages") if isinstance(order, dict) else None
        subject = e.get("subject")
        if not isinstance(msgs, list) or not isinstance(subject, str):
            return False
        shown = sum(1 for m in msgs if isinstance(m, dict) and m.get("subject") == subject)
        known = sum(1 for x in self._of(rec, e.get("order_id")) if x is not e
                    and x.get("status") == "delivered" and x.get("subject") == subject)
        return shown > known

    # ---- 3. new deliveries -------------------------------------------------------------------
    def _deliver_new(self, ctx: WorkContext, rec: dict, orders: list, events: list) -> dict:
        """Submit what the oldest orders are ready for, at most ``MAX_DELIVERIES_PER_RUN``.
        Returns what was seen of each order, for the tally."""
        looked = {"no_zip": [], "ready_next_run": 0, "settling": 0, "not_set_up": None}
        submitted = 0
        root = Path(ctx.deliveries_dir)
        for order in sorted(orders, key=_oldest_first):
            oid, status = order["id"], order.get("status")
            if status not in ("in_progress", "delivered", "balance_due") \
                    or package_of(order) == FIND:
                continue        # a "Find it for me" order is the finder's (finder.py): no zip
            if status == "balance_due" and (balance_owed(order) is None or any(
                    e.get("kind") == HELD and e.get("status") == "held"
                    for e in self._of(rec, oid))):
                continue        # the balance request went: the client's move now
            last = self._last_delivered(rec, oid)
            if status == "delivered" and last is None:
                continue        # delivered some other way: not this desk's to revise
            if any(e.get("status") == "pending_approval" for e in self._of(rec, oid)):
                continue        # one waiting on the owner already
            if not _ORDER_ID.fullmatch(oid):
                if last is None:
                    looked["no_zip"].append(oid)
                continue        # never used as a folder name
            found = newest_zip(root / oid)
            if found is None:
                if last is None:
                    looked["no_zip"].append(oid)
                continue
            path, mtime, size = found
            if ctx.now - mtime < SETTLE_SECONDS:
                looked["settling"] += 1
                continue
            if last is not None and mtime <= float(last.get("zip_mtime") or 0):
                continue        # nothing newer than what was delivered
            try:
                sha = sha256_of(path)
            except OSError as exc:
                log.warning("%s: cannot read %s: %s", self.worker_id, path, exc)
                continue
            if self._sha_closed(rec, oid, sha):
                continue        # this exact zip was submitted for this order: never twice
            if submitted >= MAX_DELIVERIES_PER_RUN:
                looked["ready_next_run"] += 1
                continue
            owed = balance_owed(order) if status in ("in_progress", "balance_due") else None
            kind = HELD if owed is not None else FIRST if last is None else REVISION
            payload = build_delivery(kind, order, path.name, sha)
            entry = {"order_id": oid, "kind": kind, "zip_name": path.name, "zip_sha256": sha,
                     "zip_mtime": mtime, "zip_bytes": size, "to": payload["to"],
                     "subject": payload["subject"]}
            earlier = [e for e in self._of(rec, oid) if e.get("zip_sha256") == sha]
            if earlier and self._shown_sent(rec, order, entry):
                rec["deliveries"].append(entry)
                self._settle(ctx, entry, "delivered", "the order's messages already show this "
                             "delivery sent; not sent again", events)
                continue
            reasons = check_delivery(payload, order, owed)
            if reasons:
                self._template_blocked(ctx, rec, entry, reasons, events)
                continue
            if self._submit(ctx, rec, entry, payload, events, looked):
                submitted += 1
        return looked

    # ---- 4. held deliveries, once the balance is paid ----------------------------------------
    def _release_new(self, ctx: WorkContext, rec: dict, orders: list, events: list) -> None:
        """For each order whose balance the webhook marked paid: ask the owner to release the
        delivery Scrooge holds for it. Once per held delivery (retried like a delivery)."""
        for order in sorted(orders, key=_oldest_first):
            held = order.get("held_delivery")
            if order.get("status") != "balance_paid" or not isinstance(held, dict) \
                    or not isinstance(held.get("id"), str):
                continue
            oid, did = order["id"], held["id"]
            tries = [e for e in self._of(rec, oid) if e.get("kind") == RELEASE
                     and e.get("delivery_id") == did]
            if any(e.get("status") not in RETRYABLE for e in tries) or any(
                    sum(1 for e in tries if e.get("status") == st) >= cap
                    for st, cap in RETRYABLE.items()):
                continue
            payload = build_release(order)
            source = next((e for e in reversed(self._of(rec, oid)) if e.get("kind") == HELD
                           and e.get("status") == "held"), {})
            entry = {"order_id": oid, "kind": RELEASE, "delivery_id": did,
                     "zip_name": source.get("zip_name"), "zip_sha256": source.get("zip_sha256"),
                     "zip_mtime": source.get("zip_mtime"), "to": payload["to"],
                     "subject": payload["subject"]}
            reasons = check_release(payload, order)
            if reasons:
                self._template_blocked(ctx, rec, entry, reasons, events)
                continue
            self._submit(ctx, rec, entry, payload, events, {"not_set_up": None})

    def _template_blocked(self, ctx: WorkContext, rec: dict, entry: dict, reasons: list,
                          events: list) -> None:
        """Stopped by this desk's own template check: recorded once, never retried for this
        zip (the check and the template are deterministic), the order left to the owner."""
        entry.update(status="blocked", reasons=[_clip(r, 200) for r in reasons[:12]],
                     settled_at=ctx.now)
        rec["deliveries"].append(entry)
        log.warning("%s: the %s for %s was BLOCKED by the template check and NOT submitted: "
                    "%s", self.worker_id, entry["kind"], entry["order_id"], "; ".join(reasons))
        events.append(self._event(ctx, "delivery.template_blocked", {
            "order_id": entry["order_id"], "zip_name": _clip(entry["zip_name"], 80),
            "reasons": [_clip(r, 90) for r in reasons[:3]]}))

    def _submit(self, ctx: WorkContext, rec: dict, entry: dict, payload: dict, events: list,
                looked: dict) -> bool:
        """Submit one delivery (or release). False when nothing was attempted (the capability
        is not set up in Pionir: no zip was looked at, so none is held against the order)."""
        capability = RELEASE_CAP if entry.get("kind") == RELEASE else DELIVER
        if capability != DELIVER:
            entry["capability"] = capability
        out = ctx.job(Job(capability, dict(payload),
                          what=f"deliver order {entry['order_id']}'s work to its client"))
        why = out.error or f"Pionir said {out.status}"
        if out.status == "failed" and (_NOT_SET_UP.search(why)
                                       or why.strip() in (capability, "CapabilityNotFound")):
            looked["not_set_up"] = _clip(why, 160)
            log.error("%s: %s is not set up in Pionir (%s); nothing delivered", self.worker_id,
                      capability, why)
            events.append(self._event(ctx, "delivery.not_set_up", {
                "order_id": entry["order_id"], "why": _clip(why, 160)}))
            return False
        entry.update(submitted_at=ctx.now, status=out.status, task_id=out.task_id,
                     approval_id=out.approval_id)
        rec["deliveries"].append(entry)
        if out.status == "pending_approval":
            log.info("%s: the %s for %s is PENDING the owner's approval (approval %s)",
                     self.worker_id, entry["kind"], entry["order_id"], out.approval_id)
            events.append(self._event(ctx, "delivery.pending", {
                "order_id": entry["order_id"], "delivery": entry["kind"],
                "zip_name": _clip(entry["zip_name"], 80), "approval_id": out.approval_id}))
        elif out.status == "done":
            # ran without being parked: the owner did NOT approve it - Pionir's gate must.
            log.error("%s: %s ran WITHOUT the owner's approval for %s; it must be "
                      "approval-gated in Pionir", self.worker_id, DELIVER, entry["order_id"])
            entry["approved_by_owner"] = False
            done = out.result if isinstance(out.result, dict) else {}
            if done.get("emailed") is True:
                self._settle(ctx, entry, "delivered", "Pionir delivered it without parking it "
                             "for the owner", events)
            else:
                self._settle(ctx, entry, "unknown", "Pionir ran it without parking it and did "
                             "not say the client was emailed", events)
        elif out.status == "failed" and out.error_type == "AdapterProtocolError":
            # Pionir's own typed refusal (its checks said no to this zip): final for this sha
            self._record_refused(ctx, entry, why, events)
        elif out.status == "failed" and (out.error_type in PASSING_TYPES
                                         or (not out.error_type and _PASSING.search(why))):
            # a typed passing problem - or, only when Pionir gave no type, the wording
            self._settle(ctx, entry, "unreachable", why, events)
        elif out.status == "failed":
            self._record_refused(ctx, entry, why, events)
        elif out.status == "unreachable":
            self._settle(ctx, entry, "unreachable", why, events)
        else:       # still running: not delivered, never assumed to be, never sent again
            self._settle(ctx, entry, "unknown", why, events)
        return True

    def _record_refused(self, ctx: WorkContext, entry: dict, why: str, events: list) -> None:
        """Pionir's own checks refused this zip before parking it (a secret in it, no README,
        an executable...). Recorded with the reason and never retried: only a different zip
        is a new attempt. Urgent for the owner - the client is waiting on it."""
        entry.update(status="refused", why=_clip(why, 300), settled_at=ctx.now)
        log.error("%s: Pionir's checks REFUSED the zip %s for %s: %s", self.worker_id,
                  entry["zip_name"], entry["order_id"], why)
        events.append(self._event(ctx, "delivery.blocked", {
            "order_id": entry["order_id"], "zip_name": _clip(entry["zip_name"], 80),
            "why": _clip(why, 200)}))

    def _settle(self, ctx: WorkContext, e: dict, status: str, why: str, events: list) -> None:
        e.update(status=status, why=_clip(why, 300), settled_at=ctx.now)
        if status == "delivered":
            log.info("%s: the %s for %s was DELIVERED", self.worker_id, e["kind"],
                     e["order_id"])
            events.append(self._event(ctx, "delivery.delivered", {
                "order_id": e["order_id"], "delivery": e["kind"],
                "zip_name": _clip(e.get("zip_name"), 80)}))
            return
        log.warning("%s: the %s for %s is %s: %s", self.worker_id, e.get("kind"),
                    e.get("order_id"), status, why)
        events.append(self._event(ctx, "delivery.not_delivered", {
            "order_id": e["order_id"], "delivery": e["kind"], "status": status,
            "why": _clip(why, 160)}))

    # ---- what the leader reads --------------------------------------------------------------
    def _event(self, ctx: WorkContext, kind: str, payload: dict, figures=()):
        # no model wrote any of it; it never carries a client's name, address, brief or the
        # private download link.
        return make_output(self, kind=kind, valid_at=ctx.now, observed_at=ctx.now,
                           payload=payload, figures=figures, entities=self.entities,
                           provenance={"source": "real", "provider": self.provider,
                                       "record": self.record_what})

    def _tally(self, ctx: WorkContext, rec: dict, orders: list, looked: dict, *,
               malformed: int):
        deliveries = rec["deliveries"]

        def n(*statuses, kind=None) -> int:
            return sum(1 for e in deliveries if e.get("status") in statuses
                       and (kind is None or e.get("kind") == kind))

        in_progress = [o for o in orders if o.get("status") == "in_progress"
                       and package_of(o) != FIND]
        delivered_ids = {e["order_id"] for e in deliveries if e.get("status") == "delivered"}
        late = []
        for o in in_progress:
            due = turnaround_due(o)
            if o["id"] not in delivered_ids and due is not None and ctx.now > due:
                late.append(o["id"])
        # blocked NOW: the order's latest attempt is a refused (or template-blocked) zip
        blocked = []
        listed = {o["id"] for o in orders if o.get("status") in ("in_progress", "delivered")}
        for oid in sorted({e.get("order_id") for e in deliveries} & listed):
            latest = max(self._of(rec, oid), key=lambda e: float(e.get("submitted_at")
                                                                 or e.get("settled_at") or 0))
            if latest.get("status") in ("refused", "blocked"):
                blocked.append({"order_id": oid, "zip_name": _clip(latest.get("zip_name"), 80),
                                "by": "Pionir's checks" if latest["status"] == "refused"
                                else "the template check",
                                "why": latest.get("why") or "; ".join(latest.get("reasons")
                                                                      or [])[:300]})
        pending = [{"order_id": e["order_id"], "delivery": e["kind"]} for e in deliveries
                   if e.get("status") == "pending_approval"]
        gave_up = 0         # zips that never reached Pionir in RETRY_UNREACHABLE tries
        for oid, sha in {(e.get("order_id"), e.get("zip_sha256")) for e in deliveries}:
            tries = [e for e in self._of(rec, oid) if e.get("zip_sha256") == sha]
            if len(tries) >= RETRY_UNREACHABLE and all(e.get("status") == "unreachable"
                                                       for e in tries):
                gave_up += 1
        figures = [
            Figure(len(in_progress), "count", "orders in progress", window="now"),
            Figure(len(looked["no_zip"]), "count",
                   "orders in progress with no delivery yet (waiting for a zip)", window="now"),
            Figure(len(late), "count", "orders past their turnaround", window="now"),
            Figure(len(pending), "count", "deliveries pending the owner's approval",
                   window="now"),
            Figure(n("delivered"), "count", "deliveries delivered", window="all_time"),
            Figure(n("held"), "count", "deliveries held for the balance (balance requested)",
                   window="all_time"),
            Figure(n("delivered", kind=RELEASE), "count",
                   "held deliveries released once the balance was paid", window="all_time"),
            Figure(n("delivered", kind=REVISION), "count", "revised deliveries delivered",
                   window="all_time"),
            Figure(len(blocked), "count", "deliveries blocked by the checks", window="now"),
            Figure(n("refused"), "count", "zips refused by Pionir's checks", window="all_time"),
            Figure(n("blocked"), "count", "delivery emails blocked by the template check",
                   window="all_time"),
            Figure(n("denied"), "count", "deliveries denied", window="all_time"),
            Figure(n("failed", "unknown") + gave_up, "count", "deliveries failed",
                   window="all_time"),
            Figure(n("undelivered"), "count",
                   "approved deliveries that failed to send (offered to the owner again)",
                   window="all_time"),
        ]
        return make_output(self, kind="delivery.tally", valid_at=ctx.now, observed_at=ctx.now,
                           payload={"pending_approval": pending[-5:], "blocked": blocked[:5],
                                    "past_turnaround": late[:10],
                                    "waiting_for_zip": looked["no_zip"][:10],
                                    "zips_still_copying": looked["settling"],
                                    "deliveries_ready_next_run": looked["ready_next_run"],
                                    "deliver_not_set_up": looked["not_set_up"],
                                    "malformed_orders": malformed},
                           figures=figures, entities=self.entities,
                           provenance={"source": "real", "provider": self.provider,
                                       "record": self.record_what})


def _refused(result) -> bool:
    """Whether an approved delivery that failed was a refusal (final for this zip) rather than
    a passing outage (offered again): a ``refused`` reason anywhere in Pionir's result, or the
    adapter's own refusal (``AdapterProtocolError`` - e.g. the zip changed since it was
    parked)."""
    if isinstance(result, dict):
        if result.get("refused"):
            return True
        err = result.get("error")
        if isinstance(err, dict) and err.get("type") in ("AdapterProtocolError",
                                                          "PermissionDenied"):
            return True
        return any(_refused(v) for v in result.values() if isinstance(v, dict))
    return False


def _inner_why(result) -> str:
    """The adapter's own words for a failed delivery (``refused`` / ``unavailable`` /
    ``error``), which Pionir's outer answer does not repeat."""
    inner = result.get("result") if isinstance(result, dict) else None
    if isinstance(inner, dict):
        for key in ("refused", "unavailable", "error"):
            if isinstance(inner.get(key), str) and inner[key].strip():
                return inner[key].strip()
    return ""
