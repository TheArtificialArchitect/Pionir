"""The order desk: every client order answered by fixed template, for the owner's yes.

Pionir is a fake (``FakePionir``): it lists the orders, parks every email under its own
approval id, and runs a status update at once. Each test fails if the rule it names is
reverted: an acknowledgement for a flagged brief, a wrong recipient, amount or turnaround,
the status moved before the email is sent, more than two emails a run, an email sent twice
or resent after a denial, an unreachable Pionir retried for ever (or never), a check that
does not fail closed, or orders that could not be read reported as none.
"""

import copy
import json
import re
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

from pionir.crew import orders as desk
from pionir.crew.hands import JobOutcome
from pionir.crew.orders import (
    ACK,
    DECLINE,
    EMAIL,
    ORDERS,
    QUOTE_ACK,
    RETRY_UNREACHABLE,
    SET_STATUS,
    OrderDesk,
    build_email,
    check_email,
    screen,
)
from pionir.crew.registry import default_registry
from pionir.crew.result import Err, Ok
from pionir.crew.worker import ErrorKind, WorkContext

T0 = 1_790_000_000.0
DAY = 86400.0
CLEAN = "A script that renames the invoice PDFs in a folder by their date."


def order(oid="ord_1", status="paid", package="small", amount=14900, brief=CLEAN,
          name="Ana Lima", email="ana@example.com", created_at=T0 - 3600, messages=None):
    return {"id": oid, "created_at": created_at, "name": name, "email": email,
            "package": package, "brief": brief, "status": status, "amount_cents": amount,
            "paid_at": created_at if status != "awaiting_payment" else None,
            "messages": messages or []}


class FakePionir:
    """``ctx.job`` and ``ctx.approval``: orders listed, emails parked, statuses set."""

    def __init__(self, *orders) -> None:
        self.orders = list(orders)
        self.jobs: list = []
        self.approvals: dict = {}
        self.orders_outcome = None
        self.email_outcome = None
        self.status_outcome = None

    def job(self, job):
        self.jobs.append(job)
        if job.capability == ORDERS:
            if self.orders_outcome is not None:
                return self.orders_outcome
            return JobOutcome("done", ORDERS, task_id="t-o",
                              result={"ok": True, "orders": copy.deepcopy(self.orders)})
        if job.capability == EMAIL:
            if self.email_outcome is not None:
                return self.email_outcome
            n = len(self.emails())
            return JobOutcome("pending_approval", EMAIL, task_id=f"t-{n}",
                              approval_id=f"em-{n}")
        if job.capability == SET_STATUS:
            if self.status_outcome is not None:
                return self.status_outcome
            for o in self.orders:
                if o["id"] == job.payload["order_id"]:
                    o["status"] = job.payload["status"]
            return JobOutcome("done", SET_STATUS, result={"ok": True})
        raise AssertionError(f"unexpected capability {job.capability}")

    def approval(self, approval_id):
        return dict(self.approvals.get(approval_id, {"status": "pending"}))

    def approve(self, approval_id) -> None:
        self.approvals[approval_id] = {"id": approval_id, "status": "approved", "result": {
            "ok": True, "agent_id": "client", "result": {"ok": True, "sent": True}}}

    def deny(self, approval_id) -> None:
        self.approvals[approval_id] = {"id": approval_id, "status": "denied", "reason": "no"}

    def emails(self) -> list:
        return [j for j in self.jobs if j.capability == EMAIL]

    def statuses(self) -> list:
        return [j.payload for j in self.jobs if j.capability == SET_STATUS]

    def status_of(self, oid) -> str:
        return next(o["status"] for o in self.orders if o["id"] == oid)


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.worker = default_registry().require("contracts.orders")
        self.pionir = FakePionir()

    def run_at(self, now=T0):
        ctx = WorkContext(now=now, http=None, secrets_dir=self.state, job=self.pionir.job,
                          approval=self.pionir.approval, state_dir=self.state)
        return self.worker.run(ctx)

    def record(self) -> dict:
        return json.loads(self.worker.record_path(self.state).read_text(encoding="utf-8"))

    @staticmethod
    def tally(result) -> dict:
        (t,) = [o for o in result.value if o.kind == "order.tally"]
        out = {}
        for f in t.figures:
            out[f.measures if f.unit == "count" else f"{f.measures} ({f.unit})"] = f.value
        return out

    @staticmethod
    def tally_payload(result) -> dict:
        (t,) = [o for o in result.value if o.kind == "order.tally"]
        return t.payload


class CatalogueTests(unittest.TestCase):
    def test_the_order_desk_is_the_contracts_division_every_fifteen_minutes(self) -> None:
        reg = default_registry()
        w = reg.require("contracts.orders")
        self.assertIsInstance(w, OrderDesk)
        self.assertEqual((w.division, w.cadence_seconds, w.live), ("contracts", 900, True))
        notes = reg.division("contracts").leader_notes
        self.assertIn("NEW PAID ORDERS", notes)
        self.assertIn("Never promise", notes)
        self.assertIn("need the owner", notes)
        self.assertIn("Stripe", reg.division("contracts").entities)


class AcknowledgementTests(_Case):
    def test_a_paid_clean_order_gets_one_acknowledgement_then_in_progress_only_once_sent(
            self) -> None:
        self.pionir.orders = [order()]
        result = self.run_at(T0)
        self.assertIsInstance(result, Ok)
        (job,) = self.pionir.emails()
        self.assertEqual(job.permissions, ())             # nothing granted: Pionir parks it
        p = job.payload
        self.assertEqual(set(p), {"order_id", "to", "subject", "body_text"})
        self.assertEqual((p["order_id"], p["to"]), ("ord_1", "ana@example.com"))
        self.assertEqual(p["subject"], "Your Dokaz order ord_1 is confirmed")
        body = p["body_text"]
        self.assertTrue(body.startswith("Hello Ana Lima,\n"), body[:40])
        for words in ("Order: ord_1", "Package: Small ($149)", "one script or automation",
                      "3 business days from your payment", "by email only",
                      "One round of revisions is included free", "full refund"):
            self.assertIn(words, body)
        self.assertTrue(body.endswith("\nDokaz"))
        self.assertEqual(re.findall(r"\$\d+", body), ["$149"])
        # parked, not sent: the order is NOT moved on
        self.assertEqual(self.pionir.statuses(), [])
        self.assertEqual(self.pionir.status_of("ord_1"), "paid")
        (entry,) = self.record()["emails"]
        self.assertEqual((entry["kind"], entry["status"], entry["approval_id"]),
                         (ACK, "pending_approval", "em-1"))
        t = self.tally(result)
        self.assertEqual(t["client emails pending the owner's approval"], 1)
        self.assertEqual(t["paid orders waiting for acknowledgement"], 1)
        self.assertEqual(t["acknowledgements sent"], 0)
        # still waiting: nothing sent again, nothing moved
        self.run_at(T0 + 900)
        self.assertEqual((len(self.pionir.emails()), self.pionir.statuses()), (1, []))
        # the owner says yes and it is sent: now, and only now, in_progress
        self.pionir.approve("em-1")
        result = self.run_at(T0 + 1800)
        self.assertEqual(self.pionir.statuses(), [{"order_id": "ord_1",
                                                   "status": "in_progress"}])
        self.assertEqual(self.pionir.status_of("ord_1"), "in_progress")
        kinds = [o.kind for o in result.value]
        self.assertIn("order.email_sent", kinds)
        self.assertIn("order.status_set", kinds)
        t = self.tally(result)
        self.assertEqual((t["acknowledgements sent"], t["paid orders waiting for acknowledgement"],
                          t["orders with status in_progress"]), (1, 0, 1))
        self.run_at(T0 + 2700)
        self.assertEqual((len(self.pionir.emails()), len(self.pionir.statuses())), (1, 1))

    def test_a_standard_order_states_its_own_price_and_turnaround(self) -> None:
        self.pionir.orders = [order(package="Standard", amount=39900)]
        self.run_at()
        body = self.pionir.emails()[0].payload["body_text"]
        self.assertIn("Package: Standard ($399)", body)
        self.assertIn("a small tool with a simple interface", body)
        self.assertIn("5 business days from your payment", body)
        self.assertEqual(re.findall(r"\$\d+", body), ["$399"])

    def test_a_denied_acknowledgement_is_not_resent_and_the_order_not_moved(self) -> None:
        self.pionir.orders = [order()]
        self.run_at(T0)
        self.pionir.deny("em-1")
        result = self.run_at(T0 + 900)
        self.assertEqual(self.record()["emails"][0]["status"], "denied")
        self.assertEqual(self.tally(result)["client emails denied"], 1)
        for i in range(3):
            self.run_at(T0 + (i + 2) * 900)
        self.assertEqual((len(self.pionir.emails()), self.pionir.statuses()), (1, []))

    def test_a_paid_order_the_templates_do_not_cover_is_held_not_emailed(self) -> None:
        self.pionir.orders = [order("ord_a", amount=9900),
                              order("ord_b", package="custom", amount=120000),
                              order("ord_c", brief="   ")]
        result = self.run_at()
        self.assertEqual(self.pionir.emails(), [])
        t = self.tally(result)
        self.assertEqual(t["paid orders held for the owner"], 3)
        held = {h["order_id"]: h["why"] for h in self.tally_payload(result)["held"]}
        self.assertIn("not the Small price", held["ord_a"])
        self.assertIn("custom", held["ord_b"])
        self.assertIn("no brief", held["ord_c"])

    def test_a_name_that_is_not_a_plain_name_is_not_put_in_the_email(self) -> None:
        for name in ("<b>Bob</b>", "Bob https://evil.example", "Win $500 now", "", None):
            email = build_email(ACK, order(name=name))
            self.assertTrue(email["body_text"].startswith("Hello there,\n"), name)
            self.assertEqual(check_email(email, order(name=name), 14900), [], name)
        email = build_email(ACK, order(name="José  O'Neil-Smith"))
        self.assertTrue(email["body_text"].startswith("Hello José O'Neil-Smith,\n"))


class QuoteTests(_Case):
    def test_a_quote_request_gets_the_quote_acknowledgement_and_keeps_its_status(self) -> None:
        self.pionir.orders = [order(status="quote_requested", package="custom", amount=None)]
        result = self.run_at(T0)
        (job,) = self.pionir.emails()
        p = job.payload
        self.assertEqual(p["subject"], "We've received your Dokaz request ord_1")
        self.assertIn("reply with a quote within 2 business days", p["body_text"])
        self.assertIn("nothing to pay until you accept the quote", p["body_text"])
        self.assertNotIn("$", p["body_text"])
        self.assertEqual(self.tally(result)["quote requests waiting for the owner"], 1)
        self.pionir.approve("em-1")
        result = self.run_at(T0 + 900)
        self.assertEqual(self.pionir.statuses(), [])      # the quote is the owner's to make
        self.assertEqual(self.tally(result)["quote acknowledgements sent"], 1)
        self.assertEqual(self.tally(result)["quote requests waiting for the owner"], 1)
        self.run_at(T0 + 1800)
        self.assertEqual(len(self.pionir.emails()), 1)


class DeclineTests(_Case):
    SCRAPE = "Please scrape emails from LinkedIn profiles into a spreadsheet."

    def test_a_flagged_paid_brief_gets_a_decline_never_an_acknowledgement(self) -> None:
        self.pionir.orders = [order(brief=self.SCRAPE)]
        result = self.run_at(T0)
        (job,) = self.pionir.emails()
        p = job.payload
        self.assertEqual(p["subject"], "About your Dokaz request ord_1")
        self.assertNotIn("confirmed", p["subject"])
        body = p["body_text"]
        self.assertIn("collecting people's personal data", body)
        self.assertIn("You'll receive a full refund of your payment.", body)
        self.assertIn("If we've misread your brief, reply to this email and tell us more.", body)
        self.assertNotIn("$", body)
        (entry,) = self.record()["emails"]
        self.assertEqual((entry["kind"], entry["flags"]), (DECLINE, ["personal_data"]))
        pending = [o for o in result.value if o.kind == "order.email_pending"]
        self.assertEqual(pending[0].payload["flags"], ["personal_data"])
        t = self.tally(result)
        self.assertEqual((t["flagged orders awaiting the owner"],
                          t["paid orders waiting for acknowledgement"],
                          t["refunds to issue by hand in Stripe"]), (1, 0, 0))
        self.assertEqual(self.pionir.statuses(), [])
        # sent: declined, and a refund for the owner to issue by hand - never automated
        self.pionir.approve("em-1")
        result = self.run_at(T0 + 900)
        self.assertEqual(self.pionir.statuses(), [{"order_id": "ord_1", "status": "declined"}])
        t = self.tally(result)
        self.assertEqual((t["declines sent"], t["refunds to issue by hand in Stripe"],
                          t["refunds to issue by hand in Stripe (usd_cents)"],
                          t["flagged orders awaiting the owner"],
                          t["acknowledgements sent"]), (1, 1, 14900, 0, 0))
        self.assertEqual(self.tally_payload(result)["refunds_to_issue"], ["ord_1"])
        self.assertEqual([j.capability for j in self.pionir.jobs].count(EMAIL), 1)
        # the owner refunds it: no longer a refund to issue
        self.pionir.orders[0]["status"] = "refunded"
        result = self.run_at(T0 + 1800)
        self.assertEqual(self.tally(result)["refunds to issue by hand in Stripe"], 0)
        self.assertEqual(len(self.pionir.emails()), 1)

    def test_a_flagged_quote_request_is_declined_without_a_refund(self) -> None:
        self.pionir.orders = [order(status="quote_requested", package="custom", amount=None,
                                    brief="A bot to auto-like posts on Instagram.")]
        self.run_at(T0)
        body = self.pionir.emails()[0].payload["body_text"]
        self.assertIn("bots or automated accounts on social platforms", body)
        self.assertIn("You haven't been charged anything.", body)
        self.assertNotIn("refund", body)
        self.pionir.approve("em-1")
        result = self.run_at(T0 + 900)
        self.assertEqual(self.pionir.statuses(), [{"order_id": "ord_1", "status": "declined"}])
        self.assertEqual(self.tally(result)["refunds to issue by hand in Stripe"], 0)

    def test_a_denied_decline_is_left_to_the_owner(self) -> None:
        self.pionir.orders = [order(brief=self.SCRAPE)]
        self.run_at(T0)
        self.pionir.deny("em-1")
        result = self.run_at(T0 + 900)
        self.run_at(T0 + 1800)
        self.assertEqual((len(self.pionir.emails()), self.pionir.statuses()), (1, []))
        t = self.tally(result)
        self.assertEqual((t["flagged orders awaiting the owner"], t["declines sent"]), (1, 0))


class WhichOrdersTests(_Case):
    def test_awaiting_payment_gets_nothing_and_old_ones_are_abandoned(self) -> None:
        self.pionir.orders = [order("ord_new", status="awaiting_payment", created_at=T0 - DAY),
                              order("ord_old", status="awaiting_payment",
                                    created_at="2026-09-01T10:00:00Z"),
                              order("ord_bad", status="awaiting_payment",
                                    brief=DeclineTests.SCRAPE, created_at=T0 - 5 * DAY)]
        result = self.run_at(1_790_000_000.0)       # 2026-09-21
        self.assertEqual(self.pionir.emails(), [])
        t = self.tally(result)
        self.assertEqual((t["orders with status awaiting_payment"], t["abandoned checkouts"]),
                         (3, 2))

    def test_orders_the_owner_already_moved_on_get_nothing(self) -> None:
        self.pionir.orders = [order(f"ord_{s}", status=s) for s in
                              ("in_progress", "delivered", "declined", "refunded", "quoted")]
        result = self.run_at()
        self.assertEqual(self.pionir.emails(), [])
        self.assertEqual(self.tally(result)["paid revenue (usd_cents)"], 2 * 14900)

    def test_at_most_two_emails_a_run_oldest_first(self) -> None:
        self.pionir.orders = [order("ord_c", created_at=T0 - 100),
                              order("ord_a", created_at=T0 - 300),
                              order("ord_b", created_at=T0 - 200)]
        result = self.run_at(T0)
        self.assertEqual([j.payload["order_id"] for j in self.pionir.emails()],
                         ["ord_a", "ord_b"])
        self.assertEqual(self.tally_payload(result)["emails_ready_next_run"], 1)
        self.run_at(T0 + 900)
        self.assertEqual([j.payload["order_id"] for j in self.pionir.emails()],
                         ["ord_a", "ord_b", "ord_c"])
        self.run_at(T0 + 1800)
        self.assertEqual(len(self.pionir.emails()), 3)

    def test_a_new_paid_order_is_reported_once_with_its_amount(self) -> None:
        self.pionir.orders = [order()]
        first = self.run_at(T0)
        (new,) = [o for o in first.value if o.kind == "order.new_paid"]
        self.assertEqual(new.payload, {"order_id": "ord_1", "package": "small"})
        self.assertEqual([(f.value, f.unit) for f in new.figures], [(14900, "usd_cents")])
        self.assertFalse(new.derived)
        self.assertNotIn("Ana", json.dumps(new.payload))
        again = self.run_at(T0 + 900)
        self.assertEqual([o for o in again.value if o.kind == "order.new_paid"], [])
        self.assertEqual(self.tally(again)["paid revenue (usd_cents)"], 14900)


class NeverTwiceTests(_Case):
    def test_an_unreachable_pionir_is_retried_then_given_up(self) -> None:
        self.pionir.orders = [order()]
        self.pionir.email_outcome = JobOutcome("unreachable", EMAIL, error="connection refused")
        for i in range(RETRY_UNREACHABLE):
            self.run_at(T0 + i * 900)
        self.assertEqual(len(self.pionir.emails()), RETRY_UNREACHABLE)   # retried, not dropped
        result = self.run_at(T0 + RETRY_UNREACHABLE * 900)
        self.assertEqual(len(self.pionir.emails()), RETRY_UNREACHABLE)   # and then it stops
        self.assertEqual(self.tally(result)["client emails failed"], 1)

    def _approved_but_failed(self, approval_id: str, inner: dict) -> None:
        self.pionir.approvals[approval_id] = {"id": approval_id, "status": "approved_failed",
                                              "result": {"ok": False, "agent_id": "client",
                                                         "result": inner}}

    def test_an_approved_email_that_hit_an_outage_is_offered_again_then_stops(self) -> None:
        # The phase 4a end-to-end run: the owner approved the acknowledgement while mail was
        # down (Scrooge 503) and the desk recorded it as final - a paid client never
        # acknowledged. It is now offered to the owner again, a bounded number of times.
        from pionir.crew.orders import RETRY_UNDELIVERED
        self.pionir.orders = [order()]
        down = {"ok": False, "unavailable": "Scrooge answered HTTP 503 - nothing was sent"}
        for i in range(RETRY_UNDELIVERED):
            self.run_at(T0 + i * 1800)
            self._approved_but_failed(f"em-{i + 1}", down)
        self.run_at(T0 + RETRY_UNDELIVERED * 1800)
        self.assertEqual(len(self.pionir.emails()), RETRY_UNDELIVERED)
        self.run_at(T0 + (RETRY_UNDELIVERED + 1) * 1800)
        self.assertEqual(len(self.pionir.emails()), RETRY_UNDELIVERED)   # and then it stops

    def test_an_approved_email_that_was_refused_is_final(self) -> None:
        self.pionir.orders = [order()]
        self.run_at(T0)
        self._approved_but_failed("em-1", {"ok": False,
                                           "refused": "to: does not match the order"})
        self.run_at(T0 + 900)
        self.run_at(T0 + 1800)
        self.assertEqual(len(self.pionir.emails()), 1)
        self.assertEqual(self.record()["emails"][0]["status"], "failed")

    def test_a_reported_failure_the_order_shows_sent_is_sent(self) -> None:
        self.pionir.orders = [order()]
        self.run_at(T0)
        subject = self.pionir.emails()[0].payload["subject"]
        self.pionir.orders[0]["messages"] = [{"at": "2026-09-25T10:00:00Z", "subject": subject}]
        self._approved_but_failed("em-1", {"ok": False, "unavailable": "HTTP 0"})
        self.run_at(T0 + 900)
        self.assertEqual(self.record()["emails"][0]["status"], "sent")
        self.assertEqual(len(self.pionir.emails()), 1)

    def test_pionir_back_up_gets_the_email_once(self) -> None:
        self.pionir.orders = [order()]
        self.pionir.email_outcome = JobOutcome("unreachable", EMAIL, error="down")
        self.run_at(T0)
        self.pionir.email_outcome = None
        self.run_at(T0 + 900)
        self.run_at(T0 + 1800)
        self.assertEqual(len(self.pionir.emails()), 2)
        self.assertEqual(self.record()["emails"][-1]["status"], "pending_approval")

    def test_a_failed_or_running_email_is_never_resent(self) -> None:
        for outcome in (JobOutcome("failed", EMAIL, error="refused"),
                        JobOutcome("running", EMAIL, task_id="t-9")):
            with self.subTest(outcome.status):
                self.setUp()
                self.pionir.orders = [order()]
                self.pionir.email_outcome = outcome
                self.run_at(T0)
                self.run_at(T0 + 900)
                self.assertEqual(len(self.pionir.emails()), 1)
                self.assertEqual(self.pionir.statuses(), [])

    def test_an_acknowledged_order_is_never_declined_even_if_the_screens_change(self) -> None:
        self.pionir.orders = [order()]
        self.run_at(T0)
        self.pionir.approve("em-1")
        self.pionir.orders[0]["status"] = "paid"         # say the status update is still due
        self.pionir.status_outcome = JobOutcome("unreachable", SET_STATUS, error="down")
        self.run_at(T0 + 900)
        # the owner widens the screening list: this brief would now be flagged
        with mock.patch.object(desk, "screen", lambda brief: [desk.SCREENS[0]]):
            self.run_at(T0 + 1800)
        self.assertEqual([j.payload["subject"] for j in self.pionir.emails()],
                         ["Your Dokaz order ord_1 is confirmed"])

    def test_an_email_the_orders_messages_already_show_is_not_sent_again(self) -> None:
        subject = "Your Dokaz order ord_1 is confirmed"
        self.pionir.orders = [order(messages=[{"at": T0 - 60, "subject": subject}])]
        result = self.run_at(T0)
        self.assertEqual(self.pionir.emails(), [])
        self.assertEqual(self.record()["emails"][0]["status"], "sent")
        self.assertEqual(self.pionir.statuses(), [{"order_id": "ord_1",
                                                   "status": "in_progress"}])
        self.assertEqual(self.tally(result)["acknowledgements sent"], 1)

    def test_a_status_update_that_fails_is_retried_and_one_the_owner_moved_is_left(
            self) -> None:
        self.pionir.orders = [order("ord_1"), order("ord_2", created_at=T0 - 60)]
        self.run_at(T0)
        self.pionir.approve("em-1")
        self.pionir.approve("em-2")
        self.pionir.orders[1]["status"] = "refunded"      # the owner moved it on himself
        self.pionir.status_outcome = JobOutcome("unreachable", SET_STATUS, error="down")
        self.run_at(T0 + 900)
        self.assertEqual(self.pionir.statuses(), [{"order_id": "ord_1",
                                                   "status": "in_progress"}])
        self.pionir.status_outcome = None
        self.run_at(T0 + 1800)
        self.assertEqual(self.pionir.statuses(), [{"order_id": "ord_1", "status": "in_progress"}]
                         * 2)
        self.assertEqual((self.pionir.status_of("ord_1"), self.pionir.status_of("ord_2")),
                         ("in_progress", "refunded"))
        self.run_at(T0 + 2700)
        self.assertEqual(len(self.pionir.statuses()), 2)


class TemplateCheckTests(_Case):
    def test_every_template_passes_its_own_check(self) -> None:
        for kind, o, cents in ((ACK, order(), 14900),
                               (ACK, order(package="standard", amount=39900), 39900),
                               (QUOTE_ACK, order(status="quote_requested"), None),
                               (DECLINE, order(brief=DeclineTests.SCRAPE), None),
                               (DECLINE, order(status="quote_requested",
                                               brief=DeclineTests.SCRAPE), None)):
            flags = tuple(screen(o["brief"]))
            self.assertEqual(check_email(build_email(kind, o, flags), o, cents), [], kind)

    def test_the_check_fails_closed(self) -> None:
        o = order()
        good = build_email(ACK, o)
        cases = {
            "links somewhere other": dict(good, body_text=good["body_text"]
                                          + "\nSee https://evil.example/x"),
            "names an address or a domain": dict(good, body_text=good["body_text"]
                                                 + "\nWrite to help@dokaz.net or evil.io"),
            "not the order's package price": dict(good, body_text=good["body_text"].replace(
                "$149", "$99")),
            "not plain text": dict(good, body_text=good["body_text"] + "\n<b>hi</b>"),
            "the recipient": dict(good, to="someone@else.com"),
            "single line": dict(good, subject="Your order\nBcc: x"),
            "5 to 120": dict(good, subject="Hi"),
            "20 to 5000": dict(good, body_text="short"),
            "exactly order_id": dict(good, cc="x@y.com"),
        }
        for words, payload in cases.items():
            reasons = check_email(payload, o, 14900)
            self.assertTrue(any(words in r for r in reasons), (words, reasons))
        # the one link allowed is the hire page; no amount at all where none is allowed
        hire = dict(good, body_text=good["body_text"] + "\nhttps://api.dokaz.net/hire.")
        self.assertEqual(check_email(hire, o, 14900), [])
        self.assertTrue(any("not allowed" in r for r in check_email(good, o, None)))
        euro = dict(good, body_text=good["body_text"].replace("$149", "€149"))
        self.assertTrue(check_email(euro, o, 14900))

    def test_a_template_that_fails_the_check_is_blocked_recorded_and_never_retried(
            self) -> None:
        self.pionir.orders = [order()]
        bad = desk.ACK_BODY + "\nMore at https://evil.example and only $99 extra."
        with mock.patch.object(desk, "ACK_BODY", bad):
            result = self.run_at(T0)
            self.run_at(T0 + 900)
        self.assertEqual(self.pionir.emails(), [])
        (entry,) = self.record()["emails"]
        self.assertEqual(entry["status"], "blocked")
        self.assertTrue(any("evil.example" in r for r in entry["reasons"]), entry["reasons"])
        self.assertTrue(any("$99" in r for r in entry["reasons"]), entry["reasons"])
        self.assertIn("order.email_blocked", [o.kind for o in result.value])
        self.assertEqual(self.tally(result)["client emails blocked by the template check"], 1)


class ScreenTests(unittest.TestCase):
    FLAGGED: ClassVar[dict] = {
        "Please scrape emails from LinkedIn profiles.": "personal_data",
        "Build me a list of phone numbers for dentists in my area.": "personal_data",
        "Crawl a directory site and collect the names and addresses.": "personal_data",
        "Something that solves the captcha on a ticket site.": "bypass",
        "Bypass the rate limit on their API with rotating proxies.": "bypass",
        "A tool to log in to my clients' accounts with their passwords.": "credentials",
        "Auto-follow bot for TikTok.": "social_bots",
        "Move money from my bank account to crypto wallets each week.": "money",
        "A sports betting odds tracker.": "gambling",
        "Send cold emails to 5000 shops.": "spam",
        "Hack into my ex's account.": "harm",
    }
    CLEAN = (CLEAN, "Merge two CSV files into one report and email it to me every Monday.",
             "Resize the photos in a folder to 1200 pixels wide.",
             "A small tool with a form that turns my notes into an invoice PDF.",
             "Extract the totals from the invoice emails I receive into a spreadsheet.")

    def test_every_decline_category_is_flagged(self) -> None:
        for brief, key in self.FLAGGED.items():
            self.assertIn(key, [s.key for s in screen(brief)], brief)

    def test_ordinary_briefs_are_not_flagged(self) -> None:
        for brief in self.CLEAN:
            self.assertEqual(screen(brief), [], brief)

    def test_every_category_has_plain_words_and_a_pattern(self) -> None:
        keys = [s.key for s in desk.SCREENS]
        self.assertEqual(len(keys), len(set(keys)))
        for s in desk.SCREENS:
            self.assertTrue(s.plain and s.patterns, s.key)
            self.assertNotRegex(s.plain, r"[<>$]")


class UnavailableTests(_Case):
    def test_orders_that_could_not_be_read_are_not_zero_orders(self) -> None:
        cases = ((JobOutcome("unreachable", ORDERS, error="connection refused"),
                  ErrorKind.UNAVAILABLE),
                 (JobOutcome("failed", ORDERS, error="client.orders is not configured"),
                  ErrorKind.NOT_CONFIGURED),
                 (JobOutcome("failed", ORDERS, error="unknown capability client.orders"),
                  ErrorKind.NOT_CONFIGURED),
                 (JobOutcome("done", ORDERS, result={"ok": True}), ErrorKind.MALFORMED),
                 (JobOutcome("done", ORDERS, result={"ok": True, "orders": [
                     {"id": "a"}, {"id": "a"}]}), ErrorKind.MALFORMED))
        for outcome, kind in cases:
            with self.subTest(kind=kind, error=outcome.error):
                self.pionir.orders = [order()]
                self.pionir.orders_outcome = outcome
                self.pionir.jobs.clear()
                result = self.run_at()
                self.assertIsInstance(result, Err)
                self.assertEqual(result.error.kind, kind)
                self.assertNotIn("order.tally", str(result))
                self.assertEqual(self.pionir.emails(), [])

    def test_an_unreadable_orders_run_does_not_follow_up_or_move_anything(self) -> None:
        self.pionir.orders = [order()]
        self.run_at(T0)
        self.pionir.approve("em-1")
        self.pionir.orders_outcome = JobOutcome("unreachable", ORDERS, error="down")
        self.run_at(T0 + 900)
        self.assertEqual(self.record()["emails"][0]["status"], "pending_approval")
        self.assertEqual(self.pionir.statuses(), [])
        self.pionir.orders_outcome = None
        self.run_at(T0 + 1800)
        self.assertEqual(self.pionir.statuses(), [{"order_id": "ord_1",
                                                   "status": "in_progress"}])

    def test_no_state_dir_no_hands_or_an_unreadable_record_refuse(self) -> None:
        r = self.worker.run(WorkContext(now=T0, http=None, secrets_dir=self.state,
                                        job=self.pionir.job))
        self.assertEqual(r.error.kind, ErrorKind.NOT_CONFIGURED)
        r = self.worker.run(WorkContext(now=T0, http=None, secrets_dir=self.state,
                                        state_dir=self.state))
        self.assertEqual(r.error.kind, ErrorKind.NOT_CONFIGURED)
        self.worker.record_path(self.state).write_text("{not json", encoding="utf-8")
        self.pionir.orders = [order()]
        r = self.run_at()
        self.assertEqual(r.error.kind, ErrorKind.MALFORMED)
        self.assertEqual(self.pionir.jobs, [])

    def test_a_malformed_order_is_counted_not_acted_on(self) -> None:
        self.pionir.orders = [order(), {"no": "id"}, "junk"]
        result = self.run_at()
        self.assertEqual(len(self.pionir.emails()), 1)
        self.assertEqual(self.tally_payload(result)["malformed_orders"], 2)
        self.assertEqual(self.tally(result)["orders listed"], 1)


if __name__ == "__main__":
    unittest.main()
