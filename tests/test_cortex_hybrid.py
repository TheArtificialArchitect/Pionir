"""Hybrid recall: BM25 fused with embeddings.

The headline is test_semantic_finds_what_lexical_cannot - a query with no content
word in common with its target, which lexical structurally misses and the hybrid
must find. Uses a deterministic fake embedder so no Ollama or model is needed.
"""

import unittest

from pionir.cortex import Cortex, NewMemory


class FakeEmbedder:
    """Deterministic hand-placed vectors, so a paraphrase pair can be made near
    in vector space while sharing no words. `table` maps a substring to a vector;
    the first matching entry wins, else a zero-ish default."""

    model = "fake-embed-v1"

    def __init__(self, table: dict[str, list[float]]) -> None:
        self.table = table
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        out = []
        for t in texts:
            low = t.lower()
            vec = next((v for key, v in self.table.items() if key in low), [0.01, 0.01, 0.01])
            out.append(vec)
        return out


class HybridRecallTests(unittest.TestCase):
    def test_semantic_finds_what_lexical_cannot(self) -> None:
        # "app shuts down" and "closing the window puts her to sleep" share no
        # content word; place them near in vector space.
        emb = FakeEmbedder({
            "shuts down": [1.0, 0.0, 0.0],
            "closing the window": [0.98, 0.05, 0.0],
            "weather": [0.0, 0.0, 1.0],
        })
        c = Cortex(":memory:", embedder=emb)
        c.remember("fact", "closing the window puts her to sleep")
        for i in range(10):
            c.remember("thought", f"an idle thought about the weather {i}")
        hits = c.recall("what happens when the app shuts down", k=3)
        self.assertTrue(hits, "hybrid returned nothing")
        self.assertIn("closing the window", hits[0].text)

    def test_falls_back_to_lexical_when_the_embedder_is_dead(self) -> None:
        class Dead:
            model = "dead"
            def embed(self, texts):
                raise RuntimeError("ollama down")
        c = Cortex(":memory:", embedder=Dead())
        c.remember("fact", "Ian is allergic to penicillin")
        hits = c.recall("what is Ian allergic to")   # lexical still works
        self.assertTrue(hits)
        self.assertIn("penicillin", hits[0].text)

    def test_a_none_embedder_reply_falls_back_cleanly(self) -> None:
        class Nones:
            model = "nones"
            def embed(self, texts):
                return None
        c = Cortex(":memory:", embedder=Nones())
        c.remember("fact", "the deploy shipped on Friday")
        self.assertEqual(c.stats()["recall"], "lexical")  # nothing embedded
        self.assertTrue(c.recall("when did the deploy ship"))

    def test_stats_reports_hybrid_only_when_vectors_exist(self) -> None:
        emb = FakeEmbedder({"anything": [1.0, 0.0]})
        c = Cortex(":memory:", embedder=emb)
        self.assertEqual(c.stats()["recall"], "lexical")  # empty store
        c.remember("fact", "anything at all")
        s = c.stats()
        self.assertEqual(s["recall"], "hybrid")
        self.assertEqual(s["embed_model"], "fake-embed-v1")
        self.assertEqual(s["embedded"], 1)

    def test_reindex_backfills_missing_vectors(self) -> None:
        c = Cortex(":memory:")                       # no embedder: stored lexical
        c.remember("fact", "a fact stored before embeddings were on")
        self.assertEqual(c.stats()["embedded"], 0)
        c.embedder = FakeEmbedder({"fact": [1.0, 0.0]})
        self.assertEqual(c.reindex(), 1)             # backfilled
        self.assertEqual(c.stats()["embedded"], 1)

    def test_a_stale_model_vector_is_ignored(self) -> None:
        # Vectors from a different model must not be cosined against the current
        # model's query - one embedding space at a time.
        c = Cortex(":memory:", embedder=FakeEmbedder({"marker": [1.0, 0.0]}))
        c.remember("fact", "the marker sits at the spot")
        c.embedder = FakeEmbedder({"marker": [1.0, 0.0]})
        c.embedder.model = "fake-embed-v2"           # model changed, old vector stale
        self.assertEqual(c.stats()["embedded"], 0)   # no current-model vectors
        # recall still works, lexically
        self.assertTrue(c.recall("where is the marker"))


if __name__ == "__main__":
    unittest.main()
