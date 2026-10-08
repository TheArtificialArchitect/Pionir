"""Marketplaces: the spec, its fail-closed check, and its one hook into the Builds backlog.

Each test fails if its rule is reverted: a dishonest or personal-data spec accepted, the
model's numbers used as evidence, a marketplace entry refused by (or priced like) the Gumroad
backlog, a Gumroad entry's rules loosened, two marketplace products waiting in the backlog at
once, a JavaScript product queued before the night builds can test it, or a Shopify app sent
to the night builds at all.
"""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from marketplace_support import APIFY_WORDS, apify_spec, chrome_spec, shopify_spec, spec_for

from pionir.adapters.marketplace_listing import claim_problems
from pionir.crew.builds import backlog as bl
from pionir.crew.marketplaces import specs


class SpecCheckTests(unittest.TestCase):
    def test_the_three_example_specs_pass(self) -> None:
        for spec in (apify_spec(), chrome_spec(), shopify_spec()):
            self.assertEqual(specs.spec_problems(spec), [], spec["market"])

    def test_unmeasured_claims_are_refused(self) -> None:
        for bad in ("The best sitemap checker", "Guaranteed to find every error",
                    "100% accurate audits", "Trusted by thousands of SEOs", "GDPR compliant"):
            spec = apify_spec()
            spec["summary"] = f"{bad}, reading every URL a sitemap lists for you."
            self.assertTrue(specs.spec_problems(spec), bad)

    def test_personal_data_is_never_specified(self) -> None:
        words = copy.deepcopy(APIFY_WORDS)
        words["brief"] = ("It reads each page and collects the email addresses and phone numbers "
                          "of the people it finds, into one table.")
        self.assertTrue(any("personal data" in r for r in
                            specs.spec_problems(spec_for("apify", words))))

    def test_the_evidence_is_measured_never_the_models(self) -> None:
        words = {**APIFY_WORDS, "evidence": {"demand": 10**9}}
        spec = spec_for("apify", words)
        self.assertEqual(spec["evidence"]["demand"], 5200)        # the niche's, as measured
        # and the Store categories come from the competing Actors, not from the model
        self.assertEqual(spec["listing"]["categories"], ["SEO_TOOLS", "DEVELOPER_TOOLS"])

    def test_store_rules(self) -> None:
        spec = apify_spec()
        spec["listing"]["event"]["price_usd"] = 0.5
        self.assertTrue(specs.spec_problems(spec))
        spec = apify_spec()
        spec["listing"]["categories"] = ["LEAD_GENERATION"]
        self.assertTrue(specs.spec_problems(spec))
        spec = chrome_spec()
        spec["listing"]["permissions"] = ["tabs", "<all_urls>"]
        self.assertTrue(specs.spec_problems(spec))
        spec = shopify_spec()
        spec["listing"]["scopes"] = ["read_customers"]
        self.assertTrue(specs.spec_problems(spec))

    def test_links_to_elsewhere_are_refused(self) -> None:
        self.assertTrue(claim_problems({"d": "see https://evil.test/x"}))
        self.assertEqual(claim_problems({"d": "see https://apify.com/store"}), [])


class BacklogHookTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.builds = Path(tmp.name) / "builds"

    def test_a_marketplace_entry_is_a_valid_backlog_entry(self) -> None:
        entry = specs.to_backlog_entry(apify_spec())
        self.assertEqual(bl.entry_problems(entry), [])
        self.assertEqual(entry["price_cents"], 0)
        self.assertEqual(bl.product_type(entry), "apify_actor")
        self.assertIn("process(record)", entry["features"][-1])

    def test_the_gumroad_rules_are_unchanged(self) -> None:
        seed = dict(bl.SEED[0])
        self.assertEqual(bl.entry_problems(seed), [])
        self.assertEqual(bl.product_type(seed), bl.GUMROAD)
        self.assertTrue(bl.entry_problems({**seed, "price_cents": 0}))          # still $9-19
        self.assertTrue(bl.entry_problems({**seed, "product_type": "rocket"}))
        self.assertTrue(bl.entry_problems({**seed, "colour": "red"}))           # unknown key
        marketplace = specs.to_backlog_entry(apify_spec())
        self.assertTrue(bl.entry_problems({**marketplace, "price_cents": 1200}))
        self.assertTrue(bl.entry_problems({**marketplace, "product_type": "shopify_app"}))

    def test_one_marketplace_product_waits_at_a_time(self) -> None:
        ok, note = specs.queue_for_build(self.builds, apify_spec(), set())
        self.assertTrue(ok, note)
        doc = bl.load(self.builds)
        self.assertEqual(doc["products"][-1]["slug"], "sitemap-audit")
        self.assertEqual(len(doc["products"]), len(bl.SEED) + 1)    # the seeds kept
        other = apify_spec()
        other["slug"] = "robots-audit"
        ok, note = specs.queue_for_build(self.builds, other, set())
        self.assertFalse(ok)
        self.assertIn("already waiting", note)
        # once the night builds took it, the next one may wait
        ok, _ = specs.queue_for_build(self.builds, other, {"sitemap-audit"})
        self.assertTrue(ok)

    def test_the_same_slug_is_never_queued_twice(self) -> None:
        self.assertTrue(specs.queue_for_build(self.builds, apify_spec(), set())[0])
        ok, note = specs.queue_for_build(self.builds, apify_spec(), {"x"})
        self.assertFalse(ok)
        self.assertIn("already", note)

    def test_javascript_waits_and_shopify_never_goes_to_the_night_builds(self) -> None:
        ok, why = specs.queue_for_build(self.builds, chrome_spec(), set())
        self.assertFalse(ok)
        self.assertIn("javascript", why)
        ok, why = specs.queue_for_build(self.builds, shopify_spec(), set())
        self.assertFalse(ok)
        self.assertIn("not built", why)
        self.assertFalse((self.builds / "backlog.json").exists())   # nothing was written


if __name__ == "__main__":
    unittest.main()
