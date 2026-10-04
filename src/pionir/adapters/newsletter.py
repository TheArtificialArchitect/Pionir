"""Adapter for the newsletter: one email to every confirmed subscriber, only after the
owner's yes.

Scrooge (``api.dokaz.net``) keeps the list - double opt-in, a one-click unsubscribe link and
a ``List-Unsubscribe`` header in every copy - and sends in batches across its cron runs.
This adapter hands it one finished newsletter. Ian's rule, the same as for the blog:
nothing customer-facing goes out without his approval, and holding a permission is not a
way around it. So ``content.newsletter_send`` is PRIVILEGED with ``requires_approval=True``
(PionirApp parks it on EVERY call), NOT batchable (an email to the whole list is its own
card, never a line in the daily digest), and ``routable=False`` (reached only by name). The
Discord card shows the subject, the whole body and the footer Scrooge adds to every copy.

The payload is exactly ``{newsletter_id, subject, body_md}``, checked here with the blog's
own rules (``content._text_problem`` / ``_scrooge_problem``) - never looser than Scrooge's
``POST /dash/content/newsletter`` - plus the newsletter's: every link to a Dokaz page
carries ``utm_source=newsletter``, ``utm_medium=email`` and ``utm_campaign`` = the
newsletter's campaign (its id, first 40 characters, no leading or trailing ``-``).
C:\\src\\Scrooge\\docs\\TRAFFIC.md is the rule.

Never twice: ``<state_root>/newsletter/sends.json`` maps each newsletter_id Scrooge has
taken to its send, and an id in it is refused - before parking (asking the owner to approve
a second copy wastes his yes) and again before sending. Scrooge's own answer that it already
has the id (409) is recorded too: it is queued there, whatever this ledger knew.

The publish token is the blog's (``ContentSettings.token_file``), read on every call, sent
only in the ``x-dash-token`` header, and never in a log, an error or a result. Scrooge
refusing the newsletter (400, 409) comes back as ``ok: false`` with ``refused``; a missing or
rejected token, Scrooge's sender not configured (503: no postal address or mail key), or
Scrooge not answering as ``ok: false`` with ``unavailable`` - answers, not faults, so the
circuit breaker is not tripped.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pionir import atomic
from pionir.adapters.content import (
    _LINK_TARGET,
    _REFERENCE_DEF,
    _SCHEMED_URL,
    ALLOWED_LINK_HOSTS,
    ContentAdapter,
    ContentSettings,
    _scrooge_problem,
    _text_problem,
    read_token,
)
from pionir.contracts import AgentManifest, Capability, RiskLevel, Task, TaskResult
from pionir.errors import AdapterProtocolError, AdapterUnavailable

_log = logging.getLogger(__name__)

SEND = "content.newsletter_send"
ROUTE = "/dash/content/newsletter"
FIELDS = frozenset({"newsletter_id", "subject", "body_md"})
LENGTHS = {"subject": (10, 120), "body_md": (200, 20_000)}
_ID = re.compile(r"[a-z0-9-]{1,64}")
_SEND_ID = re.compile(r"nl_[0-9a-f]{8,64}")

UTM_SOURCE = "newsletter"
UTM_MEDIUM = "email"
UTM_CAMPAIGN_MAX = 40

# What Scrooge adds to EVERY copy (worker/src/newsletter.ts). Part of the email the owner
# approves, so the card shows it - with the two parts that differ per copy as placeholders.
FOOTER_LINES = (
    "-- ",
    ("You are receiving this because you subscribed to the Dokaz Industries newsletter at "
     "api.dokaz.net and confirmed your address."),
    "Unsubscribe with one click: <a link of its own in each copy>",
    "Dokaz Industries · <the postal address Scrooge is configured with>",
)
FOOTER_HEADERS = "List-Unsubscribe (that copy's link) and List-Unsubscribe-Post: One-Click"


def utm_campaign(newsletter_id: str) -> str:
    """The newsletter's campaign: its id's first 40 characters, without a leading or
    trailing '-'. Scrooge derives it the same way and refuses a link that disagrees."""
    return str(newsletter_id or "")[:UTM_CAMPAIGN_MAX].strip("-")


def utm_query(newsletter_id: str) -> str:
    return (f"utm_source={UTM_SOURCE}&utm_medium={UTM_MEDIUM}"
            f"&utm_campaign={utm_campaign(newsletter_id)}")


def _links(text: str) -> list[str]:
    found = [m.group(0) for m in _SCHEMED_URL.finditer(text)]
    found += [m.group(1) for m in _LINK_TARGET.finditer(text)]
    found += [m.group(1) for m in _REFERENCE_DEF.finditer(text)]
    return [f.strip() for f in found if f.strip()]


def _utm_problem(text: str, campaign: str) -> str | None:
    """Every link to a Dokaz page carries the newsletter's three tags, each exactly once.
    The text rules have already confined links to the Dokaz hosts; an API endpoint (/v1/)
    is not a page."""
    want = {"utm_source": UTM_SOURCE, "utm_medium": UTM_MEDIUM, "utm_campaign": campaign}
    for url in _links(text):
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        if host not in ALLOWED_LINK_HOSTS:
            continue
        if host == "api.dokaz.net" and parts.path.startswith("/v1/"):
            continue
        query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
        if any(query.get(k) != [v] for k, v in want.items()):
            return ("every link to a Dokaz page must carry utm_source=newsletter&"
                    f"utm_medium=email&utm_campaign={campaign} ({url[:120]!r} does not)")
    return None


def check_newsletter(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The newsletter exactly as it will be sent, or ValueError("<field>: <why>")."""
    if not isinstance(payload, Mapping):
        # ValueError like every other rule: one "<field>: <why>" refusal type
        raise ValueError("payload: must be an object of newsletter_id, subject and body_md")  # noqa: TRY004
    unknown = sorted(set(payload) - FIELDS)
    if unknown:
        raise ValueError(f"{unknown[0]}: not a newsletter field (allowed: "
                         f"{', '.join(sorted(FIELDS))})")
    newsletter_id = payload.get("newsletter_id")
    if not isinstance(newsletter_id, str) or not _ID.fullmatch(newsletter_id):
        raise ValueError("newsletter_id: 1-64 of a-z, 0-9 and '-'")
    campaign = utm_campaign(newsletter_id)
    if not campaign:
        raise ValueError("newsletter_id: has no usable campaign (it is all '-')")
    out: dict[str, Any] = {"newsletter_id": newsletter_id}
    for key, (low, high) in LENGTHS.items():
        text = payload.get(key)
        if not isinstance(text, str):
            raise ValueError(f"{key}: required, a string")  # noqa: TRY004
        if not low <= len(text) <= high:
            raise ValueError(f"{key}: {low}-{high} characters (this is {len(text)})")
        if not text.strip():
            raise ValueError(f"{key}: is blank")
        # the blog's rules (body_md is the multi-line field there too), then the tags
        problem = (_text_problem(text) or _scrooge_problem(key, text)
                   or _utm_problem(text, campaign))
        if problem:
            raise ValueError(f"{key}: {problem}")
        out[key] = text
    return out


# ---- settings ----------------------------------------------------------------------------
Opener = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class NewsletterSettings:
    """Where Scrooge is and its publish token (the blog's ``ContentSettings``), and where
    the ledger of newsletters Scrooge has taken is kept."""

    content: ContentSettings = field(default_factory=ContentSettings)
    ledger_file: Path = Path("~/.pionir/newsletter/sends.json")

    def __post_init__(self) -> None:
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


# One send at a time per ledger: two approvals of the same newsletter running together must
# not both find it absent and both queue it.
_LOCKS: dict[Path, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
# What this process has had Scrooge take, per ledger, in case the ledger file could not be
# written: a newsletter that was queued is not queued again while Pionir runs.
_SENT: dict[Path, dict[str, dict[str, Any]]] = {}


def _ledger_lock(path: Path) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(path.resolve(), threading.Lock())


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class NewsletterAdapter:
    """Sending the newsletter to every confirmed subscriber as a gated, audited capability."""

    def __init__(self, settings: NewsletterSettings | None = None, *,
                 opener: Opener | None = None,
                 clock: Callable[[], str] | None = None) -> None:
        self.settings = settings or NewsletterSettings()
        # The blog adapter's transport: the same token handling, the same JSON reading and
        # the same status-0-means-nothing-answered contract, kept in one place.
        self._content = ContentAdapter(self.settings.content, opener=opener)
        self._base = self.settings.content.base_url.rstrip("/")
        self._now = clock or _utc_now
        self._manifest = AgentManifest(
            agent_id="newsletter",
            version="pionir/newsletter",
            capabilities=(
                Capability(
                    name=SEND,
                    description="Email one newsletter to every confirmed subscriber of the "
                                "Dokaz newsletter (only after the owner approves it)",
                    risk=RiskLevel.PRIVILEGED,
                    required_permissions=frozenset({SEND}),
                    requires_approval=True,
                    # an email to the whole list is never a line in the daily digest
                    batchable=False,
                    routable=False,
                ),
            ),
        )

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    # ---- configuration (local files only) --------------------------------------------
    def _not_configured(self) -> str:
        return (f"not configured: no publish token at {self.settings.content.token_file} - "
                "put Scrooge's publish token in that file")

    def status(self) -> Mapping[str, Any]:
        """Local only - no call to Scrooge, so doctor stays hermetic and fast."""
        if read_token(self.settings.content.token_file) is None:
            raise AdapterUnavailable(self._not_configured())
        try:
            sent: Any = len(self._read_ledger())
        except _Failure as failure:
            sent = failure.output.get("error")
        return {"url": self._base, "token": "configured", "sent": sent}

    # ---- the request -----------------------------------------------------------------
    def validate(self, task: Task) -> None:
        """Refuse a bad newsletter, or one that cannot run, before it is parked."""
        newsletter = self._check(task)
        if read_token(self.settings.content.token_file) is None:
            # Asking the owner to approve an email that cannot be sent wastes his yes.
            raise AdapterUnavailable(self._not_configured())
        try:
            done = self._already(newsletter["newsletter_id"])
        except _Failure as failure:
            raise AdapterUnavailable(str(failure.output.get("error"))) from None
        if done is not None:
            raise AdapterProtocolError(
                f"{SEND} refused by Pionir - newsletter_id: already sent (send "
                f"{done.get('send_id')}, queued at {done.get('queued_at')})")

    @staticmethod
    def _check(task: Task) -> dict[str, Any]:
        if task.capability != SEND:
            raise AdapterProtocolError(f"newsletter has no capability {task.capability!r}")
        try:
            return check_newsletter(task.payload)
        except ValueError as error:
            raise AdapterProtocolError(f"{SEND} refused by Pionir - {error}") from error

    # ---- the call --------------------------------------------------------------------
    def execute(self, task: Task) -> TaskResult:
        newsletter = self._check(task)
        token = read_token(self.settings.content.token_file)
        if token is None:
            why = self._not_configured()
            return self._result(task, {"ok": False, "unavailable": why, "error": why,
                                       "not_configured": True})
        try:
            with _ledger_lock(self.settings.ledger_file):
                output = self._send(newsletter, token)
        except _Failure as failure:
            output = failure.output
        # Belt and braces: whatever went wrong, the token does not leave in the result.
        return self._result(task, json.loads(self._scrub(json.dumps(output), token)))

    def _send(self, newsletter: Mapping[str, Any], token: str) -> dict[str, Any]:
        newsletter_id = newsletter["newsletter_id"]
        done = self._already(newsletter_id)
        if done is not None:
            raise _refused(f"already sent (send {done.get('send_id')}, queued at "
                           f"{done.get('queued_at')})", send_id=done.get("send_id"),
                           newsletter_id=newsletter_id)
        status, document = self._content._post(ROUTE, dict(newsletter), token)
        document = json.loads(self._scrub(json.dumps(dict(document)), token))
        if 200 <= status < 300:
            if document.get("ok") is not True:
                raise AdapterProtocolError(f"Scrooge answered {ROUTE} without ok: true")
            return self._record(newsletter_id, document)
        said = str(document.get("error") or f"HTTP {status}")[:300]
        if status == 409:
            # Scrooge already has this newsletter: it is queued there. Noted, so it is never
            # offered again; the answer is still a refusal of THIS call.
            send_id = document.get("send_id")
            self._note(newsletter_id, {
                "send_id": send_id if isinstance(send_id, str) else None,
                "status": document.get("status") if isinstance(document.get("status"), str)
                else None, "queued_at": None, "recipients": None, "noted_at": self._now(),
                "from": "scrooge_409"})
            raise _refused(said, status=409, send_id=send_id, newsletter_id=newsletter_id)
        if status == 400:
            raise _refused(said, status=400)
        if status in (401, 403):
            raise _unavailable("the publish token was rejected", status=status)
        if status == 503:
            raise _unavailable(f"Scrooge cannot send newsletters yet: {said}", status=503,
                               not_configured=True)
        if status == 0:
            raise _unavailable(f"Scrooge is unreachable at {self._base}; check its dashboard "
                               "before trying again", status=0)
        raise _unavailable(f"Scrooge answered HTTP {status}: {said}", status=status)

    def _record(self, newsletter_id: str, document: Mapping[str, Any]) -> dict[str, Any]:
        """Scrooge has queued it: note it at once, so it is never queued twice."""
        send_id = document.get("send_id")
        recipients = document.get("recipients")
        status = document.get("status")
        usable = (isinstance(send_id, str) and _SEND_ID.fullmatch(send_id) is not None
                  and isinstance(recipients, int) and not isinstance(recipients, bool)
                  and recipients >= 0 and status in ("queued", "done")
                  and document.get("newsletter_id") == newsletter_id)
        entry = {"send_id": send_id if isinstance(send_id, str) else None,
                 "status": status if isinstance(status, str) else None,
                 "recipients": recipients if usable else None,
                 "queued_at": document.get("queued_at")
                 if isinstance(document.get("queued_at"), str) else None,
                 "noted_at": self._now(), "from": "scrooge_200"}
        if usable:
            output: dict[str, Any] = {"ok": True, "newsletter_id": newsletter_id,
                                      "send_id": send_id, "status": status,
                                      "recipients": recipients,
                                      "queued_at": entry["queued_at"]}
        else:
            # It is probably queued: recording it keeps a retry from queueing it again.
            why = ("Scrooge accepted the newsletter but its answer did not say which send it "
                   "is - it may be going out; check Scrooge's dashboard (the ledger now holds "
                   "this newsletter)")
            output = {"ok": False, "unavailable": why, "error": why,
                      "newsletter_id": newsletter_id}
        self._note(newsletter_id, entry, output)
        return output

    def _note(self, newsletter_id: str, entry: dict[str, Any],
              output: dict[str, Any] | None = None) -> None:
        self._sent()[newsletter_id] = entry
        try:
            self._write_ledger(newsletter_id, entry)
        except OSError as error:
            if output is not None:
                # Queued: a failure to note it is a warning - reporting "failed" now would
                # invite a second, duplicate send.
                output["ledger_error"] = (
                    f"the newsletter is queued but could not be recorded in "
                    f"{self.settings.ledger_file} ({type(error).__name__}); do not send this "
                    "newsletter again")
            _log.warning("newsletter: %s is queued but the ledger write failed (%s)",
                         newsletter_id, type(error).__name__)
        else:
            _log.info("newsletter: %s recorded as send %s", newsletter_id, entry.get("send_id"))

    # ---- the ledger ------------------------------------------------------------------
    def _sent(self) -> dict[str, dict[str, Any]]:
        with _LOCKS_GUARD:
            return _SENT.setdefault(self.settings.ledger_file.resolve(), {})

    def _already(self, newsletter_id: str) -> Mapping[str, Any] | None:
        """The send a newsletter already became (the ledger, or this process's memory of
        it), or None. Any entry counts, even an empty one."""
        done = self._read_ledger().get(newsletter_id)
        return done if done is not None else self._sent().get(newsletter_id)

    def _read_ledger(self) -> dict[str, Any]:
        path = self.settings.ledger_file
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as error:
            raise _unavailable(f"the newsletter ledger at {path} cannot be read "
                               f"({type(error).__name__})") from None
        try:
            document = json.loads(raw)
        except ValueError:
            document = None
        if not isinstance(document, dict) or not all(isinstance(v, dict)
                                                     for v in document.values()):
            # Unreadable means "might already be sent": stop rather than risk a second email.
            raise _unavailable(f"the newsletter ledger at {path} is not a JSON object of "
                               "newsletter_id -> {send_id, ...}; fix it before sending")
        return document

    def _write_ledger(self, newsletter_id: str, entry: Mapping[str, Any]) -> None:
        """Add one entry, atomically: a crash leaves the old ledger or the new one."""
        path = self.settings.ledger_file
        try:
            current = self._read_ledger()
        except _Failure:
            current = {}
        current[newsletter_id] = dict(entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".sends-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(current, handle, indent=2, sort_keys=True)
            atomic.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # ---- the rest --------------------------------------------------------------------
    @staticmethod
    def _scrub(text: str, token: str) -> str:
        return text.replace(token, "<redacted>") if token else text

    def _result(self, task: Task, output: dict[str, Any]) -> TaskResult:
        evidence = ["newsletter:send"]
        if output.get("ok") is True and output.get("send_id"):
            evidence.append(f"newsletter:send:{output['send_id']}")
        return TaskResult(task_id=task.task_id, agent_id=self.manifest.agent_id,
                          output=output, evidence=tuple(evidence))
