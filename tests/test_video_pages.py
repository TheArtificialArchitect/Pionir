"""The static video pages: escaped, honest, interlinked, written only to the staging folder.

One real tiny package is rendered once (real ffmpeg, Pillow, fake tone for the voice); each test
copies it, changes the manifest the way a hostile or unfinished one would look, and builds the
site from the copy.
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

from pionir.video.disclosure import DISCLOSURE
from pionir.video.package import queue_dir
from pionir.video.pages import SITE, SiteError, build_site, clock, iso_duration, json_ld
from pionir.video.pipeline import make_video
from video_support import (
    HAVE_FFMPEG,
    HAVE_PIL,
    FakeSynth,
    FakeWriter,
    good_script,
    make_niche,
    write_pack,
)

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
CAN_RENDER = HAVE_FFMPEG and HAVE_PIL
BASE = "https://example.test/video"
LD = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)


def ld_blocks(page: str) -> list[dict[str, Any]]:
    return [json.loads(raw) for raw in LD.findall(page)]


@unittest.skipUnless(CAN_RENDER, "needs ffmpeg and Pillow")
class PagesBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._master = tempfile.TemporaryDirectory()
        base = Path(cls._master.name)
        work = base / "work"
        work.mkdir()
        cls.made = make_video(make_niche(), write_pack(work), base / "video",
                              writer=FakeWriter(good_script()), synth=FakeSynth(),
                              now=lambda: NOW, size=(480, 270))
        cls.master_dir = base / "video"
        cls.video_id = cls.made.package.id

    @classmethod
    def tearDownClass(cls) -> None:
        cls._master.cleanup()

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.video_dir = Path(self._tmp.name) / "video"
        shutil.copytree(self.master_dir, self.video_dir)

    def folder(self, video_id: str | None = None) -> Path:
        return queue_dir(self.video_dir) / (video_id or self.video_id)

    def edit_manifest(self, video_id: str | None = None, **changes: Any) -> None:
        path = self.folder(video_id) / "manifest.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc.update(changes)
        path.write_text(json.dumps(doc), encoding="utf-8")

    def second_video(self, title: str, created_at: str, series: str | None = None) -> str:
        new_id = f"second-harbor-{len(title):08d}"
        shutil.copytree(self.folder(), self.folder(new_id))
        changes: dict[str, Any] = {"id": new_id, "title": title, "created_at": created_at}
        if series:
            changes["series"] = series
        self.edit_manifest(new_id, **changes)
        return new_id

    def page(self, video_id: str | None = None, series_slug: str = "harbor-stories") -> str:
        site = build_site(self.video_dir, base_url=BASE)
        path = site.root / series_slug / (video_id or self.video_id) / "index.html"
        return path.read_text(encoding="utf-8")


class PageContentTests(PagesBase):
    def test_a_page_carries_transcript_sources_disclosure_and_valid_video_markup(self) -> None:
        page = self.page()
        self.assertIn("The harbor opened in 1851.", page)
        self.assertIn(DISCLOSURE, page)
        self.assertIn("Sources and credits", page)
        self.assertIn("https://en.wikipedia.org/wiki/Harbor", page)
        (ld,) = ld_blocks(page)
        manifest = self.made.package.manifest
        url = f"{BASE}/harbor-stories/{self.video_id}/"
        self.assertEqual(ld["@type"], "VideoObject")
        self.assertEqual(ld["name"], "The old harbor")
        self.assertEqual(ld["description"], manifest["summary"])
        self.assertEqual(ld["thumbnailUrl"], f"{url}thumbnail.png")
        self.assertEqual(ld["duration"], iso_duration(manifest["duration_seconds"]))
        self.assertRegex(ld["duration"], r"^PT(\d+H)?(\d+M)?(\d+S)?$")
        self.assertEqual(ld["isPartOf"]["name"], "Harbor stories")
        self.assertIn("The harbor opened in 1851.", ld["transcript"])
        self.assertEqual([s["url"] for s in ld["isBasedOn"]][:1],
                         ["https://en.wikipedia.org/wiki/Harbor"])

    def test_the_thumbnail_and_captions_are_copied_beside_the_page(self) -> None:
        site = build_site(self.video_dir, base_url=BASE)
        folder = site.root / "harbor-stories" / self.video_id
        self.assertTrue((folder / "thumbnail.png").is_file())
        self.assertTrue((folder / "captions.srt").is_file())
        self.assertFalse((folder / "video.mp4").exists())       # the video is not hosted here

    def test_a_video_not_on_youtube_yet_has_no_upload_date_or_embed(self) -> None:
        page = self.page()
        (ld,) = ld_blocks(page)
        self.assertNotIn("uploadDate", ld)
        self.assertNotIn("embedUrl", ld)
        self.assertNotIn("<iframe", page)
        self.assertIn("not published on YouTube yet", page)

    def test_a_recorded_upload_adds_the_date_and_the_embed(self) -> None:
        self.edit_manifest(uploaded={"upload_date": "2027-01-05T10:00:00Z",
                                     "youtube_id": "abcDEF12345"})
        page = self.page()
        (ld,) = ld_blocks(page)
        self.assertEqual(ld["uploadDate"], "2027-01-05T10:00:00Z")
        self.assertEqual(ld["embedUrl"], "https://www.youtube.com/embed/abcDEF12345")
        self.assertIn('<iframe src="https://www.youtube.com/embed/abcDEF12345"', page)

    def test_a_malformed_upload_record_is_not_believed(self) -> None:
        for bad in ({"upload_date": "tomorrow", "youtube_id": "abcDEF12345"},
                    {"upload_date": "2027-01-05T10:00:00Z", "youtube_id": "x/../y"},
                    {"upload_date": "2027-01-05T10:00:00Z"}, "yes", True):
            self.edit_manifest(uploaded=bad)
            (ld,) = ld_blocks(self.page())
            self.assertNotIn("uploadDate", ld, bad)
            self.assertNotIn("embedUrl", ld, bad)

    def test_nothing_the_pipeline_did_not_measure_is_claimed(self) -> None:
        page = self.page()
        for word in ("viewCount", "interactionStatistic", "aggregateRating", "ratingValue",
                     "datePublished", "subscriber", "views", "likes"):
            self.assertNotIn(word, page, word)

    def test_duration_helpers(self) -> None:
        self.assertEqual(iso_duration(125.4), "PT2M5S")
        self.assertEqual(iso_duration(3600), "PT1H")
        self.assertEqual(iso_duration(59.6), "PT1M")
        self.assertEqual(iso_duration(0), "PT0S")
        self.assertEqual(clock(125.4), "2:05")


class HostileTextTests(PagesBase):
    EVIL = '</script><script>alert(1)</script><img src=x onerror=alert(2)>"\'&\u2028'

    def hostile_page(self) -> str:
        evil = self.EVIL
        manifest = json.loads((self.folder() / "manifest.json").read_text(encoding="utf-8"))
        sources = [{**s, "title": evil, "credit": evil, "url": "javascript:alert(3)"}
                   for s in manifest["sources"]]
        self.edit_manifest(title=evil, summary=evil, series=evil, sources=sources,
                           images=[{"id": "i1", "url": "http://insecure.example/x", "credit": evil}])
        script_path = self.folder() / "script.json"
        script = json.loads(script_path.read_text(encoding="utf-8"))
        script["scenes"][0]["heading"] = evil
        script["scenes"][0]["lines"][0]["text"] = evil
        script_path.write_text(json.dumps(script), encoding="utf-8")
        site = build_site(self.video_dir, base_url=BASE)
        self.assertEqual(site.skipped, {})
        paths = [p for p in site.pages if p.name == "index.html"]
        self.assertGreaterEqual(len(paths), 3)
        return "\n".join(p.read_text(encoding="utf-8") for p in paths)

    def test_nothing_hostile_survives_as_markup_in_any_page(self) -> None:
        pages = self.hostile_page()
        self.assertNotIn("<img src=x", pages)
        self.assertNotIn("<script>alert", pages)
        self.assertNotIn("onerror=alert(2)>", pages)
        for block in LD.findall(pages):
            self.assertNotIn("\u2028", block)     # a raw line separator breaks old script parsers

    def test_the_structured_data_cannot_leave_its_script_element(self) -> None:
        self.hostile_page()
        site_pages = list((self.video_dir / SITE).rglob("index.html"))
        for path in site_pages:
            page = path.read_text(encoding="utf-8")
            blocks = LD.findall(page)
            self.assertEqual(page.count("<script"), len(blocks), path)
            self.assertEqual(page.count("</script"), len(blocks), path)
            for block in ld_blocks(page):
                self.assertEqual(block["@context"], "https://schema.org")
        video_page = (self.video_dir / SITE).rglob(f"{self.video_id}/index.html")
        (ld,) = ld_blocks(next(video_page).read_text(encoding="utf-8"))
        self.assertEqual(ld["name"], self.EVIL)         # the data round-trips unharmed

    def test_a_source_that_is_not_https_is_text_not_a_link(self) -> None:
        pages = self.hostile_page()
        self.assertNotIn('href="javascript:', pages)
        self.assertNotIn('href="http://', pages)
        self.assertIn("javascript:alert(3)", pages)      # shown inertly
        self.assertNotIn("isBasedOn\": [\n    {\n      \"@type\": \"CreativeWork\",\n"
                         "      \"name\": \"javascript", pages)

    def test_json_ld_escapes_the_three_characters_that_break_a_script_element(self) -> None:
        text = json_ld({"a": "</script><b>&"})
        for char in "<>&":
            self.assertNotIn(char, text)
        self.assertEqual(json.loads(text)["a"], "</script><b>&")


class InterlinkTests(PagesBase):
    def test_videos_in_a_series_link_to_each_other_and_to_the_hub(self) -> None:
        other = self.second_video("Another harbor", "2026-10-09T12:00:00Z")
        site = build_site(self.video_dir, base_url=BASE)
        first = (site.root / "harbor-stories" / self.video_id / "index.html").read_text("utf-8")
        second = (site.root / "harbor-stories" / other / "index.html").read_text("utf-8")
        self.assertIn(f'rel="next" href="../{other}/"', first)
        self.assertNotIn('rel="prev"', first)
        self.assertIn(f'rel="prev" href="../{self.video_id}/"', second)
        self.assertNotIn('rel="next"', second)
        self.assertIn(f'<a href="../{other}/">Another harbor</a>', first)    # More in this series
        for page in (first, second):
            self.assertIn('<a href="../">Harbor stories</a>', page)
            self.assertIn('<a href="../../">All series</a>', page)
        hub = (site.root / "harbor-stories" / "index.html").read_text("utf-8")
        self.assertIn(f'href="{self.video_id}/"', hub)
        self.assertIn(f'href="{other}/"', hub)
        (hub_ld,) = ld_blocks(hub)
        self.assertEqual(hub_ld["@type"], "CollectionPage")
        self.assertEqual(len(hub_ld["hasPart"]), 2)
        index = (site.root / "index.html").read_text("utf-8")
        self.assertIn('href="harbor-stories/"', index)
        self.assertIn("2 videos", index)

    def test_every_internal_link_points_at_a_page_that_was_written(self) -> None:
        self.second_video("Another harbor", "2026-10-09T12:00:00Z")
        self.second_video("A third harbor tale", "2026-10-16T12:00:00Z", series="Other series")
        site = build_site(self.video_dir, base_url=BASE)
        checked = 0
        for path in site.pages:
            for href in re.findall(r'href="([^"#]+)"', path.read_text("utf-8")):
                if href.startswith(("https:", "http:")):
                    continue
                target = (path.parent / href).resolve()
                target = target / "index.html" if target.is_dir() or href.endswith("/") else target
                self.assertTrue(target.is_file(), f"{path}: dead link {href}")
                checked += 1
        self.assertGreater(checked, 6)

    def test_three_channels_a_week_make_three_series_and_one_index(self) -> None:
        self.second_video("Telegraph lines", "2026-10-03T12:00:00Z", series="How it worked")
        self.second_video("Running a model on a small card", "2026-10-04T12:00:00Z",
                          series="Local AI on a 12 GB card")
        site = build_site(self.video_dir, base_url=BASE)
        index = (site.root / "index.html").read_text("utf-8")
        for slug in ("harbor-stories", "how-it-worked", "local-ai-on-a-12-gb-card"):
            self.assertIn(f'href="{slug}/"', index)
            self.assertTrue((site.root / slug / "index.html").is_file())
        self.assertEqual(site.skipped, {})


class SafetyTests(PagesBase):
    def test_an_example_niche_video_gets_no_page(self) -> None:
        self.edit_manifest(example=True)
        site = build_site(self.video_dir, base_url=BASE)
        self.assertIn(self.video_id, site.skipped)
        self.assertFalse((site.root / "harbor-stories" / self.video_id).exists())

    def test_a_tampered_or_disclosureless_video_gets_no_page(self) -> None:
        with (self.folder() / "video.mp4").open("ab") as handle:
            handle.write(b"x")
        site = build_site(self.video_dir, base_url=BASE)
        self.assertTrue(any("changed" in p for p in site.skipped[self.video_id]))
        self.assertEqual(site.pages[-1].name, "index.html")
        self.assertFalse(any(self.video_id in str(p) for p in site.pages))

    def test_a_folder_that_is_not_a_video_id_is_skipped_not_followed(self) -> None:
        (queue_dir(self.video_dir) / "Not An Id").mkdir()
        site = build_site(self.video_dir, base_url=BASE)
        self.assertIn("Not An Id", site.skipped)

    def test_the_build_writes_only_inside_the_site_folder(self) -> None:
        def snapshot() -> dict[str, bytes]:
            return {str(p.relative_to(self.video_dir)): p.read_bytes()
                    for p in self.video_dir.rglob("*")
                    if p.is_file() and SITE not in p.relative_to(self.video_dir).parts[:1]}
        before = snapshot()
        build_site(self.video_dir, base_url=BASE)
        self.assertEqual(snapshot(), before)
        self.assertTrue((self.video_dir / SITE / "index.html").is_file())

    def test_a_rebuild_drops_pages_for_videos_that_are_gone(self) -> None:
        other = self.second_video("Another harbor", "2026-10-09T12:00:00Z")
        site = build_site(self.video_dir, base_url=BASE)
        self.assertTrue((site.root / "harbor-stories" / other).is_dir())
        shutil.rmtree(self.folder(other))
        site = build_site(self.video_dir, base_url=BASE)
        self.assertFalse((site.root / "harbor-stories" / other).exists())

    def test_the_build_uses_no_network_and_deploys_nothing(self) -> None:
        with mock.patch("socket.socket.connect", side_effect=AssertionError("network used")), \
                mock.patch("subprocess.run", side_effect=AssertionError("process started")):
            build_site(self.video_dir, base_url=BASE)

    def test_the_base_url_must_be_a_plain_https_url(self) -> None:
        for bad in ("http://example.test/video", "https://user:pw@example.test/video",
                    "https://example.test/video?x=1", "https://example.test/video#f",
                    "file:///C:/x", "example.test/video"):
            with self.assertRaises(SiteError, msg=bad):
                build_site(self.video_dir, base_url=bad)
        self.assertFalse((self.video_dir / SITE).exists())

    def test_an_empty_queue_builds_an_empty_index_without_crashing(self) -> None:
        shutil.rmtree(queue_dir(self.video_dir))
        site = build_site(self.video_dir, base_url=BASE)
        self.assertEqual([p.name for p in site.pages], ["index.html"])


if __name__ == "__main__":
    unittest.main()
