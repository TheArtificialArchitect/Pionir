"""Daily batching of routine public approvals: the policy and the schedule.

The owner's decision, 2026-09-26: "Batch low-risk ones daily." A routine public item (a
blog post, a dev.to cross-post, an Instagram card, a product listing) no longer gets its
own Discord card the moment it is parked; it waits in the SAME approval queue, with the
same record and the same checks, and arrives with the others as ONE daily digest card
that the owner answers item by item (pionir/discord_gate.py draws and reads it).
Anything that involves money or a client - a client email, a quote, a delivery, a
refund, a pay link, any spending - is never batched: it is its own card, at once.

Three layers decide, and all must agree before anything waits for a digest:

1. the capability declares ``batchable=True`` (contracts.Capability; off by default, and
   refused at definition for anything that spends money or is not an every-call
   approval);
2. ``batch_refusal`` below re-checks the declaration per call: no money, no client
   words in the capability's name, and nothing in the payload that the gate's own money
   line would flag;
3. batching is switched on (``DigestSettings.enabled``; off -> everything is its own
   card again, which is the visible side).

A batched row waits at most ``expire_days`` (default 7) and is then denied as
"expired" - it never runs unanswered, and the next digest reports it.

Settings, environment first then ``<state_root>/discord/config.json``:
PIONIR_DIGEST (0/off disables batching) / "digest", PIONIR_DIGEST_TIME ("HH:MM" local,
default 09:00) / "digest_time", PIONIR_DIGEST_EXPIRE_DAYS (default 7) /
"digest_expire_days".

The owner can ask for the digest early: ``request_digest`` (POST /api/approvals/digest)
leaves a request the gate answers on its next poll.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from . import atomic

_log = logging.getLogger(__name__)

DEFAULT_TIME = "09:00"
DEFAULT_EXPIRE_DAYS = 7
MAX_EXPIRE_DAYS = 60
REQUEST_NAME = "digest-request.json"
# The server (asking) and the gate thread (answering) share the request file: every
# read-modify-write of it holds this lock, so an answer can never land on a newer request.
_REQUEST_LOCK = threading.RLock()

# Words that, anywhere in a capability's name, mean money or a client: such a capability
# is never batched even if someone marks it batchable. Belt and braces over the
# declaration - the gate's MONEY_WORDS are checked too, through money_line.
NEVER_BATCH_WORDS = frozenset({
    "client", "clients", "customer", "customers", "email", "mail", "quote", "quotes",
    "deliver", "delivery", "release", "remind", "reminder", "refund", "refunds", "pay",
    "payment", "payments", "payout", "paylink", "invoice", "billing", "charge", "checkout",
    "buy", "purchase", "spend", "order", "orders", "subscribe", "subscription", "transfer",
    "money", "wallet", "stripe", "paypal",
})


# Granted, alongside the row's own permissions, to an action approved as a BATCHED row: an
# adapter that allowed batching only for a narrower case (product.gumroad_publish: a NEW
# listing) refuses, touching nothing, if the case no longer holds when it runs.
BATCHED_GRANT = "approval.batched"


def batch_refusal(capability: Any, payload: Mapping[str, Any] | None,
                  context: Mapping[str, Any] | None = None) -> str | None:
    """None if this parked action may wait for the daily digest; otherwise why it must
    be its own card at once. Fails closed: anything unknown or odd is refused.

    ``context`` is what the capability's adapter found when it was parked
    (``park_context``): it can refuse by itself (``batch_refusal``), and a capability that
    carries its own sale price (``sale_price_keys``) is batchable only as a listing the
    adapter confirmed is NEW - an update of a live listing, and so any price change, is
    always its own card."""

    if capability is None:
        return "unknown capability"
    if getattr(capability, "batchable", False) is not True:
        return "not declared batchable"
    if getattr(capability, "spends_money", True) is not False:
        return "spends money"
    if getattr(capability, "requires_approval", False) is not True:
        return "not an every-call approval"
    name = str(getattr(capability, "name", "") or "")
    if not name or set(re.split(r"[^a-z]+", name.lower())) & NEVER_BATCH_WORDS:
        return "a money or client capability"
    if not isinstance(payload, Mapping):
        return "no payload"
    context = context if isinstance(context, Mapping) else {}
    refused = context.get("batch_refusal")
    if refused:
        return str(refused)
    price_keys = getattr(capability, "sale_price_keys", frozenset())
    if not isinstance(price_keys, frozenset):
        return "odd sale price keys"
    if price_keys and context.get("listing") != "new":
        return "not confirmed a new listing (an update or a price change is its own card)"
    from .discord_gate import money_line  # the card's own money check, not a copy of it
    if money_line({"capability": name, "payload": payload}, allowed=price_keys):
        return "the payload moves money"
    return None


# ---------------------------------------------------------------- settings
def _parse_time(raw: Any) -> time | None:
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(raw))
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


@dataclass(frozen=True)
class DigestSettings:
    enabled: bool = True
    at: time = time(9, 0)                 # local wall-clock time of the daily digest
    expire_days: int = DEFAULT_EXPIRE_DAYS

    @property
    def expires_after(self) -> timedelta:
        return timedelta(days=self.expire_days)

    @property
    def time_text(self) -> str:
        return f"{self.at.hour:02d}:{self.at.minute:02d}"

    def slot(self, day: date, tz: Any) -> datetime:
        """The digest moment on ``day`` in the local zone ``tz``."""
        return datetime.combine(day, self.at, tzinfo=tz)

    def next_digest(self, now_local: datetime) -> datetime:
        """The next scheduled digest at or after ``now_local`` (tz-aware, local)."""
        today = self.slot(now_local.date(), now_local.tzinfo)
        return today if now_local < today else self.slot(now_local.date() + timedelta(days=1),
                                                          now_local.tzinfo)

    def last_slot(self, now_local: datetime) -> datetime:
        """The latest scheduled digest moment at or before ``now_local``."""
        today = self.slot(now_local.date(), now_local.tzinfo)
        return today if now_local >= today else self.slot(now_local.date() - timedelta(days=1),
                                                           now_local.tzinfo)

    @classmethod
    def from_environment(cls, state_root: Path | None = None,
                         environ: Mapping[str, str] | None = None) -> DigestSettings:
        env = os.environ if environ is None else environ
        file_values: dict[str, Any] = {}
        if state_root is not None:
            path = state_root / "discord" / "config.json"
            if path.exists():
                try:
                    loaded = json.loads(path.read_text(encoding="utf-8-sig"))
                    if isinstance(loaded, dict):
                        file_values = loaded
                except (OSError, ValueError) as error:
                    _log.error("digest: cannot read %s: %s", path, error)

        def pick(env_name: str, file_key: str) -> str | None:
            raw = (env.get(env_name) or "").strip()
            if raw:
                return raw
            value = file_values.get(file_key)
            return str(value).strip() if value is not None and str(value).strip() else None

        enabled_raw = (pick("PIONIR_DIGEST", "digest") or "1").lower()
        at = time(9, 0)
        time_raw = pick("PIONIR_DIGEST_TIME", "digest_time")
        if time_raw is not None:
            parsed = _parse_time(time_raw)
            if parsed is None:
                _log.error("digest: time %r is not HH:MM; using %s", time_raw, DEFAULT_TIME)
            else:
                at = parsed
        days = DEFAULT_EXPIRE_DAYS
        days_raw = pick("PIONIR_DIGEST_EXPIRE_DAYS", "digest_expire_days")
        if days_raw is not None:
            try:
                days = int(days_raw)
            except ValueError:
                days = 0
            if not 1 <= days <= MAX_EXPIRE_DAYS:
                _log.error("digest: expiry %r is not 1..%d days; using %d", days_raw,
                           MAX_EXPIRE_DAYS, DEFAULT_EXPIRE_DAYS)
                days = DEFAULT_EXPIRE_DAYS
        return cls(enabled=enabled_raw not in ("0", "off", "false", "no"), at=at,
                   expire_days=days)


def local_now() -> datetime:
    return datetime.now().astimezone()


# ---------------------------------------------------------------- "send it now"
def request_path(state_root: Path) -> Path:
    return state_root / "discord" / REQUEST_NAME


def request_digest(state_root: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Ask the gate for the digest now (the owner's urgent override). A request is
    answered once, by id; asking twice before it is answered is the same request."""
    with _REQUEST_LOCK:
        current = read_request(state_root)
        if current is not None and current.get("answered") is not True:
            return current
        request = {"id": uuid.uuid4().hex[:12],
                   "requested_at": (now or local_now()).isoformat(), "answered": False}
        _write(request_path(state_root), request)
        return request


def read_request(state_root: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(request_path(state_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("id"), str) else None


def answer_request(state_root: Path, request_id: str) -> None:
    """Mark request ``request_id`` answered - only that one: a newer request stays open."""
    with _REQUEST_LOCK:
        current = read_request(state_root)
        if current is not None and current.get("id") == request_id:
            _write(request_path(state_root), {**current, "answered": True})


def _write(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # A tmp of its own per write: two writers (the server asking, the gate answering) can
    # never publish each other's half - an answer never lands on a newer request.
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(dict(data), ensure_ascii=False, indent=2), encoding="utf-8")
    atomic.replace(tmp, path)
