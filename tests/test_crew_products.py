"""The product shelf: each staged product put on sale on Gumroad, only on the owner's yes.

Pionir is a fake (``FakePionir``): it parks every publish under its own approval id (or
refuses it, as its pre-park checks would), reports the publish on approval, and lists the
Gumroad products with their sales. The owner's folder is a temp ``products_dir``. Each test
fails if the rule it names is reverted: content submitted twice, more than one a run, a typed
refusal retried, a passing failure never retried (or for ever), a malformed listing or a slug
that is not its folder submitted, a half-copied file hashed, a product called published before
Pionir says so, or unreadable sales reported as zeros.
"""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from pionir.crew import products as shelf_mod
from pionir.crew.hands import JobOutcome
from pionir.crew.products import (
    LIST,
    LISTING_KEYS,
    PUBLISH,
    ProductShelf,
    check_listing,
)
from pionir.crew.registry import default_registry
from pionir.crew.result import Err, Ok
from pionir.crew.worker import ErrorKind, WorkContext

T0 = 1_790_000_000.0
SECRETS = "zip: secrets found - an API key in src/config.py; nothing was parked"
DESC = ("A small kit that renames invoice PDFs by their date and number, with a README that "
        "shows every step on Windows and Linux.")


def listing(slug="invoice-kit", **over) -> dict:
    d = {"slug": slug, "name": "Invoice Renamer Kit", "version": "1.0.0", "price_cents": 1900,
         "pay_what_you_want": False, "summary": "Rename invoice PDFs by date and number.",
         "description_md": DESC, "tags": ["invoices", "pdf"], "zip_name": "kit.zip",
         "cover_name": "cover.png", "allow_executables": False}
    d.update(over)
    return d


class FakePionir:
    """``ctx.job`` and ``ctx.approval``: publishes parked or refused, products listed."""

    def __init__(self) -> None:
        self.jobs: list = []
        self.approvals: dict = {}
        self.publish_outcome = None
        self.list_outcome = None
        self.listed: list = []

    def job(self, job):
        self.jobs.append(job)
        if job.capability == LIST:
            if self.list_outcome is not None:
                return self.list_outcome
            return JobOutcome("done", LIST, task_id="t-l",
                              result={"ok": True, "products": [dict(p) for p in self.listed]})
        if job.capability == PUBLISH:
            if self.publish_outcome is not None:
                return self.publish_outcome
            n = len(self.publishes())
            return JobOutcome("pending_approval", PUBLISH, task_id=f"t-{n}",
                              approval_id=f"pr-{n}")
        raise AssertionError(f"unexpected capability {job.capability}")

    def approval(self, approval_id):
        return dict(self.approvals.get(approval_id, {"status": "pending"}))

    def approve(self, approval_id, **inner) -> None:
        payload = self.payload_of(approval_id)
        result = {"ok": True, "product_id": f"gp-{payload['slug']}",
                  "url": f"https://dokaz.gumroad.com/l/{payload['slug']}", "created": True,
                  "published": True, "version": payload["version"]}
        result.update(inner)
        self.approvals[approval_id] = {"id": approval_id, "status": "approved", "result": {
            "ok": True, "agent_id": "product", "result": result}}

    def approved_failed(self, approval_id, response) -> None:
        self.approvals[approval_id] = {"id": approval_id, "status": "approved_failed",
                                       "result": response}

    def deny(self, approval_id) -> None:
        self.approvals[approval_id] = {"id": approval_id, "status": "denied", "reason": "no"}

    def publishes(self) -> list:
        return [j for j in self.jobs if j.capability == PUBLISH]

    def payload_of(self, approval_id) -> dict:
        return self.publishes()[int(approval_id.split("-")[1]) - 1].payload


GUMROAD_DOWN = {"ok": False, "agent_id": "product", "result": {
    "ok": False, "error": "Gumroad answered HTTP 503; the product was NOT published"}}
CHANGED = {"ok": False, "error": {"type": "AdapterProtocolError",
                                  "message": "zip_sha256: the zip changed since it was parked"}}


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "state"
        self.shelf = Path(tmp.name) / "products"
        self.worker = default_registry().require("products.shelf")
        self.pionir = FakePionir()

    def stage(self, slug="invoice-kit", *, zip_data=b"PK\x03\x04 v1", cover=b"\x89PNG cover",
              mtime=T0 - 3600, raw=None, **over):
        """Stage a product; returns (zip_sha256, cover_sha256)."""
        folder = self.shelf / slug
        folder.mkdir(parents=True, exist_ok=True)
        lst = listing(slug, **over)
        files = {"listing.json": raw if raw is not None else json.dumps(lst).encode()}
        if isinstance(lst.get("zip_name"), str) and "/" not in lst["zip_name"]:
            files[lst["zip_name"]] = zip_data
        if isinstance(lst.get("cover_name"), str) and "/" not in lst["cover_name"]:
            files[lst["cover_name"]] = cover
        for name, data in files.items():
            path = folder / name
            path.write_bytes(data)
            os.utime(path, (mtime, mtime))
        return hashlib.sha256(zip_data).hexdigest(), hashlib.sha256(cover).hexdigest()

    def touch(self, slug, name, mtime) -> None:
        os.utime(self.shelf / slug / name, (mtime, mtime))

    def run_at(self, now=T0):
        ctx = WorkContext(now=now, http=None, secrets_dir=self.state, job=self.pionir.job,
                          approval=self.pionir.approval, state_dir=self.state,
                          products_dir=self.shelf)
        result = self.worker.run(ctx)
        self.assertIsInstance(result, Ok, result)
        return result

    def record(self) -> list:
        path = self.worker.record_path(self.state)
        return json.loads(path.read_text(encoding="utf-8"))["submissions"]

    @staticmethod
    def rows(result, kind) -> list:
        return [o for o in result.value if o.kind == kind]

    def tally(self, result) -> dict:
        (t,) = self.rows(result, "product.tally")
        return {f.measures: f.value for f in t.figures}

    def tally_payload(self, result) -> dict:
        (t,) = self.rows(result, "product.tally")
        return t.payload


class CatalogueTests(unittest.TestCase):
    def test_the_shelf_is_in_products_every_half_hour(self) -> None:
        reg = default_registry()
        w = reg.require("products.shelf")
        self.assertIsInstance(w, ProductShelf)
        self.assertEqual((w.division, w.cadence_seconds, w.live), ("products", 1800, True))
        notes = reg.division("products").leader_notes
        self.assertIn("URGENT", notes)
        self.assertIn("NEW SALES", notes)
        self.assertIn("Never call a product live", notes)
        self.assertIn("UNKNOWN, never zero", notes)
        self.assertIn("never invent a sale", notes)


class PublishTests(_Case):
    def test_a_ready_folder_is_one_publish_job_then_published_once_approved(self) -> None:
        zip_sha, cover_sha = self.stage()
        result = self.run_at(T0)
        (job,) = self.pionir.publishes()
        self.assertEqual(job.permissions, ())             # nothing granted: Pionir parks it
        self.assertEqual(job.payload, {**listing(), "zip_sha256": zip_sha,
                                       "cover_sha256": cover_sha})
        self.assertEqual(set(job.payload), LISTING_KEYS | {"zip_sha256", "cover_sha256"})
        (entry,) = self.record()
        self.assertEqual((entry["status"], entry["approval_id"], entry["kind"]),
                         ("pending_approval", "pr-1", "new"))
        (pending,) = self.rows(result, "product.pending")
        self.assertEqual(pending.payload["slug"], "invoice-kit")
        t = self.tally(result)
        self.assertEqual((t["products pending the owner's approval"],
                          t["products staged but not yet approved"],
                          t["product versions published"]), (1, 1, 0))
        # still waiting: not published, not submitted again
        result = self.run_at(T0 + 1800)
        self.assertEqual(len(self.pionir.publishes()), 1)
        self.assertEqual(self.rows(result, "product.published"), [])
        # the owner says yes: published - now, and only now
        self.pionir.approve("pr-1")
        result = self.run_at(T0 + 3600)
        (done,) = self.rows(result, "product.published")
        self.assertEqual((done.payload["slug"], done.payload["url"]),
                         ("invoice-kit", "https://dokaz.gumroad.com/l/invoice-kit"))
        entry = self.record()[0]
        self.assertEqual((entry["status"], entry["product_id"]), ("published", "gp-invoice-kit"))
        t = self.tally(result)
        self.assertEqual((t["product versions published"],
                          t["products staged but not yet approved"],
                          t["products pending the owner's approval"]), (1, 0, 0))
        for i in range(3):
            self.run_at(T0 + 5400 + i * 1800)
        self.assertEqual(len(self.pionir.publishes()), 1)

    def test_an_approval_without_published_true_and_a_url_is_not_published(self) -> None:
        self.stage()
        self.run_at(T0)
        self.pionir.approve("pr-1", published=False)
        result = self.run_at(T0 + 1800)
        self.assertEqual(self.rows(result, "product.published"), [])
        self.assertEqual(self.record()[0]["status"], "undelivered")
        self.assertEqual(self.tally(result)["product versions published"], 0)

    def test_unchanged_content_is_never_resubmitted_even_rewritten(self) -> None:
        self.stage()
        self.run_at(T0)
        self.pionir.deny("pr-1")
        self.run_at(T0 + 1800)
        self.assertEqual(self.record()[0]["status"], "denied")
        # the owner rewrites listing.json with another layout, and copies the same files again
        raw = json.dumps(dict(reversed(list(listing().items()))), indent=4).encode()
        self.stage(raw=raw, mtime=T0)
        for i in range(3):
            result = self.run_at(T0 + (i + 2) * 1800)
        self.assertEqual(len(self.pionir.publishes()), 1)
        t = self.tally(result)
        self.assertEqual((t["products denied by the owner"],
                          t["products staged but not yet approved"]), (1, 1))

    def test_a_changed_zip_cover_or_listing_is_a_new_submission(self) -> None:
        self.stage()
        self.run_at(T0)
        self.pionir.approve("pr-1")
        self.run_at(T0 + 1800)
        zip2, _cover = self.stage(zip_data=b"PK\x03\x04 v2", version="1.1.0", mtime=T0 + 1800)
        result = self.run_at(T0 + 3600)
        self.assertEqual(len(self.pionir.publishes()), 2)
        second = self.pionir.publishes()[1].payload
        self.assertEqual((second["zip_sha256"], second["version"]), (zip2, "1.1.0"))
        self.assertEqual(self.rows(result, "product.pending")[0].payload["kind"], "update")
        self.pionir.approve("pr-2")
        self.run_at(T0 + 5400)
        # a new cover alone is new content too
        _zip, cover3 = self.stage(zip_data=b"PK\x03\x04 v2", version="1.1.0",
                                  cover=b"\x89PNG new cover", mtime=T0 + 5400)
        self.run_at(T0 + 7200)
        self.assertEqual(self.pionir.publishes()[2].payload["cover_sha256"], cover3)
        self.pionir.approve("pr-3")
        self.run_at(T0 + 9000)
        # and so is a changed price in the listing, the files the same
        self.stage(zip_data=b"PK\x03\x04 v2", version="1.1.0", cover=b"\x89PNG new cover",
                   price_cents=2900, mtime=T0 + 9000)
        self.run_at(T0 + 10800)
        self.assertEqual([j.payload["price_cents"] for j in self.pionir.publishes()],
                         [1900, 1900, 1900, 2900])

    def test_at_most_one_publish_a_run(self) -> None:
        self.stage("alpha-kit", zip_data=b"PK a")
        self.stage("beta-kit", zip_data=b"PK b")
        result = self.run_at(T0)
        self.assertEqual([j.payload["slug"] for j in self.pionir.publishes()], ["alpha-kit"])
        self.assertEqual(self.tally_payload(result)["ready_next_run"], 1)
        self.run_at(T0 + 1800)
        self.assertEqual([j.payload["slug"] for j in self.pionir.publishes()],
                         ["alpha-kit", "beta-kit"])

    def test_one_submission_per_product_waits_on_the_owner(self) -> None:
        self.stage()
        self.run_at(T0)
        self.stage(zip_data=b"PK changed", mtime=T0)
        result = self.run_at(T0 + 1800)
        self.assertEqual(len(self.pionir.publishes()), 1)
        self.assertEqual(self.tally_payload(result)["waiting_behind_a_pending_one"],
                         ["invoice-kit"])
        self.pionir.deny("pr-1")
        self.run_at(T0 + 3600)
        self.assertEqual(len(self.pionir.publishes()), 2)

    def test_files_still_being_copied_are_skipped(self) -> None:
        for name in ("kit.zip", "cover.png", "listing.json"):
            with self.subTest(name=name):
                self.setUp()
                self.stage()
                self.touch("invoice-kit", name, T0 - 30)
                result = self.run_at(T0)
                self.assertEqual(self.pionir.publishes(), [])
                self.assertEqual(self.tally_payload(result)["still_copying"], ["invoice-kit"])
                self.run_at(T0 + 1800)
                self.assertEqual(len(self.pionir.publishes()), 1)

    def test_a_folder_without_a_listing_is_not_a_product(self) -> None:
        (self.shelf / "half-done").mkdir(parents=True)
        result = self.run_at(T0)
        self.assertEqual(self.pionir.publishes(), [])
        self.assertEqual(self.tally_payload(result)["folders_without_listing"], ["half-done"])
        self.assertEqual(self.tally(result)["products staged"], 0)


class ListingCheckTests(_Case):
    def test_a_malformed_listing_is_reported_once_never_submitted_until_fixed(self) -> None:
        self.stage(price_cents=50)
        result = self.run_at(T0)
        self.assertEqual(self.pionir.publishes(), [])
        (bad,) = self.rows(result, "product.listing_malformed")
        self.assertEqual(bad.payload["folder"], "invoice-kit")
        self.assertIn("price_cents", " ".join(bad.payload["reasons"]))
        self.assertEqual(self.tally(result)["product listings malformed (not submitted)"], 1)
        # the same listing: still listed in the tally, not reported again, never submitted
        result = self.run_at(T0 + 1800)
        self.assertEqual(self.rows(result, "product.listing_malformed"), [])
        self.assertEqual(self.tally_payload(result)["malformed"][0]["folder"], "invoice-kit")
        self.assertEqual(self.pionir.publishes(), [])
        # fixed
        self.stage(mtime=T0)
        result = self.run_at(T0 + 3600)
        self.assertEqual(len(self.pionir.publishes()), 1)
        self.assertEqual(self.tally(result)["product listings malformed (not submitted)"], 0)

    def test_unreadable_json_is_malformed(self) -> None:
        self.stage(raw=b"{not json")
        result = self.run_at(T0)
        self.assertEqual(self.pionir.publishes(), [])
        (bad,) = self.rows(result, "product.listing_malformed")
        self.assertIn("not readable JSON", bad.payload["reasons"][0])

    def test_a_slug_that_is_not_its_folder_is_refused_locally(self) -> None:
        folder = self.shelf / "invoice-kit"
        folder.mkdir(parents=True)
        for name, data in (("listing.json", json.dumps(listing("other-kit")).encode()),
                           ("kit.zip", b"PK"), ("cover.png", b"png")):
            (folder / name).write_bytes(data)
            os.utime(folder / name, (T0 - 3600, T0 - 3600))
        result = self.run_at(T0)
        self.assertEqual(self.pionir.publishes(), [])
        (bad,) = self.rows(result, "product.listing_malformed")
        self.assertIn("is not the folder's name", " ".join(bad.payload["reasons"]))

    def test_the_check_fails_closed(self) -> None:
        folder = self.shelf / "invoice-kit"
        self.stage()
        self.assertEqual(check_listing(listing(), folder), [])
        bad = {
            "a missing key": {k: v for k, v in listing().items() if k != "tags"},
            "an unknown key": {**listing(), "extra": 1},
            "a short name": listing(name="Kit"),
            "a two-line name": listing(name="Invoice\nKit"),
            "a bad version": listing(version="1.0"),
            "a price too high": listing(price_cents=100001),
            "a price as a bool": listing(price_cents=True),
            "a price in dollars": listing(price_cents=19.0),
            "pwyw not a bool": listing(pay_what_you_want="no"),
            "a short summary": listing(summary="Too short."),
            "a two-line summary": listing(summary="Rename invoice PDFs\nby date and number."),
            "a short description": listing(description_md="Short."),
            "six tags": listing(tags=["aa", "bb", "cc", "dd", "ee", "ff"]),
            "a bad tag": listing(tags=["Invoices"]),
            "a path in zip_name": listing(zip_name="../kit.zip"),
            "a missing zip": listing(zip_name="gone.zip"),
            "a hidden cover": listing(cover_name=".cover.png"),
            "the same file twice": listing(cover_name="kit.zip"),
            "executables not a bool": listing(allow_executables=None),
            "a bad slug": listing(slug="Invoice_Kit"),
            "not an object": ["a list"],
        }
        for why, lst in bad.items():
            with self.subTest(why=why):
                self.assertNotEqual(check_listing(lst, folder), [], why)


class RefusedTests(_Case):
    def test_a_product_the_checks_refuse_is_recorded_counted_and_never_retried(self) -> None:
        self.stage()
        self.pionir.publish_outcome = JobOutcome("failed", PUBLISH, error=SECRETS,
                                                 error_type="AdapterProtocolError")
        result = self.run_at(T0)
        (entry,) = self.record()
        self.assertEqual(entry["status"], "refused")
        self.assertIn("secrets found", entry["why"])
        (blocked,) = self.rows(result, "product.blocked")
        self.assertEqual(blocked.payload["slug"], "invoice-kit")
        self.assertIn("secrets found", blocked.payload["why"])
        t = self.tally(result)
        self.assertEqual((t["products blocked by the checks"],
                          t["product submissions refused by Pionir's checks"],
                          t["products pending the owner's approval"]), (1, 1, 0))
        (row,) = self.tally_payload(result)["blocked"]
        self.assertIn("secrets found", row["why"])
        # Pionir would park it now - but it is the same content, so it is never offered again
        self.pionir.publish_outcome = None
        for i in range(3):
            result = self.run_at(T0 + (i + 1) * 1800)
        self.assertEqual(len(self.pionir.publishes()), 1)
        self.assertEqual(self.tally(result)["products blocked by the checks"], 1)
        # a fixed zip is a new attempt
        self.stage(zip_data=b"PK fixed", mtime=T0)
        result = self.run_at(T0 + 9000)
        self.assertEqual(len(self.pionir.publishes()), 2)
        self.assertEqual(self.tally(result)["products blocked by the checks"], 0)

    def test_pionirs_typed_error_decides_not_the_wording(self) -> None:
        # typed refusal worded like an outage: still final
        self.stage("alpha-kit", zip_data=b"PK a")
        self.pionir.publish_outcome = JobOutcome(
            "failed", PUBLISH, error="cover: temporarily unavailable image, too small",
            error_type="AdapterProtocolError")
        for i in range(3):
            self.run_at(T0 + i * 1800)
        self.assertEqual(len(self.pionir.publishes()), 1)
        self.assertEqual(self.record()[0]["status"], "refused")
        # typed outage worded like a refusal: retried
        self.stage("beta-kit", zip_data=b"PK b")
        self.pionir.publish_outcome = JobOutcome("failed", PUBLISH, error=SECRETS,
                                                 error_type="AdapterUnavailable")
        self.run_at(T0 + 9000)
        self.run_at(T0 + 10800)
        beta = [e for e in self.record() if e["slug"] == "beta-kit"]
        self.assertEqual([e["status"] for e in beta], ["unreachable", "unreachable"])

    def test_a_passing_failure_is_retried_then_given_up(self) -> None:
        self.stage()
        self.pionir.publish_outcome = JobOutcome("failed", PUBLISH,
                                                 error="Gumroad circuit open",
                                                 error_type="CircuitOpen")
        for i in range(8):
            result = self.run_at(T0 + i * 1800)
        self.assertEqual(len(self.pionir.publishes()), shelf_mod.RETRY_UNREACHABLE)
        self.assertEqual(self.tally(result)["products whose submission failed"], 1)
        self.assertEqual(self.tally(result)["products blocked by the checks"], 0)

    def test_an_unreachable_pionir_is_retried(self) -> None:
        self.stage()
        self.pionir.publish_outcome = JobOutcome("unreachable", PUBLISH, error="refused conn")
        self.run_at(T0)
        self.pionir.publish_outcome = None
        self.run_at(T0 + 1800)
        self.assertEqual([e["status"] for e in self.record()],
                         ["unreachable", "pending_approval"])

    def test_an_approved_product_that_hit_an_outage_is_offered_again_then_stops(
            self) -> None:
        self.stage()
        for i in range(shelf_mod.RETRY_UNDELIVERED + 2):
            self.run_at(T0 + i * 3600)
            last = f"pr-{len(self.pionir.publishes())}"
            self.pionir.approved_failed(last, GUMROAD_DOWN)
            self.run_at(T0 + i * 3600 + 1800)
        self.assertEqual(len(self.pionir.publishes()), shelf_mod.RETRY_UNDELIVERED)
        self.assertTrue(all(e["status"] == "undelivered" for e in self.record()))

    def test_a_refusal_on_approval_is_final(self) -> None:
        self.stage()
        self.run_at(T0)
        self.pionir.approved_failed("pr-1", CHANGED)
        result = self.run_at(T0 + 1800)
        self.assertEqual(self.record()[0]["status"], "failed")
        (row,) = self.rows(result, "product.not_published")
        self.assertEqual(row.payload["status"], "failed")
        for i in range(3):
            self.run_at(T0 + (i + 2) * 1800)
        self.assertEqual(len(self.pionir.publishes()), 1)

    def test_an_unset_capability_does_not_burn_the_product(self) -> None:
        self.stage()
        self.pionir.publish_outcome = JobOutcome("failed", PUBLISH,
                                                 error="unknown capability " + PUBLISH)
        result = self.run_at(T0)
        self.assertEqual(self.record(), [])
        self.assertTrue(self.tally_payload(result)["publish_not_set_up"])
        self.pionir.publish_outcome = None
        self.run_at(T0 + 1800)
        self.assertEqual([e["status"] for e in self.record()], ["pending_approval"])


class SalesTests(_Case):
    def test_sales_that_cannot_be_read_are_unavailable_not_zero(self) -> None:
        for outcome in (JobOutcome("unreachable", LIST, error="no answer"),
                        JobOutcome("failed", LIST, error="Gumroad down",
                                   error_type="AdapterUnavailable"),
                        JobOutcome("done", LIST, result={"ok": True})):
            with self.subTest(outcome=outcome.status):
                self.pionir.list_outcome = outcome
                result = self.run_at(T0)
                self.assertEqual(self.rows(result, "product.sales"), [])
                (row,) = self.rows(result, "product.sales_unavailable")
                self.assertEqual(row.payload["sales"], "UNAVAILABLE")
                self.assertEqual(row.figures, ())
                figs = [f for o in result.value for f in o.figures]
                self.assertFalse([f for f in figs if f.measures in ("sales", "revenue")])

    def test_each_live_product_has_its_sales_and_revenue_as_gumroad_reports_them(
            self) -> None:
        self.pionir.listed = [
            {"id": "g1", "slug": "invoice-kit", "name": "Invoice Renamer Kit",
             "published": True, "price_cents": 1900, "sales_count": 3,
             "sales_usd_cents": 5700, "url": "https://dokaz.gumroad.com/l/invoice-kit"},
            {"id": "g2", "slug": "qr-kit", "name": "QR Batch Kit", "published": True,
             "price_cents": 900, "sales_count": 1, "sales_usd_cents": 900,
             "url": "https://dokaz.gumroad.com/l/qr-kit"},
            {"id": "g3", "slug": "draft-kit", "name": "Draft Kit", "published": False,
             "price_cents": 500, "sales_count": 0, "sales_usd_cents": 0, "url": ""},
            {"id": "g4", "slug": "broken", "published": True, "sales_count": "many",
             "sales_usd_cents": 100},
        ]
        result = self.run_at(T0)
        (row,) = self.rows(result, "product.sales")
        figs = {(f.measures, f.stream): f.value for f in row.figures}
        self.assertEqual(figs[("sales", "invoice-kit")], 3)
        self.assertEqual(figs[("revenue", "invoice-kit")], 5700)
        self.assertEqual(figs[("sales", "qr-kit")], 1)
        self.assertEqual(figs[("revenue", "qr-kit")], 900)
        self.assertEqual((figs[("sales", "all")], figs[("revenue", "all")]), (4, 6600))
        self.assertEqual(figs[("products live on Gumroad", "")], 2)
        self.assertNotIn(("sales", "draft-kit"), figs)     # not live: no sales row
        self.assertNotIn(("sales", "broken"), figs)        # malformed: never a made-up zero
        self.assertEqual(row.payload["listed_not_live"], ["draft-kit"])
        self.assertEqual(row.payload["malformed_rows"], 1)
        self.assertEqual({f.unit for f in row.figures if f.measures == "revenue"},
                         {"usd_cents"})
        self.assertIn("Invoice", row.entities)             # the leader may name the product

    def test_new_sales_are_news_but_the_first_look_is_the_baseline(self) -> None:
        p = {"id": "g1", "slug": "invoice-kit", "name": "Invoice Renamer Kit",
             "published": True, "price_cents": 1900, "sales_count": 3,
             "sales_usd_cents": 5700, "url": "https://dokaz.gumroad.com/l/invoice-kit"}
        self.pionir.listed = [p]
        result = self.run_at(T0)
        self.assertEqual(self.rows(result, "product.new_sales"), [])
        result = self.run_at(T0 + 1800)
        self.assertEqual(self.rows(result, "product.new_sales"), [])
        self.pionir.listed = [{**p, "sales_count": 5, "sales_usd_cents": 9500}]
        result = self.run_at(T0 + 3600)
        (news,) = self.rows(result, "product.new_sales")
        self.assertEqual((news.payload["slug"], news.payload["new_sales"]), ("invoice-kit", 2))
        figs = {f.measures: f.value for f in news.figures}
        self.assertEqual((figs["new sales"], figs["new revenue"]), (2, 3800))
        result = self.run_at(T0 + 5400)
        self.assertEqual(self.rows(result, "product.new_sales"), [])

    def test_the_sales_are_read_every_run_even_with_nothing_staged(self) -> None:
        self.run_at(T0)
        self.run_at(T0 + 1800)
        self.assertEqual([j.capability for j in self.pionir.jobs], [LIST, LIST])


class RefuseToRunTests(_Case):
    def test_no_state_dir_no_hands_no_folder_or_an_unreadable_record_refuse(self) -> None:
        base = {"now": T0, "http": None, "secrets_dir": self.state, "job": self.pionir.job,
                "approval": self.pionir.approval, "state_dir": self.state,
                "products_dir": self.shelf}
        for missing in ("state_dir", "job", "products_dir"):
            with self.subTest(missing=missing):
                got = self.worker.run(WorkContext(**{**base, missing: None}))
                self.assertIsInstance(got, Err)
                self.assertEqual(got.error.kind, ErrorKind.NOT_CONFIGURED)
        self.state.mkdir(parents=True)
        self.worker.record_path(self.state).write_text("{broken", encoding="utf-8")
        self.stage()
        got = self.worker.run(WorkContext(**base))
        self.assertIsInstance(got, Err)
        self.assertEqual(got.error.kind, ErrorKind.MALFORMED)
        self.assertEqual(self.pionir.jobs, [])


if __name__ == "__main__":
    unittest.main()
