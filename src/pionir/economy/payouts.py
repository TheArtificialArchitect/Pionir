"""What a verified event is worth, in Bolts. A plain table; no model, no judgement.

Edit ``PAYOUTS`` to change an amount. Every amount is a whole number. ``excellence`` is
added only when the event POSITIVELY records that the owner changed nothing
(``owner_edited is False``); unknown is not "no edit", so it pays nothing extra.

Daily mint caps bound the whole economy (per account and global). A payout over a cap is
REFUSED with a typed reason and counted in ``refusals.jsonl``, never dropped silently; it
is tried again on the next run, so it is paid the next day if still verified. This path is
separate from the approval / money gate: nothing here is consulted by it.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from .ledger import ACCOUNT_RE, Ledger, LedgerBroken, LedgerError, Row

log = logging.getLogger("pionir.economy")

PAYOUTS: dict[str, dict[str, int]] = {
    "build_staged": {"base": 25},
    "post_approved": {"base": 10, "excellence": 5},
    "order_delivered": {"base": 30},
    "product_published": {"base": 40, "excellence": 10},
    # scales with the cents newly received, capped per payout: 1 Bolt per $1, at most 100
    "revenue_received": {"base": 0, "cents_per_bolt": 100, "max": 100},
}

DAILY_CAP_PER_ACCOUNT = 100
DAILY_CAP_GLOBAL = 500

REFUSALS_NAME = "refusals.jsonl"


class RefuseReason(StrEnum):
    ACCOUNT_DAILY_CAP = "account_daily_cap"
    GLOBAL_DAILY_CAP = "global_daily_cap"
    LEDGER_BROKEN = "ledger_broken"
    UNKNOWN_KIND = "unknown_kind"
    BAD_ACCOUNT = "bad_account"


@dataclass(frozen=True, slots=True)
class Event:
    """A verified outcome that already happened and was recorded elsewhere."""

    kind: str
    event_id: str
    account: str
    ts: float
    cents: int = 0                    # revenue_received: cents newly received
    owner_edited: bool | None = None  # True / False when recorded, None when not known
    detail: str = ""


@dataclass(frozen=True, slots=True)
class PayResult:
    status: str                       # paid | duplicate | refused | nothing_due
    row: Row | None = None
    reason: RefuseReason | None = None
    amount: int = 0


def day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts).date().isoformat()


def amount_for(event: Event) -> tuple[int, bool]:
    """(Bolts, excellence_added) for an event; (-1, False) for a kind with no entry."""
    table = PAYOUTS.get(event.kind)
    if table is None:
        return -1, False
    amount = int(table.get("base", 0))
    per = int(table.get("cents_per_bolt", 0))
    if per > 0:
        amount += min(int(table.get("max", 0)), max(0, int(event.cents)) // per)
    bonus = event.owner_edited is False and int(table.get("excellence", 0)) > 0
    if bonus:
        amount += int(table["excellence"])
    return amount, bool(bonus)


class RefusalLog:
    """Refused payouts, counted. A plain JSONL beside the ledger (not part of the chain)."""

    def __init__(self, path: Path, *, now: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._now = now

    @classmethod
    def in_dir(cls, directory: Path, **kw) -> "RefusalLog":
        return cls(Path(directory) / REFUSALS_NAME, **kw)

    def _read(self) -> tuple[list[dict], int]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return [], 0
        out, bad = [], 0
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if isinstance(obj, dict):
                out.append(obj)
            else:
                bad += 1
        return out, bad

    def record(self, event: Event, reason: RefuseReason, amount: int) -> bool:
        """Count one refusal per (event, reason, day). True when newly counted."""
        today = day_of(self._now())
        entries, _ = self._read()
        for e in entries:
            if (e.get("event_id"), e.get("reason"), e.get("day")) == (
                    event.event_id, str(reason), today):
                return False
        entry = {"ts": self._now(), "day": today, "event_id": event.event_id,
                 "kind": event.kind, "account": event.account, "reason": str(reason),
                 "amount": amount}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("ab") as fh:
            fh.write((json.dumps(entry, sort_keys=True) + "\n").encode("utf-8"))
        log.warning("economy: refused %s %s for %s (%s, %d Bolts)", event.kind,
                    event.event_id, event.account, reason, amount)
        return True

    def counts(self, day: str | None = None) -> dict:
        """{"total": n, "by_reason": {...}, "unreadable_lines": n} for ``day`` (local date)."""
        day = day or day_of(self._now())
        entries, bad = self._read()
        by: dict[str, int] = {}
        for e in entries:
            if e.get("day") == day:
                by[str(e.get("reason"))] = by.get(str(e.get("reason")), 0) + 1
        return {"day": day, "total": sum(by.values()), "by_reason": by,
                "unreadable_lines": bad}


def minted_today(ledger: Ledger, day: str) -> tuple[dict[str, int], int]:
    per: dict[str, int] = {}
    for row in ledger.rows():
        if row.delta > 0 and day_of(row.ts) == day:
            per[row.account] = per.get(row.account, 0) + row.delta
    return per, sum(per.values())


def pay(event: Event, ledger: Ledger, refusals: RefusalLog | None = None, *,
        now: Callable[[], float] | None = None,
        per_account_cap: int | None = None, global_cap: int | None = None) -> PayResult:
    """Pay one verified event exactly once. Never raises on a refusal. "Today" is the
    ledger's own clock unless ``now`` is given."""
    now = now or ledger._now
    per_cap = DAILY_CAP_PER_ACCOUNT if per_account_cap is None else per_account_cap
    glob_cap = DAILY_CAP_GLOBAL if global_cap is None else global_cap

    def refuse(reason: RefuseReason, amount: int) -> PayResult:
        if refusals is not None:
            refusals.record(event, reason, amount)
        return PayResult("refused", None, reason, amount)

    amount, bonus = amount_for(event)
    if amount < 0:
        return refuse(RefuseReason.UNKNOWN_KIND, 0)
    if not ACCOUNT_RE.match(event.account or ""):
        return refuse(RefuseReason.BAD_ACCOUNT, amount)
    try:
        existing = ledger.get(event.event_id)
    except LedgerError:
        return refuse(RefuseReason.LEDGER_BROKEN, amount)
    if existing is not None:
        return PayResult("duplicate", existing, None, existing.delta)
    if amount == 0:
        return PayResult("nothing_due", None, None, 0)
    today = day_of(float(now()))
    per, total = minted_today(ledger, today)
    if per.get(event.account, 0) + amount > per_cap:
        return refuse(RefuseReason.ACCOUNT_DAILY_CAP, amount)
    if total + amount > glob_cap:
        return refuse(RefuseReason.GLOBAL_DAILY_CAP, amount)
    reason = event.kind + (" +excellence" if bonus else "")
    if event.detail:
        reason += f" ({event.detail[:80]})"
    try:
        done = ledger.append(event.account, amount, reason, event.event_id)
    except LedgerBroken:
        return refuse(RefuseReason.LEDGER_BROKEN, amount)
    return PayResult("paid" if done.created else "duplicate", done.row, None, amount)


def pay_for(event: Event, ledger: Ledger, refusals: RefusalLog | None = None,
            **kw) -> Row | None:
    """The row that now records this event's payout, or None when nothing was paid."""
    result = pay(event, ledger, refusals, **kw)
    return result.row if result.status in ("paid", "duplicate") else None
