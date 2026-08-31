"""Tests for Theo as Pionir's voice.

The endpoint this adapter calls is the whole of its design, so most of these
assert which one it reaches and what it refuses, rather than that a reply comes
back.
"""

import unittest

from pionir.adapters.theo import MAX_CONTENT_CHARS, TheoAdapter, TheoSettings
from pionir.contracts import Task
from pionir.errors import AdapterProtocolError


class FakeTransport:
    """Answers the shape `/voice/chat` actually answers with."""

    def __init__(
        self,
        *,
        voice_enabled: bool = True,
        reply: str = "Hello from Theo",
        conv: str = "conv-created-by-theo",
        capabilities: dict | None = None,
    ) -> None:
        self.voice_enabled = voice_enabled
        self.reply = reply
        self.conv = conv
        self.capabilities = capabilities
        self.calls: list[tuple[str, object]] = []

    def request(self, path: str, *, payload=None):
        self.calls.append((path, payload))
        if path == "/health":
            flags = (
                self.capabilities
                if self.capabilities is not None
                else {"voice_chat": self.voice_enabled}
            )
            return {"ok": True, "capabilities": flags}
        return {
            "ok": True,
            "conv": self.conv,
            "message": {"id": "m1", "role": "assistant", "content": self.reply, "seq": 2},
        }


def _settings() -> TheoSettings:
    return TheoSettings(token="test-token")


class SettingsTests(unittest.TestCase):
    def test_rejects_a_non_loopback_url(self) -> None:
        with self.assertRaises(ValueError):
            TheoSettings(base_url="http://192.168.1.50:8765", token="test")

    def test_requires_a_token(self) -> None:
        with self.assertRaises(ValueError):
            TheoSettings()


class EndpointTests(unittest.TestCase):
    def test_calls_the_voice_endpoint_and_never_the_peer_one(self) -> None:
        # /peer/chat would reach a Theo who is told the speaker is not Ian and
        # who holds no memory of him; /chat/send would hand him tools that reach
        # past Pionir's gates. Which endpoint is called is the design.
        transport = FakeTransport()
        adapter = TheoAdapter(_settings(), transport=transport)
        adapter.execute(Task("conversation.theo_reply", {"content": "What do you think?"}))
        paths = [path for path, _ in transport.calls]
        self.assertIn("/voice/chat", paths)
        self.assertNotIn("/peer/chat", paths)
        self.assertNotIn("/chat/send", paths)

    def test_sends_the_payload_the_source_expects(self) -> None:
        transport = FakeTransport()
        adapter = TheoAdapter(_settings(), transport=transport)
        adapter.execute(
            Task(
                "conversation.theo_reply",
                {"content": "What do you think?", "conversation_id": "existing-thread"},
            )
        )
        self.assertEqual(transport.calls[0], ("/health", None))
        self.assertEqual(
            transport.calls[1],
            ("/voice/chat", {"conv": "existing-thread", "content": "What do you think?"}),
        )

    def test_reads_the_reply_out_of_the_message_record(self) -> None:
        adapter = TheoAdapter(_settings(), transport=FakeTransport(reply="  spaced  "))
        result = adapter.execute(Task("conversation.theo_reply", {"content": "hi"}))
        self.assertEqual(result.agent_id, "theo")
        self.assertEqual(result.output["reply"], "spaced")


class ConversationTests(unittest.TestCase):
    def test_asks_theo_to_open_a_thread_rather_than_inventing_an_id(self) -> None:
        # The source 404s on a conversation id it does not already hold, so an
        # invented one fails every first turn.
        transport = FakeTransport()
        adapter = TheoAdapter(_settings(), transport=transport)
        adapter.execute(Task("conversation.theo_reply", {"content": "hi"}))
        self.assertEqual(transport.calls[1][1]["conv"], "")

    def test_returns_the_thread_theo_opened_so_it_can_be_continued(self) -> None:
        # Continuity across turns is most of why this endpoint exists rather
        # than the peer one; dropping the id would restart him every message.
        adapter = TheoAdapter(_settings(), transport=FakeTransport(conv="conv-42"))
        result = adapter.execute(Task("conversation.theo_reply", {"content": "hi"}))
        self.assertEqual(result.output["conversation_id"], "conv-42")


class HealthTests(unittest.TestCase):
    def test_fails_closed_when_the_voice_path_is_not_attached(self) -> None:
        adapter = TheoAdapter(_settings(), transport=FakeTransport(voice_enabled=False))
        with self.assertRaises(AdapterProtocolError):
            adapter.execute(Task("conversation.theo_reply", {"content": "hello"}))

    def test_probes_voice_chat_and_not_a_neighbouring_capability(self) -> None:
        # `peer` is a different endpoint and `voice` is Piper's text-to-speech.
        # A bridge with both of those up and the voice path detached must fail,
        # or the health check proves something is listening and nothing about
        # the thing being called.
        transport = FakeTransport(capabilities={"peer": True, "voice": True, "chat": True})
        adapter = TheoAdapter(_settings(), transport=transport)
        with self.assertRaises(AdapterProtocolError) as caught:
            adapter.execute(Task("conversation.theo_reply", {"content": "hello"}))
        self.assertIn("voice_chat", str(caught.exception))


class RefusalTests(unittest.TestCase):
    def test_rejects_oversized_content_before_reaching_the_transport(self) -> None:
        transport = FakeTransport()
        adapter = TheoAdapter(_settings(), transport=transport)
        with self.assertRaises(AdapterProtocolError):
            adapter.execute(
                Task("conversation.theo_reply", {"content": "x" * (MAX_CONTENT_CHARS + 1)})
            )
        self.assertEqual(transport.calls, [])

    def test_accepts_content_up_to_the_source_limit(self) -> None:
        adapter = TheoAdapter(_settings(), transport=FakeTransport())
        result = adapter.execute(
            Task("conversation.theo_reply", {"content": "x" * MAX_CONTENT_CHARS})
        )
        self.assertTrue(result.output["reply"])

    def test_an_answer_with_no_message_record_is_a_failure_not_an_empty_reply(self) -> None:
        class NoMessage(FakeTransport):
            def request(self, path: str, *, payload=None):
                document = super().request(path, payload=payload)
                return {"ok": True, "conv": "c"} if path == "/voice/chat" else document

        with self.assertRaises(AdapterProtocolError):
            TheoAdapter(_settings(), transport=NoMessage()).execute(
                Task("conversation.theo_reply", {"content": "hi"})
            )

    def test_an_empty_reply_is_refused_rather_than_passed_on(self) -> None:
        adapter = TheoAdapter(_settings(), transport=FakeTransport(reply="   "))
        with self.assertRaises(AdapterProtocolError):
            adapter.execute(Task("conversation.theo_reply", {"content": "hi"}))


if __name__ == "__main__":
    unittest.main()
