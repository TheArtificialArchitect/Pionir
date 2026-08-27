import unittest

from pionir.adapters.theo_peer import TheoPeerAdapter, TheoPeerSettings
from pionir.contracts import Task
from pionir.errors import AdapterProtocolError


class FakeTransport:
    def __init__(self, *, peer_enabled: bool = True, reply: str = "Hello from Theo") -> None:
        self.peer_enabled = peer_enabled
        self.reply = reply
        self.calls: list[tuple[str, object]] = []

    def request(self, path: str, *, payload=None):
        self.calls.append((path, payload))
        if path == "/health":
            return {"ok": True, "capabilities": {"peer": self.peer_enabled}}
        return {"ok": True, "reply": self.reply}


class TheoPeerAdapterTests(unittest.TestCase):
    def settings(self) -> TheoPeerSettings:
        return TheoPeerSettings(token="test-token")

    def test_settings_reject_non_loopback_url(self) -> None:
        with self.assertRaises(ValueError):
            TheoPeerSettings(base_url="http://192.168.1.50:8765", token="test")

    def test_settings_require_token(self) -> None:
        with self.assertRaises(ValueError):
            TheoPeerSettings()

    def test_executes_authenticated_conversation_only_contract(self) -> None:
        transport = FakeTransport()
        adapter = TheoPeerAdapter(self.settings(), transport=transport)
        task = Task(
            "conversation.theo_peer_reply",
            {"content": "What do you think?", "conversation_id": "atani:test-1"},
        )
        result = adapter.execute(task)
        self.assertEqual(result.agent_id, "theo-peer")
        self.assertEqual(result.output["reply"], "Hello from Theo")
        self.assertEqual(transport.calls[0], ("/health", None))
        self.assertEqual(
            transport.calls[1],
            (
                "/peer/chat",
                {
                    "peer": "Atani",
                    "conversation": "atani:test-1",
                    "content": "What do you think?",
                },
            ),
        )

    def test_fails_closed_when_peer_capability_is_missing(self) -> None:
        adapter = TheoPeerAdapter(self.settings(), transport=FakeTransport(peer_enabled=False))
        with self.assertRaises(AdapterProtocolError):
            adapter.execute(Task("conversation.theo_peer_reply", {"content": "hello"}))

    def test_rejects_oversized_content_before_transport(self) -> None:
        transport = FakeTransport()
        adapter = TheoPeerAdapter(self.settings(), transport=transport)
        with self.assertRaises(AdapterProtocolError):
            adapter.execute(
                Task("conversation.theo_peer_reply", {"content": "x" * 8_001})
            )
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
