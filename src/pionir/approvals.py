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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class Approval:
    id: str
    created_at: str
    capability: str
    payload: dict[str, Any]
    permissions: list[str]        # granted verbatim if approved, never widened
    summary: str
    requester: str
    status: str = "pending"       # pending | approved | denied
    resolved_at: str | None = None
    result: dict[str, Any] | None = None


class ApprovalQueue:
    """Durable, thread-safe (the server is threaded) queue of pending actions."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return data if isinstance(data, list) else []

    def _save(self, rows: list[dict[str, Any]]) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)   # atomic; a half-written queue never reaches a reader

    def enqueue(self, capability: str, payload: dict[str, Any], permissions: list[str],
                summary: str, requester: str = "moss") -> str:
        with self._lock:
            rows = self._load()
            approval = Approval(
                id=uuid.uuid4().hex[:12], created_at=_now(), capability=capability,
                payload=dict(payload), permissions=list(permissions), summary=summary,
                requester=requester,
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
            return [r for r in self._load() if r["status"] == "pending"]

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return list(reversed(self._load()))[:limit]

    def resolve(self, approval_id: str, status: str,
                result: dict[str, Any] | None = None) -> bool:
        """Mark a still-pending action approved or denied. Returns False if it was
        already resolved (or gone) - so a double tap can't run something twice."""
        with self._lock:
            rows = self._load()
            for row in rows:
                if row["id"] == approval_id and row["status"] == "pending":
                    row["status"] = status
                    row["resolved_at"] = _now()
                    if result is not None:
                        row["result"] = result
                    self._save(rows)
                    return True
        return False
