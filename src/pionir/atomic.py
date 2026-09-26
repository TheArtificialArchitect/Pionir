"""Atomic file replacement that survives Windows.

Every store in Pionir writes a temp file and swaps it into place, so a reader never sees a
half-written file. On Windows that swap fails with PermissionError while ANOTHER process holds
the destination open for a moment - an antivirus scanner or the search indexer looking at a file
that was just written. In-process locks cannot prevent that. Found by an approval whose job
finished while the queue file was held: the swap raised inside the job's finish step, the job
never completed, and the approval stayed "running". So every swap is retried briefly.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

ATTEMPTS = 40            # x BACKOFF = ~2 s, far longer than a scanner holds a small file
BACKOFF_SECONDS = 0.05


def replace(tmp: str | os.PathLike[str], dest: str | os.PathLike[str]) -> None:
    """``os.replace(tmp, dest)``, retried on PermissionError; the last error is raised."""
    for attempt in range(ATTEMPTS):
        try:
            os.replace(tmp, dest)
            return
        except PermissionError:
            if attempt == ATTEMPTS - 1:
                raise
            time.sleep(BACKOFF_SECONDS)


def write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` atomically (temp file + retried swap)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding=encoding)
    replace(tmp, path)
