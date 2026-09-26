"""Answer Pionir's approvals from Discord: one message per parked action, the
owner's ✅ or ❌ reaction is the yes or no.

The phone page stays the primary way in; this is a second window onto the SAME
queue, not a second queue. For every pending approval the bot posts one message
to a configured channel saying exactly what will happen (capability, the whole
command and payload, who asked, and any money it spends in bold at the top; a
``content.publish`` card opens with PUBLISHES PUBLICLY and shows the whole post; a
``social.instagram_post`` card opens with POSTS PUBLICLY TO INSTAGRAM, shows the full
caption and carries the rendered image itself as an attachment; a
``content.crosspost_devto`` card opens with CROSS-POSTS TO DEV.TO and shows the whole
article; a ``client.email`` card opens with EMAILS A CLIENT, names the recipient and shows
the whole message; a ``client.find_report`` card opens with SENDS A FIND REPORT, lists every
link it sends the client to with its domain in bold first, and shows the whole report; a
``client.deliver`` card opens with DELIVERS TO A CLIENT and shows the
zip's full file list as checked on disk, the secrets scan and the whole email; a
``product.gumroad_publish`` card opens with PUTS A PRODUCT ON SALE and the price, shows the
whole listing - the FULL description, the zip's file list, the secrets scan and any
executables it ships - and carries the cover image as an attachment),
adds ✅ and ❌ itself, and polls the reactions. Only the configured owner's
reaction counts; with no owner configured nothing can ever be approved here.

A ✅ goes through ``PionirApp.approve`` - the very call the phone's Approve
button makes - so the action runs exactly as it would from the phone: claimed
atomically (pending -> running) so it can never run twice, run as a job with the
permission it was parked with, and settled approved / approved_failed. A ❌ goes
through ``PionirApp.deny``. When the row settles, by whichever route, the
message is edited to say what happened, so the channel is an honest log.

Stdlib only, REST only (no gateway websocket), the same shape as Galatea's
Discord bridge: a rejected token is configuration, not weather - the loop stops
and says so once. A Discord outage never touches the queue; approvals keep
working from the phone. The bot token is read from a file, used only in the
Authorization header, and scrubbed from every error, log line and message.

Wiring (done elsewhere)::

    gate = DiscordGate.for_app(app, DiscordGateSettings.from_environment(state_root))
    gate.start()      # False (and state() says why) if not configured
    ...
    gate.stop()
"""
from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import atomic
from .adapters.clients import DELIVER as CLIENT_DELIVER
from .adapters.clients import EMAIL as CLIENT_EMAIL
from .adapters.clients import FIND_REPORT as CLIENT_FIND_REPORT
from .adapters.clients import LINK_PLACEHOLDER
from .adapters.content import PUBLISH, public_url
from .adapters.devto import CROSSPOST as DEVTO_CROSSPOST
from .adapters.instagram import POST as INSTAGRAM_POST
from .adapters.products import PUBLISH as PRODUCT_PUBLISH
from .adapters.products import price_text
from .social.card import render_card
from .social.post import full_caption

_log = logging.getLogger(__name__)

API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (pionir, 0.1)"
APPROVE = "\u2705"   # check mark
DENY = "\u274c"      # cross mark
MESSAGE_LIMIT = 2000
# Room kept free in the first message for the status line an edit adds on top
# ("APPROVED by ... - FAILED: <why>"), so resolving never has to cut the text
# that says what the action does.
STATUS_RESERVE = 500
MAX_RETRY_AFTER = 30.0
RETAIN_FINAL = timedelta(days=7)
# How long a client.deliver card's look at its zip is reused before looking again.
PREVIEW_SECONDS = 60.0
_SNOWFLAKE = re.compile(r"\d{5,25}")
_FENCE = "```"
_CONFIG_NAME = "config.json"
_STATE_NAME = "gate.json"

# Payload keys that carry an amount of money. A producer that spends money says
# so with one of these (a number, a string, or {"amount": ..., "currency": ...}),
# optionally with a sibling "currency"; ``spends_money: true`` with no amount is
# shown as an unstated amount, never as nothing.
MONEY_KEYS = ("spend", "amount", "cost", "price", "charge")
# Capability words that suggest money moves even when no amount was supplied.
MONEY_WORDS = frozenset({
    "buy", "purchase", "pay", "payment", "payout", "spend", "checkout", "charge",
    "order", "subscribe", "subscription", "invoice", "refund", "transfer",
})


# ---------------------------------------------------------------- settings
def _default_token_file() -> Path:
    return Path.home() / ".pionir" / "secrets" / "discord-bot-token.txt"


def _default_state_root() -> Path:
    raw = os.environ.get("PIONIR_STATE_ROOT", "").strip()
    return Path(raw) if raw else Path.home() / ".pionir"


def _first(environ: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = (environ.get(name) or "").strip()
        if value:
            return value
    return None


@dataclass(frozen=True)
class DiscordGateSettings:
    """Where the gate posts, whose reactions count, and where its secret lives.

    Holds the PATH of the token file, never the token. ``owner_user_id`` unset
    (or not a Discord id) means nothing can be approved from Discord.
    """

    state_root: Path
    channel_id: str | None = None
    owner_user_id: str | None = None
    token_file: Path = field(default_factory=_default_token_file)
    enabled: bool = True
    poll_seconds: float = 3.0
    api_base: str = API

    @property
    def directory(self) -> Path:
        return self.state_root / "discord"

    @property
    def state_path(self) -> Path:
        """approval id -> Discord message id, so a restart neither re-posts nor forgets."""
        return self.directory / _STATE_NAME

    @property
    def config_path(self) -> Path:
        """Non-secret settings written by tools/setup-discord-gate.ps1."""
        return self.directory / _CONFIG_NAME

    @property
    def owner(self) -> str | None:
        """The owner's id if it is a real Discord id, else None (fail closed)."""
        raw = (self.owner_user_id or "").strip()
        return raw if _SNOWFLAKE.fullmatch(raw) else None

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.channel_id)

    @classmethod
    def from_environment(
        cls, state_root: Path | None = None, environ: Mapping[str, str] | None = None
    ) -> DiscordGateSettings:
        """Environment first, then ``<state_root>/discord/config.json`` (what the
        setup script writes), then defaults.

        PIONIR_DISCORD_CHANNEL_ID (or DISCORD_CHANNEL_ID), PIONIR_DISCORD_USER_ID
        (or DISCORD_USER_ID), PIONIR_DISCORD_TOKEN_FILE, PIONIR_DISCORD_GATE
        (0/off disables), PIONIR_DISCORD_POLL_SECONDS.
        """

        env = os.environ if environ is None else environ
        root = state_root if state_root is not None else _default_state_root()
        file_values: dict[str, Any] = {}
        config_path = root / "discord" / _CONFIG_NAME
        if config_path.exists():
            try:
                loaded = json.loads(config_path.read_text(encoding="utf-8-sig"))
                if isinstance(loaded, dict):
                    file_values = loaded
                else:
                    _log.error("discord gate: %s is not a JSON object; ignored", config_path)
            except (OSError, ValueError) as error:
                _log.error("discord gate: cannot read %s: %s", config_path, error)

        def from_file(key: str) -> str | None:
            value = file_values.get(key)
            return (str(value).strip() or None) if value is not None else None

        token_file = _first(env, "PIONIR_DISCORD_TOKEN_FILE") or from_file("token_file")
        poll_raw = _first(env, "PIONIR_DISCORD_POLL_SECONDS")
        poll = 3.0
        if poll_raw is not None:
            try:
                poll = float(poll_raw)
            except ValueError:
                _log.error("discord gate: PIONIR_DISCORD_POLL_SECONDS=%r is not a number", poll_raw)
            if not poll > 0:
                poll = 3.0
        enabled_raw = (_first(env, "PIONIR_DISCORD_GATE") or "1").lower()
        return cls(
            state_root=root,
            channel_id=_first(env, "PIONIR_DISCORD_CHANNEL_ID", "DISCORD_CHANNEL_ID")
            or from_file("channel_id"),
            owner_user_id=_first(env, "PIONIR_DISCORD_USER_ID", "DISCORD_USER_ID")
            or from_file("user_id"),
            token_file=Path(token_file).expanduser() if token_file else _default_token_file(),
            enabled=enabled_raw not in ("0", "off", "false", "no"),
            poll_seconds=max(0.01, poll),
        )


def read_token(path: Path) -> str | None:
    """The bot token from its file, or None. Tolerates a BOM, whitespace and a
    pasted ``Bot `` prefix. Never logs or returns anything but the token."""

    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError:
        return None
    token = raw.strip()
    if token.lower().startswith("bot "):
        token = token[4:].strip()
    return token or None


# ---------------------------------------------------------------- REST client
class DiscordError(Exception):
    """A Discord call failed. The message is built here and never carries the token."""

    def __init__(self, message: str, *, status: int | None = None, code: int | None = None):
        super().__init__(message)
        self.status = status    # HTTP status, None for a transport failure
        self.code = code        # Discord's JSON error code, when it sent one

    @property
    def transport(self) -> bool:
        """The network or Discord itself is down: not worth trying the next message."""
        return self.status is None or self.status >= 500


class DiscordAuthError(DiscordError):
    """401: the token was rejected. Configuration, not weather - stop, say so once."""


Opener = Callable[..., Any]
Sleeper = Callable[[float], Any]


class DiscordRest:
    """A tiny Discord REST client over urllib. The token lives only here and
    only ever leaves in the Authorization header."""

    def __init__(self, token: str, *, api_base: str = API, opener: Opener | None = None,
                 sleep: Sleeper | None = None, timeout: float = 20.0) -> None:
        self.__token = token
        self.api_base = api_base.rstrip("/")
        self._open = opener or urllib.request.urlopen
        self._sleep = sleep or time.sleep
        self.timeout = timeout

    def __repr__(self) -> str:
        return f"DiscordRest(api_base={self.api_base!r}, token=<redacted>)"

    def scrub(self, text: str) -> str:
        return text.replace(self.__token, "<redacted>") if self.__token else text

    def call(self, method: str, path: str, body: dict[str, Any] | None = None, *,
             files: list[Attachment] | None = None) -> Any:
        """One call; a 429 waits out ``retry_after`` (bounded) and retries. With
        ``files``, the body goes as Discord's multipart form: ``payload_json`` plus one
        ``files[n]`` part per attachment."""

        for _attempt in range(4):
            try:
                return self._once(method, path, body, files)
            except _RateLimited as limited:
                self._sleep(limited.retry_after)
        raise DiscordError(f"{method} {path}: still rate limited after retries", status=429)

    def _once(self, method: str, path: str, body: dict[str, Any] | None,
              files: list[Attachment] | None = None) -> Any:
        headers = {"Authorization": f"Bot {self.__token}", "User-Agent": USER_AGENT}
        data: bytes | None
        if files:
            data, headers["Content-Type"] = multipart_body(body or {}, files)
        else:
            data = json.dumps(body).encode("utf-8") if body is not None else None
            if data is not None:
                headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.api_base + path, data=data, method=method,
                                         headers=headers)
        where = f"{method} {path}"
        try:
            with self._open(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            try:
                detail_raw = error.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001 - a body we cannot read is just absent
                detail_raw = ""
            parsed: Any = None
            try:
                parsed = json.loads(detail_raw) if detail_raw else None
            except ValueError:
                parsed = None
            code = parsed.get("code") if isinstance(parsed, dict) else None
            said = parsed.get("message") if isinstance(parsed, dict) else detail_raw
            said = self.scrub(str(said or ""))[:200]
            if error.code == 429:
                retry = 2.0
                if isinstance(parsed, dict):
                    try:
                        retry = float(parsed.get("retry_after", retry))
                    except (TypeError, ValueError):
                        retry = 2.0
                raise _RateLimited(min(max(retry, 0.0), MAX_RETRY_AFTER)) from None
            message = self.scrub(f"{where}: HTTP {error.code} {said}".strip())
            kind = DiscordAuthError if error.code == 401 else DiscordError
            raise kind(message, status=error.code,
                       code=code if isinstance(code, int) else None) from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as error:
            reason = getattr(error, "reason", None) or error
            raise DiscordError(self.scrub(f"{where}: {type(error).__name__}: {reason}")) from None
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            raise DiscordError(f"{where}: Discord answered with something that is not JSON") \
                from None


# (filename, content type, bytes)
Attachment = tuple[str, str, bytes]


def multipart_body(payload: Mapping[str, Any], files: list[Attachment]) -> tuple[bytes, str]:
    """Discord's message-with-files form, by hand (stdlib only): a ``payload_json`` part
    holding the JSON message (with an ``attachments`` entry per file), then ``files[n]``
    parts with the raw bytes. Returns (body, Content-Type header)."""

    message = dict(payload)
    message["attachments"] = [{"id": i, "filename": name} for i, (name, _t, _d) in
                              enumerate(files)]
    encoded = json.dumps(message, ensure_ascii=False).encode("utf-8")
    while True:
        boundary = f"pionir-{uuid4().hex}"
        mark = boundary.encode("ascii")
        if mark not in encoded and not any(mark in data for _n, _t, data in files):
            break
    head = f"--{boundary}\r\n"
    parts = [(head + 'Content-Disposition: form-data; name="payload_json"\r\n'
              "Content-Type: application/json\r\n\r\n").encode("ascii") + encoded + b"\r\n"]
    for i, (name, content_type, data) in enumerate(files):
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
        parts.append((head + f'Content-Disposition: form-data; name="files[{i}]"; '
                      f'filename="{safe}"\r\nContent-Type: {content_type}\r\n\r\n'
                      ).encode("ascii") + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class _RateLimited(Exception):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"rate limited for {retry_after}s")
        self.retry_after = retry_after


# ---------------------------------------------------------------- rendering
def _escape(text: str) -> str:
    """Discord markdown off, so a summary reads as written."""
    return re.sub(r"([\\*_~`|>#\[\]])", r"\\\1", text)


def _fence_safe(text: str) -> str:
    """Text inside a code block cannot close the block early."""
    return text.replace(_FENCE, "`\u200b`\u200b`")


def _format_amount(value: Any, currency: Any) -> str | None:
    if isinstance(value, Mapping):
        return _format_amount(value.get("amount", value.get("value")),
                              value.get("currency", currency))
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        text = f"{value:,.2f}"
    else:
        text = str(value).strip()
        if not text:
            return None
    cur = str(currency).strip().upper() if currency else ""
    return f"{text} {cur}" if cur else f"{text} (currency not stated)"


def money_line(row: Mapping[str, Any]) -> str | None:
    """The bold first line for anything that spends money, or None."""

    payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
    for source in (row, payload):
        currency = source.get("currency")
        for key in MONEY_KEYS:
            if key in source:
                amount = _format_amount(source[key], currency)
                if amount:
                    return f"\U0001f4b8 **SPENDS MONEY: {_escape(amount)}**"
    flagged = bool(row.get("spends_money") or payload.get("spends_money"))
    words = set(re.split(r"[^a-z]+", str(row.get("capability", "")).lower()))
    if flagged or words & MONEY_WORDS:
        return "\U0001f4b8 **MAY SPEND MONEY - AMOUNT NOT STATED. Do not approve blind.**"
    return None


def _command(payload: Mapping[str, Any]) -> str | None:
    action = payload.get("action")
    if not isinstance(action, str) or not action.strip():
        return None
    args = payload.get("args")
    return " ".join([action, *[str(a) for a in args]]) if isinstance(args, list) else action


def _stamp(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    unix = int(parsed.timestamp())
    return f"<t:{unix}:f> (<t:{unix}:R>)"


PUBLISH_LINE = ("\U0001f4dd **PUBLISHES PUBLICLY** - everything below goes live on the "
                "public blog if you approve. Read all of it.")


def _publish_lines(payload: Mapping[str, Any]) -> list[str]:
    """The post itself, as the owner must see it before it goes live: the address it
    will have, the title, the description, the tags and the WHOLE body, verbatim in a
    code block (split across messages by split_message, never cut)."""

    slug = payload.get("slug")
    title = payload.get("title")
    description = payload.get("description")
    body = payload.get("body_md")
    lines = [
        "**Will go live at:** " + (f"<{public_url(slug)}>" if isinstance(slug, str)
                                   else "(no valid slug)"),
        f"**Title:** {_escape(str(title))}",
        f"**Description:** {_escape(str(description))}",
    ]
    tags = payload.get("tags")
    if isinstance(tags, list) and tags:
        lines.append("**Tags:** " + ", ".join(f"`{_fence_safe(str(t))}`" for t in tags))
    text = body if isinstance(body, str) else str(body)
    lines += [(f"**The post, in full ({len(text):,} characters), exactly as it will be "
               "published:**"), _FENCE + "markdown", _fence_safe(text), _FENCE]
    return lines


DEVTO_LINE = ("📰 **CROSS-POSTS TO DEV.TO** - the blog post below, already live on "
              "api.dokaz.net, goes up on dev.to under your account if you approve.")


def _devto_lines(payload: Mapping[str, Any]) -> list[str]:
    """The article as the owner must see it before it goes up: the canonical URL it will
    point back to (the adapter builds it from the slug the same way), the title, the
    description, the tags and the WHOLE body, verbatim (split across messages, never cut)."""

    slug = payload.get("slug")
    tags = payload.get("tags")
    body = payload.get("body_md")
    lines = [
        "**Canonical URL (the original on the blog):** "
        + (f"<{public_url(slug)}>" if isinstance(slug, str) else "(no valid slug)"),
        f"**Title:** {_escape(str(payload.get('title')))}",
        f"**Description:** {_escape(str(payload.get('description')))}",
        "**Tags:** " + (", ".join(f"`{_fence_safe(str(t))}`" for t in tags)
                        if isinstance(tags, list) and tags else "(none)"),
    ]
    text = body if isinstance(body, str) else str(body)
    lines += [(f"**The article, in full ({len(text):,} characters), exactly as dev.to will "
               "receive it:**"), _FENCE + "markdown", _fence_safe(text), _FENCE]
    return lines


def client_email_line(to: Any) -> str:
    """The first line of a client.email card: who the message goes to."""
    shown = f"`{_fence_safe(str(to))}`" if isinstance(to, str) and to else "(no address)"
    return (f"✉️ **EMAILS A CLIENT** - the message below is sent to {shown} if "
            "you approve.")


def _client_email_lines(payload: Mapping[str, Any]) -> list[str]:
    """The email as the owner must see it before it is sent: the order, the recipient,
    the subject and the WHOLE message, verbatim in a plain-text block (split across
    messages by split_message, never cut)."""

    body = payload.get("body_text")
    text = body if isinstance(body, str) else str(body)
    return [
        f"**Order:** `{_fence_safe(str(payload.get('order_id')))}`",
        (f"**To:** `{_fence_safe(str(payload.get('to')))}` (Scrooge sends only to the "
         "address stored on this order)"),
        f"**Subject:** {_escape(str(payload.get('subject')))}",
        f"**The message, in full ({len(text):,} characters), exactly as it will be sent:**",
        _FENCE + "text", _fence_safe(text), _FENCE,
    ]


def find_report_line(to: Any) -> str:
    """The first line of a client.find_report card: who the report goes to."""
    shown = f"`{_fence_safe(str(to))}`" if isinstance(to, str) and to else "(no address)"
    return (f"\U0001f50e **SENDS A FIND REPORT** - the report below goes to {shown} if "
            "you approve.")


def _link_domain(link: Any) -> str:
    try:
        host = urllib.parse.urlsplit(str(link)).hostname
    except ValueError:
        host = None
    return host or "(no host)"


def _find_report_lines(payload: Mapping[str, Any]) -> list[str]:
    """The report as the owner must see it before it is sent: the order, the recipient,
    the subject, EVERY website it sends the client to (each link with its domain in bold
    first; the report can carry no link that is not on this list), and the WHOLE report,
    verbatim in a plain-text block (split across messages by split_message, never cut)."""

    raw = payload.get("links")
    links = [str(link) for link in raw] if isinstance(raw, list) else []
    domains = list(dict.fromkeys(_link_domain(link) for link in links))
    lines = [
        f"**Order:** `{_fence_safe(str(payload.get('order_id')))}`",
        (f"**To:** `{_fence_safe(str(payload.get('to')))}` (Scrooge sends only to the "
         "address stored on this order)"),
        f"**Subject:** {_escape(str(payload.get('subject')))}",
    ]
    if links:
        lines.append(f"**The websites it sends the client to ({len(domains)}):** "
                     + ", ".join(f"**{_escape(d)}**" for d in domains))
        lines.append(f"**Every link in the report ({len(links)}):**")
        # <...> keeps Discord from fetching a preview of a third-party page into the card
        lines += [f"• **{_escape(_link_domain(link))}** — <{link}>" for link in links]
    else:
        lines.append("**Links:** none - the report sends the client to no website")
    body = payload.get("body_text")
    text = body if isinstance(body, str) else str(body)
    lines += [f"**The report, in full ({len(text):,} characters), exactly as it will be sent:**",
              _FENCE + "text", _fence_safe(text), _FENCE]
    return lines


def client_deliver_line(to: Any) -> str:
    """The first line of a client.deliver card: the zip goes out, to whom."""
    shown = f"`{_fence_safe(str(to))}`" if isinstance(to, str) and to else "(no address)"
    return ("\U0001f4e6 **DELIVERS TO A CLIENT** - the zip below is uploaded to a private "
            f"link and emailed to {shown} if you approve.")


DOWNLOAD_LINK_SHOWN = "<private download link>"


def _size(n: Any) -> str:
    if not isinstance(n, int) or isinstance(n, bool):
        return "(unknown size)"
    for unit, scale in (("MB", 1_000_000), ("KB", 1_000)):
        if n >= scale:
            return f"{n:,} bytes ({n / scale:.1f} {unit})"
    return f"{n:,} bytes"


def _client_deliver_lines(payload: Mapping[str, Any],
                          preview: Mapping[str, Any] | None) -> list[str]:
    """The delivery as the owner must see it before it goes: the order, the recipient,
    the zip (its name, size and sha256 as checked on disk NOW), the FULL file list with
    sizes, the secrets scan, and the WHOLE email with the link's place marked."""

    name = _fence_safe(str(payload.get("zip_name")))
    lines = [
        f"**Order:** `{_fence_safe(str(payload.get('order_id')))}`",
        (f"**To:** `{_fence_safe(str(payload.get('to')))}` (Scrooge sends only to the "
         "address stored on this order)"),
    ]
    if preview is None:
        lines.append("⚠️ **The zip could not be inspected from here**, so its "
                     "files are not listed. Do not approve a delivery you cannot see - run "
                     "`python -m pionir deliveries`, or deny it.")
    elif preview.get("ok") is not True:
        lines.append("⚠️ **DO NOT APPROVE - the zip no longer passes the checks:** "
                     f"{_escape(str(preview.get('problem'))[:600])}. Approving will be "
                     "refused; nothing would be uploaded or emailed.")
    else:
        files = preview.get("files") or []
        lines.append(f"**Zip:** `{name}` - {_size(preview.get('size'))}, sha256 "
                     f"`{str(preview.get('sha256'))[:16]}`")
        lines += [f"**The files in it ({len(files):,}), in full:**", _FENCE + "text"]
        lines += [f"{_fence_safe(str(f[0]))}  ({_size(f[1])})" for f in files]
        lines += [_FENCE,
                  (f"\U0001f50d secrets scan: clean ({len(files):,} files, "
                   f"{int(preview.get('secret_values') or 0):,} secret values checked)")]
    body = payload.get("body_text")
    text = body if isinstance(body, str) else str(body)
    text = text.replace(LINK_PLACEHOLDER, DOWNLOAD_LINK_SHOWN)
    lines += [
        f"**Subject:** {_escape(str(payload.get('subject')))}",
        (f"**The email, in full, exactly as it will be sent - "
         f"`{DOWNLOAD_LINK_SHOWN}` becomes the link to the zip:**"),
        _FENCE + "text", _fence_safe(text), _FENCE,
    ]
    return lines


def product_line(payload: Mapping[str, Any]) -> str:
    """The first line of a product.gumroad_publish card: it goes on sale, at what price."""
    price = price_text(payload.get("price_cents"), payload.get("pay_what_you_want"))
    return ("\U0001f6d2 **PUTS A PRODUCT ON SALE** - the listing below goes live on Gumroad "
            f"at {price} if you approve.")


def _executables_line(payload: Mapping[str, Any],
                      preview: Mapping[str, Any] | None) -> str | None:
    """Called out at the top of the card: the product ships programs buyers will run."""
    if payload.get("allow_executables") is not True:
        return None
    found = ((preview or {}).get("zip") or {}).get("executables") \
        if preview and preview.get("ok") is True else None
    if isinstance(found, list) and found:
        names = ", ".join(f"`{_fence_safe(str(n))}`" for n in found)
        return (f"\u26a0\ufe0f **SHIPS EXECUTABLES ({len(found)})** - buyers will download and "
                f"can run these programs: {names}")
    if isinstance(found, list):
        return "\u2139\ufe0f allow_executables is on, but the zip holds no executables."
    return ("\u26a0\ufe0f **MAY SHIP EXECUTABLES** - allow_executables is on and the zip could "
            "not be checked from here.")


def _product_lines(payload: Mapping[str, Any],
                   preview: Mapping[str, Any] | None) -> list[str]:
    """The listing as the owner must see it before it goes on sale: the name, version,
    price, summary and tags, the zip (its FULL file list as checked on disk NOW, the
    secrets scan, any executables), the cover (attached to the message), and the WHOLE
    description as its Markdown source (split across messages, never cut)."""

    slug = payload.get("slug")
    tags = payload.get("tags")
    lines = [
        f"**Name:** {_escape(str(payload.get('name')))}",
        f"**Version:** `{_fence_safe(str(payload.get('version')))}`",
        "**Price:** " + _escape(price_text(payload.get("price_cents"),
                                           payload.get("pay_what_you_want"))),
        "**Permalink:** " + (f"`{_fence_safe(slug)}` (the listing's address ends in "
                             f"`/l/{_fence_safe(slug)}`)" if isinstance(slug, str)
                             else "(no valid slug)"),
        f"**Summary:** {_escape(str(payload.get('summary')))}",
        "**Tags:** " + (", ".join(f"`{_fence_safe(str(t))}`" for t in tags)
                        if isinstance(tags, list) and tags else "(none)"),
    ]
    name = _fence_safe(str(payload.get("zip_name")))
    if preview is None:
        lines.append("\u26a0\ufe0f **The product's files could not be inspected from here**, so "
                     "they are not listed and the cover is not shown. Do not approve a product "
                     "you cannot see - deny it.")
    elif preview.get("ok") is not True:
        lines.append("\u26a0\ufe0f **DO NOT APPROVE - the product's files no longer pass the "
                     f"checks:** {_escape(str(preview.get('problem'))[:600])}. Approving will "
                     "be refused; nothing would be published.")
    else:
        zipped = preview.get("zip") or {}
        cover = preview.get("cover") or {}
        files = zipped.get("files") or []
        executables = set(zipped.get("executables") or [])
        lines.append(f"**Zip:** `{name}` - {_size(zipped.get('size'))}, sha256 "
                     f"`{str(zipped.get('sha256'))[:16]}`")
        lines += [f"**The files in it ({len(files):,}), in full:**", _FENCE + "text"]
        lines += [f"{_fence_safe(str(f[0]))}  ({_size(f[1])})"
                  + ("  <- EXECUTABLE" if f[0] in executables else "") for f in files]
        lines += [_FENCE,
                  (f"\U0001f50d secrets scan: clean ({len(files):,} files and the cover, "
                   f"{int(zipped.get('secret_values') or 0):,} secret values checked)")]
        if executables:
            lines.append(f"\u26a0\ufe0f **{len(executables)} executable(s) in the zip** "
                         "(marked above) - allowed by allow_executables.")
        lines.append(f"**Cover:** `{_fence_safe(str(cover.get('name')))}`, attached to this "
                     f"message - {cover.get('width')}x{cover.get('height')} "
                     f"{str(cover.get('content_type')).split('/')[-1].upper()}, "
                     f"{_size(cover.get('size'))}, sha256 "
                     f"`{str(cover.get('sha256'))[:16]}`")
    body = payload.get("description_md")
    text = body if isinstance(body, str) else str(body)
    lines += [(f"**The description, in full ({len(text):,} characters) - the Markdown the "
               "listing is rendered from:**"), _FENCE + "markdown", _fence_safe(text), _FENCE]
    return lines


def product_cover(preview: Mapping[str, Any] | None) -> Attachment | None:
    """The cover to attach to a product card, as (file name, content type, bytes)."""
    if not preview or preview.get("ok") is not True:
        return None
    data = preview.get("cover_bytes")
    cover = preview.get("cover") or {}
    kind = str(cover.get("content_type"))
    if not isinstance(data, bytes) or kind not in ("image/png", "image/jpeg"):
        return None
    return ("cover.png" if kind == "image/png" else "cover.jpg", kind, data)


INSTAGRAM_LINE = ("\U0001f4f8 **POSTS PUBLICLY TO INSTAGRAM** - the image and caption below go "
                  "live on the Dokaz Instagram if you approve.")
CARD_FILENAME = "card.jpg"


@lru_cache(maxsize=16)
def _render_cached(headline: str, points: tuple[str, ...]) -> bytes | str:
    try:
        return render_card(headline, points)
    except Exception as error:  # noqa: BLE001 - any failure is shown to the owner, plainly
        return f"{type(error).__name__}: {error}"[:300]


def card_image(payload: Mapping[str, Any]) -> tuple[bytes | None, str | None]:
    """The Instagram card the post will carry, rendered exactly as the adapter will render
    it: (JPEG bytes, None), or (None, why it could not be rendered)."""

    headline = payload.get("headline")
    points = payload.get("points")
    if not isinstance(headline, str) or not isinstance(points, list) \
            or not all(isinstance(p, str) for p in points):
        return None, "the post has no usable headline and points"
    # the adapter renders check_post's normalised text: stripped
    out = _render_cached(headline.strip(), tuple(p.strip() for p in points))
    return (out, None) if isinstance(out, bytes) else (None, out)


def _instagram_lines(payload: Mapping[str, Any]) -> list[str]:
    """The post as the owner must see it before it goes live: the image (attached to the
    message), the headline and points drawn on it, and the FULL caption exactly as
    Instagram will receive it - hashtags included."""

    image, why = card_image(payload)
    lines: list[str] = []
    if image is None:
        lines.append(f"\u26a0\ufe0f **The card image could not be rendered** ({_escape(str(why))}), "
                     "so it is not attached. Do not approve a post you cannot see - deny it "
                     "and have it redrafted.")
    else:
        sha = hashlib.sha256(image).hexdigest()
        lines.append(f"**The image:** `{CARD_FILENAME}`, attached to this message - exactly "
                     f"the picture that will be posted (sha256 `{sha[:16]}`).")
        pinned = payload.get("card_sha")
        if isinstance(pinned, str) and pinned != sha:
            lines.append("\u26a0\ufe0f **This image differs from the one the post pinned "
                         "(card_sha)** - approving will be refused; deny it.")
    lines.append(f"**Headline:** {_escape(str(payload.get('headline')))}")
    points = payload.get("points")
    for point in points if isinstance(points, list) else []:
        lines.append(f"\u2022 {_escape(str(point))}")
    caption = payload.get("caption")
    tags = payload.get("hashtags") or []
    if isinstance(caption, str) and isinstance(tags, list):
        text = full_caption(caption.strip(), [str(t) for t in tags])
    else:
        text = str(caption)
    lines += [(f"**The caption, in full ({len(text):,} characters), exactly as Instagram "
               "will receive it:**"), _FENCE, _fence_safe(text), _FENCE]
    return lines


def render_request(row: Mapping[str, Any], owner: str | None, *,
                   delivery: Mapping[str, Any] | None = None,
                   product: Mapping[str, Any] | None = None) -> str:
    """The whole text of an approval message, before it is split to fit Discord.
    Nothing that says what the action does is ever cut; long text is split
    across messages instead. ``delivery`` is a client.deliver's zip as inspected on
    disk (ClientAdapter.delivery_preview), or None if it could not be; ``product`` is a
    product.gumroad_publish's files as inspected (ProductAdapter.product_preview)."""

    payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
    publishes = row.get("capability") == PUBLISH
    posts = row.get("capability") == INSTAGRAM_POST
    crossposts = row.get("capability") == DEVTO_CROSSPOST
    emails = row.get("capability") == CLIENT_EMAIL
    delivers = row.get("capability") == CLIENT_DELIVER
    finds = row.get("capability") == CLIENT_FIND_REPORT
    sells = row.get("capability") == PRODUCT_PUBLISH
    lines: list[str] = []
    if sells:
        lines.append(product_line(payload))
        executables = _executables_line(payload, product)
        if executables:
            lines.append(executables)
    if delivers:
        lines.append(client_deliver_line(payload.get("to")))
    if emails:
        lines.append(client_email_line(payload.get("to")))
    if finds:
        lines.append(find_report_line(payload.get("to")))
    if publishes:
        lines.append(PUBLISH_LINE)
    if posts:
        lines.append(INSTAGRAM_LINE)
    if crossposts:
        lines.append(DEVTO_LINE)
    money = money_line(row)
    if money:
        lines.append(money)
    lines.append(f"\U0001f510 **Pionir approval needed** · id `{row.get('id')}`")
    lines.append(f"**What it does:** {_escape(str(row.get('summary') or '(no summary)'))}")
    lines.append(f"**Capability:** `{row.get('capability')}`")
    lines.append(f"**Asked by:** {_escape(str(row.get('requester') or 'unknown'))}")
    permissions = row.get("permissions") or []
    if permissions:
        lines.append("**Runs with permission:** " + ", ".join(f"`{p}`" for p in permissions))
    expires = _stamp(row.get("expires_at"))
    if expires:
        lines.append(f"**Expires:** {expires} - unanswered, it is denied")
    if owner:
        lines.append(f"React {APPROVE} to approve or {DENY} to deny. Only <@{owner}>'s "
                     "reaction counts.")
    else:
        lines.append("⚠\ufe0f **This gate cannot accept answers: no owner user id is "
                     "configured (PIONIR_DISCORD_USER_ID).** Reactions here do nothing - "
                     "approve or deny from the phone page.")
    command = _command(payload)
    if command:
        lines += ["**Command:**", _FENCE, _fence_safe(command), _FENCE]
    shown: Mapping[str, Any] = payload
    if publishes:
        lines += _publish_lines(payload)
        if isinstance(payload.get("body_md"), str):
            # the body is shown above, in full and readable; as one JSON-escaped line
            # it would only double the messages
            shown = {**payload, "body_md": f"(the full post above, "
                                           f"{len(payload['body_md']):,} characters)"}
    if crossposts:
        lines += _devto_lines(payload)
        if isinstance(payload.get("body_md"), str):
            shown = {**payload, "body_md": f"(the full article above, "
                                           f"{len(payload['body_md']):,} characters)"}
    if emails:
        lines += _client_email_lines(payload)
        if isinstance(payload.get("body_text"), str):
            shown = {**payload, "body_text": f"(the full message above, "
                                             f"{len(payload['body_text']):,} characters)"}
    if finds:
        lines += _find_report_lines(payload)
        if isinstance(payload.get("body_text"), str):
            shown = {**payload, "body_text": f"(the full report above, "
                                             f"{len(payload['body_text']):,} characters)"}
    if delivers:
        lines += _client_deliver_lines(payload, delivery)
        if isinstance(payload.get("body_text"), str):
            shown = {**payload, "body_text": f"(the full email above, "
                                             f"{len(payload['body_text']):,} characters)"}
    if sells:
        lines += _product_lines(payload, product)
        if isinstance(payload.get("description_md"), str):
            shown = {**payload, "description_md": f"(the full description above, "
                                                  f"{len(payload['description_md']):,} "
                                                  "characters)"}
    if posts:
        lines += _instagram_lines(payload)
        if isinstance(payload.get("caption"), str):
            shown = {**payload, "caption": "(the full caption above)"}
    lines += ["**Full payload:**", _FENCE + "json",
              _fence_safe(json.dumps(shown, indent=2, ensure_ascii=False, default=str)),
              _FENCE]
    return "\n".join(lines)


def split_message(text: str, first_limit: int = MESSAGE_LIMIT - STATUS_RESERVE,
                  limit: int = MESSAGE_LIMIT) -> list[str]:
    """Split on line boundaries into Discord-sized chunks. A code block cut by a
    chunk boundary is closed and reopened, and an over-long line is hard-split;
    nothing is dropped."""

    piece_max = min(first_limit, limit) - 40
    lines: list[str] = []
    for line in text.split("\n"):
        while len(line) > piece_max:
            lines.append(line[:piece_max])
            line = line[piece_max:]
        lines.append(line)
    chunks: list[str] = []
    current: list[str] = []
    fence: str | None = None   # the opener of the code block we are inside
    for line in lines:
        cap = first_limit if not chunks else limit
        candidate = len("\n".join([*current, line])) + (len(_FENCE) + 1 if fence else 0)
        if current and candidate > cap:
            if fence:
                current.append(_FENCE)
            chunks.append("\n".join(current))
            current = [fence] if fence else []
        current.append(line)
        if line.startswith(_FENCE):
            fence = None if fence else line
    if current:
        chunks.append("\n".join(current))
    return chunks


def _failure_reason(result: Any) -> str:
    if isinstance(result, Mapping):
        error = result.get("error")
        if isinstance(error, Mapping):
            return str(error.get("message") or error.get("type") or "error")
        if isinstance(error, str) and error:
            return error
        inner = result.get("result")
        if isinstance(inner, Mapping):
            for key in ("error", "message", "reason"):
                if inner.get(key):
                    return str(inner[key])
    return "the action reported failure"


def render_status(row: Mapping[str, Any] | None, answer: Mapping[str, Any] | None) -> str:
    """One line on top of a message once it is answered: what was decided, by
    whom, and what happened."""

    if row is None:
        return ("❔ **No longer in Pionir's queue** - it was dropped after its retention; "
                "the outcome is not known here.")
    accepted = bool(answer and answer.get("outcome") == "accepted")
    who = f"by **{_escape(str(answer.get('by_name')))}** in Discord" if accepted and answer \
        else "outside Discord (phone/dashboard)"
    status = row.get("status")
    task = row.get("task_id")
    task_note = f" (task `{str(task)[:12]}`)" if task else ""
    if status == "running":
        line = f"⏳ **APPROVED** {who} - running now{task_note}"
    elif status == "approved":
        line = f"✅ **APPROVED** {who} - **ran**, finished OK{task_note}"
    elif status == "approved_failed":
        why = _escape(_failure_reason(row.get("result"))[:200])
        line = f"⚠\ufe0f **APPROVED** {who} - **ran and FAILED**: {why}{task_note}"
    elif status == "denied" and row.get("reason") == "expired":
        line = "⌛ **EXPIRED** - nobody answered in time; auto-denied, it did **not** run"
    elif status == "denied" and row.get("reason") == "interrupted":
        line = ("⚠\ufe0f **INTERRUPTED** - Pionir restarted while it was running; it may "
                "have partly run and is marked denied")
    elif status == "denied":
        line = f"❌ **DENIED** {who} - it did **not** run"
    else:
        line = f"❔ **{_escape(str(status).upper())}**"
    if answer and answer.get("outcome") == "late":
        mark = APPROVE if answer.get("decision") == "approve" else DENY
        line += (f"\nℹ\ufe0f Your {mark} in Discord came after it was already "
                 f"{_escape(str(answer.get('found') or 'resolved'))} elsewhere - nothing ran twice.")
    return line


def _compose(status: str, head: str) -> str:
    text = f"{status}\n\n{head}" if status else head
    return text if len(text) <= MESSAGE_LIMIT else text[:MESSAGE_LIMIT - 1] + "…"


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------- the gate
class DiscordGate:
    """Posts each pending approval to Discord and turns the owner's reaction
    into ``approve`` / ``deny`` - the same calls the phone page makes."""

    def __init__(
        self,
        settings: DiscordGateSettings,
        *,
        approvals: Any,
        approve: Callable[[str], Mapping[str, Any]],
        deny: Callable[[str], Mapping[str, Any]],
        opener: Opener | None = None,
        sleep: Sleeper | None = None,
        inspect_delivery: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
        inspect_product: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    ) -> None:
        self.settings = settings
        self._approvals = approvals      # ApprovalQueue: pending() / get()
        self._approve = approve          # PionirApp.approve: claim, run as a job, settle
        self._deny = deny                # PionirApp.deny
        # ClientAdapter.delivery_preview: a client.deliver card lists the zip's files as
        # they are on disk when it is posted. None: the card says it could not look.
        self._inspect_delivery = inspect_delivery
        # ProductAdapter.product_preview: a product card lists the zip's files and carries
        # the cover as they are on disk when it is posted.
        self._inspect_product = inspect_product
        self._previews: dict[str, tuple[float, Mapping[str, Any] | None]] = {}
        self._opener = opener
        self._stop = threading.Event()
        self._sleep = sleep or self._stop.wait
        self._tick_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._client: DiscordRest | None = None
        self._entries: dict[str, dict[str, Any]] = {}
        self._loaded = False
        self._bot: dict[str, Any] | None = None
        self._auth_failed = False
        self._last_logged: str | None = None
        self._outage = False
        self.running = False
        self.last_error: str | None = None
        self.last_ok_at: str | None = None
        self.reason: str | None = None   # why it is not running, when it is not
        self.errors = 0

    @classmethod
    def for_app(cls, app: Any, settings: DiscordGateSettings, **kwargs: Any) -> DiscordGate:
        """Wire to a PionirApp: its queue, and its own approve/deny (and the client
        adapter's delivery preview, when it is registered)."""
        adapters = getattr(getattr(app, "runtime", None), "adapters", None) or {}
        client = adapters.get("client") if isinstance(adapters, Mapping) else None
        preview = getattr(client, "delivery_preview", None)
        if callable(preview):
            kwargs.setdefault("inspect_delivery", preview)
        products = adapters.get("product") if isinstance(adapters, Mapping) else None
        product_preview = getattr(products, "product_preview", None)
        if callable(product_preview):
            kwargs.setdefault("inspect_product", product_preview)
        return cls(settings, approvals=app.approvals, approve=app.approve, deny=app.deny,
                   **kwargs)

    def _delivery_preview(self, row: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """The zip of a parked client.deliver (or the files of a parked product) as it is
        on disk (re-read at most every PREVIEW_SECONDS, so a zip changed after posting
        turns the card into a DO NOT APPROVE on a later poll without re-scanning it every
        tick)."""
        inspect = {CLIENT_DELIVER: self._inspect_delivery,
                   PRODUCT_PUBLISH: self._inspect_product}.get(str(row.get("capability")))
        if inspect is None:
            return None
        key = str(row.get("id"))
        cached = self._previews.get(key)
        now = time.monotonic()
        if cached is not None and now - cached[0] < PREVIEW_SECONDS:
            return cached[1]
        try:
            preview: Mapping[str, Any] | None = inspect(row.get("payload") or {})
        except Exception as error:  # noqa: BLE001 - shown to the owner as "could not look"
            _log.warning("discord gate: approval %s: the files could not be inspected: %s",
                         row.get("id"), type(error).__name__)
            preview = None
        if len(self._previews) > 200:     # answered approvals are never asked about again
            self._previews.clear()
        self._previews[key] = (now, preview)
        return preview

    def _render(self, row: Mapping[str, Any], preview: Mapping[str, Any] | None) -> str:
        if row.get("capability") == PRODUCT_PUBLISH:
            return render_request(row, self.settings.owner, product=preview)
        return render_request(row, self.settings.owner, delivery=preview)

    def __repr__(self) -> str:
        return (f"DiscordGate(channel={self.settings.channel_id!r}, "
                f"running={self.running}, token=<redacted>)")

    # ------------------------------------------------------------ lifecycle
    def connect(self) -> bool:
        """Read the token and build the client. False (and ``reason``) if it can't."""
        if not self.settings.enabled:
            self.reason = "disabled (PIONIR_DISCORD_GATE=0)"
            return False
        if not self.settings.channel_id:
            self.reason = "no channel id configured (PIONIR_DISCORD_CHANNEL_ID)"
            return False
        token = read_token(self.settings.token_file)
        if not token:
            self.reason = f"no bot token in {self.settings.token_file}"
            return False
        self._client = DiscordRest(token, api_base=self.settings.api_base,
                                   opener=self._opener, sleep=self._sleep)
        self._auth_failed = False     # a fresh connect gets to say it once more
        self._bot = None
        self.reason = None
        if not self.settings.owner:
            _log.warning("discord gate: no valid owner user id configured - it will post "
                         "approvals but cannot accept any answer from Discord")
        return True

    def start(self) -> bool:
        if self.running:
            return False
        if not self.connect():
            _log.warning("discord gate not started: %s", self.reason)
            return False
        self._stop.clear()
        self.running = True
        self._thread = threading.Thread(target=self._loop, name="pionir-discord-gate",
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 25.0) -> None:
        self._stop.set()
        self.running = False
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        self._thread = None

    def state(self) -> dict[str, Any]:
        entries = self._entries
        return {
            "enabled": self.settings.enabled,
            "configured": self.settings.configured,
            "running": self.running,
            "channel_id": self.settings.channel_id,
            "owner_configured": self.settings.owner is not None,
            "accepting_answers": bool(self.running and self.settings.owner
                                      and not self._auth_failed),
            "auth_failed": self._auth_failed,
            "bot": (self._bot or {}).get("username"),
            "reason": self.reason,
            "last_error": self.last_error,
            "last_ok_at": self.last_ok_at,
            "errors": self.errors,
            "tracked": len(entries),
            "open": sum(1 for e in entries.values() if not e.get("final")),
        }

    def _loop(self) -> None:
        failures = 0
        while not self._stop.is_set():
            ok = self.run_once()
            if self._auth_failed:
                break
            failures = 0 if ok else failures + 1
            wait = self.settings.poll_seconds * (2 ** min(failures, 5)) if failures \
                else self.settings.poll_seconds
            self._stop.wait(min(wait, 60.0))
        self.running = False

    # ------------------------------------------------------------ one pass
    def run_once(self) -> bool:
        """One poll: post what is new, follow what is open. True if Discord
        answered. Never raises: every failure is logged and left in ``state()``."""

        with self._tick_lock:
            if self._auth_failed:
                return False
            if self._client is None and not self.connect():
                return False
            try:
                self._load()
                if self._bot is None:
                    self._hello()
                self._tick()
            except DiscordAuthError as error:
                self._auth_failed = True
                self.running = False
                self._stop.set()
                self.last_error = str(error)
                self.errors += 1
                _log.error("discord gate: %s; token rejected, gate stopped. Approvals still "
                           "work from the phone. Fix the token file and restart.", error)
                return False
            except DiscordError as error:
                self._outage = True
                self._note_error(str(error))
                return False
            except Exception as error:  # noqa: BLE001 - a bug must be seen, not kill the loop
                self.errors += 1
                self.last_error = self._scrub(f"{type(error).__name__}: {error}")
                _log.exception("discord gate: unexpected failure")
                return False
            self.last_ok_at = _now().isoformat()
            if self._outage:
                _log.info("discord gate: Discord answering again")
                self._outage = False
                self._last_logged = None   # the same failure again is news again
            return True

    def _note_error(self, message: str) -> None:
        self.errors += 1
        self.last_error = message
        if message != self._last_logged:
            _log.warning("discord gate: %s", message)
            self._last_logged = message
        else:
            _log.debug("discord gate (again): %s", message)

    def _scrub(self, text: str) -> str:
        return self._client.scrub(text) if self._client else text

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None, *,
              files: list[Attachment] | None = None) -> Any:
        assert self._client is not None
        return self._client.call(method, path, body, files=files)

    def _hello(self) -> None:
        me = self._call("GET", "/users/@me")
        channel = self._call("GET", f"/channels/{self.settings.channel_id}")
        self._bot = me if isinstance(me, dict) else {}
        _log.info("discord gate up as %s in channel %s; owner %s",
                  self._bot.get("username"),
                  channel.get("name") if isinstance(channel, dict) else self.settings.channel_id,
                  "configured" if self.settings.owner else "NOT configured - answers refused")

    def _tick(self) -> None:
        pending = self._approvals.pending()     # also auto-denies the expired
        fresh = set()
        for row in pending:
            if row["id"] not in self._entries:
                fresh.add(row["id"])
                self._guard(row["id"], lambda row=row: self._post(row))
        for approval_id, entry in list(self._entries.items()):
            if entry.get("final") or approval_id in fresh:
                continue
            self._guard(approval_id, lambda a=approval_id, e=entry: self._follow(a, e))
        self._prune()

    def _guard(self, approval_id: str, work: Callable[[], None]) -> None:
        """One approval's trouble (a missing permission, a 404) is logged and
        must not stop the others. A dead network or a rejected token stops the
        pass - the caller logs it once."""
        try:
            work()
        except DiscordError as error:
            if isinstance(error, DiscordAuthError) or error.transport:
                raise
            self._note_error(f"approval {approval_id}: {error}")

    # ------------------------------------------------------------ posting
    def _post(self, row: Mapping[str, Any], *, status: str = "") -> None:
        """Post an approval (all its chunks), remember it at once, then react."""

        channel = self.settings.channel_id
        preview = self._delivery_preview(row)
        chunks = split_message(self._render(row, preview))
        owner = self.settings.owner
        mentions: dict[str, Any] = {"parse": [], "users": [owner] if owner and not status else []}
        head = chunks[0]
        message = {"content": _compose(status, head), "allowed_mentions": mentions}
        attachment: Attachment | None = None
        if row.get("capability") == INSTAGRAM_POST:
            image = card_image(row.get("payload") or {})[0]
            attachment = (CARD_FILENAME, "image/jpeg", image) if image is not None else None
        elif row.get("capability") == PRODUCT_PUBLISH:
            attachment = product_cover(preview)
        attach_failed: str | None = None
        if attachment is None:
            sent = self._call("POST", f"/channels/{channel}/messages", message)
        else:
            try:
                sent = self._call("POST", f"/channels/{channel}/messages", message,
                                  files=[attachment])
            except DiscordError as error:
                if isinstance(error, DiscordAuthError) or error.transport:
                    raise
                # e.g. 403: the bot lacks Attach Files. The approval is still shown -
                # with a plain warning right under it - and stays parked.
                attach_failed = str(error)
                sent = self._call("POST", f"/channels/{channel}/messages", message)
        previous = self._entries.get(row["id"], {})
        entry = {
            "message_id": str(sent["id"]),
            "channel_id": channel,
            "extra_ids": [],
            "posted_at": _now().isoformat(),
            "reactions_added": False,
            "head": head,
            "shown": _compose(status, head),
            "answer": previous.get("answer"),
            "final": False,
        }
        # Saved before anything else can fail: a crash from here on must not
        # post this approval a second time.
        self._entries[row["id"]] = entry
        self._save()
        if attach_failed is not None:
            _log.warning("discord gate: approval %s: the card image could not be attached: %s",
                         row["id"], attach_failed)
            what = "cover image" if row.get("capability") == PRODUCT_PUBLISH else "card image"
            thing = "a product" if row.get("capability") == PRODUCT_PUBLISH else "a post"
            warning = (f"\u26a0\ufe0f **The {what} could not be attached** "
                       f"({_escape(attach_failed[:200])}). Do not approve {thing} you cannot "
                       "see: give the bot the Attach Files permission in this channel, or "
                       "deny it.")
            extra = self._call("POST", f"/channels/{channel}/messages",
                               {"content": warning, "allowed_mentions": {"parse": []}})
            entry["extra_ids"].append(str(extra["id"]))
        for chunk in chunks[1:]:
            extra = self._call("POST", f"/channels/{channel}/messages",
                               {"content": chunk, "allowed_mentions": {"parse": []}})
            entry["extra_ids"].append(str(extra["id"]))
        self._save()
        if not status:
            self._add_reactions(entry)

    def _add_reactions(self, entry: dict[str, Any]) -> None:
        if not self.settings.owner:
            return   # no one can answer: offering the buttons would mislead
        base = f"/channels/{entry['channel_id']}/messages/{entry['message_id']}/reactions"
        for emoji in (APPROVE, DENY):
            self._call("PUT", f"{base}/{urllib.parse.quote(emoji)}/@me")
        entry["reactions_added"] = True
        self._save()

    def _edit(self, entry: dict[str, Any], content: str) -> None:
        if entry.get("shown") == content:
            return
        path = f"/channels/{entry['channel_id']}/messages/{entry['message_id']}"
        try:
            self._call("PATCH", path, {"content": content, "allowed_mentions": {"parse": []}})
        except DiscordError as error:
            if error.status != 404:
                raise
            # Deleted in Discord: the log must not silently lose this line.
            sent = self._call("POST", f"/channels/{entry['channel_id']}/messages",
                              {"content": content, "allowed_mentions": {"parse": []}})
            _log.warning("discord gate: message %s was deleted; posted its update as %s",
                         entry["message_id"], sent["id"])
            entry["message_id"] = str(sent["id"])
        entry["shown"] = content
        self._save()

    # ------------------------------------------------------------ following
    def _follow(self, approval_id: str, entry: dict[str, Any]) -> None:
        row = self._approvals.get(approval_id)
        if row is None or row.get("status") != "pending":
            self._settle_view(approval_id, entry, row)
            return
        # Still pending: is the message still there, and has the owner answered?
        path = f"/channels/{entry['channel_id']}/messages/{entry['message_id']}"
        try:
            message = self._call("GET", path)
        except DiscordError as error:
            if error.status != 404:
                raise
            _log.warning("discord gate: message for approval %s was deleted; re-posting",
                         approval_id)
            self._post(row)
            return
        if not entry.get("reactions_added"):
            # the owner can add the reaction himself, so a failure here must not
            # stop his answer from being read
            self._guard(approval_id, lambda: self._add_reactions(entry))
        head = split_message(self._render(row, self._delivery_preview(row)))[0]
        if head != entry.get("head"):
            entry["head"] = head      # e.g. an owner id configured since it was posted
            self._edit(entry, head)
        decision = self._owner_decision(entry, message)
        if decision is None:
            return
        choice, by = decision
        self._answer(approval_id, entry, choice, by)

    def _owner_decision(self, entry: Mapping[str, Any],
                        message: Any) -> tuple[str, dict[str, Any]] | None:
        """The owner's answer, or None. Nobody else's reaction is ever looked at
        as an answer, and with no owner configured there is no answer at all."""

        owner = self.settings.owner
        if owner is None:
            return None                                   # fail closed
        reactions = message.get("reactions") if isinstance(message, dict) else None
        if not isinstance(reactions, list):
            return None
        found: dict[str, dict[str, Any]] = {}
        for reaction in reactions:
            emoji = (reaction.get("emoji") or {}).get("name")
            if emoji not in (APPROVE, DENY):
                continue
            others = int(reaction.get("count") or 0) - (1 if reaction.get("me") else 0)
            if others <= 0:
                continue
            base = f"/channels/{entry['channel_id']}/messages/{entry['message_id']}/reactions"
            users = self._call("GET", f"{base}/{urllib.parse.quote(emoji)}?limit=100")
            for user in users if isinstance(users, list) else []:
                if str(user.get("id")) == owner:          # only the owner counts
                    found[emoji] = user
        # Both at once is ambiguous: refusing is the side that cannot do harm.
        if DENY in found:
            return "deny", found[DENY]
        if APPROVE in found:
            return "approve", found[APPROVE]
        return None

    def _answer(self, approval_id: str, entry: dict[str, Any], choice: str,
                by: Mapping[str, Any]) -> None:
        """Hand the owner's answer to the same approve/deny the phone uses."""

        call = self._approve if choice == "approve" else self._deny
        try:
            response = call(approval_id)
        except Exception:  # noqa: BLE001 - logged; the row is untouched or the queue says so
            _log.exception("discord gate: %s of approval %s failed", choice, approval_id)
            self.errors += 1
            return
        name = by.get("global_name") or by.get("username") or str(by.get("id"))
        answer: dict[str, Any] = {"decision": choice, "by_id": str(by.get("id")),
                                  "by_name": name, "at": _now().isoformat()}
        if response.get("ok"):
            answer["outcome"] = "accepted"
            _log.info("discord gate: approval %s %s by %s in Discord", approval_id,
                      "approved" if choice == "approve" else "denied", name)
        else:
            error = response.get("error") or {}
            current = self._approvals.get(approval_id)
            answer["outcome"] = "late"
            answer["found"] = (current or {}).get("status") or error.get("type") or "resolved"
            _log.info("discord gate: %s for approval %s arrived after it was already %s",
                      choice, approval_id, answer["found"])
        entry["answer"] = answer
        self._save()
        self._settle_view(approval_id, entry, self._approvals.get(approval_id))

    def _settle_view(self, approval_id: str, entry: dict[str, Any],
                     row: Mapping[str, Any] | None) -> None:
        """Show where the row stands; once it is final, stop following it."""

        self._edit(entry, _compose(render_status(row, entry.get("answer")), entry["head"]))
        if row is None or row.get("status") not in ("pending", "running"):
            entry["final"] = True
            entry["final_at"] = _now().isoformat()
            self._save()

    # ------------------------------------------------------------ durability
    def _load(self) -> None:
        if self._loaded:
            return
        path = self.settings.state_path
        self._loaded = True
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            entries = data.get("messages") if isinstance(data, dict) else None
            if not isinstance(entries, dict):
                raise ValueError("no 'messages' map")  # noqa: TRY004 - one unreadable path
        except (OSError, ValueError) as error:
            # Losing the map means pending approvals are posted again - a
            # duplicate, never a silence. Keep the bad file for a look.
            keep = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
            try:
                path.replace(keep)
            except OSError:
                keep = path
            _log.error("discord gate: state file unreadable (%s), kept at %s; pending "
                       "approvals will be re-posted", error, keep)
            return
        self._entries = {str(k): v for k, v in entries.items() if isinstance(v, dict)}

    def _save(self) -> None:
        path = self.settings.state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"messages": self._entries}, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        atomic.replace(tmp, path)

    def _prune(self) -> None:
        cutoff = _now() - RETAIN_FINAL
        drop = []
        for approval_id, entry in self._entries.items():
            if not entry.get("final"):
                continue
            try:
                at = datetime.fromisoformat(entry.get("final_at") or "")
            except ValueError:
                continue
            if at < cutoff:
                drop.append(approval_id)
        for approval_id in drop:
            del self._entries[approval_id]
        if drop:
            self._save()
