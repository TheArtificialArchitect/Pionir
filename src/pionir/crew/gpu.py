"""The crew YIELDS to Pionir's GPU lock. It never takes it.

The card is 12 GB. Moss (Galatea, a separate process) runs the same
gemma3:12b the crew does; Ollama loads it once and both share it, so the crew
and Moss are not rivals for the card and neither needs the lock to use the
model. The lock (``pionir.shared_gpu``) is held only for EXCLUSIVE use of the
card - Pionir handing Daedalus a coding job that sidelines gemma and loads a
30B coder. If the crew acquired the lock around each of its own calls, Moss
(who yields to any holder) would stand down on every crew call, and the
scheduler's non-blocking ``try_acquire`` for Daedalus could fail in the
instant the crew held it. So the crew only READS the lock: before a model call
it asks whether anyone holds it, and if so it waits, polling, until it frees.

The probe is Moss's (``C:\\src\\Galatea\\galatea\\gpu.py``):

- **Windows**: Pionir locks byte 0 with ``msvcrt.locking``, and Windows
  byte-range locks are *mandatory* - a read of byte 0 through any other handle
  is refused while it is held, even from the same process. So the probe is a
  one-byte read: refused means held. It takes no lock, so it can never make
  the scheduler's ``try_acquire`` fail.
- **POSIX**: ``flock`` is advisory, so a read cannot tell; the probe takes a
  shared non-blocking lock and drops it at once. That has a tiny race with
  ``try_acquire``; it is a fallback for a machine the crew does not live on.

One deliberate difference from Moss: she never yields to her own pid. The crew
may run inside Pionir's own process, where the scheduler holding the lock for
Daedalus has the crew's pid - and since the crew never takes the lock, every
holder is another owner. So the crew yields to any holder at all.

There is no latch. The OS releases the lock if its holder dies, and the moment
it is free the crew resumes: the lock itself is the way back.
"""
from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

from ..shared_gpu import SharedGpuLock
from .log import log

Probe = Callable[[], "dict | None"]


def is_held(path: Path) -> bool:
    """True while some process holds the lock. Never takes it (on Windows)."""
    try:
        if not path.exists():
            return False
    except OSError as exc:
        log.warning("gpu: cannot stat %s (%s); treating the card as free", path, exc)
        return False
    if os.name == "nt":
        try:
            with open(path, "rb") as stream:
                stream.read(1)
        except PermissionError:
            return True            # byte 0 is locked: the lease is held
        except OSError as exc:
            # Unreadable for another reason: fail open, never pause forever - but say so.
            log.warning("gpu: cannot read %s (%s); treating the card as free", path, exc)
            return False
        return False
    try:
        import fcntl
        with open(path, "rb") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        log.warning("gpu: cannot probe %s (%s); treating the card as free", path, exc)
        return False
    return False


def who(holder: dict | None) -> str:
    """A name to say: "Daedalus" from purpose "daedalus: qwen3-coder:30b"."""
    holder = holder or {}
    purpose = str(holder.get("purpose") or "")
    if ": " in purpose:
        name = purpose.split(": ", 1)[0].strip()
        if name:
            return name[:1].upper() + name[1:]
    owner = str(holder.get("owner") or "").strip()
    if owner and owner.lower() != "pionir":
        return owner[:1].upper() + owner[1:]
    return "another tenant"


def lock_probe(path: Path) -> Probe:
    """The real reader: the holder's record while the lock is held, None while free.
    The record is read through Pionir's own ``SharedGpuLock.holder`` (from byte 1,
    since byte 0 is the locked one); an unreadable record still means held."""
    reader = SharedGpuLock(path)

    def probe() -> dict | None:
        if not is_held(path):
            return None
        return reader.holder() or {}

    return probe


class CardWatch:
    """The crew's view of the lock, and its record of standing down.

    ``yields`` counts spells (one per stretch of standing down, not per poll),
    ``yield_seconds`` the total time spent waiting, and ``yielding_to`` is the
    current holder's record, or None while the card is free to use.
    """

    def __init__(self, path: Path, *, probe: Probe | None = None,
                 poll_seconds: float = 2.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.path = path
        self._probe = probe or lock_probe(path)
        self.poll_seconds = poll_seconds
        self._clock = clock
        self.yields = 0
        self.yield_seconds = 0.0
        self.probe_errors = 0
        self.yielding_to: dict | None = None

    def holder(self) -> dict | None:
        """Who holds the card right now, or None if it is free. Never raises: a
        probe that breaks must not become a pause that never ends - it is counted
        and logged, and the card is treated as free."""
        try:
            return self._probe()
        except Exception as exc:  # noqa: BLE001 - counted and logged, never silent
            self.probe_errors += 1
            log.warning("gpu: probe of %s failed (%s: %s); treating the card as free",
                        self.path, type(exc).__name__, exc)
            return None

    def wait_until_free(self, stop: threading.Event, *, waiter: str = "crew") -> bool:
        """Return True as soon as the card is free (at once, if it already is);
        False only if ``stop`` is set while standing down. Makes no model call and
        takes no lock - it only looks."""
        holder = self.holder()
        if holder is None:
            return True
        self.yields += 1
        started = self._clock()
        self.yielding_to = holder
        log.info("gpu: %s standing down: %s has the card (%s)",
                 waiter, who(holder), holder.get("purpose") or "purpose unknown")
        try:
            while not stop.wait(self.poll_seconds):
                holder = self.holder()
                if holder is None:
                    log.info("gpu: the card is free again (%s is done); %s resuming after %.0f s",
                             who(self.yielding_to), waiter, self._clock() - started)
                    return True
                self.yielding_to = holder
            log.info("gpu: %s stopped while standing down for %s", waiter, who(self.yielding_to))
            return False
        finally:
            self.yield_seconds += self._clock() - started
            self.yielding_to = None

    def snapshot(self) -> dict:
        return {"yielding": self.yielding_to is not None,
                "holder": who(self.yielding_to) if self.yielding_to is not None else None,
                "yields": self.yields, "yield_seconds": round(self.yield_seconds, 1),
                "probe_errors": self.probe_errors, "lock": str(self.path)}
