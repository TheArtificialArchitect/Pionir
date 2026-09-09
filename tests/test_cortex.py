"""Tests for the retrieval memory engine.

The headline test is `test_recalls_across_a_gap_the_window_would_have_lost`: it is
the whole reason this module exists, so it is asserted directly rather than left
implied by the smaller ones.
"""

import unittest

from pionir.cortex import Cortex, NewMemory


def _mem() -> Cortex:
    return Cortex(":memory:")


class WriteAndReadTests(unittest.TestCase):
    def test_remember_returns_an_id_and_get_round_trips(self) -> None:
        c = _mem()
        mid = c.remember("fact", "his sister Jana has an interview Tuesday", meta={"about": "ian"})
        got = c.get(mid)
        self.assertIsNotNone(got)
        self.assertEqual(got.text, "his sister Jana has an interview Tuesday")
        self.assertEqual(got.meta["about"], "ian")

    def test_a_memory_needs_text(self) -> None:
        with self.assertRaises(ValueError):
            _mem().remember("note", "   ")

    def test_kind_sets_a_default_salience_but_a_caller_can_override(self) -> None:
        c = _mem()
        default = c.get(c.remember("fact", "a durable fact"))
        override = c.get(c.remember("fact", "a louder fact", salience=9.0))
        self.assertEqual(default.salience, 6.0)  # KIND_SALIENCE['fact']
        self.assertEqual(override.salience, 9.0)

    def test_batch_write_returns_contiguous_ids(self) -> None:
        c = _mem()
        ids = c.remember_many(
            [NewMemory("note", "one"), NewMemory("note", "two"), NewMemory("note", "three")]
        )
        self.assertEqual(ids, [1, 2, 3])
        self.assertEqual(c.stats()["total"], 3)


class RecallTests(unittest.TestCase):
    def test_recalls_across_a_gap_the_window_would_have_lost(self) -> None:
        # The point of the whole module. Bury one relevant memory under a pile of
        # unrelated ones - the way a real conversation buries an old fact under a
        # hundred newer messages - and confirm it comes back on a query that never
        # shared the window with it.
        c = _mem()
        c.remember("fact", "Ian is allergic to penicillin", namespace="ian")
        for i in range(200):
            c.remember("thought", f"an idle unrelated thought number {i} about the weather")
        hits = c.recall("what medication is he allergic to", k=5)
        self.assertTrue(hits, "recall found nothing where a relevant memory exists")
        self.assertIn("penicillin", hits[0].text)

    def test_an_empty_query_recalls_nothing(self) -> None:
        c = _mem()
        c.remember("fact", "something")
        self.assertEqual(_mem().recall("   "), [])
        self.assertEqual(c.recall(""), [])

    def test_namespace_scopes_recall(self) -> None:
        c = _mem()
        c.remember("fact", "the deploy key lives in the vault", namespace="ops")
        c.remember("fact", "the deploy went out on Friday", namespace="chat")
        only_ops = c.recall("deploy", namespace="ops")
        self.assertTrue(only_ops)
        self.assertTrue(all(m.namespace == "ops" for m in only_ops))

    def test_kinds_filter_recall(self) -> None:
        c = _mem()
        c.remember("fact", "the release shipped clean")
        c.remember("thought", "the release made me nervous")
        only_facts = c.recall("release", kinds=("fact",))
        self.assertTrue(only_facts)
        self.assertTrue(all(m.kind == "fact" for m in only_facts))

    def test_recency_breaks_a_tie_toward_the_newer_memory(self) -> None:
        # Two equally-matching memories; the more recent should rank first.
        clock = {"t": 1_000_000.0}
        c = Cortex(":memory:", now=lambda: clock["t"])
        c.remember("note", "the plan is to ship the widget")
        clock["t"] += 90 * 86400  # 90 days later
        c.remember("note", "the plan is to ship the widget")
        hits = c.recall("plan to ship the widget", k=2)
        self.assertEqual(len(hits), 2)
        self.assertGreater(hits[0].ts, hits[1].ts)

    def test_budget_trims_the_recalled_block_but_never_to_empty(self) -> None:
        c = _mem()
        for i in range(10):
            c.remember("note", "shipping the widget " + "x" * 100 + f" {i}")
        hits = c.recall("shipping the widget", k=10, budget_chars=150)
        self.assertEqual(len(hits), 1)  # one fits the budget; never returns zero on a real match


class ForgetTests(unittest.TestCase):
    def test_forget_removes_from_recall_but_is_recoverable(self) -> None:
        c = _mem()
        mid = c.remember("fact", "an obsolete fact about the widget")
        self.assertTrue(c.recall("widget"))
        self.assertTrue(c.forget(mid))
        self.assertEqual(c.recall("widget"), [])
        # soft delete: the row is still there, just inactive - a one-way hard
        # delete is a door with no way back (HEAD 3.2)
        self.assertFalse(c.forget(mid))  # already inactive

    def test_stats_shows_the_store_is_not_empty(self) -> None:
        c = _mem()
        c.remember("fact", "one", namespace="a")
        c.remember("thought", "two", namespace="b")
        s = c.stats()
        self.assertEqual(s["total"], 2)
        self.assertEqual(s["by_kind"], {"fact": 1, "thought": 1})
        self.assertEqual(s["by_namespace"], {"a": 1, "b": 1})


class LinkTests(unittest.TestCase):
    def test_links_and_slug_round_trip(self) -> None:
        c = _mem()
        mid = c.remember("fact", "Jana is his sister", slug="jana", links=["ian"])
        got = c.get(mid)
        self.assertEqual(got.slug, "jana")
        self.assertEqual(got.links, ("ian",))


if __name__ == "__main__":
    unittest.main()
