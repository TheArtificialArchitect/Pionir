"""Affiliate links in "Find it for me" reports (crew/affiliate.py): the owner's tag on the
shop links the finder found, only when he has set a program up, disclosed to the client and
marked on his card - and never a change to what is recommended.

Each test fails if the rule it names is reverted: a lookalike host (amazon.com.evil.example)
or a shortener rewritten, someone else's tag or tracking passed along, a link added, dropped
or reordered, the disclosure missing when a link is tagged (or present when none is), the
report with nothing set up differing by one byte from before, the card not showing which
links are tagged, or a commission reported as if it were seen.
"""

import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from crew_support import temp_dir
from test_crew_fakes import catalogue, make_crew
from test_crew_finder import GOOD, NOT_FOUND, FakeResearch, FinderPionir, find_order

from pionir.adapters.clients import AFFILIATE_DISCLOSURE, FIND_REPORT, check_find_report
from pionir.crew.affiliate import (
    DISCLOSURE,
    apply,
    programs_from_environment,
    rewrite,
)
from pionir.crew.config import CrewSettings
from pionir.crew.finder import MAX_BODY, build_report, check_report, validate_research
from pionir.crew.registry import default_registry
from pionir.crew.result import Ok
from pionir.crew.worker import WorkContext
from pionir.discord_gate import render_request

T0 = 1_790_000_000.0
TAG = "dokaz-20"
CAMPID = "5339999999"
ENV = {"PIONIR_AFFILIATE_AMAZON_TAG": TAG, "PIONIR_AFFILIATE_EBAY_CAMPID": CAMPID}
PROGRAMS = programs_from_environment(ENV)

# as Claude finds them: someone else's tag, tracking, a slug and a ref in the path
AMAZON_FOUND = ("https://www.amazon.com/Sony-Alpha-a7-III-Mirrorless/dp/B07B43WPVK/ref=sr_1_3"
                "?crid=2XQ&keywords=sony+a7&tag=someoneelse-20&ascsubtag=abc&linkCode=ll1"
                "&th=1&psc=1&utm_source=x")
AMAZON_OURS = f"https://www.amazon.com/dp/B07B43WPVK?tag={TAG}"
EBAY_FOUND = ("https://www.ebay.com/itm/Sony-a7-III-body/204991337712?mkcid=1"
              "&mkrid=711-53200-19255-0&siteid=0&campid=5338000000&customid=theirs"
              "&toolid=10001&mkevt=1&_trkparms=abc")
EBAY_OURS = ("https://www.ebay.com/itm/204991337712?mkcid=1&mkrid=711-53200-19255-0"
             f"&siteid=0&campid={CAMPID}&toolid=10001&mkevt=1")
SHOP = "https://www.cameraworld.co.uk/sony-a7-iii-used-8812"

MIXED = {
    "found": True,
    "summary": "Three listings: a camera shop, Amazon and eBay.",
    "options": [
        {"seller": "Camera World", "url": SHOP, "price": 899, "currency": "GBP",
         "condition": "used", "availability": "in stock", "notes": "Shutter count 8,000."},
        {"seller": "Amazon", "url": AMAZON_FOUND, "price": 1498, "currency": "USD",
         "condition": "new", "availability": "in stock", "notes": ""},
        {"seller": "eBay", "url": EBAY_FOUND, "price": 1100, "currency": "USD",
         "condition": "used", "availability": "1 left", "notes": "Seller rated 99.8%."},
    ],
    "caveats": "",
}

# sha256 of json.dumps(build_report(ORDER, research), sort_keys=True), computed with the
# finder as it was BEFORE affiliate links existed (commit 0f31362): with nothing set up,
# every report must still be exactly that.
BEFORE = {
    "GOOD": "d81c1042bd65dc9ddcba4b39d78cad1fc2885bfea6c7b3b6180a7211dab15c1d",
    "NOT_FOUND": "1a3995ab132c4eec6d5b1309d696c60dd463dd28d097f97cc2abb3c3950ea289",
    "MIXED": "0b603b3c2e643672cfb60eb78b7353db380ee791327632cd664a227b7ed0d45d",
}
ORDER = {"id": "a1b2c3d4e5f6", "email": "ana@example.com", "name": "Ana Lima", "brief":
         "A used Sony A7 III camera body with under 10,000 shutter count."}


def checked(research: dict) -> dict:
    data, reasons = validate_research(copy.deepcopy(research))
    assert not reasons, reasons
    return data


def digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


# ---- the rewrite rule --------------------------------------------------------------------------
class RewriteTests(unittest.TestCase):
    def test_a_product_page_gets_our_tag_and_loses_every_other_parameter(self) -> None:
        self.assertEqual(rewrite(AMAZON_FOUND, PROGRAMS), AMAZON_OURS)
        self.assertEqual(rewrite(EBAY_FOUND, PROGRAMS), EBAY_OURS)
        for url in (rewrite(AMAZON_FOUND, PROGRAMS), rewrite(EBAY_FOUND, PROGRAMS)):
            for theirs in ("someoneelse", "ascsubtag", "linkCode", "utm_", "crid", "ref=",
                           "5338000000", "customid", "_trkparms", "psc", "keywords"):
                self.assertNotIn(theirs, url)

    def test_every_amazon_product_path_and_the_bare_host(self) -> None:
        for found in ("https://www.amazon.com/dp/B07B43WPVK",
                      "https://www.amazon.com/dp/B07B43WPVK/",
                      "https://www.amazon.com/dp/B07B43WPVK?tag=other-20",
                      "https://www.amazon.com/gp/product/B07B43WPVK?th=1",
                      "https://www.amazon.com/gp/aw/d/B07B43WPVK",
                      "https://www.amazon.com/Some-Slug/dp/B07B43WPVK/ref=sr_1_1#reviews"):
            with self.subTest(found):
                self.assertEqual(rewrite(found, PROGRAMS), AMAZON_OURS)
        self.assertEqual(rewrite("https://amazon.com/dp/B07B43WPVK?tag=x-20", PROGRAMS),
                         f"https://amazon.com/dp/B07B43WPVK?tag={TAG}")

    def test_an_ebay_variation_is_kept(self) -> None:
        self.assertEqual(rewrite("https://www.ebay.com/itm/204991337712?var=5012&campid=1",
                                 PROGRAMS),
                         "https://www.ebay.com/itm/204991337712?var=5012&mkcid=1"
                         f"&mkrid=711-53200-19255-0&siteid=0&campid={CAMPID}&toolid=10001"
                         "&mkevt=1")

    def test_rewriting_our_own_link_again_changes_nothing(self) -> None:
        self.assertEqual(rewrite(AMAZON_OURS, PROGRAMS), AMAZON_OURS)
        self.assertEqual(rewrite(EBAY_OURS, PROGRAMS), EBAY_OURS)

    def test_lookalike_unsupported_and_unresolvable_links_are_left_alone(self) -> None:
        for url in (
            "https://amazon.com.evil.example/dp/B07B43WPVK",
            "https://www.amazon.com.evil.example/dp/B07B43WPVK",
            "https://evilamazon.com/dp/B07B43WPVK",
            "https://amazon.com-deals.example/dp/B07B43WPVK",
            "https://smile.amazon.com/dp/B07B43WPVK",
            "https://www.amazon.co.uk/dp/B07B43WPVK",        # not a configured store
            "https://WWW.AMAZON.COM/dp/B07B43WPVK",
            "https://www.amazon.com:443/dp/B07B43WPVK",
            "https://user@www.amazon.com/dp/B07B43WPVK",
            "http://www.amazon.com/dp/B07B43WPVK",
            "https://amzn.to/3xYzAbC",                      # shorteners: never resolved
            "https://a.co/d/abc1234",
            "https://ebay.us/AbCdEf",
            "https://www.amazon.com/s?k=sony+a7+iii",       # not a product page
            "https://www.amazon.com/stores/Sony/page/ABC",
            "https://www.amazon.com/gp/redirect.html?location=https://evil.example/x",
            "https://www.amazon.com/gp/r.html?U=https://evil.example",
            "https://www.amazon.com/dp/b07b43wpvk",          # not an ASIN
            "https://www.amazon.com/dp/B07B43WPVK/extra/path",
            "https://www.ebay.com/sch/i.html?_nkw=sony",
            "https://www.ebay.com/itm/notanumber",
            "https://www.ebay.com/str/someshop",
            "https://www.walmart.com/ip/Sony-a7/123456",
            SHOP,
        ):
            with self.subTest(url):
                self.assertIsNone(rewrite(url, PROGRAMS))

    def test_nothing_set_up_rewrites_nothing(self) -> None:
        self.assertEqual(programs_from_environment({}), ())
        self.assertIsNone(rewrite(AMAZON_FOUND, ()))
        opts = checked(MIXED)["options"]
        self.assertEqual(apply(opts, ()), (opts, []))

    def test_a_program_rewrites_only_its_own_stores(self) -> None:
        amazon_only = programs_from_environment({"PIONIR_AFFILIATE_AMAZON_TAG": TAG})
        self.assertEqual(rewrite(AMAZON_FOUND, amazon_only), AMAZON_OURS)
        self.assertIsNone(rewrite(EBAY_FOUND, amazon_only))


class SettingsTests(unittest.TestCase):
    def test_the_stores_and_their_tags(self) -> None:
        (amazon,) = programs_from_environment({
            "PIONIR_AFFILIATE_AMAZON_TAG": TAG,
            "PIONIR_AFFILIATE_AMAZON_STORES": "amazon.com, www.amazon.co.uk=dokaz-21"})
        self.assertEqual([(s.domain, s.params) for s in amazon.stores],
                         [("amazon.com", (("tag", TAG),)),
                          ("amazon.co.uk", (("tag", "dokaz-21"),))])
        self.assertEqual(rewrite("https://www.amazon.co.uk/dp/B07B43WPVK", (amazon,)),
                         "https://www.amazon.co.uk/dp/B07B43WPVK?tag=dokaz-21")

    def test_a_bad_value_or_an_unknown_store_is_ignored_fail_closed(self) -> None:
        for env in ({"PIONIR_AFFILIATE_AMAZON_TAG": "bad tag&x=1"},
                    {"PIONIR_AFFILIATE_AMAZON_TAG": TAG,
                     "PIONIR_AFFILIATE_AMAZON_STORES": "amazon.com.evil.example"},
                    {"PIONIR_AFFILIATE_AMAZON_TAG": "   "},
                    {"PIONIR_AFFILIATE_EBAY_CAMPID": "12345"},
                    {"PIONIR_AFFILIATE_EBAY_CAMPID": CAMPID,
                     "PIONIR_AFFILIATE_EBAY_SITES": "ebay.evil.example"}):
            with self.subTest(env), self.assertLogs("pionir.crew", "ERROR") \
                    if any(v.strip() for v in env.values()) else _nothing():
                self.assertEqual(programs_from_environment(env), ())

    def test_the_crew_reads_them_from_its_environment_and_shows_no_tag(self) -> None:
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.dict(os.environ, {**ENV, "PIONIR_STATE_ROOT": root}):
            cfg = CrewSettings.from_environment()
        self.assertEqual(cfg.affiliates, PROGRAMS)
        self.assertEqual(cfg.public()["affiliates"],
                         [{"program": "Amazon Associates", "stores": ["amazon.com"]},
                          {"program": "eBay Partner Network", "stores": ["ebay.com"]}])
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.dict(os.environ, {"PIONIR_STATE_ROOT": root}):
            for key in ENV:
                os.environ.pop(key, None)
            self.assertEqual(CrewSettings.from_environment().affiliates, ())

    def test_the_crew_hands_its_programs_to_every_worker_it_runs(self) -> None:
        with temp_dir() as root:
            crew = make_crew(root, cat=catalogue({"contracts": [{"name": "w"}]}),
                             affiliates=PROGRAMS)
            try:
                ctx = crew.context_for(crew.registry.require("contracts.w"))
                self.assertEqual(ctx.affiliates, PROGRAMS)
            finally:
                crew.stop()


class _nothing:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ---- the report -----------------------------------------------------------------------------
class ReportTests(unittest.TestCase):
    def test_nothing_set_up_is_byte_identical_to_the_report_before(self) -> None:
        for name, research in (("GOOD", GOOD), ("NOT_FOUND", NOT_FOUND), ("MIXED", MIXED)):
            with self.subTest(name):
                data = checked(research)
                self.assertEqual(digest(build_report(ORDER, data)), BEFORE[name])
                self.assertEqual(digest(build_report(ORDER, data, ())), BEFORE[name])
        # set up, but no link it covers: still exactly the same report
        self.assertEqual(digest(build_report(ORDER, checked(GOOD), PROGRAMS)), BEFORE["GOOD"])

    def test_tagged_links_are_disclosed_and_named_for_the_card(self) -> None:
        p = build_report(ORDER, checked(MIXED), PROGRAMS)
        self.assertEqual(p["links"], [SHOP, AMAZON_OURS, EBAY_OURS])
        self.assertEqual(p["affiliate_links"], [AMAZON_OURS, EBAY_OURS])
        body = p["body_text"]
        self.assertEqual(body.count(DISCLOSURE), 1)
        self.assertLess(body.index(DISCLOSURE), body.index(SHOP))   # above the links
        self.assertNotIn(AMAZON_FOUND, body)
        self.assertNotIn("someoneelse", body)
        self.assertEqual(check_report(p, ORDER), [])
        check_find_report(p)                                   # Pionir's own contract

    def test_no_tagged_link_no_disclosure(self) -> None:
        p = build_report(ORDER, checked(GOOD), PROGRAMS)
        self.assertNotIn("affiliate_links", p)
        self.assertNotIn(DISCLOSURE, p["body_text"])
        self.assertNotIn("affiliate", p["body_text"].lower())

    def test_the_recommendation_is_the_same_with_and_without_affiliate_links(self) -> None:
        data = checked(MIXED)
        plain = build_report(ORDER, data)
        tagged = build_report(ORDER, data, PROGRAMS)

        def ranking(payload):
            lines = payload["body_text"].split("\n")
            return [line for line in lines if line[:3] in ("1. ", "2. ", "3. ")]

        self.assertEqual(ranking(plain), ranking(tagged))          # sellers, prices, order
        self.assertEqual(len(plain["links"]), len(tagged["links"]))
        from urllib.parse import urlsplit
        self.assertEqual([urlsplit(u).hostname for u in plain["links"]],
                         [urlsplit(u).hostname for u in tagged["links"]])
        self.assertEqual(plain["body_text"],
                         tagged["body_text"].replace(DISCLOSURE + "\n\n", "")
                         .replace(AMAZON_OURS, AMAZON_FOUND).replace(EBAY_OURS, EBAY_FOUND))
        # every option, reversed: still the same order through the rewrite
        rev = dict(MIXED, options=list(reversed(MIXED["options"])))
        self.assertEqual(ranking(build_report(ORDER, checked(rev))),
                         ranking(build_report(ORDER, checked(rev), PROGRAMS)))

    def test_two_links_to_one_product_are_not_made_one(self) -> None:
        second = "https://www.amazon.com/gp/product/B07B43WPVK?tag=other-20"
        research = dict(MIXED, options=[*MIXED["options"],
                                        dict(MIXED["options"][1], seller="Amazon again",
                                             url=second)])
        p = build_report(ORDER, checked(research), PROGRAMS)
        self.assertEqual(p["links"], [SHOP, AMAZON_OURS, EBAY_OURS, second])
        self.assertEqual(check_report(p, ORDER), [])

    def test_a_trimmed_report_discloses_only_if_a_tagged_link_is_left(self) -> None:
        # long links force the lowest options out (build_report's trimming): the disclosure
        # follows the links that are left, not the links that were found
        shops = [dict(MIXED["options"][0], seller=f"Shop {i} {'s' * 70}", notes="n" * 150,
                      url=f"https://shop{i}.example.com/{'p' * 470}-{i}") for i in range(7)]
        amazon = dict(MIXED["options"][1], notes="n" * 150)
        for opts, kept in (([*shops, amazon], False), ([amazon, *shops], True)):
            data = dict(checked(MIXED), summary="s" * 480, caveats="c" * 290, options=opts)
            with self.subTest(amazon_kept=kept):
                p = build_report(ORDER, data, PROGRAMS)
                self.assertLessEqual(len(p["body_text"]), MAX_BODY)
                self.assertIn("trimmed)", p["body_text"])
                self.assertEqual(AMAZON_OURS in p["links"], kept)
                self.assertEqual(DISCLOSURE in p["body_text"], kept)
                self.assertEqual(p.get("affiliate_links"), [AMAZON_OURS] if kept else None)
                self.assertEqual(check_report(p, ORDER), [])

    def test_the_check_holds_the_disclosure_to_the_links(self) -> None:
        good = build_report(ORDER, checked(MIXED), PROGRAMS)
        plain = build_report(ORDER, checked(GOOD))
        cases = {
            "no disclosure": {**good, "body_text": good["body_text"].replace(DISCLOSURE, "")},
            "disclosure, no tagged link": {
                **plain, "body_text": plain["body_text"].replace(
                    "Where to buy it:\n\n", "Where to buy it:\n\n" + DISCLOSURE + "\n\n")},
            "tagged link not a link": {**good, "affiliate_links": [AMAZON_FOUND]},
            "tagged twice": {**good, "affiliate_links": [AMAZON_OURS, AMAZON_OURS]},
            "empty list": {**good, "affiliate_links": []},
            "not a list": {**good, "affiliate_links": AMAZON_OURS},
        }
        check_find_report(good)
        check_find_report(plain)
        for name, payload in cases.items():
            with self.subTest(name):
                reasons = check_report(payload, ORDER)
                self.assertTrue(reasons and all("affiliate" in r for r in reasons), reasons)
                with self.assertRaisesRegex(ValueError, "affiliate"):
                    check_find_report(payload)

    def test_the_disclosure_is_the_same_words_on_both_sides(self) -> None:
        self.assertEqual(DISCLOSURE, AFFILIATE_DISCLOSURE)


# ---- the worker, the card and the counts --------------------------------------------------------
class FinderRunTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.worker = default_registry().require("contracts.finder")

    def run_with(self, programs, pionir, research, now=T0):
        ctx = WorkContext(now=now, http=None, secrets_dir=self.state, job=pionir.job,
                          approval=pionir.approval, state_dir=self.state, research=research,
                          affiliates=programs)
        result = self.worker.run(ctx)
        self.assertIsInstance(result, Ok, result)
        return result

    @staticmethod
    def tally(result):
        (t,) = [o for o in result.value if o.kind == "find.tally"]
        return t, {f.measures: f.value for f in t.figures if f.unit == "count"}

    def test_the_research_and_the_ranking_do_not_depend_on_the_programs(self) -> None:
        runs = []
        for programs in ((), PROGRAMS):
            with tempfile.TemporaryDirectory() as state:
                self.state = Path(state)
                pionir, research = FinderPionir(find_order()), FakeResearch(MIXED)
                self.run_with(programs, pionir, research)
                (job,) = pionir.reports()
                runs.append((research.prompts, job.payload))
        (prompts0, plain), (prompts1, tagged) = runs
        self.assertEqual(prompts0, prompts1)            # Claude is asked the same thing
        self.assertNotIn("affiliate", prompts1[0].lower())
        self.assertEqual([u.split("/")[2] for u in plain["links"]],
                         [u.split("/")[2] for u in tagged["links"]])
        self.assertEqual(tagged["affiliate_links"], [AMAZON_OURS, EBAY_OURS])
        self.assertNotIn("affiliate_links", plain)

    def test_sent_reports_with_tagged_links_are_counted_never_revenue(self) -> None:
        pionir, research = FinderPionir(find_order()), FakeResearch(MIXED)
        result = self.run_with(PROGRAMS, pionir, research)
        t, n = self.tally(result)
        self.assertEqual((n["affiliate programs set up"],
                          n["find reports sent with affiliate links"]), (2, 0))  # parked
        pionir.approve("fr-1")
        t, n = self.tally(self.run_with(PROGRAMS, pionir, research, T0 + 600))
        self.assertEqual((n["find reports sent with affiliate links"],
                          n["affiliate links in find reports sent"]), (1, 2))
        self.assertIn("not observed", t.payload["affiliate_commission"])
        self.assertFalse([f for f in t.figures if f.unit == "usd_cents"
                          and "affiliate" in f.measures])
        self.assertNotIn(TAG, json.dumps(t.payload))

    def test_the_card_shows_which_links_carry_the_tag(self) -> None:
        payload = build_report(ORDER, checked(MIXED), PROGRAMS)
        row = {"id": "a1", "capability": FIND_REPORT, "payload": payload,
               "summary": "order ord_1", "requester": "contracts.finder"}
        card = render_request(row, "123")
        self.assertIn("AFFILIATE LINKS: 2 of 3", card)
        self.assertIn(f"<{AMAZON_OURS}> - **affiliate link**", card)
        self.assertIn(f"<{EBAY_OURS}> - **affiliate link**", card)
        self.assertIn(f"<{SHOP}>\n", card)                  # the shop's link is not marked
        self.assertIn(DISCLOSURE, card)                     # the report, as the client reads it
        plain = render_request({**row, "payload": build_report(ORDER, checked(MIXED))}, "123")
        self.assertNotIn("affiliate", plain.lower())


if __name__ == "__main__":
    unittest.main()
