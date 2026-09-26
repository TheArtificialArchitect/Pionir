"""Quote to paid: the owner prices a custom order by replying on Discord.

Dokaz's "Hire us" custom orders have no fixed price. The order desk (crew/orders.py) asks
Pionir to post ONE quote card per quote request in the approvals channel
(``quotes.card``, adapters/quote_cards.py). The owner answers by REPLYING to that card with
the price and, optionally, the delivery time::

    $350 7d          350          $1,200 10 days          $1,200.50 3 business days

Only a reply from the configured owner's Discord user id is read; anybody else's is
ignored. The Discord gate (discord_gate.py) reads the channel through REST - no gateway
socket - and hands each owner reply to ``QuoteReplies``, which:

1. claims the reply in the card record (``QuoteCardStore``) BEFORE acting on it, so a
   restart never acts on it twice;
2. reads the price (``parse_reply``) - an unreadable reply gets a plain "couldn't read
   that" answer in the channel and nothing else happens;
3. reads the order fresh from Scrooge (name, address, status): only an order still
   ``quote_requested`` or ``quoted`` is quoted;
4. builds the quote email from the fixed template below (``build_quote``) and submits
   ``client.quote`` to Pionir, which PARKS it: the gate then posts the approval card with
   the exact email, the price, the deposit split and the pay link's validity. Nothing is
   emailed and no pay link exists until the owner reacts ✅ on that card;
5. a newer reply for the same order replaces an older one still waiting: the older
   approval is denied, so only one quote card per order is ever live.

**No model, no words, no price but his.** The price and the days are exactly what the
owner typed; nothing here suggests or adjusts one. When the reply gives no days, the
default (``PIONIR_QUOTE_DEFAULT_DAYS``, 7) is used and the card says so plainly. The
deposit split is a fixed rule: at or above ``PIONIR_QUOTE_DEPOSIT_THRESHOLD_CENTS``
(default 50000 = $500) half up front and half on delivery, below it everything up front.
Scrooge applies the same rule (``QUOTE_DEPOSIT_THRESHOLD_CENTS``) and refuses, unsent, a
quote whose split disagrees.

**One reply, one quote.** The reply's Discord id becomes the quote's ``quote_ref``:
before submitting, the approvals queue is searched for it (a crash between claiming and
recording finds its card instead of posting a second one), and Scrooge refuses a second
quote with the same ref.

Reading reply text needs Discord's privileged **Message Content** intent for the bot (a
REST read without it returns an empty ``content`` for messages that do not mention the
bot). A reply the bot cannot read is answered once with where to switch it on.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import atomic

_log = logging.getLogger(__name__)

QUOTE = "client.quote"
CARD = "quotes.card"
PAY_LINK = "{pay_link}"
LINK_DAYS = 14
REMIND_AFTER_DAYS = 10
DEFAULT_DEPOSIT_THRESHOLD_CENTS = 50_000
DEFAULT_DAYS = 7
MIN_CENTS = 1_000
MAX_CENTS = 5_000_000
MAX_DAYS = 180
QUOTABLE = ("quote_requested", "quoted")
# An order in one of these can never be quoted again: its card is closed.
CLOSED = ("paid", "in_progress", "delivered", "declined", "refunded", "balance_due",
          "balance_paid")
CARD_RETENTION = timedelta(days=60)
ORDER_TRIES = 5              # a reply whose order cannot be read is retried this many times
MAX_PAGES = 5                # at most this many pages of 100 channel messages per poll
STORE_NAME = "quote-cards.json"
INTENT_HINT = ("I can't read the text of your reply: the bot's **Message Content** intent is "
               "off. Turn it on in the Discord Developer Portal -> your application -> Bot -> "
               "Privileged Gateway Intents -> **Message Content Intent** -> Save, then reply "
               "to the quote card again.")
FORMAT_HINT = "Reply with the price and, if you like, the business days: `$350 7d`, `350` or `$1,200 10 days`."

_ORDER_ID = re.compile(r"[0-9a-f]{12}")
_SNOWFLAKE = re.compile(r"\d{5,25}")
_NAME = re.compile(r"[^\W\d_]+(?:[ '’-][^\W\d_]+){0,5}")


# ---- settings -------------------------------------------------------------------------------
def _int_env(environ: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    raw = (environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        _log.error("quotes: %s=%r is not a whole number; using %s", name, raw, default)
        return default
    if not low <= value <= high:
        _log.error("quotes: %s=%s is outside %s-%s; using %s", name, value, low, high, default)
        return default
    return value


@dataclass(frozen=True)
class QuoteSettings:
    """The deposit rule and the days used when a reply gives none. Never a price."""

    deposit_threshold_cents: int = DEFAULT_DEPOSIT_THRESHOLD_CENTS
    default_days: int = DEFAULT_DAYS

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> QuoteSettings:
        env = os.environ if environ is None else environ
        return cls(
            deposit_threshold_cents=_int_env(env, "PIONIR_QUOTE_DEPOSIT_THRESHOLD_CENTS",
                                             DEFAULT_DEPOSIT_THRESHOLD_CENTS, 1, 10**9),
            default_days=_int_env(env, "PIONIR_QUOTE_DEFAULT_DAYS", DEFAULT_DAYS, 1, MAX_DAYS),
        )


# ---- the reply ------------------------------------------------------------------------------
_REPLY = re.compile(
    r"\$?\s*(?P<amount>\d{1,3}(?:,\d{3})+|\d+)(?:\.(?P<cents>\d{2}))?(?:\s*(?:usd|dollars?))?"
    # the days only after whitespace: "15,100d" is not "$15 over 100 days", it is unreadable
    r"(?:\s+(?P<days>\d{1,3})\s*(?:d|days?|business\s+days?)\.?)?",
    re.IGNORECASE)


@dataclass(frozen=True)
class Price:
    total_cents: int
    days: int | None      # None: the reply gave no days


def usd(cents: int) -> str:
    """$350, $1,200, $1,200.50 - exactly as Scrooge checks the quote email for it."""
    return f"${cents // 100:,}" if cents % 100 == 0 else f"${cents // 100:,}.{cents % 100:02d}"


def parse_reply(text: Any) -> Price:
    """The owner's price and days, or ValueError saying what could not be read. The WHOLE
    reply must be the price (and days): anything else is not guessed at."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("the reply is empty")
    m = _REPLY.fullmatch(text.strip())
    if m is None:
        raise ValueError("that is not a price")
    cents = int(m["amount"].replace(",", "")) * 100 + int(m["cents"] or 0)
    if not MIN_CENTS <= cents <= MAX_CENTS:
        raise ValueError(f"the price must be from {usd(MIN_CENTS)} to {usd(MAX_CENTS)}")
    days = int(m["days"]) if m["days"] else None
    if days is not None and not 1 <= days <= MAX_DAYS:
        raise ValueError(f"the delivery time must be 1 to {MAX_DAYS} business days")
    return Price(cents, days)


def deposit_for(total_cents: int, threshold_cents: int) -> int:
    """Half (rounded down: the balance carries the odd cent) at or above the threshold."""
    return total_cents // 2 if total_cents >= threshold_cents else 0


# ---- the template: every word of the quote email --------------------------------------------
QUOTE_SUBJECT = "Your Dokaz quote for request {order_id}"
QUOTE_BODY = """Hello {name},

Thank you for your patience. We've read your brief, and here is our quote for request \
{order_id}.

Price: {price} (USD)
Delivery: within {days_text} of your payment
{payment}

To accept, pay securely here:
{pay_link}

This payment link is valid for 14 days. After that it expires; just reply to this email \
and we'll send you a new quote.

If you have a question about the quote, reply to this email.

Thank you,
Dokaz"""
PAY_FULL = "Payment: in full, up front, by card through Stripe."
PAY_DEPOSIT = """Payment: in two parts, by card through Stripe:
- {deposit} now, a 50% deposit, before work starts
- {balance} on delivery; your files are released once it is paid"""


def days_text(days: int) -> str:
    return f"{days} business day{'' if days == 1 else 's'}"


def greeting_name(name: Any) -> str:
    """The name the client gave, when it is a plain name; otherwise ``there``."""
    s = " ".join(name.split()) if isinstance(name, str) else ""
    return s if s and len(s) <= 60 and _NAME.fullmatch(s) else "there"


def build_quote(order: Mapping[str, Any], price: Price, settings: QuoteSettings,
                reply_id: str) -> dict[str, Any]:
    """The exact ``client.quote`` payload for this order and the owner's price. Pure. What
    the owner wrote is NOT in it: the card reads his reply from the card record."""
    days = price.days if price.days is not None else settings.default_days
    total = price.total_cents
    deposit = deposit_for(total, settings.deposit_threshold_cents)
    payment = (PAY_DEPOSIT.format(deposit=usd(deposit), balance=usd(total - deposit))
               if deposit else PAY_FULL)
    oid = str(order.get("id"))
    return {
        "order_id": oid,
        "to": order.get("email"),
        "quote_ref": f"discord-{reply_id}",
        "total_cents": total,
        "deposit_cents": deposit,
        "days": days,
        "subject": QUOTE_SUBJECT.format(order_id=oid),
        "body_text": QUOTE_BODY.format(name=greeting_name(order.get("name")), order_id=oid,
                                       price=usd(total), days_text=days_text(days),
                                       payment=payment, pay_link=PAY_LINK),
    }


_REF = re.compile(r"discord-(\d{5,25})")


def owner_reply(store: QuoteCardStore | None, order_id: Any,
                quote_ref: Any) -> Mapping[str, Any] | None:
    """The owner's reply a quote was made from, as the Discord gate recorded it, or None."""
    m = _REF.fullmatch(quote_ref) if isinstance(quote_ref, str) else None
    if store is None or m is None or not isinstance(order_id, str):
        return None
    card = store.read()["cards"].get(order_id)
    reply = ((card or {}).get("replies") or {}).get(m.group(1))
    return reply if isinstance(reply, dict) else None


def check_provenance(payload: Mapping[str, Any], store: QuoteCardStore | None,
                     owner: str | None, default_days: int) -> None:
    """ValueError unless this quote is exactly what the OWNER replied on Discord: its quote_ref
    names a reply the gate recorded from the configured owner for this order, still standing
    (not replaced by a newer reply), and its price and days are what that reply says. A quote
    made up anywhere else - by a model, a script, a hand - is refused before it is parked."""
    if store is None or not owner:
        raise ValueError("quote_ref: a quote comes only from the owner's reply on Discord, "
                         "and no reply record or owner is configured here")
    reply = owner_reply(store, payload.get("order_id"), payload.get("quote_ref"))
    if reply is None:
        raise ValueError("quote_ref: no reply of the owner's is recorded for this order - a "
                         "quote comes only from his reply to the quote card")
    if str(reply.get("by")) != str(owner):
        raise ValueError("quote_ref: that reply is not the owner's")
    if reply.get("state") not in ("claimed", "submitted"):
        raise ValueError(f"quote_ref: that reply is {reply.get('state')}, not a live quote")
    try:
        price = parse_reply(reply.get("text"))
    except ValueError as error:
        raise ValueError(f"quote_ref: the owner's reply is not a price ({error})") from None
    days = price.days if price.days is not None else default_days
    if payload.get("total_cents") != price.total_cents or payload.get("days") != days:
        raise ValueError(f"total_cents/days: the owner replied {usd(price.total_cents)}, "
                         f"{days_text(days)}; this quote says otherwise")


# ---- the card record --------------------------------------------------------------------------
_STORE_LOCKS: dict[str, threading.RLock] = {}
_STORE_LOCKS_GUARD = threading.Lock()


def _now() -> datetime:
    return datetime.now(UTC)


class QuoteCardStore:
    """``<state_root>/discord/quote-cards.json``: every quote card (order id -> its Discord
    message ids, whether it is open, and every owner reply to it with what became of it),
    and how far the channel has been read. Shared by the ``quotes.card`` adapter (which
    posts the cards) and the Discord gate (which reads the replies), in one process: one
    lock per file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        with _STORE_LOCKS_GUARD:
            self._lock = _STORE_LOCKS.setdefault(str(path.resolve()), threading.RLock())

    @classmethod
    def for_state_root(cls, state_root: Path) -> QuoteCardStore:
        return cls(Path(state_root) / "discord" / STORE_NAME)

    def _load(self) -> dict[str, Any]:
        blank: dict[str, Any] = {"cards": {}, "cursor": None}
        if not self.path.exists():
            return blank
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(doc, dict) or not isinstance(doc.get("cards"), dict):
                raise ValueError("no 'cards' map")  # noqa: TRY004 - one unreadable path
        except (OSError, ValueError) as error:
            # Kept for a look. Losing it cannot double a quote: a reply's approval is
            # found by its quote_ref before anything is submitted, and Scrooge refuses a
            # second quote for one reply.
            keep = self.path.with_name(f"{self.path.name}.corrupt-{int(time.time())}")
            try:
                atomic.replace(self.path, keep)
            except OSError:
                keep = self.path
            _log.error("quote cards: record unreadable (%s), kept at %s", error, keep)
            return blank
        doc.setdefault("cursor", None)
        return doc

    def _save(self, doc: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        atomic.replace(tmp, self.path)

    def read(self) -> dict[str, Any]:
        with self._lock:
            return self._load()

    def update(self, change: Callable[[dict[str, Any]], Any]) -> Any:
        """Load, change, save - under the lock. Returns what ``change`` returns."""
        with self._lock:
            doc = self._load()
            out = change(doc)
            self._save(doc)
            return out


# ---- the replies ------------------------------------------------------------------------------
Call = Callable[..., Any]


class QuoteReplies:
    """Turns the owner's replies to quote cards into parked ``client.quote`` approvals.
    Driven by the Discord gate's poll (``tick``); every Discord call goes through the
    gate's own client, so a rejected token or an outage is handled there."""

    def __init__(
        self,
        store: QuoteCardStore,
        *,
        submit: Callable[..., Mapping[str, Any]],
        deny: Callable[[str], Mapping[str, Any]],
        find_approvals: Callable[[str, str, str], list[Mapping[str, Any]]],
        get_order: Callable[[str], Mapping[str, Any] | None],
        settings: QuoteSettings | None = None,
        every: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self._submit = submit              # PionirApp.run_task: parks client.quote
        self._deny = deny                  # PionirApp.deny: an older reply's card
        self._find = find_approvals        # ApprovalQueue.find(capability, key, value)
        self._get_order = get_order        # ClientAdapter.order: the order, fresh
        self.settings = settings or QuoteSettings.from_environment()
        self.every = every
        self._clock = clock
        self._last = -float("inf")

    # ---- one poll ----------------------------------------------------------------------
    def tick(self, call: Call, owner: str | None, channel: str) -> None:
        """Read the channel since the cursor, act on every owner reply to an open card,
        and retry any reply claimed but not yet answered. Without an owner nothing is read:
        no reply can count."""
        if owner is None:
            return
        doc = self.store.read()
        cards = {oid: c for oid, c in doc["cards"].items() if c.get("open")}
        if not cards:
            return
        now = self._clock()
        if now - self._last < self.every:
            return
        self._last = now
        ids = [int(m) for c in cards.values() for m in c.get("message_ids") or []
               if _SNOWFLAKE.fullmatch(str(m))]
        if not ids:
            return
        cursor = doc.get("cursor")
        if not (isinstance(cursor, str) and _SNOWFLAKE.fullmatch(cursor)):
            cursor = str(min(ids))
        for _page in range(MAX_PAGES):
            got = call("GET", f"/channels/{channel}/messages?after={cursor}&limit=100")
            messages = sorted((m for m in got if isinstance(m, dict)
                               and _SNOWFLAKE.fullmatch(str(m.get("id")))),
                              key=lambda m: int(m["id"])) if isinstance(got, list) else []
            for message in messages:
                self._consider(call, owner, channel, message)
            if messages:
                # Re-reading a page after a crash is harmless: each reply is claimed by id.
                cursor = str(messages[-1]["id"])
                self.store.update(lambda d, c=cursor: d.__setitem__("cursor", c))
            if len(messages) < 100:
                break
        self._retry(call, channel)
        self._prune()

    def _consider(self, call: Call, owner: str, channel: str, message: dict[str, Any]) -> None:
        # (the reply record carries who wrote it: client.quote checks it is the owner)
        ref = (message.get("message_reference") or {}).get("message_id")
        if ref is None:
            return
        doc = self.store.read()
        order_id = next((oid for oid, c in doc["cards"].items()
                         if str(ref) in [str(m) for m in c.get("message_ids") or []]), None)
        if order_id is None:
            return
        author = str((message.get("author") or {}).get("id"))
        reply_id = str(message["id"])
        if author != owner:
            # only the owner prices work; anyone else's reply is not read at all
            _log.info("quotes: a reply to the card of %s from someone else was ignored",
                      order_id)
            return
        card = doc["cards"][order_id]
        if reply_id in (card.get("replies") or {}):
            return                                   # handled before: never twice
        if not card.get("open"):
            return
        empty = not str(message.get("content") or "").strip() and not message.get("attachments")

        def claim(d: dict[str, Any]) -> None:
            d["cards"][order_id].setdefault("replies", {})[reply_id] = {
                "state": "claimed", "at": _now().isoformat(), "by": author,
                "text": str(message.get("content") or "")[:200], "tries": 0,
                "no_content": empty}

        # Claimed BEFORE anything is acted on: a crash from here on never acts twice.
        self.store.update(claim)
        self._handle(call, channel, order_id, reply_id)

    def _set(self, order_id: str, reply_id: str, **fields: Any) -> None:
        def change(d: dict[str, Any]) -> None:
            d["cards"][order_id]["replies"][reply_id].update(fields)
        self.store.update(change)

    def _answer(self, call: Call, channel: str, reply_id: str, text: str) -> None:
        """A short answer under the owner's reply. It pings nobody."""
        call("POST", f"/channels/{channel}/messages", {
            "content": text[:1900],
            "allowed_mentions": {"parse": [], "replied_user": False},
            "message_reference": {"message_id": reply_id, "fail_if_not_exists": False}})

    def _handle(self, call: Call, channel: str, order_id: str, reply_id: str) -> None:
        reply = self.store.read()["cards"][order_id]["replies"][reply_id]
        ref = f"discord-{reply_id}"
        # Already parked (a restart between the claim and recording it): record, never re-post.
        found = [a for a in self._find(QUOTE, "quote_ref", ref)]
        if found:
            self._set(order_id, reply_id, state="submitted", approval_id=found[0].get("id"))
            return
        if reply.get("no_content"):
            self._set(order_id, reply_id, state="unreadable")
            self._answer(call, channel, reply_id, INTENT_HINT)
            return
        try:
            price = parse_reply(reply.get("text"))
        except ValueError as error:
            self._set(order_id, reply_id, state="unparsed", why=str(error))
            self._answer(call, channel, reply_id,
                         f"❔ I couldn't read that as a price ({error}). {FORMAT_HINT} "
                         "Nothing was sent.")
            return
        try:
            order = self._get_order(order_id)
        except Exception as error:  # noqa: BLE001 - retried; the owner is told if it persists
            tries = int(reply.get("tries") or 0) + 1
            self._set(order_id, reply_id, tries=tries, why=f"{type(error).__name__}: {error}"[:300])
            if tries >= ORDER_TRIES:
                self._set(order_id, reply_id, state="failed")
                self._answer(call, channel, reply_id,
                             f"⚠️ I couldn't read order `{order_id}` from Scrooge "
                             f"({str(error)[:200]}). Nothing was sent; reply again later.")
            return
        status = (order or {}).get("status")
        if order is None or status not in QUOTABLE:
            shown = "not found" if order is None else f"now `{status}`"
            self._set(order_id, reply_id, state="refused", why=f"order {shown}")
            if order is None or status in CLOSED:
                self._close(order_id)
            self._answer(call, channel, reply_id,
                         f"⛔ Order `{order_id}` is {shown}, so it can't be quoted. "
                         "Nothing was sent.")
            return
        payload = build_quote(order, price, self.settings, reply_id)
        out = self._submit(QUOTE, payload)
        approval_id = out.get("approval_id") if isinstance(out, Mapping) else None
        if not approval_id or out.get("status") != "pending_approval":
            why = _why(out)
            self._set(order_id, reply_id, state="refused", why=why[:300])
            self._answer(call, channel, reply_id,
                         f"⛔ Pionir would not take that quote: {why[:400]}. Nothing was sent.")
            return
        self._set(order_id, reply_id, state="submitted", approval_id=approval_id)
        # One live quote card per order: an older reply still waiting is withdrawn.
        superseded = self._supersede(order_id, reply_id)
        split = (f"{usd(payload['deposit_cents'])} now + "
                 f"{usd(payload['total_cents'] - payload['deposit_cents'])} on delivery"
                 if payload["deposit_cents"] else "in full up front")
        days = days_text(payload["days"]) + (" (the default - your reply gave no days)"
                                             if price.days is None else "")
        note = (f" Your earlier reply's card (`{'`, `'.join(superseded)}`) was withdrawn."
                if superseded else "")
        self._answer(call, channel, reply_id,
                     f"✍️ Read: **{usd(payload['total_cents'])}**, {split}, "
                     f"delivery {days}. The quote email is in approval `{approval_id}` - "
                     f"nothing is sent until you ✅ it.{note}")

    def _supersede(self, order_id: str, keep: str) -> list[str]:
        withdrawn: list[str] = []
        replies = self.store.read()["cards"][order_id].get("replies") or {}
        for rid, r in replies.items():
            if rid == keep or r.get("state") != "submitted" or not r.get("approval_id"):
                continue
            answer = self._deny(str(r["approval_id"]))
            if isinstance(answer, Mapping) and answer.get("ok"):
                withdrawn.append(str(r["approval_id"]))
            self._set(order_id, rid, state="superseded", superseded_by=keep)
        return withdrawn

    def _retry(self, call: Call, channel: str) -> None:
        """Replies claimed but not yet answered (Scrooge was down, or a restart): again."""
        doc = self.store.read()
        for order_id, card in doc["cards"].items():
            for reply_id, r in (card.get("replies") or {}).items():
                if r.get("state") == "claimed" and card.get("open"):
                    self._handle(call, channel, order_id, reply_id)

    def _close(self, order_id: str) -> None:
        def change(d: dict[str, Any]) -> None:
            d["cards"][order_id]["open"] = False
            d["cards"][order_id]["closed_at"] = _now().isoformat()
        self.store.update(change)

    def _prune(self) -> None:
        """Cards older than CARD_RETENTION are forgotten (their replies are no longer read)."""
        cutoff = _now() - CARD_RETENTION

        def change(d: dict[str, Any]) -> None:
            for oid in [o for o, c in d["cards"].items() if _older(c.get("posted_at"), cutoff)]:
                del d["cards"][oid]

        if any(_older(c.get("posted_at"), cutoff) for c in self.store.read()["cards"].values()):
            self.store.update(change)


def _older(stamp: Any, cutoff: datetime) -> bool:
    try:
        at = datetime.fromisoformat(str(stamp))
    except ValueError:
        return False
    return (at if at.tzinfo else at.replace(tzinfo=UTC)) < cutoff


def _why(out: Any) -> str:
    if isinstance(out, Mapping):
        error = out.get("error")
        if isinstance(error, Mapping):
            return str(error.get("message") or error.get("type") or "refused")
        if error:
            return str(error)
        return f"Pionir said {out.get('status')}"
    return "no answer"


def is_order_id(value: Any) -> bool:
    return isinstance(value, str) and _ORDER_ID.fullmatch(value) is not None
