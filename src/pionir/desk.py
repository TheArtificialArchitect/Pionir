"""The Approval Desk: everything waiting on the owner, in one list, in one order.

Three things used to wait on Ian in three places: parked approvals (pionir/approvals.py,
answered with a reaction or the phone), and the reply cards the quote, Fiverr and Builds
desks post to his Discord channel (answered by REPLYING). The desk merges them, read-only,
into one list the dashboard serves at ``GET /api/desk``. Every item is in one LANE:

- ``decide`` - a parked approval: yes or no, by its own approve path;
- ``reply``  - a card that is waiting for his words (a quote's price, a Fiverr gig's
  ``redraft``, a backlog change) and has none yet;
- ``fyi``    - news: a card that takes no reply (shelved, staged, a Fiverr note) or the
  Builds nightly report. A reply typed to a nightly report in Discord is still recorded,
  but an FYI item never counts as open.

**Money keeps a human gate per item.** ``bulk_ok`` - may this be approved as one of a group
- is true only for an approval with no money in it (the Discord card's own reading,
pionir/money.py) that touches no client. A reply card is never bulk.

**A card nobody answers is parked, not kept open forever.** A reply card with no answer
after ``PIONIR_CARD_STALE_DAYS`` (7) is parked as ``parked_no_answer`` - off the open
count, never deaf: a late reply still counts and un-parks it; once a week the
cards parked since the last time are listed in ONE "still waiting on you" note through
``owner.notify`` (never an approval card). Both ride the Discord gate's poll
(``Desk.tick``); nothing here schedules anything.

The card records stay owned by their producers (quotes.py, fiverr.py, adapters/builds.py):
the desk only adds ``status``/``closed_at``/``archived_at`` marks to them (and never
closes one: a parked card still takes a late reply), and a reply
typed on the desk goes through the very recorder a Discord reply goes through
(PionirApp.desk_reply), so the two can never parse a reply differently.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import atomic
from .money import find_money

_log = logging.getLogger(__name__)

STREAMS = ("etsy", "instagram", "blog", "devto", "newsletter", "client", "gumroad",
           "apibuild", "improve", "marketplace", "other")
# capability prefix -> stream; the longest matching prefix wins
_STREAM_PREFIXES = (
    ("content.crosspost_devto", "devto"),
    ("content.newsletter", "newsletter"),
    ("social.instagram", "instagram"),
    ("product.gumroad", "gumroad"),
    ("content.", "blog"),
    ("client.", "client"),
    ("etsy.", "etsy"),
    ("printify.", "etsy"),          # Printify designs are the Etsy print-on-demand stream
    ("apibuild.", "apibuild"),
    ("improve.", "improve"),
    ("apify.", "marketplace"),
    ("chrome.", "marketplace"),
)
# How near an item is to money in hand, lower first: a client's order, then something
# that sells, then something that publishes, then the rest.
CLIENT_MONEY, SELLABLE, PUBLISHING, OTHER = 0, 1, 2, 3
_SELLABLE = frozenset({"etsy", "gumroad", "marketplace"})
_PUBLISHING = frozenset({"instagram", "blog", "devto", "newsletter"})
_PUBLISHING_CAPS = frozenset({"client.testimonial_publish", "video.youtube_upload"})

LANES = ("decide", "reply", "fyi")
SOURCES = ("approval", "quote", "fiverr", "build")
FYI_DAYS = 7
DEFAULT_STALE_DAYS = 7
STALE_ENV = "PIONIR_CARD_STALE_DAYS"
SUMMARY_EVERY = timedelta(days=7)
SWEEP_SECONDS = 600.0
PARKED = "parked_no_answer"
# a reply that is an answer: recorded for the crew, or taken up as a quote
ANSWERED = frozenset({"recorded", "claimed", "submitted"})
PREVIEW_MAX = 4000
_IMAGE_URL = re.compile(r"https?://\S+\.(?:png|jpe?g|webp|gif)(?:\?\S*)?", re.IGNORECASE)
DESK_STATE = "desk.json"

# Which card kinds wait for a reply, per source; every other kind is news (fyi).
_REPLY_KINDS = {"quote": None,                         # every quote card takes a price
                "fiverr": frozenset({"order", "gig", "idea"}),
                "build": frozenset({"backlog"})}       # the nightly report is FYI now
_REPLY_HINTS = {
    "quote": "Reply with the price and, if you like, the business days: $350 7d, 350 or "
             "$1,200 10 days.",
    "fiverr.order": "The buyer's brief (or a note for the work), retry, or service <name>.",
    "fiverr.gig": "redraft, or price <basic> <standard> <premium>.",
    "fiverr.idea": "skip, redraft, price <basic> <standard> <premium>, or listed.",
    "build.backlog": "top <slug>, remove <slug>, or add <slug> with its fields.",
    "build.night": "A change to the backlog: top <slug>, remove <slug>, or add <slug>.",
}


# ---- an approval's desk fields ----------------------------------------------------------
def stream_of(capability: Any) -> str:
    name = str(capability or "")
    for prefix, stream in _STREAM_PREFIXES:
        if name.startswith(prefix):
            return stream
    return "other"


def proximity_of(capability: Any, stream: str) -> int:
    name = str(capability or "")
    if name in _PUBLISHING_CAPS:
        return PUBLISHING
    if stream == "client":
        return CLIENT_MONEY
    if stream in _SELLABLE:
        return SELLABLE
    if stream in _PUBLISHING:
        return PUBLISHING
    return OTHER


def approval_fields(capability: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """What the desk sorts and gates an approval by, computed once as it is parked."""
    row = {"capability": capability, "payload": dict(payload or {})}
    found = find_money(row)
    stream = stream_of(capability)
    return {"amount_cents": found.cents if found is not None else None, "stream": stream,
            "client_facing": str(capability).startswith("client."),
            "proximity": proximity_of(capability, stream)}


def with_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    """The row with its desk fields, derived now for a row parked before they existed."""
    out = dict(row)
    if any(k not in out for k in ("amount_cents", "stream", "client_facing", "proximity")):
        payload = out.get("payload") if isinstance(out.get("payload"), Mapping) else {}
        for key, value in approval_fields(str(out.get("capability")), payload).items():
            out.setdefault(key, value)
    return out


def bulk_ok(row: Mapping[str, Any]) -> bool:
    """May this approval be said yes to as one of a group? Only with no money at all in it
    (no amount, no currency, no money word - the card's own reading) and no client."""
    row = with_fields(row)
    if row.get("client_facing") or str(row.get("capability", "")).startswith("client."):
        return False
    found = find_money(row)
    return found is None or found.cents == 0


# ---- small helpers -------------------------------------------------------------------------
def _parse(stamp: Any) -> datetime | None:
    if not stamp:
        return None
    try:
        at = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


def stale_days(environ: Mapping[str, str] | None = None) -> int:
    env = os.environ if environ is None else environ
    raw = (env.get(STALE_ENV) or "").strip()
    if not raw:
        return DEFAULT_STALE_DAYS
    try:
        days = int(raw)
    except ValueError:
        _log.error("desk: %s=%r is not a whole number; using %s", STALE_ENV, raw,
                   DEFAULT_STALE_DAYS)
        return DEFAULT_STALE_DAYS
    if days < 1:
        _log.error("desk: %s=%r must be at least 1; using %s", STALE_ENV, raw,
                   DEFAULT_STALE_DAYS)
        return DEFAULT_STALE_DAYS
    return days


def split_id(desk_id: Any) -> tuple[str, str]:
    """``"<source>:<key>"`` -> (source, key); ValueError if it is not one."""
    source, sep, key = str(desk_id or "").partition(":")
    if not sep or source not in SOURCES or not key:
        raise ValueError("id: <source>:<key>, source one of " + ", ".join(SOURCES))
    return source, key


def _images(payload: Any) -> list[str]:
    """Image URLs anywhere in a payload: what the item would show, to look at first."""
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            for m in _IMAGE_URL.finditer(value):
                if m.group(0) not in found:
                    found.append(m.group(0))
        elif isinstance(value, Mapping):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    walk(payload)
    return found[:8]


def _plain(head: str) -> str:
    return re.sub(r"\*\*|__", "", head).strip()


def reply_kind_takes_reply(source: str, kind: Any) -> bool:
    kinds = _REPLY_KINDS[source]
    return kinds is None or kind in kinds


def card_lane(source: str, card: Mapping[str, Any]) -> str:
    """``reply`` for a card of a kind that asks for words and was posted asking (it is open,
    or was closed later - a ``status``); everything else, the nightly report included, is
    ``fyi``. A card of a reply kind posted WITHOUT replies (a Fiverr order card that needed
    nothing) was never open: it is news."""
    if not reply_kind_takes_reply(source, card.get("kind")):
        return "fyi"
    return "reply" if card.get("open") or card.get("status") else "fyi"


def is_parked(card: Mapping[str, Any]) -> bool:
    """Parked (by the expiry or the owner) and nothing said on it since. Parking only takes a
    card off the desk's open count and the stale clock: the card stays OPEN for its recorder,
    so a late Discord reply is still recorded and acted on exactly as on any open card - and
    a reply newer than the parking un-parks it here."""
    if card.get("status") != PARKED:
        return False
    since = _parse(card.get("closed_at"))
    replies = card.get("replies") if isinstance(card.get("replies"), Mapping) else {}
    return not any(isinstance(r, Mapping) and since is not None
                   and (_parse(r.get("at")) or since) > since for r in replies.values())


def _last_said(card: Mapping[str, Any]) -> datetime | None:
    """When the card last moved: posted, or the newest reply to it (the stale clock)."""
    stamps = [_parse(card.get("posted_at"))]
    replies = card.get("replies") if isinstance(card.get("replies"), Mapping) else {}
    stamps += [_parse(r.get("at")) for r in replies.values() if isinstance(r, Mapping)]
    found = [t for t in stamps if t is not None]
    return max(found) if found else None


def answered(card: Mapping[str, Any]) -> bool:
    replies = card.get("replies") if isinstance(card.get("replies"), Mapping) else {}
    return any(isinstance(r, Mapping) and r.get("state") in ANSWERED for r in replies.values())


# ---- the desk ------------------------------------------------------------------------------
class Desk:
    """The merged view over the approval queue and the three card records, plus the card
    expiry and the weekly "still waiting on you" note. ``render`` gives an approval's
    preview text (the Discord card's own words, PionirApp wires it to the gate)."""

    def __init__(self, state_root: Path, approvals: Any, *,
                 render: Callable[[Mapping[str, Any]], str] | None = None,
                 clock: Callable[[], datetime] | None = None,
                 monotonic: Callable[[], float] = time.monotonic,
                 environ: Mapping[str, str] | None = None) -> None:
        from .adapters.builds import store_for as build_store
        from .fiverr import store_for as fiverr_store
        from .quotes import QuoteCardStore

        self.state_root = Path(state_root)
        self.approvals = approvals
        self.render = render
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic
        self.stale_days = stale_days(environ)
        self.stores = {"quote": QuoteCardStore.for_state_root(self.state_root),
                       "fiverr": fiverr_store(self.state_root),
                       "build": build_store(self.state_root)}
        self._state_path = self.state_root / "desk" / DESK_STATE
        self._state_lock = threading.Lock()
        self._last_sweep = -float("inf")

    # ---- the list ----------------------------------------------------------------------
    def view(self, *, lane: str | None = None, stream: str | None = None) -> dict[str, Any]:
        """Every item waiting on the owner (and the last week's news), sorted: lane
        (decide, reply, fyi), then nearness to money, then oldest first. ``counts`` are
        over the whole desk, before ``lane``/``stream`` narrow the list."""
        self.sweep()
        items = [self._approval_item(r) for r in self.approvals.pending()]
        items += self._card_items()
        items.sort(key=lambda i: (LANES.index(i["lane"]), i["_proximity"],
                                  i["created_at"] or ""))
        counts = {name: sum(1 for i in items if i["lane"] == name) for name in LANES}
        if lane:
            items = [i for i in items if i["lane"] == lane]
        if stream:
            items = [i for i in items if i["stream"] == stream]
        for item in items:
            item.pop("_proximity", None)
        return {"items": items, "counts": counts, "stale_days": self.stale_days,
                "generated_at": self._clock().isoformat()}

    def _approval_item(self, row: Mapping[str, Any]) -> dict[str, Any]:
        from .discord_gate import digest_title, money_line

        row = with_fields(row)
        try:
            text = self.render(row) if self.render is not None else str(row.get("summary"))
        except Exception as error:  # noqa: BLE001 - the item still shows, its preview says why
            _log.warning("desk: approval %s: preview failed: %s", row.get("id"),
                         type(error).__name__, exc_info=True)
            text = f"(the preview could not be rendered: {type(error).__name__})\n" \
                   f"{row.get('summary')}"
        stream, capability = str(row["stream"]), str(row.get("capability"))
        return {
            "id": f"approval:{row['id']}", "ref": row["id"], "source": "approval",
            "lane": "decide", "title": digest_title(row), "summary": row.get("summary"),
            "stream": stream, "capability": capability,
            "amount_cents": row.get("amount_cents"),
            "client_facing": bool(row.get("client_facing")), "bulk_ok": bulk_ok(row),
            "group": f"{stream}:{capability}", "created_at": row.get("created_at"),
            "expires_at": row.get("expires_at"),
            "preview": {"text": text, "images": _images(row.get("payload"))},
            "detail": {"payload": row.get("payload"), "permissions": row.get("permissions"),
                       "requester": row.get("requester"), "batch": bool(row.get("batch")),
                       "digest_date": row.get("digest_date"), "context": row.get("context"),
                       "money_line": money_line(row)},
            "reply_hint": None, "_proximity": int(row["proximity"]),
        }

    def _card_items(self) -> list[dict[str, Any]]:
        now = self._clock()
        fyi_since = now - timedelta(days=FYI_DAYS)
        out = []
        for source, store in self.stores.items():
            for key, card in store.read()["cards"].items():
                if not isinstance(card, Mapping) or not card.get("message_ids"):
                    continue                     # still being posted, or never posted
                lane = card_lane(source, card)
                if lane == "reply" and not (card.get("open") and not answered(card)
                                            and not is_parked(card)):
                    continue                     # answered, closed or parked: not waiting
                if lane == "fyi":
                    posted = _parse(card.get("posted_at"))
                    if card.get("archived_at") or posted is None or posted < fyi_since:
                        continue
                out.append(self._card_item(source, key, card, lane))
        return out

    def _card_item(self, source: str, key: str, card: Mapping[str, Any],
                   lane: str) -> dict[str, Any]:
        kind = card.get("kind")
        stream, proximity, client = {
            "quote": ("client", CLIENT_MONEY, True),
            "fiverr": ("fiverr", CLIENT_MONEY if kind == "order" else SELLABLE,
                       kind == "order"),
            "build": ("builds", OTHER, False),
        }[source]
        posted = _parse(card.get("posted_at"))
        days = self.stale_days if lane == "reply" else FYI_DAYS
        replies = card.get("replies") if isinstance(card.get("replies"), Mapping) else {}
        shown = [{"id": rid, "state": r.get("state"), "at": r.get("at"),
                  "text": r.get("text"), "why": r.get("why"),
                  "approval_id": r.get("approval_id"), "via": r.get("via", "discord")}
                 for rid, r in replies.items() if isinstance(r, Mapping)]
        hint_key = source if source == "quote" else f"{source}.{kind}"
        return {
            "id": f"{source}:{key}", "ref": key, "source": source, "lane": lane,
            "title": card.get("title") or _card_title(source, key, card),
            "summary": _card_summary(source, card, lane),
            "stream": stream, "capability": None, "amount_cents": None,
            "client_facing": client, "bulk_ok": False,
            "group": f"{stream}:{source}.{kind or 'quote'}",
            "created_at": card.get("posted_at"),
            "expires_at": (posted + timedelta(days=days)).isoformat() if posted else None,
            "preview": {"text": str(card.get("preview") or ""), "images": []},
            "detail": {"kind": kind, "key": key, "ref": card.get("ref"),
                       "order_id": card.get("order_id"), "package": card.get("package"),
                       "message_ids": list(card.get("message_ids") or []),
                       "open": bool(card.get("open")), "replies": shown},
            "reply_hint": _REPLY_HINTS.get(hint_key) if card.get("open") else None,
            "_proximity": proximity,
        }

    # ---- closing and archiving ----------------------------------------------------------
    def sweep(self) -> list[str]:
        """Park every reply card left unanswered for ``stale_days`` as parked_no_answer.
        Returns the desk ids it closed. Cheap (three small files), safe to run on every
        read; a card answered, closed or of an FYI kind is never touched."""
        cutoff = self._clock() - timedelta(days=self.stale_days)
        closed: list[str] = []
        for source, store in self.stores.items():
            stale = [k for k, c in store.read()["cards"].items()
                     if self._stale(source, c, cutoff)]
            if not stale:
                continue
            now = self._clock().isoformat()

            def change(doc: dict[str, Any], stale: list[str] = stale, now: str = now,
                       source: str = source) -> list[str]:
                done = []
                for key in stale:
                    card = doc["cards"].get(key)
                    if card is None or not self._stale(source, card, cutoff):
                        continue        # answered or closed since it was read
                    # still open: a late reply is recorded and acted on as ever
                    card.update(status=PARKED, closed_at=now)
                    card.pop("dismissed_at", None)
                    card.pop("listed_at", None)
                    done.append(f"{source}:{key}")
                return done

            got = store.update(change)
            if got:
                _log.info("desk: %d %s card(s) parked with no answer after %d days: %s",
                          len(got), source, self.stale_days, ", ".join(got))
            closed += got
        return closed

    @staticmethod
    def _stale(source: str, card: Any, cutoff: datetime) -> bool:
        if not isinstance(card, Mapping) or not card.get("open") \
                or card_lane(source, card) != "reply" or answered(card) or is_parked(card):
            return False
        last = _last_said(card)
        return last is not None and last < cutoff

    def dismiss(self, desk_id: str) -> dict[str, Any]:
        """Archive an FYI item, or park a waiting reply card as parked_no_answer (the
        owner's own choice: it is not listed in the weekly note; like any parked card it
        still takes a late reply). An approval is answered with deny, never dismissed."""
        source, key = split_id(desk_id)
        if source == "approval":
            return {"ok": False, "error": {"type": "NotDismissable",
                                           "message": "an approval is approved or denied"}}
        now = self._clock().isoformat()

        def change(doc: dict[str, Any]) -> dict[str, Any]:
            card = doc["cards"].get(key)
            if not isinstance(card, dict) or not card.get("message_ids"):
                return {"ok": False, "error": {"type": "NotFound", "message": "no such card"}}
            if card_lane(source, card) == "fyi":
                if card.get("archived_at"):
                    return {"ok": True, "id": desk_id, "status": "archived", "already": True}
                card["archived_at"] = now
                return {"ok": True, "id": desk_id, "status": "archived"}
            if not card.get("open") or is_parked(card):
                status = card.get("status") or "closed"
                return {"ok": False, "error": {"type": "AlreadyClosed",
                                               "message": f"already {status}"}}
            card.update(status=PARKED, closed_at=now, dismissed_at=now)
            return {"ok": True, "id": desk_id, "status": PARKED}

        return self.stores[source].update(change)

    # ---- the weekly note ----------------------------------------------------------------
    def _read_state(self) -> dict[str, Any]:
        try:
            doc = json.loads(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            _log.error("desk: %s unreadable (%s); starting it again", self._state_path, error)
            return {}
        return doc if isinstance(doc, dict) else {}

    def _write_state(self, doc: dict[str, Any]) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        atomic.replace(tmp, self._state_path)

    def parked_unlisted(self) -> list[tuple[str, str, dict[str, Any]]]:
        """Cards the expiry parked (not the owner's own dismissals) not yet in a note."""
        out = []
        for source, store in self.stores.items():
            for key, card in store.read()["cards"].items():
                if isinstance(card, dict) and is_parked(card) \
                        and not card.get("dismissed_at") and not card.get("listed_at"):
                    out.append((source, key, card))
        return sorted(out, key=lambda t: str(t[2].get("posted_at") or ""))

    def weekly_summary(self, notify: Callable[[str, str], bool]) -> bool:
        """At most once every SUMMARY_EVERY: if any card was parked since the last note,
        send ONE note listing them (``notify(title, body)`` -> sent?). True if one was
        sent. A failed send is tried again on a later sweep; nothing is marked until it
        is sent."""
        with self._state_lock:
            state = self._read_state()
            last = _parse(state.get("last_summary_at"))
            now = self._clock()
            if last is not None and now - last < SUMMARY_EVERY:
                return False
            parked = self.parked_unlisted()
            if not parked:
                return False
            title, body = _summary_text(parked, self.stale_days)
            if not notify(title, body):
                _log.warning("desk: the weekly still-waiting note was not sent; trying later")
                return False
            stamp = now.isoformat()
            for source in {s for s, _k, _c in parked}:
                keys = {k for s, k, _c in parked if s == source}

                def mark(doc: dict[str, Any], keys: set[str] = keys) -> None:
                    for key in keys:
                        if key in doc["cards"]:
                            doc["cards"][key]["listed_at"] = stamp

                self.stores[source].update(mark)
            state["last_summary_at"] = stamp
            state["last_summary_count"] = len(parked)
            self._write_state(state)
            _log.info("desk: weekly still-waiting note sent (%d parked)", len(parked))
            return True

    def tick(self, notify: Callable[[str, str], bool]) -> None:
        """The Discord gate's poll calls this; it works at most every SWEEP_SECONDS. Never
        raises: a fault is logged and the gate's own work goes on."""
        now = self._monotonic()
        if now - self._last_sweep < SWEEP_SECONDS:
            return
        self._last_sweep = now
        try:
            self.sweep()
            self.weekly_summary(notify)
        except Exception:  # noqa: BLE001 - the gate's poll must go on
            _log.exception("desk: the card sweep failed")


def _card_title(source: str, key: str, card: Mapping[str, Any]) -> str:
    kind = card.get("kind")
    if source == "quote":
        package = card.get("package")
        return f"Quote needed · order {key}" + (f" ({package})" if package else "")
    if source == "build":
        if kind == "night":
            return f"Builds nightly report · {key.rsplit(':', 1)[-1]}"
        if kind == "backlog":
            return "Builds product backlog"
        return f"Builds {kind} · {key.split(':', 2)[-1]}"
    version = key.rsplit(":", 1)[-1] if key.count(":") >= 2 else ""
    label = {"gig": "Fiverr gig draft", "idea": "Fiverr gig idea",
             "order": "Fiverr order"}.get(str(kind), f"Fiverr {kind}")
    return f"{label} · {card.get('ref') or key}" + (f" ({version})" if version else "")


def _card_summary(source: str, card: Mapping[str, Any], lane: str) -> str:
    if lane == "fyi":
        if source == "build":
            from .adapters.builds import HEADS
        else:
            from .adapters.fiverr import HEADS
        return _plain(HEADS.get(str(card.get("kind")), str(card.get("kind"))))
    replies = card.get("replies") if isinstance(card.get("replies"), Mapping) else {}
    last = None
    for r in replies.values():
        if isinstance(r, Mapping) and (last is None or str(r.get("at")) > str(last.get("at"))):
            last = r
    waiting = "Waiting for your price" if source == "quote" else "Waiting for your reply"
    if last is not None and last.get("state") not in ANSWERED:
        why = last.get("why") or last.get("state")
        return f"{waiting} - your last reply ({last.get('text')!r}) was not taken: {why}"
    return waiting


def _summary_text(parked: list[tuple[str, str, dict[str, Any]]], days: int) -> tuple[str, str]:
    lines = [(f"Parked after {days} days with no answer - nothing was done with them. Reply "
              "on the Approval Desk (or ask for a fresh card) if you still want any:")]
    for source, key, card in parked:
        posted = str(card.get("posted_at") or "")[:10]
        lines.append(f"- {card.get('title') or _card_title(source, key, card)}"
                     + (f" (posted {posted})" if posted else ""))
    body = "\n".join(lines)
    if len(body) > 1800:
        body = body[:1780].rsplit("\n", 1)[0] + "\n- ... and more on the desk"
    return f"Still waiting on you: {len(parked)} parked", body
