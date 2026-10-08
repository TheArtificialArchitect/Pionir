"""Where the Marketplaces division keeps its files, and how they are read and written.

One root, shared by the crew (which writes candidates, specs, staged builds and listing
drafts) and Pionir (whose publish adapters read a listing's package from it):

- ``PIONIR_MARKETPLACES_DIR`` when set;
- else the sibling of the Builds folder (``<builds_dir>/../marketplaces``) on the crew's side -
  ``~/.pionir/marketplaces`` with the default ``~/.pionir/builds``, a temp folder in a test;
- else ``~/.pionir/marketplaces``.

Layout::

    candidates-<market>.json   the scout's scored candidates for one marketplace
    specs/<slug>.json          a spec handed to the Builds backlog (its marketplace fields)
    staged/<slug>/             a build Claude approved: entry.json + files/ (staging.py)
    listings/<slug>/           the listing draft: listing.json, package.zip, images
    chrome-items.json          {slug: item_id} - the owner's hand-made Chrome listings
    performance.json           what the watcher saw sell, for the scouts
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from pionir import atomic

ENV = "PIONIR_MARKETPLACES_DIR"
MARKETS = ("apify", "chrome", "shopify")
PRODUCT_TYPES = {"apify": "apify_actor", "chrome": "chrome_extension",
                 "shopify": "shopify_app"}
MARKET_OF = {v: k for k, v in PRODUCT_TYPES.items()}
CHROME_ITEMS = "chrome-items.json"
PERFORMANCE = "performance.json"


class Unreadable(ValueError):
    """A file this division keeps exists but cannot be read: never silently replaced."""


def default_root() -> Path:
    env = os.environ.get(ENV, "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".pionir" / "marketplaces"


def root_for(builds_dir) -> Path | None:
    """The crew's root: the environment's, else the Builds folder's sibling. None when the
    worker was given no Builds folder (it then cannot run)."""
    env = os.environ.get(ENV, "").strip()
    if env:
        return Path(env).expanduser()
    if builds_dir is None:
        return None
    return Path(builds_dir).parent / "marketplaces"


def read_json(path: Path, blank):
    """The document at ``path``; ``blank`` (a fresh copy) when there is none yet;
    ``Unreadable`` when it exists but is not JSON of blank's type."""
    if not path.exists():
        return json.loads(json.dumps(blank))
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Unreadable(f"{path}: {exc}") from exc
    if not isinstance(doc, type(blank)):
        raise Unreadable(f"{path} is not a JSON {type(blank).__name__}")
    return doc


def write_json(path: Path, doc) -> None:
    """Atomic: a reader sees the old document or the new one, never half of one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    atomic.replace(tmp, path)


def candidates_path(root: Path, market: str) -> Path:
    return Path(root) / f"candidates-{market}.json"


def spec_path(root: Path, slug: str) -> Path:
    return Path(root) / "specs" / f"{slug}.json"


def staged_dir(root: Path, slug: str | None = None) -> Path:
    base = Path(root) / "staged"
    return base if slug is None else base / slug


def listing_dir(root: Path, slug: str | None = None) -> Path:
    base = Path(root) / "listings"
    return base if slug is None else base / slug


def chrome_items(root: Path) -> dict:
    """The owner's hand-made Chrome listings, ``{slug: item_id}`` (32 a-p letters each).
    Written by the owner (or ``tools\\setup-chrome-webstore.ps1 -Slug .. -ItemId ..``)."""
    doc = read_json(Path(root) / CHROME_ITEMS, {})
    return {k: v for k, v in doc.items() if isinstance(k, str) and isinstance(v, str)}
