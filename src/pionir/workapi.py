"""The work log's HTTP door: the owner's surfaces read and write their hours here, and
nothing else can.

    GET    /api/work/summary                        hours and earnings per job, today/week/month/year
    GET    /api/work/jobs
    GET    /api/work/sessions?job=&from=&to=&limit=&deleted=1
    GET    /api/work/log?job=&kind=&from=&to=&limit=
    POST   /api/work/timer/start   {job, note?, allow_concurrent?}
    POST   /api/work/timer/stop    {job, end?, note?}
    POST   /api/work/session       {job, start, end, note?}   (with "id": edit that session)
    POST   /api/work/log           {job, kind, text?, amount_cents?, ts?}
    POST   /api/work/job           {name, kind, hourly_rate_cents?, currency?, set_aside_pct?}
                                   (with "id": update that job; also active)
    DELETE /api/work/session?id=   soft delete
    DELETE /api/work/log?id=       soft delete

It is financial-personal data, so: the owner's SIGNED client (Pionir Desktop) or the
dashboard's session, and nobody else - not the phone (it is relayed through Moss), not a
bot, not a local process that merely reaches the port. ``READERS`` is fixed in code, like
auth.APPROVERS: a grants file cannot add one. Every argument is shape- and size-checked and
an unknown one is refused; a bad request gets a structured ``{"error", "reason"}`` and never
a stack trace or a stored note. Every answer passes secretscrub. The server audits each
successful write (metadata only: which route, which ids - never a note or an amount).
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from urllib.parse import parse_qs

from . import secretscrub
from .worklog import LOG_KINDS, WorkError, WorkLog

READERS = frozenset({"desktop", "dashboard"})

MAX_BODY = 16_384
MAX_QUERY_VALUE = 64
_JOB_REF = re.compile(r"[^\x00-\x1f]{1,120}")

_STATUS = {"bad_input": 400, "too_long": 400, "looks_like_task": 400, "not_found": 404,
           "overlap": 409, "already_open": 409, "other_timer_open": 409, "not_open": 409,
           "stale_timer": 409, "conflict": 409, "limit": 409}

_GET = {
    "/api/work/summary": set(), "/api/work/jobs": set(),
    "/api/work/sessions": {"job", "from", "to", "limit", "deleted"},
    "/api/work/log": {"job", "kind", "from", "to", "limit"},
}
_POST = {
    "/api/work/timer/start": {"job", "note", "allow_concurrent"},
    "/api/work/timer/stop": {"job", "end", "note"},
    "/api/work/session": {"id", "job", "start", "end", "note"},
    "/api/work/log": {"job", "kind", "text", "amount_cents", "ts"},
    "/api/work/job": {"id", "name", "kind", "hourly_rate_cents", "currency", "set_aside_pct",
                      "active"},
}
_DELETE = {"/api/work/session": {"id"}, "/api/work/log": {"id"}}
ROUTES = {"GET": _GET, "POST": _POST, "DELETE": _DELETE}


class BadRequest(ValueError):
    """An argument the door refuses; its message is for the caller."""


def _one(query: Mapping[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if not values:
        return None
    if len(values) != 1:
        raise BadRequest(f"{key} given more than once")
    if len(values[0]) > MAX_QUERY_VALUE:
        raise BadRequest(f"{key} is too long")
    return values[0]


def _int(text: str | None, key: str) -> int | None:
    if text is None or text == "":
        return None
    if not text.isdigit() or len(text) > 9:
        raise BadRequest(f"{key} must be a number")
    return int(text)


def _text(body: Mapping[str, Any], key: str, *, cap: int = 700) -> Any:
    value = body.get(key)
    if value is not None and (not isinstance(value, str) or len(value) > cap):
        raise BadRequest(f"{key} must be text of at most {cap} characters")
    return value


def _job(body: Mapping[str, Any], key: str = "job") -> Any:
    ref = body.get(key)
    if isinstance(ref, bool) or not isinstance(ref, (str, int)) or (isinstance(ref, str) and not _JOB_REF.fullmatch(ref)):
        raise BadRequest("job is required (a name or an id)")
    if isinstance(ref, int) and not 0 < ref < 10**9:
        raise BadRequest("job is required (a name or an id)")
    return ref


def _body(raw: bytes, allowed: set[str]) -> dict[str, Any]:
    if len(raw) > MAX_BODY:
        raise BadRequest("body is too large")
    try:
        doc = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError) as error:
        raise BadRequest("body is not JSON") from error
    if not isinstance(doc, dict):
        raise BadRequest("body must be a JSON object")
    extra = sorted(str(k) for k in doc if k not in allowed)
    if extra:
        raise BadRequest(f"unknown field: {extra[0][:40]}")
    return doc


def _flag(body: Mapping[str, Any], key: str) -> bool:
    value = body.get(key, False)
    if not isinstance(value, bool):
        raise BadRequest(f"{key} must be true or false")
    return value


def serve(method: str, path_info: str, query_string: str, raw: bytes, *, client: str | None,
          refused: str | None, log: WorkLog,
          known: Callable[[], Iterable[str]] = tuple) -> tuple[int, dict[str, Any], str | None]:
    """(status, document, audit detail) for one request under /api/work/. ``client`` and
    ``refused`` are the caller as auth.py identified it. The audit detail is None unless a
    write succeeded; it names the route and ids only."""
    if refused is not None:
        return 401, {"error": "unauthorized", "reason": refused}, None
    if client is None:
        return 401, {"error": "unauthorized", "reason": "the work log needs the owner's signed client"}, None
    if client not in READERS:
        return 403, {"error": "forbidden", "reason": f"{client} may not use the work log"}, None
    routes = ROUTES.get(method)
    if routes is None or path_info not in routes:
        return 404, {"error": "not found"}, None
    try:
        query = parse_qs(query_string, keep_blank_values=True, max_num_fields=12, strict_parsing=False)
    except ValueError:
        return 400, {"error": "bad request", "reason": "bad query"}, None
    try:
        status, doc, detail = _dispatch(method, path_info, query, raw, routes[path_info], log)
    except BadRequest as error:
        return 400, {"error": "bad request", "reason": str(error)}, None
    except WorkError as error:
        return _STATUS.get(error.code, 400), {"error": error.code, "reason": str(error)}, None
    except sqlite3.Error as error:
        return 503, {"error": "store unavailable", "reason": type(error).__name__}, None
    return status, secretscrub.scrub_value(doc, known()), detail


def _dispatch(method: str, path: str, query: Mapping[str, list[str]], raw: bytes,
              allowed: set[str], log: WorkLog) -> tuple[int, dict[str, Any], str | None]:
    if method in ("GET", "DELETE"):
        extra = sorted(k for k in query if k not in allowed)
        if extra:
            raise BadRequest(f"unknown argument: {extra[0][:40]}")
        if raw:
            raise BadRequest(f"{method} takes no body")
    if method == "GET":
        return 200, _read(path, query, log), None
    if method == "DELETE":
        ident = _int(_one(query, "id"), "id")
        if ident is None:
            raise BadRequest("id is required")
        if path == "/api/work/session":
            return 200, {"ok": True, "session": log.delete_session(ident)}, f"session.delete session={ident}"
        return 200, {"ok": True, "entry": log.delete_log(ident)}, f"log.delete log={ident}"
    return _write(path, _body(raw, allowed), log)


def _read(path: str, query: Mapping[str, list[str]], log: WorkLog) -> dict[str, Any]:
    if path == "/api/work/summary":
        return {"ok": True, "summary": log.summary(), "reminder": _reminder()}
    if path == "/api/work/jobs":
        return {"ok": True, "jobs": log.list_jobs()}
    limit = _int(_one(query, "limit"), "limit")
    job, frm, to = _one(query, "job") or None, _one(query, "from") or None, _one(query, "to") or None
    for label, value in (("from", frm), ("to", to)):
        if value is not None:
            _aware(value, label)
    if path == "/api/work/sessions":
        deleted = _one(query, "deleted")
        if deleted not in (None, "", "0", "1"):
            raise BadRequest("deleted must be 0 or 1")
        return {"ok": True, "sessions": log.list_sessions(job, frm, to, include_deleted=deleted == "1",
                                                            limit=limit), "reminder": _reminder()}
    kind = _one(query, "kind") or None
    if kind is not None and kind not in LOG_KINDS:
        raise BadRequest("kind must be note, rubric or payout")
    return {"ok": True, "entries": log.list_log(job, kind, frm, to, limit=limit), "reminder": _reminder()}


def _reminder() -> str:
    from .worklog import CONFIDENTIALITY
    return CONFIDENTIALITY


def _aware(value: str, label: str) -> None:
    """A time over HTTP names its zone: the server's clock zone is nobody's business."""
    from .worklog import Zone, parse_ts
    try:
        parse_ts(value, Zone(None), allow_naive=False)
    except WorkError as error:
        raise BadRequest(f"{label}: {error}") from error


def _time(body: Mapping[str, Any], key: str, *, required: bool) -> Any:
    value = body.get(key)
    if value is None:
        if required:
            raise BadRequest(f"{key} is required")
        return None
    if not isinstance(value, str) or len(value) > 40:
        raise BadRequest(f"{key} must be ISO text like 2026-03-08T14:30:00Z")
    _aware(value, key)
    return value


def _write(path: str, body: dict[str, Any], log: WorkLog) -> tuple[int, dict[str, Any], str | None]:
    if path == "/api/work/timer/start":
        session = log.start_timer(_job(body), note=_text(body, "note"),
                                  allow_concurrent=_flag(body, "allow_concurrent"))
        return 200, {"ok": True, "session": session}, f"timer.start session={session['id']}"
    if path == "/api/work/timer/stop":
        end = _time(body, "end", required=False)
        session = log.stop_timer(_job(body), end=end,
                                 **({"note": _text(body, "note")} if "note" in body else {}))
        return 200, {"ok": True, "session": session}, f"timer.stop session={session['id']}"
    if path == "/api/work/session":
        if "id" in body:
            ident = body["id"]
            if isinstance(ident, bool) or not isinstance(ident, int) or not 0 < ident < 10**9:
                raise BadRequest("id must be a number")
            if "job" in body:
                raise BadRequest("a session cannot change its job")
            fields: dict[str, Any] = {}
            for key in ("start", "end"):
                if key in body:
                    fields[key] = _time(body, key, required=True)
            if "note" in body:
                fields["note"] = _text(body, "note")
            session = log.edit_session(ident, allow_naive=False, **fields)
            return 200, {"ok": True, "session": session}, f"session.edit session={ident}"
        session = log.add_session(_job(body), _time(body, "start", required=True),
                                  _time(body, "end", required=True), _text(body, "note") or "",
                                  allow_naive=False)
        return 200, {"ok": True, "session": session}, f"session.add session={session['id']}"
    if path == "/api/work/log":
        kind = body.get("kind")
        if kind not in LOG_KINDS:
            raise BadRequest("kind must be note, rubric or payout")
        entry = log.add_log(_job(body), kind, _text(body, "text") or "",
                            amount_cents=body.get("amount_cents"), ts=_time(body, "ts", required=False),
                            allow_naive=False)
        return 200, {"ok": True, "entry": entry}, f"log.add log={entry['id']} kind={kind}"
    # /api/work/job
    fields = {k: body[k] for k in ("kind", "hourly_rate_cents", "currency", "set_aside_pct", "active")
              if k in body}
    if "name" in body:
        fields["name"] = _text(body, "name", cap=200)
    if "id" in body:
        job = log.update_job(_job(body, "id"), **fields)
        return 200, {"ok": True, "job": job}, f"job.update job={job['id']}"
    if "active" in fields:
        raise BadRequest("active is for updating a job")
    if "name" not in body:
        raise BadRequest("name is required")
    job = log.create_job(fields.pop("name"), fields.pop("kind", "freelance"), **fields)
    return 200, {"ok": True, "job": job}, f"job.create job={job['id']}"
