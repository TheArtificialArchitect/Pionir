"""A finished video as a folder: the files, a manifest, and a check that they still agree.

``<video_dir>/queue/<video_id>/`` holds ``video.mp4``, ``captions.srt``, ``thumbnail.png``,
``script.json`` (the checked script, each line with its sources) and ``manifest.json``. The
manifest is what the approval card, the upload stub and the page generator all read, so the
three can never describe different videos: the video's SHA-256 is in it, and
``verify_package`` recomputes it, so an edit after the card was shown is caught before an
upload could ever run.

Nothing in this module publishes. ``uploaded`` stays ``None`` until a real upload (slice 2)
fills it, and the page generator prints ``uploadDate`` only when it is filled.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .disclosure import DISCLOSURE, description
from .niche import Niche
from .passages import Pack
from .render import Rendered
from .script import Script

QUEUE = "queue"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,90}$")
FILES = ("video.mp4", "captions.srt", "thumbnail.png", "script.json", "manifest.json")
STATUS_STAGED = "staged"


class PackageError(ValueError):
    """A package that is missing, damaged, or not what its manifest says."""


@dataclass(frozen=True, slots=True)
class VideoPackage:
    id: str
    dir: Path
    manifest: Mapping[str, Any]

    @property
    def video(self) -> Path:
        return self.dir / "video.mp4"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def slugify(text: str, limit: int = 48) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-")
    return slug or "video"


def script_document(script: Script) -> dict[str, Any]:
    return {"title": script.title, "summary": script.summary,
            "scenes": [{"type": s.type, "heading": s.heading, "image": s.image,
                        "lines": [{"text": line.text, "sources": list(line.sources),
                                   "connective": line.connective} for line in s.lines]}
                       for s in script.scenes]}


def video_id_for(script: Script, niche_id: str) -> str:
    """Readable and stable: the title's slug plus a short hash of the niche and exact script,
    so two channels that happen to write the same script never share a folder."""
    body = json.dumps([niche_id, script_document(script)], sort_keys=True, ensure_ascii=True)
    return f"{slugify(script.title)}-{hashlib.sha256(body.encode()).hexdigest()[:8]}"


def queue_dir(root: Path) -> Path:
    return Path(root) / QUEUE


def write_package(root: Path, video_id: str, niche: Niche, script: Script, pack: Pack,
                  rendered: Rendered, *, created_at: str) -> VideoPackage:
    """Write script.json and manifest.json beside the rendered files in the package folder."""
    folder = queue_dir(root) / video_id
    if not ID_RE.match(video_id) or rendered.video.parent != folder:
        raise PackageError(f"{video_id!r} is not the folder the render went to")
    (folder / "script.json").write_text(
        json.dumps(script_document(script), indent=2, ensure_ascii=False), encoding="utf-8")
    manifest = {
        "id": video_id,
        "status": STATUS_STAGED,
        "niche": niche.id,
        "example": niche.example,
        "series": niche.series[0],
        "title": script.title,
        "summary": script.summary,
        "description": description(script.summary, script.passages, rendered.images_used,
                                   niche.series[0]),
        "disclosure": DISCLOSURE,
        "duration_seconds": rendered.duration,
        "width": rendered.width,
        "height": rendered.height,
        "size_bytes": rendered.size_bytes,
        "video_sha256": sha256_file(rendered.video),
        "created_at": created_at,
        "length_target_minutes": list(niche.length_minutes),
        "sources": [{"id": p.id, "source_id": p.source_id, "title": p.title, "url": p.url,
                     "credit": p.credit} for p in script.passages],
        "images": [{"id": i.id, "url": i.url, "credit": i.credit} for i in rendered.images_used],
        "uploaded": None,
    }
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False),
                                          encoding="utf-8")
    return VideoPackage(video_id, folder, manifest)


def load_package(root: Path, video_id: str) -> VideoPackage:
    if not isinstance(video_id, str) or not ID_RE.match(video_id):
        raise PackageError(f"{video_id!r} is not a video id")
    folder = queue_dir(root) / video_id
    try:
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PackageError(f"no readable manifest for {video_id!r}: {error}") from error
    if not isinstance(manifest, dict) or manifest.get("id") != video_id:
        raise PackageError(f"the manifest in {video_id!r} names a different video")
    return VideoPackage(video_id, folder, manifest)


def verify_package(package: VideoPackage, *, allow_uploaded: bool = False) -> list[str]:
    """Every reason this package may not be offered for upload; empty means it may.

    ``allow_uploaded`` is for the page generator, which still describes a video after it is up.
    """
    m, problems = package.manifest, []
    for name in FILES:
        if not (package.dir / name).is_file():
            problems.append(f"{name} is missing")
    if m.get("status") != STATUS_STAGED:
        problems.append(f"status is {m.get('status')!r}, not {STATUS_STAGED!r}")
    if m.get("example"):
        problems.append("this video belongs to an example niche, which never publishes")
    if m.get("uploaded") and not allow_uploaded:
        problems.append("this video was already uploaded")
    if not m.get("sources"):
        problems.append("the manifest lists no sources")
    if not str(m.get("description", "")).rstrip().endswith(DISCLOSURE):
        problems.append("the description does not end with the disclosure line")
    if not isinstance(m.get("duration_seconds"), (int, float)) or m["duration_seconds"] <= 0:
        problems.append("the duration is not a positive number")
    if package.video.is_file() and m.get("video_sha256") != sha256_file(package.video):
        problems.append("video.mp4 changed after the manifest was written")
    return problems
