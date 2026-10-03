"""The only things a script may stand on: passages from sources the niche names.

A passage pack is a local JSON file (slice 1 fetches nothing from the network; a later slice
fills packs from the allowlisted hosts and Ian reads them before a script is written)::

    {"topic": "...", "passages": [
        {"id": "p1", "source_id": "wikipedia", "url": "https://en.wikipedia.org/wiki/X",
         "title": "X", "text": "...", "credit": "optional attribution line"}]}

A passage is refused at load unless its source id belongs to the niche AND its URL's host is
one of that source's hosts, so "cite loc, link to a blog" cannot happen. Images work the same
way: an image ref is a local file plus the source and URL it came from, and its credit line is
printed in the video and in the description.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .niche import Niche

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
MAX_TEXT = 6000


class PassageError(ValueError):
    """A passage pack that cannot be used, with the reason."""


@dataclass(frozen=True, slots=True)
class Passage:
    id: str
    source_id: str
    url: str
    title: str
    text: str
    credit: str


@dataclass(frozen=True, slots=True)
class ImageRef:
    id: str
    path: Path
    source_id: str
    url: str
    credit: str


@dataclass(frozen=True, slots=True)
class Pack:
    topic: str
    passages: tuple[Passage, ...]
    images: tuple[ImageRef, ...]

    def passage(self, passage_id: str) -> Passage | None:
        return next((p for p in self.passages if p.id == passage_id), None)

    def image(self, image_id: str) -> ImageRef | None:
        return next((i for i in self.images if i.id == image_id), None)


def host_allowed(url: str, hosts: tuple[str, ...]) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and host in hosts and not parts.username


def _str(raw: Mapping[str, Any], key: str, where: str, limit: int) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PassageError(f"{where}: {key!r} must be a non-empty string")
    if len(value) > limit:
        raise PassageError(f"{where}: {key!r} is longer than {limit} characters")
    return value.strip()


def load_pack(path: Path | str, niche: Niche) -> Pack:
    base = Path(path).parent
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PassageError(f"cannot read passages from {path}: {error}") from error
    if not isinstance(document, Mapping):
        raise PassageError(f"{path}: expected an object")
    topic = _str(document, "topic", str(path), 160)
    passages: list[Passage] = []
    for n, raw in enumerate(document.get("passages") or []):
        where = f"passage #{n + 1}"
        if not isinstance(raw, Mapping):
            raise PassageError(f"{where}: must be an object")
        pid = _str(raw, "id", where, 40)
        if not _ID.match(pid):
            raise PassageError(f"{where}: id {pid!r} has characters outside A-Za-z0-9_.-")
        where = f"passage {pid!r}"
        source = niche.source(_str(raw, "source_id", where, 40))
        if source is None:
            raise PassageError(f"{where}: source {raw.get('source_id')!r} is not allowlisted "
                               f"for niche {niche.id!r}")
        url = _str(raw, "url", where, 500)
        if not host_allowed(url, source.hosts):
            raise PassageError(f"{where}: {url!r} is not an https URL on {source.name}'s "
                               f"hosts {list(source.hosts)}")
        credit = raw.get("credit") or f"{source.name} ({source.license})"
        passages.append(Passage(pid, source.id, url, _str(raw, "title", where, 200),
                                _str(raw, "text", where, MAX_TEXT), str(credit)[:300]))
    if len({p.id for p in passages}) != len(passages):
        raise PassageError(f"{path}: duplicate passage ids")
    if not passages:
        raise PassageError(f"{path}: a pack with no passages gives a script nothing to stand on")
    images: list[ImageRef] = []
    for n, raw in enumerate(document.get("images") or []):
        where = f"image #{n + 1}"
        if not isinstance(raw, Mapping):
            raise PassageError(f"{where}: must be an object")
        iid = _str(raw, "id", where, 40)
        if not _ID.match(iid):
            raise PassageError(f"{where}: id {iid!r} has characters outside A-Za-z0-9_.-")
        source = niche.source(_str(raw, "source_id", where, 40))
        if source is None:
            raise PassageError(f"image {iid!r}: source is not allowlisted for niche {niche.id!r}")
        url = _str(raw, "url", where, 500)
        if not host_allowed(url, source.hosts):
            raise PassageError(f"image {iid!r}: {url!r} is not on {source.name}'s hosts")
        file = (base / _str(raw, "file", where, 300)).resolve()
        if not file.is_file():
            raise PassageError(f"image {iid!r}: file {file} does not exist")
        images.append(ImageRef(iid, file, source.id, url,
                               _str(raw, "credit", f"image {iid!r}", 300)))
    if len({i.id for i in images}) != len(images):
        raise PassageError(f"{path}: duplicate image ids")
    return Pack(topic, tuple(passages), tuple(images))
