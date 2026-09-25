"""Adapter for Instagram: a post goes live on the Dokaz Instagram only after the owner's yes.

The same rule as the blog and as money: nothing goes public without Ian's approval, and
holding a permission is not a way around it. ``social.instagram_post`` is PRIVILEGED with
``requires_approval=True`` (PionirApp parks it on EVERY call; the Discord card shows the
rendered image and the full caption) and ``routable=False`` (reached only by name).

The payload is exactly ``pionir.social.post.check_post``'s shape - the crew drafts with the
same check - and it is checked before anything is parked, and again before anything is sent.
The image is not in the payload: it is a pure function of the text (``render_card``), so the
card the owner is shown is byte for byte the card that is posted. ``card_sha`` pins it.

Publishing, once approved:

1. render the card and upload the JPEG to Scrooge (``POST /dash/content/media``, the same
   publish token as ``content.publish``); Scrooge's sha must equal the sha of what was sent;
2. create an Instagram media container from Scrooge's public URL and the full caption;
3. poll the container every 2 s (up to 60 s) until Instagram has fetched and processed it;
4. publish the container, then ask for the post's permalink.

Both secrets are read from files on every call (so adding them needs no restart), and never
appear in a log, an error or a result: every text that could echo one is scrubbed. Anything
the other side says no to comes back as ``ok: false`` with ``refused``; a missing or rejected
token, or a service that does not answer, as ``ok: false`` with ``unavailable`` - answers,
not faults, so the circuit breaker is not tripped. Nothing here runs in the background: the
Instagram token is refreshed (when it is over 7 days old) on the way to a post.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pionir.adapters.content import ContentSettings, read_token
from pionir.contracts import AgentManifest, Capability, RiskLevel, Task, TaskResult
from pionir.errors import AdapterProtocolError, AdapterUnavailable
from pionir.social.card import CardTooLong, render_card
from pionir.social.post import check_post, full_caption

_log = logging.getLogger(__name__)

POST = "social.instagram_post"
DEFAULT_GRAPH_URL = "https://graph.instagram.com/v25.0"
MAX_RESPONSE_BYTES = 1_000_000
MAX_MEDIA_BYTES = 1_500_000            # Scrooge's /dash/content/media limit
POLL_SECONDS = 2.0
POLL_LIMIT_SECONDS = 60.0
REFRESH_AFTER = timedelta(days=7)
REFRESH_MIN_AGE = timedelta(hours=24)  # Meta refuses to refresh a token younger than this
SETUP_HINT = r"run tools\setup-instagram.ps1"
TOKEN_REJECTED = f"the Instagram token was rejected or has expired - {SETUP_HINT}"
# Graph codes that mean "not now" rather than "no": rate limits and throttling.
_RATE_LIMIT_CODES = frozenset({4, 17, 32, 613})

Opener = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class InstagramToken:
    """The contents of instagram.json. ``access_token`` is never shown in a repr."""

    access_token: str = field(repr=False)
    user_id: str
    username: str
    refreshed_at: datetime | None


def _parse_time(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_instagram_token(path: Path) -> InstagramToken | None:
    """The Instagram token file, or None if it is missing or unusable. Never logs it."""
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    token = document.get("access_token")
    user_id = document.get("user_id")
    if not isinstance(token, str) or not token.strip():
        return None
    if isinstance(user_id, int) and not isinstance(user_id, bool):
        user_id = str(user_id)
    if not isinstance(user_id, str) or not user_id.strip().isdigit():
        return None
    username = document.get("username")
    return InstagramToken(access_token=token.strip(), user_id=user_id.strip(),
                          username=username if isinstance(username, str) else "",
                          refreshed_at=_parse_time(document.get("refreshed_at")))


def _write_token_file(path: Path, document: Mapping[str, Any]) -> None:
    """Replace the token file atomically: a crash leaves the old file or the new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".instagram-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _check_url(url: str, what: str) -> None:
    parsed = urllib.parse.urlparse(url)
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    # The token is a secret: it only ever travels over TLS, or to this machine.
    if not (parsed.scheme == "https" or (parsed.scheme == "http" and loopback)):
        raise ValueError(f"the {what} URL must be https: (or http: on loopback)")
    if not parsed.hostname:
        raise ValueError(f"the {what} URL needs a host")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"the {what} URL cannot carry credentials or query data")


@dataclass(frozen=True, slots=True)
class InstagramSettings:
    """Where the Graph API and Scrooge are, and where both secrets live (PATHS, never the
    secrets). ``content`` is the blog's settings: the media upload uses the same Scrooge and
    the same publish token."""

    graph_url: str = DEFAULT_GRAPH_URL
    token_file: Path = Path("~/.pionir/secrets/instagram.json")
    content: ContentSettings = field(default_factory=ContentSettings)
    timeout_seconds: int = 30
    poll_seconds: float = POLL_SECONDS
    poll_limit_seconds: float = POLL_LIMIT_SECONDS

    def __post_init__(self) -> None:
        _check_url(self.graph_url, "Instagram Graph")
        if self.timeout_seconds < 5:
            raise ValueError("the Instagram timeout must be at least 5 seconds")
        if self.poll_seconds <= 0 or self.poll_limit_seconds < self.poll_seconds:
            raise ValueError("the Instagram poll interval must be positive and within the limit")
        object.__setattr__(self, "token_file", Path(self.token_file).expanduser())

    @property
    def refresh_url(self) -> str:
        """The refresh endpoint is on the host root, not under the version path."""
        parsed = urllib.parse.urlparse(self.graph_url)
        return f"{parsed.scheme}://{parsed.netloc}/refresh_access_token"


class _Failure(Exception):
    """One step's plain answer (already scrubbed), carried out of the step to execute()."""

    def __init__(self, output: dict[str, Any]) -> None:
        super().__init__(output.get("error"))
        self.output = output


def _refused(why: str, **extra: Any) -> _Failure:
    return _Failure({"ok": False, "refused": why, "error": why, **extra})


def _unavailable(why: str, **extra: Any) -> _Failure:
    return _Failure({"ok": False, "unavailable": why, "error": why, **extra})


class InstagramAdapter:
    """Posting to the Dokaz Instagram as a gated, audited Pionir capability."""

    def __init__(self, settings: InstagramSettings | None = None, *,
                 opener: Opener | None = None,
                 sleep: Callable[[float], Any] | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.settings = settings or InstagramSettings()
        self._graph = self.settings.graph_url.rstrip("/")
        self._scrooge = self.settings.content.base_url.rstrip("/")
        self._open = opener or urllib.request.build_opener().open
        self._sleep = sleep or time.sleep
        self._clock = clock or (lambda: datetime.now(UTC))
        self._manifest = AgentManifest(
            agent_id="instagram",
            version="pionir/instagram",
            capabilities=(
                Capability(
                    name=POST,
                    description="Post an image and caption to the public Dokaz Instagram "
                                "(only after the owner approves it)",
                    risk=RiskLevel.PRIVILEGED,
                    required_permissions=frozenset({POST}),
                    requires_approval=True,
                    routable=False,
                ),
            ),
        )

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    # ---- configuration (local files only) --------------------------------------------
    def _not_configured(self) -> str | None:
        missing = []
        if read_instagram_token(self.settings.token_file) is None:
            missing.append(f"no usable Instagram token at {self.settings.token_file} - "
                           f"{SETUP_HINT}")
        if read_token(self.settings.content.token_file) is None:
            missing.append(f"no publish token at {self.settings.content.token_file} - "
                           "put Scrooge's publish token in that file")
        return ("not configured: " + "; ".join(missing)) if missing else None

    def status(self) -> Mapping[str, Any]:
        """Local only - no call to Instagram or Scrooge, so doctor stays hermetic and fast."""
        why = self._not_configured()
        if why:
            raise AdapterUnavailable(why)
        token = read_instagram_token(self.settings.token_file)
        assert token is not None
        return {"graph": self._graph, "account": token.username or token.user_id,
                "token": "configured", "publish_token": "configured",
                "refreshed_at": _iso(token.refreshed_at) if token.refreshed_at else None}

    # ---- the request -----------------------------------------------------------------
    def validate(self, task: Task) -> None:
        """Refuse a bad post, or a post that cannot run, before it is parked."""
        self._check(task)
        why = self._not_configured()
        if why:
            # Asking the owner to approve a post that cannot be sent wastes his yes.
            raise AdapterUnavailable(why)

    @staticmethod
    def _check(task: Task) -> dict[str, Any]:
        if task.capability != POST:
            raise AdapterProtocolError(f"instagram has no capability {task.capability!r}")
        try:
            return check_post(task.payload)
        except ValueError as error:
            raise AdapterProtocolError(f"{POST} refused by Pionir - {error}") from error

    # ---- the call --------------------------------------------------------------------
    def execute(self, task: Task) -> TaskResult:
        post = self._check(task)
        why = self._not_configured()
        ig = read_instagram_token(self.settings.token_file)
        publish = read_token(self.settings.content.token_file)
        if why or ig is None or publish is None:
            why = why or "not configured"
            return self._result(task, {"ok": False, "unavailable": why, "error": why,
                                       "not_configured": True})
        secrets = [ig.access_token, publish]
        try:
            output = self._publish(post, ig, publish, secrets)
        except _Failure as failure:
            output = failure.output
        # Belt and braces: whatever went wrong, no secret leaves in the result.
        return self._result(task, json.loads(self._scrub(json.dumps(output), secrets)))

    def _publish(self, post: Mapping[str, Any], ig: InstagramToken, publish: str,
                 secrets: list[str]) -> dict[str, Any]:
        try:
            image = render_card(post["headline"], post["points"])
        except (CardTooLong, ImportError, OSError, ValueError) as error:
            raise _unavailable(f"the card could not be rendered: {error}") from None
        sha = hashlib.sha256(image).hexdigest()
        pinned = post.get("card_sha")
        if pinned is not None and pinned != sha:
            raise _refused("the card renders differently from the one approved "
                           "(card_sha mismatch); nothing was posted")
        if len(image) > MAX_MEDIA_BYTES:
            raise _refused(f"the card is {len(image):,} bytes, over Scrooge's "
                           f"{MAX_MEDIA_BYTES:,} limit")

        access = self._fresh_token(ig, secrets)
        secrets.append(access)
        image_url = self._upload(image, sha, publish, secrets)
        _log.info("instagram: card %s uploaded to Scrooge", sha[:12])

        caption = full_caption(post["caption"], post["hashtags"])
        user = urllib.parse.quote(ig.user_id, safe="")
        made = self._graph_call("POST", f"/{user}/media", "container", secrets, access,
                                form={"image_url": image_url, "caption": caption})
        container = made.get("id")
        if not isinstance(container, (str, int)) or not str(container).strip():
            raise _unavailable("Instagram answered the container step without an id",
                               image_url=image_url)
        container = str(container)
        _log.info("instagram: container %s created", container)
        self._wait_for(container, secrets, access, image_url)

        done = self._graph_call("POST", f"/{user}/media_publish", "publish", secrets, access,
                                form={"creation_id": container})
        media = done.get("id")
        if not isinstance(media, (str, int)) or not str(media).strip():
            raise _unavailable("Instagram answered the publish step without a media id - "
                               "check the account before trying again",
                               container_id=container, image_url=image_url)
        media = str(media)
        _log.info("instagram: published media %s", media)
        result = {"ok": True, "media_id": media, "permalink": None, "image_url": image_url,
                  "card_sha": sha}
        # The post is live from here on: a failure to read its address is a note, not a
        # failure - reporting "failed" now would invite a second, duplicate post.
        try:
            info = self._graph_call("GET", f"/{urllib.parse.quote(media, safe='')}",
                                    "permalink", secrets, access,
                                    query={"fields": "permalink"})
            link = info.get("permalink")
            result["permalink"] = link if isinstance(link, str) else None
        except _Failure as failure:
            result["permalink_error"] = failure.output.get("error")
        return result

    def _wait_for(self, container: str, secrets: list[str], access: str,
                  image_url: str) -> None:
        """Until Instagram has fetched and processed the image, or it says it cannot."""
        waited = 0.0
        path = f"/{urllib.parse.quote(container, safe='')}"
        while True:
            state = self._graph_call("GET", path, "status check", secrets, access,
                                     query={"fields": "status_code"})
            code = str(state.get("status_code") or "").upper()
            if code == "FINISHED":
                return
            if code in ("ERROR", "EXPIRED", "PUBLISHED"):
                why = (f"Instagram could not use the image: the container ended {code}; "
                       "nothing was published")
                raise _refused(why, container_status=code, container_id=container,
                               image_url=image_url)
            if waited >= self.settings.poll_limit_seconds:
                raise _unavailable(
                    f"Instagram was still processing the image after "
                    f"{self.settings.poll_limit_seconds:.0f} s; nothing was published",
                    container_status=code or None, container_id=container,
                    image_url=image_url)
            self._sleep(self.settings.poll_seconds)
            waited += self.settings.poll_seconds

    # ---- the token refresh ------------------------------------------------------------
    def _fresh_token(self, ig: InstagramToken, secrets: list[str]) -> str:
        """The token to post with: refreshed first if it is over 7 days old. A failed
        refresh does not block the post - the old token is used, and says so if dead."""
        now = self._clock()
        if ig.refreshed_at is not None:
            age = now - ig.refreshed_at
            if age < REFRESH_AFTER or age < REFRESH_MIN_AGE:
                return ig.access_token
        query = urllib.parse.urlencode({"grant_type": "ig_refresh_token",
                                        "access_token": ig.access_token})
        status, document = self._http("GET", f"{self.settings.refresh_url}?{query}", None, {})
        fresh = document.get("access_token") if isinstance(document, Mapping) else None
        if not (200 <= status < 300) or not isinstance(fresh, str) or not fresh.strip():
            why = self._graph_reason(status, document, secrets)
            _log.warning("instagram: token refresh failed (%s); posting with the current token",
                         self._scrub(why, secrets))
            return ig.access_token
        fresh = fresh.strip()
        secrets.append(fresh)
        try:
            current = json.loads(self.settings.token_file.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            current = {}
        if not isinstance(current, dict):
            current = {}
        current.update({"access_token": fresh, "user_id": ig.user_id,
                        "username": ig.username, "refreshed_at": _iso(now)})
        try:
            _write_token_file(self.settings.token_file, current)
        except OSError as error:
            _log.warning("instagram: token refreshed but could not be saved (%s); posting "
                         "with it anyway", type(error).__name__)
            return fresh
        expires = document.get("expires_in") if isinstance(document, Mapping) else None
        days = f"{int(expires) // 86400} days" if isinstance(expires, int) else "unknown"
        _log.info("instagram: token refreshed (expires in %s)", days)
        return fresh

    # ---- Scrooge ------------------------------------------------------------------------
    def _upload(self, image: bytes, sha: str, publish: str, secrets: list[str]) -> str:
        status, document = self._http(
            "POST", f"{self._scrooge}/dash/content/media", image,
            {"Content-Type": "image/jpeg", "x-dash-token": publish})
        said = ""
        if isinstance(document, Mapping) and document.get("error"):
            said = self._scrub(str(document.get("error")), secrets)[:300]
        if status == 0:
            raise _unavailable(f"Scrooge is unreachable at {self._scrooge}")
        if status in (401, 403):
            raise _unavailable("the publish token was rejected by Scrooge's media upload")
        if status == 413:
            raise _refused("Scrooge refused the card: too big for its media limit", status=413)
        if status == 400:
            raise _refused(said or "Scrooge refused the card (HTTP 400)", status=400)
        if not 200 <= status < 300:
            raise _unavailable(f"Scrooge answered HTTP {status} to the media upload"
                               + (f": {said}" if said else ""), status=status)
        if not isinstance(document, Mapping) or document.get("ok") is not True:
            raise _unavailable("Scrooge answered the media upload without ok: true")
        stored = document.get("sha")
        url = document.get("url")
        if stored != sha:
            # Scrooge holds different bytes from the card that was approved: Instagram
            # would fetch a picture nobody has seen. Stop here.
            raise _refused("Scrooge stored different bytes from the card that was sent "
                           "(sha mismatch); nothing was posted", card_sha=sha)
        if not isinstance(url, str) or not url.startswith("https://") or sha not in url:
            raise _refused("Scrooge returned an image URL that is not https or does not "
                           "name the uploaded card; nothing was posted", card_sha=sha)
        return url

    # ---- Instagram Graph ----------------------------------------------------------------
    def _graph_call(self, method: str, path: str, step: str, secrets: list[str],
                    access: str, *, form: Mapping[str, str] | None = None,
                    query: Mapping[str, str] | None = None) -> Mapping[str, Any]:
        """One Graph call. POSTs carry the token as a form field, GETs as a query param."""
        url = f"{self._graph}{path}"
        data = None
        headers: dict[str, str] = {}
        if method == "GET":
            url += "?" + urllib.parse.urlencode({**(query or {}), "access_token": access})
        else:
            data = urllib.parse.urlencode({**(form or {}), "access_token": access}).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        status, document = self._http(method, url, data, headers)
        if 200 <= status < 300 and isinstance(document, Mapping) and "error" not in document:
            return document
        raise self._graph_failure(status, document, step, secrets)

    def _graph_reason(self, status: int, document: Any, secrets: list[str]) -> str:
        error = document.get("error") if isinstance(document, Mapping) else None
        if isinstance(error, Mapping) and error.get("message"):
            return self._scrub(str(error.get("message")), secrets)[:300]
        if status == 0:
            return "no answer"
        return f"HTTP {status}"

    def _graph_failure(self, status: int, document: Any, step: str,
                       secrets: list[str]) -> _Failure:
        error = document.get("error") if isinstance(document, Mapping) else None
        error = error if isinstance(error, Mapping) else {}
        code = error.get("code")
        reason = self._graph_reason(status, document, secrets)
        extra: dict[str, Any] = {"step": step, "status": status}
        if isinstance(code, int):
            extra["graph_code"] = code
        if code == 190:
            return _unavailable(TOKEN_REJECTED, token_rejected=True, **extra)
        if status == 0:
            host = urllib.parse.urlparse(self._graph).netloc
            return _unavailable(f"Instagram is unreachable at {host} ({step})", **extra)
        if (status == 429 or code in _RATE_LIMIT_CODES or error.get("is_transient") is True):
            return _unavailable(f"Instagram asked to wait ({step}): {reason}", **extra)
        if 400 <= status < 500:
            return _refused(reason, **extra)
        if 200 <= status < 300:
            return _unavailable(f"Instagram answered the {step} step with something that "
                                "is not a result", **extra)
        return _unavailable(f"Instagram answered HTTP {status} ({step}): {reason}", **extra)

    # ---- transport ----------------------------------------------------------------------
    def _http(self, method: str, url: str, data: bytes | None,
              headers: Mapping[str, str]) -> tuple[int, Any]:
        """(HTTP status, parsed JSON or None). Status 0 means nothing answered."""
        request = urllib.request.Request(
            url, data=data, method=method,
            headers={"Accept": "application/json", "User-Agent": "pionir-instagram/0.1",
                     **headers})
        try:
            # timeout by keyword: OpenerDirector.open(url, data=None, timeout=...) takes a
            # positional second argument as the POST body.
            with self._open(request, timeout=self.settings.timeout_seconds) as response:
                return int(getattr(response, "status", 200)), self._json(response)
        except urllib.error.HTTPError as error:
            return error.code, self._json(error)
        except (urllib.error.URLError, TimeoutError, OSError):
            # the exception text is not carried: it could echo the URL, and a GET's URL
            # carries the token
            return 0, None

    @staticmethod
    def _json(response: Any) -> Any:
        try:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, ValueError):
            return None
        if len(raw) > MAX_RESPONSE_BYTES:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None

    @staticmethod
    def _scrub(text: str, secrets: list[str]) -> str:
        for secret in secrets:
            if secret:
                text = text.replace(secret, "<redacted>")
                quoted = urllib.parse.quote_plus(secret)
                if quoted != secret:
                    text = text.replace(quoted, "<redacted>")
        return text

    def _result(self, task: Task, output: dict[str, Any]) -> TaskResult:
        evidence = ["instagram:post"]
        if output.get("ok") is True and output.get("media_id"):
            evidence.append(f"instagram:media:{output['media_id']}")
        return TaskResult(task_id=task.task_id, agent_id=self.manifest.agent_id,
                          output=output, evidence=tuple(evidence))

    # ---- the account check (python -m pionir instagram-check) --------------------------
    def check_account(self) -> tuple[int, dict[str, Any]]:
        """Who the token is for and how much of today's posting quota is used. Reads only:
        never posts, never refreshes, never shows the token. (0 ok, 1 not configured or
        not checked, 2 rejected)."""
        ig = read_instagram_token(self.settings.token_file)
        if ig is None:
            return 1, {"status": "not_configured",
                       "message": f"no usable Instagram token at {self.settings.token_file} "
                                  f"- {SETUP_HINT}"}
        secrets = [ig.access_token]
        try:
            me = self._graph_call("GET", "/me", "account check", secrets, ig.access_token,
                                  query={"fields": "user_id,username"})
        except _Failure as failure:
            out = failure.output
            if out.get("token_rejected") or out.get("status") in (401, 403):
                return 2, {"status": "rejected", "message": TOKEN_REJECTED}
            return 1, {"status": "not_checked", "message": out.get("error")}
        user_id = str(me.get("user_id") or me.get("id") or ig.user_id)
        report: dict[str, Any] = {"status": "ok", "username": me.get("username"),
                                  "user_id": user_id, "graph": self._graph}
        if user_id != ig.user_id:
            report["warning"] = (f"the token belongs to user {user_id}, but the token file "
                                 f"says {ig.user_id} - {SETUP_HINT}")
        try:
            limit = self._graph_call(
                "GET", f"/{urllib.parse.quote(user_id, safe='')}/content_publishing_limit",
                "quota check", secrets, ig.access_token,
                query={"fields": "quota_usage,config"})
        except _Failure as failure:
            if failure.output.get("token_rejected"):
                return 2, {"status": "rejected", "message": TOKEN_REJECTED}
            report["quota"] = None
            report["quota_error"] = failure.output.get("error")
            return 0, json.loads(self._scrub(json.dumps(report), secrets))
        data = limit.get("data")
        row = data[0] if isinstance(data, list) and data and isinstance(data[0], Mapping) \
            else {}
        config = row.get("config") if isinstance(row.get("config"), Mapping) else {}
        report["quota"] = {"used": row.get("quota_usage"), "total": config.get("quota_total"),
                           "window_seconds": config.get("quota_duration")}
        return 0, json.loads(self._scrub(json.dumps(report), secrets))
