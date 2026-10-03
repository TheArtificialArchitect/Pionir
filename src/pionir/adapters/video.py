"""``video.youtube_upload``: the only way out for a finished video, and it is a stub.

A video made by pionir.video is a folder under ``<video_dir>/queue/<id>/``. Publishing it is a
public act under the owner's channel, so this capability is PRIVILEGED with
``requires_approval=True``: PionirApp parks it on EVERY call, holding a permission is not a way
around that, and it is ``routable=False`` (reached only by name). The same rule as the blog,
Instagram and money.

Slice 1 builds no uploader. There is no YouTube client in this module, no credential read, no
socket: even after Ian's yes, ``execute`` re-verifies the package and answers that nothing was
uploaded, with the folder to upload by hand. When an uploader exists it will still send the video
PRIVATE (an unaudited Google API project can do no other) and Ian clicks Public in Studio himself.

The payload is exactly ``{video_id, title, series, duration_seconds, sha256}``. A caller cannot
name a path or a file: the folder is resolved here from the id, the manifest must agree with every
field on the card, and the video's hash must still be the one the card pinned.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pionir.contracts import AgentManifest, Capability, RiskLevel, Task, TaskResult
from pionir.errors import AdapterProtocolError, AdapterUnavailable
from pionir.video.disclosure import DISCLOSURE
from pionir.video.niche import NicheError, load_niches
from pionir.video.package import (
    ID_RE,
    PackageError,
    VideoPackage,
    load_package,
    queue_dir,
    verify_package,
)

UPLOAD = "video.youtube_upload"
PAYLOAD_KEYS = frozenset({"video_id", "title", "series", "duration_seconds", "sha256"})
NOT_BUILT = ("the YouTube uploader is not built yet (slice 2): nothing was uploaded. Your "
             "approval is recorded in the audit log; upload the folder by hand in YouTube "
             "Studio, as Private, then click Public yourself")


@dataclass(frozen=True, slots=True)
class VideoSettings:
    video_dir: Path = Path("~/.pionir/video")
    niches_file: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "video_dir", Path(self.video_dir).expanduser())


def video_settings(configured: Any) -> VideoSettings:
    return VideoSettings(video_dir=configured.video_path)


class VideoAdapter:
    def __init__(self, settings: VideoSettings | None = None) -> None:
        self.settings = settings or VideoSettings()
        self._manifest = AgentManifest(
            agent_id="video",
            version="pionir/video",
            capabilities=(
                Capability(
                    name=UPLOAD,
                    description="Upload a finished video to the owner's YouTube channel "
                                "(only after the owner approves it; slice 1 uploads nothing "
                                "and says so)",
                    risk=RiskLevel.PRIVILEGED,
                    required_permissions=frozenset({UPLOAD}),
                    requires_approval=True,
                    routable=False,
                ),
            ),
        )

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def status(self) -> Mapping[str, Any]:
        """Local only: the niche file parses and how many videos are staged. No network."""
        try:
            niches = load_niches(self.settings.niches_file) if self.settings.niches_file \
                else load_niches()
        except NicheError as error:
            raise AdapterUnavailable(f"the niche config is invalid: {error}") from error
        queue = queue_dir(self.settings.video_dir)
        staged = sorted(p.name for p in queue.iterdir() if p.is_dir()) if queue.is_dir() else []
        return {"video_dir": str(self.settings.video_dir),
                "niches": [n.id for n in niches],
                "live": [n.id for n in niches if n.live and not n.example],
                "staged": len(staged), "uploader": "not built (slice 1)"}

    @staticmethod
    def _payload(task: Task) -> Mapping[str, Any]:
        if task.capability != UPLOAD:
            raise AdapterProtocolError(f"video has no capability {task.capability!r}")
        payload = task.payload
        if not isinstance(payload, Mapping) or set(payload) != PAYLOAD_KEYS:
            raise AdapterProtocolError(f"{UPLOAD} refused by Pionir - the payload is exactly "
                                       f"{sorted(PAYLOAD_KEYS)}")
        if not isinstance(payload["video_id"], str) or not ID_RE.match(payload["video_id"]):
            raise AdapterProtocolError(f"{UPLOAD} refused by Pionir - video_id is not an id")
        return payload

    def _package(self, task: Task) -> VideoPackage:
        payload = self._payload(task)
        try:
            package = load_package(self.settings.video_dir, payload["video_id"])
        except PackageError as error:
            raise AdapterProtocolError(f"{UPLOAD} refused by Pionir - {error}") from error
        problems = verify_package(package)
        if problems:
            raise AdapterProtocolError(f"{UPLOAD} refused by Pionir - {'; '.join(problems)}")
        m = package.manifest
        for key, manifest_key in (("title", "title"), ("series", "series"),
                                  ("duration_seconds", "duration_seconds"),
                                  ("sha256", "video_sha256")):
            if payload[key] != m.get(manifest_key):
                raise AdapterProtocolError(f"{UPLOAD} refused by Pionir - {key} on the card "
                                           "is not what the package says")
        return package

    def validate(self, task: Task) -> None:
        self._package(task)

    def park_context(self, task: Task) -> dict[str, Any] | None:
        if task.capability != UPLOAD:
            return None
        package = self._package(task)
        m = package.manifest
        return {"folder": str(package.dir), "title": m["title"], "series": m["series"],
                "minutes": round(m["duration_seconds"] / 60, 1),
                "sources": [f"{s['title']} - {s['credit']}" for s in m["sources"]][:12],
                "disclosure": DISCLOSURE,
                "note": "approving uploads nothing in slice 1; the files stay in the folder"}

    def execute(self, task: Task) -> TaskResult:
        package = self._package(task)
        return TaskResult(task_id=task.task_id, agent_id=self.manifest.agent_id,
                          output={"ok": False, "uploaded": False, "unavailable": NOT_BUILT,
                                  "error": NOT_BUILT, "not_configured": True,
                                  "video_id": package.id, "folder": str(package.dir)},
                          evidence=(f"video:{package.id}",))
