"""The Fiverr gigs: prices nobody but the site or the owner sets, honest listings, and the
drafter that hands each one to the owner once.

Each test fails if the rule it names is reverted: a price grossed up to less than the site's
net, a site price used after its source changed, a proposal shown as a price in the listing
the owner pastes, an owner price read from a reply that is not exactly one, a listing that
claims hand-made work or invents reviews, counts or credentials, a gig redrafted without the
owner asking, or a deliverable check that lets a secret, the owner's data, a local path, an
internal name, contact details or money through.
"""
from __future__ import annotations

import copy
import dataclasses
import tempfile
import unittest
from pathlib import Path

from test_fiverr_desk import FakePionir

from pionir.crew.fiverr import checks, gigs, prices
from pionir.crew.fiverr.gigs import SERVICES, GigDrafter, check_gig, render_markdown
from pionir.crew.fiverr.prices import (
    SITE_PRICES,
    Mirror,
    Proposal,
    gross_for_net,
    net_of,
    parse_owner_prices,
    price_packages,
    verify_site_prices,
)
from pionir.crew.registry import WorkerSpec
from pionir.crew.result import Ok
from pionir.crew.worker import WorkContext

# Built from pieces so no provider-shaped key sits in the source (GitHub push protection).
FAKE_STRIPE = "sk_" + "live_" + "REALOWNERSECRET12345678"

T0 = 1_790_000_000.0
GUARD = checks.Guard(markers=("owner-real-name",))


class PriceTests(unittest.TestCase):
    def test_the_gross_up_never_leaves_the_owner_less_than_the_site(self) -> None:
        for net in (500, 1900, 2000, 5700, 9900, 14900, 24900, 12345):
            gross = gross_for_net(net)
            self.assertEqual(gross % 100, 0)                     # whole dollars on Fiverr
            self.assertGreaterEqual(net_of(gross), net)
            self.assertLess(net_of(gross - 100), net)            # and no more than needed
        self.assertEqual(gross_for_net(1900), 2400)              # $19 on the site -> $24

    def test_a_site_price_is_checked_against_its_source(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "Scrooge" / "worker" / "src"
            src.mkdir(parents=True)
            orders = src / "orders.ts"
            orders.write_text("find: {\n id: 'find',\n cents: 1900,\n days: 2 }", encoding="utf-8")
            self.assertEqual(verify_site_prices(Path(d), ["find"]), [])
            orders.write_text("find: {\n id: 'find',\n cents: 2900,\n days: 2 }", encoding="utf-8")
            problems = verify_site_prices(Path(d), ["find"])
            self.assertTrue(problems and "$29" in problems[0], problems)
            orders.write_text("nothing here", encoding="utf-8")
            self.assertTrue(verify_site_prices(Path(d), ["find"]))
        self.assertEqual(verify_site_prices(None, ["find"]), [])  # not on this machine

    def test_the_real_sources_still_say_what_the_constants_say(self) -> None:
        # read-only; skipped where the owner's repos are not checked out
        root = Path("C:/src")
        present = [k for k, sp in SITE_PRICES.items() if (root / sp.repo / sp.path).is_file()]
        if not present:
            self.skipTest("the site's source is not on this machine")
        self.assertEqual(verify_site_prices(root, present), [])

    def test_a_proposal_is_never_a_price(self) -> None:
        priced = price_packages({"basic": Mirror("find"),
                                 "standard": Proposal(5700, "why", ("find",)),
                                 "premium": Proposal(9500, "why", ("find",))})
        self.assertEqual(priced["basic"].gross_cents, 2400)
        self.assertIsNone(priced["standard"].gross_cents)
        self.assertEqual(priced["standard"].proposed_gross_cents, 7200)
        md = render_markdown(SERVICES["research"], priced)
        self.assertIn("Price: $24 on Fiverr", md)
        self.assertNotIn("$72", md)                              # the proposal stays off it
        self.assertEqual(md.count("price NOT SET"), 2)

    def test_the_owner_sets_every_package_with_his_own_reply(self) -> None:
        self.assertEqual(parse_owner_prices("price 25 60 110"),
                         {"basic": 2500, "standard": 6000, "premium": 11000})
        self.assertEqual(parse_owner_prices("Price $25 $60 $110")["premium"], 11000)
        for bad in ("25 60 110", "price 25 60", "price 60 25 110", "price 2 60 110",
                    "price 25 60 110 please", "price twenty 60 110", "", None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_owner_prices(bad)
        priced = price_packages(SERVICES["data"].prices,
                                {"basic": 2500, "standard": 6000, "premium": 11000})
        self.assertEqual({p.status for p in priced.values()}, {"owner"})


class ListingTests(unittest.TestCase):
    def test_all_four_listings_pass_their_checks(self) -> None:
        self.assertEqual(sorted(SERVICES), ["data", "research", "uptime", "website"])
        for svc in SERVICES.values():
            with self.subTest(svc.key):
                self.assertEqual(check_gig(svc, GUARD), [])
                self.assertLessEqual(len(svc.title), 80)
                self.assertIn(svc.made_by, svc.description)
        self.assertIn("AI-assisted", SERVICES["website"].description)
        self.assertIn("AI-assisted", SERVICES["research"].description)

    def test_the_two_gigs_with_impressions_use_the_words_buyers_search(self) -> None:
        # Fiverr ranks on title, tags and the opening of the description; the 2026-10 rewrite
        # moved them to buyer search language. Reverting the rewrite fails this.
        research, website = SERVICES["research"], SERVICES["website"]
        self.assertIn("find any hard to find product", research.title)
        self.assertTrue({"product finder", "find a product"} <= set(research.tags))
        self.assertTrue(research.description.startswith("Can't find a product"))
        self.assertIn("design", website.title)
        self.assertIn("landing page", website.title)
        self.assertTrue({"website design", "landing page", "one page website"}
                        <= set(website.tags))
        self.assertTrue(website.description.startswith("Need a simple, fast website or landing"))
        for svc in (research, website):
            self.assertEqual(len(svc.tags), len(set(svc.tags)))
            self.assertIn("What I won't search for" if svc is research else "never make up",
                          svc.description)

    def test_dishonest_listings_are_blocked(self) -> None:
        base = SERVICES["website"]
        for change, words in [
            ({"title": "I will hand-code your website " + "x" * 60}, "80"),
            ({"description": base.description + "\nEvery page is handcrafted by me."},
             "hand-made"),
            ({"description": base.description + "\n500+ happy clients so far!"},
             "nothing backs"),
            ({"description": base.description + "\n10 years of experience."}, "nothing backs"),
            ({"description": base.description + "\nTop rated, certified expert."},
             "nothing backs"),
            ({"description": base.description.replace(base.made_by, "")}, "how the work"),
            ({"description": base.description + "\nOnly $50!"}, "money"),
            ({"description": base.description + "\nEmail me at me@example.org"}, "email"),
            ({"description": base.description + "\nSee www.mysite.com"}, "link"),
            ({"description": base.description + "\nBuilt by Moss."}, "internal system"),
            ({"tags": ("a", "b", "c", "d", "e", "f")}, "search tags"),
        ]:
            with self.subTest(words=words):
                reasons = check_gig(dataclasses.replace(base, **change), GUARD)
                self.assertTrue(any(words in r for r in reasons), reasons)


class DrafterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._t = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._t.name)
        (self.root / "secrets").mkdir()
        self.pionir = FakePionir()
        self.drafter = GigDrafter(WorkerSpec("fiverr.gigs", "gigs", "fiverr", "fiverr_gigs",
                                             "fiverr", 3600, "none"), source_root=None)

    def tearDown(self) -> None:
        self._t.cleanup()

    def run_drafter(self, now=T0):
        return self.drafter.run(WorkContext(now=now, http=None, secrets_dir=self.root / "secrets",
                                            job=self.pionir.job, state_dir=self.root / "state",
                                            fiverr_dir=self.root / "fiverr"))

    def test_one_card_per_service_with_its_listing_and_image(self) -> None:
        self.assertIsInstance(self.run_drafter(), Ok)
        self.assertEqual(sorted(self.pionir.cards), ["gig:data:v1", "gig:research:v1",
                                                     "gig:uptime:v1", "gig:website:v1"])
        card = self.pionir.cards["gig:research:v1"][0]
        self.assertEqual(card["kind"], "gig")
        self.assertTrue(card["replies"])
        self.assertEqual(card["files"], ["gigs/research/gig.md", "gigs/research/gig.png"])
        self.assertIn("$24", card["body"])
        self.assertIn("NOT SET. Proposed $72", card["body"])
        png = (self.root / "fiverr" / "gigs" / "research" / "gig.png").read_bytes()
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_no_redraft_until_the_owner_asks(self) -> None:
        self.run_drafter()
        self.run_drafter(T0 + 3600)
        self.assertEqual(len(self.pionir.cards), 4)
        self.pionir.replies.append({"reply_id": "1500", "key": "gig:data:v1", "kind": "gig",
                                    "ref": "data", "text": "redraft", "at": "x"})
        self.run_drafter(T0 + 7200)
        self.run_drafter(T0 + 9000)                              # the reply is read once
        self.assertIn("gig:data:v2", self.pionir.cards)
        self.assertEqual(len(self.pionir.cards), 5)

    def test_the_owners_price_reply_prices_and_redrafts(self) -> None:
        self.run_drafter()
        self.pionir.replies.append({"reply_id": "1501", "key": "gig:uptime:v1", "kind": "gig",
                                    "ref": "uptime", "text": "price 25 50 75", "at": "x"})
        self.run_drafter(T0 + 60)
        body = self.pionir.cards["gig:uptime:v2"][0]["body"]
        self.assertIn("$25 (the owner's reply", body)
        self.assertNotIn("NOT SET", body)
        md = (self.root / "fiverr" / "gigs" / "uptime" / "gig.md").read_text(encoding="utf-8")
        self.assertIn("$75 on Fiverr", md)

    def test_an_unreadable_reply_changes_nothing_and_says_so(self) -> None:
        self.run_drafter()
        self.pionir.replies.append({"reply_id": "1502", "key": "gig:data:v1", "kind": "gig",
                                    "ref": "data", "text": "make it cheaper", "at": "x"})
        self.run_drafter(T0 + 60)
        self.assertNotIn("gig:data:v2", self.pionir.cards)
        self.assertIn("gig-note:data:1502", self.pionir.cards)

    def test_a_changed_site_price_blocks_the_gig(self) -> None:
        src = self.root / "src" / "Scrooge" / "worker" / "src"
        src.mkdir(parents=True)
        (src / "orders.ts").write_text("find: { cents: 2900 }\nsmall: { cents: 14900 }",
                                       encoding="utf-8")
        self.drafter.source_root = self.root / "src"
        self.run_drafter()
        self.assertIn("gig-blocked:research:v1", self.pionir.cards)
        self.assertNotIn("gig:research:v1", self.pionir.cards)

    def test_a_blocked_gig_card_that_did_not_post_is_tried_again(self) -> None:
        src = self.root / "src" / "Scrooge" / "worker" / "src"
        src.mkdir(parents=True)
        (src / "orders.ts").write_text("find: { cents: 2900 }\nsmall: { cents: 14900 }",
                                       encoding="utf-8")
        self.drafter.source_root = self.root / "src"
        self.pionir.discord_down = True
        self.run_drafter()
        self.assertEqual(self.pionir.cards, {})
        self.pionir.discord_down = False
        self.run_drafter(T0 + 3600)
        self.run_drafter(T0 + 7200)
        self.assertEqual(len(self.pionir.cards["gig-blocked:research:v1"]), 1)

    def test_pionir_off_is_not_configured(self) -> None:
        from pionir.crew.hands import JobOutcome
        real = self.pionir.job
        self.pionir.job = lambda j: (JobOutcome("failed", j.capability,
                                                error="unknown capability fiverr.inbox")
                                     if j.capability == gigs.INBOX else real(j))
        got = self.run_drafter()
        self.assertEqual(got.error.kind.value, "not_configured")
        self.assertEqual(self.pionir.cards, {})


class DeliverableCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self._t = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        d = Path(self._t.name)
        (d / "stripe.txt").write_text(FAKE_STRIPE, encoding="utf-8")
        self.guard = checks.load_guard(d, None, ("ian example", "ian@personal.example"))
        self.dir = d

    def tearDown(self) -> None:
        self._t.cleanup()

    def test_what_may_never_reach_a_buyer(self) -> None:
        for text, words in [
            ("see " + FAKE_STRIPE, "secret"),
            ("made by Ian Example", "personal data"),
            ("saved in C:\\Users\\someone\\out.csv", "local path"),
            ("saved in /home/someone/out.csv", "local path"),
            ("our Scrooge system did it", "internal system"),
            ("mail me at me@mail.example.org", "email"),
            ("call +44 20 7946 0958", "phone"),
            ("details at www.somewhere.com", "link"),
        ]:
            with self.subTest(words=words):
                reasons = checks.check_text("report", text, self.guard)
                self.assertTrue(any(words in r for r in reasons), reasons)
        self.assertEqual(checks.check_text("report", "Italian and median words", self.guard), [])

    def test_the_buyers_own_words_and_data_are_theirs(self) -> None:
        brief = "My shop is moss-and-fern.co.uk, call 020 7946 0958"
        self.assertEqual(checks.check_text("report", "Your site moss-and-fern.co.uk is up.",
                                           self.guard, brief=brief), [])
        # the buyer's data: their emails and paths stay; the owner's secret does not
        self.assertEqual(checks.check_text("data.csv", "a@b.co.uk,C:\\Users\\bob\\x",
                                           self.guard, ours=False), [])
        self.assertTrue(checks.check_text("data.csv", FAKE_STRIPE,
                                          self.guard, ours=False))

    def test_a_drafted_reply_states_no_money_and_claims_nothing_hand_made(self) -> None:
        ok = "Hi there,\n\nYour report is attached. Reply here with any questions."
        self.assertEqual(checks.check_reply(ok, self.guard), [])
        self.assertTrue(checks.check_reply(ok + " It's only $20 more.", self.guard))
        self.assertTrue(checks.check_reply(ok + " All hand-written by me.", self.guard))
        self.assertTrue(checks.check_reply("x" * 3000, self.guard))

    def test_a_buyers_site_is_matched_exactly_in_our_text(self) -> None:
        brief = "My shop is moss-and-fern.co.uk"
        for ok in ("Your site moss-and-fern.co.uk is up.", "See www.moss-and-fern.co.uk.",
                   "https://shop.moss-and-fern.co.uk/x"):
            self.assertEqual(checks.check_text("r", ok, self.guard, brief=brief), [], ok)
        for bad in ("see evilmoss-and-fern.co.uk", "see moss-and-fern.co.uk.evil.com",
                    "see fern.co.uk"):
            self.assertTrue(checks.check_text("r", bad, self.guard, brief=brief), bad)

    def test_the_owners_markers_never_stop_a_buyers_own_data(self) -> None:
        guard = checks.Guard(secrets=self.guard.secrets, markers=("ian",))
        customers = self.dir / "customers-clean.csv"
        customers.write_text("name,city\nIan Smith,Leeds\nAna,York\n", encoding="utf-8")
        self.assertEqual(checks.check_file(customers, guard, ours=False), [])
        from pionir.crew.fiverr import data
        book = self.dir / "customers-clean.xlsx"
        book.write_bytes(data.to_xlsx(data.Table("c", ["name"], [["Ian Smith"]])))
        self.assertEqual(checks.check_file(book, guard, ours=False), [])
        # the same word in a text WE wrote is still the owner's data
        self.assertTrue(checks.check_text("report", "Prepared by Ian.", guard))
        # and a marker only matches as a whole word: "Christian" is not "ian"
        self.assertEqual(checks.check_text("report", "Christian bakery", guard), [])

    def test_a_workbook_is_scanned_unpacked(self) -> None:
        from pionir.crew.fiverr import data
        table = data.Table("t", ["k"], [[FAKE_STRIPE]])
        book = self.dir / "t.xlsx"
        book.write_bytes(data.to_xlsx(table))
        self.assertTrue(checks.check_file(book, self.guard, ours=False))
        table.rows = [["fine"]]
        book.write_bytes(data.to_xlsx(table))
        self.assertEqual(checks.check_file(book, self.guard, ours=False), [])


class DataCleanupTests(unittest.TestCase):
    def test_the_cleanup_counts_every_change_and_keeps_values(self) -> None:
        from pionir.crew.fiverr import data
        t = data.Table("x.csv", [" name ", "", "name", "zip"],
                       [[" Ana ", "", "A", "02134"], ["", "", "", ""],
                        [" Ana ", "", "A", "02134"], ["Bo", "", "B"], ["Cy", "", "C", "=1+1", "x"]])
        clean, counts = data.clean(copy.deepcopy(t))
        self.assertEqual(clean.header, ["name", "name_2", "zip", "extra_1"])
        self.assertEqual(clean.rows[0], ["Ana", "A", "02134", ""])       # leading zero kept
        self.assertEqual(counts["duplicate_rows_removed"], 1)
        self.assertEqual(counts["empty_rows_removed"], 1)
        self.assertEqual(counts["empty_columns_removed"], 1)
        self.assertEqual(counts["formula_like_cells"], 1)
        roundtrip = data._read_xlsx(data.to_xlsx(clean), "x.xlsx")
        self.assertEqual(roundtrip.header, clean.header)
        self.assertEqual(roundtrip.rows[2][2], "=1+1")                    # text, not a formula

    def test_the_brief_picks_the_formats(self) -> None:
        from pionir.crew.fiverr import data
        self.assertEqual(data.wanted_formats("convert my CSV to Excel please", []), ["xlsx"])
        self.assertEqual(data.wanted_formats("json to csv", []), ["csv"])
        self.assertEqual(data.wanted_formats("clean it up", []), ["csv", "json", "xlsx"])


if __name__ == "__main__":
    unittest.main()
