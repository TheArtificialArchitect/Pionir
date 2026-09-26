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

- ``client.deliver`` is PRIVILEGED with ``requires_approval=True``: it ships the client's
  work. The owner drops the zip at ``<deliveries_dir>/<order_id>/<name>.zip``; the payload
  pins its SHA-256, and before anything is parked the zip must pass every check in
  ``deliveries.py`` (a README, no executables, no path tricks, no zip bomb, and a scan of
  every entry for the owner's real secret values and common key formats). The card shows
  the file list, the scan and the whole email. On approval everything is checked again,
  the exact bytes checked are uploaded to a private link, the client is emailed that link
  and the order is marked delivered. The link is a bearer link: logs show only its last
  six characters.

- ``client.find_report`` is PRIVILEGED with ``requires_approval=True``: a "Find it for me"
  order's report of where to buy the client's item. It necessarily links to third-party
  shops, which ``client.email`` refuses, so every link is declared in ``links`` (0-15, each
  https: to a public DNS name - no IP, no localhost/.local/.internal, no password, no
  port), every link in the body must be declared and every declared link must be in the
  body, and the card lists each one by its domain above the whole report. It is sent
  through the same Scrooge call as ``client.email``, with the same answers.

- ``client.quote`` is PRIVILEGED with ``requires_approval=True``: the owner's price for a
  custom order, from his Discord reply (pionir/quotes.py). The card shows the price, the
  deposit split, the delivery time, the pay link's 14-day validity and the whole email; on
  approval Scrooge creates the quote and its pay link and emails the client, with
  ``{pay_link}`` replaced by the link. Checked here first: the split is this Pionir's rule
  (``deposit_threshold_cents``) and the email states exactly the amounts and days charged.
- ``client.quote_reminder`` is PRIVILEGED with ``requires_approval=True``: the ONE reminder a
  quote gets, from day 10 while it is unpaid (Scrooge refuses a second, or an early one).
- ``client.release`` is PRIVILEGED with ``requires_approval=True``: a delivery that was held
  because half the price was still owed, released once the balance is paid - Scrooge gives it
  a download link, the client is emailed it (``{link}``), and the order is marked delivered.
- ``client.deliver`` with ``hold_for_balance: true``: the order still owes its balance, so
  Scrooge stores the zip WITHOUT a link and mints a balance pay link, and ``{link}`` in the
  email becomes that pay link. If Scrooge and the payload disagree about holding, nothing
  is emailed.

All eight are ``routable=False``: reached only by name.

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

import ipaddress
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pionir.adapters.content import (
    _BAD_SCHEME,
    _BARE_WWW,
    _CONTROL_LINE,
    _EMAIL,
    _HTML_COMMENT,
    _HTML_DECL,
    _HTML_TAG,
    _INVISIBLE,
    _OTHER_SCHEME,
    _PHONE,
    _PHONES_STRICT,
    _SCHEMED_URL,
    DEFAULT_CONTENT_URL,
    _link_problem,
    read_token,
)
from pionir.adapters.deliveries import (
    DeliveryProblem,
    Inspection,
    inspect_zip,
    load_secrets,
)
from pionir.contracts import AgentManifest, Capability, RiskLevel, Task, TaskResult
from pionir.errors import AdapterProtocolError, AdapterUnavailable

_log = logging.getLogger(__name__)

ORDERS = "client.orders"
EMAIL = "client.email"
SET_STATUS = "client.set_status"
DELIVER = "client.deliver"
FIND_REPORT = "client.find_report"
QUOTE = "client.quote"
REMIND = "client.quote_reminder"
RELEASE = "client.release"

ORDER_STATUSES = ("awaiting_payment", "paid", "in_progress", "delivered", "declined",
                  "refunded", "quote_requested", "quoted")
# What client.set_status may move an order to: the owner's own steps. Payment
# (awaiting_payment -> paid) and a quote request are Scrooge's to record, never Pionir's.
SETTABLE_STATUSES = ("in_progress", "delivered", "declined", "refunded", "quoted")
EMAIL_FIELDS = frozenset({"order_id", "to", "subject", "body_text"})
DELIVER_FIELDS = frozenset({"order_id", "to", "zip_name", "zip_sha256", "subject",
                            "body_text"})
# client.deliver may also say the order owes its balance (the delivery is held).
DELIVER_OPTIONAL = frozenset({"hold_for_balance"})
QUOTE_FIELDS = frozenset({"order_id", "to", "quote_ref", "total_cents", "deposit_cents", "days",
                          "subject", "body_text"})
REMIND_FIELDS = frozenset({"order_id", "quote_id", "to", "subject", "body_text"})
RELEASE_FIELDS = frozenset({"order_id", "to", "delivery_id", "subject", "body_text"})
FIND_REPORT_FIELDS = frozenset({"order_id", "to", "subject", "body_text", "links"})
# Where the download link goes in a delivery email: exactly once.
LINK_PLACEHOLDER = "{link}"
# The shape of the link, for checking the email before the upload (the real one is
# Scrooge's https://api.dokaz.net/d/<64 hex>).
SAMPLE_LINK = "https://api.dokaz.net/d/" + "0" * 64
# A quote's pay link: {pay_link} in the quote email, Scrooge's https://api.dokaz.net/pay/<64 hex>.
PAY_LINK_PLACEHOLDER = "{pay_link}"
SAMPLE_PAY_LINK = "https://api.dokaz.net/pay/" + "0" * 64
NOT_EMAILED = "the link exists but the client was NOT emailed"
NOTHING_SHIPPED = "nothing was uploaded or emailed"

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


_ZIP_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}\.(?i:zip)")
_RELEASE_ID = re.compile(r"dl_[0-9a-f]{24}")
_PAY_URL = re.compile(r"https://[A-Za-z0-9.-]+/pay/[0-9a-f]{64}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_DELIVERY_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")
_DELIVERY_URL = re.compile(r"https://[A-Za-z0-9.-]+/d/[0-9a-f]{64}")


def check_zip_name(value: Any) -> str:
    """A bare file name in the order's folder: no path separators, no ``..``."""
    if not isinstance(value, str) or not value:
        raise ValueError("zip_name: required, the zip's file name")
    if "/" in value or "\\" in value or ".." in value or ":" in value:
        raise ValueError("zip_name: a bare file name in the order's folder (no path "
                         "separators, no '..')")
    if not _ZIP_NAME.fullmatch(value):
        raise ValueError("zip_name: letters, digits, '.', '_' and '-' only, ending in "
                         ".zip (at most 100 characters)")
    return value


def check_delivery(payload: Mapping[str, Any]) -> dict[str, str]:
    """The delivery exactly as it will be shipped, or ValueError("<field>: <why>"). The
    email is checked with a link in place of ``{link}``, by client.email's rules."""
    unknown = sorted(set(payload) - DELIVER_FIELDS - DELIVER_OPTIONAL)
    if unknown:
        raise ValueError(f"{unknown[0]}: not a delivery field (allowed: "
                         f"{', '.join(sorted(DELIVER_FIELDS | DELIVER_OPTIONAL))})")
    missing = sorted(DELIVER_FIELDS - set(payload))
    if missing:
        raise ValueError(f"{missing[0]}: required")
    zip_name = check_zip_name(payload["zip_name"])
    hold = payload.get("hold_for_balance", False)
    if not isinstance(hold, bool):
        raise ValueError("hold_for_balance: true or false")  # noqa: TRY004
    sha = payload["zip_sha256"]
    if not isinstance(sha, str) or not _SHA256.fullmatch(sha):
        raise ValueError("zip_sha256: the zip's SHA-256, 64 lowercase hex characters")
    body = payload["body_text"]
    if not isinstance(body, str):
        # ValueError like every other rule: one "<field>: <why>" refusal type
        raise ValueError("body_text: required, a string")  # noqa: TRY004
    found = body.count(LINK_PLACEHOLDER)
    if found != 1:
        raise ValueError(f"body_text: must contain {LINK_PLACEHOLDER} exactly once (it "
                         f"becomes the download link; found {found})")
    email = check_email({"order_id": payload["order_id"], "to": payload["to"],
                         "subject": payload["subject"],
                         "body_text": body.replace(LINK_PLACEHOLDER,
                                                   SAMPLE_PAY_LINK if hold else SAMPLE_LINK)})
    return {**email, "body_text": body, "zip_name": zip_name, "zip_sha256": sha,
            "hold_for_balance": hold}


# ---- quotes -------------------------------------------------------------------------------
_QUOTE_REF = re.compile(r"[A-Za-z0-9_-]{8,80}")
_QUOTE_ID = re.compile(r"qt_[0-9a-f]{24}")


def _whole(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key}: a whole number")  # noqa: TRY004
    return value


def check_quote(payload: Mapping[str, Any], threshold_cents: int) -> dict[str, Any]:
    """The quote exactly as Scrooge will charge and send it, or ValueError("<field>: <why>").
    Nothing here decides a price: it only checks that the numbers, the deposit rule and the
    words of the email agree - so the card the owner approves is what the client gets."""
    from pionir.quotes import MAX_CENTS, MAX_DAYS, MIN_CENTS, days_text, deposit_for, usd

    unknown = sorted(set(payload) - QUOTE_FIELDS)
    if unknown:
        raise ValueError(f"{unknown[0]}: not a quote field (allowed: "
                         f"{', '.join(sorted(QUOTE_FIELDS))})")
    missing = sorted(QUOTE_FIELDS - set(payload))
    if missing:
        raise ValueError(f"{missing[0]}: required")
    ref = payload["quote_ref"]
    if not isinstance(ref, str) or not _QUOTE_REF.fullmatch(ref):
        raise ValueError("quote_ref: 8-80 characters of A-Z a-z 0-9 _ -")
    total, deposit, days = (_whole(payload, "total_cents"), _whole(payload, "deposit_cents"),
                            _whole(payload, "days"))
    if not MIN_CENTS <= total <= MAX_CENTS:
        raise ValueError(f"total_cents: {MIN_CENTS}-{MAX_CENTS}")
    if not 1 <= days <= MAX_DAYS:
        raise ValueError(f"days: 1-{MAX_DAYS} business days")
    want = deposit_for(total, threshold_cents)
    if deposit != want:
        raise ValueError(f"deposit_cents: the rule makes it {want} on {total} (threshold "
                         f"{threshold_cents})")
    body = payload["body_text"]
    if not isinstance(body, str):
        raise ValueError("body_text: required, a string")  # noqa: TRY004
    found = body.count(PAY_LINK_PLACEHOLDER)
    if found != 1:
        raise ValueError(f"body_text: must contain {PAY_LINK_PLACEHOLDER} exactly once "
                         f"(found {found})")
    amounts = [usd(total)] + ([usd(deposit), usd(total - deposit)] if deposit else [])
    for amount in amounts:
        if not re.search(re.escape(amount) + r"(?![0-9]|[.,][0-9])", body):
            raise ValueError(f"body_text: must state {amount}, as it will be charged")
    if not re.search(r"(?<![0-9])" + re.escape(days_text(days)) + r"\b", body):
        raise ValueError(f"body_text: must state the delivery time, {days_text(days)}")
    email = check_email({"order_id": payload["order_id"], "to": payload["to"],
                         "subject": payload["subject"],
                         "body_text": body.replace(PAY_LINK_PLACEHOLDER, SAMPLE_PAY_LINK)})
    return {**email, "body_text": body, "quote_ref": ref, "total_cents": total,
            "deposit_cents": deposit, "days": days}


def check_reminder(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The day-10 reminder: a plain client email, plus the quote it is about."""
    unknown = sorted(set(payload) - REMIND_FIELDS)
    if unknown:
        raise ValueError(f"{unknown[0]}: not a reminder field (allowed: "
                         f"{', '.join(sorted(REMIND_FIELDS))})")
    quote_id = payload.get("quote_id")
    if not isinstance(quote_id, str) or not _QUOTE_ID.fullmatch(quote_id):
        raise ValueError("quote_id: qt_ and 24 lowercase hex characters")
    email = check_email({k: payload[k] for k in EMAIL_FIELDS if k in payload})
    if PAY_LINK_PLACEHOLDER in email["body_text"]:
        raise ValueError("body_text: a reminder carries no link")
    return {**email, "quote_id": quote_id}


def check_release(payload: Mapping[str, Any]) -> dict[str, Any]:
    """A held delivery's release: its id, and the email with {link} for the download link."""
    unknown = sorted(set(payload) - RELEASE_FIELDS)
    if unknown:
        raise ValueError(f"{unknown[0]}: not a release field (allowed: "
                         f"{', '.join(sorted(RELEASE_FIELDS))})")
    missing = sorted(RELEASE_FIELDS - set(payload))
    if missing:
        raise ValueError(f"{missing[0]}: required")
    delivery_id = payload["delivery_id"]
    if not isinstance(delivery_id, str) or not _RELEASE_ID.fullmatch(delivery_id):
        raise ValueError("delivery_id: dl_ and 24 lowercase hex characters")
    body = payload["body_text"]
    if not isinstance(body, str):
        raise ValueError("body_text: required, a string")  # noqa: TRY004
    found = body.count(LINK_PLACEHOLDER)
    if found != 1:
        raise ValueError(f"body_text: must contain {LINK_PLACEHOLDER} exactly once (it "
                         f"becomes the download link; found {found})")
    email = check_email({"order_id": payload["order_id"], "to": payload["to"],
                         "subject": payload["subject"],
                         "body_text": body.replace(LINK_PLACEHOLDER, SAMPLE_LINK)})
    return {**email, "body_text": body, "delivery_id": delivery_id}


# ---- the find report rules ----------------------------------------------------------------
# A "Find it for me" report links to the shops that sell the client's item: third-party
# websites, which client.email (Dokaz links only) keeps refusing. So every link is declared
# in ``links``, checked one by one, and listed by its domain on the owner's card - and the
# report may carry no link that is not declared, nor declare one it does not carry.
# Scrooge's POST /dash/orders/email refuses a body over 5,000 characters: a longer report
# would be approved and then bounce, so it is refused here, before the owner is asked.
FIND_BODY_LENGTH = BODY_LENGTH
MAX_FIND_LINKS = 15
MAX_LINK_LENGTH = 500
# The owner's internal systems: never in text a client reads. Mirrors INTERNAL_NAMES in
# crew/contentcheck.py (a test pins the two together).
INTERNAL_NAMES = ("Pionir", "Moss", "Galatea", "Atani", "Scrooge", "Skopos", "Hearth", "Bryo",
                  "Daedalus", "Melete", "Nyx", "Voodoo", "Theo")
_INTERNAL_NAME = re.compile(r"(?i)(?<![a-z0-9])(" + "|".join(INTERNAL_NAMES) + r")(?![a-z0-9])")
# One DNS label, and a top-level domain that is letters (or an IDNA xn-- name): an
# all-digit last label is how 2130706433 or 0x7f.1 pass for an address.
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_TLD = re.compile(r"[a-z]{2,63}|xn--[a-z0-9-]{1,59}")
# Names that only mean something on a private network, never a public shop.
_PRIVATE_SUFFIXES = frozenset({"localhost", "local", "internal", "localdomain", "lan", "home",
                               "corp", "intranet", "private", "arpa", "test", "invalid",
                               "onion"})
_URL_TEXT = re.compile(r"[\x21-\x7e]+")


def _find_link_problem(url: str) -> str | None:
    """Why ``url`` cannot be a link in a find report, or None. The URL is not echoed
    (it may carry a password); the host is, once it is known to be a plain DNS name."""
    if len(url) > MAX_LINK_LENGTH:
        return f"at most {MAX_LINK_LENGTH} characters (this is {len(url)})"
    if not _URL_TEXT.fullmatch(url):
        return "no spaces, control or non-ASCII characters (percent-encode them)"
    if _BAD_SCHEME.search(url):
        return "javascript:, data: and vbscript: are not allowed"
    if not url.startswith("https://"):
        return "must start with https://"
    if "\\" in url:
        # a browser reads a backslash as a slash: https://shop.example\@evil.example
        return "no backslashes"
    netloc = re.split(r"[/?#]", url[len("https://"):], maxsplit=1)[0]
    if "@" in netloc:
        return "cannot carry a user name or password"
    if netloc.startswith("["):
        return "an IP address, not a website's name"
    if ":" in netloc:
        return "cannot name a port"
    host = netloc.lower()
    if not host:
        return "has no host"
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return "an IP address, not a website's name"
    labels = host.split(".")
    if labels[-1] in _PRIVATE_SUFFIXES and all(_DNS_LABEL.fullmatch(x) for x in labels):
        return f"{host} is a local or internal name, not a public website"
    if len(host) > 253 or len(labels) < 2 \
            or not all(_DNS_LABEL.fullmatch(label) for label in labels):
        return "the host must be a website's name (like www.example-shop.com)"
    if not _TLD.fullmatch(labels[-1]):
        return "the host must end in a name like .com, not a number (an IP in disguise)"
    if not _SCHEMED_URL.fullmatch(url) or url != url.rstrip(_TRAILING):
        # the body is read the same way: the link must read the same inside the report
        return ("must read the same inside the report: no quotes, brackets, parentheses or "
                "backticks, and no punctuation at the end")
    return None


def check_find_links(value: Any) -> list[str]:
    """0-15 distinct https: links to public websites, or ValueError("links...: <why>")."""
    if not isinstance(value, list):
        raise ValueError("links: required, a list of https: links (it may be empty)")  # noqa: TRY004
    if len(value) > MAX_FIND_LINKS:
        raise ValueError(f"links: at most {MAX_FIND_LINKS} (this has {len(value)})")
    seen: set[str] = set()
    for i, link in enumerate(value):
        if not isinstance(link, str) or not link:
            raise ValueError(f"links[{i}]: must be a link, as text")
        problem = _find_link_problem(link)
        if problem:
            raise ValueError(f"links[{i}]: {problem}")
        if link in seen:
            raise ValueError(f"links[{i}]: listed twice")
        seen.add(link)
    return list(value)


def _report_text_problem(text: str, *, links_allowed: bool) -> str | None:
    """Plain text, nothing tag-shaped, no contact details, no internal names; links only
    written as https://... (and none at all in the subject)."""
    if _INVISIBLE.search(text):
        return "contains an invisible direction or zero-width character"
    if (_HTML_COMMENT.search(text) or _HTML_DECL.search(text) or _HTML_TAG.search(text)
            or _ANGLED.search(text)):
        return "plain text only - nothing tag-shaped like <...>"
    if _BAD_SCHEME.search(text):
        return "javascript:, data: and vbscript: are not allowed"
    if _OTHER_SCHEME.search(text):
        return "mailto:, tel: and file: links are not allowed"
    if not links_allowed and _SCHEMED_URL.search(text):
        return "no links here - they belong in the report, listed in links"
    for pattern in (_BARE_WWW, _BARE_HOST_PATH):
        for match in pattern.finditer(text):
            return (f"write a link as https://... and list it in links, not "
                    f"{match.group(0)[:80]!r} (mail clients link it as it stands)")
    # the rest reads the words, not the links (a product number in a URL is not a phone)
    words = _SCHEMED_URL.sub(" ", text)
    if _EMAIL.search(words):
        # not echoed: it is someone's contact details
        return ("no email addresses - of anyone; link to the shop instead of giving its "
                "contact details")
    if _PHONE.search(words) or any(
            sum(c.isdigit() for c in m.group(0)) >= 8
            for pattern in _PHONES_STRICT for m in pattern.finditer(words)):
        return "no phone numbers - of anyone; link to the shop instead"
    name = _INTERNAL_NAME.search(words)
    if name:
        return f"names the internal system {name.group(1)!r}"
    return None


def check_find_report(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The report exactly as it will be sent (with its links), or ValueError("<field>:
    <why>"). Every link in the body is in ``links`` and every one in ``links`` is in the
    body, so the owner's card lists every website the client is sent to."""
    unknown = sorted(set(payload) - FIND_REPORT_FIELDS)
    if unknown:
        raise ValueError(f"{unknown[0]}: not a find report field (allowed: "
                         f"{', '.join(sorted(FIND_REPORT_FIELDS))})")
    missing = sorted(FIND_REPORT_FIELDS - set(payload))
    if missing:
        raise ValueError(f"{missing[0]}: required")
    report: dict[str, Any] = {"order_id": check_order_id(payload["order_id"]),
                              "to": check_address(payload["to"])}
    links = check_find_links(payload["links"])
    for key, (low, high), control in (("subject", SUBJECT_LENGTH, _CONTROL_LINE),
                                      ("body_text", FIND_BODY_LENGTH, _CONTROL_TEXT)):
        text = payload[key]
        if not isinstance(text, str):
            raise ValueError(f"{key}: required, a string")  # noqa: TRY004
        if not low <= len(text) <= high:
            raise ValueError(f"{key}: {low}-{high} characters (this is {len(text)})")
        if not text.strip():
            raise ValueError(f"{key}: cannot be blank")
        if control.search(text.replace("\r\n", "\n") if key == "body_text" else text):
            raise ValueError(f"{key}: contains a control character"
                             + (" (only line breaks are allowed)" if key == "body_text"
                                else " or line break (one line only)"))
        problem = _report_text_problem(text, links_allowed=key == "body_text")
        if problem:
            raise ValueError(f"{key}: {problem}")
        report[key] = text
    body = report["body_text"]
    listed = set(links)
    written: set[str] = set()
    for match in _SCHEMED_URL.finditer(body):
        url = match.group(0).rstrip(_TRAILING)
        problem = _find_link_problem(url)
        if problem:
            raise ValueError(f"body_text: a link {problem}")
        if url not in listed:
            host = urllib.parse.urlsplit(url).hostname
            raise ValueError(f"body_text: a link to {host} is not listed in links - every "
                             "link in the report must be, so the owner sees it on the card")
        written.add(url)
    for i, link in enumerate(links):
        if link not in written:
            raise ValueError(f"links[{i}]: not in the report - every listed link must "
                             "appear in body_text")
    report["links"] = links
    return report


def link_tail(url: str) -> str:
    """How a download link is shown in a log: its last six characters only (the link
    itself is a bearer link - whoever has it can download the client's work)."""
    return f"...{url[-6:]}" if url else "(none)"


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
    # client.deliver: where the owner drops each order's zip (<dir>/<order_id>/<name>.zip),
    # and where the owner's secrets are - every value in them is looked for in the zip.
    # The ops token file is always among them.
    deliveries_dir: Path = Path("~/.pionir/deliveries")
    secrets_dir: Path | None = Path("~/.pionir/secrets")
    secret_files: tuple[Path, ...] = ()
    # secrets held as settings rather than files: (label, value), never shown
    secret_values: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    ssh_dir: Path | None = Path("~/.ssh")
    upload_timeout_seconds: int = 180
    # client.quote: at or above this many cents, half up front and half on delivery. Must
    # equal Scrooge's QUOTE_DEPOSIT_THRESHOLD_CENTS (Scrooge refuses, unsent, a mismatch).
    deposit_threshold_cents: int = 50_000
    # client.quote is accepted only as the owner's own Discord reply: the card record the gate
    # keeps (quote-cards.json), his Discord user id, and the days used when a reply gives none.
    # No record or no owner: every quote is refused.
    quote_store: Path | None = None
    owner_user_id: str | None = None
    default_days: int = 7

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
        object.__setattr__(self, "deliveries_dir", Path(self.deliveries_dir).expanduser())
        for name in ("secrets_dir", "ssh_dir"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, Path(value).expanduser())
        object.__setattr__(self, "secret_files",
                           tuple(Path(p).expanduser() for p in self.secret_files))
        if self.upload_timeout_seconds < self.timeout_seconds:
            raise ValueError("the upload timeout cannot be shorter than the orders timeout")


def client_settings(configured: Any) -> ClientSettings:
    """The ClientSettings for a PionirSettings: its Scrooge, ops token, deliveries folder,
    and every token file Pionir is configured with (each is scanned for in a delivery)."""
    token_files = [configured.content_token_path, configured.instagram_token_path,
                   configured.devto_key_path]
    gumroad = getattr(configured, "gumroad_token_path", None)
    if gumroad is not None:
        token_files.append(gumroad)
    discord = (os.environ.get("PIONIR_DISCORD_TOKEN_FILE") or "").strip()
    if discord:
        token_files.append(Path(discord))
    values = tuple((label, token) for label, token in (
        ("the Daedalus token (PIONIR_DAEDALUS_TOKEN)", getattr(configured, "daedalus_token", None)),
        ("the Melete token (PIONIR_MELETE_TOKEN)", getattr(configured, "melete_token", None)),
    ) if token)
    from pionir.discord_gate import DiscordGateSettings
    from pionir.quotes import QuoteCardStore, QuoteSettings

    quotes = QuoteSettings.from_environment()
    return ClientSettings(base_url=configured.content_url or DEFAULT_CONTENT_URL,
                          token_file=configured.ops_token_path,
                          deliveries_dir=configured.deliveries_path,
                          secrets_dir=configured.secrets_path,
                          secret_files=tuple(token_files),
                          secret_values=values,
                          deposit_threshold_cents=quotes.deposit_threshold_cents,
                          quote_store=QuoteCardStore.for_state_root(configured.state_root).path,
                          owner_user_id=DiscordGateSettings.from_environment(
                              configured.state_root).owner,
                          default_days=quotes.default_days)


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
                    name=FIND_REPORT,
                    description="Email the client of a \"Find it for me\" order the report "
                                "of where to buy their item, with links to the shops "
                                "(only after the owner approves it and every link)",
                    risk=RiskLevel.PRIVILEGED,
                    required_permissions=frozenset({FIND_REPORT}),
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
                Capability(
                    name=DELIVER,
                    description="Deliver an order's finished work: upload the checked zip "
                                "to a private link and email the client that link "
                                "(only after the owner approves it)",
                    risk=RiskLevel.PRIVILEGED,
                    required_permissions=frozenset({DELIVER}),
                    requires_approval=True,
                    routable=False,
                ),
                Capability(
                    name=QUOTE,
                    description="Quote a custom order at the owner's price: create its pay "
                                "link and email the client the quote (only after the owner "
                                "approves the exact email)",
                    risk=RiskLevel.PRIVILEGED,
                    required_permissions=frozenset({QUOTE}),
                    requires_approval=True,
                    routable=False,
                ),
                Capability(
                    name=REMIND,
                    description="Send the one day-10 reminder of an unpaid quote (only "
                                "after the owner approves it)",
                    risk=RiskLevel.PRIVILEGED,
                    required_permissions=frozenset({REMIND}),
                    requires_approval=True,
                    routable=False,
                ),
                Capability(
                    name=RELEASE,
                    description="Release a delivery held for the balance, once the balance "
                                "is paid: email the client its download link (only after "
                                "the owner approves it)",
                    risk=RiskLevel.PRIVILEGED,
                    required_permissions=frozenset({RELEASE}),
                    requires_approval=True,
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
        if task.capability == DELIVER:
            # every zip check, before the owner is asked: a zip that cannot go is refused
            # here with the reason, not parked for a yes that would be wasted
            try:
                self._inspect(self._delivery(task))
            except DeliveryProblem as error:
                raise AdapterProtocolError(f"{DELIVER} refused by Pionir - {error}") from error
        else:
            self._request(task)
        if task.capability in (EMAIL, DELIVER, FIND_REPORT, QUOTE, REMIND, RELEASE) \
                and read_token(self.settings.token_file) is None:
            # Asking the owner to approve an email that cannot be sent wastes his yes.
            raise AdapterUnavailable(self._not_configured())

    def _request(self, task: Task) -> tuple[str, str, dict[str, Any] | None]:
        """(method, path, JSON body) for this task, or AdapterProtocolError."""
        try:
            if task.capability == QUOTE:
                quote = check_quote(task.payload, self.settings.deposit_threshold_cents)
                # ...and only as the owner's own reply: before it is parked AND when it runs
                self._provenance(task.payload)
                return "POST", "/dash/orders/quote", {k: quote[k] for k in sorted(QUOTE_FIELDS)}
            if task.capability == REMIND:
                return "POST", "/dash/orders/quote/remind", check_reminder(task.payload)
            if task.capability == RELEASE:
                check_release(task.payload)
                return "POST", "/dash/orders/delivery/release", None
            if task.capability == ORDERS:
                status = check_listing(task.payload)
                query = f"?{urllib.parse.urlencode({'status': status})}" if status else ""
                return "GET", f"/dash/orders{query}", None
            if task.capability == EMAIL:
                return "POST", "/dash/orders/email", check_email(task.payload)
            if task.capability == FIND_REPORT:
                # Scrooge takes the report as a plain-text email; the links were for the
                # checks and the owner's card, and are all in the body
                report = check_find_report(task.payload)
                return "POST", "/dash/orders/email", {k: report[k] for k in sorted(EMAIL_FIELDS)}
            if task.capability == SET_STATUS:
                return "POST", "/dash/orders/status", check_status_change(task.payload)
        except ValueError as error:
            raise AdapterProtocolError(f"{task.capability} refused by Pionir - {error}") from error
        raise AdapterProtocolError(f"client has no capability {task.capability!r}")

    def _provenance(self, payload: Mapping[str, Any]) -> None:
        from pionir.quotes import QuoteCardStore, check_provenance

        store = QuoteCardStore(self.settings.quote_store) if self.settings.quote_store else None
        check_provenance(payload, store, self.settings.owner_user_id, self.settings.default_days)

    @staticmethod
    def _delivery(task: Task) -> dict[str, str]:
        try:
            return check_delivery(task.payload)
        except ValueError as error:
            raise AdapterProtocolError(f"{DELIVER} refused by Pionir - {error}") from error

    def _inspect(self, delivery: Mapping[str, str]) -> Inspection:
        """Every zip check (deliveries.py), with the owner's secrets read fresh."""
        secrets = load_secrets(self.settings.secrets_dir,
                               (*self.settings.secret_files, self.settings.token_file),
                               self.settings.ssh_dir, self.settings.secret_values)
        root = self.settings.deliveries_dir
        return inspect_zip(root / delivery["order_id"] / delivery["zip_name"], secrets,
                           pinned_sha256=delivery["zip_sha256"], root=root)

    def delivery_preview(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """What the Discord card shows for a parked client.deliver: the zip as it is on
        disk now, checked the same way execute() will check it. Never a secret value."""
        try:
            delivery = check_delivery(payload)
            inspection = self._inspect(delivery)
        except (ValueError, DeliveryProblem) as error:
            return {"ok": False, "problem": str(error)}
        return {"ok": True, "size": inspection.size, "sha256": inspection.sha256,
                "files": [list(f) for f in inspection.files],
                "secret_values": inspection.secret_values}

    # ---- the call ------------------------------------------------------------------
    def execute(self, task: Task) -> TaskResult:
        if task.capability == DELIVER:
            return self._execute_delivery(task)
        if task.capability == RELEASE:
            return self._execute_release(task)
        method, path, body = self._request(task)
        token = read_token(self.settings.token_file)
        if token is None:
            return self._result(task, _unavailable(self._not_configured(),
                                                   not_configured=True))
        status, document = self._http(method, path, body, token)
        output = self._answer(task.capability, status, document, token)
        if task.capability in (EMAIL, FIND_REPORT, QUOTE, REMIND):
            _log.info("client: %s for order %s: %s",
                      {EMAIL: "email", FIND_REPORT: "find report", QUOTE: "quote",
                       REMIND: "quote reminder"}[task.capability],
                      body["order_id"] if body else "?",
                      "sent" if output.get("ok") is True else "not sent")
        # Belt and braces: whatever Scrooge echoed, the token does not leave in the result.
        return self._result(task, json.loads(self._scrub(json.dumps(output), token)))

    def _answer(self, capability: str, status: int, document: Any,
                token: str) -> dict[str, Any]:
        doc = document if isinstance(document, Mapping) else {}
        said = self._scrub(str(doc.get("error") or ""), token)[:300]
        emailing = capability in (EMAIL, FIND_REPORT, QUOTE, REMIND)
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
        # Only an unanswered call or an unexpected server error leaves the send unknown. Scrooge
        # answers 503 (mail not configured) BEFORE sending and 502 when the provider refused -
        # it removes the message row then - so both mean nothing went out, and saying "may have
        # been sent" there would send the owner looking for a message that doesn't exist.
        # Only Scrooge's OWN 502/503 (its JSON refusal: 503 = mail not configured, checked
        # before sending; 502 = the provider refused, and Scrooge removes the message row) means
        # nothing went out. A bare 502 from the edge (Cloudflare's HTML page), a 500, a 504 or
        # no answer at all leaves the send unknown.
        scrooge_said_no = status in (502, 503) and doc.get("ok") is False
        if emailing and not scrooge_said_no and (status == 0 or status >= 500):
            why = f"{why} - {CHECK_BEFORE_RETRY}"
        elif emailing and scrooge_said_no and "nothing was sent" not in why:
            why = f"{why} - nothing was sent"
        return _unavailable(why, status=status)

    # ---- client.deliver: check again, upload, email, mark delivered -------------------
    def _execute_delivery(self, task: Task) -> TaskResult:
        delivery = self._delivery(task)      # a malformed payload raises, as for an email
        try:
            # Everything again, from the bytes on disk NOW: they may have changed since
            # the owner approved. The bytes checked here are the bytes uploaded.
            inspection = self._inspect(delivery)
        except DeliveryProblem as error:
            _log.info("client: delivery for order %s refused before upload",
                      delivery["order_id"])
            return self._result(task, _refused(f"{error} - {NOTHING_SHIPPED}"))
        token = read_token(self.settings.token_file)
        if token is None:
            return self._result(task, _unavailable(self._not_configured(),
                                                   not_configured=True))
        output, evidence = self._deliver(delivery, inspection, token)
        return self._result(task, json.loads(self._scrub(json.dumps(output), token)),
                            extra_evidence=evidence)

    def _deliver(self, delivery: Mapping[str, str], inspection: Inspection,
                 token: str) -> tuple[dict[str, Any], list[str]]:
        order_id = delivery["order_id"]
        query = urllib.parse.urlencode({"order_id": order_id,
                                        "filename": delivery["zip_name"]})
        status, document = self._http("POST", f"/dash/orders/delivery?{query}", None, token,
                                      raw=inspection.data, content_type="application/zip",
                                      timeout=self.settings.upload_timeout_seconds)
        uploaded = self._uploaded(status, document, token, inspection)
        if uploaded.get("ok") is not True:
            _log.info("client: delivery for order %s: not uploaded", order_id)
            return uploaded, []
        held = uploaded.get("held") is True
        if held != bool(delivery.get("hold_for_balance")):
            # The email says "pay the balance" and Scrooge gave a download link, or the
            # reverse: sending either would give the client the wrong thing. Nothing goes.
            if held:
                why = ("Scrooge HELD the delivery: the order still owes its balance, and this "
                       "email was written as a download - nothing was emailed; the delivery "
                       "desk sends the balance request")
                return _refused(why, delivery_id=uploaded["delivery_id"], held=True), []
            revoked = self._revoke(uploaded["delivery_id"], token)
            why = ("Scrooge did NOT hold the delivery (no balance is owed), and this email asks "
                   "for a balance payment - " + ("the link was revoked" if revoked else
                   f"revoke delivery {uploaded['delivery_id']} by hand")
                   + "; nothing was emailed")
            return _refused(why, delivery_id=uploaded["delivery_id"], revoked=revoked), []
        url = uploaded["url"]
        evidence = [f"client:delivery:{uploaded['delivery_id']}"]
        _log.info("client: delivery %s for order %s uploaded (link %s)",
                  uploaded["delivery_id"], order_id, link_tail(url))
        details = {k: uploaded[k] for k in ("delivery_id", "url", "expires_at", "sha256",
                                            "size")}

        def not_emailed(answer: Mapping[str, Any]) -> dict[str, Any]:
            why = str(answer.get("error") or "the email was not sent").replace(url, "<link>")
            unknown = CHECK_BEFORE_RETRY in why
            message = (f"{NOT_EMAILED}{' as far as Pionir knows' if unknown else ''} "
                       f"- {why}")
            kind = "refused" if "refused" in answer else "unavailable"
            _log.info("client: delivery %s for order %s: uploaded, NOT emailed",
                      uploaded["delivery_id"], order_id)
            return {"ok": False, kind: message, "error": message, "emailed": False,
                    "uploaded": details}

        try:
            email = check_email({"order_id": order_id, "to": delivery["to"],
                                 "subject": delivery["subject"],
                                 "body_text": delivery["body_text"].replace(
                                     LINK_PLACEHOLDER, url)})
        except ValueError as error:
            return not_emailed(_refused(f"the email with the link failed Pionir's check "
                                        f"({error})")), evidence
        status, document = self._http("POST", "/dash/orders/email", email, token)
        answer = self._answer(EMAIL, status, document, token)
        if answer.get("ok") is not True:
            return not_emailed(answer), evidence
        if answer.get("id"):
            evidence.append(f"client:message:{answer['id']}")
        # Emailed: the client has the link. The approval and job records keep its tail only;
        # the whole link is kept only when it was NOT emailed (the owner must hand it over).
        result: dict[str, Any] = {"ok": True, "delivery_id": uploaded["delivery_id"],
                                  "link_tail": link_tail(url),
                                  "expires_at": uploaded["expires_at"], "emailed": True}
        if held:
            # The client was sent the balance pay link; Scrooge has already moved the order
            # to balance_due. The files wait for the balance (client.release).
            result.update(held=True, balance_cents=uploaded.get("balance_cents"))
            _log.info("client: delivery %s for order %s: HELD, balance request emailed",
                      uploaded["delivery_id"], order_id)
            return result, evidence
        status, document = self._http("POST", "/dash/orders/status",
                                      {"order_id": order_id, "status": "delivered"}, token)
        moved = self._answer(SET_STATUS, status, document, token)
        if moved.get("ok") is True:
            result["status"] = "delivered"
        else:
            # the client has the link: the delivery happened, only the record lags
            why = str(moved.get("error")).replace(url, "<link>")
            result["status_error"] = (f"the client was emailed the link, but the order "
                                      f"was not marked delivered - {why}")
        _log.info("client: delivery %s for order %s: emailed, %s", uploaded["delivery_id"],
                  order_id, "marked delivered" if "status" in result else "status NOT set")
        return result, evidence

    def _uploaded(self, status: int, document: Any, token: str,
                  inspection: Inspection) -> dict[str, Any]:
        """Scrooge's answer to the upload: {ok: true, delivery_id, url, expires_at, sha256,
        size} only when it stored exactly the bytes that were checked; otherwise why not
        (a stored upload that is wrong is revoked). Nothing has been emailed yet."""
        doc = document if isinstance(document, Mapping) else {}
        said = self._scrub(str(doc.get("error") or ""), token)[:300]
        if 200 <= status < 300:
            if doc.get("ok") is not True:
                return _unavailable("Scrooge answered the upload without ok: true - the zip "
                                    "may have been stored, but nothing was emailed to the "
                                    "client", status=status)
            delivery_id = doc.get("delivery_id")
            if not isinstance(delivery_id, str) or not _DELIVERY_ID.fullmatch(delivery_id):
                return _unavailable("Scrooge's upload answer had no usable delivery_id - "
                                    "nothing was emailed to the client; check the order's "
                                    "deliveries", status=status)
            stored, size = doc.get("sha256"), doc.get("size")
            if stored != inspection.sha256 or (size is not None and size != inspection.size):
                revoked = self._revoke(delivery_id, token)
                shown = str(stored)[:16] if isinstance(stored, str) else "(none)"
                return _refused(
                    f"Scrooge stored different bytes (sha256 {shown}...) than the zip you "
                    f"approved ({inspection.sha256[:16]}...) - "
                    + ("the link was revoked" if revoked else
                       f"the link could NOT be revoked: revoke delivery {delivery_id} by hand")
                    + "; nothing was emailed to the client",
                    delivery_id=delivery_id, revoked=revoked)
            if doc.get("held") is True:
                # Stored without a link: the order owes its balance. The link to send is the
                # balance pay link Scrooge minted with it.
                balance = doc.get("balance") if isinstance(doc.get("balance"), Mapping) else {}
                pay = balance.get("url")
                if not isinstance(pay, str) or not _PAY_URL.fullmatch(pay) \
                        or _link_problem(pay) is not None:
                    return _unavailable("Scrooge held the delivery but answered with no usable "
                                        "balance link - nothing was emailed to the client",
                                        status=status)
                amount = balance.get("amount_cents")
                return {"ok": True, "held": True, "delivery_id": delivery_id, "url": pay,
                        "expires_at": balance.get("expires_at"), "sha256": stored,
                        "size": inspection.size,
                        "balance_cents": amount if isinstance(amount, int) else None}
            url = doc.get("url")
            if not isinstance(url, str) or not _DELIVERY_URL.fullmatch(url) \
                    or _link_problem(url) is not None:
                revoked = self._revoke(delivery_id, token)
                return _unavailable(
                    "Scrooge answered with something that is not a Dokaz download link - "
                    + ("it was revoked" if revoked else
                       f"it could NOT be revoked: revoke delivery {delivery_id} by hand")
                    + "; nothing was emailed to the client",
                    delivery_id=delivery_id, revoked=revoked)
            expires = doc.get("expires_at")
            return {"ok": True, "delivery_id": delivery_id, "url": url,
                    "expires_at": expires if isinstance(expires, str) else None,
                    "sha256": stored, "size": inspection.size}
        if status in (400, 409):
            return _refused(f"{said or f'Scrooge refused the upload (HTTP {status})'} - "
                            f"{NOTHING_SHIPPED}", status=status)
        if status == 413:
            return _refused(f"the zip is too big for Scrooge (HTTP 413) - {NOTHING_SHIPPED}",
                            status=413)
        if status in (401, 403):
            return _unavailable(TOKEN_REJECTED, status=status, token_rejected=True)
        if status == 429:
            return _unavailable(f"Scrooge is rate limiting uploads - try again later; "
                                f"{NOTHING_SHIPPED}", status=429)
        if status == 0:
            why = f"Scrooge is unreachable at {urllib.parse.urlparse(self._base).netloc}"
        else:
            why = f"Scrooge answered the upload HTTP {status}" + (f": {said}" if said else "")
        return _unavailable(f"{why} - the zip may or may not have been stored, but nothing "
                            "was emailed to the client", status=status)

    def _revoke(self, delivery_id: str, token: str) -> bool:
        status, document = self._http("POST", "/dash/orders/delivery/revoke",
                                      {"delivery_id": delivery_id}, token)
        revoked = (200 <= status < 300 and isinstance(document, Mapping)
                   and document.get("ok") is True)
        _log.warning("client: delivery %s revoked: %s", delivery_id, revoked)
        return revoked

    # ---- client.release: a held delivery gets its link, once the balance is paid -------
    def _execute_release(self, task: Task) -> TaskResult:
        try:
            release = check_release(task.payload)
        except ValueError as error:
            raise AdapterProtocolError(f"{RELEASE} refused by Pionir - {error}") from error
        token = read_token(self.settings.token_file)
        if token is None:
            return self._result(task, _unavailable(self._not_configured(), not_configured=True))
        output, evidence = self._release(release, token)
        return self._result(task, json.loads(self._scrub(json.dumps(output), token)),
                            extra_evidence=evidence)

    def _release(self, release: Mapping[str, str],
                 token: str) -> tuple[dict[str, Any], list[str]]:
        order_id = release["order_id"]
        status, document = self._http("POST", "/dash/orders/delivery/release",
                                      {"order_id": order_id,
                                       "delivery_id": release["delivery_id"]}, token)
        doc = document if isinstance(document, Mapping) else {}
        if not (200 <= status < 300 and doc.get("ok") is True):
            answer = self._answer(SET_STATUS, status, document, token)
            why = f"{answer.get('error')} - nothing was released or emailed"
            for key in ("refused", "unavailable", "error"):
                if key in answer:
                    answer[key] = why
            return answer, []
        url = doc.get("url")
        if not isinstance(url, str) or not _DELIVERY_URL.fullmatch(url) \
                or _link_problem(url) is not None:
            return _unavailable("Scrooge released the delivery without a usable download link "
                                "- nothing was emailed to the client", status=status), []
        evidence = [f"client:delivery:{release['delivery_id']}"]
        try:
            email = check_email({"order_id": order_id, "to": release["to"],
                                 "subject": release["subject"],
                                 "body_text": release["body_text"].replace(LINK_PLACEHOLDER,
                                                                           url)})
        except ValueError as error:
            return _refused(f"the email with the link failed Pionir's check ({error}); "
                            f"{NOT_EMAILED}"), evidence
        status, document = self._http("POST", "/dash/orders/email", email, token)
        answer = self._answer(EMAIL, status, document, token)
        if answer.get("ok") is not True:
            why = f"{NOT_EMAILED} - " + str(answer.get("error")).replace(url, "<link>")
            kind = "refused" if "refused" in answer else "unavailable"
            return {"ok": False, kind: why, "error": why, "emailed": False,
                    "released": True}, evidence
        if answer.get("id"):
            evidence.append(f"client:message:{answer['id']}")
        # The download link went to the client; the approval and job records keep only the id.
        result: dict[str, Any] = {"ok": True, "delivery_id": release["delivery_id"],
                                  "link_tail": link_tail(url),
                                  "expires_at": doc.get("expires_at"), "emailed": True}
        status, document = self._http("POST", "/dash/orders/status",
                                      {"order_id": order_id, "status": "delivered"}, token)
        moved = self._answer(SET_STATUS, status, document, token)
        if moved.get("ok") is True:
            result["status"] = "delivered"
        else:
            result["status_error"] = ("the client was emailed the link, but the order was not "
                                      "marked delivered - "
                                      + str(moved.get("error")).replace(url, "<link>"))
        _log.info("client: held delivery %s for order %s released (link %s)",
                  release["delivery_id"], order_id, link_tail(url))
        return result, evidence

    # ---- one order, for the Discord gate's quote replies --------------------------------
    def order(self, order_id: str) -> dict[str, Any] | None:
        """The order as Scrooge has it now (name, address, status, its quote), or None when
        Scrooge has no such order. AdapterUnavailable when it cannot be read."""
        oid = check_order_id(order_id)
        token = read_token(self.settings.token_file)
        if token is None:
            raise AdapterUnavailable(self._not_configured())
        query = urllib.parse.urlencode({"id": oid})
        status, document = self._http("GET", f"/dash/orders?{query}", None, token)
        output = self._answer(ORDERS, status, document, token)
        if output.get("ok") is not True:
            raise AdapterUnavailable(self._scrub(str(output.get("error")), token))
        orders = output.get("orders")
        for o in orders if isinstance(orders, list) else []:
            if isinstance(o, Mapping) and o.get("id") == oid:
                return dict(o)
        return None

    # ---- transport -----------------------------------------------------------------
    def _http(self, method: str, path: str, body: Mapping[str, Any] | None,
              token: str, *, raw: bytes | None = None, content_type: str | None = None,
              timeout: int | None = None) -> tuple[int, Any]:
        """(HTTP status, parsed JSON or None). Status 0 means nothing answered. ``raw``
        sends those bytes as the body (with ``content_type``) instead of JSON."""
        headers = {"Accept": "application/json", "User-Agent": "pionir-client/0.1",
                   "x-dash-token": token}
        data = None
        if raw is not None:
            data = raw
            headers["Content-Type"] = content_type or "application/octet-stream"
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"{self._base}{path}", data=data, method=method,
                                         headers=headers)
        try:
            # timeout by keyword: OpenerDirector.open(url, data=None, timeout=...) takes a
            # positional second argument as the POST body.
            with self._open(request,
                            timeout=timeout or self.settings.timeout_seconds) as response:
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

    def _result(self, task: Task, output: dict[str, Any], *,
                extra_evidence: list[str] | None = None) -> TaskResult:
        evidence = [f"client:{task.capability.split('.', 1)[1]}"]
        order_id = task.payload.get("order_id")
        if isinstance(order_id, str) and _ORDER_ID.fullmatch(order_id):
            evidence.append(f"client:order:{order_id}")
        if task.capability in (EMAIL, FIND_REPORT) and output.get("ok") is True                 and output.get("id"):
            evidence.append(f"client:message:{output['id']}")
        evidence += extra_evidence or []
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

    # ---- the deliveries folder (python -m pionir deliveries) --------------------------
    def list_deliveries(self) -> tuple[int, dict[str, Any]]:
        """Each order folder under deliveries_dir with its zips, their sizes and sha256,
        and whether each passes client.deliver's checks (and why not). Local only: no
        call to Scrooge, nothing sent. Never shows a secret value - only which secret
        file matched. (0 listed, 1 no folder or the secrets could not be read.)"""
        root = self.settings.deliveries_dir
        if not root.is_dir():
            return 1, {"status": "empty", "dir": str(root),
                       "message": f"no deliveries folder at {root} - put each order's zip "
                                  "in <that folder>/<order_id>/<name>.zip"}
        try:
            secrets = load_secrets(self.settings.secrets_dir,
                                   (*self.settings.secret_files, self.settings.token_file),
                                   self.settings.ssh_dir, self.settings.secret_values)
        except DeliveryProblem as error:
            return 1, {"status": "error", "dir": str(root), "message": str(error)}
        orders: list[dict[str, Any]] = []
        for folder in sorted(p for p in root.iterdir() if p.is_dir()):
            row: dict[str, Any] = {"order_id": folder.name, "zips": []}
            if not _ORDER_ID.fullmatch(folder.name):
                row["problem"] = ("not an order id (12 lowercase hex characters) - "
                                  "client.deliver cannot ship from this folder")
            for path in sorted(p for p in folder.iterdir()
                               if p.name.lower().endswith(".zip")):
                zip_row: dict[str, Any] = {"name": path.name}
                try:
                    zip_row["size"] = path.lstat().st_size
                    check_zip_name(path.name)
                    inspection = inspect_zip(path, secrets, root=root)
                except (ValueError, OSError) as error:   # DeliveryProblem is a ValueError
                    zip_row.update(passes=False, problem=str(error))
                else:
                    zip_row.update(passes="problem" not in row, sha256=inspection.sha256,
                                   files=len(inspection.files),
                                   secret_values_checked=inspection.secret_values)
                row["zips"].append(zip_row)
            orders.append(row)
        return 0, {"status": "ok", "dir": str(root), "secret_values_checked": len(secrets),
                   "orders": orders}


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
