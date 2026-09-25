"""The one way a worker reaches the network, injected so tests use fakes.

``Http.get`` answers an ``HttpResponse`` for ANY status the server sent (a 401 or a 503
is an answer, and the worker decides what it means) and raises ``HttpUnreachable`` only
when no answer came back at all. GET only: nothing a worker reads with this can change
anything at the other end.
"""
from __future__ import annotations

import http.client
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol

MAX_BYTES = 2_000_000
USER_AGENT = "pionir-crew/1"


class HttpUnreachable(RuntimeError):
    """No usable answer came back: refused, timed out, DNS, TLS."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    elapsed_ms: float
    headers: dict = field(default_factory=dict)


class Http(Protocol):
    def get(self, url: str, *, headers: dict | None = None,
            timeout: float = 20.0) -> HttpResponse: ...


class UrllibHttp:
    """The real client. Stdlib only; never follows a redirect to somewhere unexpected
    silently - urllib does follow redirects, and the final status is what is reported."""

    def get(self, url: str, *, headers: dict | None = None,
            timeout: float = 20.0) -> HttpResponse:
        req = urllib.request.Request(url, method="GET",
                                     headers={"User-Agent": USER_AGENT, **(headers or {})})
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read(MAX_BYTES)
                return HttpResponse(r.status, body, (time.monotonic() - t0) * 1000,
                                    dict(r.headers.items()))
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read(MAX_BYTES)
            except OSError:
                body = b""
            return HttpResponse(exc.code, body, (time.monotonic() - t0) * 1000,
                                dict(exc.headers.items()) if exc.headers else {})
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            raise HttpUnreachable(f"{type(exc).__name__}: {exc}") from exc
