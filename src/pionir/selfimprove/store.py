"""Where each self-improvement candidate's files live, and who writes which.

One folder per candidate, ``<improve_dir>/<id>/`` (``~/.pionir/improve`` by default: the owner's
profile, which the sandbox user cannot read or write), and ONE writer per file, so two
processes never race on one:

- ``patch.diff`` - the crew's ``builds.improve`` worker, ONCE: the patch is FROZEN here the
  moment it passes the guard, and from then on everything - the verification, Claude's review,
  the card and the apply - uses exactly these bytes (``read_frozen`` checks their sha256 against
  the one recorded when it was frozen). Nothing is ever re-read from the sandbox repo, which
  code under test could have rewritten;
- ``card.json`` - the same worker, before it asks Ian: the evidence, the verdict, and the
  ``digest`` his approval names - one sha256 over the patch's bytes, the base commit and the
  targeted tests, so none of the three can change after the card;
- ``outcome.json`` - Pionir's ``improve.apply``, after Ian's yes (applied, stale or reverted);
- ``undone.json`` - ``python -m pionir improve undo <id>``, run by Ian.

The candidate ledger itself is the worker's own record (``<crew state>/workers/
builds.improve.json``); only the worker writes it. ``python -m pionir improve status`` reads it.
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


def digest(patch: bytes, base: str, tests) -> str:
    """What Ian approves: the patch's bytes, the commit it applies to and the tests that are
    re-run after it - one sha256 over all three."""
    doc = {"patch_sha256": sha256(patch), "base": str(base),
           "tests": sorted(str(t) for t in tests or [])}
    return sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8"))


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


def freeze(improve_dir, cid: str, patch: str) -> str:
    """Freeze the patch: its bytes written once, its sha256 returned (the worker records it)."""
    where = folder(improve_dir, cid)
    where.mkdir(parents=True, exist_ok=True)
    data = patch.encode("utf-8")
    tmp = where / (PATCH + ".tmp")
    tmp.write_bytes(data)
    atomic.replace(tmp, where / PATCH)
    return sha256(data)


def read_frozen(improve_dir, cid: str, want_sha: str) -> bytes:
    """The frozen patch's bytes - StoreError unless they hash to ``want_sha``."""
    try:
        data = (folder(improve_dir, cid) / PATCH).read_bytes()
    except OSError as exc:
        raise StoreError(f"no frozen patch for {cid}") from exc
    if sha256(data) != want_sha:
        raise StoreError(f"the frozen patch of {cid} changed since it was frozen (sha256 "
                         "differs); it is never used")
    return data


def write_card(improve_dir, cid: str, patch: bytes, card: dict) -> str:
    """The card for Ian, naming the digest of exactly (patch, base, tests); returns it."""
    d = digest(patch, card["base"], card.get("tests"))
    write_json(folder(improve_dir, cid) / CARD,
               {**card, "id": cid, "patch_sha256": sha256(patch), "digest": d})
    return d


def read_candidate(improve_dir, cid: str) -> tuple:
    """``(patch bytes, card, digest)`` - read ONCE, the digest recomputed from those bytes and
    the card's own base and tests; StoreError when anything is missing or does not match."""
    where = folder(improve_dir, cid)
    try:
        data = (where / PATCH).read_bytes()
    except OSError as exc:
        raise StoreError(f"no patch for {cid} in {where}") from exc
    card = read_json(where / CARD)
    if card is None or card.get("id") != cid or not isinstance(card.get("base"), str):
        raise StoreError(f"no readable card for {cid} in {where}")
    d = digest(data, card["base"], card.get("tests"))
    if card.get("patch_sha256") != sha256(data) or card.get("digest") != d:
        raise StoreError(f"{cid}'s patch, base or tests changed after its card was written")
    return data, card, d


def ledger_path(crew_state_dir) -> Path:
    return Path(crew_state_dir) / "workers" / LEDGER_NAME
