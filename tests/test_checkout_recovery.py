"""Checkout recovery: ONE email to a client whose Stripe checkout expired unpaid.

Scrooge records an expired /hire checkout's Stripe recovery link on the order
(``expired_at``, ``recovery_url``, ``recovery_expires_at``) and lists it; the order desk
(``contracts.orders``) proposes the ONE recovery email - a fixed template with the order's
package and ``{recovery_link}``, which Scrooge fills with the order's own link - through
``client.email`` with ``kind: "recovery"``, parked for the owner's yes on Discord. Each test
fails if its rule is reverted: a second offer after a send or a denial, an offer to an order
that paid, a flagged brief or a link about to die, a payload whose shape Scrooge does not
take, a card that does not say what the email is, or a tally that does not count it.

The JSON contract (``tests/fixtures/scrooge_checkout_recovery_contract.json``) is the same
file as Scrooge's ``worker/test/fixtures/checkout-recovery-contract.json``; both suites pin
the sha256 of its canonical JSON, and Scrooge's suite runs the same order and request
through its real routes.
"""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path
from typing import Any

from test_client_adapter import _Case as _AdapterCase
from test_crew_orders import DAY, _Case

from pionir.adapters.clients import EMAIL, check_email
from pionir.crew import orders as desk
from pionir.crew.orders import RECOVERY, _epoch
from pionir.discord_gate import RECOVERY_LINE, render_request
from pionir.errors import AdapterProtocolError

CONTRACT_FILE = Path(__file__).parent / "fixtures" / "scrooge_checkout_recovery_contract.json"
# sha256 of the contract's canonical JSON (keys sorted, no spaces). Scrooge's
# worker/test/checkout-recovery.test.ts pins the same value: change both, or neither.
CONTRACT_SHA256 = "065dedb3ede947f2d324898e63b88d4c51936fe8f1270f88b10e0a33f817c4fc"
CONTRACT = json.loads(CONTRACT_FILE.read_text(encoding="utf-8"))
NOW = _epoch(CONTRACT["now"])
FLAGGED = "Scrape the email addresses of every member of a LinkedIn group into a sheet."


def expired_order(**over: Any) -> dict[str, Any]:
    """An order as Scrooge's GET /dash/orders lists it: checkout expired, link alive."""
    o = json.loads(json.dumps(CONTRACT["listed_order"]))
    o.update(over)
    return o


class ContractTests(unittest.TestCase):
    def test_the_contract_is_the_pinned_file(self) -> None:
        canonical = json.dumps(CONTRACT, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False)
        self.assertEqual(hashlib.sha256(canonical.encode("utf-8")).hexdigest(), CONTRACT_SHA256)

    def test_the_listed_order_has_every_field_the_desk_reads(self) -> None:
        o = CONTRACT["listed_order"]
        for key in ("id", "created_at", "name", "email", "package", "brief", "status",
                    "amount_cents", "paid_at", "expired_at", "recovery_url",
                    "recovery_expires_at", "recovery_sent_at", "messages"):
            self.assertIn(key, o)
        self.assertEqual(CONTRACT["recovery_email_request"]["kind"], desk.RECOVERY_KIND)
        self.assertIn(desk.RECOVERY_LINK, CONTRACT["recovery_email_request"]["body_text"])


class DeskTests(_Case):
    def test_it_proposes_exactly_the_contract_request_through_client_email(self) -> None:
        self.pionir.orders = [expired_order()]
        result = self.run_at(NOW)
        (job,) = self.pionir.emails()
        self.assertEqual(job.capability, EMAIL)
        self.assertEqual(job.payload, CONTRACT["recovery_email_request"])
        # the link is Scrooge's to fill: the desk never writes the Stripe URL into the email
        self.assertNotIn(CONTRACT["listed_order"]["recovery_url"], json.dumps(job.payload))
        # and the adapter takes it exactly as it is (the check before parking)
        self.assertEqual(check_email(job.payload), CONTRACT["recovery_email_request"])
        (entry,) = self.record()["emails"]
        self.assertEqual((entry["kind"], entry["status"]), (RECOVERY, "pending_approval"))
        t = self.tally(result)
        self.assertEqual((t["checkouts expired"],
                          t["recovery emails pending the owner's approval"]), (1, 1))

    def test_never_offered_again_once_pending_sent_or_denied(self) -> None:
        self.pionir.orders = [expired_order(), expired_order(id="b2b2b2b2b2b2")]
        self.run_at(NOW)
        self.assertEqual(len(self.pionir.emails()), 2)
        self.run_at(NOW + 600)                        # still pending: not again
        self.pionir.approve("em-1")
        self.pionir.deny("em-2")
        result = self.run_at(NOW + 1200)
        self.run_at(NOW + 1800)
        self.assertEqual(len(self.pionir.emails()), 2)
        self.assertEqual(self.pionir.statuses(), [])   # a recovery email moves no status
        t = self.tally(result)
        self.assertEqual((t["recovery emails approved and sent"], t["recovery emails denied"],
                          t["recovery emails pending the owner's approval"]), (1, 1, 0))

    def test_a_scrooge_record_of_a_recovery_email_closes_it_too(self) -> None:
        self.pionir.orders = [expired_order(recovery_sent_at="2026-10-04T09:00:00.000Z")]
        self.run_at(NOW)
        self.assertEqual(self.pionir.emails(), [])

    def test_not_offered_to_an_order_that_paid_or_moved_on(self) -> None:
        self.pionir.orders = [
            expired_order(id="a1a1a1a1a1a1", status="paid", paid_at="2026-10-03T20:00:00Z"),
            expired_order(id="b2b2b2b2b2b2", status="declined"),
            expired_order(id="c3c3c3c3c3c3", paid_at="2026-10-03T20:00:00Z"),
        ]
        self.run_at(NOW)
        # the paid one gets its acknowledgement, nothing else gets anything
        self.assertEqual([(j.payload["order_id"], j.payload.get("kind")) for j in
                          self.pionir.emails()], [("a1a1a1a1a1a1", None)])

    def test_not_offered_without_a_live_link_a_sold_package_or_a_clean_brief(self) -> None:
        soon = "2026-10-05T06:00:00.000Z"             # under RECOVERY_MIN_LEFT from NOW
        self.pionir.orders = [
            expired_order(id="a1a1a1a1a1a1", recovery_url=None, recovery_expires_at=None),
            expired_order(id="b2b2b2b2b2b2", recovery_expires_at=soon),
            expired_order(id="c3c3c3c3c3c3", recovery_expires_at="2026-10-01T00:00:00Z"),
            expired_order(id="d4d4d4d4d4d4", expired_at=None),
            expired_order(id="e5e5e5e5e5e5", amount_cents=100),
            expired_order(id="f6f6f6f6f6f6", package="custom", amount_cents=None),
            expired_order(id="a7a7a7a7a7a7", brief=FLAGGED),
            expired_order(id="b8b8b8b8b8b8", recovery_url="http://buy.stripe.com/r/x"),
        ]
        self.run_at(NOW)
        self.assertEqual(self.pionir.emails(), [])

    def test_a_find_order_gets_its_own_package_and_price(self) -> None:
        self.pionir.orders = [expired_order(package="find", amount_cents=1900)]
        self.run_at(NOW)
        (job,) = self.pionir.emails()
        self.assertIn("Package: Find it for me ($19)", job.payload["body_text"])
        self.assertIn("until 2026-11-02 (UTC)", job.payload["body_text"])

    def test_the_tally_counts_expired_sent_and_recovered(self) -> None:
        self.pionir.orders = [expired_order(),
                              expired_order(id="b2b2b2b2b2b2", status="paid",
                                            paid_at="2026-10-03T10:00:00Z"),
                              expired_order(id="c3c3c3c3c3c3", expired_at=None,
                                            recovery_url=None, recovery_expires_at=None)]
        self.run_at(NOW)
        self.pionir.approve("em-1")
        self.run_at(NOW + 600)
        # Scrooge now shows the recovery email sent, and then the order paid after it
        o = self.pionir.orders[0]
        o.update(recovery_sent_at="2026-10-04T12:10:00.000Z", status="paid",
                 paid_at="2026-10-04T15:00:00.000Z")
        result = self.run_at(NOW + DAY)
        t = self.tally(result)
        self.assertEqual(t["checkouts expired"], 2)
        self.assertEqual(t["recovery emails approved and sent"], 1)
        # b2 paid without a recovery email: not recovered
        self.assertEqual(t["orders recovered (paid after their recovery email)"], 1)
        self.assertEqual(t["recovered revenue (usd_cents)"], 14900)

    def test_a_payment_before_the_recovery_email_is_not_a_recovery(self) -> None:
        self.pionir.orders = [expired_order(status="paid", paid_at="2026-10-04T11:00:00Z",
                                            recovery_sent_at="2026-10-04T12:00:00Z")]
        t = self.tally(self.run_at(NOW))
        self.assertEqual(t["orders recovered (paid after their recovery email)"], 0)


class AdapterTests(_AdapterCase):
    def request(self, **over: Any) -> dict[str, Any]:
        r = dict(CONTRACT["recovery_email_request"])
        r.update(over)
        return {k: v for k, v in r.items() if v is not None}

    def test_an_approved_recovery_email_reaches_scrooge_with_its_kind_and_placeholder(self) -> None:
        self.world.orders = [expired_order()]
        row = self.send(self.request())
        self.assertEqual(row["status"], "approved", row)
        (call,) = self.world.calls
        self.assertEqual(call["url"], "https://api.dokaz.test/dash/orders/email")
        self.assertEqual(call["body"], CONTRACT["recovery_email_request"])

    def test_it_parks_like_every_client_email(self) -> None:
        out = self.app.run_task(EMAIL, self.request(), permissions=[EMAIL])
        self.assertEqual(out["status"], "pending_approval")
        self.assertEqual(self.world.calls, [])

    def test_refused_before_parking(self) -> None:
        body = CONTRACT["recovery_email_request"]["body_text"]
        bad = [
            self.request(kind="reminder"),
            self.request(kind=True),
            self.request(body_text=body.replace("{recovery_link}", "")),
            self.request(body_text=body + "\nAgain: {recovery_link}"),
            self.request(subject="Pay here {recovery_link}"),
            self.request(kind=None),                      # the placeholder in a plain email
            self.request(body_text=body.replace("{recovery_link}",
                                                "https://buy.stripe.com/r/live_x")),
        ]
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(AdapterProtocolError):
                self.adapter.validate(self.adapter_task(payload))
        self.assertEqual(self.world.calls, [])

    def adapter_task(self, payload: dict[str, Any]):
        from pionir.contracts import Task
        return Task(EMAIL, payload)

    def test_a_plain_email_is_unchanged(self) -> None:
        from test_client_adapter import message
        self.assertNotIn("kind", check_email(message()))

    def test_the_card_says_it_is_the_one_recovery_email_and_shows_the_placeholder(self) -> None:
        row = {"id": "ap-1", "capability": EMAIL, "payload": self.request(),
               "summary": "email the client of order a1b2c3d4e5f6 the checkout recovery"}
        text = render_request(row, "123")
        self.assertIn(RECOVERY_LINE, text)
        self.assertIn("{recovery_link}", text)
        self.assertIn("Package: Small ($149)", text)
        plain = render_request({**row, "payload": self.request(kind=None, body_text=(
            "Hello Ana Lima,\n\nA plain note about your order.\n\nThank you,\nDokaz"))}, "123")
        self.assertNotIn(RECOVERY_LINE, plain)


if __name__ == "__main__":
    unittest.main()
