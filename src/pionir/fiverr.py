"""The owner's replies to the Fiverr desk's Discord cards.

The Fiverr desk (crew/fiverr) never touches Fiverr: it hands the owner cards in his Discord
channel (``fiverr.card``, adapters/fiverr.py). Some cards ask him something, and he answers
by REPLYING to the card:

- an ORDER card whose brief was not in Fiverr's emails: his reply is the buyer's brief (later
  replies are notes for the work, ``retry`` retries a stopped order, ``service <name>`` routes
  an order the desk could not);
- a GIG card: ``redraft``, or ``price <basic> <standard> <premium>`` - his own prices.

The Discord gate reads the channel through REST (the same way it reads quote replies,
pionir/quotes.py) and hands each reply to ``FiverrReplies``, which **records the owner's
reply and nothing else**: a reply from anyone else is not read at all - not stored, not
answered. The record (``<state_root>/discord/fiverr-cards.json``, the cards the adapter
posted and every owner reply to them) is what ``fiverr.inbox`` serves the crew, which checks
the author AGAIN before returning a reply. Each reply is recorded once, by its Discord id.

Nothing here acts on a reply: the crew reads it on its next run. Nothing is sent to a buyer
from here or anywhere else in Pionir - the owner does that on Fiverr himself.
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .quotes import INTENT_HINT, MAX_PAGES, QuoteCardStore

_log = logging.getLogger(__name__)

STORE_NAME = "fiverr-cards.json"
CARD_RETENTION = timedelta(days=90)
MAX_REPLY = 6000
_SNOWFLAKE = re.compile(r"\d{5,25}")
RECORDED = ("📥 Recorded. The Fiverr desk reads it on its next run (within minutes). Nothing "
            "is sent to the buyer: you do that on Fiverr.")

Call = Callable[..., Any]


def store_for(state_root: Path) -> QuoteCardStore:
    """The Fiverr card record: the same shape and locking as the quote cards' record."""
    return QuoteCardStore(Path(state_root) / "discord" / STORE_NAME)


def _now() -> datetime:
    return datetime.now(UTC)


def owner_replies(store: QuoteCardStore, owner: str | None, kind: str | None = None) -> list:
    """Every recorded reply BY THE OWNER to a card (of this kind), oldest first. Without an
    owner configured there are none: no reply can count."""
    if not owner:
        return []
    out = []
    for key, card in store.read()["cards"].items():
        if kind is not None and card.get("kind") != kind:
            continue
        for rid, r in (card.get("replies") or {}).items():
            if not isinstance(r, dict) or str(r.get("by")) != str(owner) \
                    or r.get("state") != "recorded":
                continue
            out.append({"reply_id": str(rid), "key": key, "kind": card.get("kind"),
                        "ref": card.get("ref"), "text": str(r.get("text") or ""),
                        "at": r.get("at")})
    return sorted(out, key=lambda r: int(r["reply_id"]) if r["reply_id"].isdigit() else 0)


class FiverrReplies:
    """Records the owner's replies to the Fiverr cards that take one. Driven by the Discord
    gate's poll (``tick``) with the gate's own client."""

    def __init__(self, store: QuoteCardStore, *, every: float = 10.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.store = store
        self.every = every
        self._clock = clock
        self._last = -float("inf")

    def tick(self, call: Call, owner: str | None, channel: str) -> None:
        if owner is None:
            return                              # no owner configured: no reply can count
        doc = self.store.read()
        cards = {k: c for k, c in doc["cards"].items() if c.get("open")}
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
                self.consider(call, owner, channel, message)
            if messages:
                cursor = str(messages[-1]["id"])
                self.store.update(lambda d, c=cursor: d.__setitem__("cursor", c))
            if len(messages) < 100:
                break
        self._prune()

    def consider(self, call: Call, owner: str, channel: str, message: dict) -> None:
        ref = (message.get("message_reference") or {}).get("message_id")
        if ref is None:
            return
        doc = self.store.read()
        key = next((k for k, c in doc["cards"].items()
                    if str(ref) in [str(m) for m in c.get("message_ids") or []]), None)
        if key is None:
            return
        author = str((message.get("author") or {}).get("id"))
        if author != str(owner):
            # only the owner answers the desk; anyone else's reply is not read at all
            _log.info("fiverr: a reply to card %s from someone else was ignored", key)
            return
        reply_id = str(message["id"])
        card = doc["cards"][key]
        if reply_id in (card.get("replies") or {}) or not card.get("open"):
            return
        text = str(message.get("content") or "").strip()
        state = "recorded" if text else "unreadable"

        def record(d: dict) -> None:
            d["cards"][key].setdefault("replies", {})[reply_id] = {
                "state": state, "at": _now().isoformat(), "by": author,
                "text": text[:MAX_REPLY]}

        self.store.update(record)       # recorded BEFORE it is answered: never twice
        answer = RECORDED if text else INTENT_HINT.replace("the quote card", "the card")
        call("POST", f"/channels/{channel}/messages", {
            "content": answer[:1900],
            "allowed_mentions": {"parse": [], "replied_user": False},
            "message_reference": {"message_id": reply_id, "fail_if_not_exists": False}})

    def _prune(self) -> None:
        cutoff = _now() - CARD_RETENTION

        def old(card: dict) -> bool:
            try:
                at = datetime.fromisoformat(str(card.get("posted_at")))
            except ValueError:
                return False
            return (at if at.tzinfo else at.replace(tzinfo=UTC)) < cutoff

        if any(old(c) for c in self.store.read()["cards"].values()):
            self.store.update(lambda d: [d["cards"].pop(k) for k in
                                         [k for k, c in d["cards"].items() if old(c)]])
