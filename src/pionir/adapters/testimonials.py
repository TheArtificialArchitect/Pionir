"""Adapter for client testimonials: what a delivered client wrote, shown on /hire only with
their consent AND the owner's yes.

Scrooge (worker/src/feedback.ts) stores each answer from a client's private feedback page. A
consented one waits there, unseen, for the owner. Ian's rule for anything public - the same as for
a blog post - is that nothing goes live without his approval, and holding a permission is not a
way around it. So:

- ``client.testimonials`` is READ_ONLY: the consented testimonials waiting for the owner, and the
  counts (feedback requested and received, the average rating, testimonials live, referral
  orders and the referral credits recorded). Read with the OPS token. Nobody is contacted.
- ``client.testimonial_publish`` is PRIVILEGED with ``requires_approval=True``: PionirApp parks it
  on EVERY call, and the Discord card shows the exact display name, rating and words that will
  appear on https://api.dokaz.net/hire. Sent with the PUBLISH token (putting something on a public
  page is that token's power on Scrooge, never the ops token's), and Scrooge refuses any text but
  the stored one, so what is approved is what is shown.

Both are ``routable=False``: reached only by name.

A testimonial is untrusted text a client typed. Before it is parked (and again when it runs) it
must pass ``check_testimonial``: the text checks of the blog's content rules (adapters/content.py:
no HTML, no contact details, nothing invisible) and stricter ones on top - no link or address of
any kind, a display name that is a name (never an email address), and the lengths Scrooge stores.
A testimonial that fails is never proposed: the crew worker records why, and the owner can hide it.

Transport, answers and token handling are ClientAdapter's (clients.py): the tokens are read from
files on every call, sent only in ``x-dash-token``, and never appear in a log, an error or a result.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from pionir.adapters.clients import ClientAdapter, ClientSettings, _unavailable, check_order_id
from pionir.adapters.content import (
    _BARE_WWW,
    _CONTROL_LINE,
    _EMAIL,
    _INVISIBLE,
    _SCHEMED_URL,
    _contact_problem,
    _scrooge_problem,
    _text_problem,
    read_token,
)
from pionir.contracts import AgentManifest, Capability, RiskLevel, Task, TaskResult
from pionir.errors import AdapterProtocolError, AdapterUnavailable

TESTIMONIALS = "client.testimonials"
PUBLISH_TESTIMONIAL = "client.testimonial_publish"
HIRE_PAGE = "https://api.dokaz.net/hire"
# Scrooge's routes and limits (worker/src/feedback.ts): a contract test pins them.
PENDING_ROUTE = "/dash/testimonials/pending"
APPROVE_ROUTE = "/dash/testimonials/approve"
PUBLISH_FIELDS = frozenset({"testimonial_id", "order_id", "rating", "display_name", "body"})
BODY_LENGTH = (10, 600)
NAME_LENGTH = (1, 40)
_TESTIMONIAL_ID = re.compile(r"tm_[0-9a-f]{24}")
# An address written without a scheme (shop.example.com, mysite.io/offer): a host ending in a
# common top-level domain. Not every dotted word - "job.Thanks" is a typo, not a link.
_DOMAIN = re.compile(r"(?i)(?<![\w@-])(?:[a-z0-9-]+\.)+(?:com|net|org|io|co|uk|us|ca|au|de|fr|es|it|"
                     r"nl|eu|ru|cn|in|info|biz|xyz|app|dev|ai|me|ly|gg|tv|site|online|store|shop|"
                     r"link|click|top|live|page)(?![\w-])")
_ANGLED = re.compile(r"[<>]")
_CONTROL_BODY = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")
PUBLISH_TOKEN_HINT = "no publish token (scrooge-publish-token.txt) - the testimonial cannot go live"


def _text_rules(key: str, text: str, low: int, high: int, *, multiline: bool) -> str:
    if not isinstance(text, str):
        raise ValueError(f"{key}: required, a string")  # noqa: TRY004
    if not low <= len(text) <= high:
        raise ValueError(f"{key}: {low}-{high} characters (this is {len(text)})")
    if not text.strip():
        raise ValueError(f"{key}: cannot be blank")
    if (_CONTROL_BODY if multiline else _CONTROL_LINE).search(text):
        raise ValueError(f"{key}: contains a control character")
    if _INVISIBLE.search(text):
        raise ValueError(f"{key}: contains an invisible direction or zero-width character")
    if _ANGLED.search(text):
        raise ValueError(f"{key}: plain text only - no < or >")
    # the blog's own text rules (adapters/content.py): no HTML, no contact details, safe links...
    problem = _text_problem(text) or _scrooge_problem("body_md" if multiline else key, text)
    if problem:
        raise ValueError(f"{key}: {problem}")
    # ...and stricter: a testimonial carries no link or address of any kind, not even ours
    if _SCHEMED_URL.search(text) or _BARE_WWW.search(text) or _DOMAIN.search(text):
        raise ValueError(f"{key}: no links or web addresses in a testimonial")
    if _EMAIL.search(text):
        raise ValueError(f"{key}: no email addresses in a testimonial (not even example ones)")
    return text


def check_testimonial(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The testimonial exactly as it would be shown, or ValueError("<field>: <why>")."""
    unknown = sorted(set(payload) - PUBLISH_FIELDS)
    if unknown:
        raise ValueError(f"{unknown[0]}: not a testimonial field (allowed: "
                         f"{', '.join(sorted(PUBLISH_FIELDS))})")
    missing = sorted(PUBLISH_FIELDS - set(payload))
    if missing:
        raise ValueError(f"{missing[0]}: required")
    tid = payload["testimonial_id"]
    if not isinstance(tid, str) or not _TESTIMONIAL_ID.fullmatch(tid):
        raise ValueError("testimonial_id: tm_ and 24 lowercase hex characters")
    rating = payload["rating"]
    if isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 5:
        raise ValueError("rating: a whole number from 1 to 5")
    name = _text_rules("display_name", payload["display_name"], *NAME_LENGTH, multiline=False)
    if "@" in name or _contact_problem(name):
        raise ValueError("display_name: a name only - never an email address or a number")
    body = _text_rules("body", payload["body"], *BODY_LENGTH, multiline=True)
    return {"testimonial_id": tid, "order_id": check_order_id(payload["order_id"]),
            "rating": rating, "display_name": name, "body": body}


class TestimonialAdapter(ClientAdapter):
    """Client testimonials as gated, audited Pionir capabilities (ClientAdapter's transport)."""

    def __init__(self, settings: ClientSettings | None = None, *, opener=None) -> None:
        super().__init__(settings, opener=opener)
        self._manifest = AgentManifest(
            agent_id="testimonials",
            version="pionir/testimonials",
            capabilities=(
                Capability(
                    name=TESTIMONIALS,
                    description="List the consented client testimonials waiting for the owner, "
                                "and the feedback and referral counts",
                    risk=RiskLevel.READ_ONLY,
                    routable=False,
                ),
                Capability(
                    name=PUBLISH_TESTIMONIAL,
                    description="Show a client's testimonial on the public /hire page "
                                "(only after the owner approves its exact words and name)",
                    risk=RiskLevel.PRIVILEGED,
                    required_permissions=frozenset({PUBLISH_TESTIMONIAL}),
                    requires_approval=True,
                    routable=False,
                ),
            ),
        )

    def status(self) -> Mapping[str, Any]:
        if read_token(self.settings.token_file) is None:
            raise AdapterUnavailable(self._not_configured())
        publish = self.settings.publish_token_file
        return {"url": self._base, "token": "configured",
                "publish_token": "configured" if publish and read_token(publish) else "missing"}

    def _publish_token(self) -> str | None:
        path = self.settings.publish_token_file
        return read_token(path) if path is not None else None

    def validate(self, task: Task) -> None:
        self._request(task)
        if task.capability == PUBLISH_TESTIMONIAL and self._publish_token() is None:
            # asking the owner to approve what cannot then go live wastes his yes
            raise AdapterUnavailable(PUBLISH_TOKEN_HINT)

    def _request(self, task: Task) -> tuple[str, str, dict[str, Any] | None]:
        try:
            if task.capability == TESTIMONIALS:
                if task.payload:
                    raise ValueError(f"{sorted(task.payload)[0]}: client.testimonials takes no "
                                     "fields")
                return "GET", PENDING_ROUTE, None
            if task.capability == PUBLISH_TESTIMONIAL:
                t = check_testimonial(task.payload)
                return "POST", APPROVE_ROUTE, {"id": t["testimonial_id"], "rating": t["rating"],
                                               "display_name": t["display_name"],
                                               "body": t["body"]}
        except ValueError as error:
            raise AdapterProtocolError(f"{task.capability} refused by Pionir - {error}") from error
        raise AdapterProtocolError(f"testimonials has no capability {task.capability!r}")

    def execute(self, task: Task) -> TaskResult:
        method, path, body = self._request(task)
        token = (self._publish_token() if task.capability == PUBLISH_TESTIMONIAL
                 else read_token(self.settings.token_file))
        if token is None:
            why = PUBLISH_TOKEN_HINT if task.capability == PUBLISH_TESTIMONIAL \
                else self._not_configured()
            return self._result(task, _unavailable(why, not_configured=True))
        status, document = self._http(method, path, body, token)
        output = self._answer(task.capability, status, document, token)
        return self._result(task, json.loads(self._scrub(json.dumps(output), token)))
