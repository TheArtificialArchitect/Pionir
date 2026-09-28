"""Signing the owner's browser in without a long-lived key in any URL.

A URL lands in browser history, in logs, on a process command line. So no launcher
opens ``/?key=<dashboard token>`` any more: it opens ``/?code=<one-time code>`` -
random, good for 60 seconds, used once - which Pionir trades for its HttpOnly,
SameSite=Strict session cookie (server.py ``_sign_in``).

Who mints a code:

- Pionir itself, in-process, for the browser it opens on start.
- Another launcher (pionir.ps1, Pionir Desktop) by ``POST /api/signin_code`` signed
  with the dashboard token - HMAC-SHA256 over ``signin|<nonce>|<unix time>|<port>`` -
  never sending the token. Pionir answers the code with a proof signed the same way
  (``signin-reply|<nonce>|<code>|<port>``), so the launcher knows it reached Pionir
  and not a squatter on its port before it opens anything. A nonce is good once, a
  request only within a minute of its time and only for the port it names.

The same shape, the other way round, gets the dashboard's Voice view a sign-in
ticket from Galatea (``galatea_ticket``): signed with her glass key, and her answer
proven with it, so whatever else might hold her port learns nothing and is handed
nothing.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from urllib.parse import urlparse

CODE_TTL = 60.0
SIGN_WINDOW = 60.0
_MAX_LIVE = 64


def sign(key: str, *parts: object) -> str:
    """HMAC-SHA256 over the parts joined by '|', as hex. The first part names the
    purpose, so a signature for one thing is never valid for another."""
    message = "|".join(str(part) for part in parts).encode("utf-8")
    return hmac.new(str(key).encode("utf-8"), message, hashlib.sha256).hexdigest()


def same(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return hmac.compare_digest(str(a).encode("utf-8", "replace"), str(b).encode("utf-8", "replace"))


def _h(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()


class SigninCodes:
    """One-time sign-in codes, in memory: a restart voids every one."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._live: dict[str, float] = {}
        self._nonces: dict[str, float] = {}

    def mint(self) -> str:
        code = secrets.token_urlsafe(24)
        now = self._clock()
        with self._lock:
            self._live = {h: exp for h, exp in self._live.items() if exp > now}
            while len(self._live) >= _MAX_LIVE:
                self._live.pop(next(iter(self._live)))
            self._live[_h(code)] = now + CODE_TTL
        return code

    def redeem(self, code: str | None) -> bool:
        """True once for a live code; it is gone after this whatever the answer."""
        if not code or len(code) > 256:
            return False
        with self._lock:
            exp = self._live.pop(_h(code), None)
        return exp is not None and exp > self._clock()

    def signed_mint(self, key: str | None, nonce: object, ts: object, sig: str | None,
                    port: int) -> dict[str, str] | None:
        """A code for a launcher that signed its ask with ``key``, with the proof;
        None for anything else (no key, a bad or stale or replayed signature)."""
        if not key or not isinstance(nonce, str) or not 16 <= len(nonce) <= 128:
            return None
        try:
            ts = int(ts)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        now = self._clock()
        if abs(now - ts) > SIGN_WINDOW or not same(sig, sign(key, "signin", nonce, ts, port)):
            return None
        with self._lock:
            self._nonces = {n: exp for n, exp in self._nonces.items() if exp > now}
            if nonce in self._nonces:
                return None
            self._nonces[nonce] = now + 2 * SIGN_WINDOW
        code = self.mint()
        return {"code": code, "proof": sign(key, "signin-reply", nonce, code, port)}


class NotGalatea(Exception):
    """What holds her port did not prove it is her - or could not be asked."""


def galatea_ticket(base_url: str, glass_key: str, *, opener=None,
                   clock: Callable[[], float] = time.time, timeout: float = 5.0) -> str:
    """A one-time sign-in ticket from Galatea for the Voice view, or NotGalatea.

    Asked with an HMAC of her glass key (never the key), over a fresh nonce, the
    time and the port it is meant for; accepted only with her proof over the nonce
    and the ticket, signed with the same key."""
    port = urlparse(base_url).port or 80
    nonce = secrets.token_hex(16)
    ts = int(clock())
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/ticket",
        data=json.dumps({"nonce": nonce, "ts": ts}).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "X-Galatea-Sign": sign(glass_key, "ticket", nonce, ts, port)},
        method="POST")
    open_ = opener or urllib.request.build_opener(urllib.request.ProxyHandler({})).open
    try:
        with open_(request, timeout=timeout) as response:
            document = json.loads(response.read(65536).decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise NotGalatea(f"her port refused the ask (HTTP {error.code})") from error
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise NotGalatea("nothing answering as her on her port") from error
    ticket = document.get("ticket") if isinstance(document, dict) else None
    proof = document.get("proof") if isinstance(document, dict) else None
    if not isinstance(ticket, str) or not ticket or len(ticket) > 256 or not same(
            proof, sign(glass_key, "ticket-reply", nonce, ticket, port)):
        raise NotGalatea("what answers on her port could not prove it is her")
    return ticket
