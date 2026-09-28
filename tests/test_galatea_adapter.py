"""Tests for Galatea as Pionir's voice.

Her seam is asynchronous - send queues, the reply arrives later on her own
thread and is read by polling - so most of these assert that the adapter waits
for the right bubbles and refuses to mistake a rumination or a fragment for the
answer, rather than that a reply merely comes back.
"""

import json
import secrets
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from pionir.adapters import galatea as galatea_module
from pionir.adapters.galatea import (
    MAX_CONTENT_CHARS,
    GalateaAdapter,
    GalateaSettings,
    resolve_served_model,
)
from pionir.contracts import Task
from pionir.errors import AdapterProtocolError, AdapterUnavailable


class FakeClock:
    """Deterministic monotonic time; sleeping just advances it."""

    def __init__(self) -> None:
        self.t = 0.0

    def sleep(self, seconds: float) -> None:
        self.t += seconds

    def now(self) -> float:
        return self.t


class FakeGalatea:
    """A tiny model of her store: send appends an `ian` turn; her bubbles land
    after a set number of polls, exactly as her background thread would."""

    def __init__(
        self,
        *,
        name: str | None = "Mara",
        model: str = "qwen2.5:7b-instruct",
        bubbles: tuple[dict, ...] = ({"text": "hey you"},),
        appear_after_polls: int = 1,
        accept: bool = True,
    ) -> None:
        self.name = name
        self.model = model
        self._bubbles = bubbles
        self._appear_after = appear_after_polls
        self._accept = accept
        self.messages: list[dict] = []
        self._next_id = 1
        self._polls = 0
        self._released = False
        self.sent: list[str] = []

    def _add(self, role: str, text: str, initiated: bool = False) -> int:
        row = {"id": self._next_id, "role": role, "text": text, "initiated": initiated}
        self.messages.append(row)
        self._next_id += 1
        return row["id"]

    def get(self, path: str):
        if path == "/api/state":
            return {"name": self.name, "naming": self.name is None, "status": "awake"}
        if path == "/api/settings":
            return {"model": self.model, "models": [self.model]}
        if path.startswith("/api/messages"):
            self._polls += 1
            after = 0
            if "after=" in path:
                after = int(path.split("after=", 1)[1].split("&", 1)[0])
            if not self._released and self._polls >= self._appear_after:
                for bubble in self._bubbles:
                    self._add(
                        "her",
                        bubble["text"],
                        initiated=bool(bubble.get("initiated")),
                    )
                self._released = True
            return {"messages": [m for m in self.messages if m["id"] > after]}
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path: str, payload):
        if path == "/api/send":
            self.sent.append(payload["text"])
            if not self._accept:
                return {"error": "she's still choosing a name"}
            return {"ok": True, "id": self._add("ian", payload["text"])}
        raise AssertionError(f"unexpected POST {path}")


def _adapter(fake: FakeGalatea, clock: FakeClock | None = None) -> GalateaAdapter:
    clock = clock or FakeClock()
    return GalateaAdapter(
        GalateaSettings(poll_interval_seconds=1.0, reply_grace_seconds=2.0),
        transport=fake,
        sleep=clock.sleep,
        monotonic=clock.now,
    )


def _task(content: str = "hello") -> Task:
    return Task("conversation.galatea_reply", {"content": content})


class SettingsTests(unittest.TestCase):
    def test_rejects_a_non_loopback_url(self) -> None:
        with self.assertRaises(ValueError):
            GalateaSettings(base_url="http://192.168.1.50:8799")

    def test_construction_does_not_demand_a_key(self) -> None:
        # the key is read per call from her file; with no folder she simply refuses
        self.assertIsNone(GalateaSettings().key_dir)


class _Opener:
    def __init__(self) -> None:
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)

        class _Resp:
            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

            def read(s, n=-1):
                return json.dumps({"ok": True, "id": 7, "messages": []}).encode()

        return _Resp()


class LocalKeyTests(unittest.TestCase):
    """Loopback is not an identity: her server takes nothing unauthenticated, so every
    call is SIGNED with her LOCAL key - which never crosses the wire (a squatter on
    her port while she is down would capture it) - and never uses the glass key."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="pionir-galatea-key-"))
        self.key = secrets.token_urlsafe(32)
        self.glass = secrets.token_urlsafe(32)
        (self.dir / "galatea-local-token.txt").write_text(self.key + "\n", encoding="utf-8")
        (self.dir / "galatea-glass-token.txt").write_text(self.glass, encoding="utf-8")
        self.transport = galatea_module.LoopbackTransport(GalateaSettings(key_dir=self.dir))
        self.opener = _Opener()
        self.transport._opener = self.opener

    def _check(self, request, key: str) -> None:
        method = request.get_method()
        ts, nonce, sig = (request.get_header(h) for h in ("X-galatea-ts", "X-galatea-nonce", "X-galatea-sig"))
        self.assertTrue(ts and nonce and sig, request.header_items())
        self.assertLessEqual(abs(int(ts) - time.time()), 5)
        want = galatea_module.request_sig(key, method, request.selector, ts, nonce, request.data or b"")
        self.assertEqual(sig, want)
        wire = json.dumps(request.header_items()) + request.full_url + (request.data or b"").decode()
        self.assertNotIn(key, wire)
        self.assertNotIn(self.glass, wire)
        self.assertIsNone(request.get_header("X-galatea-token"))

    def test_every_call_is_signed_and_never_carries_a_key(self) -> None:
        self.transport.get("/api/state")
        self.transport.get("/api/messages?after=7")
        self.transport.post("/api/send", {"text": "hi"})
        self.assertEqual(len(self.opener.requests), 3)
        for request in self.opener.requests:
            self._check(request, self.key)
        self.assertEqual(self.opener.requests[1].selector, "/api/messages?after=7")
        self.assertEqual(self.opener.requests[2].get_header("Content-type"), "application/json")
        nonces = {r.get_header("X-galatea-nonce") for r in self.opener.requests}
        self.assertEqual(len(nonces), 3)                               # a fresh nonce every time

    def test_a_key_she_remade_is_picked_up_without_a_restart(self) -> None:
        fresh = secrets.token_urlsafe(32)
        (self.dir / "galatea-local-token.txt").write_text(fresh, encoding="utf-8")
        self.transport.get("/api/state")
        self._check(self.opener.requests[0], fresh)

    def test_no_key_file_or_a_malformed_one_signs_nothing(self) -> None:
        (self.dir / "galatea-local-token.txt").write_text("short", encoding="utf-8")
        self.transport.get("/api/state")
        (self.dir / "galatea-local-token.txt").unlink()
        self.transport.get("/api/state")
        for request in self.opener.requests:
            self.assertIsNone(request.get_header("X-galatea-sig"))

    def test_a_squatter_on_her_port_never_sees_the_key(self) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        seen: list[bytes] = []

        class Squatter(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def _record(self):
                n = int(self.headers.get("Content-Length") or 0)
                seen.append(self.requestline.encode() + bytes(self.headers) + self.rfile.read(n))
                data = json.dumps({"ok": True, "id": 1, "messages": [], "typing": False}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _record

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Squatter)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            transport = galatea_module.LoopbackTransport(GalateaSettings(
                base_url=f"http://127.0.0.1:{httpd.server_address[1]}", key_dir=self.dir))
            transport.get("/api/state")
            transport.get("/api/messages?after=0")
            transport.post("/api/send", {"text": "hello"})
            galatea_module.resolve_served_model(GalateaSettings(
                base_url=f"http://127.0.0.1:{httpd.server_address[1]}", key_dir=self.dir))
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.assertEqual(len(seen), 4)
        for got in seen:
            self.assertIn(b"X-Galatea-Sig", got)
            self.assertNotIn(self.key.encode(), got)
            self.assertNotIn(self.glass.encode(), got)

    def test_the_runtime_points_her_adapter_at_the_owners_secrets(self) -> None:
        from pionir.bootstrap import _galatea_settings
        from pionir.config import PionirSettings
        configured = PionirSettings(state_root=self.dir, galatea_url="http://127.0.0.1:1",
                                    galatea_model_id="stub", embed_model=None)
        self.assertEqual(_galatea_settings(configured).key_dir, configured.client_token_path)


class SendAndReadTests(unittest.TestCase):
    def test_sends_the_message_and_returns_her_reply(self) -> None:
        fake = FakeGalatea(bubbles=({"text": "  hey you  "},))
        result = _adapter(fake).execute(_task("what do you think?"))
        self.assertEqual(fake.sent, ["what do you think?"])
        self.assertEqual(result.agent_id, "galatea")
        self.assertEqual(result.output["reply"], "hey you")

    def test_joins_every_bubble_of_one_turn(self) -> None:
        # She answers in more than one bubble; returning on the first would hand
        # back a fragment and call it her reply.
        fake = FakeGalatea(bubbles=({"text": "one moment"}, {"text": "okay, here"}))
        result = _adapter(fake).execute(_task())
        self.assertEqual(result.output["reply"], "one moment\n\nokay, here")

    def test_an_initiated_thought_is_not_read_as_the_reply(self) -> None:
        # A rumination racing into the window is her speaking first, not an
        # answer to us. Read as the reply it would put words in her mouth.
        fake = FakeGalatea(
            bubbles=(
                {"text": "(wondering about the sea)", "initiated": True},
                {"text": "sorry - yes, I'm here"},
            )
        )
        result = _adapter(fake).execute(_task())
        self.assertEqual(result.output["reply"], "sorry - yes, I'm here")

    def test_ignores_the_echo_of_our_own_message(self) -> None:
        fake = FakeGalatea(bubbles=({"text": "my answer"},))
        result = _adapter(fake).execute(_task("my question"))
        self.assertNotIn("my question", result.output["reply"])
        self.assertEqual(result.output["reply"], "my answer")


class ReadinessTests(unittest.TestCase):
    def test_fails_closed_while_she_is_still_choosing_a_name(self) -> None:
        # Before she has a name /api/send answers 409 and no turn is possible;
        # probing state turns that into an honest up-front unavailability.
        fake = FakeGalatea(name=None)
        with self.assertRaises(AdapterUnavailable):
            _adapter(fake).execute(_task())
        self.assertEqual(fake.sent, [])  # never even sent


class RefusalTests(unittest.TestCase):
    def test_rejects_oversized_content_before_the_transport(self) -> None:
        fake = FakeGalatea()
        with self.assertRaises(AdapterProtocolError):
            _adapter(fake).execute(_task("x" * (MAX_CONTENT_CHARS + 1)))
        self.assertEqual(fake.sent, [])

    def test_rejects_empty_content(self) -> None:
        fake = FakeGalatea()
        with self.assertRaises(AdapterProtocolError):
            _adapter(fake).execute(_task("   "))

    def test_a_refused_send_is_surfaced_not_swallowed(self) -> None:
        fake = FakeGalatea(accept=False)
        with self.assertRaises(AdapterProtocolError):
            _adapter(fake).execute(_task())

    def test_no_reply_before_the_timeout_is_unavailable_not_empty(self) -> None:
        # Bubbles that never appear must strand the turn as unavailable, not
        # return a blank reply that reads as her having nothing to say.
        fake = FakeGalatea(appear_after_polls=10_000)
        with self.assertRaises(AdapterUnavailable):
            _adapter(fake).execute(_task())


class ServedModelTests(unittest.TestCase):
    """resolve_served_model reads the model she actually serves, fail-open.

    The id drives Pionir's VRAM admission discount; hardcoding it meant every
    promotion silently stopped it matching. These patch the module's transport
    so the real function runs end to end without a socket.
    """

    def test_reads_the_model_she_actually_serves(self) -> None:
        fake = FakeGalatea(model="qwen2.5:14b-instruct-q4")
        with mock.patch.object(galatea_module, "LoopbackTransport", lambda _s: fake):
            self.assertEqual(
                resolve_served_model(GalateaSettings()), "qwen2.5:14b-instruct-q4"
            )

    def test_fails_open_when_she_cannot_say(self) -> None:
        class Down:
            def __init__(self, _settings) -> None:
                pass

            def get(self, path: str):
                raise AdapterUnavailable("down")

        with mock.patch.object(galatea_module, "LoopbackTransport", Down):
            self.assertIsNone(resolve_served_model(GalateaSettings()))


if __name__ == "__main__":
    unittest.main()
