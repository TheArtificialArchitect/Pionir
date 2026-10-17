"""Techne, Scrooge's site-delivery jobs, as a Pionir organ.

Scrooge (the Dokaz API at ``api.dokaz.net``) runs the Techne pipeline: paid website jobs
that move through stages - ``awaiting_brief`` -> ``queued`` -> ``building`` ->
``preview_ready`` -> ``approved`` -> ``going_live`` -> ``live`` (with ``taking_down`` ->
``down``, and ``rejected`` / ``failed``). The ADMIN steps - create a job, approve or reject a
preview, requeue it, take a live site down, keep or dismiss one - are the owner's, and
Scrooge answers 403 to Pionir's ops token for every one of them. Pionir never attempts
them: that 403, and the fact that this organ exposes no name for them, is what keeps the
human approval gate structural rather than a convention.

What the ops token CAN do is the doer's side of a job, and that is all this organ is:

- ``techne.jobs`` is READ_ONLY (``routable=True``): the jobs as Scrooge lists them
  (optionally one status), with every PII field REDACTED. A job carries the client's brief
  (``intake_json``), their email, Stripe ids and any note; none of that leaves this adapter.
  Pionir keeps only ``id``, ``slug``, ``domain``, ``status``, ``failed_stage``,
  ``preview_url``, ``live_url``, ``plan_interval`` and the ``*_at`` timestamps - a strict
  allowlist, so a field Scrooge adds later is dropped until it is listed here on purpose.
- ``techne.run`` is PRIVILEGED with ``requires_approval=True`` and ``routable=False``: it
  performs ONE allowlisted doer action and so it parks on the owner's Desk on EVERY call,
  exactly like ``client.email`` or ``security.nyx_run``. The ``action`` is one of
  ``claim``, ``preview``, ``live``, ``down`` or ``fail`` - checked against that allowlist in
  ``validate()`` BEFORE anything is parked, so a request for an admin action (``approve``,
  ``create``, ``requeue``, ``takedown`` ...) is refused outright, never parked for a yes
  that could never run. Each action's body is checked too: ``claim`` {id, stage},
  ``preview`` {id, preview_url} (a ``*.pages.dev`` link, as Scrooge requires), ``live``
  {id, live_url}, ``down`` {id}, ``fail`` {id, note}.

The ops token is read from a file on every call (so adding it needs no restart), sent only
in the ``x-dash-token`` header, and never appears in a log, an error or a result. Scrooge
saying no to a request (a field it rejects, a status it will not move from - its
transitions are guarded ``UPDATE ... WHERE id=? AND status=?``, so a wrongly-timed action
is a no-op, not a corruption) comes back as ``ok: false`` with ``refused``; a missing or
rejected token, a rate limit, or Scrooge not answering as ``ok: false`` with
``unavailable`` - answers, not faults, so the circuit breaker is not tripped.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pionir.adapters.content import DEFAULT_CONTENT_URL, read_token
from pionir.contracts import AgentManifest, Capability, RiskLevel, Task, TaskResult
from pionir.errors import AdapterProtocolError, AdapterUnavailable

_log = logging.getLogger(__name__)

JOBS = "techne.jobs"
RUN = "techne.run"

# Every status a Techne job can be in (worker/src/techne-api.ts). techne.jobs' optional
# `status` filter is checked against this before any call.
TECHNE_STATES = ("awaiting_brief", "queued", "building", "preview_ready", "approved",
                 "going_live", "live", "taking_down", "down", "rejected", "failed")

# The ONLY actions techne.run may perform: the doer's side of a job. Every ADMIN action
# (create, attach, approve, reject, requeue, takedown, keep, dismiss) is the owner's and is
# 403 to the ops token - none is here, so none can be reached, parked or attempted.
RUN_ACTIONS = ("claim", "preview", "live", "down", "fail")
# What `claim` may set the stage to.
CLAIM_STAGES = ("build", "live", "down")
# The body each action needs, besides `action` itself.
RUN_FIELDS: dict[str, frozenset[str]] = {
    "claim": frozenset({"id", "stage"}),
    "preview": frozenset({"id", "preview_url"}),
    "live": frozenset({"id", "live_url"}),
    "down": frozenset({"id"}),
    "fail": frozenset({"id", "note"}),
}

# What a redacted job keeps. A strict allowlist: intake_json, customer_email, stripe_* and
# note are PII and are dropped by not being here, as is anything Scrooge adds later.
KEEP_FIELDS = ("id", "slug", "domain", "status", "failed_stage", "preview_url", "live_url",
               "plan_interval")

NOTE_MAX = 1000
MAX_RESPONSE_BYTES = 4_000_000

SETUP_HINT = r"run Scrooge's tools\setup-ops-token.ps1"
TOKEN_REJECTED = f"the ops token was rejected - {SETUP_HINT}"


def redact_job(job: Mapping[str, Any]) -> dict[str, Any]:
    """One Techne job with every PII field removed: only the allowlisted fields and the
    ``*_at`` timestamps are kept."""
    out = {k: job[k] for k in KEEP_FIELDS if k in job}
    for key, value in job.items():
        if isinstance(key, str) and key.endswith("_at"):
            out[key] = value
    return out


def check_listing(payload: Mapping[str, Any]) -> str | None:
    """The optional ``status`` filter for techne.jobs, or None, or ValueError."""
    unknown = sorted(set(payload) - {"status"})
    if unknown:
        raise ValueError(f"{unknown[0]}: not a jobs field (only status)")
    status = payload.get("status")
    if status is None:
        return None
    if status not in TECHNE_STATES:
        raise ValueError(f"status: one of {', '.join(TECHNE_STATES)}")
    return str(status)


def _check_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError("id: required, the job's id")
    return value


def _check_pages_dev(value: Any) -> str:
    """A preview URL: https to a ``*.pages.dev`` host, as Scrooge requires."""
    host = _https_host(value, "preview_url")
    if host != "pages.dev" and not host.endswith(".pages.dev"):
        raise ValueError("preview_url: must be a *.pages.dev link (the Cloudflare Pages preview)")
    return value


def _check_live_url(value: Any) -> str:
    _https_host(value, "live_url")
    return value


def _https_host(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field}: required, an https URL")
    if len(value) > 2000:
        raise ValueError(f"{field}: at most 2000 characters")
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError as error:
        raise ValueError(f"{field}: not a valid URL") from error
    if parsed.scheme != "https":
        raise ValueError(f"{field}: must start with https://")
    host = (parsed.hostname or "").lower()
    if not host or parsed.username or parsed.password:
        raise ValueError(f"{field}: must be a plain https URL with no credentials")
    return host


def _check_note(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("note: required, a short reason the job failed")
    if len(value) > NOTE_MAX:
        raise ValueError(f"note: at most {NOTE_MAX} characters (this is {len(value)})")
    if any(ord(ch) < 32 and ch not in "\r\n\t" for ch in value):
        raise ValueError("note: plain text only (no control characters)")
    return value


def check_run(payload: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """``(action, body)`` for techne.run, or ValueError. The allowlist is checked FIRST, so
    an admin action is refused before its fields are ever looked at."""
    action = payload.get("action")
    if not isinstance(action, str) or action not in RUN_ACTIONS:
        raise ValueError(
            f"action: one of {', '.join(RUN_ACTIONS)} "
            "(admin actions such as approve, reject, create, requeue, takedown, keep or "
            "dismiss are the owner's and are not allowed)")
    fields = RUN_FIELDS[action]
    unknown = sorted(set(payload) - {"action"} - fields)
    if unknown:
        raise ValueError(f"{unknown[0]}: not a field of action {action!r} "
                         f"(allowed: action, {', '.join(sorted(fields))})")
    missing = sorted(fields - set(payload))
    if missing:
        raise ValueError(f"{missing[0]}: required for action {action!r}")
    body: dict[str, Any] = {"id": _check_id(payload["id"])}
    if action == "claim":
        stage = payload["stage"]
        if stage not in CLAIM_STAGES:
            raise ValueError(f"stage: one of {', '.join(CLAIM_STAGES)}")
        body["stage"] = stage
    elif action == "preview":
        body["preview_url"] = _check_pages_dev(payload["preview_url"])
    elif action == "live":
        body["live_url"] = _check_live_url(payload["live_url"])
    elif action == "fail":
        body["note"] = _check_note(payload["note"])
    return action, body


# ---- settings -----------------------------------------------------------------------------
Opener = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class ScroogeTechneSettings:
    """Where Scrooge is and where the ops token lives (its PATH, never the token)."""

    base_url: str = DEFAULT_CONTENT_URL
    token_file: Path = Path("~/.pionir/secrets/scrooge-ops-token.txt")
    timeout_seconds: int = 30

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlparse(self.base_url)
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        # The token is a secret: it only ever travels over TLS, or to this machine.
        if not (parsed.scheme == "https" or (parsed.scheme == "http" and loopback)):
            raise ValueError("the Techne URL must be https: (or http: on loopback)")
        if not parsed.hostname:
            raise ValueError("the Techne URL needs a host")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("the Techne URL cannot carry credentials or query data")
        if self.timeout_seconds < 5:
            raise ValueError("the Techne timeout must be at least 5 seconds")
        object.__setattr__(self, "token_file", Path(self.token_file).expanduser())


def scrooge_techne_settings(configured: Any) -> ScroogeTechneSettings:
    """The ScroogeTechneSettings for a PionirSettings: its Scrooge and the ops token the
    client.* capabilities already use (the same file; no new secret)."""
    return ScroogeTechneSettings(base_url=configured.content_url or DEFAULT_CONTENT_URL,
                                 token_file=configured.ops_token_path)


def _refused(why: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "refused": why, "error": why, **extra}


def _unavailable(why: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "unavailable": why, "error": why, **extra}


class ScroogeTechneAdapter:
    """Techne's site-delivery jobs as a read capability plus one gated, approval-parked
    doer action."""

    def __init__(self, settings: ScroogeTechneSettings | None = None, *,
                 opener: Opener | None = None) -> None:
        self.settings = settings or ScroogeTechneSettings()
        self._base = self.settings.base_url.rstrip("/")
        self._open = opener or urllib.request.build_opener().open
        self._manifest = AgentManifest(
            agent_id="scrooge",
            version="pionir/techne",
            capabilities=(
                Capability(
                    name=JOBS,
                    description="List the Techne site-delivery jobs (optionally by status); "
                                "the client's brief, email and payment details are redacted",
                    risk=RiskLevel.READ_ONLY,
                    routing_hints=frozenset({"techne", "site", "website", "delivery",
                                             "hosting", "pages", "job"}),
                    routable=True,
                ),
                Capability(
                    name=RUN,
                    description="Perform one Techne doer action - claim, preview, live, "
                                "down or fail (only after the owner approves it; admin "
                                "steps are the owner's and are refused)",
                    risk=RiskLevel.PRIVILEGED,
                    required_permissions=frozenset({RUN}),
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
        """Refuse a bad request - above all a non-allowlisted action - before it is parked."""
        self._request(task)
        if task.capability == RUN and read_token(self.settings.token_file) is None:
            # Asking the owner to approve an action that cannot run wastes his yes.
            raise AdapterUnavailable(self._not_configured())

    def _request(self, task: Task) -> tuple[str, str, dict[str, Any] | None]:
        """(method, path, JSON body) for this task, or AdapterProtocolError."""
        try:
            if task.capability == JOBS:
                status = check_listing(task.payload)
                query = f"?{urllib.parse.urlencode({'status': status})}" if status else ""
                return "GET", f"/dash/techne/jobs{query}", None
            if task.capability == RUN:
                action, body = check_run(task.payload)
                return "POST", f"/dash/techne/jobs/{action}", body
        except ValueError as error:
            raise AdapterProtocolError(f"{task.capability} refused by Pionir - {error}") from error
        raise AdapterProtocolError(f"techne has no capability {task.capability!r}")

    # ---- the call ------------------------------------------------------------------
    def execute(self, task: Task) -> TaskResult:
        method, path, body = self._request(task)
        token = read_token(self.settings.token_file)
        if token is None:
            return self._result(task, _unavailable(self._not_configured(),
                                                   not_configured=True))
        status, document = self._http(method, path, body, token)
        output = self._answer(task.capability, status, document, token)
        if task.capability == RUN:
            _log.info("techne: run %s for job %s: %s",
                      task.payload.get("action"), body.get("id") if body else "?",
                      "done" if output.get("ok") is True else "not done")
        # Belt and braces: whatever Scrooge echoed, the token does not leave in the result.
        return self._result(task, json.loads(self._scrub(json.dumps(output), token)))

    def _answer(self, capability: str, status: int, document: Any,
                token: str) -> dict[str, Any]:
        doc = document if isinstance(document, Mapping) else {}
        said = self._scrub(str(doc.get("error") or ""), token)[:300]
        if 200 <= status < 300:
            if doc.get("ok") is True:
                if capability == JOBS:
                    jobs = doc.get("jobs")
                    rows = [redact_job(j) for j in jobs if isinstance(j, Mapping)] \
                        if isinstance(jobs, list) else []
                    return {"ok": True, "jobs": rows}
                return dict(doc)
            return _unavailable("Scrooge answered without ok: true", status=status)
        if status in (400, 409):
            # Scrooge said no to this request: an answer, not a fault. Its transitions are
            # guarded WHERE id=? AND status=?, so a wrongly-timed action is a refusal here.
            return _refused(said or f"Scrooge refused it (HTTP {status})", status=status)
        if status in (401, 403):
            return _unavailable(TOKEN_REJECTED, status=status, token_rejected=True)
        if status == 429:
            return _unavailable("Scrooge is rate limiting - try again later", status=429)
        if status == 0:
            why = f"Scrooge is unreachable at {urllib.parse.urlparse(self._base).netloc}"
        else:
            why = f"Scrooge answered HTTP {status}" + (f": {said}" if said else "")
        return _unavailable(why, status=status)

    # ---- transport -----------------------------------------------------------------
    def _http(self, method: str, path: str, body: Mapping[str, Any] | None,
              token: str) -> tuple[int, Any]:
        """(HTTP status, parsed JSON or None). Status 0 means nothing answered."""
        headers = {"Accept": "application/json", "User-Agent": "pionir-techne/0.1",
                   "x-dash-token": token}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"{self._base}{path}", data=data, method=method,
                                         headers=headers)
        try:
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
        evidence = [f"techne:{task.capability.split('.', 1)[1]}"]
        job_id = task.payload.get("id")
        if isinstance(job_id, str) and job_id.strip():
            evidence.append(f"techne:job:{job_id}")
        action = task.payload.get("action")
        if task.capability == RUN and isinstance(action, str):
            evidence.append(f"techne:run:{action}")
        return TaskResult(task_id=task.task_id, agent_id=self.manifest.agent_id,
                          output=output, evidence=tuple(evidence))


__all__ = [
    "JOBS",
    "RUN",
    "RUN_ACTIONS",
    "TECHNE_STATES",
    "ScroogeTechneAdapter",
    "ScroogeTechneSettings",
    "check_run",
    "redact_job",
    "scrooge_techne_settings",
]
