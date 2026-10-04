"""The feedback desk: one feedback request per delivered order, and each consented testimonial
proposed - both for the owner's yes.

Pionir is a fake (``FakePionir``): it lists the orders (with Scrooge's ``feedback`` summary) and
the testimonials waiting, and parks every request and proposal under its own approval id. Each
test fails if the rule it names is reverted: a request before the day Scrooge allows, for an
order not delivered or already asked, twice, more than two a run, to anyone but the order's
address, with words other than the template; a testimonial proposed that fails the content
checks, proposed twice, or proposed with anything but the client's own words; or orders that
could not be read reported as nothing to do.
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from pionir.crew import feedback as desk
from pionir.crew.feedback import (
    EMAIL,
    MAX_REQUESTS_PER_RUN,
    ORDERS,
    PUBLISH,
    TESTIMONIALS,
    FeedbackDesk,
    build_feedback_email,
    check_feedback_email,
)
from pionir.crew.hands import JobOutcome
from pionir.crew.registry import default_registry
from pionir.crew.result import Err, Ok
from pionir.crew.worker import ErrorKind, WorkContext

T0 = 1_790_000_000.0
DAY = 86400.0


def iso(t: float) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(t, dt.UTC).isoformat().replace("+00:00", "Z")


def order(oid="a1a1a1a1a1a1", status="delivered", eligible=T0 - DAY, requested=None,
          name="Ann Client", email="ann@example.com", messages=None, feedback=True):
    o = {"id": oid, "name": name, "email": email, "package": "small", "status": status,
         "amount_cents": 14900, "created_at": iso(T0 - 10 * DAY), "messages": messages or []}
    o["feedback"] = ({"eligible_from": None if eligible is None else iso(eligible),
                      "requested_at": requested, "expires_at": None, "received_at": None,
                      "referral_code": None} if feedback else None)
    return o


def tm(tid="tm_" + "a" * 24, body="Fast, careful, and it just works.", name="Ann C.", rating=5,
       oid="a1a1a1a1a1a1"):
    return {"id": tid, "order_id": oid, "rating": rating, "body": body, "display_name": name,
            "created_at": iso(T0 - DAY)}


STATS = {"feedback_requested": 4, "feedback_received": 3, "avg_rating": 4.67,
         "testimonials_pending": 1, "testimonials_live": 2, "referral_orders": 2,
         "referral_orders_paid": 1, "credits_earned_cents": 2500, "credits_applied_cents": 0}


class FakePionir:
    def __init__(self) -> None:
        self.orders: list = []
        self.testimonials: list = []
        self.stats = dict(STATS)
        self.jobs: list = []
        self.approvals: dict = {}
        self.orders_outcome = None
        self.testimonials_outcome = None

    def job(self, job):
        self.jobs.append(job)
        if job.capability == ORDERS:
            return self.orders_outcome or JobOutcome(
                "done", ORDERS, result={"ok": True, "orders": copy.deepcopy(self.orders)})
        if job.capability == TESTIMONIALS:
            return self.testimonials_outcome or JobOutcome(
                "done", TESTIMONIALS, result={"ok": True,
                                              "testimonials": copy.deepcopy(self.testimonials),
                                              "stats": dict(self.stats)})
        if job.capability in (EMAIL, PUBLISH):
            n = len(self.parked())
            return JobOutcome("pending_approval", job.capability, task_id=f"t-{n}",
                              approval_id=f"ap-{n}")
        raise AssertionError(f"unexpected capability {job.capability}")

    def approval(self, approval_id):
        return dict(self.approvals.get(approval_id, {"status": "pending"}))

    def approve(self, approval_id) -> None:
        self.approvals[approval_id] = {"status": "approved", "result": {
            "ok": True, "agent_id": "client", "result": {"ok": True}}}

    def deny(self, approval_id) -> None:
        self.approvals[approval_id] = {"status": "denied", "reason": "no"}

    def parked(self) -> list:
        return [j for j in self.jobs if j.capability in (EMAIL, PUBLISH)]

    def emails(self) -> list:
        return [j for j in self.jobs if j.capability == EMAIL]

    def proposals(self) -> list:
        return [j for j in self.jobs if j.capability == PUBLISH]


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.worker = default_registry().require("contracts.feedback")
        self.pionir = FakePionir()

    def run_at(self, now=T0):
        ctx = WorkContext(now=now, http=None, secrets_dir=self.state, job=self.pionir.job,
                          approval=self.pionir.approval, state_dir=self.state)
        return self.worker.run(ctx)

    def record(self) -> dict:
        return json.loads(self.worker.record_path(self.state).read_text(encoding="utf-8"))

    @staticmethod
    def tally(result) -> dict:
        (t,) = [o for o in result.value if o.kind == "feedback.tally"]
        return {(f.measures if f.unit == "count" else f"{f.measures} ({f.unit})"): f.value
                for f in t.figures}


class CatalogueTests(unittest.TestCase):
    def test_the_feedback_desk_is_in_the_contracts_division(self) -> None:
        reg = default_registry()
        w = reg.require("contracts.feedback")
        self.assertIsInstance(w, FeedbackDesk)
        self.assertEqual((w.division, w.cadence_seconds, w.live, w.provider),
                         ("contracts", 900, True, "none"))
        self.assertEqual(set(reg.uses("contracts.feedback")),
                         {ORDERS, EMAIL, TESTIMONIALS, PUBLISH})
        notes = reg.division("contracts").leader_notes
        self.assertIn("contracts.feedback", notes)
        self.assertIn("apply by hand", notes)


class TemplateTests(unittest.TestCase):
    def test_the_email_is_the_template_to_the_order_address_with_its_kind(self) -> None:
        o = order()
        e = build_feedback_email(o)
        self.assertEqual(set(e), {"order_id", "to", "kind", "subject", "body_text"})
        self.assertEqual((e["order_id"], e["to"], e["kind"]),
                         ("a1a1a1a1a1a1", "ann@example.com", "feedback_request"))
        self.assertTrue(e["body_text"].startswith("Hello Ann Client,\n"))
        self.assertEqual(e["body_text"].count("{feedback_link}"), 1)
        self.assertEqual(e["body_text"].count("{referral_code}"), 2)
        self.assertEqual(check_feedback_email(e, o), [])

    def test_the_check_fails_closed(self) -> None:
        o = order()
        e = build_feedback_email(o)
        self.assertTrue(check_feedback_email({**e, "to": "someone@else.example"}, o))
        self.assertTrue(check_feedback_email({**e, "kind": None}, o))
        self.assertTrue(check_feedback_email(
            {**e, "body_text": e["body_text"].replace("$25", "$50")}, o))
        self.assertTrue(check_feedback_email(
            {**e, "body_text": e["body_text"] + "\nhttps://evil.example/x"}, o))
        self.assertTrue(check_feedback_email(
            {**e, "body_text": e["body_text"].replace("{feedback_link}", "")}, o))

    def test_an_odd_name_is_greeted_as_there(self) -> None:
        e = build_feedback_email(order(name="<script>"))
        self.assertTrue(e["body_text"].startswith("Hello there,\n"))


class RequestTests(_Case):
    def test_a_due_delivered_order_gets_one_request_parked_for_the_owner(self) -> None:
        self.pionir.orders = [order()]
        result = self.run_at()
        self.assertIsInstance(result, Ok)
        (job,) = self.pionir.emails()
        self.assertEqual(job.permissions, ())             # nothing granted: Pionir parks it
        self.assertEqual(job.payload, build_feedback_email(order()))
        (entry,) = self.record()["requests"]
        self.assertEqual((entry["status"], entry["approval_id"]), ("pending_approval", "ap-1"))
        self.assertIn("feedback.request_pending", [o.kind for o in result.value])
        t = self.tally(result)
        self.assertEqual(t["feedback requests pending the owner's approval"], 1)
        # still waiting, and Scrooge still says never asked: NOT asked again
        self.run_at(T0 + 900)
        self.assertEqual(len(self.pionir.emails()), 1)
        # the owner says yes: sent; never again, whatever the listing says
        self.pionir.approve("ap-1")
        result = self.run_at(T0 + 1800)
        self.assertIn("feedback.request_sent", [o.kind for o in result.value])
        self.assertEqual(self.tally(result)["feedback requests sent by this desk"], 1)
        self.run_at(T0 + 2700)
        self.assertEqual(len(self.pionir.emails()), 1)

    def test_denied_is_never_asked_again(self) -> None:
        self.pionir.orders = [order()]
        self.run_at()
        self.pionir.deny("ap-1")
        result = self.run_at(T0 + 900)
        self.run_at(T0 + 1800)
        self.assertEqual(len(self.pionir.emails()), 1)
        self.assertEqual(self.tally(result)["feedback requests denied"], 1)

    def test_not_before_its_day_not_twice_and_only_delivered(self) -> None:
        self.pionir.orders = [
            order("111111111111", eligible=T0 + 60),                       # not yet
            order("222222222222", requested=iso(T0 - 5 * DAY)),            # Scrooge: asked
            order("333333333333", status="in_progress"),                   # not delivered
            order("444444444444", status="refunded"),
            order("555555555555", eligible=None),                          # refunded part
            order("666666666666", feedback=False),                         # before migration
        ]
        result = self.run_at()
        self.assertEqual(self.pionir.emails(), [])
        self.assertEqual(self.tally(result)["delivered orders due a feedback request"], 0)
        # the day comes for the first: asked then, and only then
        self.run_at(T0 + 61)
        self.assertEqual([j.payload["order_id"] for j in self.pionir.emails()],
                         ["111111111111"])

    def test_at_most_two_a_run_oldest_delivery_first(self) -> None:
        self.pionir.orders = [order(f"{i}" * 12, eligible=T0 - (10 - i) * DAY)
                              for i in range(1, 5)]
        result = self.run_at()
        self.assertEqual([j.payload["order_id"] for j in self.pionir.emails()],
                         ["111111111111", "222222222222"])
        self.assertEqual(len(self.pionir.emails()), MAX_REQUESTS_PER_RUN)
        (t,) = [o for o in result.value if o.kind == "feedback.tally"]
        self.assertEqual(t.payload["requests_ready_next_run"], 2)

    def test_already_in_the_order_messages_is_recorded_sent_not_sent_again(self) -> None:
        subject = build_feedback_email(order())["subject"]
        self.pionir.orders = [order(messages=[{"at": iso(T0), "subject": subject}])]
        self.run_at()
        self.assertEqual(self.pionir.emails(), [])
        self.assertEqual(self.record()["requests"][0]["status"], "sent")

    def test_orders_unreadable_is_unavailable_never_nothing_to_do(self) -> None:
        self.pionir.orders_outcome = JobOutcome("unreachable", ORDERS, error="down")
        result = self.run_at()
        self.assertIsInstance(result, Err)
        self.assertEqual(result.error.kind, ErrorKind.UNAVAILABLE)
        self.assertEqual([j.capability for j in self.pionir.jobs], [ORDERS])


class TestimonialTests(_Case):
    def test_a_consented_testimonial_is_proposed_once_exactly_as_written(self) -> None:
        self.pionir.testimonials = [tm()]
        result = self.run_at()
        (job,) = self.pionir.proposals()
        self.assertEqual(job.payload, {"testimonial_id": "tm_" + "a" * 24,
                                       "order_id": "a1a1a1a1a1a1", "rating": 5,
                                       "display_name": "Ann C.",
                                       "body": "Fast, careful, and it just works."})
        self.assertEqual(job.permissions, ())
        self.assertIn("feedback.testimonial_pending", [o.kind for o in result.value])
        self.run_at(T0 + 900)                     # still pending on Scrooge: not again
        self.assertEqual(len(self.pionir.proposals()), 1)
        self.pionir.approve("ap-1")
        result = self.run_at(T0 + 1800)
        self.assertIn("feedback.testimonial_live", [o.kind for o in result.value])
        self.assertEqual(len(self.pionir.proposals()), 1)

    def test_one_that_fails_the_content_checks_is_blocked_and_never_proposed(self) -> None:
        self.pionir.testimonials = [tm(body="Great! Buy cheap at https://spam.example now"),
                                    tm("tm_" + "b" * 24, name="ann@example.com")]
        result = self.run_at()
        self.assertEqual(self.pionir.proposals(), [])
        kinds = [o.kind for o in result.value]
        self.assertEqual(kinds.count("feedback.testimonial_blocked"), 2)
        self.assertEqual(self.tally(result)["testimonials blocked by the content checks"], 2)
        # the leader's rows never carry the client's words or name
        for o in result.value:
            self.assertNotIn("spam.example", json.dumps(o.payload))
            self.assertNotIn("ann@example.com", json.dumps(o.payload))
        self.run_at(T0 + 900)
        self.assertEqual(self.pionir.proposals(), [])

    def test_the_counts_come_from_scrooge_and_unknown_is_said(self) -> None:
        result = self.run_at()
        t = self.tally(result)
        self.assertEqual(t["feedback requests sent"], 4)
        self.assertEqual(t["feedback answers received"], 3)
        self.assertEqual(t["average client rating, in stars out of 5"], 4.67)
        self.assertEqual(t["testimonials live on the hire page"], 2)
        self.assertEqual(t["orders placed through a referral code"], 2)
        self.assertEqual(t["referral orders paid"], 1)
        self.assertEqual(t["referral credits to apply by hand (usd_cents)"], 2500)
        self.pionir.testimonials_outcome = JobOutcome("failed", TESTIMONIALS, error="403")
        self.pionir.orders = [order()]
        result = self.run_at(T0 + 60)
        (tally,) = [o for o in result.value if o.kind == "feedback.tally"]
        self.assertIn("UNKNOWN", tally.payload["scrooge_counts"])
        self.assertNotIn("testimonials live on the hire page", self.tally(result))
        self.assertEqual(len(self.pionir.emails()), 1)   # the requests still went


if __name__ == "__main__":
    unittest.main()
