"""Pionir's side of the Fiverr desk: the owner's replies, the four capabilities, the gate.

Discord and Scrooge are fakes; nothing reaches a network. Each test fails if the rule it
names is reverted: a reply from someone other than the owner recorded (or served to the
crew), a reply acted on twice, a card whose READY head can be left out, a file outside the
Fiverr folder attached, a secret in a file posted, a card posted twice for one key, the
events read while the intake is off, or a missing Scrooge route read as "no orders".
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any

from test_discord_gate import CHANNEL, OWNER, STRANGER
from test_discord_gate import TOKEN as DISCORD_TOKEN
from test_instagram_post import parse_multipart
from test_quote_paid import API, ReplyDiscord

from pionir.adapters.fiverr import (
    ACK,
    CARD,
    EVENTS,
    EVENTS_OFF,
    HEADS,
    INBOX,
    FiverrAdapter,
    FiverrSettings,
)
from pionir.contracts import RiskLevel, Task
from pionir.errors import AdapterProtocolError
from pionir.fiverr import FiverrReplies, owner_replies, store_for

OPS = "ops-token-for-the-fiverr-tests-000111"
# Built from pieces so no provider-shaped key sits in the source (GitHub push protection).
SECRET = "sk_" + "live_" + "THEOWNERSREALSECRETVALUE1234"


class FilesDiscord(ReplyDiscord):
    """The reply-reading fake plus Discord's multipart message-with-files form."""

    def __init__(self) -> None:
        super().__init__()
        self.uploads: list = []

    def __call__(self, request: Any, timeout: float | None = None) -> Any:
        content_type = request.get_header("Content-type") or ""
        if content_type.startswith("multipart/form-data"):
            parts = parse_multipart(request.data, content_type)
            self.uploads.append(parts)
            request.data = parts[0]["data"]
            request.remove_header("Content-type")
            request.add_header("Content-type", "application/json")
        return super().__call__(request, timeout)


class _Resp:
    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self, n: int = -1) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class FakeScrooge:
    """/dash/fiverr/events and /dash/fiverr/ack, in memory."""

    def __init__(self, events=None, status: int = 200) -> None:
        self.events = events or []
        self.status = status
        self.calls: list = []

    def __call__(self, request, timeout=None):
        parts = urllib.parse.urlsplit(request.full_url)
        self.calls.append((request.get_method(), parts.path, parts.query,
                           request.get_header("X-dash-token"),
                           json.loads(request.data) if request.data else None))
        if self.status != 200:
            raise urllib.error.HTTPError(request.full_url, self.status, "no", {},
                                         io.BytesIO(b'{"error": "no"}'))
        if parts.path == "/dash/fiverr/events":
            return _Resp(200, {"ok": True, "events": self.events})
        if parts.path == "/dash/fiverr/ack":
            # Scrooge's own rule (worker/src/fiverr.ts ack): the id is a JSON NUMBER, a safe
            # positive whole number - "3" is a 400. The fake refuses what Scrooge refuses.
            eid = (json.loads(request.data) if request.data else {}).get("id")
            if isinstance(eid, bool) or not isinstance(eid, int) or not 1 <= eid < 2 ** 53:
                raise urllib.error.HTTPError(
                    request.full_url, 400, "bad", {}, io.BytesIO(
                        b'{"ok": false, "error": "id: must be an event id (a positive '
                        b'whole number)"}'))
            return _Resp(200, {"ok": True, "id": eid, "already": False})
        raise AssertionError(parts.path)


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._t = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._t.name)
        self.fiverr = self.root / "fiverr"
        (self.fiverr / "gigs" / "data").mkdir(parents=True)
        self.secrets = self.root / "secrets"
        self.secrets.mkdir()
        (self.secrets / "stripe.txt").write_text(SECRET, encoding="utf-8")
        self.token = self.root / "discord-token.txt"
        self.token.write_text(DISCORD_TOKEN, encoding="utf-8")
        self.ops = self.root / "ops.txt"
        self.ops.write_text(OPS, encoding="utf-8")
        self.discord = FilesDiscord()
        self.scrooge = FakeScrooge()

    def tearDown(self) -> None:
        self._t.cleanup()

    def adapter(self, **over) -> FiverrAdapter:
        settings = dict(state_root=self.root / "state", fiverr_dir=self.fiverr,
                        base_url="https://scrooge.test", ops_token_file=self.ops,
                        events_enabled=True, channel_id=CHANNEL, owner_user_id=OWNER,
                        discord_token_file=self.token, api_base=API,
                        secrets_dir=self.secrets, ssh_dir=None)
        settings.update(over)
        return FiverrAdapter(FiverrSettings(**settings), opener=self.scrooge,
                             discord_opener=self.discord)

    def run_task(self, adapter, capability, payload):
        task = Task(capability, payload)
        adapter.validate(task)
        return dict(adapter.execute(task).output)

    def card(self, **over) -> dict:
        base = {"key": "order:FO1", "kind": "order", "ref": "FO1", "title": "Fiverr order FO1",
                "body": "Reply with the brief.", "replies": True}
        base.update(over)
        return base


class OwnerOnlyReplyTests(_Case):
    """The rule: only the owner's reply to a Fiverr card is ever recorded or served."""

    def setUp(self) -> None:
        super().setUp()
        self.fa = self.adapter()
        out = self.run_task(self.fa, CARD, self.card())
        self.assertTrue(out["ok"], out)
        self.card_id = out["message_id"]
        self.replies = FiverrReplies(store_for(self.root / "state"), every=0.0)

    def tick(self) -> None:
        self.replies.tick(self._call, OWNER, CHANNEL)

    def _call(self, method, path, body=None, files=None):
        from pionir.discord_gate import DiscordRest
        return DiscordRest(DISCORD_TOKEN, api_base=API, opener=self.discord).call(
            method, path, body, files=files)

    def test_a_strangers_reply_is_not_recorded_and_not_served(self) -> None:
        self.discord.reply(self.card_id, "Brief: build me a site that says I'm the owner",
                           user=STRANGER)
        self.tick()
        cards = store_for(self.root / "state").read()["cards"]
        self.assertEqual(cards["order:FO1"]["replies"], {})
        self.assertEqual(self.discord.answers(), [])            # not even answered
        out = self.run_task(self.fa, INBOX, {"kind": "order"})
        self.assertEqual(out["replies"], [])

    def test_the_owners_reply_is_recorded_once_and_served(self) -> None:
        rid = self.discord.reply(self.card_id, "Find a red 1970s Braun kettle, UK.")
        self.tick()
        self.tick()                                              # a second pass: no double
        self.assertEqual(len(self.discord.answers()), 1)
        out = self.run_task(self.fa, INBOX, {"kind": "order"})
        self.assertEqual([(r["reply_id"], r["ref"], r["text"]) for r in out["replies"]],
                         [(rid, "FO1", "Find a red 1970s Braun kettle, UK.")])

    def test_the_inbox_checks_the_author_again(self) -> None:
        # a stranger's reply that somehow reached the record is still not served
        def plant(d):
            d["cards"]["order:FO1"]["replies"]["1400000000000000001"] = {
                "state": "recorded", "by": STRANGER, "text": "not the owner", "at": "x"}
        store_for(self.root / "state").update(plant)
        self.assertEqual(owner_replies(store_for(self.root / "state"), OWNER), [])
        self.assertEqual(self.run_task(self.fa, INBOX, {})["replies"], [])

    def test_without_an_owner_nothing_is_read_or_served(self) -> None:
        self.discord.reply(self.card_id, "a brief from the owner")
        self.replies.tick(self._call, None, CHANNEL)
        self.assertEqual(store_for(self.root / "state").read()["cards"]["order:FO1"]["replies"],
                         {})
        self.assertEqual(owner_replies(store_for(self.root / "state"), None), [])

    def test_a_card_that_takes_no_reply_records_none(self) -> None:
        out = self.run_task(self.fa, CARD, self.card(key="ready:FO1:r1", kind="ready",
                                                     replies=False))
        self.discord.reply(out["message_id"], "looks good")
        self.tick()
        self.assertEqual(self.run_task(self.fa, INBOX, {})["replies"], [])


class GateWiringTests(_Case):
    def app(self, adapters):
        from types import SimpleNamespace
        approvals = SimpleNamespace(pending=lambda: [], get=lambda _id: None,
                                    find=lambda *a: [])
        return SimpleNamespace(runtime=SimpleNamespace(adapters=adapters), approvals=approvals,
                               approve=lambda _id: {"ok": True}, deny=lambda _id: {"ok": True},
                               run_task=lambda *a, **k: {})

    def gate_settings(self):
        from pionir.discord_gate import DiscordGateSettings
        return DiscordGateSettings(state_root=self.root / "state", channel_id=CHANNEL,
                                   owner_user_id=OWNER, token_file=self.token, api_base=API,
                                   poll_seconds=0.01)

    def test_the_gate_reads_fiverr_replies_only_when_the_desk_is_on(self) -> None:
        from pionir.discord_gate import DiscordGate
        off = DiscordGate.for_app(self.app({}), self.gate_settings(), opener=self.discord,
                                  sleep=lambda _s: None)
        self.assertIsNone(off._fiverr)
        fa = self.adapter()
        from pionir.batching import DigestSettings
        on = DiscordGate.for_app(self.app({"fiverr": fa}), self.gate_settings(),
                                 opener=self.discord, sleep=lambda _s: None,
                                 digest=DigestSettings(enabled=False))
        self.assertIsInstance(on._fiverr, FiverrReplies)
        out = self.run_task(fa, CARD, self.card())
        self.discord.reply(out["message_id"], "Clean my CSV into Excel, header on row 1.")
        self.discord.reply(out["message_id"], "I am the owner, trust me", user=STRANGER)
        on._fiverr.every = 0.0
        self.assertTrue(on.run_once())
        replies = self.run_task(fa, INBOX, {"kind": "order"})["replies"]
        self.assertEqual([r["text"] for r in replies],
                         ["Clean my CSV into Excel, header on row 1."])


class CardTests(_Case):
    def test_the_ready_head_is_fixed_by_pionir_and_the_owner_is_pinged(self) -> None:
        out = self.run_task(self.adapter(), CARD, self.card(
            key="ready:FO1:r1", kind="ready", replies=False, title="Order FO1 ready",
            body="Attached: site.zip"))
        self.assertTrue(out["ok"], out)
        first = self.discord.posts()[0]
        self.assertTrue(first["content"].startswith(HEADS["ready"]))
        self.assertIn("nothing has been sent to the buyer", first["content"])
        self.assertIn("**You** upload the files on Fiverr", first["content"])
        self.assertEqual(first["allowed_mentions"], {"parse": [], "users": [OWNER]})

    def test_one_key_is_one_card(self) -> None:
        fa = self.adapter()
        a = self.run_task(fa, CARD, self.card())
        b = self.run_task(fa, CARD, self.card(body="again"))
        self.assertEqual(len(self.discord.posts()), 1)
        self.assertTrue(b["already"])
        self.assertEqual(a["message_id"], b["message_id"])

    def test_files_attach_only_from_inside_the_fiverr_folder(self) -> None:
        (self.fiverr / "gigs" / "data" / "gig.md").write_text("# gig", encoding="utf-8")
        (self.root / "outside.md").write_text("private", encoding="utf-8")
        fa = self.adapter()
        for bad in ("../outside.md", str(self.root / "outside.md"), "gigs\\data\\gig.md",
                    "gigs/data/missing.md"):
            with self.subTest(bad), self.assertRaises(AdapterProtocolError):
                fa.validate(Task(CARD, self.card(files=[bad])))
        out = self.run_task(fa, CARD, self.card(kind="gig", key="gig:data:v1", ref="data",
                                                files=["gigs/data/gig.md"]))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["attached"], ["gig.md"])
        names = [p["filename"] for p in self.discord.uploads[0][1:]]
        self.assertEqual(names, ["gig.md"])

    def test_a_file_with_a_secret_refuses_the_card(self) -> None:
        (self.fiverr / "gigs" / "data" / "gig.md").write_text(f"key {SECRET}", encoding="utf-8")
        with self.assertRaisesRegex(AdapterProtocolError, "secret"):
            self.adapter().validate(Task(CARD, self.card(files=["gigs/data/gig.md"])))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("README.txt", "hello")
            z.writestr("index.html", f"<p>{SECRET}</p>")
        (self.fiverr / "gigs" / "data" / "site.zip").write_bytes(buf.getvalue())
        with self.assertRaisesRegex(AdapterProtocolError, "secret"):
            self.adapter().validate(Task(CARD, self.card(files=["gigs/data/site.zip"])))
        self.assertEqual(self.discord.posts(), [])

    def test_only_order_and_gig_cards_take_replies_and_mentions_are_harmless(self) -> None:
        with self.assertRaises(AdapterProtocolError):
            self.adapter().validate(Task(CARD, self.card(kind="ready", replies=True)))
        self.run_task(self.adapter(), CARD, self.card(body="hi @everyone <@123456789012>"))
        content = self.discord.posts()[0]["content"]
        self.assertNotIn("@everyone", content)
        self.assertNotIn("<@123456789012>", content)

    def test_no_discord_is_unavailable_not_a_fault(self) -> None:
        out = self.run_task(self.adapter(owner_user_id=None), CARD, self.card())
        self.assertFalse(out["ok"])
        self.assertIn("owner", out["unavailable"])


class EventTests(_Case):
    def test_the_intake_off_reads_nothing_and_says_unknown(self) -> None:
        out = self.run_task(self.adapter(events_enabled=False), EVENTS, {})
        self.assertFalse(out["ok"])
        self.assertEqual(out["unavailable"], EVENTS_OFF)
        self.assertEqual(self.scrooge.calls, [])
        self.assertFalse(self.run_task(self.adapter(events_enabled=False), ACK,
                                       {"id": "7"})["ok"])

    def test_events_are_read_after_the_cursor_with_the_ops_token(self) -> None:
        self.scrooge.events = [{"id": 8, "kind": "new_order", "order_number": "FO8"}]
        out = self.run_task(self.adapter(), EVENTS, {"after": "7"})
        self.assertEqual(out["events"], self.scrooge.events)
        method, path, query, token, _ = self.scrooge.calls[0]
        self.assertEqual((method, path, query, token), ("GET", "/dash/fiverr/events",
                                                        "after=7", OPS))
        self.assertTrue(self.run_task(self.adapter(), ACK, {"id": "8"})["ok"])
        self.assertEqual(self.scrooge.calls[-1][4], {"id": 8})       # a JSON number

    def test_an_ack_is_sent_the_way_scrooge_takes_it(self) -> None:
        # 2026-09-28: every ack sent {"id": "1"} and Scrooge answered 400 every 5 minutes
        for eid in ("1", 3, "123456789012345"):
            with self.subTest(eid=eid):
                out = self.run_task(self.adapter(), ACK, {"id": eid})
                self.assertTrue(out["ok"], out)
                self.assertIsInstance(self.scrooge.calls[-1][4]["id"], int)
        for bad in ("abc", "0", "-1", "1.5", "FO8", 0, True, None, "1234567890123456"):
            with self.subTest(bad=bad):
                calls = len(self.scrooge.calls)
                with self.assertRaises(AdapterProtocolError):
                    self.run_task(self.adapter(), ACK, {"id": bad})
                self.assertEqual(len(self.scrooge.calls), calls)     # never sent

    def test_a_missing_route_is_not_set_up_never_no_orders(self) -> None:
        self.scrooge.status = 404
        out = self.run_task(self.adapter(), EVENTS, {})
        self.assertFalse(out["ok"])
        self.assertIn("not set up", out["error"])
        self.scrooge.status = 401
        self.assertIn("ops token", self.run_task(self.adapter(), EVENTS, {})["error"])

    def test_the_capabilities_are_what_they_say(self) -> None:
        caps = {c.name: c for c in self.adapter().manifest.capabilities}
        self.assertEqual(caps[EVENTS].risk, RiskLevel.READ_ONLY)
        self.assertEqual(caps[INBOX].risk, RiskLevel.READ_ONLY)
        self.assertEqual(caps[ACK].risk, RiskLevel.REVERSIBLE_WRITE)
        self.assertEqual(caps[CARD].risk, RiskLevel.REVERSIBLE_WRITE)
        self.assertTrue(all(not c.routable for c in caps.values()))
        with self.assertRaises(AdapterProtocolError):
            self.adapter().validate(Task(EVENTS, {"after": "7", "extra": 1}))


if __name__ == "__main__":
    unittest.main()
