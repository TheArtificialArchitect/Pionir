"""Consolidation: folding raw turns into an episode, with an injected distiller."""

import unittest

from pionir.consolidate import Consolidator, Distilled
from pionir.cortex import Cortex


class FakeDistiller:
    def __init__(self, distilled=None):
        self.distilled = distilled
        self.seen = None

    def distill(self, turns):
        self.seen = list(turns)
        return self.distilled


def _seed(c: Cortex, ns: str, n: int) -> None:
    for i in range(n):
        c.remember("message", f"turn number {i} about the deploy plan", namespace=ns)


class ConsolidateTests(unittest.TestCase):
    def test_folds_turns_into_an_episode_and_retires_them(self) -> None:
        c = Cortex(":memory:")
        _seed(c, "chat", 6)
        distilled = Distilled("they discussed the deploy plan and settled on Friday",
                              ("the deploy is set for Friday",))
        con = Consolidator(c, FakeDistiller(distilled))
        outcome = con.consolidate("chat")
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome.folded_turns, 6)
        # the episode and fact exist, the raw turns are gone from recall
        self.assertEqual(len(c.memories("chat", kind="message")), 0)
        self.assertEqual(len(c.memories("chat", kind="episode")), 1)
        self.assertEqual(len(c.memories("chat", kind="fact")), 1)
        ep = c.get(outcome.episode_id)
        self.assertIn("Friday", ep.text)
        self.assertEqual(ep.meta["folded_turns"], 6)

    def test_below_the_threshold_it_leaves_the_turns_alone(self) -> None:
        c = Cortex(":memory:")
        _seed(c, "chat", 3)
        con = Consolidator(c, FakeDistiller(Distilled("x")))
        self.assertIsNone(con.consolidate("chat", min_turns=6))
        self.assertEqual(len(c.memories("chat", kind="message")), 3)  # untouched

    def test_a_declining_distiller_never_loses_the_turns(self) -> None:
        # Model down / nothing usable: the turns must stay for a later pass.
        c = Cortex(":memory:")
        _seed(c, "chat", 8)
        for distiller in (FakeDistiller(None), FakeDistiller(Distilled("   "))):
            self.assertIsNone(Consolidator(c, distiller).consolidate("chat"))
            self.assertEqual(len(c.memories("chat", kind="message")), 8)

    def test_a_raising_distiller_is_fail_open(self) -> None:
        class Boom:
            def distill(self, turns):
                raise RuntimeError("ollama fell over")
        c = Cortex(":memory:")
        _seed(c, "chat", 8)
        self.assertIsNone(Consolidator(c, Boom()).consolidate("chat"))
        self.assertEqual(len(c.memories("chat", kind="message")), 8)

    def test_the_recalled_episode_survives_the_window(self) -> None:
        # The point of it: after folding, a query recalls the episode though the
        # raw turns it came from are gone.
        c = Cortex(":memory:")
        _seed(c, "chat", 6)
        con = Consolidator(c, FakeDistiller(Distilled("they settled the deploy for Friday")))
        con.consolidate("chat")
        hits = c.recall("when is the deploy", namespace="chat")
        self.assertTrue(hits)
        self.assertIn("Friday", hits[0].text)


if __name__ == "__main__":
    unittest.main()
