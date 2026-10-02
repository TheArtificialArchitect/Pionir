"""Reads events that were ALREADY recorded elsewhere and pays each verified one once.

Sources: the approvals queue, the Fiverr desk record, the Builds and API-builder records,
and Scrooge's revenue figure in the crew store. Nothing here asks an agent whether it did
well and nothing calls a model. It only reads; it writes nothing but the Bolts ledger.

Fail closed: a source that cannot be read (missing, unparseable, wrong shape) is reported
UNKNOWN - never zero - and pays nothing, while the other sources still pay.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .ledger import Ledger, LedgerError
from .payouts import Event, RefusalLog, pay

log = logging.getLogger("pionir.economy")

# capability of an approved action -> (payout kind, account that did the work)
APPROVAL_CAPABILITIES: dict[str, tuple[str, str]] = {
    "content.publish": ("post_approved", "posting.blog"),
    "content.crosspost_devto": ("post_approved", "posting.devto"),
    "social.instagram_post": ("post_approved", "posting.instagram"),
    "product.gumroad_publish": ("product_published", "products.shelf"),
}
FIVERR_ACCOUNT = "fiverr.desk"
BUILDS_ACCOUNT = "builds.daedalus"
API_BUILDER_ACCOUNT = "products.api_builder"
REVENUE_ACCOUNT = "moss"
REVENUE_PREFIX = "revenue:all:"


class SourceUnknown(Exception):
    """A source could not be read; the reason becomes its UNKNOWN detail."""


@dataclass(slots=True)
class SourceReport:
    name: str
    status: str                  # "ok" | "unknown"
    detail: str = ""
    events: int = 0              # verified events found
    paid: int = 0
    refused: int = 0


@dataclass(slots=True)
class CollectResult:
    sources: list = field(default_factory=list)
    paid: list = field(default_factory=list)       # of Row
    duplicates: int = 0
    refused: int = 0
    nothing_due: int = 0

    @property
    def unknown(self) -> list:
        return [s for s in self.sources if s.status == "unknown"]

    @property
    def bolts_paid(self) -> int:
        return sum(r.delta for r in self.paid)


def _read_json(path: Path, what: str) -> object:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SourceUnknown(f"{what}: no file at {path}") from None
    except (OSError, ValueError) as exc:
        raise SourceUnknown(f"{what}: unreadable ({type(exc).__name__}: {exc})") from exc


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


class Collector:
    def __init__(self, ledger: Ledger, refusals: RefusalLog, *,
                 approvals_path: Path | None, state_dir: Path | None,
                 outputs: Callable[..., list] | None = None,
                 now: Callable[[], float] = time.time) -> None:
        self.ledger = ledger
        self.refusals = refusals
        self.approvals_path = Path(approvals_path) if approvals_path else None
        self.state_dir = Path(state_dir) if state_dir else None
        self.outputs = outputs
        self._now = now

    # ---- sources: each returns verified Events or raises SourceUnknown ------------------

    def _approvals(self) -> list[Event]:
        if self.approvals_path is None:
            raise SourceUnknown("approvals queue: no path configured")
        rows = _read_json(self.approvals_path, "approvals queue")
        if not isinstance(rows, list):
            raise SourceUnknown("approvals queue: not a list")
        out = []
        for row in rows:
            if not isinstance(row, dict) or row.get("status") != "approved":
                continue
            mapped = APPROVAL_CAPABILITIES.get(str(row.get("capability")))
            rid = row.get("id")
            if mapped is None or not isinstance(rid, str) or not rid:
                continue
            ts = _num(row.get("resolved_at")) or _num(row.get("created_at")) or self._now()
            edited = row.get("owner_edited")
            out.append(Event(mapped[0], f"approval:{rid}", mapped[1], ts,
                             owner_edited=edited if isinstance(edited, bool) else None,
                             detail=str(row.get("capability"))))
        return out

    def _record(self, worker_id: str) -> dict:
        if self.state_dir is None:
            raise SourceUnknown(f"{worker_id}: no state dir configured")
        doc = _read_json(self.state_dir / f"{worker_id}.json", f"{worker_id} record")
        if not isinstance(doc, dict):
            raise SourceUnknown(f"{worker_id} record: not an object")
        return doc

    def _fiverr(self) -> list[Event]:
        orders = self._record("fiverr.desk").get("orders")
        if not isinstance(orders, dict):
            raise SourceUnknown("fiverr.desk record: no orders table")
        out = []
        for key, order in orders.items():
            if not isinstance(order, dict) or order.get("state") != "delivered":
                continue
            number = str(order.get("order_number") or key)
            out.append(Event("order_delivered", f"fiverr:order:{number}", FIVERR_ACCOUNT,
                             self._now(), detail=f"order {number}"))
        return out

    def _staged(self, worker_id: str, account: str, prefix: str) -> list[Event]:
        products = self._record(worker_id).get("products")
        if not isinstance(products, dict):
            raise SourceUnknown(f"{worker_id} record: no products table")
        out = []
        for key, p in products.items():
            staged_at = _num(p.get("staged_at")) if isinstance(p, dict) else None
            if staged_at:
                out.append(Event("build_staged", f"{prefix}:{p.get('slug') or p.get('id') or key}",
                                 account, staged_at, detail=str(p.get("slug") or key)))
        return out

    def _revenue_total(self) -> tuple[int, float]:
        if self.outputs is None:
            raise SourceUnknown("scrooge revenue: crew store not available")
        try:
            outs = self.outputs(division="treasury", worker_id="treasury.ledger", limit=5)
        except Exception as exc:  # noqa: BLE001 - an unreadable store is UNKNOWN, not zero
            raise SourceUnknown(f"scrooge revenue: store unreadable ({exc})") from exc
        for out in outs:
            if getattr(out, "derived", False):
                continue
            for fig in out.figures:
                if (fig.unit == "usd_cents" and fig.measures == "revenue"
                        and fig.stream == "all" and fig.window == "all_time"):
                    value = _num(fig.value)
                    if value is not None and value >= 0:
                        return int(value), float(out.valid_at)
        raise SourceUnknown("scrooge revenue: no all-time revenue figure recorded yet")

    def _revenue_watermark(self) -> int | None:
        last = None
        for row in self.ledger.rows():
            if row.event_id.startswith(REVENUE_PREFIX):
                last = row
        if last is None:
            return None
        try:
            return int(last.event_id[len(REVENUE_PREFIX):])
        except ValueError:
            raise SourceUnknown(f"revenue watermark unreadable: {last.event_id!r}") from None

    # ---- the run ------------------------------------------------------------------------

    def collect(self) -> CollectResult:
        result = CollectResult()
        status = self.ledger.verify_chain()
        if not status.ok:
            for name in ("approvals", "fiverr", "builds", "api_builder", "revenue"):
                result.sources.append(SourceReport(name, "unknown",
                                                   f"ledger {status.label}; nothing paid"))
            log.error("economy collector: ledger %s (%s); no payouts this run",
                      status.label, status.problem)
            return result
        for name, read in (
                ("approvals", self._approvals),
                ("fiverr", self._fiverr),
                ("builds", lambda: self._staged("builds.daedalus", BUILDS_ACCOUNT, "build")),
                ("api_builder", lambda: self._staged("products.api_builder",
                                                     API_BUILDER_ACCOUNT, "apibuild"))):
            report = SourceReport(name, "ok")
            result.sources.append(report)
            try:
                events = read()
            except SourceUnknown as exc:
                report.status, report.detail = "unknown", str(exc)
                log.warning("economy collector: %s source UNKNOWN: %s", name, exc)
                continue
            report.events = len(events)
            for event in events:
                self._pay(event, report, result)
        self._revenue(result)
        return result

    def _pay(self, event: Event, report: SourceReport, result: CollectResult) -> None:
        paid = pay(event, self.ledger, self.refusals, now=self._now)
        if paid.status == "paid":
            report.paid += 1
            result.paid.append(paid.row)
        elif paid.status == "duplicate":
            result.duplicates += 1
        elif paid.status == "nothing_due":
            result.nothing_due += 1
        else:
            report.refused += 1
            result.refused += 1

    def _revenue(self, result: CollectResult) -> None:
        report = SourceReport("revenue", "ok")
        result.sources.append(report)
        try:
            total, valid_at = self._revenue_total()
            mark = self._revenue_watermark()
        except SourceUnknown as exc:
            report.status, report.detail = "unknown", str(exc)
            log.warning("economy collector: revenue source UNKNOWN: %s", exc)
            return
        event_id = f"{REVENUE_PREFIX}{total}"
        if mark is None:
            # revenue earned before Bolts existed is not paid for; record where we started
            try:
                self.ledger.append(REVENUE_ACCOUNT, 0, "revenue baseline", event_id)
            except LedgerError as exc:
                report.status, report.detail = "unknown", f"baseline not recorded: {exc}"
            else:
                report.detail = f"baseline {total} cents"
            return
        if total <= mark:
            return
        report.events = 1
        event = Event("revenue_received", event_id, REVENUE_ACCOUNT, valid_at,
                      cents=total - mark, detail=f"{total - mark} cents newly received")
        self._pay(event, report, result)

