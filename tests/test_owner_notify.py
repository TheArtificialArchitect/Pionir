"""owner.notify: Moss's daily brief and rare alert reach the owner as ONE plain Discord
message - never an approval, never a ping, never more than the limits allow.

Discord is faked twice: in memory at the HTTP opener (so the real REST client - auth
header, error handling, token scrubbing - is under test), and once as a real loopback
HTTP server reached through urllib's own default opener. Nothing reaches real Discord.
Each test names what it would catch if its behaviour were reverted.
"""
from __future__ import annotations

import io
import json
import logging
import tempfile
import threading
import unittest
import urllib.error
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar, Self

from pionir.adapters.owner import (
    ALERTS_PER_DAY,
    BRIEFS_PER_DAY,
    NOTIFY,
    OwnerNotifyAdapter,
    OwnerNotifySettings,
    neutralise_mentions,
)
from pionir.bootstrap import build_runtime
from pionir.config import PionirSettings
from pionir.contracts import RiskLevel, Task
from pionir.discord_gate import DiscordGateSettings
from pionir.errors import AdapterProtocolError, AdapterUnavailable
from pionir.server import PionirApp

API = "https://discord.test/api/v10"
# A bot-token-shaped fake, built so no scanner mistakes it for a real one.
TOKEN = "MTk4NjIy" + "NDgzNDcx" + ".owner-notify-fake." + "Zz9_q"
CHANNEL = "444444444444444444"
BODY = "Posting has no goal set. Contracts: 3 orders in progress (read 12 minutes ago)."


class _Response:
    def __init__(self, payload: Any) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class FakeDiscord:
    """Just enough of POST /channels/<id>/messages, in memory, checking the token."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None, str | None]] = []
        self.down = False
        self.status: int | None = None
        self._next = 1_400_000_000_000_000_000

    def __call__(self, request: Any, timeout: float | None = None) -> _Response:
        method = request.get_method()
        url = request.full_url
        assert url.startswith(API), url
        body = json.loads(request.data) if request.data else None
        self.calls.append((method, url[len(API):], body, request.get_header("Authorization")))
        if self.down:
            raise urllib.error.URLError(f"connection refused (Bot {TOKEN})")
        status = self.status
        if status is None and request.get_header("Authorization") != f"Bot {TOKEN}":
            status = 401
        if status is not None:
            raw = json.dumps({"message": f"{status}: no (Bot {TOKEN})", "code": 0}).encode()
            raise urllib.error.HTTPError(url, status, "error", {}, io.BytesIO(raw))  # type: ignore[arg-type]
        self._next += 1
        return _Response({"id": str(self._next), "channel_id": CHANNEL,
                          "content": (body or {}).get("content")})

    def posts(self) -> list[dict[str, Any]]:
        return [body for method, path, body, _auth in self.calls
                if method == "POST" and path == f"/channels/{CHANNEL}/messages" and body]


class Clock:
    def __init__(self) -> None:
        self.at = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.at


def _note(kind: str = "brief", **over: Any) -> dict[str, Any]:
    note = {"title": "Tonight's brief", "body_text": BODY, "kind": kind}
    note.update(over)
    return note


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.token_file = self.root / "secrets" / "discord-bot-token.txt"
        self.token_file.parent.mkdir(parents=True)
        self.token_file.write_text(TOKEN + "\n", encoding="utf-8")
        self.discord = FakeDiscord()
        self.clock = Clock()

    def settings(self, **over: Any) -> OwnerNotifySettings:
        values: dict[str, Any] = {"state_dir": self.root / "discord", "channel_id": CHANNEL,
                                  "token_file": self.token_file, "api_base": API}
        values.update(over)
        return OwnerNotifySettings(**values)

    def adapter(self, **over: Any) -> OwnerNotifyAdapter:
        return OwnerNotifyAdapter(self.settings(**over), opener=self.discord, clock=self.clock)

    @staticmethod
    def run_note(adapter: OwnerNotifyAdapter, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(adapter.execute(Task(NOTIFY, payload)).output)


class ManifestTests(Base):
    def test_a_reversible_write_that_never_parks_and_is_never_routed(self) -> None:
        """Reverted to privileged/approval: every brief would wait on the owner's yes."""
        (cap,) = self.adapter().manifest.capabilities
        self.assertEqual(cap.name, NOTIFY)
        self.assertIs(cap.risk, RiskLevel.REVERSIBLE_WRITE)
        self.assertFalse(cap.requires_approval)
        self.assertFalse(cap.spends_money)
        self.assertFalse(cap.routable)
        self.assertEqual(self.adapter().manifest.agent_id, "owner")


class ValidationTests(Base):
    BAD: ClassVar[dict[str, tuple[dict[str, Any], str]]] = {
        "short title": (_note(title="Hey"), "title is 5-80"),
        "long title": (_note(title="x" * 81), "title is 5-80"),
        "two-line title": (_note(title="line one\nline two"), "single line"),
        "short body": (_note(body_text="too short"), "body_text is 20-1800"),
        "long body": (_note(body_text="y" * 1801), "body_text is 20-1800"),
        "control char": (_note(body_text=BODY + "\x07"), "control characters"),
        "bad kind": (_note(kind="call"), "kind is 'brief' or 'alert'"),
        "no kind": ({"title": "Tonight's brief", "body_text": BODY}, "kind is"),
        "extra field": ({**_note(), "to": "everyone"}, "unknown field"),
        "body not text": (_note(body_text=["a"] * 30), "body_text is a string"),
    }

    def test_a_malformed_note_is_refused_before_anything_is_sent(self) -> None:
        adapter = self.adapter()
        for name, (payload, why) in self.BAD.items():
            with self.subTest(name):
                with self.assertRaises(AdapterProtocolError) as caught:
                    adapter.validate(Task(NOTIFY, payload))
                self.assertIn(why, str(caught.exception))
                self.assertTrue(str(caught.exception).startswith("owner.notify: "))
                with self.assertRaises(AdapterProtocolError):
                    adapter.execute(Task(NOTIFY, payload))
        self.assertEqual(self.discord.calls, [])

    def test_the_bounds_themselves_are_allowed(self) -> None:
        adapter = self.adapter()
        adapter.validate(Task(NOTIFY, _note(title="x" * 5, body_text="z" * 20)))
        adapter.validate(Task(NOTIFY, _note(title="x" * 80, body_text="z" * 1800)))


class MessageTests(Base):
    def test_a_brief_is_one_plain_message_with_moss_prefix_and_no_reactions(self) -> None:
        """Reverted: an approval-style card, reactions added, or no prefix."""
        out = self.run_note(self.adapter(), _note())
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["sent"], True)
        self.assertEqual(out["kind"], "brief")
        self.assertEqual(len(self.discord.calls), 1)          # one POST; no PUT reactions
        method, path, body, auth = self.discord.calls[0]
        self.assertEqual((method, path), ("POST", f"/channels/{CHANNEL}/messages"))
        self.assertEqual(auth, f"Bot {TOKEN}")
        self.assertEqual(body["content"],
                         f"\U0001f4ca **Moss — Tonight's brief**\n\n{BODY}")
        self.assertEqual(body["allowed_mentions"], {"parse": []})
        self.assertEqual(out["message_id"], str(self.discord._next))
        self.assertEqual(out["channel_id"], CHANNEL)
        self.assertEqual(out["remaining"], {"brief": BRIEFS_PER_DAY - 1, "alert": ALERTS_PER_DAY})

    def test_an_alert_has_the_warning_prefix(self) -> None:
        out = self.run_note(self.adapter(), _note("alert", title="Delivery blocked"))
        self.assertTrue(out["ok"], out)
        self.assertTrue(self.discord.posts()[0]["content"].startswith(
            "⚠️ **Moss — Delivery blocked**\n\n"))

    def test_mentions_are_neutralised_in_title_and_body(self) -> None:
        """Reverted neutralising: @everyone in a brief pings the whole server (and
        allowed_mentions alone is one lock, not two)."""
        body = ("Heads up @everyone and @here: <@123456789012345678> and <@!123456789012345678> "
                "and role <@&987654321098765432> all mentioned here.")
        out = self.run_note(self.adapter(), _note(title="Ping @everyone now", body_text=body))
        self.assertTrue(out["ok"], out)
        content = self.discord.posts()[0]["content"]
        for raw in ("@everyone", "@here", "<@123456789012345678>", "<@!123456789012345678>",
                    "<@&987654321098765432>"):
            self.assertNotIn(raw, content)
        self.assertIn("@\u200beveryone", content)
        self.assertIn("@\u200bhere", content)
        self.assertIn("@user and @user", content)
        self.assertIn("role @role", content)
        self.assertEqual(self.discord.posts()[0]["allowed_mentions"], {"parse": []})
        self.assertEqual(neutralise_mentions("mail me @ home"), "mail me @ home")

    def test_title_markdown_cannot_break_out_of_the_bold(self) -> None:
        self.run_note(self.adapter(), _note(title="**not bold** _x_"))
        self.assertIn("**Moss — \\*\\*not bold\\*\\* \\_x\\_**",
                      self.discord.posts()[0]["content"])


class LimitTests(Base):
    def test_two_briefs_a_day_then_refused_until_the_first_is_a_day_old(self) -> None:
        """Reverted limit: a third brief reaches the owner the same day."""
        adapter = self.adapter()
        for i in range(BRIEFS_PER_DAY):
            self.clock.at += timedelta(hours=1)
            self.assertTrue(self.run_note(adapter, _note(title=f"Brief number {i}"))["ok"])
        first = self.clock.at - timedelta(hours=BRIEFS_PER_DAY - 1)
        self.clock.at += timedelta(hours=1)
        out = self.run_note(adapter, _note(title="One brief too many"))
        self.assertFalse(out["ok"])
        self.assertIn("the limit of 2 briefs in any 24 hours is reached", out["refused"])
        self.assertEqual(out["error"], out["refused"])
        self.assertEqual((out["kind"], out["limit"]), ("brief", 2))
        self.assertEqual(out["next_at"], (first + timedelta(hours=24)).isoformat(
            timespec="seconds"))
        self.assertEqual(len(self.discord.posts()), BRIEFS_PER_DAY)
        # rolling: once the first is 24 hours old, one more may go
        self.clock.at = first + timedelta(hours=24, seconds=1)
        self.assertTrue(self.run_note(adapter, _note(title="Next day's brief"))["ok"])

    def test_alerts_have_their_own_limit_of_four(self) -> None:
        adapter = self.adapter()
        self.assertTrue(self.run_note(adapter, _note())["ok"])
        self.assertTrue(self.run_note(adapter, _note())["ok"])
        for _ in range(ALERTS_PER_DAY):
            self.assertTrue(self.run_note(adapter, _note("alert", title="Order waiting"))["ok"])
        out = self.run_note(adapter, _note("alert", title="Order waiting"))
        self.assertIn("the limit of 4 alerts in any 24 hours is reached", out["refused"])
        self.assertEqual(len(self.discord.posts()), 2 + ALERTS_PER_DAY)

    def test_the_count_survives_a_restart(self) -> None:
        """Reverted to an in-memory count: restarting Pionir resets the limit."""
        for _ in range(BRIEFS_PER_DAY):
            self.assertTrue(self.run_note(self.adapter(), _note())["ok"])
        out = self.run_note(self.adapter(), _note())            # a fresh adapter: a restart
        self.assertIn("refused", out)
        saved = json.loads((self.root / "discord" / "owner-notify.json").read_text("utf-8"))
        self.assertEqual([e["kind"] for e in saved["sent"]], ["brief", "brief"])

    def test_a_note_discord_did_not_take_does_not_count(self) -> None:
        adapter = self.adapter()
        self.discord.down = True
        with self.assertLogs("pionir.adapters.owner", level=logging.WARNING):
            for _ in range(3):
                self.assertIn("unavailable", self.run_note(adapter, _note()))
        self.discord.down = False
        self.assertTrue(self.run_note(adapter, _note())["ok"])
        self.assertTrue(self.run_note(adapter, _note())["ok"])

    def test_an_unreadable_record_sends_nothing(self) -> None:
        """Fail closed: a record that cannot be read means the limit is unknown."""
        path = self.root / "discord" / "owner-notify.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        out = self.run_note(self.adapter(), _note())
        self.assertFalse(out["ok"])
        self.assertIn("unreadable", out["unavailable"])
        self.assertEqual(self.discord.calls, [])

    def test_concurrent_notes_never_squeeze_past_the_limit(self) -> None:
        adapter = self.adapter()
        results: list[dict[str, Any]] = []
        threads = [threading.Thread(target=lambda: results.append(self.run_note(adapter, _note())))
                   for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(sum(1 for r in results if r["ok"]), BRIEFS_PER_DAY)
        self.assertEqual(len(self.discord.posts()), BRIEFS_PER_DAY)


class UnavailableTests(Base):
    def test_discord_not_configured_is_unavailable_and_sends_nothing(self) -> None:
        for over, why in (({"channel_id": None}, "no Discord channel"),
                          ({"enabled": False}, "disabled"),
                          ({"token_file": None}, "no Discord bot token file")):
            with self.subTest(why):
                adapter = self.adapter(**over)
                out = self.run_note(adapter, _note())
                self.assertFalse(out["ok"])
                self.assertIn(why, out["unavailable"])
                self.assertNotIn("refused", out)
                with self.assertRaises(AdapterUnavailable):
                    adapter.status()
        self.assertEqual(self.discord.calls, [])

    def test_an_empty_token_file_is_unavailable(self) -> None:
        self.token_file.write_text("  \n", encoding="utf-8")
        out = self.run_note(self.adapter(), _note())
        self.assertIn("no Discord bot token", out["unavailable"])
        self.assertEqual(self.discord.calls, [])

    def test_the_token_is_never_in_an_output_an_error_or_a_log(self) -> None:
        """Reverted scrubbing: Discord's error echoing the token lands in Moss's record."""
        adapter = self.adapter()
        with self.assertLogs("pionir.adapters.owner", level=logging.WARNING) as logs:
            self.discord.status = 401
            rejected = self.run_note(adapter, _note())
            self.discord.status = 403
            refused = self.run_note(adapter, _note())
            self.discord.status = None
            self.discord.down = True
            down = self.run_note(adapter, _note())
        self.assertIn("rejected the bot token", rejected["unavailable"])
        self.assertIn("Discord refused the message", refused["unavailable"])
        self.assertIn("Discord did not answer", down["unavailable"])
        everything = json.dumps([rejected, refused, down]) + "\n".join(logs.output) + repr(adapter)
        self.assertNotIn(TOKEN, everything)
        self.assertIn("<redacted>", everything)

    def test_status_counts_what_was_sent(self) -> None:
        adapter = self.adapter()
        self.run_note(adapter, _note("alert", title="Order waiting"))
        self.assertEqual(adapter.status()["sent_24h"], {"brief": 0, "alert": 1})


class _WithApp(Base):
    def setUp(self) -> None:
        super().setUp()
        self.app = PionirApp(build_runtime(PionirSettings(
            state_root=self.root / "pionir",
            atani_command=("pionir-test-no-such-binary",),
            daedalus_url=None, melete_url=None, galatea_url=None, crew_url=None,
            content_url=None, gumroad_url=None,
            bryo_status_command=None, nyx_status_command=None, voodoo_status_command=None,
            embed_model=None, evict_to_fit=False, owner_notify=False,
        )))
        self.addCleanup(self.app.runtime.cortex.close)
        self.app.runtime.register(self.adapter())


class OwnerNotifyContractTests(_WithApp):
    """The agreement with Moss. Galatea's tests/test_owner_notify_contract.py pins these
    very shapes (captured from this code through PionirApp.run_task) and maps each to
    done / refused / failed. Change a shape here and that side must change with it."""

    NOTE: ClassVar[dict[str, str]] = {"title": "Daily brief for Saturday",
                                      "body_text": "From my read of the crew's digest, just now.",
                                      "kind": "brief"}

    def test_the_bounds_and_fields_moss_builds_to(self) -> None:
        from pionir.adapters import owner
        self.assertEqual((owner.TITLE_MIN, owner.TITLE_MAX, owner.BODY_MIN, owner.BODY_MAX),
                         (5, 80, 20, 1800))
        self.assertEqual(owner.FIELDS, {"title", "body_text", "kind"})
        self.assertEqual(owner.KINDS, ("brief", "alert"))
        self.assertEqual((owner.BRIEFS_PER_DAY, owner.ALERTS_PER_DAY), (2, 4))

    def test_sent(self) -> None:
        out = self.app.run_task(NOTIFY, dict(self.NOTE))
        self.assertEqual(set(out), {"ok", "agent_id", "result", "evidence", "task_id"})
        self.assertIs(out["ok"], True)
        self.assertEqual(out["agent_id"], "owner")
        self.assertEqual(set(out["result"]), {"ok", "sent", "kind", "message_id", "channel_id",
                                              "sent_at", "remaining"})
        self.assertEqual((out["result"]["ok"], out["result"]["sent"], out["result"]["kind"]),
                         (True, True, "brief"))
        self.assertEqual(out["result"]["remaining"], {"brief": 1, "alert": 4})
        self.assertEqual(out["evidence"], ["owner:notify", "owner:notify:brief",
                                           f"discord:message:{out['result']['message_id']}"])

    def test_over_the_limit(self) -> None:
        for _ in range(BRIEFS_PER_DAY):
            self.app.run_task(NOTIFY, dict(self.NOTE))
        out = self.app.run_task(NOTIFY, dict(self.NOTE))
        self.assertIs(out["ok"], False)
        self.assertNotIn("status", out)
        self.assertEqual(set(out["result"]), {"ok", "refused", "error", "kind", "limit", "next_at"})
        self.assertTrue(out["result"]["refused"].startswith(
            "owner.notify: the limit of 2 briefs in any 24 hours is reached; the next one can go at "))
        self.assertEqual(out["result"]["error"], out["result"]["refused"])
        self.assertEqual(out["evidence"], ["owner:notify", "owner:notify:brief"])

    def test_invalid(self) -> None:
        out = self.app.run_task(NOTIFY, {**self.NOTE, "title": "Hi"})
        self.assertEqual({k: v for k, v in out.items() if k != "task_id"},
                         {"ok": False, "status": "error",
                          "error": {"type": "AdapterProtocolError",
                                    "message": "owner.notify: title is 5-80 characters, not 2"}})

    def test_discord_not_configured(self) -> None:
        self.app.runtime.executive.registry.unregister("owner")
        self.app.runtime.adapters.pop("owner")
        self.app.runtime.register(self.adapter(channel_id=None))
        out = self.app.run_task(NOTIFY, {**self.NOTE, "kind": "alert"})
        why = "owner.notify: no Discord channel is configured (PIONIR_DISCORD_CHANNEL_ID)"
        self.assertEqual(out["result"], {"ok": False, "unavailable": why, "error": why})
        self.assertEqual(out["evidence"], ["owner:notify", "owner:notify:alert"])


class ThroughPionirTests(_WithApp):
    """PionirApp.run_task -> executive -> adapter: runs at once, never parks."""

    def test_a_note_runs_without_approval_and_is_in_the_ledger(self) -> None:
        out = self.app.run_task(NOTIFY, _note(), permissions=[])
        self.assertTrue(out["ok"], out)
        self.assertNotEqual(out.get("status"), "pending_approval")
        self.assertEqual(self.app.approvals.pending(), [])
        self.assertEqual(out["agent_id"], "owner")
        self.assertTrue(out["result"]["sent"])
        self.assertIn(f"discord:message:{out['result']['message_id']}", out["evidence"])
        events = [e for e in self.app.runtime.executive.audit_sink.recent(50)
                  if e["agent_id"] == "owner"]
        self.assertIn("task.completed", {e["event_type"] for e in events})

    def test_a_bad_note_is_an_adapter_protocol_error_and_never_parked(self) -> None:
        out = self.app.run_task(NOTIFY, _note(title="Hi"), permissions=[])
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["type"], "AdapterProtocolError")
        self.assertEqual(self.app.approvals.pending(), [])
        self.assertEqual(self.discord.calls, [])

    def test_over_the_limit_is_refused_and_leaves_the_circuit_closed(self) -> None:
        for _ in range(BRIEFS_PER_DAY):
            self.assertTrue(self.app.run_task(NOTIFY, _note())["ok"])
        for _ in range(5):
            out = self.app.run_task(NOTIFY, _note())
            self.assertFalse(out["ok"])
            self.assertIn("limit", out["result"]["refused"])
        self.assertEqual(self.app.runtime.executive.circuit("owner").snapshot().state.value,
                         "closed")


class WiringTests(unittest.TestCase):
    def test_bootstrap_registers_it_from_the_gate_settings_and_the_switch_removes_it(self) -> None:
        for on in (True, False):
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root:
                runtime = build_runtime(PionirSettings(
                    state_root=Path(root), atani_command=("pionir-test-no-such-binary",),
                    daedalus_url=None, melete_url=None, galatea_url=None, crew_url=None,
                    bryo_status_command=None, nyx_status_command=None,
                    voodoo_status_command=None, embed_model=None, evict_to_fit=False,
                    owner_notify=on))
                try:
                    self.assertEqual("owner" in runtime.adapters, on)
                    if on:
                        adapter = runtime.adapters["owner"]
                        gate = DiscordGateSettings.from_environment(Path(root))
                        self.assertEqual(adapter.settings.state_dir, gate.directory)
                        self.assertEqual(adapter.settings.token_file, gate.token_file)
                        self.assertEqual(adapter.settings.channel_id, gate.channel_id)
                finally:
                    runtime.cortex.close()

    def test_the_switch_comes_from_the_environment(self) -> None:
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"PIONIR_OWNER_NOTIFY": "0"}):
            self.assertFalse(PionirSettings.from_environment().owner_notify)
        with mock.patch.dict(os.environ, {"PIONIR_OWNER_NOTIFY": "1"}):
            self.assertTrue(PionirSettings.from_environment().owner_notify)


class LoopbackTests(Base):
    """The real urllib opener against a fake Discord on a real loopback socket."""

    def test_a_brief_over_a_real_socket(self) -> None:
        seen: list[dict[str, Any]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                seen.append({"path": self.path, "auth": self.headers.get("Authorization"),
                             "type": self.headers.get("Content-Type"),
                             "agent": self.headers.get("User-Agent"),
                             "body": json.loads(raw)})
                ok = self.headers.get("Authorization") == f"Bot {TOKEN}"
                answer = json.dumps({"id": "1500000000000000001"} if ok
                                    else {"message": "401: Unauthorized"}).encode()
                self.send_response(200 if ok else 401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(answer)))
                self.end_headers()
                self.wfile.write(answer)

            def log_message(self, *args: Any) -> None:
                pass

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        base = f"http://127.0.0.1:{httpd.server_address[1]}/api/v10"
        adapter = OwnerNotifyAdapter(self.settings(api_base=base), clock=self.clock)  # real opener
        out = self.run_note(adapter, _note("alert", title="Paid order waiting",
                                           body_text="1 paid orders waiting for "
                                                     "acknowledgement (read 5 minutes ago)."))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["message_id"], "1500000000000000001")
        (req,) = seen
        self.assertEqual(req["path"], f"/api/v10/channels/{CHANNEL}/messages")
        self.assertEqual(req["auth"], f"Bot {TOKEN}")
        self.assertEqual(req["type"], "application/json")
        self.assertTrue(req["agent"].startswith("DiscordBot"))
        self.assertEqual(req["body"]["allowed_mentions"], {"parse": []})
        self.assertTrue(req["body"]["content"].startswith(
            "⚠️ **Moss — Paid order waiting**\n\n1 paid orders waiting"))
        # and a rejected token over the same socket is unavailable, token scrubbed
        self.token_file.write_text("MTk4" + "wrong.token", encoding="utf-8")
        bad = self.run_note(adapter, _note())
        self.assertIn("rejected the bot token", bad["unavailable"])
        self.assertNotIn("wrong.token", json.dumps(bad))


if __name__ == "__main__":
    unittest.main()
