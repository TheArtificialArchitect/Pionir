"""The Ollama gate: Peter's model calls under Pionir's GPU arbitration.

Nothing here reaches a live model, a live Ollama or a live port: the card is a fake, the
"Ollama" behind the gate is a stand-in on an ephemeral port, and the gate itself binds an
ephemeral port. Each test names the behaviour it pins; reverting it fails the test:

- the voice's model is sidelined only after she SAYS she stood down - never mid-turn;
- when the lease cannot be had, the call runs on the CPU on purpose (num_gpu = 0), with
  the reason carried back - never a silent spill;
- the lease lingers across a burst and its release hands the card back (unload the 7B,
  re-warm her model);
- the gate forwards Ollama only: never another host through the proxy, never pull/delete,
  never another model.
"""

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pionir.errors import AdapterUnavailable, ResourceUnavailable
from pionir.ollama_gate import (
    PETER_MODEL,
    guarded_evictor,
    GpuArbiter,
    OllamaGate,
    on_cpu,
    peter_requirement,
    refusal,
    target_path,
    voice_probe,
)
from pionir.scheduler import ModelLeaseScheduler, ResourceBudget
from pionir.shared_gpu import SharedGpuLock

VOICE = "gemma3:12b"


class _Card:
    SIZES = {VOICE: 7_800, PETER_MODEL: 4_900}

    def __init__(self, resident: list[str]) -> None:
        self.resident = list(resident)
        self.unloaded: list[str] = []
        self.warmed: list[str] = []

    def free(self) -> int:
        return 10_450 - sum(self.SIZES.get(m, 0) for m in self.resident)

    def loaded(self) -> list[str]:
        return list(self.resident)

    def unload(self, name: str) -> None:
        self.unloaded.append(name)
        if name in self.resident:
            self.resident.remove(name)

    def warm(self, name: str) -> None:
        self.warmed.append(name)
        if name not in self.resident:
            self.resident.append(name)


def _scheduler(card: _Card, lock: SharedGpuLock | None) -> ModelLeaseScheduler:
    return ModelLeaseScheduler(
        ResourceBudget(), lock, vram_probe=card.free, residency_probe=lambda m: False,
        evict_to_fit=True, evictor=card.unload, loaded_probe=card.loaded,
        protected_models=[VOICE], rewarmer=card.warm, background=lambda work: work(),
        sleep=lambda _s: None,
    )


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def _arbiter(card, lock, voice, clock=None, **kw) -> GpuArbiter:
    clock = clock or _Clock()
    return GpuArbiter(_scheduler(card, lock), voice_idle=voice, clock=clock, sleep=clock.sleep,
                      confirm_seconds=kw.pop("confirm_seconds", 5.0),
                      linger_seconds=kw.pop("linger_seconds", 30.0), **kw)


class PlacementTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.lock = SharedGpuLock(Path(self._tmp.name) / "gpu.lock")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_the_voice_is_sidelined_only_after_she_says_she_stood_down(self) -> None:
        card = _Card([VOICE])
        answers = iter([False, False, True])      # mid-turn, mid-turn, then yielding
        asked: list[bool] = []

        def voice():
            asked.append(self.lock.holder() is not None)   # the lease is held while we ask
            return next(answers)

        arbiter = _arbiter(card, self.lock, voice)
        placement = arbiter.place(PETER_MODEL)
        self.assertEqual(placement.where, "gpu")
        self.assertEqual(card.unloaded, [VOICE])
        self.assertEqual(asked, [True, True, True])
        self.assertEqual(self.lock.holder()["purpose"], f"peter: {PETER_MODEL}")
        arbiter.close()

    def test_a_voice_that_never_stands_down_keeps_her_model_and_peter_runs_on_the_cpu(self) -> None:
        card = _Card([VOICE])
        arbiter = _arbiter(card, self.lock, lambda: False)
        placement = arbiter.place(PETER_MODEL)
        self.assertEqual(placement.where, "cpu")
        self.assertIn("has not stood down", placement.reason)
        self.assertEqual(card.unloaded, [])                  # nothing of hers was touched
        self.assertFalse(self._held())                        # and the lease was let go
        self.assertEqual(arbiter.counts["cpu"], 1)
        self.assertEqual(arbiter.recent[0]["event"], "gpu.cpu")

    def _held(self) -> bool:
        probe = SharedGpuLock(self.lock.path).try_acquire(owner="test", purpose="probe")
        if probe is None:
            return True
        probe.release()
        return False

    def test_an_unreadable_voice_is_not_an_idle_one(self) -> None:
        card = _Card([VOICE])

        def broken():
            raise RuntimeError("state unreadable")

        with self.assertLogs("pionir.ollama_gate", "WARNING"):
            self.assertEqual(_arbiter(card, self.lock, broken).place(PETER_MODEL).where, "cpu")
        self.assertEqual(_arbiter(card, self.lock, lambda: None).place(PETER_MODEL).where, "cpu")
        self.assertEqual(card.unloaded, [])

    def test_a_card_held_by_another_tenant_means_cpu_on_purpose_not_a_wait_past_peter(self) -> None:
        card = _Card([VOICE])
        other = SharedGpuLock(self.lock.path).try_acquire(owner="pionir", purpose="daedalus: coder")
        try:
            placement = _arbiter(card, self.lock, lambda: True).place(PETER_MODEL)
        finally:
            other.release()
        self.assertEqual(placement.where, "cpu")
        self.assertIn("leased by another", placement.reason)
        self.assertEqual(card.unloaded, [])

    def test_the_lease_lingers_across_a_burst_and_its_release_hands_the_card_back(self) -> None:
        card = _Card([VOICE])
        clock = _Clock()
        arbiter = _arbiter(card, self.lock, lambda: True, clock=clock, linger_seconds=30.0)
        self.assertEqual(arbiter.place(PETER_MODEL).where, "gpu")
        card.resident.append(PETER_MODEL)          # Ollama loads the 7B on the first call
        for _ in range(5):                         # the rest of the burst: no new swap
            clock.t += 5
            self.assertEqual(arbiter.place(PETER_MODEL).reason, "lease held for this burst")
            arbiter.done()
            arbiter.reap()
        self.assertEqual(arbiter.counts["leases"], 1)
        self.assertEqual(card.unloaded, [VOICE])
        clock.t += 31
        arbiter.reap()
        self.assertFalse(self._held())
        self.assertEqual(card.unloaded, [VOICE, PETER_MODEL])   # the 7B goes...
        self.assertEqual(card.warmed, [VOICE])                  # ...and her model comes back

    def test_cpu_mode_never_touches_the_card(self) -> None:
        card = _Card([VOICE])
        arbiter = _arbiter(card, self.lock, lambda: True, mode="cpu")
        placement = arbiter.place(PETER_MODEL)
        self.assertEqual((placement.where, card.unloaded), ("cpu", []))
        self.assertFalse(self._held())

    def test_peter_fits_the_budget_and_needs_the_card_to_himself_on_this_card(self) -> None:
        requirement = peter_requirement(PETER_MODEL)
        self.assertTrue(requirement.requires_gpu and requirement.exclusive_card)
        self.assertLessEqual(requirement.total_vram_mb, ResourceBudget().usable_vram_mb)
        # beside the voice's model it does not fit: that is why it must be arbitrated
        self.assertGreater(requirement.total_vram_mb, _Card([VOICE]).free())


class SchedulerSettleTests(unittest.TestCase):
    def test_settle_false_refuses_and_lets_the_lease_go(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = SharedGpuLock(Path(tmp) / "gpu.lock")
            card = _Card([VOICE])
            with self.assertRaises(ResourceUnavailable):
                _scheduler(card, lock).acquire(peter_requirement(PETER_MODEL), settle=lambda: False)
            self.assertEqual(card.unloaded, [])
            again = lock.try_acquire(owner="test", purpose="after")
            self.assertIsNotNone(again)
            again.release()


class FilterTests(unittest.TestCase):
    UP = ("127.0.0.1", 11500)     # a stand-in upstream port, never the live daemon

    def test_only_the_upstream_ollama_is_served_through_the_proxy(self) -> None:
        self.assertEqual(target_path("/api/generate", self.UP), "/api/generate")
        self.assertEqual(target_path("http://127.0.0.1:11500/api/generate", self.UP), "/api/generate")
        self.assertEqual(target_path("http://localhost:11500/api/tags", self.UP), "/api/tags")
        for other in ("http://feeds.bbci.co.uk/news/rss.xml", "http://127.0.0.1:8780/api/task",
                      "http://127.0.0.1:8771/jobs", "http://10.0.0.1:11500/api/generate",
                      "https://127.0.0.1:11500/api/generate"):
            self.assertIsNone(target_path(other, self.UP), other)

    def test_the_night_builds_filter_holds_for_many_models(self) -> None:
        models = frozenset({PETER_MODEL})
        body = json.dumps({"model": PETER_MODEL, "prompt": "x"}).encode()
        self.assertIsNone(refusal("POST", "/api/generate", body, models))
        self.assertIsNone(refusal("GET", "/api/tags", b"", models))
        self.assertIn("not allowed", refusal("POST", "/api/pull", body, models))
        self.assertIn("not allowed", refusal("POST", "/api/delete", body, models))
        self.assertIn("not allowed", refusal("GET", "/api/blobs/x", b"", models))
        other = json.dumps({"model": VOICE, "prompt": "x"}).encode()
        self.assertIn("may be used", refusal("POST", "/api/generate", other, models))
        self.assertIn("not JSON", refusal("POST", "/api/generate", b"{", models))

    def test_cpu_placement_keeps_the_callers_options_and_sets_num_gpu_zero(self) -> None:
        body = json.dumps({"model": PETER_MODEL, "options": {"temperature": 0}}).encode()
        doc = json.loads(on_cpu(body))
        self.assertEqual(doc["options"], {"temperature": 0, "num_gpu": 0})


class _FakeOllama(BaseHTTPRequestHandler):
    seen: list = []

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        type(self).seen.append((self.path, json.loads(body)))
        data = json.dumps({"response": '{"ok": 1}'}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_a) -> None:
        pass


class HttpTests(unittest.TestCase):
    """The proxy as Peter's urllib sees it: HTTP_PROXY pointed at the gate."""

    def setUp(self) -> None:
        _FakeOllama.seen = []
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOllama)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.up = f"http://127.0.0.1:{self.upstream.server_address[1]}"
        self._tmp = tempfile.TemporaryDirectory()
        self.card = _Card([VOICE])
        self.voice = {"idle": False}
        self.arbiter = _arbiter(self.card, SharedGpuLock(Path(self._tmp.name) / "gpu.lock"),
                                lambda: self.voice["idle"], confirm_seconds=0.0)
        self.gate = OllamaGate([PETER_MODEL], upstream=self.up, port=0, arbiter=self.arbiter,
                               reap_seconds=3600)
        self.gate.start()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": self.gate.url}))

    def tearDown(self) -> None:
        self.gate.stop()
        self.upstream.shutdown()
        self.upstream.server_close()
        self._tmp.cleanup()

    def _generate(self, model: str = PETER_MODEL):
        req = urllib.request.Request(f"{self.up}/api/generate", method="POST",
                                     data=json.dumps({"model": model, "prompt": "p",
                                                      "options": {"temperature": 0}}).encode(),
                                     headers={"Content-Type": "application/json"})
        return self.opener.open(req, timeout=10)

    def test_a_mid_turn_voice_sends_peters_call_to_the_cpu_and_says_so(self) -> None:
        with self._generate() as resp:
            self.assertEqual(json.loads(resp.read())["response"], '{"ok": 1}')
            self.assertTrue(resp.headers["X-Pionir-Placement"].startswith("cpu;"))
        path, doc = _FakeOllama.seen[-1]
        self.assertEqual((path, doc["options"]["num_gpu"]), ("/api/generate", 0))
        self.assertEqual(self.card.unloaded, [])

    def test_a_yielding_voice_gives_peter_the_card(self) -> None:
        self.voice["idle"] = True
        with self._generate() as resp:
            self.assertTrue(resp.headers["X-Pionir-Placement"].startswith("gpu;"))
        self.assertNotIn("num_gpu", _FakeOllama.seen[-1][1]["options"])
        self.assertEqual(self.card.unloaded, [VOICE])

    def test_another_model_or_another_host_through_the_proxy_is_refused(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._generate(VOICE)
        self.assertEqual(caught.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.opener.open("http://example.invalid/feed.xml", timeout=10)
        self.assertEqual(caught.exception.code, 403)
        self.assertEqual(_FakeOllama.seen, [])
        self.assertEqual(self.arbiter.counts["refused"], 2)

    def test_the_gate_reports_its_placements(self) -> None:
        self._generate().close()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"{self.gate.url}/pionir/gate", timeout=10) as resp:
            doc = json.loads(resp.read())
        self.assertEqual(doc["models"], [PETER_MODEL])
        self.assertEqual(doc["arbiter"]["counts"]["cpu"], 1)


class VoiceProbeTests(unittest.TestCase):
    class _Adapter:
        def __init__(self, answer):
            self.answer = answer

        def status(self):
            if isinstance(self.answer, BaseException):
                raise self.answer
            return self.answer

    def _refused(self) -> AdapterUnavailable:
        error = AdapterUnavailable("down")
        error.__cause__ = urllib.error.URLError(ConnectionRefusedError())
        return error

    def test_yielding_absent_busy_and_unknown(self) -> None:
        probe = lambda answer: voice_probe(self._Adapter(answer))()  # noqa: E731
        self.assertIs(probe({"gpu": {"yielding": True, "in_flight": 0}}), True)
        self.assertIs(probe({"gpu": {"yielding": False, "in_flight": 0}}), False)
        # yielding is set at the top of a tick that can still go on into a model call:
        # while one is on the wire she is NOT idle, and a Galatea that cannot say is unknown
        self.assertIs(probe({"gpu": {"yielding": True, "in_flight": 1}}), False)
        self.assertIsNone(probe({"gpu": {"yielding": True}}))
        self.assertIsNone(probe({"gpu": {"yielding": True, "in_flight": True}}))
        self.assertIs(probe(self._refused()), True)                     # not running at all
        self.assertIsNone(probe(AdapterUnavailable("timed out")))        # up but unreadable
        self.assertIsNone(probe({"status": "awake"}))                    # no gpu field
        self.assertIs(voice_probe(None)(), True)                         # no voice wired


if __name__ == "__main__":
    unittest.main()


class RecheckBeforeUnloadTests(unittest.TestCase):
    """Her model goes only if she is idle AT THE MOMENT of the unload: a turn she started
    after saying she yielded keeps it, and Peter goes to the CPU."""

    def test_a_voice_busy_again_at_unload_time_keeps_her_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            card = _Card([VOICE])
            answers = iter([True, False])           # idle when the lease settles, busy at unload
            voice = lambda: next(answers)           # noqa: E731
            scheduler = _scheduler(card, SharedGpuLock(Path(tmp) / "gpu.lock"))
            scheduler.evictor = guarded_evictor(card.unload, voice, [VOICE])
            clock = _Clock()
            arbiter = GpuArbiter(scheduler, voice_idle=voice, clock=clock, sleep=clock.sleep,
                                 confirm_seconds=5.0)
            placement = arbiter.place(PETER_MODEL)
            self.assertEqual(placement.where, "cpu")
            self.assertEqual(card.unloaded, [])
            self.assertIn(VOICE, card.resident)

    def test_other_models_are_not_held_hostage_by_the_guard(self) -> None:
        card = _Card(["some-model:latest"])
        guarded_evictor(card.unload, lambda: False, [VOICE])("some-model:latest")
        self.assertEqual(card.unloaded, ["some-model:latest"])


class _SlowOllama(BaseHTTPRequestHandler):
    """Answers only when released; records every request that reached it and whether the
    gate hung up on it."""
    started: list = []
    hung_up: list = []
    release = threading.Event()

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        type(self).started.append(body.get("prompt"))
        import select as _select
        while not type(self).release.wait(0.05):
            r, _, _ = _select.select([self.connection], [], [], 0)
            if r and self.connection.recv(1, __import__("socket").MSG_PEEK) == b"":
                type(self).hung_up.append(body.get("prompt"))
                return
        data = json.dumps({"response": "ok"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_a) -> None:
        pass


class AbandonedCallTests(unittest.TestCase):
    """Peter gives up at 120 s. The gate must not keep his abandoned calls alive: the
    upstream call is closed when he hangs up or his budget runs out, and a queued call
    whose caller has gone is dropped before it ever reaches Ollama."""

    def setUp(self) -> None:
        _SlowOllama.started, _SlowOllama.hung_up = [], []
        _SlowOllama.release = threading.Event()
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), _SlowOllama)
        self.upstream.daemon_threads = True
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.up = f"http://127.0.0.1:{self.upstream.server_address[1]}"

    def tearDown(self) -> None:
        _SlowOllama.release.set()
        self.gate.stop()
        self.upstream.shutdown()
        self.upstream.server_close()

    def _gate(self, budget: float) -> OllamaGate:
        self.gate = OllamaGate([PETER_MODEL], upstream=self.up, port=0, caller_budget=budget,
                               watch_seconds=0.05)
        self.gate.start()
        return self.gate

    def _call(self, prompt: str, timeout: float):
        import socket as _socket
        body = json.dumps({"model": PETER_MODEL, "prompt": prompt}).encode()
        sock = _socket.create_connection(("127.0.0.1", self.gate.port), timeout=timeout)
        sock.sendall(b"POST /api/generate HTTP/1.1\r\nHost: x\r\n"
                     b"Content-Type: application/json\r\nContent-Length: " + str(len(body)).encode()
                     + b"\r\n\r\n" + body)
        return sock

    def _wait(self, cond, seconds: float = 5.0) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if cond():
                return True
            time.sleep(0.02)
        return cond()

    def test_a_caller_that_hangs_up_is_aborted_upstream_and_the_queue_moves(self) -> None:
        self._gate(budget=30)
        first = self._call("first", 5)
        self.assertTrue(self._wait(lambda: _SlowOllama.started == ["first"]))
        queued = self._call("queued", 5)                  # waits its turn behind "first"
        time.sleep(0.2)
        queued.close()                                    # ...and gives up while queued
        # dropped from the queue at once - not left waiting behind "first" for its turn
        self.assertTrue(self._wait(lambda: self.gate.abandoned == 1, 2.0), self.gate.abandoned)
        self.assertEqual(_SlowOllama.hung_up, [])
        first.close()                                     # the first caller gives up too
        self.assertTrue(self._wait(lambda: _SlowOllama.hung_up == ["first"]), _SlowOllama.hung_up)
        self.assertTrue(self._wait(lambda: self.gate.abandoned == 2), self.gate.abandoned)
        # the abandoned queued call never reached Ollama, and the gate is free again
        _SlowOllama.release.set()
        live = self._call("live", 5)
        self.assertIn(b"200", live.recv(64))
        live.close()
        self.assertEqual(_SlowOllama.started, ["first", "live"])

    def test_the_upstream_call_gets_no_more_than_the_callers_budget(self) -> None:
        self._gate(budget=0.6)
        t0 = time.monotonic()
        caller = self._call("slow", 10)
        reply = caller.recv(256)
        took = time.monotonic() - t0
        caller.close()
        self.assertIn(b"504", reply)
        self.assertLess(took, 3.0)                        # not the 900 s it used to hold
        self.assertTrue(self._wait(lambda: _SlowOllama.hung_up == ["slow"]))
