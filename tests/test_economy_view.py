"""The Bolts dashboard payload: read-only, zero shown as zero, a broken chain shown as broken.

Each test fails if its rule is reverted: an empty economy that read as an error (or created
files on a GET); a tampered ledger shown as OK; refused counts or caps hidden; a payload
that drifted into offering a write path.
"""
import json
import re
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from pionir.economy import payouts
from pionir.economy.ledger import Ledger
from pionir.economy.payouts import Event, RefusalLog, pay
from pionir.economy.view import economy_payload

T0 = datetime(2026, 9, 30, 12, 0, 0).timestamp()
SRC = Path(__file__).resolve().parents[1] / "src" / "pionir"


class ViewCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name) / "economy"
        self.now = lambda: T0
        self.ledger = Ledger.in_dir(self.dir, now=self.now)
        self.refusals = RefusalLog.in_dir(self.dir, now=self.now)

    def pay(self, kind: str, n: int, account: str, **kw):
        return pay(Event(kind, f"{kind}:{n}", account, T0, **kw), self.ledger, self.refusals)

    def payload(self, **kw) -> dict:
        return economy_payload(self.dir, now=self.now, **kw)


class EmptyTests(ViewCase):
    def test_nothing_paid_is_zero_everywhere_and_the_chain_is_ok(self) -> None:
        p = self.payload()
        self.assertEqual(p["chain"]["label"], "OK")
        self.assertEqual((p["chain"]["rows"], p["total_supply"]), (0, 0))
        self.assertEqual((p["balances"], p["recent"]), ([], []))
        self.assertEqual(p["today"]["minted"], 0)
        self.assertEqual(p["refused"]["total"], 0)
        self.assertEqual(p["caps"], {"per_account": payouts.DAILY_CAP_PER_ACCOUNT,
                                     "global": payouts.DAILY_CAP_GLOBAL})
        self.assertTrue(p["play_currency"])

    def test_asking_creates_nothing_on_disk(self) -> None:
        self.payload()
        self.assertFalse(self.dir.exists())


class FundedTests(ViewCase):
    def test_balances_recent_rows_and_today_are_reported(self) -> None:
        self.pay("build_staged", 1, "builds.daedalus")
        self.pay("post_approved", 2, "posting.blog", owner_edited=False)
        self.pay("order_delivered", 3, "fiverr.desk")
        p = self.payload()
        self.assertEqual([(b["account"], b["balance"]) for b in p["balances"]],
                         [("fiverr.desk", 30), ("builds.daedalus", 25), ("posting.blog", 15)])
        self.assertEqual(p["total_supply"], 70)
        self.assertEqual(p["today"]["minted"], 70)
        self.assertEqual([r["event_id"] for r in p["recent"]],
                         ["order_delivered:3", "post_approved:2", "build_staged:1"])
        self.assertEqual(p["chain"]["label"], "OK")
        self.assertEqual(self.payload(recent=1)["recent"][0]["event_id"], "order_delivered:3")

    def test_refused_counts_by_reason_are_shown(self) -> None:
        for n in range(5):
            self.pay("build_staged", n, "builds.daedalus")        # 5th is over the 100 cap
        p = self.payload()
        self.assertEqual(p["refused"]["total"], 1)
        self.assertEqual(p["refused"]["by_reason"], {"account_daily_cap": 1})
        self.assertEqual(p["today"]["per_account"],
                         [{"account": "builds.daedalus", "minted": 100}])

    def test_the_payout_table_is_shown(self) -> None:
        self.assertEqual(self.payload()["payouts"]["build_staged"]["base"], 25)


class BrokenTests(ViewCase):
    def test_a_tampered_ledger_is_shown_as_broken_at_its_seq(self) -> None:
        for n in range(4):
            self.pay("order_delivered", n, "fiverr.desk")
        lines = self.ledger.path.read_text(encoding="ascii").splitlines()
        row = json.loads(lines[2])
        row["delta"] = 9999
        lines[2] = json.dumps(row, sort_keys=True, separators=(",", ":"))
        self.ledger.path.write_text("\n".join(lines) + "\n", encoding="ascii")
        p = self.payload()
        self.assertFalse(p["chain"]["ok"])
        self.assertEqual(p["chain"]["label"], "BROKEN at seq 2")
        self.assertEqual(p["chain"]["first_bad_seq"], 2)

    def test_a_torn_tail_is_flagged(self) -> None:
        self.pay("order_delivered", 1, "fiverr.desk")
        with self.ledger.path.open("ab") as fh:
            fh.write(b'{"seq":1,"ts"')
        p = self.payload()
        self.assertEqual(p["chain"]["label"], "TORN TAIL")
        self.assertTrue(p["chain"]["torn"])


class ReadOnlyTests(unittest.TestCase):
    def test_viewing_does_not_change_the_ledger_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            led = Ledger.in_dir(d, now=lambda: T0)
            led.append("a", 5, "r", "e1")
            before = {p.name: p.read_bytes() for p in d.iterdir()}
            economy_payload(d, now=lambda: T0)
            self.assertEqual({p.name: p.read_bytes() for p in d.iterdir()}, before)

    def test_the_server_exposes_a_get_route_and_no_write_route(self) -> None:
        text = (SRC / "server.py").read_text(encoding="utf-8")
        routes = re.findall(r'route\.path == "(/api/economy[^"]*)"', text)
        self.assertEqual(routes, ["/api/economy"])
        start = text.index('route.path == "/api/economy"')
        before = text[:start]
        self.assertGreater(before.rfind("def do_GET"), before.rfind("def do_POST"))

    def test_the_dashboard_tab_never_posts(self) -> None:
        html = (SRC / "web" / "dashboard.html").read_text(encoding="utf-8")
        body = html[html.index("function pollBolts"):]
        body = body[:body.index("\n}\n") + 3]
        self.assertIn("/api/economy", body)
        self.assertNotRegex(body, r"POST|method\s*:|PUT|DELETE")


if __name__ == "__main__":
    unittest.main()
