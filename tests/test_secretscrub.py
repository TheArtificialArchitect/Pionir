"""The Library's scrubber, and its lock-free ranked search.

* secretscrub.py runs the SAME vectors as Pionir Desktop's scrub.ts - both read them from
  the one pattern file (secret_patterns.json here, shared/secret-patterns.json there,
  byte-identical; compared when the desktop repo is on disk: PIONIR_DESKTOP_ROOT, else
  C:\\src\\pionir-desktop).
* Every Library answer is scrubbed server-side, this server's client tokens included.
* An owner's ranked search never takes the live cortex lock, and never waits on a slow
  embedder longer than its short timeout (then it is BM25).
"""

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from pionir import library, secretscrub
from pionir.cortex import Cortex

R = "\u00abredacted\u00bb"


class SharedVectors(unittest.TestCase):
    def test_every_redact_vector(self) -> None:
        for given, want in secretscrub.pattern_doc()["vectors"]["redact"]:
            self.assertEqual(secretscrub.scrub_patterns(given), want, given)

    def test_every_keep_vector_is_left_alone(self) -> None:
        for given in secretscrub.pattern_doc()["vectors"]["keep"]:
            self.assertEqual(secretscrub.scrub_patterns(given), given, given)

    def test_every_structured_vector(self) -> None:
        for given, want in secretscrub.pattern_doc()["vectors"]["structured"]:
            self.assertEqual(secretscrub.scrub_value(given), want, given)

    def test_the_two_copies_are_one(self) -> None:
        root = Path(os.environ.get("PIONIR_DESKTOP_ROOT", r"C:\src\pionir-desktop"))
        theirs = root / "shared" / "secret-patterns.json"
        if not theirs.is_file():
            self.skipTest(f"the desktop's copy is not on this disk ({theirs})")
        self.assertEqual(theirs.read_bytes(), secretscrub.PATTERN_FILE.read_bytes())

    def test_known_values_and_deep_values(self) -> None:
        token = "Pionir-client-token-" + "Zq9" * 9
        got = secretscrub.scrub_value({"a": [f"x {token} y"], f"k{token}": 1, "n": 5}, [token])
        self.assertEqual(got, {"a": [f"x {R} y"], f"k{R}": 1, "n": 5})

    def test_the_heuristic_on_its_own(self) -> None:
        self.assertTrue(secretscrub.looks_like_token("uA9qQarqQDPEqsokeKXQhZy8SFSqYr88sTuN1fIg6KU"))
        for word in ("GetUserProfileSettingsForAccount2024", "a" * 40, "ae43406d1812a35abbbca96f1122cca4",
                     "/energy/articles/chevron-exxonmobil-agree-Potential-Crude-151710400"):
            self.assertFalse(secretscrub.looks_like_token(word), word)


class _SlowEmbedder:
    model = "slow-embed"

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    def embed(self, texts):
        time.sleep(self.seconds)
        return [[1.0, 0.0] for _ in texts]


class _MeaningEmbedder:
    """Maps a text to a vector by meaning the test decides: anything about 'ocean' or
    'sea' points one way, everything else another."""
    model = "meaning-embed"

    def embed(self, texts):
        return [[1.0, 0.0] if ("ocean" in t or "sea" in t) else [0.0, 1.0] for t in texts]


class LockFreeRankedSearch(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "memory.db"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_ranked_search_runs_while_the_live_lock_is_held(self) -> None:
        cortex = Cortex(self.path)
        cortex.record_lesson("fiverr ack failed: send the numeric id")
        cortex.record_lesson("scrooge refused a call")
        held, release = threading.Event(), threading.Event()

        def hold() -> None:
            with cortex._lock:                       # a bot mid-write, say
                held.set()
                release.wait(10)

        threading.Thread(target=hold, daemon=True).start()
        held.wait(5)
        try:
            started = time.monotonic()
            out = library.entries(self.path, {"q": ["fiverr ack"]})
            self.assertLess(time.monotonic() - started, 3.0)
            self.assertEqual(out["mode"], "recall")
            self.assertIn("fiverr", out["entries"][0]["preview"])
            ov = library.overview(self.path, cortex.embedder)
            self.assertEqual(ov["total"], 2)
        finally:
            release.set()
            cortex.close()

    def test_a_slow_embedder_costs_the_short_timeout_then_it_is_bm25(self) -> None:
        cortex = Cortex(self.path)
        cortex.record_lesson("fiverr ack failed")
        cortex.close()
        started = time.monotonic()
        out = library.entries(self.path, {"q": ["fiverr"]}, _SlowEmbedder(5.0), embed_timeout=0.2)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(len(out["entries"]), 1)
        self.assertIsNone(library.query_vector(_SlowEmbedder(5.0), "x", timeout=0.1))

        class Paused(_SlowEmbedder):
            def paused_for(self):
                return 30.0

        started = time.monotonic()
        self.assertIsNone(library.query_vector(Paused(5.0), "x", timeout=3.0))
        self.assertLess(time.monotonic() - started, 0.5)

    def test_with_an_embedder_it_ranks_by_meaning_too_like_the_bots_recall(self) -> None:
        emb = _MeaningEmbedder()
        cortex = Cortex(self.path, embedder=emb)
        cortex.remember("fact", "the tide came in over the harbour wall - sea level", namespace="n")
        cortex.remember("fact", "fiverr ack failed", namespace="n")
        want = [m.id for m in cortex.recall("ocean waves", k=5, namespace="n")]
        cortex.close()
        with library._reading(self.path) as db:
            got = [row["id"] for _, row in library.ranked(db, "ocean waves", 5, namespace="n",
                                                            kind=None, embedder=emb)]
        self.assertEqual(got, want)
        self.assertTrue(got)                          # no shared word: only meaning found it
        self.assertEqual(library.overview(self.path, emb)["recall"], "hybrid")


class ScrubbedAnswers(unittest.TestCase):
    def test_every_answer_is_scrubbed_with_this_servers_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memory.db"
            token = "Q" + "w3E" * 14
            cortex = Cortex(path)
            mid = cortex.record_lesson(f"a call failed with {token} and sk-ant-api03-{'A1b2' * 6} "
                                       "and aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                                       meta={"password": "hunter2-Correct"})
            cortex.close()
            store = lambda: (path, None)  # noqa: E731
            for p, q in (("/api/library/entries", ""), ("/api/library/entries", "mode=text&q=failed"),
                         ("/api/library/entries", "q=call+failed"), ("/api/library/entry", f"id={mid}")):
                status, doc = library.serve(p, q, client="desktop", refused=None, store=store,
                                            known=lambda: [token])
                self.assertEqual(status, 200, doc)
                text = repr(doc)
                self.assertIn(R, text)
                for leak in (token, "sk-ant-api03", "wJalrXUtnFEMI", "hunter2-Correct"):
                    self.assertNotIn(leak, text, p)


if __name__ == "__main__":
    unittest.main()
