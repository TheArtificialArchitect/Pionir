"""Adapter for publishing: a post goes live on the public blog only after the owner's yes.

Scrooge (the Dokaz API at ``api.dokaz.net``) serves the blog; this adapter hands it a
finished draft. Ian's rule, the same as for money: nothing is published without his
approval, and holding a permission is not a way around it. So:

- ``content.publish`` is PRIVILEGED with ``requires_approval=True``: PionirApp parks it on
  EVERY call, and the Discord card shows the whole post (title, URL, description, the
  full body) before he answers. It is ``routable=False`` - reached only by name.
- ``content.unpublish`` is PRIVILEGED but does NOT require approval: pulling a post down
  must be fast, and it is the safe direction. A caller holding ``content.unpublish``
  runs it at once; one without the permission is parked like any privileged action.

A draft is checked here, with the same rules Scrooge applies, before anything is sent
(or parked): a bad draft is refused by Pionir with ``AdapterProtocolError`` naming the
field and why, in Scrooge's ``<field>: <why>`` shape. The rules are deliberately never
looser than Scrooge's - at worst stricter, which only means a draft is fixed sooner.

The publish token is read from a file on every call (so adding it needs no restart),
sent only in the ``x-dash-token`` header, and never appears in a log, an error or a
result. A missing token, a rejected token, or Scrooge being unreachable comes back as
``ok: false`` with ``unavailable`` - an ordinary state, not a fault of the doer, so it
does not trip the circuit breaker and then hide the plain answer. Scrooge saying no to
the post itself (a taken slug, a field it rejects) comes back as ``ok: false`` with
``refused``: the other side said no.
"""

from __future__ import annotations

import ipaddress
import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pionir.contracts import AgentManifest, Capability, RiskLevel, Task, TaskResult
from pionir.errors import AdapterProtocolError, AdapterUnavailable

DEFAULT_CONTENT_URL = "https://api.dokaz.net"
# Where a published post lives. The Discord card shows this so the owner sees the
# address a post will have before he approves it.
PUBLIC_BLOG_BASE = "https://api.dokaz.net/blog/"
MAX_RESPONSE_BYTES = 1_000_000

PUBLISH = "content.publish"
UNPUBLISH = "content.unpublish"
PUBLISH_FIELDS = frozenset({"draft_id", "slug", "title", "description", "body_md", "tags"})

# ---- the draft rules (mirror Scrooge's /dash/content/publish) ----------------------
_DRAFT_ID = re.compile(r"[a-z0-9-]{1,64}")
_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]{1,78})[a-z0-9]")
_TAG = re.compile(r"[a-z0-9-]{1,24}")
LENGTHS = {"title": (10, 120), "description": (50, 300), "body_md": (300, 30_000)}
MAX_TAGS = 8
ALLOWED_LINK_HOSTS = frozenset({
    "api.dokaz.net", "dokazindustries.com", "www.dokazindustries.com", "dokaz.gumroad.com",
})

# Any tag-like `<...>`: an element, a closing tag, and an autolink `<https://...>` (write
# links as [text](url) instead). Stricter than a pure HTML check on purpose.
_HTML_TAG = re.compile(r"</?[A-Za-z][^<>]*>")
_HTML_COMMENT = re.compile(r"<!--")
_HTML_DECL = re.compile(r"<[!?]")
_BAD_SCHEME = re.compile(r"\b(?:javascript|vbscript|data)\s*:", re.IGNORECASE)
# Every scheme-qualified URL, every Markdown link / image target, every reference
# definition, and bare www. addresses (GFM links those too).
_SCHEMED_URL = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s<>()\[\]\"'`]+")
_LINK_TARGET = re.compile(r"\]\(\s*<?([^\s)>]*)")
_REFERENCE_DEF = re.compile(r"^\s{0,3}\[(?!\^)[^\]]+\]:\s*<?(\S*?)>?(?:\s|$)", re.MULTILINE)
_BARE_WWW = re.compile(r"(?<![\w./:@-])www\.[^\s<>()\[\]\"'`]+", re.IGNORECASE)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_PHONE = re.compile(
    r"(?<![\w.])(?:"
    r"\+\d[\d\s().-]{6,}\d"                                          # +1 555 123 4567
    r"|(?:\(\d{2,4}\)|\d{2,4})[\s.-]?\d{3,4}[\s.-]\d{3,4}"           # (555) 123-4567
    r")(?![\w.])"
)
_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
_IPV6_CANDIDATE = re.compile(r"(?<![\w:])[0-9A-Fa-f:]*:[0-9A-Fa-f:]*:[0-9A-Fa-f:.]*(?![\w:])")

# Mirrors of Scrooge's own refusals (worker/src/blog.ts) that the rules above let through, so
# nothing Pionir parks for the owner's yes can then bounce at the endpoint: an approval that
# cannot publish wastes his attention. Found by the end-to-end agreement run; rerun it if
# Scrooge's rules change.
_CONTROL_LINE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_CONTROL_BODY = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_INVISIBLE = re.compile("[\u200b\u200c\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
_OTHER_SCHEME = re.compile(r"\b(?:mailto|tel|file)\s*:(?=\S)", re.IGNORECASE)
_IMAGE = re.compile(r"!\[")
_FENCE = re.compile(r"^\s{0,3}```", re.MULTILINE)
_PHONES_STRICT = (
    re.compile(r"\+\d[\d\s().-]{6,20}\d"),
    re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]\d{4}(?!\d)"),
    re.compile(r"(?<![\d.])1?[2-9]\d{9}(?![\d.])"),
)

# RFC 2606 / 6761 reserve example.com, example.org, example.net and the .example TLD for
# documentation: an address there can never reach a person, so it is not personal data. A
# post about checking email addresses needs to show one; every other address still blocks.
# Mirrored in Pionir (adapters/content.py) and Scrooge (worker/src/blog.ts).
_RESERVED_MAIL = re.compile(r"(?i)@(?:[a-z0-9-]+\.)*(?:example\.(?:com|org|net)|[a-z0-9-]+\.example)$")


def reserved_email(address: str) -> bool:
    return _RESERVED_MAIL.search(address) is not None


READ_TIMEOUT_SECONDS = 30


def public_url(slug: str) -> str:
    """The public address a post with this slug will have."""
    return f"{PUBLIC_BLOG_BASE}{slug}"


def _is_ipv6(candidate: str) -> bool:
    if candidate.count(":") < 2 or not any(ch.isalnum() for ch in candidate):
        return False
    try:
        return isinstance(ipaddress.ip_address(candidate.strip(".")), ipaddress.IPv6Address)
    except ValueError:
        return False


def _link_problem(url: str) -> str | None:
    """Why this link target is not allowed, or None if it is."""
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https":
        # only the scheme is echoed: a mailto: target is someone's address
        this = f"{parsed.scheme}:" if parsed.scheme else "a relative link"
        return f"links must be https: to an allowed host, not {this}"
    host = (parsed.hostname or "").lower()
    if parsed.username or parsed.password or parsed.port is not None:
        return f"links cannot carry credentials or a port ({host or url[:80]!r})"
    if host not in ALLOWED_LINK_HOSTS:
        return (f"links may only go to {', '.join(sorted(ALLOWED_LINK_HOSTS))}; "
                f"not {host or url[:80]!r}")
    return None


def _contact_problem(text: str) -> str | None:
    """No email address, phone number or IP address anywhere in published text. The
    match itself is not echoed: it may be someone's contact details."""
    if any(not reserved_email(m.group(0)) for m in _EMAIL.finditer(text)):
        return "no email addresses are allowed (except at example.com, .org, .net)"
    if _IPV4.search(text) or any(_is_ipv6(m.group(0)) for m in _IPV6_CANDIDATE.finditer(text)):
        return "no IP addresses are allowed"
    if _PHONE.search(text):
        return "no phone numbers are allowed"
    return None


def _text_problem(text: str) -> str | None:
    """Markdown only, safe links, no contact details - for every published text field."""
    if _HTML_COMMENT.search(text):
        return "HTML comments are not allowed"
    if _HTML_DECL.search(text) or _HTML_TAG.search(text):
        return "raw HTML tags are not allowed (Markdown only; write links as [text](url))"
    if _BAD_SCHEME.search(text):
        return "javascript:, data: and vbscript: are not allowed"
    targets = [m.group(0) for m in _SCHEMED_URL.finditer(text)]
    targets += [m.group(1) for m in _LINK_TARGET.finditer(text)]
    targets += [m.group(1) for m in _REFERENCE_DEF.finditer(text)]
    for target in targets:
        problem = _link_problem(target.strip())
        if problem:
            return problem
    for match in _BARE_WWW.finditer(text):
        return f"write a bare address as an https: link, not {match.group(0)[:80]!r}"
    return _contact_problem(text)


def _scrooge_problem(key: str, text: str) -> str | None:
    """What Scrooge's endpoint would refuse that `_text_problem` lets through."""
    if (_CONTROL_BODY if key == "body_md" else _CONTROL_LINE).search(text):
        return "contains a control character" + ("" if key == "body_md" else " or line break")
    if _INVISIBLE.search(text):
        return "contains an invisible direction or zero-width character"
    if _OTHER_SCHEME.search(text):
        return "mailto:, tel: and file: are not allowed"
    if _IMAGE.search(text):
        return "images are not supported"
    if key == "body_md" and len(_FENCE.findall(text)) % 2:
        return "a ``` code fence is not closed"
    for pattern in _PHONES_STRICT:
        if any(sum(c.isdigit() for c in m.group(0)) >= 8 for m in pattern.finditer(text)):
            return "no phone numbers are allowed"
    return None


def _require_str(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        # ValueError like every other draft rule: one "<field>: <why>" refusal type
        raise ValueError(f"{key}: required, a string")  # noqa: TRY004
    return value


def check_slug(value: Any) -> str:
    if not isinstance(value, str) or not _SLUG.fullmatch(value):
        raise ValueError("slug: 3-80 of a-z, 0-9 and '-', not starting or ending with '-'")
    problem = _contact_problem(value)
    if problem:
        raise ValueError(f"slug: {problem}")
    return value


def check_draft(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The draft exactly as it will be sent, or ValueError("<field>: <why>")."""

    unknown = sorted(set(payload) - PUBLISH_FIELDS)
    if unknown:
        raise ValueError(f"{unknown[0]}: not a publish field (allowed: "
                         f"{', '.join(sorted(PUBLISH_FIELDS))})")
    draft_id = _require_str(payload, "draft_id")
    if not _DRAFT_ID.fullmatch(draft_id):
        raise ValueError("draft_id: 1-64 of a-z, 0-9 and '-'")
    body: dict[str, Any] = {"draft_id": draft_id, "slug": check_slug(payload.get("slug"))}
    for key, (low, high) in LENGTHS.items():
        text = _require_str(payload, key)
        if not low <= len(text) <= high:
            raise ValueError(f"{key}: {low}-{high} characters (this is {len(text)})")
        problem = _text_problem(text) or _scrooge_problem(key, text)
        if problem:
            raise ValueError(f"{key}: {problem}")
        body[key] = text
    if "tags" in payload and payload["tags"] is not None:
        tags = payload["tags"]
        if not isinstance(tags, list) or len(tags) > MAX_TAGS:
            raise ValueError(f"tags: a list of at most {MAX_TAGS}")
        for tag in tags:
            if not isinstance(tag, str) or not _TAG.fullmatch(tag):
                raise ValueError("tags: each 1-24 of a-z, 0-9 and '-'")
            problem = _contact_problem(tag)
            if problem:
                raise ValueError(f"tags: {problem}")
        body["tags"] = list(tags)
    return body


def read_token(path: Path) -> str | None:
    """The publish token, or None. Never logged, never returned anywhere else."""
    try:
        token = path.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return None
    return token or None


# ---- settings and transport ---------------------------------------------------------
Opener = Callable[[urllib.request.Request, float], Any]


@dataclass(frozen=True, slots=True)
class ContentSettings:
    """Where Scrooge is and where the publish token lives (its PATH, never the token)."""

    base_url: str = DEFAULT_CONTENT_URL
    token_file: Path = Path("~/.pionir/secrets/scrooge-publish-token.txt")
    timeout_seconds: int = READ_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        # The token is a secret: it only ever travels over TLS, or to this machine.
        if not (parsed.scheme == "https" or (parsed.scheme == "http" and loopback)):
            raise ValueError("the content URL must be https: (or http: on loopback)")
        if not parsed.hostname:
            raise ValueError("the content URL needs a host")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("the content URL cannot carry credentials or query data")
        if self.timeout_seconds < 5:
            raise ValueError("the content timeout must be at least 5 seconds")
        object.__setattr__(self, "token_file", Path(self.token_file).expanduser())


class ContentAdapter:
    """Publishing to the public blog as gated, audited Pionir capabilities."""

    def __init__(self, settings: ContentSettings | None = None, *,
                 opener: Opener | None = None) -> None:
        self.settings = settings or ContentSettings()
        self._base = self.settings.base_url.rstrip("/")
        self._open = opener or urllib.request.build_opener().open
        self._manifest = AgentManifest(
            agent_id="content",
            version="pionir/content",
            capabilities=(
                Capability(
                    name=PUBLISH,
                    description="Publish a finished post to the public Dokaz blog "
                                "(only after the owner approves it)",
                    risk=RiskLevel.PRIVILEGED,
                    required_permissions=frozenset({PUBLISH}),
                    requires_approval=True,
                    routable=False,
                ),
                Capability(
                    name=UNPUBLISH,
                    description="Take a post down from the public Dokaz blog",
                    risk=RiskLevel.PRIVILEGED,
                    required_permissions=frozenset({UNPUBLISH}),
                    routable=False,
                ),
            ),
        )

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def status(self) -> Mapping[str, Any]:
        """Local only - no call to Scrooge, so doctor stays hermetic and fast."""
        if read_token(self.settings.token_file) is None:
            raise AdapterUnavailable(self._not_configured())
        return {"url": self._base, "token": "configured"}

    def _not_configured(self) -> str:
        return (f"not configured: no publish token at {self.settings.token_file} - "
                "put Scrooge's publish token in that file")

    # ---- the request ---------------------------------------------------------------
    def validate(self, task: Task) -> None:
        """Refuse a bad draft, or a publish that cannot run, before it is parked."""
        self._request(task)
        if task.capability == PUBLISH and read_token(self.settings.token_file) is None:
            # Asking the owner to approve a post that cannot be sent wastes his yes.
            raise AdapterUnavailable(self._not_configured())

    @staticmethod
    def _request(task: Task) -> tuple[str, dict[str, Any]]:
        try:
            if task.capability == PUBLISH:
                return "/dash/content/publish", check_draft(task.payload)
            if task.capability == UNPUBLISH:
                extra = sorted(set(task.payload) - {"slug"})
                if extra:
                    raise ValueError(f"{extra[0]}: not an unpublish field (only slug)")
                return "/dash/content/unpublish", {"slug": check_slug(task.payload.get("slug"))}
        except ValueError as error:
            raise AdapterProtocolError(f"{task.capability} refused by Pionir - {error}") from error
        raise AdapterProtocolError(f"content has no capability {task.capability!r}")

    # ---- the call ------------------------------------------------------------------
    def execute(self, task: Task) -> TaskResult:
        path, body = self._request(task)
        token = read_token(self.settings.token_file)
        if token is None:
            why = self._not_configured()
            return self._result(task, {"ok": False, "unavailable": why, "error": why,
                                       "not_configured": True})
        status, document = self._post(path, body, token)
        if 200 <= status < 300:
            if document.get("ok") is not True:
                raise AdapterProtocolError(f"Scrooge answered {path} without ok: true")
            return self._result(task, dict(document))
        reason = self._scrub(str(document.get("error") or f"HTTP {status}"), token)
        if status in (400, 409):
            # Scrooge said no to this post (a field, a taken slug): an answer, not a fault.
            return self._result(task, {"ok": False, "refused": reason, "error": reason,
                                       "status": status})
        if status in (401, 403):
            why = "the publish token was rejected"
        elif status == 0:
            why = f"Scrooge is unreachable at {self._base}"
        else:
            why = f"Scrooge answered HTTP {status}: {reason}"
        return self._result(task, {"ok": False, "unavailable": why, "error": why,
                                   "status": status})

    def _post(self, path: str, body: dict[str, Any], token: str) -> tuple[int, Mapping[str, Any]]:
        """(HTTP status, JSON object). Status 0 means nothing answered."""
        request = urllib.request.Request(
            f"{self._base}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "User-Agent": "pionir-content/0.1", "x-dash-token": token},
            method="POST",
        )
        try:
            with self._open(request, self.settings.timeout_seconds) as response:
                return int(getattr(response, "status", 200)), self._json(response)
        except urllib.error.HTTPError as error:
            try:
                return error.code, self._json(error)
            except AdapterProtocolError:
                return error.code, {}
        except (urllib.error.URLError, TimeoutError, OSError):
            # the exception text is not carried: it could echo the request
            return 0, {}

    @staticmethod
    def _json(response: Any) -> Mapping[str, Any]:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise AdapterProtocolError("Scrooge's response exceeded the size limit")
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AdapterProtocolError("Scrooge returned invalid JSON") from error
        if not isinstance(document, dict):
            raise AdapterProtocolError("Scrooge returned a non-object response")
        return document

    @staticmethod
    def _scrub(text: str, token: str) -> str:
        return text.replace(token, "<redacted>") if token else text

    def _result(self, task: Task, output: dict[str, Any]) -> TaskResult:
        evidence = [f"content:{task.capability.split('.', 1)[1]}"]
        if output.get("ok") is True and output.get("slug"):
            evidence.append(f"content:slug:{output.get('slug')}")
        return TaskResult(task_id=task.task_id, agent_id=self.manifest.agent_id,
                          output=output, evidence=tuple(evidence))
