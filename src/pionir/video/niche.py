"""A niche is a config entry, and a bad entry is refused at load, not at render.

Picking a channel's subject is an edit to ``niches.json`` (or a file named by
``PIONIR_VIDEO_NICHES``). Everything a niche decides - which sources a script may stand on,
how long a video runs, how often one is made, which scene types it mixes - is data checked
here. The rules that protect the channel are code that a config line cannot waive:

* a niche marked ``example`` is never ``live`` (a placeholder for a copy, never a channel),
* a cadence faster than one a week is refused (a flood teaches the owner to stop reading and
  is what YouTube's mass-produced-content policy looks for),
* every source names the hosts it may be quoted from, so a passage from anywhere else is
  refused no matter what the model cites.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(__file__).with_name("niches.json")
MIN_CADENCE_DAYS = 7
KINDS = ("history", "explainer", "tutorial")
# Scene types the renderer can draw today. A niche may only mix what exists.
SCENE_TYPES = ("image", "card", "timeline", "run")

_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{1,40}$")
_VOICE = re.compile(r"^[abefhijpz][fm]_[a-z]{2,20}$")
_HOST = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")
_KEYS = {"id", "example", "live", "title", "kind", "voice", "length_minutes",
         "cadence_days", "series", "sources", "scene_mix"}
_SOURCE_KEYS = {"id", "name", "hosts", "license"}


class NicheError(ValueError):
    """A niche entry that cannot be used, with the reason."""


@dataclass(frozen=True, slots=True)
class Source:
    id: str
    name: str
    hosts: tuple[str, ...]
    license: str


@dataclass(frozen=True, slots=True)
class Niche:
    id: str
    title: str
    kind: str
    voice: str
    length_minutes: tuple[int, int]
    cadence_days: int
    series: tuple[str, ...]
    sources: tuple[Source, ...]
    scene_mix: Mapping[str, float]
    example: bool
    live: bool

    def source(self, source_id: str) -> Source | None:
        return next((s for s in self.sources if s.id == source_id), None)


def _text(entry: Mapping[str, Any], key: str, where: str, limit: int = 120) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise NicheError(f"{where}: {key!r} must be a non-empty string")
    if len(value) > limit:
        raise NicheError(f"{where}: {key!r} is longer than {limit} characters")
    return value.strip()


def _whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _source(raw: Any, where: str) -> Source:
    if not isinstance(raw, Mapping):
        raise NicheError(f"{where}: a source must be an object")
    stray = set(raw) - _SOURCE_KEYS
    if stray:
        raise NicheError(f"{where}: unknown source keys {sorted(stray)}")
    sid = _text(raw, "id", where, 40)
    if not _SLUG.match(sid):
        raise NicheError(f"{where}: source id {sid!r} is not a lower-case slug")
    hosts = raw.get("hosts")
    if not isinstance(hosts, list) or not hosts:
        raise NicheError(f"{where}: source {sid!r} needs a non-empty list of hosts")
    for host in hosts:
        if not isinstance(host, str) or not _HOST.match(host):
            raise NicheError(f"{where}: source {sid!r} has a bad host {host!r}")
    return Source(sid, _text(raw, "name", where), tuple(hosts), _text(raw, "license", where))


def parse_niche(entry: Any) -> Niche:
    if not isinstance(entry, Mapping):
        raise NicheError("a niche must be an object")
    nid = entry.get("id")
    where = f"niche {nid!r}"
    stray = set(entry) - _KEYS
    if stray:
        raise NicheError(f"{where}: unknown keys {sorted(stray)} (a typo would silently do nothing)")
    if not isinstance(nid, str) or not _SLUG.match(nid):
        raise NicheError(f"{where}: id must be a lower-case slug of 2-41 characters")
    kind = entry.get("kind")
    if kind not in KINDS:
        raise NicheError(f"{where}: kind must be one of {KINDS}, not {kind!r}")
    voice = _text(entry, "voice", where, 40)
    if not _VOICE.match(voice):
        raise NicheError(f"{where}: {voice!r} is not a Kokoro voice id")
    length = entry.get("length_minutes")
    if (not isinstance(length, list) or len(length) != 2
            or not all(_whole(n) for n in length) or not 1 <= length[0] <= length[1] <= 20):
        raise NicheError(f"{where}: length_minutes must be [low, high], whole minutes, 1 to 20")
    cadence = entry.get("cadence_days")
    if not _whole(cadence) or cadence < MIN_CADENCE_DAYS:
        raise NicheError(f"{where}: cadence_days must be a whole number of at least "
                         f"{MIN_CADENCE_DAYS} (one video a week is the ceiling, not a target)")
    series = entry.get("series")
    if (not isinstance(series, list) or not series
            or not all(isinstance(s, str) and s.strip() for s in series)):
        raise NicheError(f"{where}: series must be a non-empty list of names")
    raw_sources = entry.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise NicheError(f"{where}: sources must be a non-empty list (a script may only stand "
                         "on sources the niche names)")
    sources = tuple(_source(s, where) for s in raw_sources)
    if len({s.id for s in sources}) != len(sources):
        raise NicheError(f"{where}: duplicate source ids")
    mix = entry.get("scene_mix")
    if not isinstance(mix, Mapping) or not mix:
        raise NicheError(f"{where}: scene_mix must be an object of scene type to share")
    for scene, share in mix.items():
        if scene not in SCENE_TYPES:
            raise NicheError(f"{where}: scene type {scene!r} cannot be drawn yet "
                             f"(available: {SCENE_TYPES})")
        if isinstance(share, bool) or not isinstance(share, (int, float)) or not 0 < share <= 1:
            raise NicheError(f"{where}: scene_mix[{scene!r}] must be a share above 0 and up to 1")
    if abs(sum(mix.values()) - 1.0) > 0.011:
        raise NicheError(f"{where}: scene_mix shares must sum to 1, not {sum(mix.values()):.2f}")
    example, live = entry.get("example", False), entry.get("live", False)
    if not isinstance(example, bool) or not isinstance(live, bool):
        raise NicheError(f"{where}: example and live must be true or false")
    if example and live:
        raise NicheError(f"{where}: an example niche cannot be live; copy it, rename it, and "
                         "set example to false")
    return Niche(nid, _text(entry, "title", where, 100), kind, voice,
                 (length[0], length[1]), cadence, tuple(s.strip() for s in series), sources,
                 {k: float(v) for k, v in mix.items()}, example, live)


def load_niches(path: Path | str | None = None) -> list[Niche]:
    """Every niche in the file, or NicheError. One bad entry refuses the whole file: a
    half-loaded roster would quietly drop the niche Ian meant to run. With no path: the file
    named by PIONIR_VIDEO_NICHES, else the one shipped beside this module."""
    path = path or os.environ.get("PIONIR_VIDEO_NICHES", "").strip() or DEFAULT_PATH
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise NicheError(f"cannot read niches from {path}: {error}") from error
    entries = document.get("niches") if isinstance(document, Mapping) else None
    if not isinstance(entries, list):
        raise NicheError(f"{path}: expected an object with a 'niches' list")
    niches = [parse_niche(e) for e in entries]
    if len({n.id for n in niches}) != len(niches):
        raise NicheError(f"{path}: duplicate niche ids")
    return niches


def live_niches(niches: list[Niche]) -> list[Niche]:
    return [n for n in niches if n.live and not n.example]
