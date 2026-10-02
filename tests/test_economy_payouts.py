"""What an event is worth, and the caps that bound the whole economy.

Each test fails if its rule is reverted: a bonus paid for an edit that was merely unknown;
an over-cap payout silently dropped (or paid anyway); the same event paid twice; a refusal
counted again on every run; a revenue payout that ignored its ceiling.
"""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from pionir.economy import payouts
from pionir.economy.ledger import Ledger
from pionir.economy.payouts import (Event, RefusalLog, RefuseReason, amount_for, pay, pay_for)

T0 = datetime(2026, 9, 30, 12, 0, 0).timestamp()
DAY = 86400.0


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class PayoutCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.clock = Clock()
        self.ledger = Ledger.in_dir(self.dir, now=self.clock)
        self.refusals = RefusalLog.in_dir(self.dir, now=self.clock)

    def ev(self, kind: str, n: int = 1, account: str = "posting.blog", **kw) -> Event:
        return Event(kind, f"{kind}:{n}", account, T0, **kw)

    def pay(self, event: Event):
        return pay(event, self.ledger, self.refusals)


class AmountTests(PayoutCase):
    def test_each_kind_pays_its_table_amount(self) -> None:
        for kind in ("build_staged", "post_approved", "order_delivered", "product_published"):
            self.assertEqual(amount_for(self.ev(kind)), (payouts.PAYOUTS[kind]["base"], False))

    def test_excellence_only_when_the_owner_positively_changed_nothing(self) -> None:
        for kind in ("post_approved", "product_published"):
            base = payouts.PAYOUTS[kind]["base"]
            bonus = payouts.PAYOUTS[kind]["excellence"]
            self.assertEqual(amount_for(self.ev(kind, owner_edited=False)),
                             (base + bonus, True))
            self.assertEqual(amount_for(self.ev(kind, owner_edited=True)), (base, False))
            self.assertEqual(amount_for(self.ev(kind, owner_edited=None)), (base, False))

    def test_a_kind_with_no_excellence_entry_pays_none_for_it(self) -> None:
        self.assertEqual(amount_for(self.ev("order_delivered", owner_edited=False)),
                         (payouts.PAYOUTS["order_delivered"]["base"], False))

    def test_revenue_scales_with_cents_and_is_capped(self) -> None:
        t = payouts.PAYOUTS["revenue_received"]
        per, cap = t["cents_per_bolt"], t["max"]
        self.assertEqual(amount_for(self.ev("revenue_received", cents=per - 1))[0], 0)
        self.assertEqual(amount_for(self.ev("revenue_received", cents=per * 7 + 5))[0], 7)
        self.assertEqual(amount_for(self.ev("revenue_received", cents=per * cap * 50))[0], cap)
        self.assertEqual(amount_for(self.ev("revenue_received", cents=-500))[0], 0)

    def test_an_unknown_kind_has_no_amount(self) -> None:
        self.assertEqual(amount_for(self.ev("mystery")), (-1, False))

    def test_the_table_is_whole_numbers_only(self) -> None:
        for kind, row in payouts.PAYOUTS.items():
            for key, value in row.items():
                self.assertIsInstance(value, int, f"{kind}.{key}")
                self.assertGreaterEqual(value, 0)


class PayTests(PayoutCase):
    def test_a_paid_event_lands_as_one_row_with_its_event_id(self) -> None:
        res = self.pay(self.ev("post_approved", 7, owner_edited=False))
        self.assertEqual(res.status, "paid")
        self.assertEqual((res.row.account, res.row.delta, res.row.event_id),
                         ("posting.blog", 15, "post_approved:7"))
        self.assertIn("excellence", res.row.reason)
        self.assertEqual(self.ledger.balance("posting.blog"), 15)

    def test_the_same_event_is_never_paid_twice(self) -> None:
        first = self.pay(self.ev("build_staged", 1, account="builds.daedalus"))
        again = self.pay(self.ev("build_staged", 1, account="builds.daedalus"))
        self.assertEqual((first.status, again.status), ("paid", "duplicate"))
        self.assertEqual(again.row, first.row)
        self.assertEqual(self.ledger.balance("builds.daedalus"), 25)

    def test_pay_for_returns_the_row_or_none(self) -> None:
        row = pay_for(self.ev("order_delivered", 1, account="fiverr.desk"), self.ledger,
                      self.refusals)
        self.assertEqual(row.delta, 30)
        self.assertIsNone(pay_for(self.ev("mystery"), self.ledger, self.refusals))
        self.assertIsNone(pay_for(self.ev("revenue_received", cents=10, account="moss"),
                                  self.ledger, self.refusals))

    def test_an_unknown_kind_or_bad_account_is_refused_with_a_reason(self) -> None:
        a = self.pay(self.ev("mystery"))
        b = self.pay(self.ev("build_staged", 2, account="Not An Account"))
        self.assertEqual((a.status, a.reason), ("refused", RefuseReason.UNKNOWN_KIND))
        self.assertEqual((b.status, b.reason), ("refused", RefuseReason.BAD_ACCOUNT))
        self.assertEqual(self.ledger.rows(), [])

    def test_zero_due_is_nothing_due_not_a_row(self) -> None:
        res = self.pay(self.ev("revenue_received", cents=40, account="moss"))
        self.assertEqual(res.status, "nothing_due")
        self.assertEqual(self.ledger.rows(), [])


class CapTests(PayoutCase):
    def test_the_per_account_daily_cap_refuses_and_counts_not_drops(self) -> None:
        for n in range(4):                       # 4 x 25 = 100 = the cap exactly
            self.assertEqual(self.pay(self.ev("build_staged", n, account="builds.daedalus"))
                             .status, "paid")
        over = self.pay(self.ev("build_staged", 99, account="builds.daedalus"))
        self.assertEqual((over.status, over.reason), ("refused", RefuseReason.ACCOUNT_DAILY_CAP))
        self.assertIsNone(over.row)
        self.assertEqual(self.ledger.balance("builds.daedalus"), 100)
        counts = self.refusals.counts()
        self.assertEqual(counts["total"], 1)
        self.assertEqual(counts["by_reason"], {"account_daily_cap": 1})

    def test_other_accounts_are_untouched_by_one_accounts_cap(self) -> None:
        for n in range(4):
            self.pay(self.ev("build_staged", n, account="builds.daedalus"))
        self.assertEqual(self.pay(self.ev("order_delivered", 1, account="fiverr.desk")).status,
                         "paid")

    def test_the_global_daily_cap_bounds_the_whole_economy(self) -> None:
        with mock.patch.object(payouts, "DAILY_CAP_GLOBAL", 60):
            self.assertEqual(self.pay(self.ev("order_delivered", 1, account="fiverr.desk"))
                             .status, "paid")                         # 30
            self.assertEqual(self.pay(self.ev("order_delivered", 2, account="fiverr.two"))
                             .status, "paid")                         # 60
            over = self.pay(self.ev("post_approved", 3))              # 70 > 60
            self.assertEqual((over.status, over.reason),
                             ("refused", RefuseReason.GLOBAL_DAILY_CAP))
        self.assertEqual(self.refusals.counts()["by_reason"], {"global_daily_cap": 1})

    def test_a_refusal_is_counted_once_per_day_however_often_it_is_retried(self) -> None:
        for n in range(4):
            self.pay(self.ev("build_staged", n, account="builds.daedalus"))
        for _ in range(5):
            self.pay(self.ev("build_staged", 99, account="builds.daedalus"))
        self.assertEqual(self.refusals.counts()["total"], 1)

    def test_a_refused_payout_is_paid_the_next_day_if_still_verified(self) -> None:
        for n in range(4):
            self.pay(self.ev("build_staged", n, account="builds.daedalus"))
        self.assertEqual(self.pay(self.ev("build_staged", 99, account="builds.daedalus"))
                         .status, "refused")
        self.clock.t += DAY
        res = self.pay(self.ev("build_staged", 99, account="builds.daedalus"))
        self.assertEqual(res.status, "paid")
        self.assertEqual(self.ledger.balance("builds.daedalus"), 125)
        self.assertEqual(self.refusals.counts()["total"], 0)          # today's count is fresh

    def test_a_cap_binds_the_whole_payout_not_a_part_of_it(self) -> None:
        for n in range(3):
            self.pay(self.ev("build_staged", n, account="posting.blog"))   # 75
        res = self.pay(self.ev("post_approved", 5, owner_edited=False))    # 15 > 25 left? no
        self.assertEqual(res.status, "paid")                               # 90
        res = self.pay(self.ev("post_approved", 6, owner_edited=False))    # 105 > 100
        self.assertEqual(res.status, "refused")
        self.assertEqual(self.ledger.balance("posting.blog"), 90)

    def test_unreadable_refusal_lines_are_counted_not_hidden(self) -> None:
        self.refusals.path.write_text("garbage\n", encoding="utf-8")
        self.assertEqual(self.refusals.counts()["unreadable_lines"], 1)


if __name__ == "__main__":
    unittest.main()
