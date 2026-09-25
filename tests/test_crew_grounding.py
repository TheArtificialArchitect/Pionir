"""Grounding: a report may state only what a worker recorded - value AND unit.

Each test fails if the check is loosened: "$12" backed by "12 replies", a structured
figure backed by the same number in another unit, a name nothing recorded, or a figure a
model wrote vouching for itself.
"""

import unittest

from pionir.crew.figures import Figure
from pionir.crew.grounding import (
    backs,
    check_report,
    claims_in,
    unbacked_claims,
    unbacked_figures,
    unknown_names,
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


class ReportTests(unittest.TestCase):
    def test_a_report_with_an_unbacked_figure_has_a_problem_in_words(self) -> None:
        problems = check_report(["Revenue reached $470 this month."], [], [REVENUE], set())
        self.assertEqual(len(problems), 1)
        self.assertIn("$470", problems[0])

    def test_a_grounded_report_has_none(self) -> None:
        self.assertEqual(check_report(["Revenue is $12 so far this month."],
                                      [Figure(1200, "usd_cents", "revenue")], [REVENUE],
                                      set()), [])


if __name__ == "__main__":
    unittest.main()
