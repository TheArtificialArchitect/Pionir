"""What the Etsy workers share: the stage folder, their records, the daily caps, and the
follow-up of jobs Pionir parked for the owner.

The stage folder is ``~/.pionir/etsy`` (``PIONIR_ETSY_DIR`` overrides, as it does for
Pionir's adapters, so both sides read the same files; a catalogue ``stage_dir`` param wins
over both). Digital listings stage under ``digital/<slug>/``, print-on-demand designs under
``pod/<slug>/``.
"""
from __future__ import annotations

import hashlib
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from ..blog import _Unreadable, read_record, record_path, save_record
from ..hands import outcome_of
from ..log import log

SCOUT_FILE = "etsy-demand.json"
SCOUT_STALE_AFTER = 3 * 86400.0       # a ranking older than this steers nothing
FORGOTTEN_AFTER = 14 * 86400.0
RETRY_UNREACHABLE = 3


def etsy_dir(param: str | None = None) -> Path:
    if param:
        return Path(param).expanduser()
    env = (os.environ.get("PIONIR_ETSY_DIR") or "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".pionir" / "etsy"


def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), UTC).strftime("%Y-%m-%d")


def slug_for(prefix: str, keyword: str, now: float) -> str:
    words = re.sub(r"[^a-z0-9]+", "-", keyword.lower()).strip("-")[:36].strip("-")
    tail = hashlib.sha256(f"{keyword}|{now}".encode()).hexdigest()[:6]
    if words == prefix or words.startswith(prefix + "-"):
        prefix = ""   # "budget" + "budget tracker" was staged as budget-budget-tracker-...
    return "-".join(p for p in (prefix, words, tail) if p)[:60].strip("-")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_staged(folder: Path, files: dict[str, bytes]) -> None:
    """Write each file into a fresh stage folder. A folder that already exists is refused:
    staged files are never overwritten (an approval card pins their SHA-256)."""
    folder.mkdir(parents=True, exist_ok=False)
    for name, data in files.items():
        (folder / name).write_bytes(data)


class Record:
    """A worker's own small record: ``items`` (each thing it made, and what became of it),
    ``counts``, and anything else it keeps. Unreadable is an error, never a fresh start - a
    fresh start could make or list something twice."""

    def __init__(self, state_dir: Path, worker_id: str, blank: dict) -> None:
        self.path = record_path(state_dir, worker_id)
        doc = read_record(state_dir, worker_id)
        self.doc = {**blank, **(doc or {})}

    def save(self) -> None:
        save_record(self.path, self.doc)

    def count(self, what: str, n: int = 1) -> None:
        c = self.doc.setdefault("counts", {})
        c[what] = int(c.get(what, 0)) + n

    def made_today(self, now: float) -> int:
        today = utc_day(now)
        return sum(1 for i in self.doc.get("items") or []
                   if isinstance(i, dict) and i.get("submitted_at")
                   and utc_day(i["submitted_at"]) == today)


Unreadable = _Unreadable


def follow(ctx, item: dict, capability: str) -> tuple[str, object]:
    """What became of a job Pionir parked: ("waiting", None), ("approved", JobOutcome),
    ("denied", reason), ("failed", reason) or ("unknown", reason)."""
    if ctx.approval is None or not item.get("approval_id"):
        return "waiting", None
    got = ctx.approval(item["approval_id"]) or {}
    state = got.get("status")
    item["checked_at"] = ctx.now
    if state in ("pending", "running", "unreachable"):
        return "waiting", None
    if state == "denied":
        return "denied", f"the owner did not approve it ({got.get('reason') or 'denied'})"
    if state == "approved":
        out = outcome_of(capability, got.get("result"))
        if out.ran:
            return "approved", out
        return "failed", out.error or f"Pionir said {out.status}"
    if state == "approved_failed":
        out = outcome_of(capability, got.get("result"))
        return "failed", out.error or "it failed after approval"
    if state == "unknown":
        if ctx.now - float(item.get("submitted_at") or ctx.now) > FORGOTTEN_AFTER:
            return "unknown", "Pionir no longer lists this approval"
        return "waiting", None
    log.warning("etsy: approval %s has a status nobody knows (%r); still waiting",
                item.get("approval_id"), state)
    return "waiting", None


def read_scout(state_dir: Path, now: float) -> list | None:
    """The scout's ranking, best first, or None when there is none or it is stale."""
    path = Path(state_dir) / SCOUT_FILE
    try:
        import json
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("etsy: the scout's ranking %s is unreadable (%s)", path, exc)
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("ranking"), list):
        return None
    if now - float(doc.get("made_at") or 0) > SCOUT_STALE_AFTER:
        return None
    return [r for r in doc["ranking"] if isinstance(r, dict)]


def ninety_nine(cents: int) -> int:
    """A price ending in 99 cents, at or just under ``cents`` (never under 1.99)."""
    return max(199, (int(cents) // 100) * 100 - 1 if int(cents) % 100 != 99 else int(cents))
