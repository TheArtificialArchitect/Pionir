"""Adapter for dev.to: a blog post already live on api.dokaz.net is republished on dev.to,
only after the owner's yes.

The same rule as the blog and Instagram: nothing goes public without Ian's approval, and
holding a permission is not a way around it. ``content.crosspost_devto`` is PRIVILEGED with
``requires_approval=True`` (PionirApp parks it on EVERY call; the Discord card shows the
canonical URL, the title, the tags and the whole body) and ``routable=False`` (reached only
by name).

The payload is the blog's own shape, ``{draft_id, slug, title, description, body_md,
tags}``, checked with the blog's rules (``content.check_draft``) so the dev.to copy can
carry nothing the blog could not, plus what dev.to needs or would do differently:

- 1-4 tags, each ``[a-z0-9]{1,30}`` (dev.to tags are alphanumeric);
- the body ends with the attribution line, ``*Originally published at
  [api.dokaz.net](https://api.dokaz.net/blog/<slug>?utm_source=devto&...)*``, for THIS slug;
- every link to a Dokaz page carries ``utm_source=devto``, never the blog's ``blog``;
- nothing dev.to renders that the blog shows as text: no front matter (dev.to would read
  its own title, tags, ``published`` or ``canonical_url`` from it), no Liquid tags (``{% %}``
  embeds, ``{{ }}``), and no ``@handles`` (dev.to turns one into a mention that notifies
  that person).

It is checked before anything is parked, and again before anything is sent. Once approved:

1. the original must be live: ``GET <content_url>/blog/<slug>`` answers 200, or nothing is
   cross-posted;
2. never twice: ``<state_root>/devto/posts.json`` maps each draft_id already cross-posted
   to its dev.to article, and a draft_id in it is refused with the article's URL;
3. ``POST <devto_url>/articles`` with ``published: true`` and a ``canonical_url`` built
   HERE from the slug (never taken from the payload), so search engines credit the blog;
4. the ledger is written (atomically) as soon as dev.to says the article exists.

The API key is read from a file on every call (so adding it needs no restart), sent only
in the ``api-key`` header, and never appears in a log, an error or a result. dev.to saying
no to the article comes back as ``ok: false`` with ``refused``; a missing or rejected key,
a rate limit, or dev.to not answering as ``ok: false`` with ``unavailable`` - answers, not
faults, so the circuit breaker is not tripped.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pionir import atomic
from pionir.adapters.content import (
    _EMAIL,
    _LINK_TARGET,
    _REFERENCE_DEF,
    _SCHEMED_URL,
    ALLOWED_LINK_HOSTS,
    DEFAULT_CONTENT_URL,
    PUBLISH_FIELDS,
    _contact_problem,
    check_draft,
    public_url,
    read_token,
)
from pionir.contracts import AgentManifest, Capability, RiskLevel, Task, TaskResult
from pionir.errors import AdapterProtocolError, AdapterUnavailable

_log = logging.getLogger(__name__)

CROSSPOST = "content.crosspost_devto"
DEFAULT_DEVTO_URL = "https://dev.to/api"
FOREM_ACCEPT = "application/vnd.forem.api-v1+json"
# Contains "bot", so Scrooge's traffic counter (CRAWLER = /bot|crawl|spider|slurp|preview/i)
# never counts the liveness check as a visitor.
LIVENESS_AGENT = "PionirBot/0.1 (liveness check before a dev.to cross-post; not a visitor)"
MAX_RESPONSE_BYTES = 1_000_000
SETUP_HINT = r"run tools\setup-devto.ps1"
KEY_REJECTED = f"the dev.to API key was rejected - {SETUP_HINT}"
NOT_LIVE = "the blog post is not live, so there is nothing to cross-post"

UTM_SOURCE = "devto"
UTM_MEDIUM = "referral"
UTM_CAMPAIGN_MAX = 40
MIN_TAGS, MAX_TAGS = 1, 4
_TAG = re.compile(r"[a-z0-9]{1,30}")
# dev.to reads a body that opens with a --- block as front matter, and its values
# (title, tags, published, canonical_url) override the article's own.
_FRONT_MATTER = re.compile(r"\A\s*---")
_LIQUID = re.compile(r"\{%|\{\{")
# Mirrors the crew's content check: an @name outside an email address.
_HANDLE = re.compile(r"(?<![\w@])@[A-Za-z0-9_][A-Za-z0-9_.]*")


def utm_campaign(draft_id: str) -> str:
    """The post's campaign: the first 40 characters of its draft_id, with no leading or
    trailing '-'. Mirrors ``pionir.crew.contentcheck.utm_campaign`` so the crew that writes
    the attribution line and the check that verifies it can never disagree."""
    return str(draft_id or "")[:UTM_CAMPAIGN_MAX].strip("-")


def attribution_line(slug: str, draft_id: str) -> str:
    """The line a dev.to body must end with: the original's address, tagged for dev.to."""
    query = (f"utm_source={UTM_SOURCE}&utm_medium={UTM_MEDIUM}"
             f"&utm_campaign={utm_campaign(draft_id)}")
    return f"*Originally published at [api.dokaz.net]({public_url(slug)}?{query})*"


def _links(text: str) -> list[str]:
    found = [m.group(0) for m in _SCHEMED_URL.finditer(text)]
    found += [m.group(1) for m in _LINK_TARGET.finditer(text)]
    found += [m.group(1) for m in _REFERENCE_DEF.finditer(text)]
    return [f.strip() for f in found if f.strip()]


def _utm_problem(text: str) -> str | None:
    """Every link to a Dokaz page carries utm_source=devto. check_draft has already
    confined links to the Dokaz hosts; an API endpoint (/v1/) is not a page."""
    for url in _links(text):
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        if host not in ALLOWED_LINK_HOSTS:
            continue
        if host == "api.dokaz.net" and parts.path.startswith("/v1/"):
            continue
        source = urllib.parse.parse_qs(parts.query, keep_blank_values=True).get("utm_source")
        if source != [UTM_SOURCE]:
            said = f"utm_source={source[0]}" if source and len(source) == 1 else \
                "no single utm_source"
            return (f"every link to a Dokaz page must carry utm_source={UTM_SOURCE}; "
                    f"{url[:120]!r} has {said}")
    return None


def _devto_problem(key: str, text: str) -> str | None:
    """What dev.to would render that the blog shows as plain text."""
    if key == "body_md" and _FRONT_MATTER.match(text):
        return "cannot open with --- (dev.to reads it as front matter)"
    if _LIQUID.search(text):
        return "cannot carry {% %} or {{ }} (dev.to runs them as Liquid tags)"
    handle = _HANDLE.search(_EMAIL.sub(" ", text))
    if handle:
        return (f"cannot carry an @-handle ({handle.group(0)[:40]!r}): dev.to turns it into "
                "a mention that notifies that person")
    return None


def check_crosspost(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The article exactly as it will be sent, or ValueError("<field>: <why>")."""
    unknown = sorted(set(payload) - PUBLISH_FIELDS)
    if unknown:
        raise ValueError(f"{unknown[0]}: not a cross-post field (allowed: "
                         f"{', '.join(sorted(PUBLISH_FIELDS))})")
    missing = sorted(PUBLISH_FIELDS - set(payload))
    if missing:
        raise ValueError(f"{missing[0]}: required")
    # The blog's rules for everything but the tags: the dev.to copy can carry nothing
    # the blog could not.
    article = check_draft({k: v for k, v in payload.items() if k != "tags"})
    tags = payload.get("tags")
    if not isinstance(tags, list) or not MIN_TAGS <= len(tags) <= MAX_TAGS:
        raise ValueError(f"tags: a list of {MIN_TAGS}-{MAX_TAGS} (dev.to allows at most "
                         f"{MAX_TAGS})")
    for tag in tags:
        if not isinstance(tag, str) or not _TAG.fullmatch(tag):
            raise ValueError("tags: each 1-30 of a-z and 0-9 (dev.to tags are alphanumeric)")
        problem = _contact_problem(tag)
        if problem:
            raise ValueError(f"tags: {problem}")
    if len(set(tags)) != len(tags):
        raise ValueError("tags: each tag once")
    article["tags"] = list(tags)
    for key in ("title", "description", "body_md"):
        problem = _devto_problem(key, article[key]) or _utm_problem(article[key])
        if problem:
            raise ValueError(f"{key}: {problem}")
    if not utm_campaign(article["draft_id"]):
        raise ValueError("draft_id: has no usable campaign (it is all '-')")
    expected = attribution_line(article["slug"], article["draft_id"])
    body = article["body_md"].rstrip()
    last = body.rsplit("\n", 1)[-1].strip()
    if last != expected:
        raise ValueError(f"body_md: must end with the line {expected}")
    return article


# ---- settings ----------------------------------------------------------------------------
Opener = Callable[..., Any]


def _check_url(url: str, what: str) -> None:
    parsed = urllib.parse.urlparse(url)
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    # The key is a secret: it only ever travels over TLS, or to this machine.
    if not (parsed.scheme == "https" or (parsed.scheme == "http" and loopback)):
        raise ValueError(f"the {what} URL must be https: (or http: on loopback)")
    if not parsed.hostname:
        raise ValueError(f"the {what} URL needs a host")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"the {what} URL cannot carry credentials or query data")


@dataclass(frozen=True, slots=True)
class DevtoSettings:
    """Where dev.to and the blog are, where the API key lives (its PATH, never the key),
    and where the ledger of cross-posted drafts is kept."""

    api_url: str = DEFAULT_DEVTO_URL
    key_file: Path = Path("~/.pionir/secrets/devto-api-key.txt")
    ledger_file: Path = Path("~/.pionir/devto/posts.json")
    blog_url: str = DEFAULT_CONTENT_URL
    timeout_seconds: int = 30

    def __post_init__(self) -> None:
        _check_url(self.api_url, "dev.to API")
        _check_url(self.blog_url, "blog")
        if self.timeout_seconds < 5:
            raise ValueError("the dev.to timeout must be at least 5 seconds")
        object.__setattr__(self, "key_file", Path(self.key_file).expanduser())
        object.__setattr__(self, "ledger_file", Path(self.ledger_file).expanduser())


class _Failure(Exception):
    """One step's plain answer (already scrubbed), carried out of the step to execute()."""

    def __init__(self, output: dict[str, Any]) -> None:
        super().__init__(output.get("error"))
        self.output = output


def _refused(why: str, **extra: Any) -> _Failure:
    return _Failure({"ok": False, "refused": why, "error": why, **extra})


def _unavailable(why: str, **extra: Any) -> _Failure:
    return _Failure({"ok": False, "unavailable": why, "error": why, **extra})


# One cross-post at a time per ledger: two approvals of the same draft running together
# must not both find it absent and both post.
_LOCKS: dict[Path, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
# What this process has cross-posted, per ledger, in case the ledger file could not be
# written: a draft that went up is not posted again while Pionir runs, whatever the disk did.
_POSTED: dict[Path, dict[str, dict[str, Any]]] = {}


def _ledger_lock(path: Path) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(path.resolve(), threading.Lock())


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class DevtoAdapter:
    """Cross-posting a live blog post to dev.to as a gated, audited Pionir capability."""

    def __init__(self, settings: DevtoSettings | None = None, *,
                 opener: Opener | None = None,
                 clock: Callable[[], str] | None = None) -> None:
        self.settings = settings or DevtoSettings()
        self._api = self.settings.api_url.rstrip("/")
        self._blog = self.settings.blog_url.rstrip("/")
        self._open = opener or urllib.request.build_opener().open
        self._now = clock or _utc_now
        self._manifest = AgentManifest(
            agent_id="devto",
            version="pionir/devto",
            capabilities=(
                Capability(
                    name=CROSSPOST,
                    description="Republish a post already live on the Dokaz blog to dev.to "
                                "(only after the owner approves it)",
                    risk=RiskLevel.PRIVILEGED,
                    required_permissions=frozenset({CROSSPOST}),
                    requires_approval=True,
                    routable=False,
                ),
            ),
        )

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    # ---- configuration (local files only) --------------------------------------------
    def _not_configured(self) -> str:
        return f"not configured: no dev.to API key at {self.settings.key_file} - {SETUP_HINT}"

    def status(self) -> Mapping[str, Any]:
        """Local only - no call to dev.to or the blog, so doctor stays hermetic and fast."""
        if read_token(self.settings.key_file) is None:
            raise AdapterUnavailable(self._not_configured())
        try:
            posted: Any = len(self._read_ledger())
        except _Failure as failure:
            posted = failure.output.get("error")
        return {"api": self._api, "key": "configured", "cross_posted": posted}

    # ---- the request -----------------------------------------------------------------
    def validate(self, task: Task) -> None:
        """Refuse a bad article, or one that cannot run, before it is parked."""
        article = self._check(task)
        if read_token(self.settings.key_file) is None:
            # Asking the owner to approve a post that cannot be sent wastes his yes.
            raise AdapterUnavailable(self._not_configured())
        try:
            done = self._already(article["draft_id"])
        except _Failure as failure:
            raise AdapterUnavailable(str(failure.output.get("error"))) from None
        if done is not None:
            # Approving a second copy of an article that is already up wastes his yes.
            raise AdapterProtocolError(
                f"{CROSSPOST} refused by Pionir - draft_id: already cross-posted to dev.to "
                f"at {done.get('url')}")

    @staticmethod
    def _check(task: Task) -> dict[str, Any]:
        if task.capability != CROSSPOST:
            raise AdapterProtocolError(f"devto has no capability {task.capability!r}")
        try:
            return check_crosspost(task.payload)
        except ValueError as error:
            raise AdapterProtocolError(f"{CROSSPOST} refused by Pionir - {error}") from error

    # ---- the call --------------------------------------------------------------------
    def execute(self, task: Task) -> TaskResult:
        article = self._check(task)
        key = read_token(self.settings.key_file)
        if key is None:
            why = self._not_configured()
            return self._result(task, {"ok": False, "unavailable": why, "error": why,
                                       "not_configured": True})
        try:
            with _ledger_lock(self.settings.ledger_file):
                output = self._crosspost(article, key)
        except _Failure as failure:
            output = failure.output
        # Belt and braces: whatever went wrong, the key does not leave in the result.
        return self._result(task, json.loads(self._scrub(json.dumps(output), key)))

    def _crosspost(self, article: Mapping[str, Any], key: str) -> dict[str, Any]:
        draft_id = article["draft_id"]
        done = self._already(draft_id)
        if done is not None:
            raise _refused(f"already cross-posted to dev.to at {done.get('url')}",
                           url=done.get("url"), id=done.get("id"))

        slug = urllib.parse.quote(article["slug"], safe="")
        # "bot" in the agent: Scrooge's traffic counter skips it, so this liveness check is
        # never counted as a visitor (it was, in the end-to-end run: 8 views for 7 visits)
        status, _ = self._http("GET", f"{self._blog}/blog/{slug}", None,
                               {"Accept": "text/html", "User-Agent": LIVENESS_AGENT})
        if status != 200:
            said = f"HTTP {status}" if status else "no answer"
            raise _refused(f"{NOT_LIVE} ({self._blog}/blog/{article['slug']}: {said})",
                           live_status=status)

        canonical = public_url(article["slug"])    # built here, never from the payload
        body = {"article": {
            "title": article["title"],
            "body_markdown": article["body_md"],
            "published": True,
            "canonical_url": canonical,
            "description": article["description"],
            "tags": ",".join(article["tags"]),
        }}
        status, document = self._http(
            "POST", f"{self._api}/articles", json.dumps(body).encode("utf-8"),
            {"api-key": key, "Accept": FOREM_ACCEPT, "Content-Type": "application/json"})
        # Scrubbed before anything is kept: what dev.to says goes into the ledger too.
        document = json.loads(self._scrub(json.dumps(document), key))
        if 200 <= status < 300:
            return self._record(draft_id, canonical, document)
        raise self._failure(status, document, key)

    def _record(self, draft_id: str, canonical: str, document: Any) -> dict[str, Any]:
        """dev.to says the article exists: note it at once, so it is never posted twice."""
        doc = document if isinstance(document, Mapping) else {}
        article_id = doc.get("id")
        url = doc.get("url")
        usable = (isinstance(article_id, int) and not isinstance(article_id, bool)
                  and isinstance(url, str) and url.startswith("https://"))
        entry = {"id": article_id if usable else None, "url": url if usable else None,
                 "posted_at": self._now()}
        output: dict[str, Any]
        if usable:
            output = {"ok": True, "id": article_id, "url": url, "canonical_url": canonical}
        else:
            # It is probably live: recording it keeps a retry from posting it again.
            why = ("dev.to accepted the article but answered without its id and URL - it "
                   "may be live; check dev.to (the ledger now holds this draft)")
            output = {"ok": False, "unavailable": why, "error": why,
                      "canonical_url": canonical}
        self._posted()[draft_id] = entry
        try:
            self._write_ledger(draft_id, entry)
        except OSError as error:
            # The article is live: a failure to note it is a warning, not a failure -
            # reporting "failed" now would invite a second, duplicate post.
            output["ledger_error"] = (f"the article is live but could not be recorded in "
                                      f"{self.settings.ledger_file} ({type(error).__name__}); "
                                      "do not cross-post this draft again")
            _log.warning("devto: article for %s is live but the ledger write failed (%s)",
                         draft_id, type(error).__name__)
        else:
            _log.info("devto: cross-posted %s as article %s", draft_id, entry["id"])
        return output

    def _failure(self, status: int, document: Any, key: str) -> _Failure:
        said = ""
        if isinstance(document, Mapping) and document.get("error"):
            said = self._scrub(str(document.get("error")), key)[:300]
        if status in (401, 403):
            return _unavailable(KEY_REJECTED, status=status, key_rejected=True)
        if status in (400, 422):
            return _refused(said or f"dev.to refused the article (HTTP {status})",
                            status=status)
        if status == 429:
            return _unavailable("dev.to is rate limiting this account - try again later"
                                + (f": {said}" if said else ""), status=429)
        # Unanswered or 5xx: the article may or may not exist now.
        check = "; check dev.to before trying again"
        if status == 0:
            host = urllib.parse.urlparse(self._api).netloc
            return _unavailable(f"dev.to is unreachable at {host}{check}", status=0)
        return _unavailable(f"dev.to answered HTTP {status}" + (f": {said}" if said else "")
                            + (check if status >= 500 else ""), status=status)

    # ---- the ledger ------------------------------------------------------------------
    def _posted(self) -> dict[str, dict[str, Any]]:
        with _LOCKS_GUARD:
            return _POSTED.setdefault(self.settings.ledger_file.resolve(), {})

    def _already(self, draft_id: str) -> Mapping[str, Any] | None:
        """The article a draft was already cross-posted as (the ledger, or this process's
        memory of it), or None. Any entry counts, even an empty one."""
        done = self._read_ledger().get(draft_id)
        return done if done is not None else self._posted().get(draft_id)

    def _read_ledger(self) -> dict[str, Any]:
        path = self.settings.ledger_file
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as error:
            raise _unavailable(f"the dev.to ledger at {path} cannot be read "
                               f"({type(error).__name__})") from None
        try:
            document = json.loads(raw)
        except ValueError:
            document = None
        if not isinstance(document, dict) or not all(isinstance(v, dict)
                                                     for v in document.values()):
            # Unreadable means "might already be posted": stop rather than risk a twin.
            raise _unavailable(f"the dev.to ledger at {path} is not a JSON object of "
                               "draft_id -> {id, url, posted_at}; fix it before cross-posting")
        return document

    def _write_ledger(self, draft_id: str, entry: Mapping[str, Any]) -> None:
        """Add one entry, atomically: a crash leaves the old ledger or the new one."""
        path = self.settings.ledger_file
        try:
            current = self._read_ledger()
        except _Failure:
            current = {}
        current[draft_id] = dict(entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".posts-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(current, handle, indent=2, sort_keys=True)
            atomic.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # ---- transport ---------------------------------------------------------------------
    def _http(self, method: str, url: str, data: bytes | None,
              headers: Mapping[str, str]) -> tuple[int, Any]:
        """(HTTP status, parsed JSON or None). Status 0 means nothing answered."""
        request = urllib.request.Request(
            url, data=data, method=method,
            headers={"User-Agent": "pionir-devto/0.1", **headers})
        try:
            # timeout by keyword: OpenerDirector.open(url, data=None, timeout=...) takes a
            # positional second argument as the POST body.
            with self._open(request, timeout=self.settings.timeout_seconds) as response:
                return int(getattr(response, "status", 200)), self._json(response)
        except urllib.error.HTTPError as error:
            return error.code, self._json(error)
        except (urllib.error.URLError, TimeoutError, OSError):
            # the exception text is not carried: it could echo the request
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
    def _scrub(text: str, key: str) -> str:
        if not key:
            return text
        text = text.replace(key, "<redacted>")
        quoted = urllib.parse.quote_plus(key)
        return text.replace(quoted, "<redacted>") if quoted != key else text

    def _result(self, task: Task, output: dict[str, Any]) -> TaskResult:
        evidence = ["devto:crosspost"]
        if output.get("ok") is True and output.get("id") is not None:
            evidence.append(f"devto:article:{output['id']}")
        return TaskResult(task_id=task.task_id, agent_id=self.manifest.agent_id,
                          output=output, evidence=tuple(evidence))

    # ---- the account check (python -m pionir devto-check) ------------------------------
    def check_account(self) -> tuple[int, dict[str, Any]]:
        """Who the key belongs to. Reads only: never posts, never shows the key.
        (0 ok, 1 not configured or not checked, 2 rejected)."""
        key = read_token(self.settings.key_file)
        if key is None:
            return 1, {"status": "not_configured", "message": self._not_configured()}
        status, document = self._http("GET", f"{self._api}/users/me", None,
                                      {"api-key": key, "Accept": FOREM_ACCEPT})
        if status in (401, 403):
            return 2, {"status": "rejected", "message": KEY_REJECTED}
        if status != 200 or not isinstance(document, Mapping):
            why = self._failure(status, document, key).output.get("error") \
                if status != 200 else "dev.to answered without an account"
            return 1, {"status": "not_checked",
                       "message": self._scrub(str(why), key)}
        report = {"status": "ok", "username": document.get("username"),
                  "name": document.get("name"), "id": document.get("id"), "api": self._api}
        return 0, json.loads(self._scrub(json.dumps(report), key))
