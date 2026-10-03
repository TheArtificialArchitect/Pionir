"""Niche config validation: every rule fails when its line is removed."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from video_support import make_niche, niche_entry

from pionir.video.niche import DEFAULT_PATH, NicheError, live_niches, load_niches, parse_niche


def refused(**overrides) -> str:
    with unittest.TestCase().assertRaises(NicheError) as caught:
        parse_niche(niche_entry(**overrides))
    return str(caught.exception)


class ShippedConfigTests(unittest.TestCase):
    def test_the_shipped_niches_load_and_none_is_live_until_a_channel_exists(self) -> None:
        niches = load_niches(DEFAULT_PATH)
        self.assertEqual({n.id for n in niches},
                         {"seattle-local-history", "forgotten-engineering", "local-ai-12gb"})
        self.assertEqual(live_niches(niches), [])
        self.assertTrue(all(not n.example and n.voice == "af_nicole" for n in niches))
        # one video a week per channel, three channels, so three a week in all
        self.assertTrue(all(n.cadence_days == 7 for n in niches))

    def test_the_environment_can_point_at_another_file(self) -> None:
        import os
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "n.json"
            path.write_text(json.dumps({"niches": [niche_entry()]}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"PIONIR_VIDEO_NICHES": str(path)}):
                self.assertEqual([n.id for n in load_niches()], ["test-harbor"])


class ValidationTests(unittest.TestCase):
    def test_a_good_entry_parses(self) -> None:
        niche = make_niche()
        self.assertEqual(niche.source("loc").hosts, ("www.loc.gov",))
        self.assertIsNone(niche.source("nope"))

    def test_an_unknown_key_is_refused_so_a_typo_cannot_silently_do_nothing(self) -> None:
        self.assertIn("unknown keys", refused(cadense_days=7))

    def test_a_cadence_faster_than_weekly_is_refused(self) -> None:
        self.assertIn("cadence", refused(cadence_days=3))

    def test_an_example_cannot_be_live(self) -> None:
        self.assertIn("example", refused(example=True, live=True))

    def test_the_id_must_be_a_slug(self) -> None:
        refused(id="Bad Id/../x")

    def test_the_voice_must_be_a_kokoro_voice_name(self) -> None:
        refused(voice="not a voice")

    def test_the_length_must_be_a_sane_whole_minute_range(self) -> None:
        refused(length_minutes=[0, 12])
        refused(length_minutes=[12, 8])
        refused(length_minutes=[8, 90])
        refused(length_minutes=[8.5, 12])

    def test_the_kind_must_be_known(self) -> None:
        self.assertIn("kind", refused(kind="gambling"))

    def test_a_niche_needs_series_and_sources(self) -> None:
        refused(series=[])
        refused(sources=[])

    def test_a_source_needs_valid_hosts(self) -> None:
        bad = [{"id": "x", "name": "X", "hosts": [], "license": "pd"}]
        refused(sources=bad)
        bad[0]["hosts"] = ["http://evil.example/path"]
        refused(sources=bad)

    def test_the_scene_mix_must_sum_to_one_and_use_real_scene_types(self) -> None:
        refused(scene_mix={"image": 0.5, "card": 0.2})
        refused(scene_mix={"hologram": 1.0})

    def test_one_bad_entry_refuses_the_whole_file_and_duplicates_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "n.json"
            path.write_text(json.dumps({"niches": [niche_entry(), niche_entry(cadence_days=1,
                                                                              id="other")]}))
            with self.assertRaises(NicheError):
                load_niches(path)
            path.write_text(json.dumps({"niches": [niche_entry(), niche_entry()]}))
            with self.assertRaises(NicheError):
                load_niches(path)
            path.write_text("not json")
            with self.assertRaises(NicheError):
                load_niches(path)


if __name__ == "__main__":
    unittest.main()
