"""Grounding: a report may state only what a worker recorded - value AND unit.

Each test fails if the check is loosened: "$12" backed by "12 replies", a structured
figure backed by the same number in another unit, a name nothing recorded, or a figure a
model wrote vouching for itself.
"""

import unittest

from pionir.crew.figures import Figure
from pionir.crew.grounding import (
    HEALTHY,
    UNCLEAR,
    UNHEALTHY,
    backs,
    check_report,
    claims_in,
    health_claims,
    topic_claims,
    unbacked_claims,
    unbacked_figures,
    unknown_names,
    vocabulary_words,
)

REPLIES = Figure(12, "count", "replies")
REVENUE = Figure(1200, "usd_cents", "revenue", "all", "mtd")


class TypedFigureTests(unittest.TestCase):
    def test_twelve_dollars_is_not_backed_by_twelve_replies(self) -> None:
        self.assertEqual([c.text for c in unbacked_claims("We made $12 this month.",
                                                          [REPLIES])], ["$12"])
        stated = Figure(12, "usd_cents", "revenue")
        self.assertEqual(unbacked_figures([stated], [REPLIES]), [stated])
        (claim,) = claims_in("$12")
        self.assertFalse(backs(REPLIES, claim))

    def test_twelve_replies_is_not_backed_by_twelve_dollars_either(self) -> None:
        self.assertTrue(unbacked_claims("We got 12 replies.", [Figure(12, "count", "sales")]))
        self.assertTrue(unbacked_claims("We got 12 replies.", [Figure(1200, "usd_cents", "x")]))

    def test_the_right_unit_backs_it(self) -> None:
        self.assertEqual(unbacked_claims("We made $12 and got 12 replies.",
                                         [REVENUE, REPLIES]), [])
        self.assertEqual(unbacked_claims("$12.00 so far", [REVENUE]), [])
        self.assertEqual(unbacked_figures([Figure(1200, "usd_cents", "revenue")], [REVENUE]), [])

    def test_whole_dollar_rounding_only(self) -> None:
        recorded = [Figure(46980, "usd_cents", "revenue")]
        self.assertEqual(unbacked_claims("about $470", recorded), [])      # $469.80
        self.assertTrue(unbacked_claims("about $471", recorded))
        self.assertTrue(unbacked_claims("exactly $469.00", recorded))

    def test_a_payload_number_backs_a_bare_number_only(self) -> None:
        self.assertEqual(unbacked_claims("Topics left: 13.", [], [13.0]), [])
        self.assertTrue(unbacked_claims("Topics left: 13.", [], [14.0]))
        self.assertTrue(unbacked_claims("We made $13.", [], [13.0]))
        self.assertTrue(unbacked_claims("We got 13 replies.", [], [13.0]))

    def test_other_currencies_are_never_backed(self) -> None:
        self.assertTrue(unbacked_claims("we made £12", [REVENUE]))

    def test_durations_times_and_years_are_not_claims(self) -> None:
        self.assertEqual(claims_in("checked 12 min ago, at 10:30, in 2026"), [])

    def test_a_structured_figure_must_match_what_it_measures(self) -> None:
        self.assertTrue(unbacked_figures([Figure(1200, "usd_cents", "gross_revenue")],
                                         [REVENUE]))
        self.assertTrue(unbacked_figures([Figure(1200, "usd_cents", "revenue", "api")],
                                         [REVENUE]))


class NameTests(unittest.TestCase):
    def test_a_name_nothing_recorded_is_caught(self) -> None:
        self.assertEqual(unknown_names("Sales came from Etsy and Gumroad.", {"Gumroad"}),
                         ["Etsy"])
        self.assertEqual(unknown_names("Nothing new from Gumroad today.", {"gumroad"}), [])


class OrdinaryWordTests(unittest.TestCase):
    """The live crew's leaders were rejected for these, word for word."""

    LIVE = (
        "Posting Division Report: Two Drafts Blocked, One Post Pending Approval",
        "Contracts Division Report: No New Paid Orders",
        "Revenue Data Stale; Web Sales Minimal",
        "Treasury Reporting Shows Recent Activity, Target Missed",
        "Posting Status: Workers Unconfigured",
        "The contracts Division Report says the Finder never ran.",
    )

    def test_ordinary_words_in_a_title_case_headline_are_not_names(self) -> None:
        for text in self.LIVE:
            self.assertEqual(unknown_names(text, set()), [], text)
            self.assertEqual(check_report([text], [], [Figure(2, "count", "drafts blocked")],
                                          set()), [], text)

    def test_the_briefs_own_vocabulary_is_not_a_name(self) -> None:
        vocab = vocabulary_words(["posting.instagram", "www.facebook.com", "card-press"])
        self.assertEqual(unknown_names("Instagram and Facebook sent visits to Card-Press.",
                                       set(), vocab), [])
        self.assertEqual(unknown_names("Instagram sent visits.", set()), ["Instagram"])


class InventedNameTests(unittest.TestCase):
    def test_a_name_no_dictionary_holds_fails_anywhere_in_a_sentence(self) -> None:
        self.assertEqual(unknown_names("Etsy sent two sales.", set()), ["Etsy"])
        self.assertEqual(unknown_names("Two sales came from Etsy.", set()), ["Etsy"])
        self.assertEqual(unknown_names("Sales Via Etsy Rose", set()), ["Etsy"])

    def test_a_proper_noun_fails(self) -> None:
        self.assertEqual(unknown_names("A client, Kimberly, paid.", set()), ["Kimberly"])

    def test_a_dual_word_alone_mid_sentence_is_a_name(self) -> None:
        self.assertEqual(unknown_names("It was paid by Mark yesterday.", set()), ["Mark"])
        self.assertEqual(unknown_names("Most sales were on Amazon.", set()), ["Amazon"])
        # ...and a word when the report uses it as one
        self.assertEqual(unknown_names("Mark it done; we hit the mark.", set()), [])

    def test_a_company_of_ordinary_words_fails(self) -> None:
        self.assertEqual(unknown_names("A deal with Blue Sky Ltd is close.", set()),
                         ["Blue Sky Ltd"])

    def test_a_recorded_name_passes(self) -> None:
        self.assertEqual(unknown_names("Etsy sent two sales.", {"Etsy"}), [])


OK = (HEALTHY, "ok, last success 1 min ago")
NEVER = (UNHEALTHY, "NEVER SUCCEEDED in 3 attempts")
SILENT = (UNCLEAR, "ok; succeeded 2 times in a row producing NOTHING")
POSTING = {"posting.blog": OK, "posting.devto": OK, "posting.instagram": OK}
CONTRACTS = {"contracts.orders": OK, "contracts.delivery": OK, "contracts.finder": NEVER}


class HealthClaimTests(unittest.TestCase):
    """A report may not call a worker failing that last ran ok, nor call all well while
    one is failing - checked against the runs, conservatively."""

    def test_the_live_posting_claim_is_caught(self) -> None:
        live = "Posting.blog and posting.devto have not succeeded in any attempts."
        (p,) = health_claims([live], POSTING)
        self.assertIn("'have not succeeded'", p)
        self.assertIn("posting.blog: ok, last success", p)
        self.assertTrue(health_claims(["Posting.instagram has also not succeeded."], POSTING))
        self.assertTrue(health_claims(["The workers have not succeeded."], POSTING))

    def test_a_true_failure_claim_passes(self) -> None:
        for text in ("1 worker failed: contracts.finder has never succeeded.",
                     "The finder desk is failing; contracts.orders is fine.",
                     "The workers have not all succeeded.",
                     "Some workers are running normally."):     # no "all": not a claim
            self.assertEqual(health_claims([text], CONTRACTS), [], text)

    def test_a_false_all_healthy_claim_is_caught(self) -> None:
        for text in ("All workers are healthy.", "All three desks report no issues.",
                     "contracts.finder is healthy.",
                     "Delivery and finder desks are operating normally."):
            self.assertTrue(health_claims([text], CONTRACTS), text)
        self.assertEqual(health_claims(["All workers are healthy."], POSTING), [])

    def test_negations_and_counted_things_claim_nothing(self) -> None:
        for text in ("posting.blog has 0 failures and has not failed.",
                     "Two drafts failed the content check at posting.blog.",
                     "At posting.blog 2 drafts failed.",
                     "posting.devto reports 0 cross-posts failed to send.",
                     "The workers report no failing products.",
                     "Blog post generation failed twice."):
            self.assertEqual(health_claims([text], POSTING, {"cross-posts failed", "drafts"}),
                             [], text)

    def test_an_unclear_worker_is_not_judged(self) -> None:
        self.assertEqual(health_claims(["posting.blog has stalled."],
                                       {"posting.blog": SILENT}), [])


class ReportTests(unittest.TestCase):
    def test_a_report_with_an_unbacked_figure_has_a_problem_in_words(self) -> None:
        problems = check_report(["Revenue reached $470 this month."], [], [REVENUE], set())
        self.assertEqual(len(problems), 1)
        self.assertIn("$470", problems[0])

    def test_a_grounded_report_has_none(self) -> None:
        self.assertEqual(check_report(["Revenue is $12 so far this month."],
                                      [Figure(1200, "usd_cents", "revenue")], [REVENUE],
                                      set()), [])


def _contracts_figures() -> list:
    now = lambda v, m: Figure(v, "count", m, window="now")      # noqa: E731
    return [now(1, "orders listed"), now(1, "orders with status quote_requested"),
            now(0, "orders with status paid"), Figure(0, "count", "new paid orders",
                                                      window="this_run"),
            now(1, "quote requests waiting for the owner"),
            now(0, "client emails pending the owner's approval")]


class CountedEventTests(unittest.TestCase):
    """Report #871 told Moss of a paid order and a client email that never existed."""

    REPORT_871 = ("Contracts Division Report: 1 new paid order, one client email pending, "
                  "1 quote request waiting for the owner.")

    def test_report_871_wording_is_rejected_against_its_own_figures(self) -> None:
        problems = check_report([self.REPORT_871], [], _contracts_figures(), set(),
                                {"contracts", "division", "report"})
        joined = " | ".join(problems)
        self.assertIn("1 new paid order", joined)
        self.assertIn("one client email pending", joined)
        self.assertNotIn("quote request waiting", joined)    # that one is true

    def test_the_true_quote_request_alone_passes(self) -> None:
        self.assertEqual(topic_claims("There is one quote request waiting for the owner.",
                                      _contracts_figures()), [])

    def test_no_new_paid_orders_passes_when_zero_and_fails_when_one(self) -> None:
        self.assertEqual(topic_claims("Contracts Division Report: No New Paid Orders",
                                      _contracts_figures()), [])
        one = [f if f.measures != "new paid orders" else Figure(1, "count", "new paid orders",
                                                                 window="this_run")
               for f in _contracts_figures()]
        self.assertTrue(topic_claims("Contracts Division Report: No New Paid Orders", one))

    def test_number_words_and_digits_both_count(self) -> None:
        figs = _contracts_figures()
        self.assertTrue(topic_claims("Two quote requests are waiting.", figs))
        self.assertTrue(topic_claims("2 quote requests are waiting.", figs))
        self.assertTrue(topic_claims("A new paid order arrived.", figs))

    def test_a_dollar_order_with_no_recorded_order_is_rejected(self) -> None:
        zero = [Figure(0, "count", "orders listed", window="now")]
        self.assertTrue(topic_claims("A $200 order came in.", zero))
        self.assertTrue(check_report(["A $200 order came in."], [], zero, set()))

    def test_a_claimed_count_with_no_figure_at_all_fails_closed(self) -> None:
        other = [Figure(3, "count", "drafts blocked")]
        self.assertTrue(topic_claims("2 paid orders were delivered.", other))
        self.assertEqual(topic_claims("No paid orders this run.", other), [])

    def test_a_count_of_a_modified_noun_is_backed_by_the_head_noun(self) -> None:
        # leader.contracts, 11 of 76 runs: "1 client order is listed" was refused with
        # "states '1', which no worker recorded" over its own "orders listed = 1" - the
        # number was read as counting "client", the modifier, not the order
        live = _contracts_figures() + [Figure(0, "count", "orders with status declined",
                                              window="now"),
                                       Figure(1, "count", "quote acknowledgements sent",
                                              window="now")]
        for text in ("1 client order is listed, requiring owner approval.",
                     "The order desk has 1 open order and 1 quote acknowledgement sent.",
                     "There is 1 customer order with status quote_requested."):
            self.assertEqual(check_report([text], [], live, set(), set()), [], text)
        for text in ("3 client orders are listed.",          # the wrong number still fails
                     "1 client is waiting.",                 # nothing counts clients
                     "1 client email reply arrived."):       # nor emails or replies
            self.assertIn("which no worker recorded",
                          " ".join(check_report([text], [], live, set(), set())), text)

    def test_a_declined_order_is_held_to_the_declined_count(self) -> None:
        # leader.contracts on 2026-10-04, refused over its own "orders with status
        # declined = 1": "one declined order awaiting a manual" was held only to the order
        # counts that say waiting (all 0), since "declined" was not a qualifier
        now = lambda v, m: Figure(v, "count", m, window="now")      # noqa: E731
        live = [now(1, "orders listed"), now(1, "orders with status declined"),
                now(0, "orders with status paid"), now(0, "orders with status refunded"),
                now(0, "new paid orders"), now(0, "find orders waiting for research"),
                now(0, "flagged orders awaiting the owner"),
                now(0, "paid orders waiting for acknowledgement")]
        for text in ("There is one declined order awaiting a manual refund in Stripe.",
                     "There is 1 declined paid order awaiting a refund.",
                     "1 order with status declined."):
            self.assertEqual(check_report([text], [], live, set(), set()), [], text)
        for text in ("Two declined orders await a refund.", "1 new paid order arrived.",
                     "1 paid order is waiting."):
            self.assertTrue(check_report([text], [], live, set(), set()), text)

    def test_plain_prose_and_outreach_email_are_not_claims(self) -> None:
        figs = _contracts_figures()
        for text in ("We should send an email to Kim.", "Drafted 3 emails today.",
                     "1 of 3 orders is paid.", "Write a quote for the next build."):
            self.assertEqual(topic_claims(text, figs), [], text)


if __name__ == "__main__":
    unittest.main()
