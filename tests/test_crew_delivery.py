"""The delivery desk: each order's finished zip shipped by fixed template, for the owner's yes.

Pionir is a fake (``FakePionir``): it lists the orders, parks every delivery under its own
approval id (or refuses it, as its pre-park checks would), and on approval reports the
upload and the email. The owner's folder is a temp ``deliveries_dir``. Each test fails if
the rule it names is reverted: a zip submitted twice (by its sha, not its name), a revision
sent for a zip no newer than the delivered one, a refused zip retried, a new zip after a
refusal NOT tried, an outage offered again for ever (or never), a delivery claimed before
Pionir says it was emailed, a late order not counted, or orders that could not be read
reported as nothing to deliver.
"""

import copy
import datetime as dt
import hashlib
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pionir.crew import delivery as desk
from pionir.crew.delivery import (
    DELIVER,
    LINK,
    REVISION,
    DeliveryDesk,
    build_delivery,
    check_delivery,
    turnaround_due,
)
from pionir.crew.hands import JobOutcome
from pionir.crew.orders import ORDERS, RETRY_UNDELIVERED, RETRY_UNREACHABLE
from pionir.crew.registry import default_registry
from pionir.crew.result import Err, Ok
from pionir.crew.worker import ErrorKind, WorkContext

T0 = 1_790_000_000.0            # 2026-09-21 14:13 UTC, a Monday
DAY = 86400.0
SECRETS = "zip: secrets found - an API key in src/config.py; nothing was parked"


def order(oid="ord_1", status="in_progress", package="small", amount=14900, name="Ana Lima",
          email="ana@example.com", paid_at=T0 - DAY, messages=None):
    return {"id": oid, "created_at": paid_at - 600, "name": name, "email": email,
            "package": package, "brief": "Rename my invoice PDFs by date.", "status": status,
            "amount_cents": amount, "paid_at": paid_at, "messages": messages or []}


def at(iso: str) -> float:
    return dt.datetime.fromisoformat(iso).replace(tzinfo=dt.UTC).timestamp()


class FakePionir:
    """``ctx.job`` and ``ctx.approval``: orders listed, deliveries parked or refused."""

    def __init__(self) -> None:
        self.orders: list = []
        self.jobs: list = []
        self.approvals: dict = {}
        self.orders_outcome = None
        self.deliver_outcome = None

    def job(self, job):
        self.jobs.append(job)
        if job.capability == ORDERS:
            if self.orders_outcome is not None:
                return self.orders_outcome
            return JobOutcome("done", ORDERS, task_id="t-o",
                              result={"ok": True, "orders": copy.deepcopy(self.orders)})
        if job.capability == DELIVER:
            if self.deliver_outcome is not None:
                return self.deliver_outcome
            n = len(self.deliveries())
            return JobOutcome("pending_approval", DELIVER, task_id=f"t-{n}",
                              approval_id=f"dl-{n}")
        raise AssertionError(f"unexpected capability {job.capability}")

    def approval(self, approval_id):
        return dict(self.approvals.get(approval_id, {"status": "pending"}))

    def approve(self, approval_id) -> None:
        """Uploaded and emailed; Pionir marks the order delivered."""
        self.approvals[approval_id] = {"id": approval_id, "status": "approved", "result": {
            "ok": True, "agent_id": "client", "result": {
                "ok": True, "delivery_id": f"dv-{approval_id}",
                "url": "https://api.dokaz.net/d/PRIVATE-TOKEN", "expires_at": "2026-10-25",
                "emailed": True, "status": "delivered"}}}
        payload = self.payload_of(approval_id)
        for o in self.orders:
            if o["id"] == payload["order_id"]:
                o["status"] = "delivered"

    def approved_failed(self, approval_id, response) -> None:
        self.approvals[approval_id] = {"id": approval_id, "status": "approved_failed",
                                       "result": response}

    def deny(self, approval_id) -> None:
        self.approvals[approval_id] = {"id": approval_id, "status": "denied", "reason": "no"}

    def deliveries(self) -> list:
        return [j for j in self.jobs if j.capability == DELIVER]

    def payload_of(self, approval_id) -> dict:
        return self.deliveries()[int(approval_id.split("-")[1]) - 1].payload


# what Pionir's approval record holds when the upload worked but the email did not
NOT_EMAILED = {"ok": False, "agent_id": "client", "result": {
    "ok": False, "delivery_id": "dv-x", "emailed": False,
    "error": "uploaded, but the mail service answered HTTP 503 - NOT emailed"}}
# ... and when the adapter refused it on approval (the zip changed since it was parked)
CHANGED = {"ok": False, "error": {"type": "AdapterProtocolError",
                                  "message": "zip_sha256: the zip changed since it was parked"}}


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "state"
        self.drops = Path(tmp.name) / "deliveries"
        self.worker = default_registry().require("contracts.delivery")
        self.pionir = FakePionir()

    def drop(self, oid="ord_1", name="build.zip", data=b"PK\x03\x04 v1", mtime=T0 - 3600):
        folder = self.drops / oid
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(data)
        os.utime(path, (mtime, mtime))
        return hashlib.sha256(data).hexdigest()

    def run_at(self, now=T0):
        ctx = WorkContext(now=now, http=None, secrets_dir=self.state, job=self.pionir.job,
                          approval=self.pionir.approval, state_dir=self.state,
                          deliveries_dir=self.drops)
        return self.worker.run(ctx)

    def record(self) -> list:
        path = self.worker.record_path(self.state)
        return json.loads(path.read_text(encoding="utf-8"))["deliveries"]

    @staticmethod
    def tally(result) -> dict:
        (t,) = [o for o in result.value if o.kind == "delivery.tally"]
        return {f.measures: f.value for f in t.figures}

    @staticmethod
    def tally_payload(result) -> dict:
        (t,) = [o for o in result.value if o.kind == "delivery.tally"]
        return t.payload


class CatalogueTests(unittest.TestCase):
    def test_the_delivery_desk_is_in_contracts_every_ten_minutes(self) -> None:
        reg = default_registry()
        w = reg.require("contracts.delivery")
        self.assertIsInstance(w, DeliveryDesk)
        self.assertEqual((w.division, w.cadence_seconds, w.live), ("contracts", 600, True))
        notes = reg.division("contracts").leader_notes
        self.assertIn("URGENT", notes)
        self.assertIn("nothing is delivered until a delivery.delivered row says so", notes)


class FirstDeliveryTests(_Case):
    def test_a_zip_for_an_in_progress_order_is_one_delivery_then_delivered_once_approved(
            self) -> None:
        self.pionir.orders = [order()]
        sha = self.drop(name="ord_1-build.zip")
        result = self.run_at(T0)
        self.assertIsInstance(result, Ok)
        (job,) = self.pionir.deliveries()
        self.assertEqual(job.permissions, ())             # nothing granted: Pionir parks it
        p = job.payload
        self.assertEqual(set(p), {"order_id", "to", "zip_name", "zip_sha256", "subject",
                                  "body_text"})
        self.assertEqual((p["order_id"], p["to"], p["zip_name"], p["zip_sha256"]),
                         ("ord_1", "ana@example.com", "ord_1-build.zip", sha))
        self.assertEqual(p["subject"], "Your Dokaz delivery for order ord_1")
        body = p["body_text"]
        self.assertTrue(body.startswith("Hello Ana Lima,\n"), body[:40])
        self.assertEqual(body.count(LINK), 1)
        for words in ("Your order ord_1 is ready.", "The link works for 30 days",
                      "README or HOWTO", "One round of revisions is included free",
                      "reply to this email"):
            self.assertIn(words, body)
        self.assertTrue(body.endswith("\nDokaz"))
        self.assertNotRegex(body, r"https?://|www\.|\$")
        self.assertEqual(check_delivery(p, order()), [])
        (entry,) = self.record()
        self.assertEqual((entry["status"], entry["approval_id"], entry["kind"]),
                         ("pending_approval", "dl-1", "delivery"))
        t = self.tally(result)
        self.assertEqual((t["deliveries pending the owner's approval"],
                          t["deliveries delivered"], t["orders in progress"]), (1, 0, 1))
        # still waiting: not delivered, not submitted again
        result = self.run_at(T0 + 600)
        self.assertEqual(len(self.pionir.deliveries()), 1)
        self.assertNotIn("delivery.delivered", [o.kind for o in result.value])
        # the owner says yes: uploaded and emailed - now, and only now, delivered
        self.pionir.approve("dl-1")
        result = self.run_at(T0 + 1200)
        (done,) = [o for o in result.value if o.kind == "delivery.delivered"]
        self.assertEqual(done.payload["order_id"], "ord_1")
        self.assertNotIn("PRIVATE-TOKEN", json.dumps([o.payload for o in result.value]))
        self.assertEqual(self.record()[0]["status"], "delivered")
        t = self.tally(result)
        self.assertEqual((t["deliveries delivered"], t["deliveries pending the owner's approval"]),
                         (1, 0))
        self.run_at(T0 + 1800)
        self.assertEqual(len(self.pionir.deliveries()), 1)

    def test_an_approval_that_does_not_say_emailed_is_not_delivered(self) -> None:
        self.pionir.orders = [order()]
        self.drop()
        self.run_at(T0)
        self.pionir.approvals["dl-1"] = {"id": "dl-1", "status": "approved", "result": {
            "ok": True, "agent_id": "client", "result": {"ok": True, "delivery_id": "d"}}}
        result = self.run_at(T0 + 600)
        self.assertNotIn("delivery.delivered", [o.kind for o in result.value])
        self.assertEqual(self.record()[0]["status"], "undelivered")
        self.assertEqual(self.tally(result)["deliveries delivered"], 0)

    def test_the_same_zip_is_never_submitted_twice_even_renamed(self) -> None:
        self.pionir.orders = [order()]
        self.drop(name="build.zip")
        self.run_at(T0)
        self.pionir.deny("dl-1")
        self.run_at(T0 + 600)
        self.assertEqual(self.record()[0]["status"], "denied")
        # the owner copies the very same bytes under a new, newer name: still the same zip
        self.drop(name="build-final.zip", mtime=T0)
        for i in range(3):
            self.run_at(T0 + (i + 2) * 600)
        self.assertEqual(len(self.pionir.deliveries()), 1)

    def test_a_zip_still_being_copied_waits(self) -> None:
        self.pionir.orders = [order()]
        self.drop(mtime=T0 - 30)
        result = self.run_at(T0)
        self.assertEqual(self.pionir.deliveries(), [])
        self.assertEqual(self.tally_payload(result)["zips_still_copying"], 1)
        self.run_at(T0 + 600)
        self.assertEqual(len(self.pionir.deliveries()), 1)

    def test_at_most_one_delivery_a_run_oldest_order_first(self) -> None:
        self.pionir.orders = [order("ord_b", paid_at=T0 - DAY),
                              order("ord_a", paid_at=T0 - 2 * DAY)]
        self.drop("ord_a")
        self.drop("ord_b", data=b"PK other")
        result = self.run_at(T0)
        self.assertEqual([j.payload["order_id"] for j in self.pionir.deliveries()], ["ord_a"])
        self.assertEqual(self.tally_payload(result)["deliveries_ready_next_run"], 1)
        self.run_at(T0 + 600)
        self.assertEqual([j.payload["order_id"] for j in self.pionir.deliveries()],
                         ["ord_a", "ord_b"])

    def test_orders_not_in_progress_get_nothing(self) -> None:
        self.pionir.orders = [order(f"ord_{s}", status=s) for s in
                              ("paid", "awaiting_payment", "declined", "refunded", "quoted",
                               "delivered")]
        for o in self.pionir.orders:
            self.drop(o["id"])
        self.run_at(T0)
        # "delivered" too: delivered some other way, never by this desk - not its to revise
        self.assertEqual(self.pionir.deliveries(), [])


class RevisionTests(_Case):
    def _delivered(self) -> None:
        self.pionir.orders = [order()]
        self.drop(name="v1.zip", data=b"PK v1", mtime=T0 - 3600)
        self.run_at(T0)
        self.pionir.approve("dl-1")
        self.run_at(T0 + 600)
        self.assertEqual(self.pionir.orders[0]["status"], "delivered")

    def test_a_newer_zip_for_a_delivered_order_is_a_revision_email(self) -> None:
        self._delivered()
        sha = self.drop(name="v2.zip", data=b"PK v2", mtime=T0 + 900)
        result = self.run_at(T0 + 1800)
        self.assertEqual(len(self.pionir.deliveries()), 2)
        p = self.pionir.deliveries()[1].payload
        self.assertEqual((p["zip_name"], p["zip_sha256"]), ("v2.zip", sha))
        self.assertEqual(p["subject"], "Your revised Dokaz delivery for order ord_1")
        self.assertIn("this is the revised version", p["body_text"])
        self.assertEqual(p["body_text"].count(LINK), 1)
        self.assertEqual(check_delivery(p, self.pionir.orders[0]), [])
        pending = [o for o in result.value if o.kind == "delivery.pending"]
        self.assertEqual(pending[0].payload["delivery"], REVISION)
        self.pionir.approve("dl-2")
        result = self.run_at(T0 + 2400)
        t = self.tally(result)
        self.assertEqual((t["deliveries delivered"], t["revised deliveries delivered"]), (2, 1))
        self.run_at(T0 + 3000)
        self.assertEqual(len(self.pionir.deliveries()), 2)

    def test_a_zip_no_newer_than_the_delivered_one_is_not_a_revision(self) -> None:
        self._delivered()
        # a different zip, but older than the one delivered (an old build left behind)
        (self.drops / "ord_1" / "v1.zip").unlink()
        self.drop(name="old.zip", data=b"PK old", mtime=T0 - 7200)
        for i in range(2):
            self.run_at(T0 + 1800 + i * 600)
        self.assertEqual(len(self.pionir.deliveries()), 1)


class RefusedTests(_Case):
    def test_a_zip_the_checks_refuse_is_recorded_blocked_and_never_retried(self) -> None:
        self.pionir.orders = [order()]
        self.drop()
        self.pionir.deliver_outcome = JobOutcome("failed", DELIVER, error=SECRETS)
        result = self.run_at(T0)
        (entry,) = self.record()
        self.assertEqual(entry["status"], "refused")
        self.assertIn("secrets found", entry["why"])
        (blocked,) = [o for o in result.value if o.kind == "delivery.blocked"]
        self.assertIn("secrets found", blocked.payload["why"])
        t = self.tally(result)
        self.assertEqual((t["deliveries blocked by the checks"], t["deliveries delivered"],
                          t["deliveries pending the owner's approval"]), (1, 0, 0))
        (row,) = self.tally_payload(result)["blocked"]
        self.assertEqual((row["order_id"], row["by"]), ("ord_1", "Pionir's checks"))
        self.assertIn("secrets found", row["why"])
        # Pionir would park it now - but it is the same zip, so it is never offered again
        self.pionir.deliver_outcome = None
        for i in range(3):
            result = self.run_at(T0 + (i + 1) * 600)
        self.assertEqual(len(self.pionir.deliveries()), 1)
        self.assertEqual(self.tally(result)["deliveries blocked by the checks"], 1)

    def test_a_new_zip_after_a_refusal_is_a_new_attempt(self) -> None:
        self.pionir.orders = [order()]
        self.drop(name="build.zip", data=b"PK with a key")
        self.pionir.deliver_outcome = JobOutcome("failed", DELIVER, error=SECRETS)
        self.run_at(T0)
        self.pionir.deliver_outcome = None
        sha = self.drop(name="build-fixed.zip", data=b"PK without", mtime=T0)
        result = self.run_at(T0 + 600)
        self.assertEqual(len(self.pionir.deliveries()), 2)
        self.assertEqual(self.pionir.deliveries()[1].payload["zip_sha256"], sha)
        self.assertEqual(self.record()[1]["status"], "pending_approval")
        self.assertEqual(self.tally(result)["deliveries blocked by the checks"], 0)
        self.assertEqual(self.tally(result)["zips refused by Pionir's checks"], 1)

    def test_pionirs_typed_error_decides_not_the_wording(self) -> None:
        # a typed refusal whose message happens to say "unavailable" is still a refusal;
        # a typed passing problem with neutral wording is still retried
        self.pionir.orders = [order()]
        self.drop()
        self.pionir.deliver_outcome = JobOutcome(
            "failed", DELIVER, error="the zip names a file that is unavailable in the archive",
            error_type="AdapterProtocolError")
        self.run_at(T0)
        self.assertEqual(self.record()[-1]["status"], "refused")
        self.setUp()
        self.pionir.orders = [order()]
        self.drop()
        self.pionir.deliver_outcome = JobOutcome("failed", DELIVER, error="Scrooge said no",
                                                 error_type="AdapterUnavailable")
        self.run_at(T0)
        self.assertEqual(self.record()[-1]["status"], "unreachable")

    def test_the_crew_keeps_pionirs_error_type(self) -> None:
        from pionir.crew.hands import outcome_of
        out = outcome_of(DELIVER, {"ok": False, "task_id": "t",
                                   "error": {"type": "AdapterProtocolError", "message": "no"}})
        self.assertEqual((out.status, out.error, out.error_type),
                         ("failed", "no", "AdapterProtocolError"))

    def test_a_passing_failure_or_an_unset_capability_does_not_burn_the_zip(self) -> None:
        self.pionir.orders = [order()]
        self.drop()
        self.pionir.deliver_outcome = JobOutcome("failed", DELIVER,
                                                 error="unknown capability client.deliver")
        result = self.run_at(T0)
        self.assertEqual(self.tally_payload(result)["deliver_not_set_up"],
                         "unknown capability client.deliver")
        self.assertEqual(self.record(), [])
        self.pionir.deliver_outcome = JobOutcome("unreachable", DELIVER, error="refused conn")
        self.run_at(T0 + 600)
        self.assertEqual(self.record()[-1]["status"], "unreachable")
        self.pionir.deliver_outcome = None
        self.run_at(T0 + 1200)
        self.assertEqual(self.record()[-1]["status"], "pending_approval")
        self.assertEqual(len(self.pionir.deliveries()), 3)

    def test_an_unreachable_pionir_is_retried_then_given_up(self) -> None:
        self.pionir.orders = [order()]
        self.drop()
        self.pionir.deliver_outcome = JobOutcome("unreachable", DELIVER, error="down")
        for i in range(RETRY_UNREACHABLE + 2):
            result = self.run_at(T0 + i * 600)
        self.assertEqual(len(self.pionir.deliveries()), RETRY_UNREACHABLE)
        self.assertEqual(self.tally(result)["deliveries failed"], 1)


class OutageTests(_Case):
    def test_an_approved_delivery_that_hit_an_outage_is_offered_again_then_stops(
            self) -> None:
        self.pionir.orders = [order()]
        self.drop()
        for i in range(RETRY_UNDELIVERED):
            self.run_at(T0 + i * 1200)
            self.pionir.approved_failed(f"dl-{i + 1}", NOT_EMAILED)
        result = self.run_at(T0 + RETRY_UNDELIVERED * 1200)
        self.assertEqual(len(self.pionir.deliveries()), RETRY_UNDELIVERED)
        self.assertEqual([e["status"] for e in self.record()], ["undelivered"] * 3)
        self.assertEqual(self.tally(result)[
            "approved deliveries that failed to send (offered to the owner again)"], 3)
        self.assertEqual(self.tally(result)["deliveries delivered"], 0)
        self.run_at(T0 + (RETRY_UNDELIVERED + 1) * 1200)
        self.assertEqual(len(self.pionir.deliveries()), RETRY_UNDELIVERED)   # and it stops

    def test_a_refusal_on_approval_is_final(self) -> None:
        self.pionir.orders = [order()]
        self.drop()
        self.run_at(T0)
        self.pionir.approved_failed("dl-1", CHANGED)
        for i in range(3):
            self.run_at(T0 + (i + 1) * 600)
        self.assertEqual(len(self.pionir.deliveries()), 1)
        self.assertEqual(self.record()[0]["status"], "failed")

    def test_a_reported_failure_the_orders_messages_show_sent_is_delivered(self) -> None:
        self.pionir.orders = [order()]
        self.drop()
        self.run_at(T0)
        subject = self.pionir.deliveries()[0].payload["subject"]
        self.pionir.orders[0]["messages"] = [{"at": "2026-09-21T15:00:00Z", "subject": subject}]
        self.pionir.approved_failed("dl-1", NOT_EMAILED)
        self.run_at(T0 + 600)
        self.assertEqual(self.record()[0]["status"], "delivered")
        self.run_at(T0 + 1200)
        self.assertEqual(len(self.pionir.deliveries()), 1)


class TurnaroundTests(_Case):
    def test_an_in_progress_order_with_no_zip_past_its_turnaround_is_counted(self) -> None:
        now = at("2026-09-25T12:00:00")                  # a Friday
        self.pionir.orders = [
            order("ord_late", paid_at=at("2026-09-21T10:00:00")),          # small: Thu 24th
            order("ord_ok", package="standard", amount=39900,
                  paid_at=at("2026-09-21T10:00:00")),                      # standard: Mon 28th
            order("ord_custom", package="custom", amount=120000,
                  paid_at=at("2026-09-01T10:00:00")),                      # no fixed date
        ]
        result = self.run_at(now)
        self.assertEqual(self.pionir.deliveries(), [])
        t = self.tally(result)
        self.assertEqual((t["orders in progress"],
                          t["orders in progress with no delivery yet (waiting for a zip)"],
                          t["orders past their turnaround"]), (3, 3, 1))
        payload = self.tally_payload(result)
        self.assertEqual(payload["past_turnaround"], ["ord_late"])
        self.assertEqual(sorted(payload["waiting_for_zip"]), ["ord_custom", "ord_late", "ord_ok"])

    def test_the_turnaround_counts_business_days(self) -> None:
        # paid Thursday: 3 business days is Tuesday, not Sunday
        o = order(paid_at=at("2026-09-24T10:00:00"))
        self.assertEqual(turnaround_due(o), at("2026-09-29T10:00:00"))
        self.pionir.orders = [o]
        result = self.run_at(at("2026-09-28T09:00:00"))
        self.assertEqual(self.tally(result)["orders past their turnaround"], 0)
        self.assertIsNone(turnaround_due(order(package="custom")))
        self.assertIsNone(turnaround_due(dict(order(), paid_at=None)))


class TemplateCheckTests(_Case):
    SHA = "a" * 64

    def test_both_templates_pass_their_own_check(self) -> None:
        for kind in ("delivery", REVISION):
            for name in ("Ana Lima", "<b>Bob</b>", "Bob https://evil.example", None):
                o = order(name=name)
                p = build_delivery(kind, o, "build.zip", self.SHA)
                self.assertEqual(check_delivery(p, o), [], (kind, name))
                self.assertEqual(p["body_text"].count(LINK), 1)

    def test_the_check_fails_closed(self) -> None:
        o = order()
        good = build_delivery("delivery", o, "build.zip", self.SHA)
        body = good["body_text"]
        cases = {
            "exactly once": dict(good, body_text=body.replace(LINK, "the link")),
            "exactly once, not 2": dict(good, body_text=body + "\nAgain: " + LINK),
            "a link of its own": dict(good, body_text=body + "\nhttps://evil.example/x"),
            "names an address or a domain": dict(good, body_text=body + "\nhelp@dokaz.net"),
            "not plain text": dict(good, body_text=body + "\n<b>hi</b>"),
            "unfilled": dict(good, body_text=body + "\nHello {name}"),
            "amount": dict(good, body_text=body + "\nThat was $149."),
            "the recipient": dict(good, to="someone@else.com"),
            "zip_name": dict(good, zip_name="../../secrets.zip"),
            "zip_sha256": dict(good, zip_sha256="ABC"),
            "single line": dict(good, subject="Your delivery\nBcc: x"),
            "the subject holds": dict(good, subject="Your delivery " + LINK),
            "exactly order_id": dict(good, cc="x@y.com"),
        }
        for words, payload in cases.items():
            reasons = check_delivery(payload, o)
            self.assertTrue(any(words in r for r in reasons), (words, reasons))

    def test_a_template_that_fails_the_check_is_blocked_and_never_retried(self) -> None:
        self.pionir.orders = [order()]
        self.drop()
        with mock.patch.object(desk, "DELIVERY_BODY", "Hello {name}, here: https://x.example"):
            result = self.run_at(T0)
            self.run_at(T0 + 600)
        self.assertEqual(self.pionir.deliveries(), [])
        (entry,) = self.record()
        self.assertEqual(entry["status"], "blocked")
        self.assertIn("delivery.template_blocked", [o.kind for o in result.value])
        self.assertEqual(self.tally(result)["delivery emails blocked by the template check"], 1)
        self.assertEqual(self.tally_payload(result)["blocked"][0]["by"], "the template check")


class UnavailableTests(_Case):
    def test_orders_that_could_not_be_read_are_not_nothing_to_deliver(self) -> None:
        self.drop()
        cases = ((JobOutcome("unreachable", ORDERS, error="connection refused"),
                  ErrorKind.UNAVAILABLE),
                 (JobOutcome("failed", ORDERS, error="client.orders is not configured"),
                  ErrorKind.NOT_CONFIGURED),
                 (JobOutcome("done", ORDERS, result={"ok": True}), ErrorKind.MALFORMED))
        for outcome, kind in cases:
            with self.subTest(kind=kind):
                self.pionir.orders = [order()]
                self.pionir.orders_outcome = outcome
                self.pionir.jobs.clear()
                result = self.run_at()
                self.assertIsInstance(result, Err)
                self.assertEqual(result.error.kind, kind)
                self.assertIn("UNKNOWN", result.error.message)
                self.assertNotIn("delivery.tally", str(result))
                self.assertEqual(self.pionir.deliveries(), [])

    def test_no_state_dir_no_hands_no_folder_or_an_unreadable_record_refuse(self) -> None:
        for ctx in (WorkContext(now=T0, http=None, secrets_dir=self.state, job=self.pionir.job,
                                deliveries_dir=self.drops),
                    WorkContext(now=T0, http=None, secrets_dir=self.state,
                                state_dir=self.state, deliveries_dir=self.drops),
                    WorkContext(now=T0, http=None, secrets_dir=self.state,
                                job=self.pionir.job, state_dir=self.state)):
            self.assertEqual(self.worker.run(ctx).error.kind, ErrorKind.NOT_CONFIGURED)
        self.worker.record_path(self.state).parent.mkdir(parents=True, exist_ok=True)
        self.worker.record_path(self.state).write_text("{not json", encoding="utf-8")
        self.pionir.orders = [order()]
        self.assertEqual(self.run_at().error.kind, ErrorKind.MALFORMED)
        self.assertEqual(self.pionir.jobs, [])

    def test_one_delivery_waits_on_the_owner_at_a_time(self) -> None:
        self.pionir.orders = [order()]
        self.drop(name="v1.zip")
        self.run_at(T0)
        self.drop(name="v2.zip", data=b"PK v2", mtime=T0)
        self.run_at(T0 + 600)
        self.assertEqual(len(self.pionir.deliveries()), 1)
        self.pionir.deny("dl-1")
        self.run_at(T0 + 1200)
        self.assertEqual([j.payload["zip_name"] for j in self.pionir.deliveries()],
                         ["v1.zip", "v2.zip"])
        self.assertTrue(re.fullmatch(r"[0-9a-f]{64}",
                                     self.pionir.deliveries()[1].payload["zip_sha256"]))


if __name__ == "__main__":
    unittest.main()
