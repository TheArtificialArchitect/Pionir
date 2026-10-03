"""Fetching: politeness and the public-domain gate. A fake Http only; no test touches a network.
Each rule has a test that fails when the rule is removed."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pionir.crew.net import HttpResponse, HttpUnreachable
from pionir.video import fetch as fetch_module
from pionir.video.fetch import (
    USER_AGENT,
    FetchError,
    PoliteFetcher,
    documented_api,
    fetch_item,
    write_pack,
)
from pionir.video.niche import load_niches
from pionir.video.passages import load_pack

NICHES = {n.id: n for n in load_niches()}
SEATTLE = NICHES["seattle-local-history"]
ENGINEERING = NICHES["forgotten-engineering"]
NOW = 1_790_000_000.0                      # October 2026


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class FakeHttp:
    """url -> a response, or a list of them returned in turn; robots.txt is a 404 unless set."""

    def __init__(self, routes: dict[str, object] | None = None) -> None:
        self.routes = dict(routes or {})
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, *, headers: dict | None = None, timeout: float = 20.0) -> HttpResponse:
        self.calls.append((url, dict(headers or {})))
        route = self.routes.get(url)
        if route is None:
            if url.endswith("/robots.txt"):
                return HttpResponse(404, b"", 0.0, {})
            return HttpResponse(404, b"missing", 0.0, {})
        if isinstance(route, list):
            route = route.pop(0) if len(route) > 1 else route[0]
        if isinstance(route, Exception):
            raise route
        if isinstance(route, tuple):
            status, body, *rest = route
            return HttpResponse(status, body if isinstance(body, bytes) else body.encode(), 0.0,
                                rest[0] if rest else {})
        return HttpResponse(200, route if isinstance(route, bytes) else route.encode(), 0.0, {})

    def urls(self) -> list[str]:
        return [u for u, _ in self.calls]


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.clock = FakeClock()
        self.time_now = NOW

    def fresh_fetcher(self, http: FakeHttp) -> PoliteFetcher:
        shutil.rmtree(self.root / "cache", ignore_errors=True)
        return self.fetcher(http)

    def fetcher(self, http: FakeHttp, **kw) -> PoliteFetcher:
        return PoliteFetcher(http, self.root / "cache", clock=lambda: self.time_now,
                             sleep=self.clock.sleep, monotonic=self.clock.monotonic, **kw)

    def source(self, niche=SEATTLE, source_id="loc"):
        return niche.source(source_id)


PAGE = "https://www.loc.gov/pictures/item/abc/"
LOC = "https://www.loc.gov/"


class PolitenessTests(Base):
    def test_every_request_identifies_itself_including_robots(self) -> None:
        http = FakeHttp({PAGE: "hello"})
        self.fetcher(http).get(PAGE, self.source())
        self.assertGreaterEqual(len(http.calls), 2)
        for _url, headers in http.calls:
            self.assertEqual(headers.get("User-Agent"), USER_AGENT)
        self.assertIn("dokazindustries.com", USER_AGENT)

    def test_robots_disallow_stops_the_fetch_before_the_page_is_asked_for(self) -> None:
        http = FakeHttp({LOC + "robots.txt": "User-agent: *\nDisallow: /pictures/\n",
                         PAGE: "hello"})
        with self.assertRaisesRegex(FetchError, "robots.txt"):
            self.fetcher(http).get(PAGE, self.source())
        self.assertNotIn(PAGE, http.urls())

    def test_robots_allow_for_other_paths_still_works(self) -> None:
        http = FakeHttp({LOC + "robots.txt": "User-agent: *\nDisallow: /private/\n", PAGE: "ok"})
        self.assertEqual(self.fetcher(http).get(PAGE, self.source()), b"ok")

    def test_an_unreadable_robots_file_fails_closed(self) -> None:
        for answer in ((500, b""), (503, b""), (403, b""), (401, b""), HttpUnreachable("down")):
            http = FakeHttp({LOC + "robots.txt": answer, PAGE: "hello"})
            with self.assertRaises(FetchError, msg=str(answer)):
                self.fetcher(http).get(PAGE, self.source())
            self.assertNotIn(PAGE, http.urls(), str(answer))

    def test_a_missing_robots_file_means_no_rules(self) -> None:
        http = FakeHttp({PAGE: "hello"})                      # robots.txt answers 404
        self.assertEqual(self.fetcher(http).get(PAGE, self.source()), b"hello")

    def test_a_declared_crawl_delay_is_honoured(self) -> None:
        a, b = LOC + "pictures/item/a/", LOC + "pictures/item/b/"
        http = FakeHttp({LOC + "robots.txt": "User-agent: *\nCrawl-delay: 9\n", a: "A", b: "B"})
        fetcher = self.fetcher(http)
        fetcher.get(a, self.source())
        fetcher.get(b, self.source())
        self.assertTrue(any(s >= 9 - 0.01 for s in self.clock.slept), self.clock.slept)

    def test_requests_to_one_host_are_spaced(self) -> None:
        a, b = LOC + "pictures/item/a/", LOC + "pictures/item/b/"
        http = FakeHttp({a: "A", b: "B"})
        fetcher = self.fetcher(http, min_interval=2.0)
        fetcher.get(a, self.source())
        fetcher.get(b, self.source())
        self.assertTrue(self.clock.slept)
        self.assertTrue(all(s >= 1.99 for s in self.clock.slept[:1]), self.clock.slept)

    def test_429_backs_off_for_retry_after_then_succeeds(self) -> None:
        http = FakeHttp({PAGE: [(429, b"slow", {"Retry-After": "7"}), "fine"]})
        self.assertEqual(self.fetcher(http).get(PAGE, self.source()), b"fine")
        self.assertIn(7.0, self.clock.slept)

    def test_a_server_that_keeps_saying_503_is_given_up_on(self) -> None:
        http = FakeHttp({PAGE: (503, b"no")})
        with self.assertRaisesRegex(FetchError, "503"):
            self.fetcher(http).get(PAGE, self.source())
        self.assertEqual(http.urls().count(PAGE), fetch_module.RETRIES + 1)

    def test_a_dead_connection_is_a_clear_error(self) -> None:
        http = FakeHttp({PAGE: HttpUnreachable("timed out")})
        with self.assertRaisesRegex(FetchError, "did not answer"):
            self.fetcher(http).get(PAGE, self.source())


class HostAndRedirectTests(Base):
    def test_a_url_off_the_sources_hosts_or_not_https_is_refused_with_no_request(self) -> None:
        http = FakeHttp()
        fetcher = self.fetcher(http)
        for url in ("https://evil.example/x", "http://www.loc.gov/x",
                    "https://www.loc.gov.evil.example/x", "https://user@www.loc.gov/x"):
            with self.assertRaises(FetchError, msg=url):
                fetcher.get(url, self.source())
        self.assertEqual(http.calls, [])

    def test_a_redirect_off_host_is_not_followed(self) -> None:
        http = FakeHttp({PAGE: (302, b"", {"Location": "https://evil.example/steal"}),
                         "https://evil.example/steal": "pwned"})
        with self.assertRaises(FetchError):
            self.fetcher(http).get(PAGE, self.source())
        self.assertNotIn("https://evil.example/steal", http.urls())

    def test_a_redirect_to_http_is_not_followed(self) -> None:
        http = FakeHttp({PAGE: (301, b"", {"Location": "http://www.loc.gov/plain"})})
        with self.assertRaises(FetchError):
            self.fetcher(http).get(PAGE, self.source())
        self.assertNotIn("http://www.loc.gov/plain", http.urls())

    def test_a_redirect_within_the_allowlist_is_followed(self) -> None:
        target = "https://tile.loc.gov/storage/x.txt"
        http = FakeHttp({PAGE: (301, b"", {"Location": target}), target: "moved here"})
        self.assertEqual(self.fetcher(http).get(PAGE, self.source()), b"moved here")

    def test_a_redirect_loop_ends(self) -> None:
        http = FakeHttp({PAGE: (302, b"", {"Location": PAGE})})
        with self.assertRaisesRegex(FetchError, "redirects"):
            self.fetcher(http).get(PAGE, self.source())


class BodyAndCacheTests(Base):
    def test_a_body_over_the_cap_is_refused_not_used_half(self) -> None:
        http = FakeHttp({PAGE: b"x" * 101})
        with self.assertRaisesRegex(FetchError, "cut-off"):
            self.fetcher(http).get(PAGE, self.source(), limit=100)

    def test_a_non_200_is_an_error(self) -> None:
        http = FakeHttp({PAGE: (404, b"gone")})
        with self.assertRaisesRegex(FetchError, "404"):
            self.fetcher(http).get(PAGE, self.source())

    def test_the_second_ask_is_served_from_the_cache(self) -> None:
        http = FakeHttp({PAGE: "hello"})
        fetcher = self.fetcher(http)
        fetcher.get(PAGE, self.source())
        before = (fetcher.requests, http.urls().count(PAGE))
        self.assertEqual(self.fetcher(http).get(PAGE, self.source()), b"hello")   # a new fetcher
        self.assertEqual((fetcher.requests, http.urls().count(PAGE)), before)

    def test_a_stale_cache_entry_is_fetched_again(self) -> None:
        http = FakeHttp({PAGE: ["first", "second"]})
        self.fetcher(http).get(PAGE, self.source())
        self.time_now += fetch_module.CACHE_TTL + 10
        self.assertEqual(self.fetcher(http).get(PAGE, self.source()), b"second")

    def test_a_tampered_cache_entry_is_not_trusted(self) -> None:
        http = FakeHttp({PAGE: ["real", "real"]})
        self.fetcher(http).get(PAGE, self.source())
        for body in (self.root / "cache").glob("*.body"):
            if body.read_bytes() == b"real":
                body.write_bytes(b"fake")
        self.assertEqual(self.fetcher(http).get(PAGE, self.source()), b"real")

    def test_nothing_is_written_outside_the_cache_folder(self) -> None:
        self.fetcher(FakeHttp({PAGE: "hello"})).get(PAGE, self.source())
        self.assertEqual({p.name for p in self.root.iterdir()}, {"cache"})


class DocumentedApiTests(Base):
    COMMONS_API = "https://commons.wikimedia.org/w/api.php?action=query&titles=File:A.jpg"

    def test_the_exemption_is_exactly_the_documented_endpoints(self) -> None:
        self.assertTrue(documented_api(self.COMMONS_API))
        self.assertTrue(documented_api("https://archive.org/metadata/someid"))
        self.assertTrue(documented_api("https://www.loc.gov/item/2001/?fo=json"))
        self.assertTrue(documented_api(
            "https://digitalcollections.lib.washington.edu/digital/api/collections/a/items/1/false"))
        for url in ("https://commons.wikimedia.org/w/index.php?title=X",
                    "https://commons.wikimedia.org/wiki/File:A.jpg",
                    "https://archive.org/download/x/x.txt", "https://archive.org/details/x",
                    "https://www.loc.gov/item/2001/",           # no fo=json: a page, not the API
                    "https://www.loc.gov/pictures/item/abc/",
                    "https://evil.example/w/api.php"):
            self.assertFalse(documented_api(url), url)

    def test_the_api_is_fetched_even_when_robots_disallow_everything(self) -> None:
        http = FakeHttp({"https://commons.wikimedia.org/robots.txt": "User-agent: *\nDisallow: /\n",
                         self.COMMONS_API: "{}"})
        body = self.fetcher(http).get(self.COMMONS_API, self.source(SEATTLE, "commons"))
        self.assertEqual(body, b"{}")

    def test_a_non_api_page_on_the_same_host_still_obeys_robots(self) -> None:
        index = "https://commons.wikimedia.org/w/index.php?title=File:A.jpg"
        http = FakeHttp({"https://commons.wikimedia.org/robots.txt": "User-agent: *\nDisallow: /\n",
                         index: "page"})
        with self.assertRaisesRegex(FetchError, "robots"):
            self.fetcher(http).get(index, self.source(SEATTLE, "commons"))
        self.assertNotIn(index, http.urls())

    def test_the_api_still_identifies_itself_and_spaces_requests(self) -> None:
        http = FakeHttp({self.COMMONS_API: "{}", self.COMMONS_API + "2": "{}"})
        fetcher = self.fetcher(http)
        fetcher.get(self.COMMONS_API, self.source(SEATTLE, "commons"))
        fetcher.get(self.COMMONS_API + "2", self.source(SEATTLE, "commons"))
        self.assertTrue(all(h.get("User-Agent") == USER_AGENT for _u, h in http.calls))
        self.assertTrue(self.clock.slept)


def ia_meta(**meta) -> str:
    base = {"title": "How the lighthouse lens works", "creator": "A. Fresnel",
            "description": "A long description of the lens, its rings, and how a lamp became "
                           "a beam visible for twenty miles."}
    base.update(meta)
    return json.dumps({"metadata": base})


class InternetArchiveTests(Base):
    URL = "https://archive.org/metadata/lens1850"

    def item(self, **meta):
        http = FakeHttp({self.URL: ia_meta(**meta)})
        return fetch_item(self.fetcher(http), ENGINEERING, "internet_archive", "lens1850",
                          clock=lambda: NOW)

    def test_a_public_domain_item_carries_its_basis_credit_and_url(self) -> None:
        item = self.item(licenseurl="https://creativecommons.org/publicdomain/mark/1.0/")
        self.assertEqual(item.source_id, "internet_archive")
        self.assertEqual(item.url, "https://archive.org/details/lens1850")
        self.assertIn("public domain", item.license_basis)
        self.assertIn("A. Fresnel", item.credit)
        self.assertIn("lens1850", item.credit)

    def test_an_item_with_no_stated_basis_is_refused(self) -> None:
        with self.assertRaisesRegex(FetchError, "public-domain basis"):
            self.item()
        with self.assertRaises(FetchError):
            self.item(licenseurl="https://creativecommons.org/licenses/by-nc/4.0/")

    def test_a_denied_status_beats_a_stray_public_domain_word(self) -> None:
        with self.assertRaises(FetchError):
            self.item(rights="Not in the public domain. All rights reserved.")

    def test_a_dark_item_and_a_hostile_identifier_are_refused(self) -> None:
        http = FakeHttp({self.URL: json.dumps({})})
        with self.assertRaisesRegex(FetchError, "no metadata"):
            fetch_item(self.fetcher(http), ENGINEERING, "internet_archive", "lens1850")
        with self.assertRaisesRegex(FetchError, "not an Internet Archive identifier"):
            fetch_item(self.fetcher(http), ENGINEERING, "internet_archive", "../x?y=1")

    def test_markup_in_a_description_is_flattened_to_text(self) -> None:
        item = self.item(licenseurl="https://creativecommons.org/publicdomain/mark/1.0/",
                         description="<script>alert(1)</script>The lens is made of many glass "
                                     "rings set in a brass frame.")
        self.assertNotIn("<", item.text)


def commons_doc(licence="Public domain", mime="image/jpeg", url=None) -> str:
    return json.dumps({"query": {"pages": {"1": {"imageinfo": [{
        "url": url or "https://upload.wikimedia.org/wikipedia/commons/a/a1/Pier.jpg",
        "descriptionurl": "https://commons.wikimedia.org/wiki/File:Pier.jpg", "mime": mime,
        "extmetadata": {"LicenseShortName": {"value": licence},
                        "Artist": {"value": "<a href='x'>Jane Photographer</a>"},
                        "ImageDescription": {"value": "A timber pier at low tide in 1907, "
                                                      "photographed from the shore."}}}]}}}})


class CommonsTests(Base):
    API = ("https://commons.wikimedia.org/w/api.php?action=query&format=json&prop=imageinfo"
           "&iiprop=url|mime|extmetadata&titles=File%3APier.jpg")
    IMG = "https://upload.wikimedia.org/wikipedia/commons/a/a1/Pier.jpg"

    def item(self, doc=None, image=b"\xff\xd8\xffJPEGBYTES"):
        http = FakeHttp({self.API: doc or commons_doc(), self.IMG: image})
        self.http = http
        return fetch_item(self.fetcher(http), SEATTLE, "commons", "Pier.jpg", clock=lambda: NOW)

    def test_a_public_domain_file_returns_the_image_credit_and_licence(self) -> None:
        item = self.item()
        self.assertEqual(item.image, b"\xff\xd8\xffJPEGBYTES")
        self.assertIn("Jane Photographer", item.credit)
        self.assertEqual(item.license_basis, "Public domain")
        self.assertNotIn("<", item.credit)

    def test_cc_by_is_accepted_with_attribution_required(self) -> None:
        item = self.item(commons_doc("CC BY-SA 4.0"))
        self.assertIn("attribution required", item.credit)

    def test_non_free_or_unstated_licences_are_refused_and_no_image_is_downloaded(self) -> None:
        for licence in ("CC BY-NC 4.0", "CC BY-ND 2.0", "Fair use", "GFDL", "", "All rights reserved"):
            with self.assertRaises(FetchError, msg=licence):
                self.item(commons_doc(licence))
            self.assertNotIn(self.IMG, self.http.urls(), licence)

    def test_a_non_image_file_is_refused(self) -> None:
        with self.assertRaisesRegex(FetchError, "not an image"):
            self.item(commons_doc(mime="application/pdf"))

    def test_a_file_url_off_the_commons_hosts_is_refused(self) -> None:
        with self.assertRaisesRegex(FetchError, "not on the Commons hosts"):
            self.item(commons_doc(url="https://evil.example/Pier.jpg"))

    def test_a_missing_file_and_a_hostile_title_are_refused(self) -> None:
        http = FakeHttp({self.API: json.dumps({"query": {"pages": {"-1": {"missing": ""}}}})})
        with self.assertRaisesRegex(FetchError, "no such file"):
            fetch_item(self.fetcher(http), SEATTLE, "commons", "Pier.jpg")
        with self.assertRaisesRegex(FetchError, "not a Commons file title"):
            fetch_item(self.fetcher(http), SEATTLE, "commons", "x|y{evil}")


class PageSourceTests(Base):
    def test_chronicling_america_refuses_pages_after_1928(self) -> None:
        http = FakeHttp()
        with self.assertRaisesRegex(FetchError, "1928"):
            fetch_item(self.fetcher(http), SEATTLE, "chronicling_america",
                       "sn83045487/1930-05-01/ed-1/seq-1")
        self.assertEqual(http.calls, [])

    def test_chronicling_america_returns_ocr_text_marked_as_ocr(self) -> None:
        url = "https://chroniclingamerica.loc.gov/lccn/sn83045487/1907-05-01/ed-1/seq-1/ocr.txt"
        http = FakeHttp({url: "The steamer arrived in port on Tuesday with nine hundred "
                              "passengers and a cargo of lumber."})
        item = fetch_item(self.fetcher(http), SEATTLE, "chronicling_america",
                          "sn83045487/1907-05-01/ed-1/seq-1", clock=lambda: NOW)
        self.assertIn("OCR", item.text)
        self.assertIn("public domain", item.license_basis)
        self.assertIn("1907", item.license_basis)

    def test_chronicling_america_refuses_a_malformed_reference(self) -> None:
        with self.assertRaises(FetchError):
            fetch_item(self.fetcher(FakeHttp()), SEATTLE, "chronicling_america", "../../x")

    def loc(self, rights):
        url = "https://www.loc.gov/item/2001/?fo=json"
        doc = {"item": {"title": "Waterfront, Seattle", "rights_advisory": rights,
                        "description": ["A view of the waterfront and its docks with a ferry "
                                        "crossing the sound."]}}
        return fetch_item(self.fresh_fetcher(FakeHttp({url: json.dumps(doc)})), SEATTLE, "loc", "2001",
                          clock=lambda: NOW)

    def test_loc_needs_a_public_domain_statement(self) -> None:
        item = self.loc("No known restrictions on publication.")
        self.assertIn("Library of Congress", item.credit)
        for rights in ("", "Rights status not evaluated.", "Used with permission.",
                       "Copyrighted by the photographer"):
            with self.assertRaises(FetchError, msg=rights):
                self.loc(rights)

    def uw(self, rights):
        url = ("https://digitalcollections.lib.washington.edu/digital/api/collections/"
               "postcard/items/55/false")
        doc = {"fields": [{"key": "title", "value": "Pike Place, 1912"},
                          {"key": "descri", "value": "A busy market street with horse carts and "
                                                     "a row of fruit stands."},
                          {"key": "rights", "value": rights}]}
        return fetch_item(self.fresh_fetcher(FakeHttp({url: json.dumps(doc)})), SEATTLE, "uw_collections",
                          "postcard/55", clock=lambda: NOW)

    def test_uw_collections_needs_a_public_domain_rights_field(self) -> None:
        item = self.uw("Public domain")
        self.assertIn("University of Washington", item.credit)
        for rights in ("", "Contact Special Collections for permission", "All rights reserved"):
            with self.assertRaises(FetchError, msg=rights):
                self.uw(rights)

    def patent(self, date_text):
        url = "https://patents.google.com/patent/US123456A/en"
        page = ('<html><head><meta name="DC.date" content="%s">'
                '<meta name="DC.title" content="Improvement in lamps">'
                '<meta name="DC.contributor" content="J. Inventor">'
                '<meta name="DC.description" content="A lamp with a double wick and a glass '
                'chimney that draws air through the flame."></head></html>' % date_text)
        return fetch_item(self.fresh_fetcher(FakeHttp({url: page})), ENGINEERING, "patents",
                          "US123456A", clock=lambda: NOW)

    def test_a_patent_must_be_at_least_25_years_old(self) -> None:
        item = self.patent("1884-03-04")
        self.assertIn("expired", item.license_basis)
        with self.assertRaisesRegex(FetchError, "years old"):
            self.patent("2005-03-04")
        with self.assertRaises(FetchError):
            self.patent("")                                    # no date, no use

    def test_a_source_the_niche_does_not_allowlist_is_refused(self) -> None:
        with self.assertRaisesRegex(FetchError, "not allowlisted"):
            fetch_item(self.fetcher(FakeHttp()), SEATTLE, "patents", "US123456A")
        with self.assertRaisesRegex(FetchError, "no reader"):
            fetch_item(self.fetcher(FakeHttp()), SEATTLE, "spl", "x")


class PackTests(Base):
    def test_fetched_items_land_in_a_pack_the_loader_accepts_with_their_credit(self) -> None:
        http = FakeHttp({CommonsTests.API: commons_doc(), CommonsTests.IMG: b"\x89PNGbytes"})
        item = fetch_item(self.fetcher(http), SEATTLE, "commons", "Pier.jpg", clock=lambda: NOW)
        pack_path = self.root / "pack.json"
        counts = write_pack(pack_path, "The pier", [item])
        self.assertEqual(counts, {"passages": 1, "images": 1})
        pack = load_pack(pack_path, SEATTLE)
        self.assertEqual(pack.topic, "The pier")
        self.assertEqual(len(pack.passages), 1)
        self.assertIn("Jane Photographer", pack.images[0].credit)
        self.assertTrue((self.root / pack.images[0].file).is_file()
                        if hasattr(pack.images[0], "file") else True)

    def test_fetching_the_same_item_twice_does_not_duplicate_it(self) -> None:
        http = FakeHttp({CommonsTests.API: commons_doc(), CommonsTests.IMG: b"x"})
        item = fetch_item(self.fetcher(http), SEATTLE, "commons", "Pier.jpg", clock=lambda: NOW)
        pack_path = self.root / "pack.json"
        write_pack(pack_path, "The pier", [item])
        self.assertEqual(write_pack(pack_path, "The pier", [item]), {"passages": 0, "images": 0})

    def test_a_corrupt_pack_file_is_not_overwritten(self) -> None:
        pack_path = self.root / "pack.json"
        pack_path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(FetchError):
            write_pack(pack_path, "t", [])
        self.assertEqual(pack_path.read_text(encoding="utf-8"), "{not json")


class NoNetworkTests(Base):
    def test_the_fake_path_never_opens_a_socket(self) -> None:
        http = FakeHttp({PAGE: "hello"})
        with mock.patch("socket.socket.connect", side_effect=AssertionError("network used")), \
                mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used")):
            self.assertEqual(self.fetcher(http).get(PAGE, self.source()), b"hello")


if __name__ == "__main__":
    unittest.main()
