"""Tamper-evident, metadata-only audit storage."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Any, Iterator

from .errors import AuditIntegrityError
from .runtime import AuditEvent
from .shared_gpu import hold_file_lock

_log = logging.getLogger(__name__)
_GENESIS = "0" * 64
# How much of the file's end to read when looking for the last complete line.
# One event is ~300 bytes; the chunk grows if the tail is longer than this.
_TAIL_CHUNK = 64 * 1024


def _canonical(document: dict[str, Any]) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class JsonlAuditSink:
    """Append-only hash chain that deliberately excludes task payloads and output.

    Several processes append to the one ledger (the server, and any CLI command
    run while it is up), so the chain head is never trusted from memory: every
    ``record`` takes an OS file lock, re-reads the last complete line, and chains
    from that. Construction reads only the tail - a full verification belongs to
    doctor and ``audit-verify``, not to every command's start-up, where a broken
    line would otherwise refuse the whole estate until someone hand-edited it.
    """

    def __init__(self, path: Path, *, fsync: bool = True) -> None:
        self.path = path
        self._fsync = fsync
        self._lock = Lock()
        self._lock_path = path.with_name(path.name + ".lock")
        # verify() cache: (size, mtime_ns) -> (count, head). The dashboard polls
        # the ledger every few seconds; without this each poll re-hashed it all.
        self._verified: tuple[tuple[int, int], tuple[int, str]] | None = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        try:
            self._sequence, self._last_hash, truncated = self._tail()
        except AuditIntegrityError as error:
            # Reported, not latched: doctor and the next record() say exactly
            # what is wrong; every CLI command still starts.
            _log.warning("audit ledger tail unreadable at start-up: %s", error)
            self._sequence, self._last_hash = 0, _GENESIS
        else:
            if truncated:
                _log.warning(
                    "audit ledger %s ends in a partial line (a crash mid-write); "
                    "it will be healed on the next record", self.path,
                )

    @contextmanager
    def _held(self) -> Iterator[None]:
        """Thread lock plus the cross-process file lock, in that order."""
        with self._lock, hold_file_lock(self._lock_path):
            yield

    def _tail(self) -> tuple[int, str, int]:
        """The chain head from the last complete line: (sequence, sha256,
        truncated_bytes). ``truncated_bytes`` > 0 means the file ends in a
        partial line with no newline - a crash mid-write - which ``record``
        truncates under the lock before appending. Raises AuditIntegrityError if
        the last complete line is not a ledger record."""

        if not self.path.exists():
            return 0, _GENESIS, 0
        with self.path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            if size == 0:
                return 0, _GENESIS, 0
            chunk = min(size, _TAIL_CHUNK)
            data = b""
            while True:
                stream.seek(size - chunk)
                data = stream.read(chunk)
                # The last complete line needs its terminating newline AND the
                # one before it (or the file start) inside the chunk.
                complete = data if data.endswith(b"\n") else data[: data.rfind(b"\n") + 1]
                if complete.count(b"\n") >= 2 or chunk >= size:
                    break
                chunk = min(size, chunk * 4)
        truncated = len(data) - len(complete)
        last = complete.rstrip(b"\n").rsplit(b"\n", 1)[-1] if complete else b""
        if not last.strip():
            # Nothing but a partial line (or blank lines): effectively empty.
            return 0, _GENESIS, truncated
        try:
            record = json.loads(last.decode("utf-8"))
            sequence = int(record["sequence"])
            digest = str(record["sha256"])
        except (ValueError, KeyError, TypeError, UnicodeDecodeError) as error:
            raise AuditIntegrityError(
                "audit ledger's last complete line is not a ledger record"
            ) from error
        if len(digest) != 64:
            raise AuditIntegrityError("audit ledger's last record has no valid sha256")
        return sequence, digest, truncated

    def _resync(self) -> None:
        """Under the lock: re-read the head from disk and heal a partial tail."""
        self._sequence, self._last_hash, truncated = self._tail()
        if truncated:
            size = self.path.stat().st_size
            _log.warning(
                "audit ledger %s: truncating %d bytes of partial line before appending",
                self.path, truncated,
            )
            with self.path.open("r+b") as stream:
                stream.truncate(size - truncated)
                stream.flush()
                if self._fsync:
                    os.fsync(stream.fileno())

    def record(self, event: AuditEvent) -> None:
        with self._held():
            self._resync()
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
            self._verified = None  # the file changed; the cached verdict is stale

    def _stat_key(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return stat.st_size, stat.st_mtime_ns

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """The last ``limit`` ledger events, newest first, for a live view.

        Read-only and best-effort: the dashboard polls this often and a display
        must never take the ledger's integrity down, so a read error yields an
        empty list rather than raising - ``verify()`` is the call that judges
        integrity, not this one. Events are already metadata-only by design.
        """

        if limit <= 0 or not self.path.exists():
            return []
        try:
            with self._held():
                lines = self._tail_lines(limit)
        except (OSError, UnicodeDecodeError, TimeoutError) as error:
            _log.warning("audit recent() could not read the ledger: %s", error)
            return []
        events: list[dict[str, Any]] = []
        for line in reversed(lines):
            if len(events) >= limit:
                break
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                events.append(record)
        return events

    def _tail_lines(self, limit: int) -> list[str]:
        """The last ``limit`` complete lines, reading from the end - never the
        whole file for a 60-event dashboard view of a year-long ledger."""
        with self.path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            chunk = min(size, _TAIL_CHUNK)
            while True:
                stream.seek(size - chunk)
                data = stream.read(chunk)
                if data.count(b"\n") > limit or chunk >= size:
                    break
                chunk = min(size, chunk * 4)
        if not data.endswith(b"\n"):
            data = data[: data.rfind(b"\n") + 1]  # drop a partial tail line
        lines = data.decode("utf-8", errors="replace").splitlines()
        if chunk < size:
            lines = lines[1:]  # the first line of a mid-file chunk is partial
        return lines[-limit:]

    def verify(self) -> tuple[int, str]:
        """Return the final sequence/hash, or fail on truncation, edits, or reordering.

        Full verification - every line re-hashed - but cached against the file's
        (size, mtime) so a dashboard poll costs a stat, not a re-read, and taken
        under the same lock as ``record`` so it never reads a half-written line.
        A final line with no newline is reported as a truncated tail: a crash
        mid-write, healed by the next ``record``.
        """

        if not self.path.exists():
            return 0, _GENESIS
        key = self._stat_key()
        if self._verified is not None and key is not None and self._verified[0] == key:
            return self._verified[1]
        with self._held():
            key = self._stat_key()
            verdict = self._verify_all()
            if key is not None:
                self._verified = (key, verdict)
            return verdict

    def _verify_all(self) -> tuple[int, str]:
        sequence = 0
        previous = _GENESIS
        try:
            raw = self.path.read_bytes()
        except OSError as error:
            raise AuditIntegrityError("audit ledger cannot be read") from error
        truncated_tail = bool(raw) and not raw.endswith(b"\n")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise AuditIntegrityError("audit ledger cannot be read") from error
        lines = text.splitlines()
        if truncated_tail:
            lines = lines[:-1]
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
        if truncated_tail:
            raise AuditIntegrityError(
                f"audit ledger line {len(lines) + 1} is a partial line with no newline "
                "(a crash mid-write); the next recorded event heals it"
            )
        return sequence, previous
