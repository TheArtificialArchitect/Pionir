"""Bounded JSON reads from independently managed loopback services."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from pionir.errors import AdapterProtocolError, AdapterUnavailable

MAX_RESPONSE_BYTES = 1_000_000


def validate_loopback_url(base_url: str, *, service: str) -> str:
    parsed = urlparse(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError(f"{service} must use loopback HTTP")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"{service} URL cannot contain credentials or query data")
    if parsed.path not in {"", "/"}:
        raise ValueError(f"{service} URL cannot contain a path")
    return base_url.rstrip("/")


class LoopbackJsonReader:
    """GET JSON without inheriting system proxy settings."""

    def __init__(self, base_url: str, *, timeout_seconds: int, service: str) -> None:
        self._base_url = validate_loopback_url(base_url, service=service)
        self._timeout_seconds = timeout_seconds
        self._service = service
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(self, path: str) -> Mapping[str, Any]:
        if not path.startswith("/") or "?" in path or "#" in path:
            raise ValueError("adapter paths must be absolute and contain no query data")
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            with self._opener.open(request, timeout=self._timeout_seconds) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            raise AdapterProtocolError(
                f"{self._service} rejected the status request with HTTP {error.code}"
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise AdapterUnavailable(
                f"{self._service} is unavailable at {self._base_url}"
            ) from error
        if len(raw) > MAX_RESPONSE_BYTES:
            raise AdapterProtocolError(
                f"{self._service} response exceeded the size limit"
            )
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AdapterProtocolError(
                f"{self._service} returned invalid JSON"
            ) from error
        if not isinstance(document, dict):
            raise AdapterProtocolError(
                f"{self._service} returned a non-object response"
            )
        return document
