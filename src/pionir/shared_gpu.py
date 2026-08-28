"""Cooperative cross-process GPU exclusion for Pionir-compatible agents."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO


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
