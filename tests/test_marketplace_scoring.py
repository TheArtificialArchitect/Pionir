"""Marketplaces: reading the stores' public data and scoring gaps (terms.py, providers.py).

Each test fails if its rule is reverted: a niche found from one listing, a personal-data niche
allowed through, an unrated niche scored as poor, a page robots.txt disallows read, a store
figure that was not shown recorded as a zero, or the same niche counted twice.
"""
from __future__ import annotations

import unittest

from marketplace_support import FakeHttp

from pionir.crew.marketplaces import providers, terms


def listing(key, title, users, rating=None, ratings=None, text="", market="apify",
            category="SEO_TOOLS"):
    return {"market": market, "key": key, "title": title, "text": text, "category": category,
            "users": users, "rating": rating, "ratings": ratings, "url": f"https://x/{key}"}


class NicheTests(unittest.TestCase):
    def test_a_niche_needs_two_listings_and_some_demand(self) -> None:
        found = terms.niches([listing("a", "Sitemap Checker", 500, 3.0, 4),
                              listing("b", "Broken Sitemap Finder", 300, 2.5, 3),
                              listing("c", "Weather Now", 9000, 4.9, 100)])
        names = [n["niche"] for n in found]
        self.assertIn("sitemap", names)
        self.assertNotIn("weather", names)          # one listing is not a niche

    def test_poorly_rated_beats_well_rated_at_equal_demand(self) -> None:
        poor = terms.niches([listing("a", "Pdf Merge", 1000, 2.0, 50),
                             listing("b", "Pdf Merge Tool", 1000, 2.2, 50)])[0]
        good = terms.niches([listing("c", "Pdf Merge", 1000, 4.9, 50),
                             listing("d", "Pdf Merge Tool", 1000, 4.8, 50)])[0]
        self.assertGreater(poor["score"], good["score"])

    def test_an_unrated_niche_is_not_scored_as_a_poorly_rated_one(self) -> None:
        unrated = terms.niches([listing("a", "Csv Clean", 800), listing("b", "Csv Clean", 800)])
        poor = terms.niches([listing("c", "Csv Clean", 800, 1.5, 20),
                             listing("d", "Csv Clean", 800, 1.5, 20)])
        self.assertEqual(unrated[0]["quality"], terms.UNKNOWN_QUALITY)
        self.assertLess(unrated[0]["score"], poor[0]["score"])

    def test_personal_data_niches_are_marked(self) -> None:
        found = terms.niches([listing("a", "LinkedIn Profile Scraper", 9000),
                              listing("b", "Profile Scraper Pro", 8000)])
        self.assertTrue(all(n["personal"] for n in found))
        found = terms.niches([listing("a", "Website Audit", 900, text="finds emails and phone"),
                              listing("b", "Website Audit Tool", 800)])
        self.assertTrue(found[0]["personal"])

    def test_the_same_listings_are_one_niche(self) -> None:
        found = terms.niches([listing("a", "Image Compress", 900),
                              listing("b", "Image Compress", 800)])
        self.assertEqual(len(found), 1)

    def test_preferred_words_only_weigh_what_the_data_found(self) -> None:
        found = terms.niches([listing("a", "Pdf Split", 100), listing("b", "Pdf Split", 100)])
        self.assertTrue(found[0]["preferred"])
        self.assertEqual(terms.niches([listing("a", "Pdf", 100)]), [])


class RobotsTests(unittest.TestCase):
    TXT = ("User-agent: Googlebot\nDisallow: /\n\nUser-agent: *\nDisallow: /search\n"
           "Disallow: /detail/*/reviews\nAllow: /detail/ok/reviews\n")

    def test_the_star_group_and_wildcards(self) -> None:
        rules = providers.robots_rules(self.TXT)
        self.assertFalse(providers.robots_allows(rules, "https://h/search/pdf"))
        self.assertFalse(providers.robots_allows(rules, "https://h/detail/x/abc/reviews"))
        self.assertTrue(providers.robots_allows(rules, "https://h/detail/ok/reviews"))
        self.assertTrue(providers.robots_allows(rules, "https://h/detail/x/abc"))
        self.assertTrue(providers.robots_allows(rules, "https://h/"))

    def test_a_disallowed_page_is_never_fetched(self) -> None:
        http = FakeHttp({"https://h.test/robots.txt": (200, self.TXT)})
        reader = providers.Reader(http, pause=0)
        with self.assertRaises(providers.ProviderError):
            reader.get_text("https://h.test/search/pdf")
        self.assertEqual(http.calls, ["https://h.test/robots.txt"])

    def test_no_robots_answer_means_nothing_is_read(self) -> None:
        http = FakeHttp({"https://h.test/robots.txt": (503, "")})
        with self.assertRaises(providers.ProviderError):
            providers.Reader(http, pause=0).get_text("https://h.test/page")
        self.assertEqual(http.calls, ["https://h.test/robots.txt"])

    def test_it_pauses_between_reads_of_one_host(self) -> None:
        slept = []
        http = FakeHttp({"https://h.test/robots.txt": (404, ""),
                         "https://h.test/a": (200, "a"), "https://h.test/b": (200, "b")})
        reader = providers.Reader(http, pause=2.5, sleep=slept.append)
        reader.get_text("https://h.test/a")
        reader.get_text("https://h.test/b")
        self.assertEqual(slept, [2.5, 2.5])     # robots -> a, a -> b


class ParsingTests(unittest.TestCase):
    def test_an_apify_store_item(self) -> None:
        got = providers.apify_item({
            "username": "dev", "name": "pdf-to-text", "title": "PDF to Text",
            "description": "Converts PDFs.", "categories": ["DEVELOPER_TOOLS"],
            "stats": {"totalUsers30Days": 412}, "actorReviewRating": 3.4,
            "actorReviewCount": 7, "currentPricingInfo": {"pricingModel": "PAY_PER_EVENT"}})
        self.assertEqual(got["key"], "dev/pdf-to-text")
        self.assertEqual(got["users"], 412)
        self.assertEqual(got["url"], "https://apify.com/dev/pdf-to-text")
        unknown = providers.apify_item({"username": "u", "name": "n"})
        self.assertIsNone(unknown["users"])        # not shown is UNKNOWN, never zero
        self.assertIsNone(unknown["rating"])

    def test_the_apify_store_is_read_from_its_public_api(self) -> None:
        url = f"{providers.APIFY_STORE}?limit=500&offset=0&sortBy=popularity"
        http = FakeHttp({url: (200, {"data": {"items": [
            {"username": "u", "name": "a", "stats": {"totalUsers30Days": 1}}]}})})
        got = providers.read_apify(providers.Reader(http, pause=0), pages=3)
        self.assertEqual([x["key"] for x in got], ["u/a"])
        self.assertEqual(http.calls, [url])        # a short page ends the read

    def test_a_chrome_category_page_and_an_item_page(self) -> None:
        page = ('<a href="./detail/pdf-tool/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"><div>PDF Tool</div>'
                '<span>3.2</span><span>Average rating 3.2 out of 5 stars.</span></a>'
                '<a href="./category/extensions/productivity/tools">x</a>')
        items = providers.cws_list(page, "extensions/productivity/tools")
        self.assertEqual(items[0]["title"], "PDF Tool")
        self.assertEqual(items[0]["rating"], 3.2)
        self.assertIsNone(items[0]["users"])
        self.assertEqual(providers.cws_categories(page), ["extensions/productivity/tools"])
        detail = providers.cws_detail(
            '<meta property="og:title" content="PDF Tool - Chrome Web Store">'
            '<div>4.1 out of 5</div><p>3.6K ratings</p><div>21,000 users</div>')
        self.assertEqual((detail["users"], detail["ratings"], detail["rating"]),
                         (21000, 3600, 4.1))
        self.assertEqual(detail["title"], "PDF Tool")

    def test_a_shopify_category_page(self) -> None:
        page = ('<div data-app-card-handle-value="alt-fix" data-app-card-name-value="Alt Fix">'
                '<span>4.1 out of 5 stars</span> (12) <span>12 total reviews</span> • '
                '<span>Free plan available</span></div>'
                '<a href="https://apps.shopify.com/categories/store-design">x</a>')
        got = providers.shopify_list(page, "store-design")
        self.assertEqual((got[0]["key"], got[0]["users"], got[0]["rating"]),
                         ("alt-fix", 12, 4.1))
        self.assertEqual(providers.shopify_categories(page), ["store-design"])


if __name__ == "__main__":
    unittest.main()
