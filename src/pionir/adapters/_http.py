"""A small loopback JSON client shared by Pionir's HTTP specialist adapters.

Daedalus and Melete are both standalone FastAPI services bound to loopback, so
the boundary to each is the same shape: a POST that hands over an intent and a
GET /health that names the model actually loaded. This is that shape, once,
rather than copied per adapter. Galatea predates it and keeps her own client -
her seam is asynchronous and deliberately different, so there is nothing to
share there and no reason to disturb working code.

Loopback only, by construction: an off-machine base url is refused, so a
misconfiguration cannot quietly send an intent - or a bearer token - to a
remote host. The token is optional and used only when the service was started
with one; on loopback these services accept unauthenticated calls.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from pionir.errors import (
    AdapterAuthenticationError,
    AdapterProtocolError,
    AdapterUnavailable,
)

MAX_RESPONSE_BYTES = 4_000_000


@dataclass(frozen=True, slots=True)
class LoopbackHttpSettings:
    """Connection details common to a loopback HTTP specialist."""

    base_url: str
    token: str = field(default="", repr=False)
    timeout_seconds: int = 180

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme != "http" or parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("a loopback HTTP specialist must use loopback HTTP")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("the specialist URL cannot carry credentials or query data")
        if self.timeout_seconds < 5:
            raise ValueError("the specialist timeout must be at least 5 seconds")


class LoopbackJsonClient:
    """stdlib JSON over loopback HTTP that ignores proxy configuration.

    Every call returns a JSON object or raises one of Pionir's adapter errors;
    it never returns a bare status code or a truncated body. The service's own
    error text, when it sends one, is preferred over the status number.
    """

    def __init__(self, label: str, settings: LoopbackHttpSettings) -> None:
        self._label = label
        self._base_url = settings.base_url.rstrip("/")
        self._token = settings.token.strip()
        self._timeout = settings.timeout_seconds
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _reason(self, error: urllib.error.HTTPError) -> str:
        try:
            document = json.loads(error.read(MAX_RESPONSE_BYTES).decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return ""
        if isinstance(document, dict):
            reason = document.get("detail") or document.get("error")
            if isinstance(reason, str) and reason.strip():
                return f": {reason.strip()}"
        return ""

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _open(self, request: urllib.request.Request) -> Mapping[str, Any]:
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            if error.code in {401, 403}:
                raise AdapterAuthenticationError(
                    f"{self._label} rejected Pionir's token ({error.code})"
                ) from error
            raise AdapterProtocolError(
                f"{self._label} answered HTTP {error.code}{self._reason(error)}"
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise AdapterUnavailable(
                f"{self._label} is unavailable at {self._base_url}"
            ) from error
        if len(raw) > MAX_RESPONSE_BYTES:
            raise AdapterProtocolError(f"{self._label}'s response exceeded the size limit")
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AdapterProtocolError(f"{self._label} returned invalid JSON") from error
        if not isinstance(document, dict):
            raise AdapterProtocolError(f"{self._label} returned a non-object response")
        return document

    def get(self, path: str) -> Mapping[str, Any]:
        return self._open(
            urllib.request.Request(
                f"{self._base_url}{path}", headers=self._headers(), method="GET"
            )
        )

    def post(self, path: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        body = json.dumps(dict(payload)).encode("utf-8")
        return self._open(
            urllib.request.Request(
                f"{self._base_url}{path}",
                data=body,
                headers=self._headers(),
                method="POST",
            )
        )


def health_model(client: LoopbackJsonClient) -> str | None:
    """The model a service's /health reports, or None if it cannot say.

    The id drives Pionir's VRAM admission discount, and these services are
    promoted like any other. Reading it live keeps a promotion from silently
    stopping the id from matching - the stale-constant scar the estate has paid
    for more than once. Failure is not an error: the service being down is the
    ordinary boot-time case and the adapter's declared default covers it.
    """

    try:
        document = client.get("/health")
    except (AdapterUnavailable, AdapterProtocolError, AdapterAuthenticationError):
        return None
    model = document.get("model")
    return model.strip() if isinstance(model, str) and model.strip() else None
