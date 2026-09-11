"""The Pionir->Bryo pulse feed: snapshot shape, activity delta, atomic write.

Bryo's sensor reads whatever this writes, so the contract is the test: the
numbers are all normalised 0..1 (or a plain count), a poll that can't reach the
server is `reachable: false` rather than a crash, and activity is a delta from
the previous poll, not a running total.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pionir import bryofeed


def _state(free, total, resident, roster):
    return {
        "roster": [{"name": f"cap{i}"} for i in range(roster)],
        "gpu": {
            "observed_free_mb": free,
            "budget": {"total_mb": total},
            "resident": [{"name": f"m{i}"} for i in range(resident)],
        },
    }


class SnapshotTests(unittest.TestCase):
    def test_snapshot_normalises_every_signal(self):
        def fake_get(url, timeout):
            if url.endswith("/api/state"):
                return _state(free=6144, total=12288, resident=1, roster=8)
            return {"events_total": 100}

        with mock.patch.object(bryofeed, "_get_json", side_effect=fake_get):
            snap, events = bryofeed.snapshot("http://x", prev_events_total=None)
        self.assertTrue(snap["reachable"])
        self.assertAlmostEqual(snap["gpu_free_frac"], 0.5, places=3)
        self.assertEqual(snap["gpu_resident"], 1)
        self.assertAlmostEqual(snap["gpu_resident_frac"], 1 / 3, places=3)
        self.assertEqual(snap["roster"], 8)
        # first poll has no previous total, so activity cannot be a delta yet
        self.assertEqual(snap["activity"], 0.0)
        self.assertEqual(events, 100)

    def test_activity_is_a_delta_not_a_total(self):
        def fake_get(url, timeout):
            if url.endswith("/api/state"):
                return _state(free=1000, total=12288, resident=0, roster=8)
            return {"events_total": 112}

        with mock.patch.object(bryofeed, "_get_json", side_effect=fake_get):
            snap, events = bryofeed.snapshot("http://x", prev_events_total=100)
        # 12 new events since last poll -> a positive, bounded activity signal
        self.assertGreater(snap["activity"], 0.0)
        self.assertLessEqual(snap["activity"], 1.0)
        self.assertEqual(events, 112)

    def test_free_vram_unknown_is_null_not_a_guess(self):
        def fake_get(url, timeout):
            if url.endswith("/api/state"):
                return _state(free=None, total=12288, resident=0, roster=1)
            return {"events_total": 0}

        with mock.patch.object(bryofeed, "_get_json", side_effect=fake_get):
            snap, _ = bryofeed.snapshot("http://x", prev_events_total=0)
        self.assertIsNone(snap["gpu_free_frac"])

    def test_unreachable_server_is_reported_not_raised(self):
        with mock.patch.object(bryofeed, "_get_json", return_value=None):
            snap, events = bryofeed.snapshot("http://x", prev_events_total=5)
        self.assertFalse(snap["reachable"])
        self.assertIn("ts", snap)
        # the previous total is carried through, not lost
        self.assertEqual(events, 5)

    def test_write_atomic_leaves_valid_json_and_no_tmp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "pionir.json"
            bryofeed._write_atomic(path, {"ts": 1.0, "reachable": True})
            self.assertEqual(json.loads(path.read_text())["reachable"], True)
            # no leftover .pionir-*.tmp stragglers next to it
            self.assertEqual(list(path.parent.glob(".pionir-*")), [])


if __name__ == "__main__":
    unittest.main()
