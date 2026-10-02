"""The collector and the ``treasury.bolts`` worker: events already on record, paid once.

Each test fails if its rule is reverted: a pending/denied approval paid; an unreadable
source read as zero (or paying anything); a second run paying again; revenue paid for money
earned before Bolts existed; a broken ledger still accepting payouts. All data is fake and
lives in temp dirs: no real ~/.pionir, queue, Discord or network is touched.
"""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from pionir.crew.figures import Figure
from pionir.crew.registry import default_registry
from pionir.crew.result import Err, Ok
from pionir.crew.worker import ErrorKind, WorkContext
from pionir.economy.collector import Collector
from pionir.economy.ledger import Ledger
from pionir.economy.payouts import RefusalLog

T0 = datetime(2026, 9, 30, 12, 0, 0).timestamp()


def revenue_output(cents: int, derived: bool = False):
    fig = Figure(cents, "usd_cents", "revenue", stream="all", window="all_time")
    return SimpleNamespace(derived=derived, valid_at=T0, figures=(fig,))


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.economy = root / "economy"
        self.state = root / "state"
        self.state.mkdir()
        self.queue = root / "queue.json"
        self.revenue: list = []
        self.clock = lambda: T0
        self.ledger = Ledger.in_dir(self.economy, now=self.clock)
        self.refusals = RefusalLog.in_dir(self.economy, now=self.clock)

    def collector(self, outputs="default") -> Collector:
        if outputs == "default":
            outputs = lambda **kw: list(self.revenue)  # noqa: E731
        return Collector(self.ledger, self.refusals, approvals_path=self.queue,
                         state_dir=self.state, outputs=outputs, now=self.clock)

    def queue_rows(self, rows) -> None:
        self.queue.write_text(json.dumps(rows), encoding="utf-8")

    def record(self, worker_id: str, doc) -> None:
        (self.state / f"{worker_id}.json").write_text(json.dumps(doc), encoding="utf-8")

    def healthy(self) -> None:
        """Every source readable and empty, so a test adds only what it is about."""
        self.queue_rows([])
        self.record("fiverr.desk", {"orders": {}})
        self.record("builds.daedalus", {"products": {}})
        self.record("products.api_builder", {"products": {}})
        self.revenue = [revenue_output(0)]


class PayKindTests(Case):
    def test_each_verified_event_pays_once_to_the_right_account(self) -> None:
        self.healthy()
        self.queue_rows([
            {"id": "a1", "capability": "content.publish", "status": "approved",
             "owner_edited": True},
            {"id": "a2", "capability": "product.gumroad_publish", "status": "approved"},
        ])
        self.record("fiverr.desk", {"orders": {"7": {"state": "delivered", "order_number": "FO7"},
                                               "8": {"state": "in_progress"}}})
        self.record("builds.daedalus", {"products": {"x": {"slug": "widget", "staged_at": T0},
                                                     "y": {"slug": "draft"}}})
        self.record("products.api_builder", {"products": {"z": {"slug": "api-1",
                                                               "staged_at": T0}}})
        res = self.collector().collect()
        self.assertEqual(res.unknown, [])
        self.assertEqual(self.ledger.balances(), {
            "posting.blog": 10, "products.shelf": 40, "fiverr.desk": 30,
            "builds.daedalus": 25, "products.api_builder": 25, "moss": 0})
        ids = {r.event_id for r in self.ledger.rows()}
        self.assertTrue({"approval:a1", "approval:a2", "fiverr:order:FO7", "build:widget",
                         "apibuild:api-1"} <= ids)

    def test_an_approval_with_no_owner_edits_earns_the_excellence_bonus(self) -> None:
        self.healthy()
        self.queue_rows([
            {"id": "clean", "capability": "content.publish", "status": "approved",
             "owner_edited": False},
            {"id": "edited", "capability": "content.crosspost_devto", "status": "approved",
             "owner_edited": True},
            {"id": "unknown", "capability": "social.instagram_post", "status": "approved"},
        ])
        self.collector().collect()
        bal = self.ledger.balances()
        self.assertEqual(bal["posting.blog"], 15)        # base 10 + excellence 5
        self.assertEqual(bal["posting.devto"], 10)       # edited: base only
        self.assertEqual(bal["posting.instagram"], 10)   # edit status unknown: base only

    def test_only_an_exactly_approved_status_pays(self) -> None:
        self.healthy()
        self.queue_rows([{"id": str(i), "capability": "content.publish", "status": s}
                         for i, s in enumerate(("pending", "denied", "approved_failed",
                                                "APPROVED", "expired", None))])
        res = self.collector().collect()
        self.assertEqual(res.paid, [])
        self.assertEqual([r.event_id for r in self.ledger.rows()], ["revenue:all:0"])

    def test_a_capability_with_no_payout_pays_nothing(self) -> None:
        self.healthy()
        self.queue_rows([{"id": "q", "capability": "device.change", "status": "approved"}])
        self.assertEqual(self.collector().collect().paid, [])

    def test_a_second_run_pays_nothing_new(self) -> None:
        self.healthy()
        self.queue_rows([{"id": "a1", "capability": "content.publish", "status": "approved"}])
        self.record("fiverr.desk", {"orders": {"1": {"state": "delivered"}}})
        first = self.collector().collect()
        again = self.collector().collect()
        self.assertEqual(len(first.paid), 2)
        self.assertEqual(again.paid, [])
        self.assertEqual(again.duplicates, 2)
        self.assertEqual(len(self.ledger.rows()), 3)       # 2 payouts + the revenue baseline


class UnknownSourceTests(Case):
    def unknown_names(self, res) -> set:
        return {s.name for s in res.unknown}

    def test_a_missing_approvals_queue_is_unknown_and_the_rest_still_pay(self) -> None:
        self.healthy()
        self.queue.unlink(missing_ok=True)
        self.record("fiverr.desk", {"orders": {"1": {"state": "delivered"}}})
        res = self.collector().collect()
        self.assertEqual(self.unknown_names(res), {"approvals"})
        self.assertEqual(self.ledger.balance("fiverr.desk"), 30)

    def test_garbage_or_non_list_approvals_are_unknown_never_zero(self) -> None:
        self.healthy()
        for text in ("not json {", json.dumps({"id": "a"}), ""):
            self.queue.write_text(text, encoding="utf-8")
            res = self.collector().collect()
            self.assertEqual(self.unknown_names(res), {"approvals"}, text)
            self.assertEqual(self.ledger.rows()[-1].event_id.split(":")[0], "revenue")
        self.assertEqual(self.ledger.balances(), {"moss": 0})

    def test_a_record_without_its_table_is_unknown(self) -> None:
        self.healthy()
        self.record("fiverr.desk", {"orders": []})
        self.record("builds.daedalus", {"something": "else"})
        (self.state / "products.api_builder.json").unlink()
        res = self.collector().collect()
        self.assertEqual(self.unknown_names(res), {"fiverr", "builds", "api_builder"})
        for s in res.unknown:
            self.assertEqual((s.events, s.paid), (0, 0))

    def test_no_store_or_an_unreadable_store_is_unknown_revenue(self) -> None:
        self.healthy()
        self.assertEqual(self.unknown_names(self.collector(outputs=None).collect()),
                         {"revenue"})

        def boom(**_kw):
            raise OSError("db locked")
        res = self.collector(outputs=boom).collect()
        self.assertEqual(self.unknown_names(res), {"revenue"})
        self.assertIn("db locked", res.unknown[0].detail)
        self.assertEqual(self.ledger.rows(), [])

    def test_a_derived_only_or_figureless_output_is_unknown_revenue(self) -> None:
        self.healthy()
        self.revenue = [revenue_output(5000, derived=True)]
        self.assertEqual(self.unknown_names(self.collector().collect()), {"revenue"})
        self.revenue = [SimpleNamespace(derived=False, valid_at=T0, figures=())]
        self.assertEqual(self.unknown_names(self.collector().collect()), {"revenue"})
        self.assertEqual(self.ledger.rows(), [])


class RevenueTests(Case):
    def test_the_first_run_records_a_baseline_and_pays_nothing_for_the_past(self) -> None:
        self.healthy()
        self.revenue = [revenue_output(250_000)]
        res = self.collector().collect()
        self.assertEqual(res.paid, [])
        (row,) = self.ledger.rows()
        self.assertEqual((row.account, row.delta, row.event_id), ("moss", 0,
                                                                  "revenue:all:250000"))

    def test_new_money_pays_one_bolt_per_dollar(self) -> None:
        self.healthy()
        self.revenue = [revenue_output(1000)]
        self.collector().collect()
        self.revenue = [revenue_output(1000 + 1234)]
        res = self.collector().collect()
        self.assertEqual(self.ledger.balance("moss"), 12)
        self.assertEqual(res.bolts_paid, 12)

    def test_a_huge_sale_is_capped_per_payout(self) -> None:
        self.healthy()
        self.revenue = [revenue_output(0)]
        self.collector().collect()
        self.revenue = [revenue_output(5_000_000)]
        self.collector().collect()
        self.assertEqual(self.ledger.balance("moss"), 100)

    def test_unchanged_or_falling_revenue_pays_nothing(self) -> None:
        self.healthy()
        self.revenue = [revenue_output(5000)]
        self.collector().collect()
        for total in (5000, 3000):                       # a refund drops the total
            self.revenue = [revenue_output(total)]
            self.assertEqual(self.collector().collect().paid, [])
        self.assertEqual(self.ledger.balance("moss"), 0)

    def test_less_than_a_bolt_waits_and_is_not_lost(self) -> None:
        self.healthy()
        self.revenue = [revenue_output(1000)]
        self.collector().collect()
        self.revenue = [revenue_output(1050)]            # 50 cents: nothing due, mark stays
        self.assertEqual(self.collector().collect().paid, [])
        self.revenue = [revenue_output(1150)]            # 150 cents since the mark -> 1 Bolt
        self.collector().collect()
        self.assertEqual(self.ledger.balance("moss"), 1)


class FailClosedTests(Case):
    def test_a_broken_ledger_pays_nothing_and_every_source_is_unknown(self) -> None:
        self.healthy()
        self.queue_rows([{"id": "a1", "capability": "content.publish", "status": "approved"}])
        self.collector().collect()
        lines = self.ledger.path.read_text(encoding="ascii").splitlines()
        row = json.loads(lines[0])
        row["delta"] = 9999
        lines[0] = json.dumps(row, sort_keys=True, separators=(",", ":"))
        self.ledger.path.write_text("\n".join(lines) + "\n", encoding="ascii")
        before = self.ledger.path.read_bytes()
        self.queue_rows([{"id": "a2", "capability": "content.publish", "status": "approved"}])
        res = self.collector().collect()
        self.assertEqual(res.paid, [])
        self.assertEqual({s.name for s in res.unknown},
                         {"approvals", "fiverr", "builds", "api_builder", "revenue"})
        self.assertEqual(self.ledger.path.read_bytes(), before)

    def test_over_cap_payouts_are_refused_and_counted_not_dropped(self) -> None:
        self.healthy()
        self.record("builds.daedalus", {"products": {
            str(i): {"slug": f"p{i}", "staged_at": T0} for i in range(6)}})
        res = self.collector().collect()
        self.assertEqual(self.ledger.balance("builds.daedalus"), 100)    # 4 x 25 = the cap
        self.assertEqual(res.refused, 2)
        self.assertEqual(self.refusals.counts()["by_reason"], {"account_daily_cap": 2})
        self.assertEqual(self.refusals.counts()["total"], 2)


class WorkerTests(Case):
    def ctx(self, **kw) -> WorkContext:
        base = dict(now=T0, http=None, secrets_dir=Path(self._tmp.name) / "secrets",
                    state_dir=self.state, economy_dir=self.economy,
                    approvals_path=self.queue, outputs=lambda **k: list(self.revenue))
        base.update(kw)
        return WorkContext(**base)

    def worker(self):
        return default_registry().require("treasury.bolts")

    def test_the_worker_is_in_the_treasury_division_at_900_seconds(self) -> None:
        w = self.worker()
        self.assertEqual(w.worker_id, "treasury.bolts")
        self.assertTrue(w.live)
        self.assertEqual(w.cadence_seconds, 900)
        self.assertEqual(w.provider, "none")

    def test_a_run_pays_and_reports_figures(self) -> None:
        self.healthy()
        self.queue_rows([{"id": "a1", "capability": "content.publish", "status": "approved"}])
        res = self.worker().run(self.ctx())
        self.assertIsInstance(res, Ok, res)
        (out,) = res.value
        self.assertEqual(out.kind, "bolts.run")
        self.assertEqual(out.payload["paid"][0]["bolts"], 10)
        self.assertEqual(out.payload["unknown_sources"], [])
        by_measure = {f.measures: f.value for f in out.figures}
        self.assertEqual(by_measure["Bolts paid this run"], 10)
        self.assertEqual(by_measure["sources UNKNOWN this run"], 0)
        self.assertEqual(self.ledger.balance("posting.blog"), 10)

    def test_a_run_furnishes_homes_with_what_accounts_hold_and_a_rerun_buys_nothing(self) -> None:
        self.healthy()
        self.ledger.append("daedalus", 90, "build_staged", "seed:daedalus")
        (out,) = self.worker().run(self.ctx()).value
        bought = out.payload["bought"]
        self.assertTrue(bought)
        self.assertEqual(self.ledger.balance("daedalus"), 90 - sum(b["price"] for b in bought))
        by_measure = {f.measures: f.value for f in out.figures}
        self.assertEqual(by_measure["things bought for homes this run"], len(bought))
        (again,) = self.worker().run(self.ctx()).value
        self.assertEqual(again.payload["bought"], [])

    def test_an_unreadable_source_shows_in_the_figures_as_unknown(self) -> None:
        self.healthy()
        self.queue.unlink()
        (out,) = self.worker().run(self.ctx()).value
        self.assertEqual(out.payload["unknown_sources"], ["approvals"])
        self.assertEqual({f.measures: f.value for f in out.figures}["sources UNKNOWN this run"], 1)

    def test_a_broken_ledger_is_a_malformed_error_that_pays_nothing(self) -> None:
        self.healthy()
        self.queue_rows([{"id": "a1", "capability": "content.publish", "status": "approved"}])
        self.worker().run(self.ctx())
        lines = self.ledger.path.read_text(encoding="ascii").splitlines()
        self.ledger.path.write_text("\n".join(lines[1:]) + "\n", encoding="ascii")
        res = self.worker().run(self.ctx())
        self.assertIsInstance(res, Err, res)
        self.assertEqual(res.error.kind, ErrorKind.MALFORMED)
        self.assertFalse(res.error.retryable)

    def test_without_an_economy_dir_the_worker_is_not_configured(self) -> None:
        res = self.worker().run(self.ctx(economy_dir=None))
        self.assertIsInstance(res, Err, res)
        self.assertEqual(res.error.kind, ErrorKind.NOT_CONFIGURED)


if __name__ == "__main__":
    unittest.main()
