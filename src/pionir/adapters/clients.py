"""Adapter for client orders: Dokaz's paid "I do it for you" orders on api.dokaz.net.

Scrooge (the Dokaz API) takes the orders; this adapter lets Pionir read them, move one
along, and write to the client who placed it. Ian's rule: EVERY email to a client is
approved by him on Discord before it is sent, and holding a permission is not a way
around it. So:

- ``client.orders`` is READ_ONLY: the orders, as Scrooge lists them. Nobody is contacted.
- ``client.email`` is PRIVILEGED with ``requires_approval=True``: PionirApp parks it on
  EVERY call, and the Discord card shows the recipient, the subject and the whole message
  before he answers. Scrooge only ever emails the address stored on the order; ``to`` is a
  check that the owner saw the right address (Scrooge answers 409 if it does not match).
- ``client.set_status`` is REVERSIBLE_WRITE and does NOT require approval: it only moves
  the order's status in Scrooge's records - nobody is contacted, nothing is paid out.

All three are ``routable=False``: reached only by name.

An email is checked here before anything is parked or sent - a bad one is refused by
Pionir with ``AdapterProtocolError`` naming the field and why, in Scrooge's
``<field>: <why>`` shape: plain text only (nothing tag-shaped), no control or invisible
characters, and links only as https: to the Dokaz hosts.

The ops token is read from a file on every call (so adding it needs no restart), sent only
in the ``x-dash-token`` header, and never appears in a log, an error or a result. Scrooge
saying no (a field, an address that does not match the order, a status it will not move
to) comes back as ``ok: false`` with ``refused``; a missing or rejected token, a rate
limit, or Scrooge not answering as ``ok: false`` with ``unavailable`` - answers, not
faults, so the circuit breaker is not tripped and the plain answer is not hidden.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pionir.adapters.content import (
    _BAD_SCHEME,
    _BARE_WWW,
    _CONTROL_LINE,
    _HTML_COMMENT,
    _HTML_DECL,
    _HTML_TAG,
    _INVISIBLE,
    _OTHER_SCHEME,
    _SCHEMED_URL,
    DEFAULT_CONTENT_URL,
    _link_problem,
    read_token,
)
from pionir.contracts import AgentManifest, Capability, RiskLevel, Task, TaskResult
from pionir.errors import AdapterProtocolError, AdapterUnavailable

_log = logging.getLogger(__name__)

ORDERS = "client.orders"
EMAIL = "client.email"
SET_STATUS = "client.set_status"

ORDER_STATUSES = ("awaiting_payment", "paid", "in_progress", "delivered", "declined",
                  "refunded", "quote_requested", "quoted")
# What client.set_status may move an order to: the owner's own steps. Payment
# (awaiting_payment -> paid) and a quote request are Scrooge's to record, never Pionir's.
SETTABLE_STATUSES = ("in_progress", "delivered", "declined", "refunded", "quoted")
EMAIL_FIELDS = frozenset({"order_id", "to", "subject", "body_text"})

SETUP_HINT = r"run Scrooge's tools\setup-ops-token.ps1"
TOKEN_REJECTED = f"the ops token was rejected - {SETUP_HINT}"
RATE_LIMITED = "too many emails to this order today"
CHECK_BEFORE_RETRY = ("check the order's messages before retrying - the email may have "
                      "been sent")
MAX_RESPONSE_BYTES = 4_000_000

# ---- the email rules -----------------------------------------------------------------
_ORDER_ID = re.compile(r"[0-9a-f]{12}")
_ADDRESS = re.compile(
    r"[A-Za-z0-9_%+-]+(?:\.[A-Za-z0-9_%+-]+)*"
    r"@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}"
)
MAX_ADDRESS = 200
SUBJECT_LENGTH = (5, 120)
BODY_LENGTH = (20, 5000)
# Anything tag-shaped on one line (<b>, <3 days>, <https://...>) - the message is plain
# text, and a mail client may render what looks like markup.
_ANGLED = re.compile(r"<[^<>\n]*>")
# Every control character but the newline (a CRLF pair counts as one).
_CONTROL_TEXT = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")
# A host with a path written without a scheme (evil.example/login): mail clients link it.
_BARE_HOST_PATH = re.compile(
    r"(?<![\w@./:%-])(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}/[^\s<>()\[\]\"'`]*")
# Punctuation that ends a sentence, not a link (mail clients leave it out of the link).
_TRAILING = ".,;:!?'\")]}"


def check_order_id(value: Any) -> str:
    if not isinstance(value, str) or not _ORDER_ID.fullmatch(value):
        raise ValueError("order_id: 12 lowercase hex characters")
    return value


def check_address(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("to: required, an email address")
    if len(value) > MAX_ADDRESS:
        raise ValueError(f"to: at most {MAX_ADDRESS} characters")
    if not _ADDRESS.fullmatch(value) or len(value.split("@", 1)[0]) > 64:
        # the address itself is not echoed: it is someone's contact details
        raise ValueError("to: not a valid email address")
    return value


def _plain_text_problem(text: str) -> str | None:
    """Plain text with safe links - for the subject and the message."""
    if _INVISIBLE.search(text):
        return "contains an invisible direction or zero-width character"
    if (_HTML_COMMENT.search(text) or _HTML_DECL.search(text) or _HTML_TAG.search(text)
            or _ANGLED.search(text)):
        return "plain text only - nothing tag-shaped like <...>"
    if _BAD_SCHEME.search(text):
        return "javascript:, data: and vbscript: are not allowed"
    if _OTHER_SCHEME.search(text):
        return "mailto:, tel: and file: links are not allowed"
    for match in _SCHEMED_URL.finditer(text):
        problem = _link_problem(match.group(0).rstrip(_TRAILING))
        if problem:
            return problem
    for pattern in (_BARE_WWW, _BARE_HOST_PATH):
        for match in pattern.finditer(text):
            return (f"write a link as https://..., not {match.group(0)[:80]!r} "
                    "(mail clients link it as it stands)")
    return None


def check_email(payload: Mapping[str, Any]) -> dict[str, str]:
    """The email exactly as it will be sent, or ValueError("<field>: <why>")."""
    unknown = sorted(set(payload) - EMAIL_FIELDS)
    if unknown:
        raise ValueError(f"{unknown[0]}: not an email field (allowed: "
                         f"{', '.join(sorted(EMAIL_FIELDS))})")
    missing = sorted(EMAIL_FIELDS - set(payload))
    if missing:
        raise ValueError(f"{missing[0]}: required")
    email = {"order_id": check_order_id(payload["order_id"]),
             "to": check_address(payload["to"])}
    for key, (low, high), control in (("subject", SUBJECT_LENGTH, _CONTROL_LINE),
                                      ("body_text", BODY_LENGTH, _CONTROL_TEXT)):
        text = payload[key]
        if not isinstance(text, str):
            # ValueError like every other email rule: one "<field>: <why>" refusal type
            raise ValueError(f"{key}: required, a string")  # noqa: TRY004
        if not low <= len(text) <= high:
            raise ValueError(f"{key}: {low}-{high} characters (this is {len(text)})")
        if not text.strip():
            raise ValueError(f"{key}: cannot be blank")
        if control.search(text.replace("\r\n", "\n") if key == "body_text" else text):
            raise ValueError(f"{key}: contains a control character"
                             + (" (only line breaks are allowed)" if key == "body_text"
                                else " or line break (one line only)"))
        problem = _plain_text_problem(text)
        if problem:
            raise ValueError(f"{key}: {problem}")
        email[key] = text
    return email


def check_status_change(payload: Mapping[str, Any]) -> dict[str, str]:
    unknown = sorted(set(payload) - {"order_id", "status"})
    if unknown:
        raise ValueError(f"{unknown[0]}: not a status field (only order_id and status)")
    status = payload.get("status")
    if status not in SETTABLE_STATUSES:
        raise ValueError(f"status: one of {', '.join(SETTABLE_STATUSES)}")
    return {"order_id": check_order_id(payload.get("order_id")), "status": status}


def check_listing(payload: Mapping[str, Any]) -> str | None:
    unknown = sorted(set(payload) - {"status"})
    if unknown:
        raise ValueError(f"{unknown[0]}: not an orders field (only status)")
    status = payload.get("status")
    if status is None:
        return None
    if status not in ORDER_STATUSES:
        raise ValueError(f"status: one of {', '.join(ORDER_STATUSES)}")
    return str(status)


# ---- settings -----------------------------------------------------------------------------
Opener = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class ClientSettings:
    """Where Scrooge is and where the ops token lives (its PATH, never the token)."""

    base_url: str = DEFAULT_CONTENT_URL
    token_file: Path = Path("~/.pionir/secrets/scrooge-ops-token.txt")
    timeout_seconds: int = 30

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlparse(self.base_url)
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        # The token is a secret: it only ever travels over TLS, or to this machine.
        if not (parsed.scheme == "https" or (parsed.scheme == "http" and loopback)):
            raise ValueError("the orders URL must be https: (or http: on loopback)")
        if not parsed.hostname:
            raise ValueError("the orders URL needs a host")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("the orders URL cannot carry credentials or query data")
        if self.timeout_seconds < 5:
            raise ValueError("the orders timeout must be at least 5 seconds")
        object.__setattr__(self, "token_file", Path(self.token_file).expanduser())


def _refused(why: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "refused": why, "error": why, **extra}


def _unavailable(why: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "unavailable": why, "error": why, **extra}


class ClientAdapter:
    """Client orders as gated, audited Pionir capabilities."""

    def __init__(self, settings: ClientSettings | None = None, *,
                 opener: Opener | None = None) -> None:
        self.settings = settings or ClientSettings()
        self._base = self.settings.base_url.rstrip("/")
        self._open = opener or urllib.request.build_opener().open
        self._manifest = AgentManifest(
            agent_id="client",
            version="pionir/client",
            capabilities=(
                Capability(
                    name=ORDERS,
                    description="List the paid Dokaz client orders (optionally by status)",
                    risk=RiskLevel.READ_ONLY,
                    routable=False,
                ),
                Capability(
                    name=EMAIL,
                    description="Email the client who placed an order "
                                "(only after the owner approves the message)",
                    risk=RiskLevel.PRIVILEGED,
                    required_permissions=frozenset({EMAIL}),
                    requires_approval=True,
                    routable=False,
                ),
                Capability(
                    name=SET_STATUS,
                    description="Move a client order to in_progress, delivered, declined, "
                                "refunded or quoted (records only; nobody is contacted)",
                    risk=RiskLevel.REVERSIBLE_WRITE,
                    routable=False,
                ),
            ),
        )

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    # ---- configuration (local files only) ------------------------------------------
    def _not_configured(self) -> str:
        return f"not configured: no ops token at {self.settings.token_file} - {SETUP_HINT}"

    def status(self) -> Mapping[str, Any]:
        """Local only - no call to Scrooge, so doctor stays hermetic and fast."""
        if read_token(self.settings.token_file) is None:
            raise AdapterUnavailable(self._not_configured())
        return {"url": self._base, "token": "configured"}

    # ---- the request ---------------------------------------------------------------
    def validate(self, task: Task) -> None:
        """Refuse a bad request, or an email that cannot be sent, before it is parked."""
        self._request(task)
        if task.capability == EMAIL and read_token(self.settings.token_file) is None:
            # Asking the owner to approve an email that cannot be sent wastes his yes.
            raise AdapterUnavailable(self._not_configured())

    @staticmethod
    def _request(task: Task) -> tuple[str, str, dict[str, Any] | None]:
        """(method, path, JSON body) for this task, or AdapterProtocolError."""
        try:
            if task.capability == ORDERS:
                status = check_listing(task.payload)
                query = f"?{urllib.parse.urlencode({'status': status})}" if status else ""
                return "GET", f"/dash/orders{query}", None
            if task.capability == EMAIL:
                return "POST", "/dash/orders/email", check_email(task.payload)
            if task.capability == SET_STATUS:
                return "POST", "/dash/orders/status", check_status_change(task.payload)
        except ValueError as error:
            raise AdapterProtocolError(f"{task.capability} refused by Pionir - {error}") from error
        raise AdapterProtocolError(f"client has no capability {task.capability!r}")

    # ---- the call ------------------------------------------------------------------
    def execute(self, task: Task) -> TaskResult:
        method, path, body = self._request(task)
        token = read_token(self.settings.token_file)
        if token is None:
            return self._result(task, _unavailable(self._not_configured(),
                                                   not_configured=True))
        status, document = self._http(method, path, body, token)
        output = self._answer(task.capability, status, document, token)
        if task.capability == EMAIL:
            _log.info("client: email for order %s: %s", body["order_id"] if body else "?",
                      "sent" if output.get("ok") is True else "not sent")
        # Belt and braces: whatever Scrooge echoed, the token does not leave in the result.
        return self._result(task, json.loads(self._scrub(json.dumps(output), token)))

    def _answer(self, capability: str, status: int, document: Any,
                token: str) -> dict[str, Any]:
        doc = document if isinstance(document, Mapping) else {}
        said = self._scrub(str(doc.get("error") or ""), token)[:300]
        emailing = capability == EMAIL
        if 200 <= status < 300:
            if doc.get("ok") is True:
                return dict(doc)
            why = "Scrooge answered without ok: true"
            return _unavailable(f"{why} - {CHECK_BEFORE_RETRY}" if emailing else why,
                                status=status)
        if status in (400, 409):
            # Scrooge said no to this request: an answer, not a fault. Nothing was sent.
            return _refused(said or f"Scrooge refused it (HTTP {status})", status=status)
        if status in (401, 403):
            return _unavailable(TOKEN_REJECTED, status=status, token_rejected=True)
        if status == 429:
            why = RATE_LIMITED if emailing else "Scrooge is rate limiting - try again later"
            return _unavailable(why, status=429)
        if status == 0:
            why = f"Scrooge is unreachable at {urllib.parse.urlparse(self._base).netloc}"
        else:
            why = f"Scrooge answered HTTP {status}" + (f": {said}" if said else "")
        if emailing and (status == 0 or status >= 500):
            # Unanswered or a server error: the email may or may not have gone out.
            why = f"{why} - {CHECK_BEFORE_RETRY}"
        return _unavailable(why, status=status)

    # ---- transport -----------------------------------------------------------------
    def _http(self, method: str, path: str, body: Mapping[str, Any] | None,
              token: str) -> tuple[int, Any]:
        """(HTTP status, parsed JSON or None). Status 0 means nothing answered."""
        headers = {"Accept": "application/json", "User-Agent": "pionir-client/0.1",
                   "x-dash-token": token}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"{self._base}{path}", data=data, method=method,
                                         headers=headers)
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
    def _scrub(text: str, token: str) -> str:
        if not token:
            return text
        text = text.replace(token, "<redacted>")
        quoted = urllib.parse.quote_plus(token)
        return text.replace(quoted, "<redacted>") if quoted != token else text

    def _result(self, task: Task, output: dict[str, Any]) -> TaskResult:
        evidence = [f"client:{task.capability.split('.', 1)[1]}"]
        order_id = task.payload.get("order_id")
        if isinstance(order_id, str) and _ORDER_ID.fullmatch(order_id):
            evidence.append(f"client:order:{order_id}")
        if task.capability == EMAIL and output.get("ok") is True and output.get("id"):
            evidence.append(f"client:message:{output['id']}")
        return TaskResult(task_id=task.task_id, agent_id=self.manifest.agent_id,
                          output=output, evidence=tuple(evidence))

    # ---- the orders list (python -m pionir orders) ----------------------------------
    def list_orders(self, status: str | None = None) -> tuple[int, dict[str, Any]]:
        """The orders, for a person to read. Never shows the token.
        (0 ok, 1 not configured or not listed, 2 rejected)."""
        try:
            wanted = check_listing({"status": status} if status is not None else {})
        except ValueError as error:
            return 1, {"status": "error", "message": str(error)}
        token = read_token(self.settings.token_file)
        if token is None:
            return 1, {"status": "not_configured", "message": self._not_configured()}
        query = f"?{urllib.parse.urlencode({'status': wanted})}" if wanted else ""
        code, document = self._http("GET", f"/dash/orders{query}", None, token)
        output = self._answer(ORDERS, code, document, token)
        if output.get("ok") is not True:
            report = {"status": "rejected" if output.get("token_rejected") else "not_listed",
                      "message": output.get("error")}
            return (2 if output.get("token_rejected") else 1), \
                json.loads(self._scrub(json.dumps(report), token))
        orders = output.get("orders")
        rows = [_order_row(o) for o in orders if isinstance(o, Mapping)] \
            if isinstance(orders, list) else []
        report = {"status": "ok", "count": len(rows), "orders": rows}
        return 0, json.loads(self._scrub(json.dumps(report, default=str), token))


BRIEF_PREVIEW = 120


def _order_row(order: Mapping[str, Any]) -> dict[str, Any]:
    cents = order.get("amount_cents")
    amount = (f"{cents / 100:.2f}"
              if isinstance(cents, int) and not isinstance(cents, bool) else None)
    brief = str(order.get("brief") or "")
    if len(brief) > BRIEF_PREVIEW:
        brief = brief[:BRIEF_PREVIEW - 3] + "..."
    return {"id": order.get("id"), "created": order.get("created_at"),
            "package": order.get("package"), "status": order.get("status"),
            "amount": amount, "name": order.get("name"), "email": order.get("email"),
            "brief": brief}
