"""``treasury.etsy``: what the Etsy shop sold, read-only, as Etsy reports it.

Each run asks Pionir's READ_ONLY ``etsy.receipts`` for the last ``window_days`` of receipts -
amounts and listing ids only: no buyer's name, address, e-mail or message ever reaches the
crew (the adapter drops them), and nothing is ever sent to a buyer (Etsy's API cannot message
one; digital files are delivered by Etsy itself).

What it reports is the **Etsy-reported gross** of paid receipts (``grandtotal``), in USD
cents, over the last 30 days and over everything it has seen: before Etsy's fees, and NOT our
revenue - Scrooge's ledger never sees it, and a new shop's earnings are 75% held by Etsy for
a rolling 90 days. A receipt in another currency is counted, never valued. Each receipt it
has not seen before is one ``etsy.sale`` row (its id, amount and listing ids), once.
"""
from __future__ import annotations

from pathlib import Path

from ...adapters.etsy import RECEIPTS, credentials_problem
from ..figures import Figure
from ..hands import Job
from ..result import Ok, Result
from ..worker import ErrorKind, WorkContext, make_output, never_raises
from ..workers import _Base
from .common import Record, Unreadable

NOTE = ("Etsy-reported gross of paid receipts, before Etsy's fees; not our revenue (Scrooge "
        "never sees it), and a new shop's earnings are 75% held by Etsy for 90 days")
KEEP_SEEN = 5000


class EtsySales(_Base):
    def __init__(self, spec, *, window_days: int = 90) -> None:
        super().__init__(spec)
        if isinstance(window_days, bool) or not isinstance(window_days, int) \
                or not 1 <= window_days <= 365:
            raise ValueError("window_days: 1-365")
        self.window_days = window_days

    def readiness(self, secrets_dir) -> str | None:
        return credentials_problem(Path(secrets_dir) / "etsy.json")

    @never_raises()
    def run(self, ctx: WorkContext) -> Result:
        if ctx.state_dir is None:
            return self._err(ErrorKind.NOT_CONFIGURED, "no state dir", retryable=False)
        problem = self.readiness(ctx.secrets_dir)
        if problem:
            return self._err(ErrorKind.NOT_CONFIGURED, problem, retryable=False)
        if ctx.job is None:
            return self._err(ErrorKind.NOT_CONFIGURED, "no hands: Etsy is read through Pionir",
                             retryable=False)
        try:
            record = Record(ctx.state_dir, self.worker_id,
                            {"seen": {}, "counts": {}})
        except Unreadable as exc:
            return self._err(ErrorKind.MALFORMED, f"its record is unreadable ({exc})",
                             retryable=False)
        since = int(ctx.now - self.window_days * 86400)
        out = ctx.job(Job(RECEIPTS, {"min_created": since},
                          what="read the Etsy shop's receipts (amounts only)"))
        if not out.ran or not isinstance(out.result, dict) \
                or not isinstance(out.result.get("receipts"), list):
            kind = ErrorKind.NOT_CONFIGURED if "not configured" in (out.error or "") \
                else ErrorKind.UNAVAILABLE
            return self._err(kind, f"the receipts could not be read ({out.status}: "
                             f"{out.error}); Etsy sales are UNKNOWN, never zero")
        seen = record.doc["seen"]
        events = []
        for r in out.result["receipts"]:
            if not isinstance(r, dict) or not isinstance(r.get("receipt_id"), int):
                continue
            key = str(r["receipt_id"])
            row = {"created": r.get("created"), "paid": r.get("paid") is True,
                   "cents": r.get("grandtotal_cents"), "currency": r.get("currency"),
                   "listing_ids": r.get("listing_ids") or []}
            if key not in seen and row["paid"]:
                events.append(make_output(
                    self, kind="etsy.sale", valid_at=float(row["created"] or ctx.now),
                    observed_at=ctx.now,
                    payload={"receipt_id": r["receipt_id"], "listing_ids": row["listing_ids"],
                             "currency": row["currency"], "note": NOTE},
                    figures=[Figure(row["cents"], "usd_cents", "Etsy-reported gross of one "
                                    "paid receipt")] if row["currency"] == "USD"
                    and isinstance(row["cents"], int) else [],
                    entities=self.entities,
                    provenance={"source": "real", "provider": self.provider,
                                "record": "Etsy receipts, via Pionir"}))
            seen[key] = row
        if len(seen) > KEEP_SEEN:
            for k in sorted(seen, key=lambda k: seen[k].get("created") or 0)[
                    :len(seen) - KEEP_SEEN]:
                del seen[k]
        record.save()
        paid = [s for s in seen.values() if s.get("paid")]
        recent = [s for s in paid if float(s.get("created") or 0) >= ctx.now - 30 * 86400]

        def usd(rows):
            return sum(s["cents"] for s in rows if s.get("currency") == "USD"
                       and isinstance(s.get("cents"), int))

        other = sum(1 for s in paid if s.get("currency") != "USD"
                    or not isinstance(s.get("cents"), int))
        figures = [
            Figure(usd(recent), "usd_cents", "Etsy-reported gross, paid receipts",
                   stream="etsy", window="last30"),
            Figure(len(recent), "count", "paid Etsy receipts", stream="etsy", window="last30"),
            Figure(usd(paid), "usd_cents", "Etsy-reported gross, paid receipts seen",
                   stream="etsy", window="all_time"),
            Figure(other, "count", "paid Etsy receipts not in USD (counted, not valued)",
                   stream="etsy", window="all_time"),
        ]
        return Ok((*events, make_output(
            self, kind="etsy.sales", valid_at=ctx.now, observed_at=ctx.now,
            payload={"note": NOTE, "new_receipts": len(events)}, figures=figures,
            entities=self.entities,
            provenance={"source": "real", "provider": self.provider,
                        "record": "Etsy receipts, via Pionir"})))
