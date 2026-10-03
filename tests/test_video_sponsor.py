"""The sponsor media-kit page: only measured numbers, fail closed to "too new", no invented price,
logo, rating or testimonial, every string escaped. Each rule has a test that fails when the rule
is removed."""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from datetime import date
from pathlib import Path

from pionir.video.disclosure import DISCLOSURE
from pionir.video.pages import build_site
from pionir.video.sponsor import (
    DEFAULT_PACKAGES,
    MAX_AGE_DAYS,
    NOT_ENOUGH,
    load_analytics,
    load_config,
    sponsor_page,
)

TODAY = date(2026, 12, 10)
BASE = "https://example.test/video"
LD = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
EVIL = '<script>alert(1)</script>"><img src=x onerror=alert(2)>&\'</script>'


def manifest(n: int, title: str = "A video") -> dict:
    return {"id": f"vid-{n:02d}", "title": title, "created_at": f"2026-11-{n:02d}T10:00:00Z"}


GROUPS = {"harbor-stories": ("Harbor stories", [manifest(1, "The pier"), manifest(2, "The ships")])}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def write(self, name: str, document) -> None:
        text = document if isinstance(document, str) else json.dumps(document)
        (self.dir / name).write_text(text, encoding="utf-8")

    def analytics(self, *, published: int = 1, **fields):
        base = {"as_of": "2026-12-01", "source": "YouTube Studio, last 28 days",
                "views_28d": 1234, "subscribers": 56}
        base.update(fields)
        self.write("analytics.json", base)
        return load_analytics(self.dir, today=TODAY, published=published)

    def page(self, **config) -> str:
        if config:
            self.write("sponsor.json", config)
        analytics, _why = load_analytics(self.dir, today=TODAY, published=1)
        return sponsor_page(load_config(self.dir), analytics, GROUPS, BASE)


class AnalyticsFailClosedTests(Base):
    def test_real_recent_figures_are_accepted_and_formatted_unrounded(self) -> None:
        got, why = self.analytics(views_28d=1234, watch_hours=12.34, videos_measured=3)
        self.assertEqual(why, "")
        self.assertEqual(dict(got.figures), {"Views, last 28 days": "1,234", "Subscribers": "56",
                                             "Watch hours, last 28 days": "12.3",
                                             "Videos measured": "3"})
        self.assertEqual(got.as_of, "2026-12-01")

    def test_no_published_video_means_no_numbers_even_with_a_file(self) -> None:
        got, why = self.analytics(published=0)
        self.assertIsNone(got)
        self.assertIn("published", why)

    def test_a_missing_or_unreadable_file_shows_nothing(self) -> None:
        got, why = load_analytics(self.dir, today=TODAY, published=1)
        self.assertIsNone(got)
        self.assertIn("no analytics.json", why)
        self.write("analytics.json", "{broken")
        self.assertIsNone(load_analytics(self.dir, today=TODAY, published=1)[0])
        self.write("analytics.json", [1, 2])
        self.assertIsNone(load_analytics(self.dir, today=TODAY, published=1)[0])

    def test_figures_that_do_not_say_where_they_come_from_are_refused(self) -> None:
        got, why = self.analytics(source="")
        self.assertIsNone(got)
        self.assertIn("source", why)
        self.write("analytics.json", {"as_of": "2026-12-01", "views_28d": 5})
        self.assertIsNone(load_analytics(self.dir, today=TODAY, published=1)[0])

    def test_stale_future_and_undated_figures_are_refused(self) -> None:
        self.assertIsNone(self.analytics(as_of="2026-12-11")[0])             # the future
        stale = date.fromordinal(TODAY.toordinal() - MAX_AGE_DAYS - 1).isoformat()
        got, why = self.analytics(as_of=stale)
        self.assertIsNone(got)
        self.assertIn(str(MAX_AGE_DAYS), why)
        on_the_limit = date.fromordinal(TODAY.toordinal() - MAX_AGE_DAYS).isoformat()
        self.assertIsNotNone(self.analytics(as_of=on_the_limit)[0])
        self.assertIsNone(self.analytics(as_of="last week")[0])

    def test_a_bad_figure_poisons_the_whole_file(self) -> None:
        for bad in (-1, "1,000", True, None, float("inf"), float("nan"), [5]):
            got, _why = self.analytics(views_28d=bad)
            self.assertIsNone(got, repr(bad))

    def test_a_file_with_no_figures_shows_nothing(self) -> None:
        self.write("analytics.json", {"as_of": "2026-12-01", "source": "YouTube Studio"})
        got, why = load_analytics(self.dir, today=TODAY, published=1)
        self.assertIsNone(got)
        self.assertIn("no figures", why)

    def test_a_figure_that_is_not_one_of_the_known_ones_is_ignored(self) -> None:
        got, _ = self.analytics(estimated_revenue=9999, projected_views=1e9)
        self.assertNotIn("9,999", sponsor_page(load_config(self.dir), got, GROUPS, BASE))
        self.assertNotIn("projected", " ".join(label for label, _n in got.figures).lower())


class AudienceSectionTests(Base):
    def test_with_no_data_the_page_says_too_new_and_contains_no_figure(self) -> None:
        page = self.page()
        self.assertIn(NOT_ENOUGH, page)
        self.assertNotRegex(page, r"(?i)\bviews\b|subscribers|watch hours")
        self.assertNotIn("<table>", page)

    def test_with_real_data_the_page_shows_the_figures_date_and_source(self) -> None:
        self.analytics(views_28d=1234, subscribers=56)
        page = self.page()
        self.assertNotIn(NOT_ENOUGH, page)
        self.assertIn("1,234", page)
        self.assertIn("As of 2026-12-01", page)
        self.assertIn("Source: YouTube Studio, last 28 days", page)

    def test_stale_data_falls_back_to_too_new_rather_than_showing_old_numbers(self) -> None:
        self.analytics(as_of="2026-01-01", views_28d=99999)
        page = self.page()
        self.assertIn(NOT_ENOUGH, page)
        self.assertNotIn("99,999", page)

    def test_a_channel_link_must_be_https(self) -> None:
        self.analytics(channel_url="javascript:alert(1)")
        self.assertNotIn("javascript:", self.page())
        self.analytics(channel_url="https://www.youtube.com/@example")
        self.assertIn('href="https://www.youtube.com/@example"', self.page())


class PackagesAndPriceTests(Base):
    def test_by_default_there_is_no_price_anywhere(self) -> None:
        page = self.page()
        self.assertNotRegex(page, r"[$€£]\s?\d")
        self.assertNotIn("Price:", page)
        self.assertIn("agreed with each sponsor by email", page)
        for p in DEFAULT_PACKAGES:
            self.assertNotIn("price", p)
            self.assertIn(p["name"], page)

    def test_a_price_appears_only_when_it_is_configured(self) -> None:
        page = self.page(packages=[{"name": "One mention", "description": "A credit.",
                                    "price": "$250"}])
        self.assertIn("<strong>Price:</strong> $250", page)
        self.assertNotIn("agreed with each sponsor by email", page)

    def test_a_package_without_a_price_does_not_borrow_one(self) -> None:
        page = self.page(packages=[{"name": "A", "description": "x"},
                                   {"name": "B", "description": "y", "price": "$9"}])
        self.assertEqual(page.count("Price:"), 1)

    def test_a_malformed_package_is_skipped_and_named(self) -> None:
        self.write("sponsor.json", {"packages": [{"name": "No description"}, "text",
                                                 {"name": "Fine", "description": "ok"}]})
        config = load_config(self.dir)
        self.assertEqual([p["name"] for p in config.packages], ["Fine"])
        self.assertEqual(sum("needs a name" in p for p in config.problems), 2)

    def test_a_non_text_price_is_dropped(self) -> None:
        self.write("sponsor.json", {"packages": [{"name": "A", "description": "x", "price": 99}]})
        config = load_config(self.dir)
        self.assertNotIn("price", config.packages[0])
        self.assertTrue(any("price" in p for p in config.problems))

    def test_no_logo_testimonial_rating_or_as_seen_on_claim_can_appear(self) -> None:
        self.analytics()
        page = self.page(organization="Dokaz Industries", contact_email="a@example.com")
        self.assertNotRegex(page, r"(?i)<img|testimonial|as seen on|trusted by|\d stars|rated|reviews?")


class ContactTests(Base):
    def test_with_nothing_configured_enquiries_are_not_open_and_nothing_is_linked(self) -> None:
        page = self.page()
        self.assertIn("enquiries are not open yet", page)
        self.assertNotIn("mailto:", page)
        self.assertNotIn("<form", page)

    def test_an_email_gives_a_mailto_link_and_says_nothing_is_automatic(self) -> None:
        page = self.page(contact_email="pantheonunknown@gmail.com")
        self.assertIn('href="mailto:pantheonunknown@gmail.com?subject=Sponsorship%20enquiry"', page)
        self.assertIn("nothing is answered automatically", page)
        self.assertNotIn("enquiries are not open yet", page)

    def test_a_form_action_gives_a_post_form_to_exactly_that_url(self) -> None:
        page = self.page(form_action="https://forms.example.test/submit/abc")
        self.assertIn('<form method="post" action="https://forms.example.test/submit/abc">', page)
        self.assertEqual(page.count("<form"), 1)

    def test_an_invalid_email_or_non_https_form_is_dropped_and_named(self) -> None:
        for field, value in (("contact_email", "not an email"),
                             ("contact_email", 'a@b.co"><script>'),
                             ("contact_email", "a b@example.com"),
                             ("form_action", "http://insecure.example/x"),
                             ("form_action", "javascript:alert(1)"),
                             ("form_action", "https://user:pw@evil.example/x")):
            self.write("sponsor.json", {field: value})
            config = load_config(self.dir)
            self.assertIsNone(config.email if field == "contact_email" else config.form_action,
                              value)
            self.assertTrue(config.problems, value)
            page = sponsor_page(config, None, GROUPS, BASE)
            self.assertNotIn("mailto:", page, value)
            self.assertNotIn("<form", page, value)

    def test_an_unreadable_config_falls_back_to_the_defaults_and_says_so(self) -> None:
        self.write("sponsor.json", "{nope")
        config = load_config(self.dir)
        self.assertTrue(any("unreadable" in p for p in config.problems))
        self.assertEqual(config.packages, DEFAULT_PACKAGES)
        self.assertIsNone(config.email)


class EscapingTests(Base):
    def test_hostile_strings_from_both_files_never_become_markup(self) -> None:
        self.analytics(source=EVIL, channel_url="https://example.test/?q=" + "x")
        page = self.page(organization=EVIL, packages=[{"name": EVIL, "description": EVIL,
                                                       "price": EVIL}])
        for fragment in ("<script>alert", "<img src=x", 'onerror=alert(2)>', "alert(1)</script>"):
            body = LD.sub("", page)
            self.assertNotIn(fragment, body, fragment)
        self.assertIn("&lt;script&gt;alert(1)", page)

    def test_json_ld_cannot_be_closed_early_by_a_string(self) -> None:
        page = self.page(organization=EVIL, url="https://example.test/")
        blocks = LD.findall(page)
        self.assertEqual(len(blocks), 1)                       # a stray </script> would split it
        self.assertNotIn("</script", blocks[0])
        graph = json.loads(blocks[0])["@graph"]
        self.assertEqual(graph[0]["name"], " ".join(EVIL.split()))     # round-trips as data

    def test_hostile_series_and_video_titles_are_escaped_in_the_links(self) -> None:
        groups = {"x": (EVIL, [manifest(1, EVIL)])}
        page = sponsor_page(load_config(self.dir), None, groups, BASE)
        self.assertNotIn("<img src=x", page)
        self.assertNotIn("<script>alert", page)


class StructuredDataAndLinksTests(Base):
    def blocks(self, page: str) -> list[dict]:
        return [json.loads(b) for b in LD.findall(page)]

    def test_json_ld_is_organization_and_service_with_no_rating_review_or_offer(self) -> None:
        page = self.page(organization="Dokaz Industries", url="https://dokazindustries.com",
                         contact_email="a@example.com")
        (doc,) = self.blocks(page)
        types = [n["@type"] for n in doc["@graph"]]
        self.assertEqual(types, ["Organization", "Service"])
        text = json.dumps(doc)
        for banned in ("aggregateRating", "ratingValue", "review", "Review", "offers", "Offer",
                       "price", "reviewCount"):
            self.assertNotIn(banned, text)
        self.assertEqual(doc["@graph"][0]["contactPoint"]["email"], "a@example.com")
        self.assertEqual(doc["@graph"][1]["url"], f"{BASE}/sponsor/")

    def test_the_disclosure_line_and_a_canonical_link_are_present(self) -> None:
        page = self.page()
        self.assertIn(DISCLOSURE, page)
        self.assertIn(f'<link rel="canonical" href="{BASE}/sponsor/">', page)

    def test_it_links_to_every_series_hub_and_the_recent_videos(self) -> None:
        page = self.page()
        self.assertIn('<a href="../harbor-stories/">Harbor stories</a>', page)
        self.assertIn('href="../harbor-stories/vid-02/"', page)
        self.assertLess(page.index("vid-02"), page.index("vid-01"))      # newest first

    def test_with_no_series_there_are_no_empty_link_sections(self) -> None:
        page = sponsor_page(load_config(self.dir), None, {}, BASE)
        self.assertNotIn("The series", page)
        self.assertNotIn("Recent videos", page)


class BuildSiteTests(Base):
    def test_build_site_writes_the_sponsor_page_into_staging_only(self) -> None:
        site = build_site(self.dir, base_url=BASE, today=TODAY)
        sponsor_file = site.root / "sponsor" / "index.html"
        self.assertTrue(sponsor_file.is_file())
        self.assertIn(sponsor_file, site.pages)
        self.assertEqual(site.root, self.dir / "site")
        written = {p for p in self.dir.rglob("*") if p.is_file()}
        self.assertTrue(all(site.root in p.parents for p in written), written)
        self.assertIn(NOT_ENOUGH, sponsor_file.read_text(encoding="utf-8"))

    def test_figures_in_the_directory_are_not_shown_when_nothing_is_published(self) -> None:
        self.write("analytics.json", {"as_of": "2026-12-01", "source": "YouTube Studio",
                                      "views_28d": 777})
        site = build_site(self.dir, base_url=BASE, today=TODAY)
        text = (site.root / "sponsor" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("777", text)
        self.assertIn(NOT_ENOUGH, text)

    def test_the_site_builder_never_runs_a_deploy(self) -> None:
        import pionir.video.pages as pages
        import pionir.video.sponsor as sponsor_module

        for module in (pages, sponsor_module):
            source = Path(module.__file__).read_text(encoding="utf-8")
            for word in ("wrangler", "subprocess", "socket", "urllib.request"):
                self.assertNotIn(word, source, f"{module.__name__}: {word}")


if __name__ == "__main__":
    unittest.main()
