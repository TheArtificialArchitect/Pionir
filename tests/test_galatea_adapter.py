"""Tests for Galatea as Pionir's voice.

Her seam is asynchronous - send queues, the reply arrives later on her own
thread and is read by polling - so most of these assert that the adapter waits
for the right bubbles and refuses to mistake a rumination or a fragment for the
answer, rather than that a reply merely comes back.
"""

import unittest
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

    def test_needs_no_token_on_loopback(self) -> None:
        # Her server authorises 127.0.0.1 outright, so unlike Theo's bridge this
        # carries no credential at all - and construction must not demand one.
        GalateaSettings()


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
