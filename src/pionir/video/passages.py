"""The only things a script may stand on: passages from sources the niche names.

A passage pack is a local JSON file (slice 1 fetches nothing from the network; a later slice
fills packs from the allowlisted hosts and Ian reads them before a script is written)::

    {"topic": "...", "passages": [
        {"id": "p1", "source_id": "wikipedia", "url": "https://en.wikipedia.org/wiki/X",
         "title": "X", "text": "...", "credit": "optional attribution line"}]}

A pack may also carry ``runs``: commands that were actually run in the build sandbox (see
tutorial.py) with their captured stdout and measured numbers. A run is evidence, and it is
offered to the script as a passage (id ``run-<id>``) so every number a tutorial states is
checked against what the command printed. A pack for a live niche accepts only runs recorded
by the real sandbox runner.

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
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .niche import Niche

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
MAX_TEXT = 6000
MAX_STDOUT = 20_000
SANDBOX_RUNNER = "sandbox"
RUN_PREFIX = "run-"


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
class RunMeasure:
    name: str
    value: float
    unit: str


@dataclass(frozen=True, slots=True)
class RunEvidence:
    """One command that was run, as recorded: what it was, how it ended, what it printed."""

    id: str
    title: str
    argv: tuple[str, ...]
    exit_code: int | None
    timed_out: bool
    stdout: str
    measured: tuple[RunMeasure, ...]
    runner: str
    ran_at: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and bool(self.stdout.strip())

    @property
    def command(self) -> str:
        return " ".join(self.argv)

    @property
    def passage_id(self) -> str:
        return RUN_PREFIX + self.id

    def as_passage(self) -> Passage:
        measured = "; ".join(f"{m.name} = {m.value:g} {m.unit}".strip() for m in self.measured)
        text = (f"Command: {self.command}\nExit code: {self.exit_code}\nOutput:\n"
                f"{self.stdout.strip()}"
                + (f"\nMeasured: {measured}" if measured else ""))
        return Passage(self.passage_id, "run_logs", f"sandbox://pionir-builds/run/{self.id}",
                       f"Run: {self.title}", text[:MAX_TEXT],
                       f"Run in the Pionir build sandbox on {self.ran_at} (runner: {self.runner})")


@dataclass(frozen=True, slots=True)
class Pack:
    topic: str
    passages: tuple[Passage, ...]
    images: tuple[ImageRef, ...]
    runs: tuple[RunEvidence, ...] = ()

    @property
    def all_passages(self) -> tuple[Passage, ...]:
        return self.passages + tuple(r.as_passage() for r in self.runs)

    def run(self, run_id: str) -> RunEvidence | None:
        return next((r for r in self.runs if r.id == run_id), None)

    def passage(self, passage_id: str) -> Passage | None:
        found = next((p for p in self.passages if p.id == passage_id), None)
        if found is not None:
            return found
        run = next((r for r in self.runs if r.passage_id == passage_id), None)
        return run.as_passage() if run else None

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


def _run(raw: Any, n: int, niche: Niche) -> RunEvidence:
    where = f"run #{n + 1}"
    if not isinstance(raw, Mapping):
        raise PassageError(f"{where}: must be an object")
    rid = _str(raw, "id", where, 40)
    if not _ID.match(rid):
        raise PassageError(f"{where}: id {rid!r} has characters outside A-Za-z0-9_.-")
    where = f"run {rid!r}"
    argv = raw.get("argv")
    if (not isinstance(argv, list) or not argv or len(argv) > 24
            or not all(isinstance(a, str) and a and len(a) <= 400 for a in argv)):
        raise PassageError(f"{where}: argv must be a list of 1-24 non-empty strings")
    code = raw.get("exit_code")
    if code is not None and (isinstance(code, bool) or not isinstance(code, int)):
        raise PassageError(f"{where}: exit_code must be a whole number or null")
    timed_out = raw.get("timed_out", False)
    if not isinstance(timed_out, bool):
        raise PassageError(f"{where}: timed_out must be true or false")
    stdout = raw.get("stdout")
    if not isinstance(stdout, str) or len(stdout) > MAX_STDOUT:
        raise PassageError(f"{where}: stdout must be text of at most {MAX_STDOUT} characters")
    measured = []
    for m in raw.get("measured") or []:
        if (not isinstance(m, Mapping) or not isinstance(m.get("name"), str)
                or isinstance(m.get("value"), bool)
                or not isinstance(m.get("value"), (int, float))
                or not isinstance(m.get("unit", ""), str)):
            raise PassageError(f"{where}: a measurement needs a name, a number and a unit")
        measured.append(RunMeasure(m["name"][:60], float(m["value"]), str(m.get("unit", ""))[:20]))
    runner = _str(raw, "runner", where, 40)
    if niche.live and not niche.example and runner != SANDBOX_RUNNER:
        raise PassageError(f"{where}: a live niche accepts only runs recorded by the sandbox "
                           f"runner, not {runner!r}")
    ran_at = _str(raw, "ran_at", where, 40)
    try:
        datetime.fromisoformat(ran_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise PassageError(f"{where}: ran_at {ran_at!r} is not a date") from error
    return RunEvidence(rid, _str(raw, "title", where, 160), tuple(argv), code, timed_out, stdout,
                       tuple(measured), runner, ran_at)


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
    runs = tuple(_run(raw, n, niche) for n, raw in enumerate(document.get("runs") or []))
    if len({r.id for r in runs}) != len(runs):
        raise PassageError(f"{path}: duplicate run ids")
    clash = {r.passage_id for r in runs} & {p.id for p in passages}
    if clash:
        raise PassageError(f"{path}: passage ids {sorted(clash)} collide with run passages")
    if not passages and not runs:
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
    return Pack(topic, tuple(passages), tuple(images), runs)
