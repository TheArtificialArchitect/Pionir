"""Fetching a URL a BUYER named, without letting it reach anything it should not.

A buyer's brief names a website (the health report checks it; a research report's shop
links are opened to verify them). A plain HTTP client would resolve the name, follow any
redirect and resolve again - so a name that answers public once and private the next time,
or a public page that redirects to ``http://127.0.0.1:8780/``, would reach the owner's own
machine or LAN. So here:

- **resolve once**, and every address must be a public internet address
  (``ipaddress.is_global``, not multicast); a host of our own (``OWN_DOMAINS``) or a name
  that is not a public DNS name is refused before anything is resolved;
- **connect to that pinned address**, with the name only as the TLS server name (SNI and
  certificate check) and the ``Host`` header - nothing resolves it again;
- **https only, port 443 only**, and **no automatic redirects**: each ``Location`` is
  followed by hand (at most ``MAX_REDIRECTS``), and every hop passes the same checks;
- the body is read up to ``MAX_BODY`` bytes; the whole answer is bounded in time.

The two outside pieces - ``resolve`` and ``open`` - are injected, so a test never reaches
a network.
"""
from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

MAX_REDIRECTS = 3
MAX_BODY = 1_500_000
USER_AGENT = "Mozilla/5.0 (compatible; DokazCheck/1.0)"
OWN_DOMAINS = ("dokaz.net", "dokazindustries.com", "localhost")
_HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
_NOT_PUBLIC = (".localhost", ".local", ".internal", ".lan", ".home", ".arpa", ".test",
               ".invalid", ".example", ".onion", ".corp", ".intranet", ".home.arpa")


class Refused(ValueError):
    """The URL (or a hop it redirected to) may not be fetched: the reason, in words."""


@dataclass
class Answer:
    status: int
    headers: dict = field(default_factory=dict)     # lower-case names
    body: bytes = b""
    url: str = ""                                    # the last hop's URL
    hops: list = field(default_factory=list)         # every URL visited, in order
    elapsed_ms: float = 0.0


def host_problem(host: str) -> str | None:
    """Why this host may never be fetched, or None."""
    host = (host or "").lower().rstrip(".")
    try:
        ipaddress.ip_address(host.strip("[]"))
        return "it is an IP address, not a domain name"
    except ValueError:
        pass
    if not _HOST.fullmatch(host) or host.endswith(_NOT_PUBLIC):
        return "it is not a public domain name"
    for own in OWN_DOMAINS:
        if host == own or host.endswith("." + own):
            return "it is one of our own hosts"
    return None


def public_only(ips) -> list:
    """The addresses that are NOT public internet addresses (empty: all public)."""
    bad = []
    for ip in ips:
        try:
            addr = ipaddress.ip_address(str(ip).split("%", 1)[0])
        except ValueError:
            bad.append(str(ip))
            continue
        if not addr.is_global or addr.is_multicast:
            bad.append(str(ip))
    return bad


def dns_resolve(host: str) -> list:
    return sorted({info[4][0] for info in socket.getaddrinfo(host, 443,
                                                              proto=socket.IPPROTO_TCP)})


class _Pinned(http.client.HTTPSConnection):
    """An https connection to a PINNED address, with ``host`` as the TLS server name and in
    the Host header. Nothing here resolves ``host``."""

    def __init__(self, host: str, ip: str, timeout: float) -> None:
        super().__init__(host, 443, timeout=timeout,
                         context=ssl.create_default_context())
        self._pinned_ip = ip

    def connect(self) -> None:
        sock = socket.create_connection((self._pinned_ip, 443), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def pinned_open(ip: str, host: str, target: str, timeout: float) -> tuple:
    """One GET over https to ``ip`` as ``host``: (status, headers, body). No redirect is
    followed here."""
    conn = _Pinned(host, ip, timeout)
    try:
        conn.request("GET", target, headers={"Host": host, "User-Agent": USER_AGENT,
                                             "Accept": "text/html,*/*;q=0.5"})
        resp = conn.getresponse()
        body = resp.read(MAX_BODY)
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, body
    finally:
        conn.close()


class SafeHttp:
    """GET a buyer-named https URL through the rules above."""

    def __init__(self, *, resolve: Callable[[str], list] = dns_resolve,
                 open: Callable[..., tuple] = pinned_open) -> None:  # noqa: A002
        self.resolve = resolve
        self.open = open

    def check(self, url: str) -> tuple:
        """``(host, target)`` for a URL that may be fetched, or Refused."""
        try:
            parts = urlsplit(url)
        except ValueError as exc:
            raise Refused(f"{url[:80]!r} cannot be read") from exc
        if parts.scheme != "https":
            raise Refused(f"{url[:80]!r} is not https")
        if parts.username or parts.password:
            raise Refused(f"{url[:80]!r} carries a login")
        try:
            port = parts.port
        except ValueError as exc:
            raise Refused(f"{url[:80]!r} has a bad port") from exc
        if port not in (None, 443):
            raise Refused(f"{url[:80]!r} names a port")
        host = (parts.hostname or "").lower().rstrip(".")
        why = host_problem(host)
        if why:
            raise Refused(f"{host or url[:60]!r}: {why}")
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        return host, target

    def pin(self, host: str, first_ip: str | None = None) -> str:
        """One public address for ``host``, resolved once here (or the one given, checked)."""
        if first_ip is not None:
            ips = [first_ip]
        else:
            try:
                ips = self.resolve(host)
            except OSError as exc:
                raise Refused(f"{host} does not resolve ({type(exc).__name__})") from exc
        if not ips:
            raise Refused(f"{host} does not resolve")
        bad = public_only(ips)
        if bad:
            raise Refused(f"{host} resolves to a private or reserved address; not fetched")
        return ips[0]

    def get(self, url: str, *, timeout: float = 20.0, first_ip: str | None = None,
            max_redirects: int = MAX_REDIRECTS) -> Answer:
        """The answer, following at most ``max_redirects`` redirects, each hop checked and
        pinned. Refused for a URL or hop that breaks a rule; OSError (and ssl/http errors)
        when nothing answered."""
        t0 = time.monotonic()
        hops: list = []
        current = url
        for n in range(max_redirects + 1):
            host, target = self.check(current)
            ip = self.pin(host, first_ip if n == 0 else None)
            hops.append(current)
            status, headers, body = self.open(ip, host, target, timeout)
            headers = {str(k).lower(): v for k, v in (headers or {}).items()}
            if status in (301, 302, 303, 307, 308) and headers.get("location"):
                current = urljoin(current, str(headers["location"]))
                continue
            return Answer(status, headers, body[:MAX_BODY], current, hops,
                          (time.monotonic() - t0) * 1000)
        raise Refused(f"more than {max_redirects} redirects from {url[:80]!r}")
