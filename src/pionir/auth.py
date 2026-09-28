"""Who is calling Pionir's HTTP API, and what each caller may do.

The hole this closes: the loopback API trusted the ``permissions`` a caller put in its
request body, so any local process could POST ``{"capability": "coding.daedalus_solve",
"permissions": ["daedalus.solve"]}`` and the privileged action ran without ever being
parked for the owner. The rule is the owner's: every privileged / public / client /
money action waits for HIS yes (Discord, or the approve button on his phone or
dashboard). Nothing self-approves.

So, here:

- **Permissions are never taken from a request.** They come from exactly two places:
  the server's own approval flow (an approved queue row runs, in-process, with exactly
  the permission recorded on it) and the grant table below, keyed by an authenticated
  client. No client is pre-granted any privileged permission (``GRANTABLE_PERMISSIONS``
  is empty), so every privileged action from every client parks.
- **Callers are identified by a bearer token**, one per client, kept as a file
  ``pionir-client-<client>.token`` in the client token directory (``~/.pionir/secrets``
  on the live box). Pionir makes any missing one on start: 32 random bytes, url-safe,
  readable by the user. A client sends ``Authorization: Bearer <token>``. The owner's
  dashboard holds a SESSION instead: an HttpOnly cookie with a random session id (never
  the token) AND the session's proof, sent as ``X-Session-Proof`` on every call. The
  cookie alone is nothing - cookies ignore the port, so a server another local user
  runs on 127.0.0.1:5555 (or on this port while Pionir is down) is sent it the moment
  the owner's browser goes there. The proof reaches the page in the sign-in
  redirect's #fragment (never sent to a server) and lives in the page's
  sessionStorage, which belongs to this origin alone, port included. Tokens are
  compared in constant time, against every client, with no early exit.
- **Routes are granted per client.** ``approve`` (approve / deny / the digest request)
  belongs to the owner's own surfaces only - ``dashboard`` and ``phone`` - and code, not
  config, decides that (``APPROVERS``). A client never approves an item it parked.

Compatibility window (``PIONIR_AUTH_COMPAT``, default on for this release): a request
with NO token is served as ``anonymous`` - non-privileged work only, a warning logged
each time (rate-limited) - so a partial rollout does not break the stack. A privileged
call without a token is refused (401), never parked, never run. A request carrying a
WRONG token is always refused. Turn it off (``PIONIR_AUTH_COMPAT=off``) once every
client is confirmed sending its token.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from . import atomic
from .errors import PionirError

_log = logging.getLogger(__name__)

# Every client that holds a token. ``phone`` is the owner's phone glass, relayed by
# Galatea's server; ``dashboard`` is the owner's own page on this server; ``desktop`` is
# Pionir Desktop (it only reads today).
CLIENTS = ("crew", "galatea", "atani", "desktop", "dashboard", "phone")
TOKEN_PREFIX = "pionir-client-"
TOKEN_SUFFIX = ".token"
TOKEN_BYTES = 32
_TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_-]{43,256}")
ANONYMOUS = "anonymous"
SESSION_COOKIE = "pionir_session"
PROOF_HEADER = "X-Session-Proof"
SESSION_TTL = 12 * 3600.0
_MAX_SESSIONS = 64

# Routes a grant can name.
TASK = "task"          # POST /api/task
INTENT = "intent"      # POST /api/intent
ROUTE = "route"        # POST /api/route
APPROVE = "approve"    # POST /api/approvals/approve, /deny, /digest
ROUTES = frozenset({TASK, INTENT, ROUTE, APPROVE})

# Only the owner's own surfaces approve. Fixed in code: a grants file cannot add one.
APPROVERS = frozenset({"dashboard", "phone"})

# The hook for the one narrow pre-grant ever planned: the Daedalus sandbox work will let
# the crew run sandboxed solves without parking. It adds that permission HERE and to the
# crew's DEFAULT_GRANTS entry. Until then this is empty, so no client - and no grants
# file - can hold a privileged permission: every privileged action parks for the owner.
GRANTABLE_PERMISSIONS: frozenset[str] = frozenset()

COMPAT_ENV = "PIONIR_AUTH_COMPAT"
TOKEN_DIR_ENV = "PIONIR_CLIENT_TOKEN_DIR"
_OFF = {"0", "off", "false", "no"}


class Unauthenticated(PionirError):
    """The caller did not prove who it is, and what it asked needs that."""


@dataclass(frozen=True, slots=True)
class ClientGrant:
    """What one client may do without the owner: which routes, which capabilities
    (fnmatch patterns over capability names), and which permissions it holds. A
    privileged capability still parks unless its permissions are all in
    ``permissions`` - and ``GRANTABLE_PERMISSIONS`` keeps that set empty today."""

    routes: frozenset[str] = frozenset()
    capabilities: tuple[str, ...] = ()
    permissions: frozenset[str] = frozenset()

    def allows_capability(self, name: str) -> bool:
        return any(fnmatchcase(name, pattern) for pattern in self.capabilities)


# The code default: the same non-privileged set each client could reach before (every
# non-privileged capability; privileged ones park, as they did for every caller that
# sent no permission), bounded to the routes each actually uses.
DEFAULT_GRANTS: Mapping[str, ClientGrant] = {
    "crew": ClientGrant(frozenset({TASK}), ("*",)),
    "galatea": ClientGrant(frozenset({TASK, INTENT}), ("*",)),
    "atani": ClientGrant(frozenset({TASK}), ("*",)),
    "dashboard": ClientGrant(frozenset({TASK, INTENT, ROUTE, APPROVE}), ("*",)),
    "phone": ClientGrant(frozenset({APPROVE}), ()),
    "desktop": ClientGrant(frozenset(), ()),
}
# Served only while the compatibility window is open, and never anything privileged.
ANONYMOUS_GRANT = ClientGrant(frozenset({TASK, INTENT, ROUTE}), ("*",))


def _sha(value: str) -> str:
    import hashlib
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()


# ---- token files ------------------------------------------------------------------------
def token_path(directory: Path, client: str) -> Path:
    return Path(directory) / f"{TOKEN_PREFIX}{client}{TOKEN_SUFFIX}"


def read_token(path: Path | None) -> str | None:
    """A client's token from its file, or None (missing, unreadable, or malformed)."""
    if path is None:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text if _TOKEN_SHAPE.fullmatch(text) else None


def _write_new(path: Path, token: str) -> None:
    """Create the token file readable by this user only (as far as the OS allows)."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")


def ensure_tokens(directory: Path, clients: Iterable[str] = CLIENTS) -> dict[str, str]:
    """Every client's token, making any that is missing. A malformed file (empty, cut
    short) is replaced - it could never authenticate anyone - and said so."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    tokens: dict[str, str] = {}
    for client in clients:
        path = token_path(directory, client)
        token = read_token(path)
        if token is None:
            fresh = secrets.token_urlsafe(TOKEN_BYTES)
            try:
                _write_new(path, fresh)
            except FileExistsError:
                token = read_token(path)          # another process made it first
                if token is None:
                    _log.warning("client token %s was malformed; replacing it", path)
                    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
                    _write_new(tmp, fresh)
                    atomic.replace(tmp, path)
                    token = fresh
            else:
                _log.info("made the Pionir client token %s", path)
                token = fresh
        tokens[client] = token
    return tokens


def parse_bearer(header: str | None) -> tuple[bool, str | None]:
    """(present, token). A present but malformed header is (True, None)."""
    if header is None:
        return False, None
    scheme, _, token = header.strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return True, None
    return True, token


def cookie_value(header: str | None, name: str) -> str | None:
    for part in (header or "").split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key == name:
            return value.strip() or None
    return None


# ---- grants -----------------------------------------------------------------------------
def load_grants(path: Path | None) -> dict[str, ClientGrant]:
    """The code default, overridden per client by an optional JSON file::

        {"crew": {"capabilities": ["client.*", "crew.*"], "routes": ["task"]}}

    A file can narrow or re-shape a client's grant but never: add a client that holds
    no token, give ``approve`` to anyone but the owner's surfaces, or grant a permission
    outside ``GRANTABLE_PERMISSIONS``. Each such entry is dropped with a warning."""
    grants = dict(DEFAULT_GRANTS)
    if path is None or not Path(path).is_file():
        return grants
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        _log.warning("client grants %s unreadable (%s); using the code default", path, error)
        return grants
    if not isinstance(document, dict):
        _log.warning("client grants %s is not an object; using the code default", path)
        return grants
    for client, entry in document.items():
        if client not in CLIENTS or not isinstance(entry, dict):
            _log.warning("client grants: %r is not a known client; ignored", client)
            continue
        base = grants[client]
        routes = frozenset(str(r) for r in entry.get("routes", base.routes)) & ROUTES
        if APPROVE in routes and client not in APPROVERS:
            _log.warning("client grants: %s may not approve; dropped", client)
            routes -= {APPROVE}
        capabilities = tuple(str(c) for c in entry.get("capabilities", base.capabilities))
        asked = frozenset(str(p) for p in entry.get("permissions", base.permissions))
        refused = asked - GRANTABLE_PERMISSIONS
        if refused:
            _log.warning("client grants: %s may not hold %s; dropped", client, sorted(refused))
        grants[client] = ClientGrant(routes, capabilities, asked & GRANTABLE_PERMISSIONS)
    return grants


def compat_from_environment() -> bool:
    return os.environ.get(COMPAT_ENV, "on").strip().lower() not in _OFF


# ---- the authenticator ------------------------------------------------------------------
@dataclass
class ClientAuth:
    tokens: Mapping[str, str]
    grants: Mapping[str, ClientGrant] = field(default_factory=lambda: dict(DEFAULT_GRANTS))
    compat: bool = True
    _warned: dict[str, float] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # the owner's dashboard sessions: sha256(session id) -> (expires, sha256(proof)).
    # In memory: a restart signs the dashboard out (the launcher signs it in again).
    _sessions: dict[str, tuple[float, str]] = field(default_factory=dict, repr=False)
    _clock: Any = field(default=time.time, repr=False)

    def start_session(self) -> tuple[str, str]:
        """A new dashboard session: (its id, for the cookie; its proof, for the page)."""
        sid, proof = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = self._clock()
        with self._lock:
            self._sessions = {h: v for h, v in self._sessions.items() if v[0] > now}
            while len(self._sessions) >= _MAX_SESSIONS:
                self._sessions.pop(min(self._sessions, key=lambda h: self._sessions[h][0]))
            self._sessions[_sha(sid)] = (now + SESSION_TTL, _sha(proof))
        return sid, proof

    def end_session(self, sid: str | None) -> bool:
        if not sid:
            return False
        with self._lock:
            return self._sessions.pop(_sha(sid), None) is not None

    def session_ok(self, sid: str | None, proof: str | None) -> bool:
        """The cookie's session is live AND the proof is the one bound to it."""
        if not sid or not proof or len(sid) > 256 or len(proof) > 256:
            return False
        with self._lock:
            got = self._sessions.get(_sha(sid))
        return bool(got) and got[0] > self._clock() and hmac.compare_digest(got[1], _sha(proof))

    @classmethod
    def for_settings(cls, settings: Any) -> ClientAuth:
        directory = getattr(settings, "client_token_path", None)
        if directory is None:
            directory = Path(settings.state_root) / "secrets"
        grants_file = Path(settings.state_root) / "auth" / "grants.json"
        return cls(ensure_tokens(directory), load_grants(grants_file),
                   compat=compat_from_environment())

    def identify(self, token: str | None) -> str | None:
        """The client a token belongs to, or None. Constant time per comparison, and
        every client is compared - no early exit on a match."""
        if not token:
            return None
        presented = token.encode("utf-8", "replace")
        found = None
        for client, expected in self.tokens.items():
            if hmac.compare_digest(presented, expected.encode("utf-8")):
                found = client
        return found

    def from_headers(self, authorization: str | None, cookie: str | None,
                     proof: str | None = None) -> tuple[str | None, str | None]:
        """(client, refusal). ``(None, None)`` is a request that carried no credential.
        A session is the owner's dashboard only with its proof; the cookie alone is
        refused, as is a token in the cookie."""
        present, token = parse_bearer(authorization)
        if present:
            client = self.identify(token)
            return (client, None) if client else (None, "invalid bearer token")
        session = cookie_value(cookie, SESSION_COOKIE)
        if session is not None:
            if self.session_ok(session, proof):
                return "dashboard", None
            return None, "invalid session (sign in again from the launcher)"
        return None, None

    def grant(self, client: str) -> ClientGrant:
        if client == ANONYMOUS:
            return ANONYMOUS_GRANT if self.compat else ClientGrant()
        return self.grants.get(client, ClientGrant())

    def warn_anonymous(self, path: str, what: str = "") -> None:
        """Say, at most once a minute per path, that an unauthenticated call was served."""
        now = time.monotonic()
        with self._lock:
            last = self._warned.get(path)
            if last is not None and now - last < 60.0:
                return
            self._warned[path] = now
        _log.warning(
            "UNAUTHENTICATED call to %s%s served under %s=on: the caller sent no "
            "client token. Update it to send `Authorization: Bearer` from its "
            "pionir-client-<client>.token, then turn the window off.",
            path, f" ({what})" if what else "", COMPAT_ENV)
