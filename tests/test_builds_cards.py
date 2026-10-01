"""Pionir's side of the Builds division: its cards, and the owner's replies to them.

Discord is a fake; nothing reaches a network. Each test fails if the rule it names is
reverted: a reply from someone other than the owner recorded or served, a card whose fixed
head can be left out (a STAGED card that does not say the product is not on sale), a card
that takes replies it should not, a card posted twice for one key, or the gate not reading
the Builds replies once the adapter is registered.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from test_discord_gate import CHANNEL, OWNER, STRANGER
from test_discord_gate import TOKEN as DISCORD_TOKEN
from test_quote_paid import API, ReplyDiscord

from pionir.adapters.builds import (
    CARD,
    HEADS,
    INBOX,
    BuildCardAdapter,
    BuildCardSettings,
    build_replies,
)
from pionir.contracts import RiskLevel, Task
from pionir.errors import AdapterProtocolError
from pionir.fiverr import FiverrReplies


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._t = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._t.name)
        self.token = self.root / "discord-token.txt"
        self.token.write_text(DISCORD_TOKEN, encoding="utf-8")
        self.discord = ReplyDiscord()

    def tearDown(self) -> None:
        self._t.cleanup()

    def adapter(self, **over) -> BuildCardAdapter:
        settings = dict(state_root=self.root / "state", channel_id=CHANNEL,
                        owner_user_id=OWNER, discord_token_file=self.token, api_base=API)
        settings.update(over)
        return BuildCardAdapter(BuildCardSettings(**settings), discord_opener=self.discord)

    def run_task(self, adapter, capability, payload) -> dict:
        task = Task(capability, payload)
        adapter.validate(task)
        return dict(adapter.execute(task).output)

    def card(self, **over) -> dict:
        base = {"key": "builds:night:2026-10-01", "kind": "night",
                "title": "Builds, the night of 2026-10-01", "body": "exif-strip: staged.",
                "replies": True}
        base.update(over)
        return base

    def posted(self) -> list:
        return [b["content"] for m, p, b in self.discord.calls
                if m == "POST" and p.endswith("/messages") and isinstance(b, dict)
                and "message_reference" not in b]

    def tick(self, replies: FiverrReplies) -> None:
        from pionir.discord_gate import DiscordRest

        def call(method, path, body=None, files=None):
            return DiscordRest(DISCORD_TOKEN, api_base=API, opener=self.discord).call(
                method, path, body, files=files)

        replies.tick(call, OWNER, CHANNEL)


class CardTests(_Case):
    def test_the_capabilities_are_by_name_only_and_never_parked(self) -> None:
        caps = {c.name: c for c in self.adapter().manifest.capabilities}
        self.assertEqual(caps[CARD].risk, RiskLevel.REVERSIBLE_WRITE)
        self.assertEqual(caps[INBOX].risk, RiskLevel.READ_ONLY)
        self.assertFalse(caps[CARD].routable)
        self.assertFalse(caps[CARD].requires_approval)

    def test_every_card_opens_with_its_fixed_head(self) -> None:
        a = self.adapter()
        out = self.run_task(a, CARD, self.card(key="builds:staged:exif-strip", kind="staged",
                                               title="EXIF Strip", replies=False))
        self.assertTrue(out["ok"], out)
        content = self.posted()[-1]
        self.assertTrue(content.startswith(HEADS["staged"]))
        self.assertIn("NOT on sale", content)

    def test_only_the_nightly_and_backlog_cards_take_replies(self) -> None:
        a = self.adapter()
        with self.assertRaises(AdapterProtocolError):
            a.validate(Task(CARD, self.card(kind="shelved", replies=True)))
        for bad in ({"kind": "sale"}, {"key": "x"}, {"title": "two\nlines"}, {"body": ""},
                    {"files": ["a.zip"]}, {"replies": "yes"}):
            with self.assertRaises(AdapterProtocolError, msg=str(bad)):
                a.validate(Task(CARD, self.card(**bad)))
        with self.assertRaises(AdapterProtocolError):
            a.validate(Task(INBOX, {"kind": "staged"}))

    def test_a_card_is_posted_once_per_key(self) -> None:
        a = self.adapter()
        first = self.run_task(a, CARD, self.card())
        again = self.run_task(a, CARD, self.card(body="different"))
        self.assertTrue(again["already"])
        self.assertEqual(again["message_id"], first["message_id"])
        self.assertEqual(len(self.posted()), 1)

    def test_without_discord_configured_it_says_unavailable(self) -> None:
        out = self.run_task(self.adapter(owner_user_id=None), CARD, self.card())
        self.assertIs(out["ok"], False)
        self.assertIn("unavailable", out)
        self.assertEqual(self.posted(), [])


class ReplyTests(_Case):
    def test_only_the_owners_reply_is_recorded_and_served(self) -> None:
        a = self.adapter()
        out = self.run_task(a, CARD, self.card())
        replies = build_replies(self.root / "state")
        replies.every = 0.0
        self.discord.reply(out["message_id"], "top cron-explain")
        self.discord.reply(out["message_id"], "add evil-tool\nprice: 19", user=STRANGER)
        self.tick(replies)
        served = self.run_task(a, INBOX, {})["replies"]
        self.assertEqual([r["text"] for r in served], ["top cron-explain"])
        self.tick(replies)                                   # recorded once
        self.assertEqual(len(self.run_task(a, INBOX, {"kind": "night"})["replies"]), 1)
        answers = self.discord.answers()
        self.assertEqual(len(answers), 1)
        self.assertIn("Builds worker", answers[0]["content"])

    def test_the_gate_reads_build_replies_once_the_adapter_is_registered(self) -> None:
        from pionir.batching import DigestSettings
        from pionir.discord_gate import DiscordGate, DiscordGateSettings

        def app(adapters):
            approvals = SimpleNamespace(pending=lambda: [], get=lambda _id: None,
                                        find=lambda *a: [])
            return SimpleNamespace(runtime=SimpleNamespace(adapters=adapters),
                                   approvals=approvals, approve=lambda _id: {"ok": True},
                                   deny=lambda _id: {"ok": True}, run_task=lambda *a, **k: {})

        settings = DiscordGateSettings(state_root=self.root / "state", channel_id=CHANNEL,
                                       owner_user_id=OWNER, token_file=self.token,
                                       api_base=API, poll_seconds=0.01)
        off = DiscordGate.for_app(app({}), settings, opener=self.discord,
                                  sleep=lambda _s: None, digest=DigestSettings(enabled=False))
        self.assertIsNone(off._builds)
        a = self.adapter()
        on = DiscordGate.for_app(app({"builds": a}), settings, opener=self.discord,
                                 sleep=lambda _s: None, digest=DigestSettings(enabled=False))
        self.assertIsInstance(on._builds, FiverrReplies)
        out = self.run_task(a, CARD, self.card(kind="backlog", key="builds:backlog:1"))
        self.discord.reply(out["message_id"], "remove csv-to-ics")
        self.discord.reply(out["message_id"], "add x", user=STRANGER)
        on._builds.every = 0.0
        self.assertTrue(on.run_once())
        self.assertEqual([r["text"] for r in self.run_task(a, INBOX, {})["replies"]],
                         ["remove csv-to-ics"])


class WiringTests(unittest.TestCase):
    def test_bootstrap_registers_the_build_cards_only_when_asked(self) -> None:
        from unittest import mock

        from pionir.bootstrap import build_runtime
        from pionir.config import PionirSettings
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp, \
                mock.patch("pionir.discord_gate.DiscordRest"):
            for on in (False, True):
                runtime = build_runtime(PionirSettings(
                    state_root=Path(tmp).resolve(), atani_command=("no-such-binary",),
                    bryo_status_command=None, nyx_status_command=None,
                    voodoo_status_command=None, daedalus_url=None, melete_url=None,
                    crew_url=None, galatea_url=None, content_url=None, gumroad_url=None,
                    evict_to_fit=False, embed_model=None, builds_cards=on))
                try:
                    self.assertEqual("builds" in runtime.adapters, on)
                finally:
                    runtime.cortex.close()

    def test_the_sandbox_root_reaches_the_daedalus_adapter(self) -> None:
        from pionir.bootstrap import build_runtime
        from pionir.config import PionirSettings
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            runtime = build_runtime(PionirSettings(
                state_root=Path(tmp).resolve(), atani_command=("no-such-binary",),
                bryo_status_command=None, nyx_status_command=None,
                voodoo_status_command=None, daedalus_url="http://127.0.0.1:65530",
                melete_url=None, crew_url=None, galatea_url=None, content_url=None,
                gumroad_url=None, evict_to_fit=False, embed_model=None,
                daedalus_sandbox_root=tmp))
            try:
                self.assertEqual(runtime.adapters["daedalus"].settings.sandbox_root, tmp)
            finally:
                runtime.cortex.close()

    def test_the_sandbox_root_default_and_override(self) -> None:
        import os
        from unittest import mock

        from pionir.config import PionirSettings
        from pionir.crew.config import CrewSettings
        self.assertEqual(PionirSettings(state_root=Path(tempfile.gettempdir()))
                         .daedalus_sandbox_root, r"C:\src\daedalus-work")
        keep = {k: os.environ[k] for k in ("USERPROFILE", "HOME", "HOMEDRIVE", "HOMEPATH")
                if k in os.environ}
        with mock.patch.dict(os.environ, {**keep, "PIONIR_DAEDALUS_SANDBOX": r"D:\sandbox",
                                          "PIONIR_STATE_ROOT": tempfile.gettempdir()},
                             clear=True):
            self.assertEqual(PionirSettings.from_environment().daedalus_sandbox_root,
                             r"D:\sandbox")
            self.assertEqual(CrewSettings.from_environment().builds_sandbox,
                             Path(r"D:\sandbox"))


if __name__ == "__main__":
    unittest.main()
