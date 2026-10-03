"""One video, start to finish: passages -> checked script -> voice -> render -> parked for approval.

The order is the order of trust. The script is checked before a single sample is spoken, and
the voice and render run only on a script that passed, so a rejected script costs seconds and
no machine time. The last step is not an upload: it hands a payload to ``submit`` (the
approval queue), and a video from a niche that is not live (an example, or one Ian has not
switched on) is built but never offered for approval at all, because his yes would be spent on
something that cannot be published.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .narrate import Synthesizer, narrate
from .niche import Niche
from .package import VideoPackage, queue_dir, verify_package, video_id_for, write_package
from .passages import load_pack
from .render import render_video
from .writer import ScriptWriter, generate_script

CAPABILITY = "video.youtube_upload"


@dataclass(frozen=True, slots=True)
class Made:
    package: VideoPackage
    problems: tuple[str, ...]
    parked: Any
    not_parked_because: str | None


def upload_payload(package: VideoPackage) -> dict[str, Any]:
    """What the approval card carries: an id and the hash that pins the video it was shown."""
    m = package.manifest
    return {"video_id": package.id, "title": m["title"], "series": m["series"],
            "duration_seconds": m["duration_seconds"], "sha256": m["video_sha256"]}


def make_video(niche: Niche, pack_path: Path | str, root: Path, *, writer: ScriptWriter,
               synth: Synthesizer, now: Callable[[], datetime] | None = None,
               size: tuple[int, int] = (1920, 1080),
               submit: Callable[[Mapping[str, Any]], Any] | None = None) -> Made:
    clock = now or (lambda: datetime.now(UTC))
    pack = load_pack(pack_path, niche)
    script = generate_script(writer, niche, pack)          # raises ScriptRejected: nothing built
    video_id = video_id_for(script, niche.id)
    folder = queue_dir(root) / video_id
    narration = narrate(script, synth, niche.voice, folder / "narration.wav")
    rendered = render_video(script, narration, niche, pack, folder, size=size)
    created = clock().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    package = write_package(root, video_id, niche, script, pack, rendered, created_at=created)
    (folder / "narration.wav").unlink(missing_ok=True)
    problems = verify_package(package)
    if niche.example or not niche.live:
        why = ("an example niche never goes to approval" if niche.example
               else "the niche is not live")
        return Made(package, tuple(problems), None, why)
    if problems:
        return Made(package, tuple(problems), None, "the package failed its own checks")
    if submit is None:
        return Made(package, (), None, "no approval queue was given")
    return Made(package, (), submit(upload_payload(package)), None)
