"""A dead or wedged embedder must cost one timeout, not one per call.

2026-09-26: Ollama's scheduler wedged for 75 minutes; every embed waited out its
30 s timeout, so every Pionir task took 30 s and Moss's 20 s business reads all
failed. The embedder now pauses itself (lexical recall only) with a doubling
cooldown, and the first success clears it. No network: the opener is faked.
"""
import io
import json
import unittest

from pionir.cortex import OllamaEmbedder


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Opener:
    """Answers with ``mode``: "timeout" raises, "ok" returns one vector per input."""

    def __init__(self) -> None:
        self.mode = "timeout"
        self.calls = 0

    def open(self, request, timeout=None):
        self.calls += 1
        if self.mode == "timeout":
            raise TimeoutError("timed out")
        n = len(json.loads(request.data.decode("utf-8"))["input"])
        return io.BytesIO(json.dumps({"embeddings": [[0.1, 0.2]] * n}).encode("utf-8"))


class BackoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = _Clock()
        self.emb = OllamaEmbedder(clock=self.clock)
        self.opener = _Opener()
        self.emb._opener = self.opener

    def test_a_timeout_pauses_it_so_later_calls_do_not_wait(self) -> None:
        with self.assertLogs("pionir.cortex", "WARNING"):
            self.assertIsNone(self.emb.embed(["a"]))
        for _ in range(5):
            self.assertIsNone(self.emb.embed(["b"]))
        self.assertEqual(self.opener.calls, 1)          # one timeout, not six
        self.assertEqual(self.emb.paused_for(), OllamaEmbedder.BACKOFF_FIRST_SECONDS)

    def test_the_cooldown_doubles_and_is_capped(self) -> None:
        waits = []
        with self.assertLogs("pionir.cortex", "WARNING"):
            for _ in range(7):
                self.emb.embed(["a"])
                waits.append(self.emb.paused_for())
                self.clock.now += waits[-1]
        self.assertEqual(waits, [60.0, 120.0, 240.0, 480.0, 900.0, 900.0, 900.0])

    def test_after_the_cooldown_it_tries_again_and_a_success_clears_it(self) -> None:
        with self.assertLogs("pionir.cortex", "WARNING"):
            self.emb.embed(["a"])
            self.clock.now += 60
            self.emb.embed(["a"])                        # second failure: 120 s
        self.clock.now += 120
        self.opener.mode = "ok"
        self.assertEqual(self.emb.embed(["a", "b"]), [[0.1, 0.2], [0.1, 0.2]])
        self.assertEqual(self.emb.paused_for(), 0.0)
        self.opener.mode = "timeout"
        with self.assertLogs("pionir.cortex", "WARNING"):
            self.emb.embed(["a"])
        self.assertEqual(self.emb.paused_for(), 60.0)   # backoff restarted, not 240

    def test_a_malformed_answer_is_lexical_but_not_a_pause(self) -> None:
        class Garbage(_Opener):
            def open(self, request, timeout=None):
                self.calls += 1
                return io.BytesIO(b"not json")
        self.emb._opener = Garbage()
        self.assertIsNone(self.emb.embed(["a"]))
        self.assertEqual(self.emb.paused_for(), 0.0)


if __name__ == "__main__":
    unittest.main()
