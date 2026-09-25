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
- ``traffic_reader`` - REAL (results.py). The same dash read as the ledger (``DashReader``),
  its ``traffic`` object: what the posts did, per post and per channel, last 30 days.
- ``devto_crossposter`` - REAL (devto.py). Cross-posts each published blog post to dev.to
  once, unchanged but for its links' ``utm_source``, for the owner's approval. No words.
- ``order_desk`` - REAL (orders.py). Reads the client orders through Pionir and answers each
  by fixed template (acknowledgement, quote acknowledgement or decline) for the owner's
  approval. No words: no model can put a promise, a price or a date in a client's email.
- ``delivery_desk`` - REAL (delivery.py). Ships each order's finished zip, dropped by the
  owner in its order's folder, as ``client.deliver`` by fixed template for his approval.
- ``product_shelf`` - REAL (products.py). Submits each product the owner stages in its own
  folder as ``product.gumroad_publish`` for his approval, and reads what the live ones sold
  (``product.gumroad_list``). No words: every word of a listing is the owner's.
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


class DashReader(_Base):
    """A read of Scrooge's ``GET /dash/api.json`` with the read-only token. The token can
    reach only GET endpoints on an allowlist (Scrooge's ``READ_PATHS``); it cannot approve
    or send anything. Shared by every worker that reads the dash (the ledger, the traffic
    results), so the token is found, checked and sent in exactly one place.

    ``unknown`` names what a subclass reads, for the error when it cannot: no token is
    ``NOT_CONFIGURED`` and that thing is UNKNOWN - never a zero."""

    unknown = "the dash"

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

    def _read_dash(self, ctx: WorkContext) -> Result:
        """``Ok((doc, resp))`` with the parsed JSON object, or the Err to return."""
        path = self._token_path(ctx.secrets_dir)
        if not path.is_file():
            return self._err(ErrorKind.NOT_CONFIGURED,
                             f"no read token at {path}; {self.unknown} is UNKNOWN, not zero. "
                             f"Run Scrooge's tools/setup-read-token.ps1 to create it",
                             retryable=False)
        token = path.read_text(encoding="utf-8").strip()
        if not token:
            return self._err(ErrorKind.NOT_CONFIGURED,
                             f"the read token at {path} is empty; {self.unknown} is UNKNOWN, "
                             "not zero", retryable=False)
        try:
            resp = ctx.http.get(self.url, headers={"x-dash-token": token}, timeout=20.0)
        except HttpUnreachable as exc:
            return self._err(ErrorKind.UNAVAILABLE, f"{self.url}: {exc}")
        if resp.status != 200:
            return _status_error(self, resp.status, resp.body)
        try:
            doc = json.loads(resp.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            return self._err(ErrorKind.MALFORMED, f"{type(exc).__name__}: {exc}")
        if not isinstance(doc, dict):
            return self._err(ErrorKind.MALFORMED, "the dash answered with something that is "
                             "not an object")
        return Ok((doc, resp))

    def _provenance(self, resp) -> dict:
        return {"source": "real", "provider": self.provider, "url": self.url,
                "status": resp.status, "latency_ms": round(resp.elapsed_ms)}


class LedgerWorker(DashReader):
    """Scrooge's ledger, read-only (``summary`` of ``/dash/api.json``)."""

    unknown = "revenue"

    @never_raises()
    def run(self, ctx: WorkContext) -> Result:
        got = self._read_dash(ctx)
        if isinstance(got, Err):
            return got
        doc, resp = got.value
        try:
            summary = doc.get("summary")
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
            provenance=self._provenance(resp),
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


def traffic_reader(spec, **params):
    """REAL. ``posting.results`` (results.py): what the posts did - the site's traffic from
    ``/dash/api.json``, matched to the blog worker's published posts by campaign. Imported
    lazily: results.py builds on ``DashReader`` above."""
    from .results import TrafficWorker
    return TrafficWorker(spec, **params)


def devto_crossposter(spec, **params):
    """REAL. ``posting.devto`` (devto.py): each published blog post cross-posted to dev.to,
    once, checked, as ``content.crosspost_devto`` for the owner's approval."""
    from .devto import DevtoWorker
    return DevtoWorker(spec, **params)


def order_desk(spec, **params):
    """REAL. ``contracts.orders`` (orders.py): every client order answered by fixed
    template, as ``client.email`` for the owner's approval."""
    from .orders import OrderDesk
    return OrderDesk(spec, **params)


def delivery_desk(spec, **params):
    """REAL. ``contracts.delivery`` (delivery.py): each order's finished zip delivered to its
    client by fixed template, as ``client.deliver`` for the owner's approval."""
    from .delivery import DeliveryDesk
    return DeliveryDesk(spec, **params)


def product_shelf(spec, **params):
    """REAL. ``products.shelf`` (products.py): each staged product published on Gumroad, for
    the owner's approval, and the live products' sales as Gumroad reports them."""
    from .products import ProductShelf
    return ProductShelf(spec, **params)


# The whole list of worker implementations. A catalogue ``impl`` not named here is an
# error at load time, never a worker that silently does not exist.
IMPLS = {
    "placeholder": Placeholder,
    "scrooge_ledger": LedgerWorker,
    "http_health": HealthWorker,
    "blog_writer": blog_writer,
    "instagram_writer": instagram_writer,
    "traffic_reader": traffic_reader,
    "devto_crossposter": devto_crossposter,
    "order_desk": order_desk,
    "delivery_desk": delivery_desk,
    "product_shelf": product_shelf,
}
