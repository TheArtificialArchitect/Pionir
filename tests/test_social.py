"""The shared social vocabulary: the card renderer and the post check."""

from __future__ import annotations

import hashlib
import io
import unittest

from pionir.social import card
from pionir.social.card import CardTooLong, card_sha, layout, render_card
from pionir.social.post import check_post, full_caption

GOOD = {
    "draft_id": "2026-09-25-verify-email",
    "headline": "Check an email address before you hit send",
    "points": ["A typo in the domain bounces, and bounces hurt everything you send after.",
               "Syntax, domain and mail server: three checks, one call.",
               "Catch throwaway addresses at sign-up, not after."],
    "caption": "Every bounced message chips away at how inboxes treat your mail. Checking the "
               "address first is cheap, fast, and keeps your sender reputation clean.",
    "hashtags": ["emaildeliverability", "webdev"],
}


def post(**over):
    return {**GOOD, **over}


class CardTests(unittest.TestCase):
    def test_same_text_same_bytes(self) -> None:
        a = render_card(GOOD["headline"], GOOD["points"])
        b = render_card(GOOD["headline"], GOOD["points"])
        self.assertEqual(hashlib.sha256(a).hexdigest(), hashlib.sha256(b).hexdigest())
        self.assertNotEqual(card_sha(GOOD["headline"], GOOD["points"]),
                            card_sha(GOOD["headline"] + "!", GOOD["points"]))

    def test_it_is_a_plain_4_5_jpeg_instagram_accepts(self) -> None:
        from PIL import Image
        data = render_card(GOOD["headline"], GOOD["points"])
        self.assertTrue(data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9"))
        img = Image.open(io.BytesIO(data))
        self.assertEqual((img.format, img.mode, img.size), ("JPEG", "RGB", (1080, 1350)))
        self.assertNotIn("exif", img.info)
        self.assertLess(len(data), 1_000_000)

    def test_text_that_does_not_fit_is_refused_never_clipped(self) -> None:
        with self.assertRaises(CardTooLong):
            layout("word " * 60, GOOD["points"])
        with self.assertRaises(CardTooLong):
            layout(GOOD["headline"], ["a long point that keeps going " * 5])
        with self.assertRaises(CardTooLong):
            layout(GOOD["headline"], ["W" * 60])            # one unbreakable word
        layout(GOOD["headline"], GOOD["points"])            # and the good one fits

    def test_the_font_is_bundled(self) -> None:
        self.assertTrue(card.FONT_PATH.exists())
        self.assertTrue((card.FONT_PATH.parent / "OFL.txt").exists())


class PostCheckTests(unittest.TestCase):
    def test_a_good_post_passes_and_is_normalised(self) -> None:
        out = check_post(post(headline="  " + GOOD["headline"] + " "))
        self.assertEqual(out["headline"], GOOD["headline"])

    def test_refusals(self) -> None:
        cases = {
            "link": post(caption=GOOD["caption"] + " See https://api.dokaz.net/blog."),
            "domain": post(caption=GOOD["caption"] + " More at dokaz.net today."),
            "www": post(caption=GOOD["caption"] + " Visit www.example today."),
            "mention": post(caption=GOOD["caption"] + " Thanks to @someone."),
            "hash in caption": post(caption=GOOD["caption"] + " #webdev"),
            "email": post(points=["Write to someone at jane@mailbox.org for help."]),
            "phone": post(caption=GOOD["caption"] + " Call 2065550123 now."),
            "angle bracket": post(headline="Use <b> tags for bold text here"),
            "emoji": post(headline="Check an email address before \U0001F680"),
            "multi-line headline": post(headline="Check an email\naddress first"),
            "too many points": post(points=GOOD["points"] + ["One more point that is long enough."]),
            "no points": post(points=[]),
            "hashtag with #": post(hashtags=["#webdev"]),
            "hashtag caps": post(hashtags=["WebDev"]),
            "repeated hashtag": post(hashtags=["webdev", "webdev"]),
            "too many hashtags": post(hashtags=[f"tag{i}" for i in range(11)]),
            "unknown field": post(image_url="https://x"),
            "bad draft id": post(draft_id="Bad ID"),
            "does not fit": post(points=[("WWWWWWW " * 14).strip()[:110]]),  # wide: 4 lines
        }
        for name, payload in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                check_post(payload)

    def test_unlike_the_blog_no_address_at_all_not_even_a_reserved_one(self) -> None:
        # a post mentions nobody and shows no address; the @ itself is refused
        with self.assertRaises(ValueError):
            check_post(post(caption=GOOD["caption"] + " Try it with user@example.com first."))

    def test_card_sha_pins_the_image(self) -> None:
        sha = card_sha(GOOD["headline"], GOOD["points"])
        self.assertEqual(check_post(post(card_sha=sha))["card_sha"], sha)
        with self.assertRaises(ValueError):
            check_post(post(card_sha="0" * 64))
        with self.assertRaises(ValueError):
            check_post(post(card_sha=sha, headline=GOOD["headline"] + " now"))
        with self.assertRaises(ValueError):
            check_post(post(card_sha="not-a-sha"))
        self.assertNotIn("card_sha", check_post(post()))

    def test_full_caption_appends_hashtags(self) -> None:
        self.assertEqual(full_caption("Hello there.", ["a1", "b_2"]), "Hello there.\n\n#a1 #b_2")
        self.assertEqual(full_caption("Hello there.", []), "Hello there.")


if __name__ == "__main__":
    unittest.main()
