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
zip's full file list as checked on disk, the secrets scan and the whole email (and, for an
order that still owes half its price, HELD FOR THE BALANCE: the email carries the balance
pay link, never the files); a ``client.quote`` card opens with SENDS A QUOTE and shows the
owner's price, the deposit split, the delivery time, the pay link's validity and the whole
email; a ``client.quote_reminder`` card opens with SENDS THE QUOTE REMINDER; a
``client.release`` card opens with RELEASES A HELD DELIVERY; a
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

The daily digest. A row parked with ``batch: true`` (a routine public item - see
pionir/batching.py; never money, never a client) is not posted at once: at the daily digest
time (PIONIR_DIGEST_TIME, default 09:00 local), or when the owner asks for it early
(POST /api/approvals/digest), each waiting item gets its full card posted as a quiet
preview and ONE digest card lists them, numbered 1-10 (more cards past ten, or past what
one message holds), each with a link to its preview. On the digest card a number approves
that item and ✅ approves every item still waiting on it; ❌ on an item's preview rejects
it (the previews are read first, so a reject always beats an approve-all in the same
poll); the owner's ❌ on the digest card itself approves nothing from it. Approving goes
through the same ``PionirApp.approve`` as any card. Undecided items carry over to the next
digest (the older card is marked superseded and its reactions stop counting); an item left
unanswered for PIONIR_DIGEST_EXPIRE_DAYS (default 7) is denied as expired, never run, and
the next digest reports it.

Quote replies. The same poll also reads the owner's REPLIES to the quote cards
(``quotes.card``) and turns each into a parked ``client.quote`` - see pionir/quotes.py. That
is a REST read of the channel's messages (``GET /channels/<id>/messages?after=``): no
gateway, no slash command (a slash command needs an interactions endpoint or a gateway
socket, and this gate has neither). Reading a reply's text needs the bot's privileged
Message Content intent, switched on in the Discord Developer Portal (Bot -> Privileged
Gateway Intents -> Message Content Intent); a reply the bot cannot read is answered with
exactly that. Only the owner's reply counts, and each reply is acted on once.

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
from .adapters.clients import LINK_PLACEHOLDER, PAY_LINK_PLACEHOLDER
from .adapters.clients import QUOTE as CLIENT_QUOTE
from .adapters.clients import RELEASE as CLIENT_RELEASE
from .adapters.clients import REMIND as CLIENT_REMIND
from .adapters.content import PUBLISH, public_url
from .adapters.devto import CROSSPOST as DEVTO_CROSSPOST
from .adapters.instagram import POST as INSTAGRAM_POST
from .adapters.products import PUBLISH as PRODUCT_PUBLISH
from .adapters.products import price_text
from .batching import DigestSettings, answer_request, local_now, read_request
from .quotes import (
    LINK_DAYS,
    QuoteCardStore,
    QuoteReplies,
    days_text,
    owner_reply,
    parse_reply,
    usd,
)
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
    tagged = payload.get("affiliate_links")
    tagged = {str(link) for link in tagged} if isinstance(tagged, list) else set()
    if links:
        lines.append(f"**The websites it sends the client to ({len(domains)}):** "
                     + ", ".join(f"**{_escape(d)}**" for d in domains))
        if tagged:
            lines.append(f"\U0001f4b5 **AFFILIATE LINKS: {len(tagged)} of {len(links)}** carry "
                         "your affiliate tag (rewritten from the links found; any other tag "
                         "removed). The report tells the client so.")
        lines.append(f"**Every link in the report ({len(links)}):**")
        # <...> keeps Discord from fetching a preview of a third-party page into the card
        lines += [f"• **{_escape(_link_domain(link))}** — <{link}>"
                  + (" - **affiliate link**" if link in tagged else "") for link in links]
    else:
        lines.append("**Links:** none - the report sends the client to no website")
    body = payload.get("body_text")
    text = body if isinstance(body, str) else str(body)
    lines += [f"**The report, in full ({len(text):,} characters), exactly as it will be sent:**",
              _FENCE + "text", _fence_safe(text), _FENCE]
    return lines


HOLD_LINE = ("\U0001f4b0 **HELD FOR THE BALANCE** - the client still owes half the price, so "
             "the email below carries a link to PAY the balance, not the files. The zip is "
             "stored with no link until the balance is paid; then a release card asks your "
             "\u2705.")
PAY_LINK_SHOWN = "<private pay link>"
BALANCE_LINK_SHOWN = "<balance pay link>"


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
    held = payload.get("hold_for_balance") is True
    shown = BALANCE_LINK_SHOWN if held else DOWNLOAD_LINK_SHOWN
    text = text.replace(LINK_PLACEHOLDER, shown)
    lines += [
        f"**Subject:** {_escape(str(payload.get('subject')))}",
        (f"**The email, in full, exactly as it will be sent - "
         f"`{shown}` becomes " + ("the link to pay the balance:**" if held
                                   else "the link to the zip:**")),
        _FENCE + "text", _fence_safe(text), _FENCE,
    ]
    return lines


def _cents(value: Any) -> str:
    return usd(value) if isinstance(value, int) and not isinstance(value, bool) else repr(value)


def quote_line(to: Any) -> str:
    """The first line of a client.quote card."""
    shown = f"`{_fence_safe(str(to))}`" if isinstance(to, str) and to else "(no address)"
    return ("\U0001f4b5 **SENDS A QUOTE** - on your \u2705 its pay link is created and the "
            f"email below goes to {shown}. Nothing happens before that.")


NOT_YOUR_REPLY = ("\u26d4 **NOT FROM YOUR REPLY** - no reply of yours on Discord is recorded "
                  "for this quote. Deny it: a quote is only ever your own price.")


def _quote_lines(payload: Mapping[str, Any],
                 reply: Mapping[str, Any] | None = None) -> list[str]:
    """The quote as the owner must see it before it goes: his reply AS THE GATE RECORDED IT
    (never anything the payload says he wrote), the price, the deposit split, the delivery
    time, what the pay link does, and the WHOLE email."""
    total, deposit, days = (payload.get("total_cents"), payload.get("deposit_cents"),
                            payload.get("days"))
    lines = [f"**Order:** `{_fence_safe(str(payload.get('order_id')))}`"]
    defaulted = False
    if reply is None:
        lines.append(NOT_YOUR_REPLY)
    else:
        text = str(reply.get("text"))
        lines.append(f"**Your reply:** `{_fence_safe(text)}`")
        try:
            defaulted = parse_reply(text).days is None
        except ValueError:
            lines.append(NOT_YOUR_REPLY)
    lines.append(f"**Price:** **{_cents(total)}** (USD)")
    if isinstance(deposit, int) and not isinstance(deposit, bool) and deposit > 0 \
            and isinstance(total, int):
        lines.append(f"**Payment:** 50% deposit **{usd(deposit)}** before work starts, then "
                     f"**{usd(total - deposit)}** on delivery - the files are held until the "
                     "balance is paid")
    else:
        lines.append(f"**Payment:** in full, **{_cents(total)}**, up front")
    shown_days = days_text(days) if isinstance(days, int) and not isinstance(days, bool) \
        else repr(days)
    lines.append(f"**Delivery:** {shown_days} from payment"
                 + (" - **the default: your reply gave no days**" if defaulted else ""))
    lines.append(f"**Pay link:** created only on your \u2705; valid {LINK_DAYS} days, then it "
                 "says the quote expired and to reply. Unpaid on day 10: one reminder is "
                 "drafted for your \u2705. A newer reply replaces this quote and stops its link.")
    body = payload.get("body_text")
    text = (body if isinstance(body, str) else str(body)).replace(PAY_LINK_PLACEHOLDER,
                                                                  PAY_LINK_SHOWN)
    lines += [
        (f"**To:** `{_fence_safe(str(payload.get('to')))}` (Scrooge sends only to the "
         "address stored on this order)"),
        f"**Subject:** {_escape(str(payload.get('subject')))}",
        (f"**The email, in full, exactly as it will be sent - `{PAY_LINK_SHOWN}` becomes the "
         "pay link:**"),
        _FENCE + "text", _fence_safe(text), _FENCE,
    ]
    return lines


def reminder_line(to: Any) -> str:
    shown = f"`{_fence_safe(str(to))}`" if isinstance(to, str) and to else "(no address)"
    return ("\u23f0 **SENDS THE QUOTE REMINDER** - the one reminder this quote gets goes to "
            f"{shown} if you approve.")


def release_line(to: Any) -> str:
    shown = f"`{_fence_safe(str(to))}`" if isinstance(to, str) and to else "(no address)"
    return ("\U0001f4e6 **RELEASES A HELD DELIVERY** - the balance is paid; on your \u2705 "
            f"the files get a private download link and the email below goes to {shown}.")


def _release_lines(payload: Mapping[str, Any]) -> list[str]:
    body = payload.get("body_text")
    text = (body if isinstance(body, str) else str(body)).replace(LINK_PLACEHOLDER,
                                                                  DOWNLOAD_LINK_SHOWN)
    return [
        f"**Order:** `{_fence_safe(str(payload.get('order_id')))}`",
        f"**Delivery:** `{_fence_safe(str(payload.get('delivery_id')))}` (held until now)",
        (f"**To:** `{_fence_safe(str(payload.get('to')))}` (Scrooge sends only to the "
         "address stored on this order)"),
        f"**Subject:** {_escape(str(payload.get('subject')))}",
        (f"**The email, in full, exactly as it will be sent - `{DOWNLOAD_LINK_SHOWN}` becomes "
         "the link to the zip:**"),
        _FENCE + "text", _fence_safe(text), _FENCE,
    ]


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
                   product: Mapping[str, Any] | None = None,
                   quote_reply: Mapping[str, Any] | None = None,
                   digest: bool = False) -> str:
    """The whole text of an approval message, before it is split to fit Discord.
    Nothing that says what the action does is ever cut; long text is split
    across messages instead. ``delivery`` is a client.deliver's zip as inspected on
    disk (ClientAdapter.delivery_preview), or None if it could not be; ``product`` is a
    product.gumroad_publish's files as inspected (ProductAdapter.product_preview).
    ``digest``: the row waits in the daily digest, so this is its full preview, answered
    by its number on the digest card (or here)."""

    payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
    publishes = row.get("capability") == PUBLISH
    posts = row.get("capability") == INSTAGRAM_POST
    crossposts = row.get("capability") == DEVTO_CROSSPOST
    emails = row.get("capability") == CLIENT_EMAIL
    delivers = row.get("capability") == CLIENT_DELIVER
    finds = row.get("capability") == CLIENT_FIND_REPORT
    sells = row.get("capability") == PRODUCT_PUBLISH
    quotes = row.get("capability") == CLIENT_QUOTE
    reminds = row.get("capability") == CLIENT_REMIND
    releases = row.get("capability") == CLIENT_RELEASE
    lines: list[str] = []
    if quotes:
        lines.append(quote_line(payload.get("to")))
    if reminds:
        lines.append(reminder_line(payload.get("to")))
    if releases:
        lines.append(release_line(payload.get("to")))
    if sells:
        lines.append(product_line(payload))
        executables = _executables_line(payload, product)
        if executables:
            lines.append(executables)
    if delivers:
        lines.append(client_deliver_line(payload.get("to")))
        if payload.get("hold_for_balance") is True:
            lines.append(HOLD_LINE)
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
    if owner and digest:
        lines.append(f"\U0001f4ec **Waits in the daily digest** - approve it with its number "
                     f"on the digest card (or {APPROVE} here); react {DENY} here to reject it. "
                     f"Only <@{owner}>'s reaction counts.")
    elif owner:
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
    if emails or reminds:
        lines += _client_email_lines(payload)
        if reminds:
            lines.append(f"**Quote:** `{_fence_safe(str(payload.get('quote_id')))}`")
        if isinstance(payload.get("body_text"), str):
            shown = {**payload, "body_text": f"(the full message above, "
                                             f"{len(payload['body_text']):,} characters)"}
    if quotes:
        lines += _quote_lines(payload, quote_reply)
        if isinstance(payload.get("body_text"), str):
            shown = {**payload, "body_text": f"(the full email above, "
                                             f"{len(payload['body_text']):,} characters)"}
    if releases:
        lines += _release_lines(payload)
        if isinstance(payload.get("body_text"), str):
            shown = {**payload, "body_text": f"(the full email above, "
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


# ---------------------------------------------------------------- the daily digest
# One keycap per item on a digest card: react the number to approve that item.
NUMBERS = tuple(f"{n}️⃣" for n in range(1, 10)) + ("\U0001f51f",)
DIGEST_MAX_ITEMS = len(NUMBERS)
_DIGEST_KINDS = {PUBLISH: "Blog post", DEVTO_CROSSPOST: "dev.to cross-post",
                 INSTAGRAM_POST: "Instagram post", PRODUCT_PUBLISH: "Gumroad listing"}
_LONGEST_LINK = "https://discord.com/channels/" + "/".join(["9" * 20] * 3)


def _emoji_key(name: Any) -> str:
    """Discord may or may not keep the variation selector on a keycap's name."""
    return str(name or "").replace("️", "")


def digest_kind(row: Mapping[str, Any]) -> str:
    capability = str(row.get("capability"))
    return _DIGEST_KINDS.get(capability, capability)


def digest_title(row: Mapping[str, Any]) -> str:
    """Enough to recognise the item on the card; its full preview says everything."""
    payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
    capability = row.get("capability")
    title: Any = None
    if capability in (PUBLISH, DEVTO_CROSSPOST):
        title = payload.get("title")
    elif capability == INSTAGRAM_POST:
        title = payload.get("headline")
    elif capability == PRODUCT_PUBLISH:
        name = payload.get("name")
        if isinstance(name, str) and name.strip():
            price = price_text(payload.get("price_cents"), payload.get("pay_what_you_want"))
            title = f"{name.strip()} ({price})"
    if not isinstance(title, str) or not title.strip():
        title = row.get("summary") or row.get("id")
    text = " ".join(str(title).split())
    return text if len(text) <= 80 else text[:79] + "…"


def digest_status(row: Mapping[str, Any] | None, carried_to: str | None = None) -> str:
    """One item's standing, as its line on a digest card shows it."""
    if row is None:
        return "❔ gone from the queue"
    status = row.get("status")
    if status == "pending":
        return f"↪️ carried over to {carried_to}" if carried_to else "⏳ waiting"
    if status == "running":
        return "▶️ approved, running"
    if status == "approved":
        return "✅ approved, ran OK"
    if status == "approved_failed":
        return "⚠️ approved, run FAILED"
    if status == "denied" and row.get("reason") == "expired":
        return "⌛ expired, not run"
    if status == "denied" and row.get("reason") == "interrupted":
        return "⚠️ interrupted"
    if status == "denied":
        return "❌ rejected, not run"
    return f"❔ {_escape(str(status))}"


def _parked(row: Mapping[str, Any] | None) -> str:
    try:
        created = datetime.fromisoformat(str((row or {}).get("created_at")))
    except ValueError:
        return ""
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    local = created.astimezone()
    return f"{local:%b} {local.day}"


def superseded_line(day: str) -> str:
    return (f"↪️ **Superseded by the digest of {day}** - every item still waiting is "
            "listed there; reactions here no longer count.")


DECIDED_LINE = "✔️ **Every item on this card is decided.**"


def render_digest_card(day: str, index: int, total: int,
                       items: list[tuple[Mapping[str, Any] | None, str | None, str]],
                       owner: str | None, expire_days: int, *, requested: bool = False,
                       closing: str | None = None) -> str:
    """A digest card: one numbered line per item (its kind, title, when it was parked and
    a link to its full preview) and how to answer. ``items`` is (row, preview link,
    status) in card order; ``closing`` goes on top once the card is final."""

    count = len(items)
    head = (f"\U0001f4ec **Daily digest · {day}** · card {index} of {total} · "
            f"{count} routine public item{'s' if count != 1 else ''}"
            + (" · sent early, as you asked" if requested else ""))
    lines = [closing, ""] if closing else []
    lines += [head, ("Nothing here spends money or contacts a client - those always get "
                     "their own card at once.")]
    for number, (row, link, status) in zip(NUMBERS, items, strict=False):
        kind = digest_kind(row) if row is not None else "?"
        title = digest_title(row) if row is not None else "(no longer in the queue)"
        line = f"{number} {status} · **{_escape(kind)}** — {_escape(title)}"
        parked = _parked(row)
        if parked:
            line += f" · parked {parked}"
        line += (f" · [full preview](<{link}>)" if link
                 else " · (preview not posted: not approvable here)")
        lines.append(line)
    if owner:
        lines.append(f"**Answer:** react a number to approve that item, or {APPROVE} to approve "
                     f"every item still waiting here. To reject one, react {DENY} on its full "
                     f"preview. Undecided items carry over to the next digest; unanswered for "
                     f"{expire_days} days, an item expires and does not run. Only "
                     f"<@{owner}>'s reactions count.")
    else:
        lines.append("⚠️ **This gate cannot accept answers: no owner user id is "
                     "configured (PIONIR_DISCORD_USER_ID).** Approve or deny from the phone page.")
    return "\n".join(lines)


def render_expired(rows: list[Mapping[str, Any]], expire_days: int) -> str:
    """The report of batched items that expired unanswered: denied, never run."""
    lines = [(f"⌛ **Expired, not run** - unanswered for {expire_days} days in the daily "
              "digest, so these were denied and did **not** run:")]
    for row in rows:
        lines.append(f"• **{_escape(digest_kind(row))}** — "
                     f"{_escape(digest_title(row))} · parked {_parked(row) or '?'} "
                     f"· id `{row.get('id')}`")
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


def _fresh_digest_state() -> dict[str, Any]:
    """The gate's own record of the daily digest (saved in gate.json with the cards)."""
    return {"last_run_at": None, "handled_request": None, "run": None, "cards": {},
            "reported_expired": []}


def _parse_when(stamp: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _older_than(stamp: Any, cutoff: datetime) -> bool:
    parsed = _parse_when(stamp) if stamp else None
    return parsed is not None and parsed < cutoff


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
        quotes: QuoteReplies | None = None,
        digest: DigestSettings | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        # The daily digest of batched (routine public) approvals: when, and for how long
        # an item may wait. ``clock`` is local, tz-aware wall time (tests fake it).
        self._digest = digest or DigestSettings.from_environment(settings.state_root)
        self._clock = clock or local_now
        self._dstate: dict[str, Any] = _fresh_digest_state()
        self._guild: str | None = None
        # The owner's replies to quote cards -> parked client.quote (pionir/quotes.py).
        self._quotes = quotes
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
        order = getattr(client, "order", None)
        if callable(order) and "quotes" not in kwargs:
            # Replies are read only for cards in this record: with no quote card posted, the
            # gate never reads the channel's messages nor asks Scrooge for an order.
            kwargs["quotes"] = QuoteReplies(
                QuoteCardStore.for_state_root(settings.state_root),
                submit=lambda capability, payload: app.run_task(capability, payload, wait=0),
                deny=app.deny, find_approvals=app.approvals.find, get_order=order)
        if isinstance(getattr(app, "digest", None), DigestSettings):
            kwargs.setdefault("digest", app.digest)   # the same settings that batch at enqueue
        return cls(settings, approvals=app.approvals, approve=app.approve, deny=app.deny,
                   **kwargs)

    def _batched(self, row: Mapping[str, Any]) -> bool:
        """This row waits for the daily digest (and is not posted on its own)."""
        return bool(row.get("batch")) and self._digest.enabled

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
        digest = self._batched(row)
        if row.get("capability") == CLIENT_QUOTE:
            payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
            store = (self._quotes.store if self._quotes is not None
                     else QuoteCardStore.for_state_root(self.settings.state_root))
            reply = owner_reply(store, payload.get("order_id"), payload.get("quote_ref"))
            if reply is not None and str(reply.get("by")) != str(self.settings.owner):
                reply = None
            return render_request(row, self.settings.owner, quote_reply=reply, digest=digest)
        if row.get("capability") == PRODUCT_PUBLISH:
            return render_request(row, self.settings.owner, product=preview, digest=digest)
        return render_request(row, self.settings.owner, delivery=preview, digest=digest)

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
            "digest": {"enabled": self._digest.enabled, "time": self._digest.time_text,
                       "expire_days": self._digest.expire_days,
                       "last_run_at": self._dstate.get("last_run_at"),
                       "open_cards": sum(1 for c in self._dstate["cards"].values()
                                         if not c.get("final"))},
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
        guild = channel.get("guild_id") if isinstance(channel, dict) else None
        self._guild = str(guild) if guild else None    # for links to a digest item's preview
        _log.info("discord gate up as %s in channel %s; owner %s",
                  self._bot.get("username"),
                  channel.get("name") if isinstance(channel, dict) else self.settings.channel_id,
                  "configured" if self.settings.owner else "NOT configured - answers refused")

    def _tick(self) -> None:
        if self._quotes is not None:
            # First, so a quote card parked from a reply is posted in this same pass.
            self._guard("quote-replies", lambda: self._quotes.tick(
                self._call, self.settings.owner, str(self.settings.channel_id)))
        pending = self._approvals.pending()     # also auto-denies the expired
        fresh = set()
        for row in pending:
            if row["id"] not in self._entries:
                if self._batched(row):
                    continue     # waits for the daily digest, which posts its preview
                fresh.add(row["id"])
                self._guard(row["id"], lambda row=row: self._post(row))
        # Every card and digest preview first - so an owner's reject on a preview is
        # always read before an approve-all on the digest card in the same pass.
        for approval_id, entry in list(self._entries.items()):
            if entry.get("final") or approval_id in fresh:
                continue
            self._guard(approval_id, lambda a=approval_id, e=entry: self._follow(a, e))
        self._guard("digest", self._digest_tick)
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
        # A digest item's preview never pings: the digest card itself is the one ping.
        ping = bool(owner and not status and not self._batched(row))
        mentions: dict[str, Any] = {"parse": [], "users": [owner] if ping else []}
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

    # ------------------------------------------------------------ the daily digest
    # Batched rows are ordinary queue rows that wait: once a day (or when the owner asks)
    # every one still pending gets its full preview posted - the very card it would have
    # had, answerable with ✅/❌ like any card, but without a ping - and then one digest
    # card per up-to-ten items lists them, numbered, each linking its preview. A number
    # on the card approves that item, ✅ approves every item still waiting on the card;
    # both go through the same _answer -> PionirApp.approve as any card, so an item runs
    # exactly as its own card would and never twice. A newer digest supersedes the older
    # cards (their reactions stop counting) and lists what they left undecided again.
    #
    # Restart-safe: a digest run is planned (items, pages) and saved before anything is
    # posted, and every preview and card is saved the moment Discord has it; a restart
    # finishes the planned run instead of starting another. The worst a crash can do is
    # post one card twice (the untracked copy never counts) - never drop an item, and
    # never run one twice (that is the queue's atomic claim).

    def _digest_tick(self) -> None:
        state = self._dstate
        for key, card in list(state["cards"].items()):
            if not card.get("final"):
                self._guard(f"digest card {key}",
                            lambda k=key, c=card: self._follow_card(k, c))
        run = state.get("run")
        if isinstance(run, dict) and not run.get("complete"):
            self._continue_run(run)
            return
        if not self._digest.enabled:
            return
        reason, request = self._digest_due()
        if reason is not None:
            self._start_run(reason, request)

    def _digest_due(self) -> tuple[str | None, dict[str, Any] | None]:
        """("requested", request) when the owner asked for it and it is not yet answered;
        ("scheduled", None) when today's digest time has passed since the last run."""
        request = read_request(self.settings.state_root)
        if request is not None and request.get("answered") is not True \
                and request.get("id") != self._dstate.get("handled_request"):
            return "requested", request
        now = self._clock()
        last = _parse_when(self._dstate.get("last_run_at"))
        if last is None or last < self._digest.last_slot(now):
            return "scheduled", None
        return None, None

    def _start_run(self, reason: str, request: Mapping[str, Any] | None) -> None:
        now = self._clock()
        day = now.date().isoformat()
        items = [r["id"] for r in self._approvals.pending() if self._batched(r)]
        reported = set(self._dstate["reported_expired"])
        expired = [r["id"] for r in self._approvals.batched_expired() if r["id"] not in reported]
        run = {"id": uuid4().hex[:12], "date": day, "reason": reason,
               "request_id": (request or {}).get("id"), "started_at": now.isoformat(),
               "items": items, "pages": self._pack(items, day, reason == "requested"),
               "posted": {}, "expired": expired, "expired_posted": not expired,
               "complete": False}
        self._dstate["run"] = run
        if request is not None:
            self._dstate["handled_request"] = request.get("id")
        self._save()                        # planned before anything is posted
        _log.info("discord gate: digest of %s (%s): %d item(s), %d expired", day, reason,
                  len(items), len(expired))
        self._continue_run(run)

    def _continue_run(self, run: dict[str, Any]) -> None:
        day = run["date"]
        cards = self._dstate["cards"]
        # Older cards stop counting: what they left undecided is listed again below.
        for card in list(cards.values()):
            superseded = card.get("final") or card.get("superseded_by")
            if card.get("run") != run["id"] and not superseded:
                self._supersede(card, day)
        if not run.get("expired_posted"):
            rows = [r for r in (self._approvals.get(a) for a in run["expired"]) if r]
            if rows:
                for chunk in split_message(render_expired(rows, self._digest.expire_days),
                                           MESSAGE_LIMIT):
                    self._call("POST", f"/channels/{self.settings.channel_id}/messages",
                               {"content": chunk, "allowed_mentions": {"parse": []}})
            run["expired_posted"] = True
            self._save()
        pending = {r["id"]: r for r in self._approvals.pending()}
        for approval_id in run["items"]:
            row = pending.get(approval_id)
            entry = self._entries.get(approval_id)
            if row is not None and (entry is None or entry.get("final")):
                # its full preview, saved the moment it is sent. One that cannot be posted
                # does not hold up the rest: its line says so and it is not approvable
                # from the card (never approved blind); the next digest tries again.
                self._guard(approval_id, lambda row=row: self._post(row))
        total = len(run["pages"])
        for index, page in enumerate(run["pages"], start=1):
            if str(index) not in run["posted"]:
                self._post_card(run, index, total, page)
        run["complete"] = True
        self._dstate["last_run_at"] = run["started_at"]
        self._dstate["reported_expired"] += [a for a in run["expired"]
                                             if a not in self._dstate["reported_expired"]]
        self._save()
        if run.get("request_id"):
            answer_request(self.settings.state_root, run["request_id"])

    def _pack(self, items: list[str], day: str, requested: bool) -> list[list[str]]:
        """Items into cards: at most ten each, and never more than one message can hold
        (measured with the longest link, status and closing line a card can ever show)."""
        rows = {a: self._approvals.get(a) for a in items}
        worst = digest_status({"status": "pending"}, day)
        pages: list[list[str]] = []
        current: list[str] = []
        for approval_id in items:
            trial = [*current, approval_id]
            text = render_digest_card(
                day, 99, 99, [(rows[a], _LONGEST_LINK, worst) for a in trial],
                self.settings.owner, self._digest.expire_days, requested=requested,
                closing=superseded_line(day))
            if current and (len(trial) > DIGEST_MAX_ITEMS or len(text) > MESSAGE_LIMIT):
                pages.append(current)
                current = [approval_id]
            else:
                current = trial
        if current:
            pages.append(current)
        return pages

    def _preview_link(self, approval_id: str) -> str | None:
        entry = self._entries.get(approval_id)
        if not entry or not entry.get("message_id"):
            return None
        return (f"https://discord.com/channels/{self._guild or '@me'}/"
                f"{entry.get('channel_id')}/{entry['message_id']}")

    def _card_text(self, card: Mapping[str, Any],
                   rows: Mapping[str, Mapping[str, Any] | None]) -> str:
        carried = card.get("superseded_by")
        items = [(rows.get(a), self._preview_link(a), digest_status(rows.get(a), carried))
                 for a in card["items"]]
        closing = None
        if carried:
            closing = superseded_line(carried)
        elif card.get("final"):
            closing = DECIDED_LINE
        text = render_digest_card(card["date"], card["index"], card["total"], items,
                                  self.settings.owner, self._digest.expire_days,
                                  requested=bool(card.get("requested")), closing=closing)
        return text if len(text) <= MESSAGE_LIMIT else text[:MESSAGE_LIMIT - 1] + "…"

    def _post_card(self, run: dict[str, Any], index: int, total: int, page: list[str]) -> None:
        channel = self.settings.channel_id
        key = f"{run['id']}-{index}"
        card: dict[str, Any] = {
            "run": run["id"], "date": run["date"], "index": index, "total": total,
            "items": list(page), "requested": run.get("reason") == "requested",
            "channel_id": channel, "final": False, "reactions_added": False,
        }
        rows = {a: self._approvals.get(a) for a in page}
        text = self._card_text(card, rows)
        owner = self.settings.owner
        ping = [owner] if owner and index == 1 else []
        sent = self._call("POST", f"/channels/{channel}/messages",
                          {"content": text, "allowed_mentions": {"parse": [], "users": ping}})
        card.update({"message_id": str(sent["id"]), "posted_at": _now().isoformat(),
                     "shown": text})
        self._dstate["cards"][key] = card
        run["posted"][str(index)] = key
        self._save()                        # saved at once: a restart never re-posts it
        self._approvals.note_digest(page, run["date"])
        self._add_card_reactions(card)

    def _add_card_reactions(self, card: dict[str, Any]) -> None:
        if not self.settings.owner:
            return
        base = f"/channels/{card['channel_id']}/messages/{card['message_id']}/reactions"
        for emoji in (*NUMBERS[:len(card["items"])], APPROVE):
            self._call("PUT", f"{base}/{urllib.parse.quote(emoji)}/@me")
        card["reactions_added"] = True
        self._save()

    def _follow_card(self, key: str, card: dict[str, Any]) -> None:
        rows = {a: self._approvals.get(a) for a in card["items"]}
        waiting = [a for a in card["items"] if (rows[a] or {}).get("status") == "pending"]
        # A superseded card still shows how its items ended, but its reactions no longer
        # count: its numbers are not the numbers on the newer card.
        if waiting and not card.get("superseded_by"):
            path = f"/channels/{card['channel_id']}/messages/{card['message_id']}"
            try:
                message = self._call("GET", path)
            except DiscordError as error:
                if error.status != 404:
                    raise
                _log.warning("discord gate: digest card %s was deleted; re-posting", key)
                sent = self._call("POST", f"/channels/{card['channel_id']}/messages",
                                  {"content": self._card_text(card, rows),
                                   "allowed_mentions": {"parse": []}})
                card.update({"message_id": str(sent["id"]), "reactions_added": False,
                             "shown": None})
                self._save()
                self._guard(key, lambda: self._add_card_reactions(card))
                return
            if not card.get("reactions_added"):
                self._guard(key, lambda: self._add_card_reactions(card))
            chosen, by = self._card_decision(card, message, waiting)
            for approval_id in chosen:
                # through the same _answer as a card's own ✅, which also marks its preview
                self._answer(approval_id, self._entries[approval_id], "approve", by)
            if chosen:
                rows = {a: self._approvals.get(a) for a in card["items"]}
        done = not any((rows[a] or {}).get("status") in ("pending", "running")
                       for a in card["items"])
        if done:
            card["final"] = True
            card["final_at"] = _now().isoformat()
        self._edit(card, self._card_text(card, rows))
        if done:
            self._save()

    def _card_decision(self, card: Mapping[str, Any], message: Any,
                       waiting: list[str]) -> tuple[list[str], dict[str, Any]]:
        """The items the owner approved on this card: a number is that item, ✅ is every
        item still waiting. Only the owner's reactions count; with no owner configured,
        or with the owner's ❌ on the card itself (ambiguous: "reject" is answered on an
        item's preview, never here), nothing on this card is approved."""

        owner = self.settings.owner
        reactions = message.get("reactions") if isinstance(message, dict) else None
        if owner is None or not isinstance(reactions, list):
            return [], {}
        # Never approved blind: an item whose full preview is not posted cannot be
        # approved from the card (its line says so).
        waiting = [a for a in waiting if self._preview_link(a)]
        numbered = {_emoji_key(n): a for n, a in zip(NUMBERS, card["items"], strict=False)}
        base = f"/channels/{card['channel_id']}/messages/{card['message_id']}/reactions"
        chosen: set[str] = set()
        by: dict[str, Any] = {}
        refused = False
        for reaction in reactions:
            name = (reaction.get("emoji") or {}).get("name")
            key = _emoji_key(name)
            if key == _emoji_key(APPROVE):
                targets = list(waiting)
            elif key == _emoji_key(DENY):
                targets = []
            elif numbered.get(key) in waiting:
                targets = [numbered[key]]
            else:
                continue
            others = int(reaction.get("count") or 0) - (1 if reaction.get("me") else 0)
            if others <= 0 or (not targets and key != _emoji_key(DENY)):
                continue
            users = self._call("GET", f"{base}/{urllib.parse.quote(str(name))}?limit=100")
            for user in users if isinstance(users, list) else []:
                if str(user.get("id")) == owner:          # only the owner counts
                    if key == _emoji_key(DENY):
                        refused = True
                    else:
                        chosen.update(targets)
                        by = user
        if refused:
            return [], {}
        return [a for a in card["items"] if a in chosen], by

    def _supersede(self, card: dict[str, Any], day: str) -> None:
        """A newer digest lists this card's undecided items: this card's reactions stop
        counting. It is still followed - only to show how its items end."""
        rows = {a: self._approvals.get(a) for a in card["items"]}
        card["superseded_by"] = day
        self._edit(card, self._card_text(card, rows))
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
        digest = data.get("digest")
        if isinstance(digest, dict):
            state = _fresh_digest_state()
            state.update({k: v for k, v in digest.items() if k in state})
            if not isinstance(state["cards"], dict):
                state["cards"] = {}
            if not isinstance(state["reported_expired"], list):
                state["reported_expired"] = []
            self._dstate = state

    def _save(self) -> None:
        path = self.settings.state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        # One file, one atomic write: an item's preview and the digest card that lists it
        # are never saved out of step with each other.
        tmp.write_text(json.dumps({"messages": self._entries, "digest": self._dstate},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
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
        cards = self._dstate["cards"]
        old_cards = [key for key, card in cards.items() if card.get("final")
                     and _older_than(card.get("final_at"), cutoff)]
        for key in old_cards:
            del cards[key]
        reported = self._dstate["reported_expired"]
        if len(reported) > 500:        # the queue drops resolved rows after a week anyway
            del reported[:-500]
        if drop or old_cards:
            self._save()
