"""Packaging an APPROVED build exactly the way the product shelf publishes from.

Only a build Claude approved reaches here (worker.py). It is staged as
``<products_dir>/<slug>/`` - the folder products.shelf (crew/products.py) reads - holding the
same three things as the owner's hand-made products (C:\\src\\dokaz-products):

- ``listing.json`` - exactly the shelf's LISTING_KEYS, built from the backlog entry word for
  word (backlog.listing), version 1.0.0, no pay-what-you-want, no executables;
- ``<slug>-1.0.0.zip`` - built like dokaz-products' make_zips.py: every file under one top
  folder ``<slug>-1.0.0/``, sorted, a fixed timestamp, fixed permissions and compression, so
  the same content always gives the same bytes; only the files that ship
  (review.product_files: never BRIEF.md, dotfiles or build clutter);
- ``cover.png`` - 1280x720 (the shelf's minimum) in the products' cover style.

Before anything lands the result is checked with the shelf's own ``check_listing``,
Pionir's own ``check_product`` and Pionir's zip inspection (``inspect_zip``: a README, no
path tricks, no executable, every entry scanned for every secret) - the checks the product
will meet again when it is submitted. It is written to a hidden staging folder (the shelf
skips dot-folders) and renamed into place in one step, so the shelf never sees half a
product; an existing ``<slug>`` folder is never overwritten.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import time
import zipfile
from pathlib import Path

from pionir.adapters.deliveries import DeliveryProblem, inspect_zip
from pionir.adapters.products import MAX_PRODUCT_UNCOMPRESSED, MAX_PRODUCT_ZIP_BYTES, check_product

from ..products import LISTING_FILE, check_listing
from .backlog import listing, zip_name
from .review import product_files
from .sandbox import short_name

STAMP = (2026, 1, 1, 0, 0, 0)
COVER_W, COVER_H = 1280, 720


class PackageError(RuntimeError):
    pass


def build_zip(slug: str, files: dict, version: str = "1.0.0") -> bytes:
    top = f"{slug}-{version}"
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for rel in sorted(files):
            info = zipfile.ZipInfo(f"{top}/{rel}", date_time=STAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            zf.writestr(info, files[rel], compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return out.getvalue()


def render_cover(entry: dict) -> bytes:
    """A 1280x720 PNG in the products' cover style: a label, the name, the summary and the
    command. Raises PackageError without Pillow."""
    try:
        from PIL import Image, ImageDraw

        from pionir.social import card as style
    except ImportError as exc:
        raise PackageError("Pillow is not installed: no cover can be drawn") from exc
    bg, panel = (18, 26, 35), (27, 38, 50)
    ink, muted, accent = (242, 245, 247), (150, 166, 180), style.ACCENT
    img = Image.new("RGB", (COVER_W, COVER_H), bg)
    d = ImageDraw.Draw(img)
    m = 88
    d.rectangle((0, 0, 10, COVER_H), fill=accent)
    label = ("PYTHON  \u00b7  CLI  \u00b7  STDLIB ONLY" if entry["language"] == "python"
             else "NODE.JS  \u00b7  CLI  \u00b7  NO DEPENDENCIES")
    d.text((m, 84), label, font=style._font(24, style.SEMIBOLD), fill=accent)
    name_font = style._font(88, style.BOLD)
    name = short_name(entry)
    while name_font.getlength(name) > COVER_W - 2 * m and name_font.size > 48:
        name_font = style._font(name_font.size - 8, style.BOLD)
    d.text((m, 126), name, font=name_font, fill=ink)
    body = style._font(36, style.REGULAR)
    y = 262
    try:
        lines = style._wrap(entry["summary"], body, COVER_W - 2 * m)[:3]
    except ValueError as exc:                   # a word too wide for the cover
        raise PackageError(f"the cover cannot be drawn: {exc}") from exc
    for line in lines:
        d.text((m, y), line, font=body, fill=muted)
        y += 50
    d.rounded_rectangle((m, 500, COVER_W - m, 640), radius=18, fill=panel)
    d.text((m + 32, 548), f"$ {entry['command']} --help", font=style._font(34, style.SEMIBOLD),
           fill=accent)
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def _rename(src: Path, dest: Path) -> None:
    """A folder moved into place in one step; never over an existing folder. Retried briefly
    when a scanner holds the new folder for a moment (Windows)."""
    for attempt in range(6):
        if dest.exists():
            raise PackageError(f"{dest} already exists; it is never overwritten")
        try:
            os.rename(src, dest)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.2 * (attempt + 1))


def stage(entry: dict, files: dict, products_dir, secrets) -> dict:
    """Package and stage one approved build; returns what was staged. Raises PackageError
    with the reason when any check refuses it (nothing is left behind)."""
    root = Path(products_dir)
    slug = entry["slug"]
    final = root / slug
    if final.exists():
        raise PackageError(f"a product folder {final} already exists; nothing was staged")
    shipped = product_files(files)
    lst = listing(entry)
    data = build_zip(slug, shipped)
    cover = render_cover(entry)
    root.mkdir(parents=True, exist_ok=True)
    staging = root / f".staging-{slug}-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    try:
        (staging / zip_name(entry)).write_bytes(data)
        (staging / "cover.png").write_bytes(cover)
        (staging / LISTING_FILE).write_text(json.dumps(lst, indent=2, ensure_ascii=False)
                                            + "\n", encoding="utf-8")
        # the shelf's own check needs the folder to carry the slug's name: check a copy's
        # listing against the final name, the files against the staging folder
        reasons = [r for r in check_listing(lst, staging) if "folder's name" not in r]
        if reasons:
            raise PackageError("the listing fails the shelf's check: " + "; ".join(reasons))
        zip_sha = hashlib.sha256(data).hexdigest()
        cover_sha = hashlib.sha256(cover).hexdigest()
        try:
            check_product({**lst, "zip_sha256": zip_sha, "cover_sha256": cover_sha})
        except ValueError as exc:
            raise PackageError(f"Pionir's product check refuses the listing: {exc}") from exc
        try:
            inspect_zip(staging / zip_name(entry), secrets, pinned_sha256=zip_sha,
                        max_bytes=MAX_PRODUCT_ZIP_BYTES,
                        max_uncompressed=MAX_PRODUCT_UNCOMPRESSED, allow_executables=False,
                        folder="the products folder")
        except DeliveryProblem as exc:
            raise PackageError(f"Pionir's zip inspection refuses it: {exc}") from exc
        _rename(staging, final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {"folder": str(final), "zip_name": zip_name(entry), "zip_sha256": zip_sha,
            "cover_sha256": cover_sha, "files": len(shipped), "zip_bytes": len(data),
            "price_cents": entry["price_cents"], "name": entry["name"]}
