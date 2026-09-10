"""Tests for the memory recall eval.

The important one is test_the_check_actually_bites: an eval that passes even when
recall is broken proves nothing (HEAD 3.21), so it is asserted directly that a
degraded engine fails the check.
"""

import unittest
from unittest import mock

from pionir import recallcheck
from pionir.cortex import Cortex, NewMemory
from pionir.recallcheck import RecallProbe, run


class RecallCheckTests(unittest.TestCase):
    def test_the_real_engine_passes_the_gated_probes(self) -> None:
        check = run()
        self.assertEqual(check.status, "ok")
        self.assertGreaterEqual(check.gated_recall, recallcheck.MIN_GATED_RECALL)
        self.assertEqual(check.misses, ())

    def test_paraphrase_is_measured_but_never_gates(self) -> None:
        # The lexical engine is expected to do poorly on pure paraphrase; that is
        # the signal for semantic recall, not a failure. Whatever the paraphrase
        # score is, it must not affect passed/failing.
        check = run()
        self.assertIn(check.status, {"ok", "failing"})
        # A paraphrase miss is not in `misses` and does not break the gate.
        self.assertTrue(all(m.category in recallcheck.GATED_CATEGORIES for m in check.misses))

    def test_the_check_actually_bites(self) -> None:
        # Replace recall with one that always returns nothing; every gated probe
        # must miss and the check must fail. If this still passed, the eval would
        # be measuring nothing.
        with mock.patch.object(Cortex, "recall", return_value=[]):
            broken = run()
        self.assertEqual(broken.status, "failing")
        self.assertFalse(broken.passed)
        self.assertEqual(broken.gated_recall, 0.0)
        self.assertTrue(broken.misses)

    def test_a_perfect_run_reports_every_probe(self) -> None:
        check = run()
        self.assertEqual(len(check.results), len(recallcheck.default_probes()))
        self.assertTrue(all(r.rank is not None for r in check.results if r.hit))

    def test_a_custom_probe_scores_recall_at_k(self) -> None:
        probe = RecallProbe(
            name="synthetic exact",
            category="exact",
            corpus=(
                NewMemory("fact", "the launch gate is akmon M12 finality"),
                NewMemory("thought", "unrelated musing about coffee"),
            ),
            query="what is the akmon launch gate",
            expect_contains="M12 finality",
        )
        check = run([probe])
        self.assertTrue(check.passed)
        self.assertEqual(check.results[0].rank, 1)


if __name__ == "__main__":
    unittest.main()
