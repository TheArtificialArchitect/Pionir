"""The workers themselves: the real ones and honest placeholders.

None of these holds a model or imports the brain. Each is built from a catalogue entry
(catalogue.json -> registry.py) by the factory named in its ``impl``; ``IMPLS`` is the
whole list, and a name not in it fails loudly at load time.

- ``placeholder`` - a job that has not been built yet. Every run answers
  ``Err(not_wired)``: it never fakes output, never succeeds, and is reported as such.
- ``scrooge_ledger`` - REAL. Reads Scrooge's ledger read-only (``GET /dash/api.json``
  with the read token from ``scrooge-read-token.txt`` in the secrets dir). No token file
  is ``Err(not_configured)``, loudly - never a fake zero. Revenue is recorded as typed
  figures in ``usd_cents``, exactly as Scrooge keeps it.
- ``http_health`` - REAL. ``GET``s a health URL and records up/down and latency.
- ``blog_writer`` - REAL (blog.py). Drafts a blog post through the shared brain, checks
  it with contentcheck.py, and submits it for the owner's approval. The one worker here
  that gets words; it gets them only through ``ctx.words``.
- ``instagram_writer`` - REAL (instagram.py). The same for an Instagram card post: checked
  with ``contentcheck.check_social``, submitted as ``social.instagram_post``.
"""
from __future__ import annotations

import json

from .figures import Figure
from .net import HttpUnreachable
from .result import Err, Ok, Result
from .worker import ErrorKind, WorkContext, WorkerError, make_output, never_raises


class _Base:
    live = True

    def __init__(self, spec) -> None:
        self.worker_id = spec.worker_id
        self.name = spec.name
        self.division = spec.division
        self.kind = spec.kind
        self.cadence_seconds = int(spec.cadence_seconds)
        self.provider = spec.provider
        self.stage = int(spec.stage)
        self.entities = tuple(spec.entities)
        self.note = spec.note

    def readiness(self, secrets_dir) -> str | None:
        """Why this worker cannot do its job yet, or None if nothing is known missing."""
        return None

    def _err(self, kind: ErrorKind, message: str, retryable: bool = True) -> Err:
        return Err(WorkerError(self.worker_id, kind, message, retryable))

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<{type(self).__name__} {self.worker_id}>"


class Placeholder(_Base):
    """A job not built yet. Says so on every run; never produces a row."""

    live = False

    def readiness(self, secrets_dir) -> str | None:
        return f"not wired yet{': ' + self.note if self.note else ''}"

    @never_raises()
    def run(self, ctx: WorkContext) -> Result:
        return self._err(ErrorKind.NOT_WIRED, self.readiness(ctx.secrets_dir) or "not wired yet",
                         retryable=False)


def _status_error(worker: _Base, status: int, body: bytes) -> Err:
    snippet = body[:160].decode("utf-8", "replace")
    if status in (401, 403):
        return worker._err(ErrorKind.AUTH, f"HTTP {status}: the token was refused ({snippet})",
                           retryable=False)
    if status == 429:
        return worker._err(ErrorKind.RATE_LIMITED, f"HTTP 429: {snippet}")
    return worker._err(ErrorKind.HTTP_ERROR, f"HTTP {status}: {snippet}")


def _cents(obj: dict, key: str) -> int:
    """A cents field, or MALFORMED. Never a default: a missing number is not a zero."""
    v = obj.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not float(v).is_integer():
        raise _Malformed(f"summary.{key} is {v!r}, not whole cents")
    return int(v)


class _Malformed(ValueError):
    pass


class LedgerWorker(_Base):
    """Scrooge's ledger, read-only. The read token can reach only GET endpoints on an
    allowlist (Scrooge's ``READ_PATHS``); it cannot approve or send anything."""

    def __init__(self, spec, *, url: str, token_file: str) -> None:
        super().__init__(spec)
        self.url = url
        self.token_file = token_file

    def _token_path(self, secrets_dir):
        return secrets_dir / self.token_file

    def readiness(self, secrets_dir) -> str | None:
        path = self._token_path(secrets_dir)
        if not path.is_file():
            return (f"NOT CONFIGURED: no read token at {path} (Scrooge's "
                    f"tools/setup-read-token.ps1 writes it)")
        return None

    @never_raises()
    def run(self, ctx: WorkContext) -> Result:
        path = self._token_path(ctx.secrets_dir)
        if not path.is_file():
            return self._err(ErrorKind.NOT_CONFIGURED,
                             f"no read token at {path}; revenue is UNKNOWN, not zero. Run "
                             f"Scrooge's tools/setup-read-token.ps1 to create it",
                             retryable=False)
        token = path.read_text(encoding="utf-8").strip()
        if not token:
            return self._err(ErrorKind.NOT_CONFIGURED,
                             f"the read token at {path} is empty; revenue is UNKNOWN, not zero",
                             retryable=False)
        try:
            resp = ctx.http.get(self.url, headers={"x-dash-token": token}, timeout=20.0)
        except HttpUnreachable as exc:
            return self._err(ErrorKind.UNAVAILABLE, f"{self.url}: {exc}")
        if resp.status != 200:
            return _status_error(self, resp.status, resp.body)
        try:
            doc = json.loads(resp.body.decode("utf-8"))
            summary = doc["summary"] if isinstance(doc, dict) else None
            if not isinstance(summary, dict):
                raise _Malformed("no summary object in the answer")
            figures = self._figures(summary)
        except (TypeError, ValueError, KeyError, UnicodeDecodeError) as exc:
            return self._err(ErrorKind.MALFORMED, f"{type(exc).__name__}: {exc}")
        streams = sorted({f.stream for f in figures if f.stream and f.stream != "all"})
        return Ok((make_output(
            self, valid_at=ctx.now, observed_at=ctx.now,
            payload={"streams": streams},
            figures=figures, entities=(*self.entities, *streams),
            provenance={"source": "real", "provider": self.provider, "url": self.url,
                        "status": resp.status, "latency_ms": round(resp.elapsed_ms)},
        ),))

    @staticmethod
    def _figures(s: dict) -> list:
        figs = [
            Figure(_cents(s, "all_time_net"), "usd_cents", "revenue", "all", "all_time"),
            Figure(_cents(s, "mtd_net"), "usd_cents", "revenue", "all", "mtd"),
            Figure(_cents(s, "last30_net"), "usd_cents", "revenue", "all", "last30"),
            Figure(_cents(s, "last30_gross"), "usd_cents", "gross_revenue", "all", "last30"),
        ]
        if "target_month" in s:
            figs.append(Figure(_cents(s, "target_month"), "usd_cents", "revenue_target",
                               "all", "month"))
        rows = s.get("by_stream_30")
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            raise _Malformed("summary.by_stream_30 is not a list")
        for row in rows[:50]:
            if not isinstance(row, dict) or not isinstance(row.get("stream"), str):
                raise _Malformed(f"a by_stream_30 row is {row!r}")
            stream = row["stream"]
            figs.append(Figure(_cents(row, "net"), "usd_cents", "revenue", stream, "last30"))
            sales = row.get("sales")
            if isinstance(sales, int) and not isinstance(sales, bool):
                figs.append(Figure(sales, "count", "sales", stream, "last30"))
        return figs


# A connectivity check on a DIFFERENT provider from the site's (the site sits behind
# Cloudflare; this is Google's endpoint built for exactly this job). It is asked only when
# the site gives no answer at all, to tell "the site is down" from "this machine is offline".
CONTROL_URL = "https://www.google.com/generate_204"


class HealthWorker(_Base):
    """Is the site up? One GET; up/down, latency, and which products it says failed.

    No answer at all is NOT automatically "down". If the control site on another provider
    does not answer either, this machine is offline, and that is reported as a check that
    could not be made (an Err) - never as the site being down. The owner's own internet
    dropping must not read as his storefront failing: a wrong red light costs trust as
    fast as a wrong green one (HEAD 3.20)."""

    def __init__(self, spec, *, url: str, control_url: str = CONTROL_URL) -> None:
        super().__init__(spec)
        self.url = url
        self.control_url = control_url

    @never_raises()
    def run(self, ctx: WorkContext) -> Result:
        try:
            resp = ctx.http.get(self.url, timeout=20.0)
        except HttpUnreachable as exc:
            try:
                ctx.http.get(self.control_url, timeout=10.0)
            except HttpUnreachable as control_exc:
                return Err(WorkerError(
                    self.worker_id, ErrorKind.UNAVAILABLE,
                    "could not check: this machine looks offline - the control site did not "
                    f"answer either ({str(control_exc)[:120]}). The site itself was NOT judged.",
                    retryable=True,
                ))
            return Ok((make_output(
                self, valid_at=ctx.now, observed_at=ctx.now,
                payload={"up": False, "reachable": False, "control": "answered",
                         "error": str(exc)[:300]},
                figures=[Figure(0, "boolean", "up", window="now")],
                entities=self.entities,
                provenance={"source": "real", "provider": self.provider, "url": self.url},
            ),))
        doc = None
        try:
            doc = json.loads(resp.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            doc = None
        products = doc.get("products") if isinstance(doc, dict) else None
        failing = sorted(k for k, v in (products or {}).items() if v != "ok") \
            if isinstance(products, dict) else []
        said_ok = doc.get("ok") if isinstance(doc, dict) else None
        up = resp.status == 200 and said_ok is not False
        figures = [
            Figure(1 if up else 0, "boolean", "up", window="now"),
            Figure(round(resp.elapsed_ms), "ms", "latency", window="now"),
            Figure(resp.status, "http_status", "status", window="now"),
        ]
        if isinstance(products, dict):
            figures.append(Figure(len(failing), "count", "failing_products", window="now"))
            figures.append(Figure(len(products), "count", "checked_products", window="now"))
        return Ok((make_output(
            self, valid_at=ctx.now, observed_at=ctx.now,
            payload={"up": up, "reachable": True, "status": resp.status,
                     "failing": failing[:20]},
            figures=figures, entities=self.entities,
            provenance={"source": "real", "provider": self.provider, "url": self.url,
                        "status": resp.status, "latency_ms": round(resp.elapsed_ms)},
        ),))


def blog_writer(spec, **params):
    """REAL. ``posting.blog`` (blog.py): one checked draft a day for api.dokaz.net, words
    from the shared brain only, submitted as ``content.publish`` for the owner's approval.
    Imported here rather than at the top because blog.py builds on ``_Base`` above."""
    from .blog import BlogWorker
    return BlogWorker(spec, **params)


def instagram_writer(spec, **params):
    """REAL. ``posting.instagram`` (instagram.py): one checked card post a day, words from
    the shared brain only, submitted as ``social.instagram_post`` for the owner's approval.
    Imported lazily for the same reason as the blog's."""
    from .instagram import InstagramWorker
    return InstagramWorker(spec, **params)


# The whole list of worker implementations. A catalogue ``impl`` not named here is an
# error at load time, never a worker that silently does not exist.
IMPLS = {
    "placeholder": Placeholder,
    "scrooge_ledger": LedgerWorker,
    "http_health": HealthWorker,
    "blog_writer": blog_writer,
    "instagram_writer": instagram_writer,
}
