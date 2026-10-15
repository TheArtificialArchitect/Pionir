"""Where an approval's money is: the one reading both the Discord card and the Approval Desk use.

The Discord gate's bold money line (discord_gate.money_line) and the desk's
``amount_cents`` / ``bulk_ok`` used to be two readings of one payload, free to drift
apart. Both now ask ``find_money`` here: the same keys, the same order, the same first
hit - so an item the card calls money is never one the desk lets through in a batch.

Pure and light (no adapters, no Discord): pionir/approvals.py imports it at enqueue.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# Payload keys that carry an amount of money. A producer that spends money says
# so with one of these (a number, a string, or {"amount": ..., "currency": ...}),
# optionally with a sibling "currency"; ``spends_money: true`` with no amount is
# shown as an unstated amount, never as nothing.
MONEY_KEYS = ("spend", "amount", "cost", "price", "charge")
# Capability words that suggest money moves even when no amount was supplied.
MONEY_WORDS = frozenset({
    "buy", "purchase", "pay", "payment", "payout", "spend", "checkout", "charge",
    "order", "subscribe", "subscription", "invoice", "refund", "transfer",
})
# The desk's own fields on an approval row (pionir/desk.py), derived FROM the payload: never
# read back as money themselves, or the card would name ``amount_cents`` instead of the
# payload key it came from.
DERIVED_KEYS = frozenset({"amount_cents", "stream", "client_facing", "proximity"})
_PLAIN_AMOUNT = re.compile(r"[$€£]?\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?")


@dataclass(frozen=True)
class Money:
    """The first money finding in a row. ``kind``: ``amount`` (a MONEY_KEYS value),
    ``cents`` (a ``*_cents`` number), ``currency`` (a currency named, no amount) or
    ``unstated`` (spends_money / a money word, no amount). ``text`` is the amount as the
    card shows it; ``cents`` is it in cents when it reads as a plain number, else None."""

    kind: str
    key: str | None = None
    text: str | None = None
    cents: int | None = None


def format_amount(value: Any, currency: Any) -> str | None:
    if isinstance(value, Mapping):
        return format_amount(value.get("amount", value.get("value")),
                             value.get("currency", currency))
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        text = f"{value:,.2f}"
    else:
        text = str(value).strip()
        if not text:
            return None
    cur = str(currency).strip().upper() if currency else ""
    return f"{text} {cur}" if cur else f"{text} (currency not stated)"


def _cents_of(value: Any) -> int | None:
    """A MONEY_KEYS value in cents, when it is a plain number (or a plain "$12.50"); None
    when it cannot be read as one - the card still shows it, the desk just has no number."""
    if isinstance(value, Mapping):
        return _cents_of(value.get("amount", value.get("value")))
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return round(value * 100)
    m = _PLAIN_AMOUNT.fullmatch(str(value).strip())
    if m is None:
        return None
    return int(m.group(1).replace(",", "")) * 100 + int((m.group(2) or "0").ljust(2, "0"))


def find_money(row: Mapping[str, Any], *,
               allowed: frozenset[str] | set[str] = frozenset()) -> Money | None:
    """The first sign of money in an approval row (its own fields, then its payload), or
    None. ``allowed`` names payload keys a caller has already accounted for (a new
    listing's own sale price, pionir/batching.py) - every other money field still counts."""
    payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
    for source in (row, payload):
        currency = source.get("currency")
        for key in MONEY_KEYS:
            if key in source and key not in allowed:
                text = format_amount(source[key], currency)
                if text:
                    return Money("amount", key, text, _cents_of(source[key]))
    for source in (row, payload):
        for key, value in source.items():
            if not isinstance(key, str) or key in allowed                     or (source is row and key in DERIVED_KEYS):
                continue
            name = key.lower()
            if name.endswith("_cents") and isinstance(value, (int, float)) \
                    and not isinstance(value, bool):
                return Money("cents", key, format_amount(value / 100, source.get("currency")),
                             round(value))
            if (name == "currency" or name.endswith("_currency")) and value:
                return Money("currency", key)
    flagged = bool(row.get("spends_money") or payload.get("spends_money"))
    words = set(re.split(r"[^a-z]+", str(row.get("capability", "")).lower()))
    if flagged or words & MONEY_WORDS:
        return Money("unstated")
    return None


def amount_cents(row: Mapping[str, Any]) -> int | None:
    """The row's amount in cents as the card's money line reads it, or None (no money, or
    money whose amount is not a plain number)."""
    found = find_money(row)
    return found.cents if found is not None else None
