"""A queue of privileged actions waiting on Ian's yes or no.

Pionir's gate used to simply refuse a privileged action that carried no
permission. This parks it instead: the action waits here with a plain-language
summary, Ian approves or denies it (from Moss's phone glass), and on approval it
runs with exactly the permission it needed - nothing more. Durable on disk, so a
waiting action survives a restart and is never silently lost.

This is the whole reason offensive tools can be reached at all: Moss's own
initiative can propose a scan or a defense, but it lands here, not on the wire,
until Ian says yes.
"""
from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pionir import atomic

# A parked action is a question for Ian; a question nobody answered for a day is
# stale, not still open. Expired rows are auto-denied (reason "expired") the next
# time anyone looks, and resolved rows are dropped after a week so the queue file
# never grows without bound. Both are ways back, not latches.
EXPIRES_AFTER = timedelta(hours=24)
RETAIN_RESOLVED = timedelta(days=7)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


@dataclass
class Approval:
    id: str
    created_at: str
    capability: str
    payload: dict[str, Any]
    permissions: list[str]        # granted verbatim if approved, never widened
    summary: str
    requester: str
    status: str = "pending"       # pending | running | approved | approved_failed | denied
    resolved_at: str | None = None
    result: dict[str, Any] | None = None
    expires_at: str | None = None  # a pending row past this is auto-denied ("expired")
    reason: str | None = None      # why it was denied, when it was
    task_id: str | None = None     # the job that ran it, once claimed
    batch: bool = False            # waits for the owner's daily digest (pionir/batching.py)
    digest_date: str | None = None # the digest it is (or will next be) listed in, local date
    context: dict[str, Any] | None = None  # what its adapter found when it was parked


class ApprovalQueue:
    """Durable, thread-safe (the server is threaded) queue of pending actions."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # A running approval belonged to a worker in the previous process.
        # Workers are not resumable, so leaving it "running" forever would turn
        # a crash into a latch. Fail closed and make the interruption visible.
        with self._lock:
            rows = self._load()
            now = _now()
            changed = False
            for row in rows:
                if row.get("status") == "running":
                    row["status"] = "denied"
                    row["reason"] = "interrupted"
                    row["resolved_at"] = now
                    changed = True
            if changed:
                self._save(rows)

    def _load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return data if isinstance(data, list) else []

    def _save(self, rows: list[dict[str, Any]]) -> None:
        rows = self._retain(rows)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        atomic.replace(tmp, self._path)   # atomic, retried on Windows; a half-written queue never reaches a reader

    @staticmethod
    def _retain(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop rows resolved more than RETAIN_RESOLVED ago. Pending and running
        rows are never dropped here - expiry is a visible denial, not a vanishing."""
        cutoff = datetime.now(UTC) - RETAIN_RESOLVED
        kept = []
        for row in rows:
            if row.get("status") in ("pending", "running"):
                kept.append(row)
                continue
            resolved = _parse(row.get("resolved_at"))
            if resolved is None or resolved >= cutoff:
                kept.append(row)
        return kept

    @staticmethod
    def _expired(row: dict[str, Any], now: datetime) -> bool:
        deadline = _parse(row.get("expires_at"))
        if deadline is None:
            created = _parse(row.get("created_at"))
            if created is None:
                return False
            deadline = created + EXPIRES_AFTER
        return now >= deadline

    def _sweep(self, rows: list[dict[str, Any]]) -> bool:
        """Auto-deny pending rows past their expiry. Returns True if any changed."""
        now = datetime.now(UTC)
        changed = False
        for row in rows:
            if row.get("status") == "pending" and self._expired(row, now):
                row["status"] = "denied"
                row["reason"] = "expired"
                row["resolved_at"] = now.isoformat()
                changed = True
        return changed

    def enqueue(self, capability: str, payload: dict[str, Any], permissions: list[str],
                summary: str, requester: str = "moss", *, batch: bool = False,
                digest_date: str | None = None,
                expires_after: timedelta | None = None,
                context: dict[str, Any] | None = None) -> str:
        """Park one action. ``batch`` rows are the same record - same checks, same claim,
        same settle - that wait for the daily digest instead of their own card, with
        their own (longer) ``expires_after``; nothing else about them differs."""
        with self._lock:
            rows = self._load()
            self._sweep(rows)
            now = datetime.now(UTC)
            approval = Approval(
                id=uuid.uuid4().hex[:12], created_at=now.isoformat(), capability=capability,
                payload=dict(payload), permissions=list(permissions), summary=summary,
                requester=requester,
                expires_at=(now + (expires_after or EXPIRES_AFTER)).isoformat(),
                batch=bool(batch), digest_date=digest_date if batch else None,
                context=dict(context) if context else None,
            )
            rows.append(asdict(approval))
            self._save(rows)
            return approval.id

    def get(self, approval_id: str) -> dict[str, Any] | None:
        with self._lock:
            for row in self._load():
                if row["id"] == approval_id:
                    return row
        return None

    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._load()
            if self._sweep(rows):
                self._save(rows)
            return [r for r in rows if r["status"] == "pending"]

    def note_digest(self, approval_ids: list[str], digest_date: str) -> int:
        """Record that these still-pending batched rows are listed in the digest of
        ``digest_date`` (a carried-over row moves to the new date). Touches nothing
        else - never a status. Returns how many rows changed."""
        wanted = set(approval_ids)
        with self._lock:
            rows = self._load()
            changed = 0
            for row in rows:
                if row.get("id") in wanted and row.get("status") == "pending" \
                        and row.get("batch") and row.get("digest_date") != digest_date:
                    row["digest_date"] = digest_date
                    changed += 1
            if changed:
                self._save(rows)
            return changed

    def batched_expired(self) -> list[dict[str, Any]]:
        """Batched rows that expired unanswered (denied, reason "expired"), oldest first:
        what the next digest reports as "expired, not run"."""
        with self._lock:
            rows = self._load()
            if self._sweep(rows):
                self._save(rows)
            return [dict(r) for r in rows if r.get("batch") and r.get("status") == "denied"
                    and r.get("reason") == "expired"]

    def find(self, capability: str, key: str, value: Any) -> list[dict[str, Any]]:
        """Every row (any status, newest first) for ``capability`` whose payload[key] is
        ``value``: how a producer finds the approval it already parked for one request
        (a quote's quote_ref) instead of parking a second one."""
        with self._lock:
            return [dict(r) for r in reversed(self._load())
                    if r.get("capability") == capability
                    and isinstance(r.get("payload"), dict) and r["payload"].get(key) == value]

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return list(reversed(self._load()))[:limit]

    def claim(self, approval_id: str, *, task_id: str | None = None) -> dict[str, Any] | None:
        """Atomically move a pending action to ``running`` and return it, so
        exactly one caller gets to run it. Returns None if it is not pending
        (already claimed, resolved, expired, or gone). The caller then finishes
        it with ``resolve(..., from_status="running")``."""
        with self._lock:
            rows = self._load()
            swept = self._sweep(rows)
            for row in rows:
                if row["id"] == approval_id and row["status"] == "pending":
                    row["status"] = "running"
                    row["claimed_at"] = _now()
                    if task_id is not None:
                        row["task_id"] = task_id
                    self._save(rows)
                    return dict(row)
            if swept:
                self._save(rows)
        return None

    def resolve(self, approval_id: str, status: str,
                result: dict[str, Any] | None = None, *,
                from_status: str = "pending", reason: str | None = None) -> bool:
        """Mark a still-pending (or, with ``from_status="running"``, a claimed)
        action approved or denied. Returns False if it was already resolved (or
        gone) - so a double tap can't run something twice."""
        with self._lock:
            rows = self._load()
            swept = self._sweep(rows)
            for row in rows:
                if row["id"] == approval_id and row["status"] == from_status:
                    row["status"] = status
                    row["resolved_at"] = _now()
                    if reason is not None:
                        row["reason"] = reason
                    if result is not None:
                        row["result"] = result
                    self._save(rows)
                    return True
            if swept:
                self._save(rows)
        return False
