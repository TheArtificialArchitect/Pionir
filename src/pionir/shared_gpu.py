"""Cooperative cross-process GPU exclusion for Pionir-compatible agents."""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Iterator

_log = logging.getLogger(__name__)


@contextmanager
def hold_file_lock(path: Path, *, timeout: float = 10.0, poll: float = 0.05) -> Iterator[None]:
    """Hold an OS-level exclusive lock on a sidecar file for the duration of the
    block, waiting up to ``timeout`` seconds for another process to let go.

    The lock lives on its own file (never on the data file): Windows byte-range
    locks are mandatory, so locking the data file itself would make a reader in
    another process fail with a permission error. Raises TimeoutError if the
    lock cannot be had in time - never blocks forever.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    try:
        if path.stat().st_size == 0:
            stream.write(b"\n")
            stream.flush()
        deadline = time.monotonic() + timeout
        while not _try_lock(stream):
            if time.monotonic() >= deadline:
                raise TimeoutError(f"could not lock {path} within {timeout:g}s")
            time.sleep(poll)
        try:
            yield
        finally:
            try:
                _unlock(stream)
            except OSError as error:  # already released with the handle; say so
                _log.warning("unlocking %s failed: %s", path, error)
    finally:
        stream.close()


def _try_lock(stream: BinaryIO) -> bool:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    import fcntl

    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(stream: BinaryIO) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@dataclass(slots=True)
class SharedGpuLease:
    """An OS-owned lock released automatically if the holding process exits."""

    path: Path
    _stream: BinaryIO
    _released: bool = False

    def release(self) -> None:
        if self._released:
            return
        try:
            _unlock(self._stream)
        finally:
            self._stream.close()
            self._released = True

    def __enter__(self) -> "SharedGpuLease":
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.release()


class SharedGpuLock:
    """Non-blocking lock shared by Pionir, Bryo, and future GPU specialists."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def holder(self) -> dict[str, Any] | None:
        """Who holds the lock, as the holder wrote it (owner/pid/purpose/acquired_at),
        or None if the file is empty or unreadable. Informational: the file's
        contents are the last writer's claim, the OS lock is the truth."""

        try:
            raw = self.path.read_bytes()
        except OSError:
            # Windows byte-range locks are mandatory: the holder locks byte 0,
            # so a whole-file read is refused while it is held. The record
            # always starts with "{", so read from byte 1 and put it back.
            try:
                with self.path.open("rb") as stream:
                    stream.seek(1)
                    raw = b"{" + stream.read()
            except OSError:
                return None
        try:
            text = raw.decode("utf-8").strip()
            document = json.loads(text.splitlines()[0]) if text else None
        except (ValueError, IndexError):
            return None
        return document if isinstance(document, dict) else None

    def describe_holder(self) -> str:
        holder = self.holder()
        if not holder:
            return "holder unknown"
        return (
            f"owner={holder.get('owner', '?')} pid={holder.get('pid', '?')} "
            f"purpose={holder.get('purpose', '?')} since={holder.get('acquired_at', '?')}"
        )

    def try_acquire(self, *, owner: str, purpose: str) -> SharedGpuLease | None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass

        stream = self.path.open("a+b")
        try:
            if self.path.stat().st_size == 0:
                stream.write(b"\n")
                stream.flush()
            if not _try_lock(stream):
                stream.close()
                _log.info("GPU lock %s refused to %s (%s): %s",
                          self.path, owner, purpose, self.describe_holder())
                return None
            metadata = {
                "owner": owner,
                "pid": os.getpid(),
                "purpose": purpose,
                "acquired_at": datetime.now(UTC).isoformat(),
            }
            stream.seek(0)
            stream.truncate()
            stream.write(json.dumps(metadata, sort_keys=True).encode("utf-8") + b"\n")
            stream.flush()
            try:
                os.fsync(stream.fileno())
                self.path.chmod(0o600)
            except OSError:
                pass
            return SharedGpuLease(self.path, stream)
        except BaseException:
            if not stream.closed:
                stream.close()
            raise
