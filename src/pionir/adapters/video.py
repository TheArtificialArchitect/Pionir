"""``video.youtube_upload``: the only way out for a finished video, and it goes out PRIVATE.

A video made by pionir.video is a folder under ``<video_dir>/queue/<id>/``. Publishing it is a
public act under the owner's channel, so this capability is PRIVILEGED with
``requires_approval=True``: PionirApp parks it on EVERY call, holding a permission is not a way
around that, and it is ``routable=False`` (reached only by name). The same rule as the blog,
Instagram and money.

``execute`` runs only after Ian's yes: it needs the grant ``PionirApp.approve`` adds
(``OWNER_APPROVED_GRANT``) - holding ``video.youtube_upload`` is not enough - then the upload gates
in ``pionir.video.upload`` (niche ``live: true`` and not an example, a package that still verifies,
secrets in ``~/.pionir/secrets``). The video is sent PRIVATE (an unaudited Google API project can
do no other); Ian clicks Public in Studio himself and records it with ``pionir video published``.
A refusal or failure is returned as an ordinary unsuccessful result with the reason, never raised
past the approval, and no secret is ever in it.

The payload is exactly ``{video_id, title, series, duration_seconds, sha256}``. A caller cannot
name a path or a file: the folder is resolved here from the id, the manifest must agree with every
field on the card, and the video's hash must still be the one the card pinned.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pionir.batching import OWNER_APPROVED_GRANT
from pionir.contracts import AgentManifest, Capability, RiskLevel, Task, TaskResult
from pionir.errors import AdapterProtocolError, AdapterUnavailable
from pionir.video.disclosure import DISCLOSURE
from pionir.video.niche import NicheError, load_niches
from pionir.video.upload import (
    QuotaExhausted,
    Transport,
    UploadError,
    UploadRefused,
    default_secrets_dir,
    secrets_present,
    upload_package,
)
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
NOT_APPROVED = "the owner has not approved this upload: nothing was sent"


@dataclass(frozen=True, slots=True)
class VideoSettings:
    video_dir: Path = Path("~/.pionir/video")
    niches_file: Path | None = None
    secrets_dir: Path = Path("~/.pionir/secrets")

    def __post_init__(self) -> None:
        object.__setattr__(self, "video_dir", Path(self.video_dir).expanduser())
        object.__setattr__(self, "secrets_dir", Path(self.secrets_dir).expanduser())


def video_settings(configured: Any) -> VideoSettings:
    return VideoSettings(video_dir=configured.video_path)


class VideoAdapter:
    def __init__(self, settings: VideoSettings | None = None, *,
                 transport: Transport | None = None, sleep=None) -> None:
        self.settings = settings or VideoSettings()
        self._transport = transport
        self._sleep = sleep
        self._manifest = AgentManifest(
            agent_id="video",
            version="pionir/video",
            capabilities=(
                Capability(
                    name=UPLOAD,
                    description="Upload a finished video to the owner's YouTube channel "
                                "PRIVATE (only after the owner approves it; the owner makes it "
                                "public in Studio)",
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
                "staged": len(staged),
                "uploader": "ready" if secrets_present(self.settings.secrets_dir)
                else "secrets not set up"}

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
                "note": "approving uploads it as PRIVATE if the niche is live and the YouTube "
                        "secrets are set up; you make it public in Studio"}

    def execute(self, task: Task) -> TaskResult:
        package = self._package(task)
        approved = OWNER_APPROVED_GRANT in task.granted_permissions
        out: dict[str, Any] = {"video_id": package.id, "folder": str(package.dir)}
        try:
            if not approved:
                raise UploadRefused(NOT_APPROVED)
            try:
                niches = {n.id: n for n in (load_niches(self.settings.niches_file)
                                            if self.settings.niches_file else load_niches())}
            except NicheError as error:
                raise UploadRefused(f"the niche config is invalid: {error}") from error
            niche = niches.get(package.manifest.get("niche"))
            if niche is None:
                raise UploadRefused("the package's niche is not in the niche config")
            kwargs: dict[str, Any] = {}
            if self._sleep is not None:
                kwargs["sleep"] = self._sleep
            record = upload_package(package, niche, approved=approved,
                                    secrets_dir=self.settings.secrets_dir,
                                    transport=self._transport, **kwargs)
        except QuotaExhausted as error:
            out.update(ok=False, uploaded=False, quota_exhausted=True, error=str(error))
        except (UploadRefused, UploadError) as error:
            out.update(ok=False, uploaded=False, error=str(error))
        else:
            out.update(ok=True, uploaded=True, privacy=record["privacy"],
                       youtube_id=record["youtube_id"],
                       note="uploaded PRIVATE: make it public in YouTube Studio, then run "
                            "pionir video published " + package.id)
        return TaskResult(task_id=task.task_id, agent_id=self.manifest.agent_id, output=out,
                          evidence=(f"video:{package.id}",))
