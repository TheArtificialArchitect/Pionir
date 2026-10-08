"""The Builds worker's one hook into this division: an approved store build, staged.

Called by ``builds/worker.py`` ``_stage`` - ONLY for a build Claude approved - for an entry
whose ``product_type`` is a store's. It writes ``<root>/staged/<slug>/``:

- ``entry.json`` - the backlog entry exactly as built;
- ``files/<path>`` - what ships (``review.product_files``: never BRIEF.md, dotfiles or build
  clutter), every file scanned for every secret the owner keeps before anything is written.

Written to a hidden folder and renamed into place in one step, so the packager never sees
half a build; an existing ``<slug>`` folder is never overwritten. Raises with the reason
(the Builds worker shelves the product with it).
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from pionir.adapters.deliveries import scan_bytes

from ..builds.package import PackageError, _rename
from ..builds.review import product_files
from ..log import log
from . import paths


def stage_build(entry: dict, files: dict, builds_dir, secrets) -> dict:
    root = paths.root_for(builds_dir)
    if root is None:
        raise PackageError("no Builds folder, so no Marketplaces folder to stage into")
    slug = entry["slug"]
    final = paths.staged_dir(root, slug)
    if final.exists():
        raise PackageError(f"{final} already exists; it is never overwritten")
    shipped = product_files(files)
    for rel, data in sorted(shipped.items()):
        why = scan_bytes(rel, data, secrets)
        if why:
            raise PackageError(f"secrets found - {why}; nothing was staged")
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.parent / f".staging-{slug}-{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    try:
        for rel, data in shipped.items():
            path = tmp / "files" / Path(*rel.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (tmp / "entry.json").write_text(json.dumps(entry, indent=1, ensure_ascii=False) + "\n",
                                        encoding="utf-8")
        _rename(tmp, final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return {"folder": str(final), "files": len(shipped),
            "bytes": sum(len(d) for d in shipped.values()),
            "product_type": entry.get("product_type"), "name": entry["name"],
            "price_cents": 0}


def read_staged(root: Path, slug: str) -> tuple:
    """``(entry, {rel: bytes})`` of one staged build. Raises paths.Unreadable."""
    folder = paths.staged_dir(root, slug)
    try:
        entry = json.loads((folder / "entry.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise paths.Unreadable(f"{folder / 'entry.json'}: {exc}") from exc
    if not isinstance(entry, dict) or entry.get("slug") != slug:
        raise paths.Unreadable(f"{folder / 'entry.json'} is not this build's entry")
    base = folder / "files"
    out: dict = {}
    try:
        for path in sorted(base.rglob("*")):
            if path.is_symlink():
                raise paths.Unreadable(f"{path} is a link; a staged build holds plain files")
            if path.is_file():
                out[path.relative_to(base).as_posix()] = path.read_bytes()
    except OSError as exc:
        raise paths.Unreadable(f"{base}: {exc}") from exc
    return entry, out


def staged_slugs(root: Path) -> list:
    base = paths.staged_dir(root)
    try:
        return sorted(p.name for p in base.iterdir()
                      if p.is_dir() and not p.name.startswith(".") and not p.is_symlink()
                      and (p / "entry.json").is_file())
    except FileNotFoundError:
        return []
    except OSError as exc:
        log.warning("marketplaces: cannot list %s: %s", base, exc)
        return []
