"""The YouTube uploader: one approved video, sent PRIVATE, resumable, and nothing else.

``upload_package`` refuses unless every one of these holds, and says which did not:

* the owner approved it (``approved`` is True only when the adapter saw the grant
  ``PionirApp.approve`` adds - the module has no way to approve anything itself),
* the niche is ``live: true`` and not an example,
* the package verifies (files, hash, disclosure line, not already uploaded),
* the secrets exist in ``~/.pionir/secrets`` (``youtube-client.json``, ``youtube-token.json``).

Then, through an injected ``Transport`` (the tests' is a fake; no test and no session of the
build touched Google):

* the access token comes from the refresh token (read from the secrets files only; nothing
  here prints, logs, returns or raises with a token or secret - error text carries Google's
  error CODE and status, never a body that could echo one),
* the video is sent with ``privacyStatus: private`` (an unaudited API project can do nothing
  else; Ian clicks Public in Studio), declared synthetic media and not made for kids,
* the upload is RESUMABLE: the session URL is persisted beside the package
  (``upload-state.json``), so a crash or timeout resumes the same session by asking Google how
  many bytes it has - it can never create a second copy of the video,
* a 5xx or a dropped connection is retried with exponential backoff (``sleep`` is injected),
  a 401 refreshes the token once, and a quota answer (the default allowance is 10,000 units a
  day and one ``videos.insert`` costs 1,600) stops cleanly with ``QuotaExhausted`` and keeps the
  session for tomorrow,
* on success the manifest records ``uploaded`` with ``privacy: "private"`` - the pages treat a
  video as published only after ``mark_public`` (``pionir video published``) records that Ian
  made it public.

Captions are not uploaded (they need a wider OAuth scope than ``youtube.upload``); the thumbnail
is, with the same scope, and its failure never undoes the upload.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from ..atomic import write_text
from .niche import Niche
from .package import VideoPackage, load_package, sha256_file, verify_package

CLIENT_FILE = "youtube-client.json"
TOKEN_FILE = "youtube-token.json"
STATE_FILE = "upload-state.json"
SCOPE = "https://www.googleapis.com/auth/youtube.upload"
TOKEN_URL = "https://oauth2.googleapis.com/token"
INSERT_URL = ("https://www.googleapis.com/upload/youtube/v3/videos"
              "?uploadType=resumable&part=snippet,status")
THUMB_URL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId="
CHUNK = 32 * 256 * 1024                 # 8 MiB, a multiple of 256 KiB as the API requires
MAX_ATTEMPTS = 6
MAX_BACKOFF = 64.0
MAX_TITLE = 100
MAX_DESCRIPTION_BYTES = 5000
MAX_THUMBNAIL = 2_000_000
CATEGORY_EDUCATION = "27"
QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded", "uploadLimitExceeded", "rateLimitExceeded"}
_YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
_RANGE = re.compile(r"bytes=0-(\d+)")


class UploadError(RuntimeError):
    """The upload did not complete; the session is kept so the next try resumes it."""


class UploadRefused(UploadError):
    """A gate said no, before any network use."""


class QuotaExhausted(UploadError):
    """Google's daily quota is spent; try again after it resets (midnight Pacific)."""


class TransportError(RuntimeError):
    """No answer came back at all."""


@dataclass(frozen=True, slots=True)
class TransportResponse:
    status: int
    headers: dict = field(default_factory=dict)
    body: bytes = b""


class Transport(Protocol):
    def request(self, method: str, url: str, *, headers: dict, body: bytes | None,
                timeout: float = 60.0) -> TransportResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None            # 308 is "resume incomplete" here, not a redirect


class UrllibTransport:
    """The real transport. Only ever built after every gate has passed."""

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(_NoRedirect)

    def request(self, method: str, url: str, *, headers: dict, body: bytes | None,
                timeout: float = 60.0) -> TransportResponse:
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            try:
                with self._opener.open(req, timeout=timeout) as r:
                    return TransportResponse(r.status, dict(r.headers.items()), r.read())
            except urllib.error.HTTPError as exc:
                return TransportResponse(exc.code, dict(exc.headers.items()) if exc.headers else {},
                                         exc.read())
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransportError(type(exc).__name__) from exc


@dataclass(frozen=True, slots=True)
class Credentials:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    refresh_token: str = field(repr=False)


def default_secrets_dir() -> Path:
    return Path.home() / ".pionir" / "secrets"


def secrets_present(secrets_dir: Path) -> bool:
    return (Path(secrets_dir) / CLIENT_FILE).is_file() and (Path(secrets_dir) / TOKEN_FILE).is_file()


def load_credentials(secrets_dir: Path) -> Credentials:
    """Read both secrets files. Errors name the file and the missing key, never a value."""
    values: dict[str, str] = {}
    for name, keys in ((CLIENT_FILE, ("client_id", "client_secret")), (TOKEN_FILE, ("refresh_token",))):
        try:
            doc = json.loads((Path(secrets_dir) / name).read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            raise UploadRefused(f"{name} is not in the secrets folder (see "
                                "docs/VIDEO_SETUP_FOR_IAN.md)") from None
        except (OSError, ValueError):
            raise UploadRefused(f"{name} is unreadable") from None
        for key in keys:
            value = doc.get(key) if isinstance(doc, dict) else None
            if not isinstance(value, str) or not value.strip():
                raise UploadRefused(f"{name} has no {key}")
            values[key] = value.strip()
    return Credentials(values["client_id"], values["client_secret"], values["refresh_token"])


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _reason(response: TransportResponse) -> str:
    """Google's error code from an error body; nothing else of the body is ever used."""
    try:
        doc = json.loads(response.body.decode("utf-8", "replace"))
    except ValueError:
        return ""
    error = doc.get("error") if isinstance(doc, dict) else None
    if isinstance(error, dict):
        for item in error.get("errors") or []:
            if isinstance(item, dict) and isinstance(item.get("reason"), str):
                return item["reason"][:60]
        status = error.get("status")
        return status[:60] if isinstance(status, str) else ""
    return error[:60] if isinstance(error, str) else ""


def gate(package: VideoPackage, niche: Niche, *, approved: bool, secrets_dir: Path) -> None:
    """Every refusal that needs no network, in the order a person would want to hear it."""
    if not approved:
        raise UploadRefused("the owner has not approved this upload (no approval grant)")
    if niche.example or not niche.live:
        raise UploadRefused(f"niche {niche.id!r} is not live: uploading stays disabled until "
                            "its niche entry says live: true")
    if package.manifest.get("niche") != niche.id:
        raise UploadRefused("the package was made for a different niche")
    problems = verify_package(package)
    if problems:
        raise UploadRefused("the package does not verify: " + "; ".join(problems))
    if not secrets_present(secrets_dir):
        raise UploadRefused("the YouTube secrets are not set up (see docs/VIDEO_SETUP_FOR_IAN.md)")


def metadata(manifest: dict[str, Any]) -> dict[str, Any]:
    title = re.sub(r"[<>]", "", str(manifest["title"])).strip()
    description = re.sub(r"[<>]", "", str(manifest["description"])).rstrip()
    if not title or len(title) > MAX_TITLE:
        raise UploadRefused(f"the title is empty or longer than {MAX_TITLE} characters")
    if len(description.encode("utf-8")) > MAX_DESCRIPTION_BYTES:
        raise UploadRefused(f"the description is longer than {MAX_DESCRIPTION_BYTES} bytes")
    return {"snippet": {"title": title, "description": description,
                        "categoryId": CATEGORY_EDUCATION},
            "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False,
                       "containsSyntheticMedia": True, "embeddable": True}}


class _Session:
    def __init__(self, transport: Transport, creds: Credentials,
                 sleep: Callable[[float], None]) -> None:
        self.transport, self.creds, self.sleep = transport, creds, sleep
        self.token = ""
        self.refreshes = 0

    def refresh(self) -> None:
        body = urllib.parse.urlencode({
            "client_id": self.creds.client_id, "client_secret": self.creds.client_secret,
            "refresh_token": self.creds.refresh_token, "grant_type": "refresh_token"}).encode()
        try:
            response = self.transport.request(
                "POST", TOKEN_URL, headers={"Content-Type": "application/x-www-form-urlencoded"},
                body=body)
        except TransportError as error:
            raise UploadError(f"the token request did not get an answer ({error})") from None
        if response.status != 200:
            reason = _reason(response) or "no reason given"
            hint = (" - the refresh token was revoked or expired; run "
                    "tools\\video-youtube-consent.ps1 again") if reason == "invalid_grant" else ""
            raise UploadError(f"Google refused the token request ({response.status}: {reason})"
                              f"{hint}")
        try:
            token = json.loads(response.body.decode("utf-8")).get("access_token")
        except (ValueError, AttributeError):
            token = None
        if not isinstance(token, str) or not token:
            raise UploadError("Google's token answer held no access token")
        self.token = token
        self.refreshes += 1

    def call(self, method: str, url: str, headers: dict, body: bytes | None) -> TransportResponse:
        """One authorised request; a 401 refreshes once and retries it."""
        for attempt in (0, 1):
            if not self.token:
                self.refresh()
            response = self.transport.request(
                method, url, headers={**headers, "Authorization": f"Bearer {self.token}"},
                body=body)
            if response.status == 401 and attempt == 0:
                self.token = ""
                continue
            return response
        return response

    def backoff(self, attempt: int) -> None:
        self.sleep(min(2.0 ** attempt, MAX_BACKOFF))


def _check_quota(response: TransportResponse) -> None:
    if response.status in (403, 429) and _reason(response) in QUOTA_REASONS:
        raise QuotaExhausted(f"YouTube's quota is spent ({_reason(response)}); the upload "
                             "session is kept - run it again after the quota resets "
                             "(midnight Pacific)")


def _state_path(package: VideoPackage) -> Path:
    return package.dir / STATE_FILE


def _read_state(package: VideoPackage, total: int, digest: str) -> str | None:
    try:
        state = json.loads(_state_path(package).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ok = (isinstance(state, dict) and state.get("total") == total and state.get("sha256") == digest
          and isinstance(state.get("session_url"), str)
          and state["session_url"].startswith("https://"))
    return state["session_url"] if ok else None


def _start(session: _Session, package: VideoPackage, total: int, digest: str) -> str:
    meta = json.dumps(metadata(dict(package.manifest))).encode("utf-8")
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = session.call("POST", INSERT_URL, {
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(total), "X-Upload-Content-Type": "video/mp4"}, meta)
        except TransportError:
            session.backoff(attempt)
            continue
        _check_quota(response)
        if response.status == 200:
            url = next((v for k, v in response.headers.items() if k.lower() == "location"), "")
            if not url.startswith("https://"):
                raise UploadError("YouTube did not return an upload session URL")
            write_text(_state_path(package), json.dumps(
                {"session_url": url, "total": total, "sha256": digest}))
            return url
        if response.status >= 500:
            session.backoff(attempt)
            continue
        raise UploadError(f"YouTube refused to start the upload ({response.status}: "
                          f"{_reason(response) or 'no reason given'})")
    raise UploadError("YouTube did not answer the upload start after several tries")


def _result(response: TransportResponse) -> tuple[str, str]:
    try:
        doc = json.loads(response.body.decode("utf-8"))
    except ValueError:
        doc = None
    video_id = doc.get("id") if isinstance(doc, dict) else None
    if not isinstance(video_id, str) or not _YOUTUBE_ID.match(video_id):
        raise UploadError("the upload finished but YouTube's answer held no video id")
    status = doc.get("status") if isinstance(doc.get("status"), dict) else {}
    return video_id, str(status.get("privacyStatus") or "private")


def _offset(response: TransportResponse) -> int:
    sent = _RANGE.match(next((v for k, v in response.headers.items() if k.lower() == "range"), ""))
    return int(sent.group(1)) + 1 if sent else 0


def _send(session: _Session, package: VideoPackage, url: str, total: int, digest: str,
          *, resumed: bool = False) -> TransportResponse:
    offset = 0
    attempt = 0
    if resumed:
        # A stored session may already hold part of the video: ask where it stands first.
        try:
            probe = session.call("PUT", url, {"Content-Length": "0",
                                              "Content-Range": f"bytes */{total}"}, b"")
        except TransportError:
            probe = None
        if probe is not None:
            _check_quota(probe)
            if probe.status in (200, 201):
                return probe
            if probe.status in (404, 410):
                _state_path(package).unlink(missing_ok=True)
                raise UploadError("the upload session expired; run it again to start a new one")
            if probe.status == 308:
                offset = _offset(probe)
    with package.video.open("rb") as handle:
        while True:
            handle.seek(offset)
            chunk = handle.read(CHUNK)
            end = offset + len(chunk) - 1
            try:
                response = session.call("PUT", url, {
                    "Content-Type": "video/mp4", "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {offset}-{end}/{total}"}, chunk)
            except TransportError:
                response = None
            if response is not None:
                _check_quota(response)
                if response.status in (200, 201):
                    return response
                if response.status == 308:
                    attempt = 0
                    offset = _offset(response)
                    continue
                if response.status in (404, 410):
                    _state_path(package).unlink(missing_ok=True)
                    raise UploadError("the upload session expired; run it again to start a new one")
                if response.status < 500:
                    raise UploadError(f"YouTube refused the upload ({response.status}: "
                                      f"{_reason(response) or 'no reason given'})")
            attempt += 1
            if attempt >= MAX_ATTEMPTS:
                raise UploadError("the upload kept failing; the session is kept, so the next "
                                  "run resumes where it stopped")
            session.backoff(attempt)
            try:
                probe = session.call("PUT", url, {"Content-Length": "0",
                                                  "Content-Range": f"bytes */{total}"}, b"")
            except TransportError:
                continue
            _check_quota(probe)
            if probe.status in (200, 201):
                return probe
            if probe.status == 308:
                offset = _offset(probe)


def _thumbnail(session: _Session, package: VideoPackage, video_id: str) -> bool:
    path = package.dir / "thumbnail.png"
    try:
        data = path.read_bytes()
        if not data or len(data) > MAX_THUMBNAIL:
            return False
        response = session.call("POST", THUMB_URL + urllib.parse.quote(video_id),
                                {"Content-Type": "image/png"}, data)
        return response.status == 200
    except (OSError, TransportError):
        return False


def upload_package(package: VideoPackage, niche: Niche, *, approved: bool, secrets_dir: Path,
                   transport: Transport | None = None, sleep: Callable[[float], None] = time.sleep,
                   clock: Callable[[], str] = _now) -> dict[str, Any]:
    """Upload one approved package PRIVATE. Returns what was recorded; raises UploadError."""
    gate(package, niche, approved=approved, secrets_dir=Path(secrets_dir))
    creds = load_credentials(Path(secrets_dir))
    session = _Session(transport or UrllibTransport(), creds, sleep)
    total = package.video.stat().st_size
    digest = sha256_file(package.video)
    if digest != package.manifest.get("video_sha256"):
        raise UploadRefused("video.mp4 changed after the manifest was written")
    url = _read_state(package, total, digest)
    resumed = url is not None
    url = url or _start(session, package, total, digest)
    try:
        response = _send(session, package, url, total, digest, resumed=resumed)
    except UploadError as error:
        if "expired" in str(error):
            url = _start(session, package, total, digest)
            response = _send(session, package, url, total, digest)
        else:
            raise
    video_id, privacy = _result(response)
    record = {"youtube_id": video_id, "upload_date": clock(), "privacy": privacy,
              "sha256": digest, "thumbnail_set": _thumbnail(session, package, video_id)}
    manifest = dict(package.manifest)
    manifest["uploaded"] = record
    write_text(package.dir / "manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
    _state_path(package).unlink(missing_ok=True)
    return record


def mark_public(root: Path, video_id: str, *, clock: Callable[[], str] = _now) -> dict[str, Any]:
    """Record that Ian made an uploaded video public in Studio. Nothing is sent to YouTube."""
    package = load_package(root, video_id)
    uploaded = package.manifest.get("uploaded")
    if not isinstance(uploaded, dict) or not _YOUTUBE_ID.match(str(uploaded.get("youtube_id"))):
        raise UploadRefused("this video has no recorded upload")
    if uploaded.get("privacy") == "public":
        raise UploadRefused("this video is already recorded as public")
    manifest = dict(package.manifest)
    manifest["uploaded"] = {**uploaded, "privacy": "public", "published_at": clock()}
    write_text(package.dir / "manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
    return manifest["uploaded"]
