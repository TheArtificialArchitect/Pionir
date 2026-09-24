"""Logging and the lesion counter.

Every subsystem call in the tick goes through ``safe`` so one failure can
never take the crew down, and never disappears either: it is logged with a
traceback and counted per subsystem, and the snapshot shows the counts. A
silent failure would look exactly like a quiet agent; nothing here is silent.

The logger is ``pionir.crew``, a child of Pionir's own, so whatever handlers
Pionir installs see the crew too. ``setup`` adds a rotating file of the crew's
own only when a caller asks for one; importing this module configures nothing.
"""
from __future__ import annotations

import collections
import logging
import logging.handlers
import threading
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

LOG_MAX_BYTES = 8 * 1024 * 1024
LOG_BACKUPS = 1

log = logging.getLogger("pionir.crew")

_lock = threading.Lock()
lesions: collections.Counter[str] = collections.Counter()
recent_lesions: collections.deque = collections.deque(maxlen=50)

T = TypeVar("T")


def setup(path: Path, console: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(
        path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if console:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        log.addHandler(ch)


def check_source() -> list:
    """A control byte in the source is almost always a mangled escape: '\\b' meant as a regex
    word boundary arriving as 0x08 matches nothing and fails silently. Hearth found one in
    conversation.py on 2026-09-20 that made a rewrite do nothing while reading correctly."""
    bad = []
    here = Path(__file__).resolve().parent
    for path in sorted(here.rglob("*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("could not read %s for the source check: %s", path.name, exc)
            continue
        for ch, name in (("\x07", r"\a"), ("\x08", r"\b"), ("\x0b", r"\v"), ("\x0c", r"\f")):
            if ch in text:
                n = text.count(ch)
                bad.append(f"{path.name}: {n} literal {name} control byte(s)")
                log.error("MANGLED SOURCE %s: %d control byte(s) where %s was meant",
                          path.name, n, name)
    return bad


def lesion(where: str, exc: BaseException) -> None:
    """Record a failure: log it with a traceback and count it. Never swallow."""
    with _lock:
        lesions[where] += 1
        recent_lesions.append((time.time(), where, f"{type(exc).__name__}: {exc}"))
    log.error("lesion in %s: %s\n%s", where, exc, traceback.format_exc())


def safe(where: str, fn: Callable[[], T], default: Any = None) -> T | Any:
    """Run ``fn``; on any exception record a lesion and return ``default``."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - recorded, counted, shown; never silent
        lesion(where, exc)
        return default


def lesion_snapshot() -> dict:
    with _lock:
        return {
            "counts": dict(lesions),
            "recent": [
                {"t": t, "where": w, "error": e} for (t, w, e) in list(recent_lesions)[-10:]
            ],
        }
