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
  readable by the user. A client sends ``Authorization: Bearer <token>``; the owner's
  dashboard holds the dashboard token as an HttpOnly session cookie instead. Tokens are
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

# Routes a grant can name.
TASK = "task"          # POST /api/task
INTENT = "intent"      # POST /api/intent
ROUTE = "route"        # POST /api/route
APPROVE = "approve"    # POST /api/approvals/approve, /deny, /digest
ROUTES = frozenset({TASK, INTENT, ROUTE, APPROVE})

# Only the owner's own surfaces approve. Fixed in code: a grants file cannot add one.
APPROVERS = frozenset({"dashboard", "phone"})

# The one narrow pre-grant: the crew's Builds division may run Daedalus in its SANDBOX
# without parking. A grantable permission is scoped twice, here and in code only: to the
# ONE client that may hold it, and to the ONE capability it unlocks. ``coding.daedalus_build``
# itself reaches nothing but a sandbox repo the Builds worker made (adapters/daedalus.py),
# running as the contained ``pionir-builds`` user. No other permission is grantable, so every
# other privileged action from every client still parks for the owner.
PERMISSION_SCOPES: Mapping[str, tuple[frozenset[str], frozenset[str]]] = {
    "daedalus.build_sandbox": (frozenset({"crew"}), frozenset({"coding.daedalus_build"})),
}
GRANTABLE_PERMISSIONS: frozenset[str] = frozenset(PERMISSION_SCOPES)


def scoped(permissions: Iterable[str], client: str, capability: str | None = None
           ) -> frozenset[str]:
    """The permissions this client may hold (and, given a capability, the ones that
    unlock THAT capability): every other one is dropped."""
    out = set()
    for permission in permissions:
        scope = PERMISSION_SCOPES.get(permission)
        if scope is None or client not in scope[0]:
            continue
        if capability is not None and capability not in scope[1]:
            continue
        out.add(permission)
    return frozenset(out)

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

    def permissions_for(self, capability: str | None) -> frozenset[str]:
        """The permissions this grant holds that unlock ``capability`` - never one that is
        scoped to another capability (PERMISSION_SCOPES)."""
        out = set()
        for permission in self.permissions:
            scope = PERMISSION_SCOPES.get(permission)
            if scope is not None and capability in scope[1]:
                out.add(permission)
        return frozenset(out)


# The code default: the same non-privileged set each client could reach before (every
# non-privileged capability; privileged ones park, as they did for every caller that
# sent no permission), bounded to the routes each actually uses.
DEFAULT_GRANTS: Mapping[str, ClientGrant] = {
    # the crew's one privileged permission: Daedalus builds in the sandbox, nothing else
    "crew": ClientGrant(frozenset({TASK}), ("*",), frozenset({"daedalus.build_sandbox"})),
    "galatea": ClientGrant(frozenset({TASK, INTENT}), ("*",)),
    "atani": ClientGrant(frozenset({TASK}), ("*",)),
    "dashboard": ClientGrant(frozenset({TASK, INTENT, ROUTE, APPROVE}), ("*",)),
    "phone": ClientGrant(frozenset({APPROVE}), ()),
    "desktop": ClientGrant(frozenset(), ()),
}
# Served only while the compatibility window is open, and never anything privileged.
ANONYMOUS_GRANT = ClientGrant(frozenset({TASK, INTENT, ROUTE}), ("*",))


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
        allowed = scoped(asked, client)
        refused = asked - allowed
        if refused:
            _log.warning("client grants: %s may not hold %s; dropped", client, sorted(refused))
        grants[client] = ClientGrant(routes, capabilities, allowed)
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

    def from_headers(self, authorization: str | None, cookie: str | None
                     ) -> tuple[str | None, str | None]:
        """(client, refusal). ``(None, None)`` is a request that carried no credential."""
        present, token = parse_bearer(authorization)
        if present:
            client = self.identify(token)
            return (client, None) if client else (None, "invalid bearer token")
        session = cookie_value(cookie, SESSION_COOKIE)
        if session is not None:
            # the session cookie only ever carries the owner's dashboard token
            if self.identify(session) == "dashboard":
                return "dashboard", None
            return None, "invalid session"
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
