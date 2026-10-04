"""The Mail tab's two read-only routes: ``GET /api/mail/inbox`` and ``GET /api/mail/message/<id>``.

The owner's surfaces only (Pionir Desktop signed, or the dashboard's session). Each route
runs ONE read-only capability of the mailbox adapter (``mail.inbox`` / ``mail.read``,
adapters/mailbox.py) through the executive, so the audit trail, the permission rules and the
circuit breaker apply exactly as for any task. There is no route here that sends, deletes,
moves, marks or replies - only GET is handled, and the adapter has no such path either.

Nothing is stored: the mail is returned to the caller and dropped. It never becomes a job
record (the job store keeps results on disk), and never reaches the audit ledger (which is
payload-free by design).

Everything the sender wrote stays under ``untrusted`` exactly as the adapter produced it, so
a surface can render it as inert text. A document that carries no ``ok: true`` always has a
``state`` saying why (``not_configured`` / ``unavailable`` / ``not_found`` / ``error``) and,
when the adapter has one, the plain fix hint in ``error`` - never an empty list in place of a
failure.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from urllib.parse import parse_qs

from . import secretscrub

INBOX_PATH = "/api/mail/inbox"
MESSAGE_PREFIX = "/api/mail/message/"
INBOX_CAPABILITY = "mail.inbox"
READ_CAPABILITY = "mail.read"
OWNER_READERS = frozenset({"dashboard", "desktop"})
_ID = re.compile(r"[1-9][0-9]{0,11}")
_STATUS = {"ok": 200, "not_found": 404, "not_configured": 503, "unavailable": 503, "error": 502}

Run = Callable[[str, dict[str, Any]], Mapping[str, Any]]


def state_of(output: Mapping[str, Any]) -> str:
    """How an adapter answer reads: ok, or the one reason it is not."""
    if output.get("ok") is True:
        return "ok"
    if output.get("not_configured"):
        return "not_configured"
    if output.get("not_found"):
        return "not_found"
    if output.get("unavailable"):
        return "unavailable"
    return "error"


def _bad(reason: str) -> tuple[int, dict[str, Any]]:
    return 400, {"ok": False, "state": "error", "error": reason}


def serve(path_info: str, query_string: str, *, client: str | None, refused: str | None,
          allows: Callable[[str, str], bool], run: Run,
          known: Callable[[], Iterable[str]] = tuple) -> tuple[int, dict[str, Any]]:
    """(status, document) for one GET under /api/mail/.

    ``client``/``refused`` are the caller as auth.py identified it; ``allows(client,
    capability)`` is that client's grant; ``run(capability, payload)`` runs one adapter
    capability through the executive and returns its output; ``known`` gives values that are
    secrets here (this server's client tokens), scrubbed from the answer."""
    if refused is not None:
        return 401, {"error": "unauthorized", "reason": refused}
    if client is None:
        return 401, {"error": "unauthorized", "reason": "the mailbox needs the owner's signed client"}
    if client not in OWNER_READERS:
        return 403, {"error": "forbidden", "reason": f"{client} may not read the mailbox"}

    if path_info == INBOX_PATH:
        capability, payload = INBOX_CAPABILITY, {}
        try:
            query = parse_qs(query_string, keep_blank_values=True, max_num_fields=8)
        except ValueError:
            return _bad("bad query")
        if set(query) - {"limit", "unread"}:
            return _bad("the inbox takes limit and unread only")
        if "limit" in query:
            value = query["limit"][0]
            if not value.isascii() or not value.isdigit() or not 1 <= int(value) <= 50:
                return _bad("limit is a number from 1 to 50")
            payload["limit"] = int(value)
        if "unread" in query:
            if query["unread"][0] not in ("1", "true"):
                return _bad("unread is 1 or true")
            payload["unread"] = True
    elif path_info.startswith(MESSAGE_PREFIX):
        if query_string:
            return _bad("a message takes no query")
        uid = path_info[len(MESSAGE_PREFIX):]
        if not _ID.fullmatch(uid):
            return _bad("a message id is the number mail.inbox gave")
        capability, payload = READ_CAPABILITY, {"id": uid}
    else:
        return 404, {"error": "not found"}

    if not allows(client, capability):
        return 403, {"error": "forbidden", "reason": f"{client} may not use {capability}"}
    output = dict(run(capability, payload))
    state = state_of(output)
    document = {**output, "ok": state == "ok", "state": state}
    if state != "ok" and not isinstance(document.get("error"), str):
        document["error"] = "the mailbox could not be read"
    return _STATUS[state], secretscrub.scrub_value(document, known())
