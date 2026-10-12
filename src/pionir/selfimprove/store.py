"""Where each self-improvement candidate's files live, and who writes which.

One folder per candidate, ``<improve_dir>/<id>/`` (``~/.pionir/improve`` by default), and ONE
writer per file, so two processes never race on one:

- ``patch.diff`` and ``card.json`` - the crew's ``builds.improve`` worker, before it asks Ian;
- ``outcome.json`` - Pionir's ``improve.apply``, after Ian's yes (applied, stale or reverted);
- ``undone.json`` - ``python -m pionir improve undo <id>``, run by Ian.

The candidate ledger itself (every state a candidate passed through) is the worker's own record,
``<crew state>/workers/builds.improve.json``; only the worker writes it, and it folds the two
outcome files in on its next run. ``python -m pionir improve status`` reads all of it.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from pionir import atomic

CANDIDATE_ID = re.compile(r"imp-\d{8}-[0-9a-f]{4,8}")
PATCH = "patch.diff"
CARD = "card.json"
OUTCOME = "outcome.json"
UNDONE = "undone.json"
LEDGER_NAME = "builds.improve.json"
STATES = ("detected", "writing", "verifying", "reviewing", "awaiting_owner", "applied",
          "denied", "shelved", "stale", "undone")
OPEN = frozenset({"detected", "writing", "verifying", "reviewing", "awaiting_owner"})


class StoreError(ValueError):
    pass


def check_id(cid) -> str:
    if not isinstance(cid, str) or not CANDIDATE_ID.fullmatch(cid):
        raise StoreError(f"{str(cid)[:40]!r} is not a candidate id like imp-20261012-ab12")
    return cid


def folder(improve_dir, cid: str) -> Path:
    return Path(improve_dir) / check_id(cid)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1, sort_keys=True), encoding="utf-8")
    atomic.replace(tmp, path)


def read_json(path: Path) -> dict | None:
    """The object at ``path``, or None when there is none or it cannot be read."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def write_patch(improve_dir, cid: str, patch: str, card: dict) -> str:
    """The patch and its card, written for Pionir; returns the patch's sha256 (what Ian's
    approval names)."""
    where = folder(improve_dir, cid)
    where.mkdir(parents=True, exist_ok=True)
    data = patch.encode("utf-8")
    tmp = where / (PATCH + ".tmp")
    tmp.write_bytes(data)
    atomic.replace(tmp, where / PATCH)
    digest = sha256(data)
    write_json(where / CARD, {**card, "id": cid, "patch_sha256": digest})
    return digest


def read_patch(improve_dir, cid: str) -> tuple:
    """``(patch text, sha256, card)``; StoreError when it is missing or unreadable."""
    where = folder(improve_dir, cid)
    try:
        data = (where / PATCH).read_bytes()
    except OSError as exc:
        raise StoreError(f"no patch for {cid} in {where}") from exc
    card = read_json(where / CARD)
    if card is None or card.get("id") != cid:
        raise StoreError(f"no readable card for {cid} in {where}")
    return data.decode("utf-8", "replace"), sha256(data), card


def ledger_path(crew_state_dir) -> Path:
    return Path(crew_state_dir) / "workers" / LEDGER_NAME
