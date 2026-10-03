"""Shared fixtures for the video pipeline tests. Not a test module (never collected)."""
from __future__ import annotations

import json
import math
import shutil
import struct
from pathlib import Path
from typing import Any

from pionir.video.niche import Niche, parse_niche
from pionir.video.passages import load_pack

HAVE_FFMPEG = shutil.which("ffmpeg") is not None
try:
    import PIL  # noqa: F401
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False


def niche_entry(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": "test-harbor", "example": False, "live": True, "title": "The old harbor",
        "kind": "history", "voice": "af_nicole", "length_minutes": [8, 12], "cadence_days": 7,
        "series": ["Harbor stories"],
        "sources": [
            {"id": "wikipedia", "name": "Wikipedia", "hosts": ["en.wikipedia.org"],
             "license": "CC BY-SA 4.0"},
            {"id": "loc", "name": "Library of Congress", "hosts": ["www.loc.gov"],
             "license": "public domain"}],
        "scene_mix": {"image": 0.6, "card": 0.2, "timeline": 0.2},
    }
    entry.update(overrides)
    return entry


def make_niche(**overrides: Any) -> Niche:
    return parse_niche(niche_entry(**overrides))


def pack_document(image_file: str | None = None) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "topic": "The old harbor",
        "passages": [
            {"id": "p1", "source_id": "wikipedia", "url": "https://en.wikipedia.org/wiki/Harbor",
             "title": "Harbor", "credit": "Wikipedia, CC BY-SA 4.0",
             "text": "The harbor opened in 1851 and held forty ships."},
            {"id": "p2", "source_id": "wikipedia", "url": "https://en.wikipedia.org/wiki/Pier",
             "title": "Pier", "credit": "Wikipedia, CC BY-SA 4.0",
             "text": "Marlow Pier was built of timber and ran two hundred feet."}],
        "images": [],
    }
    if image_file:
        doc["images"] = [{"id": "i1", "source_id": "loc", "url": "https://www.loc.gov/item/1/",
                          "file": image_file, "credit": "Library of Congress, public domain"}]
    return doc


def write_pack(folder: Path, *, with_image: bool = True) -> Path:
    image = None
    if with_image and HAVE_PIL:
        from PIL import Image
        Image.new("RGB", (640, 360), (120, 90, 60)).save(folder / "a.png")
        image = "a.png"
    path = folder / "pack.json"
    path.write_text(json.dumps(pack_document(image)), encoding="utf-8")
    return path


def good_script(with_image: bool = True) -> dict[str, Any]:
    scenes: list[dict[str, Any]] = [
        {"type": "card", "heading": "The old harbor", "lines": [
            {"text": "The harbor opened in 1851.", "sources": ["p1"]},
            {"text": "Marlow Pier was built of timber.", "sources": ["p2"]},
            {"text": "Now, the ships.", "connective": True}]},
        {"type": "timeline", "heading": "Harbor", "lines": [
            {"text": "It held forty ships.", "sources": ["p1"]},
            {"text": "Marlow Pier ran two hundred feet.", "sources": ["p2"]}]},
    ]
    if with_image:
        scenes.insert(1, {"type": "image", "heading": "Ships", "image": "i1", "lines": [
            {"text": "It held forty ships.", "sources": ["p1"]}]})
    return {"title": "The old harbor", "summary": "How the harbor opened.", "scenes": scenes}


def load(folder: Path, niche: Niche | None = None, *, with_image: bool = True):
    niche = niche or make_niche()
    return niche, load_pack(write_pack(folder, with_image=with_image), niche)


class FakeWriter:
    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.calls: list[list[str]] = []

    def write(self, niche, pack, feedback):
        self.calls.append(list(feedback))
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


class FakeSynth:
    """A tone as long as the text: no Kokoro, no model, deterministic."""
    sample_rate = 24000

    def __init__(self) -> None:
        self.spoken: list[str] = []

    def synthesize(self, text: str, voice: str) -> bytes:
        self.spoken.append(text)
        n = int(0.05 * len(text) * self.sample_rate)
        return b"".join(struct.pack("<h", int(3000 * math.sin(i * 0.05))) for i in range(n))
