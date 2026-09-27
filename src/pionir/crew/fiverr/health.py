"""The uptime / SSL / domain worker: a health report for the buyer's website.

The checks are ours: our Site Intel API (``GET https://api.dokaz.net/v1/site/intel?url=``,
Scrooge ``products/site.ts`` - the redirect chain, the page's title and description, its
structured data, the server), a direct request to the site (up, status, response time, the
security headers), the site's TLS certificate (who issued it, when it expires, whether it
covers the name) and its DNS (the name and ``www.``). Every probe is injected: a test never
reaches a network.

**Never the owner's own network.** The brief names the site; a name that is not a public DNS
name (an IP address, ``localhost``, ``.local``, one of our own hosts) is refused before
anything is asked, and so is a name that RESOLVES to a private, loopback or reserved address.
The name is resolved ONCE; the page request and the certificate check connect to that pinned
address (safehttp.py: https only, no automatic redirects - each hop checked the same way), so
a name that answers public once and private the next, or a page that redirects inward, can
never make the report a way to probe the machine it runs on or its LAN.

The report is a fixed template around what was measured (``build_report``): each check says
OK, ATTENTION or PROBLEM, what was seen, and - for anything not OK - the step to fix it. It
states nothing it did not measure: a probe that could not run says so ("not checked").
"""
from __future__ import annotations

import http.client as http_client
import json
import re
import socket
import ssl
import time
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

from ..net import HttpUnreachable
from . import safehttp

SITE_INTEL = "https://api.dokaz.net/v1/site/intel?url="
CERT_WARN_DAYS = 21
SLOW_MS = 1500
SECURITY_HEADERS = (
    ("strict-transport-security", "Strict-Transport-Security (HSTS)",
     "Turn on HSTS at your host or CDN (for example `max-age=31536000`) so browsers always "
     "use https."),
    ("x-content-type-options", "X-Content-Type-Options",
     "Add the header `X-Content-Type-Options: nosniff` in your host's header settings."),
    ("content-security-policy", "Content-Security-Policy",
     "Consider a Content-Security-Policy; start in report-only mode so nothing breaks."),
    ("referrer-policy", "Referrer-Policy",
     "Add `Referrer-Policy: strict-origin-when-cross-origin`."),
)
_FIND = re.compile(r"(?i)\b(?:https?://)?((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
                   r"[a-z]{2,63})(?::\d+)?(?:/[^\s]*)?")


class HealthProblem(ValueError):
    """Why the report cannot be made (no public site named), for the owner."""


def site_of(brief: str) -> str:
    """The one host the brief names (the first that is a public DNS name), or
    HealthProblem."""
    for m in _FIND.finditer(brief or ""):
        host = m.group(1).lower().rstrip(".")
        if "@" in (brief or "")[max(0, m.start() - 1):m.start()]:
            continue                        # the domain of an email address, not a site
        why = host_problem(host)
        if why is None:
            return host
    raise HealthProblem("the brief names no public website (a domain like example.co.uk)")


host_problem = safehttp.host_problem          # public DNS names only, never our own hosts
private_addresses = safehttp.public_only


# ---- the probes (real ones; tests inject fakes) -----------------------------------------------
def dns_probe(host: str) -> list:
    return sorted({info[4][0] for info in socket.getaddrinfo(host, 443,
                                                              proto=socket.IPPROTO_TCP)})


def tls_probe(host: str, ip: str, timeout: float = 10.0) -> dict:
    """The certificate the site presents at the PINNED address ``ip`` (never resolved again
    here), verified against the system's trust store for ``host``."""
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((ip, 443), timeout=timeout) as sock, \
                ctx.wrap_socket(sock, server_hostname=host) as tls:
            cert = tls.getpeercert()
            version = tls.version()
    except ssl.SSLCertVerificationError as exc:
        return {"trusted": False, "error": str(exc.verify_message or exc)[:200]}
    issuer = dict(x[0] for x in cert.get("issuer", ()))
    return {"trusted": True, "version": version,
            "not_after": ssl.cert_time_to_seconds(cert["notAfter"]),
            "issuer": issuer.get("organizationName") or issuer.get("commonName") or "",
            "names": [v for k, v in cert.get("subjectAltName", ()) if k == "DNS"]}


# ---- the checks -----------------------------------------------------------------------------
@dataclass
class Check:
    name: str
    verdict: str                # OK | ATTENTION | PROBLEM | NOT CHECKED
    seen: str
    fix: str = ""


@dataclass
class Health:
    host: str
    checks: list = field(default_factory=list)
    title: str | None = None

    def add(self, *args) -> None:
        self.checks.append(Check(*args))

    @property
    def problems(self) -> int:
        return sum(1 for c in self.checks if c.verdict == "PROBLEM")

    @property
    def attention(self) -> int:
        return sum(1 for c in self.checks if c.verdict == "ATTENTION")


def _covers(names: list, host: str) -> bool:
    for n in names:
        n = n.lower()
        if n == host or (n.startswith("*.") and host.endswith(n[1:])
                         and host.count(".") == n.count(".")):
            return True
    return False


def run_checks(host: str, *, http, fetch: safehttp.SafeHttp | None = None, dns=dns_probe,
               tls=tls_probe, now: float | None = None,
               intel_url: str = SITE_INTEL) -> Health:
    """Every check on ``host``. HealthProblem when the name or its addresses are not public
    (nothing else is asked then)."""
    now = time.time() if now is None else now
    why = host_problem(host)
    if why:
        raise HealthProblem(f"{host}: {why}")
    h = Health(host)
    try:
        ips = dns(host)
    except OSError as exc:
        h.add("DNS", "PROBLEM", f"{host} does not resolve ({type(exc).__name__})",
              "Check the domain's DNS records at your registrar: an A (or AAAA/CNAME) record "
              "must point at your host.")
        return h
    bad = private_addresses(ips)
    if bad:
        raise HealthProblem(f"{host} resolves to a private or reserved address; it is not "
                            "checked")
    if not ips:
        raise HealthProblem(f"{host} resolves to nothing; it is not checked")
    pinned = ips[0]                 # the ONE address every connection below goes to
    fetch = fetch or safehttp.SafeHttp()
    h.add("DNS", "OK", f"{host} resolves ({len(ips)} address{'es' if len(ips) != 1 else ''})")
    www = host if host.startswith("www.") else f"www.{host}"
    other = host[4:] if host.startswith("www.") else www
    try:
        if private_addresses(dns(other)):
            h.add("DNS (other name)", "ATTENTION", f"{other} points at a private address",
                  f"Point {other} at your host, or remove the record.")
        else:
            h.add("DNS (other name)", "OK", f"{other} resolves too")
    except OSError:
        h.add("DNS (other name)", "ATTENTION", f"{other} does not resolve",
              f"Add a record for {other} (a CNAME to {host} is usual) so both names work.")
    # the site itself
    resp = None
    try:
        resp = fetch.get(f"https://{host}/", timeout=20.0, first_ip=pinned)
    except safehttp.Refused as exc:
        h.add("Uptime (https)", "ATTENTION", f"not followed: {str(exc)[:100]}",
              "The home page redirects somewhere a visitor should not be sent (another port, "
              "plain http or a private address): check the redirect rules at your host.")
    except (OSError, ssl.SSLError, ValueError, http_client.HTTPException) as exc:
        h.add("Uptime (https)", "PROBLEM", f"https://{host}/ did not answer "
              f"({type(exc).__name__})", "Check that the site is up at your host and that it "
              "serves https.")
    if resp is not None:
        up = 200 <= resp.status < 400
        h.add("Uptime (https)", "OK" if up else "PROBLEM",
              f"answered HTTP {resp.status}", "" if up else
              "The home page answers with an error: check the host's error log.")
        ms = round(resp.elapsed_ms)
        h.add("Response time", "OK" if ms <= SLOW_MS else "ATTENTION", f"{ms:,} ms",
              "" if ms <= SLOW_MS else "Slow: enable caching or a CDN, and compress images.")
        headers = {str(k).lower(): v for k, v in (resp.headers or {}).items()}
        for key, label, fix in SECURITY_HEADERS:
            present = key in headers
            h.add(f"Header: {label}", "OK" if present else "ATTENTION",
                  "present" if present else "missing", "" if present else fix)
    # the certificate
    try:
        cert = tls(host, pinned)
    except (OSError, ssl.SSLError, ValueError) as exc:
        cert = None
        h.add("SSL certificate", "PROBLEM", f"no TLS connection ({type(exc).__name__})",
              "Install a certificate (most hosts offer a free Let's Encrypt one) and make sure "
              "port 443 is open.")
    if cert is not None:
        if not cert.get("trusted"):
            h.add("SSL certificate", "PROBLEM", f"not trusted: {cert.get('error', '')[:120]}",
                  "Replace it with a certificate from a trusted authority (Let's Encrypt is "
                  "free) that covers this name.")
        else:
            days = int((float(cert["not_after"]) - now) // 86400)
            verdict = "PROBLEM" if days < 0 else "ATTENTION" if days < CERT_WARN_DAYS else "OK"
            h.add("SSL certificate", verdict,
                  f"issued by {cert.get('issuer') or 'an unnamed authority'}; "
                  f"{'expired' if days < 0 else f'expires in {days} days'}",
                  "" if verdict == "OK" else "Renew it now and turn on auto-renewal at your "
                  "host.")
            names = cert.get("names") or []
            if names and not _covers(names, host):
                h.add("SSL name match", "PROBLEM", f"the certificate does not cover {host}",
                      f"Reissue the certificate to include {host}.")
            elif names:
                h.add("SSL name match", "OK", f"covers {host}")
    # our Site Intel: the redirect from http, and the page basics
    try:
        r = http.get(intel_url + quote(f"http://{host}/", safe=""), timeout=30.0)
        doc = json.loads(r.body.decode("utf-8")) if r.status == 200 else None
    except (HttpUnreachable, ValueError, UnicodeDecodeError):
        doc = None
    if not isinstance(doc, dict):
        h.add("http -> https redirect", "NOT CHECKED", "our site check did not answer")
        return h
    final = str(doc.get("final_url") or "")
    to_https = urlsplit(final).scheme == "https"
    h.add("http -> https redirect", "OK" if to_https else "PROBLEM",
          "http:// sends visitors to https://" if to_https else "http:// does not redirect to "
          "https://", "" if to_https else "Turn on 'always use https' (or a 301 redirect from "
          "http to https) at your host or CDN.")
    title = doc.get("title")
    h.title = title if isinstance(title, str) else None
    h.add("Page title", "OK" if title else "ATTENTION", "present" if title else "missing",
          "" if title else "Give the home page a <title> with your business name and what "
                           "you do.")
    desc = doc.get("description")
    h.add("Meta description", "OK" if desc else "ATTENTION", "present" if desc else "missing",
          "" if desc else "Add a meta description (one or two sentences) for search results.")
    types = [t for t in doc.get("schema_org_types") or [] if isinstance(t, str)]
    h.add("Structured data", "OK" if types else "ATTENTION",
          f"structured data types: {', '.join(types[:6])}" if types else "none found",
          "" if types else "Add LocalBusiness structured data so search engines "
                           "and AI assistants can read your name, address and hours.")
    return h


def build_report(h: Health) -> str:
    lines = [f"# Website health report: {h.host}", "",
             f"Checks: {len(h.checks)} - problems: {h.problems}, needs attention: "
             f"{h.attention}.", "",
             "| Check | Result | What was seen |", "|---|---|---|"]
    for c in h.checks:
        lines.append(f"| {c.name} | {c.verdict} | {c.seen.replace('|', '/')} |")
    fixes = [c for c in h.checks if c.fix]
    lines += ["", "## Step-by-step fixes", ""]
    if fixes:
        lines += [f"{i}. **{c.name}** - {c.fix}" for i, c in enumerate(fixes, 1)]
    else:
        lines.append("Nothing needs fixing: every check passed.")
    lines += ["", "## Keeping it healthy", "",
              "- Keep certificate auto-renewal on at your host, and check this report again "
              "before the certificate's expiry date.",
              "- Use an uptime monitor that checks the site every few minutes and alerts you "
              "when it goes down or the certificate is close to expiring.",
              "- Renew the domain itself before it lapses: turn on auto-renew at your "
              "registrar.", "",
              "Every result above was measured when this report was made; nothing in it is "
              "estimated.", ""]
    return "\n".join(lines)
