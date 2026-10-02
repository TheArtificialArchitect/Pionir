"""``treasury.bolts``: the worker that pays Bolts for outcomes already on record.

A job, not an agent: no model, no Claude call, no network. It reads the approvals queue, the
Fiverr desk, the Builds and API-builder records and Scrooge's revenue figure, and appends
one ledger row per verified event, once (see ``pionir.economy``). Bolts are a play currency:
this worker never touches money and nothing it writes can authorise an action.
"""
from __future__ import annotations

from ..economy.collector import Collector
from ..economy.ledger import Ledger
from ..economy.payouts import RefusalLog
from .figures import Figure
from .result import Ok, Result
from .worker import ErrorKind, WorkContext, make_output, never_raises
from .workers import _Base


class BoltsWorker(_Base):
    @never_raises()
    def run(self, ctx: WorkContext) -> Result:
        if ctx.economy_dir is None:
            return self._err(ErrorKind.NOT_CONFIGURED, "no economy dir", retryable=False)
        now = lambda: ctx.now  # noqa: E731 - one clock for the whole run
        ledger = Ledger.in_dir(ctx.economy_dir, now=now)
        refusals = RefusalLog.in_dir(ctx.economy_dir, now=now)
        collector = Collector(ledger, refusals, approvals_path=ctx.approvals_path,
                              state_dir=ctx.state_dir, outputs=ctx.outputs, now=now)
        result = collector.collect()
        status = ledger.verify_chain()
        if not status.ok:
            return self._err(ErrorKind.MALFORMED,
                             f"the Bolts ledger is {status.label} ({status.problem}); "
                             "nothing is paid until a person looks at it", retryable=False)
        counts = refusals.counts()
        unknown = result.unknown
        payload = {
            "note": "Bolts are a play currency, never revenue or money.",
            "chain": status.to_dict(),
            "paid": [{"account": r.account, "bolts": r.delta, "event_id": r.event_id}
                     for r in result.paid],
            "sources": [{"name": s.name, "status": s.status, "detail": s.detail,
                         "events": s.events, "paid": s.paid, "refused": s.refused}
                        for s in result.sources],
            "unknown_sources": [s.name for s in unknown],
            "refused_today": counts["by_reason"],
        }
        figures = [
            Figure(result.bolts_paid, "count", "Bolts paid this run", window="run"),
            Figure(len(result.paid), "count", "payouts made this run", window="run"),
            Figure(counts["total"], "count", "payouts refused today", window="today"),
            Figure(len(unknown), "count", "sources UNKNOWN this run", window="run"),
        ]
        return Ok((make_output(self, kind="bolts.run", valid_at=ctx.now, observed_at=ctx.now,
                               payload=payload, figures=figures, entities=self.entities,
                               provenance={"source": "real", "provider": self.provider,
                                           "record": "the ledger and records it reads"}),))
