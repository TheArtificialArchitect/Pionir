"""Tamper-evident, metadata-only audit storage."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from threading import Lock
from typing import Any

from .errors import AuditIntegrityError
from .runtime import AuditEvent


def _canonical(document: dict[str, Any]) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class JsonlAuditSink:
    """Append-only hash chain that deliberately excludes task payloads and output."""

    def __init__(self, path: Path, *, fsync: bool = True) -> None:
        self.path = path
        self._fsync = fsync
        self._lock = Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        self._sequence, self._last_hash = self.verify()

    def record(self, event: AuditEvent) -> None:
        with self._lock:
            body: dict[str, Any] = {
                "sequence": self._sequence + 1,
                "previous_sha256": self._last_hash,
                "event_type": event.event_type,
                "task_id": str(event.task_id),
                "agent_id": event.agent_id,
                "occurred_at": event.occurred_at.isoformat(),
                "detail": event.detail,
            }
            digest = hashlib.sha256(_canonical(body)).hexdigest()
            record = {**body, "sha256": digest}
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                stream.flush()
                if self._fsync:
                    os.fsync(stream.fileno())
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
            self._sequence = int(body["sequence"])
            self._last_hash = digest

    def verify(self) -> tuple[int, str]:
        """Return the final sequence/hash, or fail on truncation, edits, or reordering."""

        if not self.path.exists():
            return 0, "0" * 64
        sequence = 0
        previous = "0" * 64
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as error:
            raise AuditIntegrityError("audit ledger cannot be read") from error
        for line_number, line in enumerate(lines, start=1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise AuditIntegrityError(
                    f"audit ledger line {line_number} is invalid JSON"
                ) from error
            if not isinstance(record, dict):
                raise AuditIntegrityError(f"audit ledger line {line_number} is not an object")
            digest = record.pop("sha256", None)
            if record.get("sequence") != sequence + 1:
                raise AuditIntegrityError(
                    f"audit ledger sequence breaks at line {line_number}"
                )
            if record.get("previous_sha256") != previous:
                raise AuditIntegrityError(
                    f"audit ledger chain breaks at line {line_number}"
                )
            expected = hashlib.sha256(_canonical(record)).hexdigest()
            if digest != expected:
                raise AuditIntegrityError(
                    f"audit ledger digest fails at line {line_number}"
                )
            sequence += 1
            previous = expected
        return sequence, previous
