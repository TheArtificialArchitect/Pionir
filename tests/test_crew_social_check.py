"""``contentcheck.check_social``: the crew's fail-closed rules for an Instagram post.

Pionir's own post check (``pionir.social.post.check_post``) runs first, so every one of its
refusals must block here too; then the crew's own rules - no links at all, personal data,
names (with the dictionary; the headline is Title Case), business figures, internal system
names, hashtags that are words - run on the headline, each point, the caption and the tags.
Each test fails if the rule it names is reverted. A clean post must pass, or the check is a
wall rather than a gate. The blog's ``check`` must behave exactly as before.
"""

import unittest
from typing import ClassVar
from unittest import mock

from pionir.crew import contentcheck as cc
from pionir.social.card import card_sha

HEADLINE = "Check an email address before you hit send"
POINTS = ["A typo in the domain bounces, and bounces hurt everything you send after.",
          "Syntax, domain and mail server: three checks, one call.",
          "Catch throwaway addresses at sign-up, not after."]
CAPTION = ("Every bounced message chips away at how inboxes treat your mail. Checking the "
           "address first is cheap, fast, and keeps your sender reputation clean. The Email "
           "Verify API does it in one request. The full guide is at the link in bio.")
SHA = card_sha(HEADLINE, POINTS)


def clean(**over) -> dict:
    post = {"draft_id": "2026-09-25-ig-check-an-email-address-before-you-hit-send",
            "headline": HEADLINE, "points": list(POINTS), "caption": CAPTION,
            "hashtags": ["emaildeliverability", "webdev"], "card_sha": SHA}
    post.update(over)
    return post


def caption_plus(extra: str) -> dict:
    return clean(caption=CAPTION + " " + extra)


class CleanPostTests(unittest.TestCase):
    def test_a_clean_post_passes(self) -> None:
        self.assertEqual(cc.check_social(clean()), [])

    def test_a_title_case_headline_passes_but_a_name_in_it_does_not(self) -> None:
        # headlines are Title Case: capitals there carry no signal, names still block
        headline = "Check An Email Address Before You Hit Send"
        post = clean(headline=headline, card_sha=card_sha(headline, POINTS))
        self.assertEqual(cc.check_social(post), [])
        # "Signups" is in no dictionary: mid-headline it is vouched for only because the
        # headline is read as Title Case and the caption uses the word in lower case
        headline = "Keep Throwaway Signups Out Of Your List"
        post = clean(headline=headline, card_sha=card_sha(headline, POINTS),
                     caption=CAPTION + " It keeps fake signups out.")
        self.assertEqual(cc.check_social(post), [])
        headline = "Ask Marko About Email Addresses"
        post = clean(headline=headline, card_sha=card_sha(headline, POINTS))
        self.assertTrue(any("'Marko'" in r for r in cc.check_social(post)))


class PionirRefusalTests(unittest.TestCase):
    """Every refusal of Pionir's post check blocks the crew's check too, named as such."""

    CASES: ClassVar[dict] = {
        "an unknown field": clean(extra="x"),
        "a bad draft id": clean(draft_id="Not An Id"),
        "a headline too long": clean(headline="word " * 20),
        "a headline too short": clean(headline="Hi"),
        "a two-line headline": clean(headline="Check an email\naddress first"),
        "no points": clean(points=[]),
        "four points": clean(points=POINTS + ["One more point that is long enough."]),
        "a point too long": clean(points=["a point that goes on " * 7]),
        "a caption too short": clean(caption="Too short. Link in bio."),
        "a caption too long": clean(caption="word " * 400),
        "an @ in the caption": caption_plus("Say hi to the team at our page."
                                            .replace("at our", "@ our")),
        "a hashtag in the caption": caption_plus("#webdev"),
        "a URL in the caption": caption_plus("See https://api.dokaz.net for more."),
        "a domain in the caption": caption_plus("See dokaz.net for more."),
        "an emoji": caption_plus("Try it \U0001f680"),
        "a hashtag with its #": clean(hashtags=["#webdev"]),
        "an upper-case hashtag": clean(hashtags=["WebDev"]),
        "too many hashtags": clean(hashtags=[f"email{n}" for n in "abcdefghijk"]),
        "a repeated hashtag": clean(hashtags=["webdev", "webdev"]),
        "a malformed card_sha": clean(card_sha="abc"),
        "a card_sha for another card": clean(card_sha=card_sha(HEADLINE + "!", POINTS)),
        "a word too wide for the card": clean(points=["w" * 60]),
        "not text": clean(caption=42),
    }

    def test_each_refusal_blocks(self) -> None:
        for what, post in self.CASES.items():
            with self.subTest(what):
                reasons = cc.check_social(post)
                self.assertTrue(any(r.startswith("Pionir's post check refuses it:")
                                    for r in reasons), (what, reasons))

    def test_not_an_object_blocks(self) -> None:
        self.assertEqual(len(cc.check_social(["not", "a", "post"])), 1)
        self.assertNotEqual(cc.check_social(None), [])

    def test_a_post_check_that_cannot_run_blocks(self) -> None:
        with mock.patch.object(cc, "check_post", side_effect=RuntimeError("boom")):
            reasons = cc.check_social(clean())
        self.assertTrue(any("could not run" in r and "boom" in r for r in reasons), reasons)


class CrewRuleTests(unittest.TestCase):
    """What only the crew's rules catch, named in the reason."""

    def assertBlocked(self, post: dict, *needles: str) -> list:
        reasons = cc.check_social(post)
        for needle in needles:
            self.assertTrue(any(needle in r for r in reasons), (needle, reasons))
        return reasons

    def test_a_persons_name_blocks(self) -> None:
        self.assertBlocked(caption_plus("A friend, Jane Doe, swears by it."), "'Jane Doe'")

    def test_a_name_in_a_point_blocks(self) -> None:
        self.assertBlocked(clean(points=[*POINTS[:2], "Marko checks every address first."]),
                           "'Marko'")

    def test_a_company_blocks(self) -> None:
        self.assertBlocked(caption_plus("Teams at Acme Corp check every address."),
                           "the company 'Acme Corp'")

    def test_an_invented_street_address_blocks(self) -> None:
        self.assertBlocked(caption_plus("Mail it to 12 Harbor Road if you like."),
                           "street address")

    def test_an_email_address_blocks(self) -> None:
        # the @ is Pionir's refusal; the address itself is the crew's
        self.assertBlocked(caption_plus("Write to jane.doe@mailbox.com for help."),
                           "email address ('jane.doe@mailbox.com')")
        self.assertBlocked(caption_plus("Write to jane [at] mailbox for help."),
                           "spelled-out email address")

    def test_a_business_figure_blocks(self) -> None:
        self.assertBlocked(caption_plus("We made 5000 dollars from it last month."),
                           "first-person business claim", "a social post must not state")
        self.assertBlocked(caption_plus("Over 300 customers rely on it."),
                           "a count of customers or users")
        self.assertBlocked(caption_plus("It costs $5 a month."), "a money amount")

    def test_an_internal_name_blocks(self) -> None:
        self.assertBlocked(caption_plus("Moss wrote this one."),
                           "names the internal system 'Moss'")
        self.assertBlocked(clean(headline="How Pionir checks an address"),
                           "headline names the internal system 'Pionir'")
        self.assertBlocked(clean(hashtags=["pionir"]), "hashtags names the internal system")

    def test_a_domain_pionir_does_not_list_still_blocks(self) -> None:
        # Pionir's check knows a short list of top-level domains; the crew's knows more
        post = caption_plus("The guide lives on dokaz.rs as well.")
        self.assertFalse(any(r.startswith("Pionir's") for r in cc.check_social(post)))
        self.assertBlocked(post, "has a link, URL or domain ('dokaz.rs')")

    def test_a_hashtag_that_is_a_name_blocks(self) -> None:
        self.assertBlocked(clean(hashtags=["janedoe"]), "hashtag 'janedoe'")
        self.assertBlocked(clean(hashtags=["seattle"]), "hashtag 'seattle'")
        self.assertEqual(cc.check_social(clean(hashtags=["smallbusiness", "qrcode", "saas",
                                                         "email_tips"])), [])

    def test_the_card_must_be_pinned(self) -> None:
        post = clean()
        del post["card_sha"]
        self.assertBlocked(post, "card_sha is missing")

    def test_text_that_does_not_fit_the_card_is_named_even_behind_another_refusal(self) -> None:
        # Pionir's check stops at the first refusal (the hashtag); the crew still names the fit
        reasons = self.assertBlocked(clean(points=["w" * 60], hashtags=["#x"]),
                                     "card: the word")
        self.assertTrue(any("hashtags" in r for r in reasons))

    def test_a_rule_that_cannot_run_blocks(self) -> None:
        with mock.patch.object(cc, "_personal", side_effect=RuntimeError("broken rule")):
            reasons = cc.check_social(clean())
        self.assertEqual(reasons, ["the content check could not run: RuntimeError: broken rule"])


class BlogCheckUnchangedTests(unittest.TestCase):
    def test_the_blog_reasons_still_speak_of_the_blog(self) -> None:
        from test_crew_contentcheck import clean as blog_clean
        from test_crew_contentcheck import with_body
        self.assertEqual(cc.check(blog_clean()), [])
        reasons = cc.check(with_body("We made 5000 dollars from it last month."))
        self.assertTrue(any("the blog must not state business numbers" in r for r in reasons))
        # the blog's title is still read as Title Case; a social headline field is not
        self.assertEqual(cc.unknown_names([("title", "A Practical Guide To Checks"),
                                           ("body", "a practical guide to checks")]), [])


if __name__ == "__main__":
    unittest.main()
