"""Daily batching of routine public approvals.

The owner's decision: "Batch low-risk ones daily." Routine public items wait for one
daily digest card he answers item by item; anything with money or a client stays its
own card, at once. These tests pin the safety rules - batchability is an explicit,
fail-closed allowlist; money and clients are never batched; an undecided item carries
over and an old one expires unrun; nothing ever runs twice; a restart mid-digest loses
nothing - and that a batched item is the same queue record, run the same way, as one
with its own card. Discord is faked at the HTTP opener (test_discord_gate.FakeDiscord).
"""
from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
from datetime import UTC, datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import test_discord_gate as tdg
from test_content_publish import SECRET as CONTENT_SECRET
from test_content_publish import _settings as content_settings
from test_content_publish import draft
from test_discord_gate import APPROVE, CHANNEL, DENY, OWNER, STRANGER, GateCase

from pionir.adapters.content import ContentAdapter, ContentSettings
from pionir.batching import (
    BATCHED_GRANT,
    NEVER_BATCH_WORDS,
    DigestSettings,
    batch_refusal,
    read_request,
)
from pionir.bootstrap import build_runtime
from pionir.contracts import AgentManifest, Capability, RiskLevel, Task, TaskResult
from pionir.discord_gate import MESSAGE_LIMIT, MONEY_WORDS, NUMBERS, DiscordGate
from pionir.server import PionirApp

# What the owner said may wait for the digest. Adding to it is a decision, made here.
EXPECTED_BATCHABLE = {"content.publish", "content.crosspost_devto", "social.instagram_post",
                      "product.gumroad_publish"}
TZ = timezone(timedelta(hours=-4))
DAY1 = datetime(2026, 9, 26, 8, 0, tzinfo=TZ)
PAGE = "site.publish_page"
BUY = "shop.buy"
NEWSLETTER = "client.newsletter"


class _Stand:
    """A routine public capability (batchable), one that spends money, and one that
    contacts clients but was (wrongly) declared batchable. Counts what ran."""

    def __init__(self) -> None:
        self.ran: list[str] = []
        self._manifest = AgentManifest(
            agent_id="stand", version="test",
            capabilities=(
                Capability(name=PAGE, description="publish a free-tool page",
                           risk=RiskLevel.PRIVILEGED, required_permissions=frozenset({PAGE}),
                           requires_approval=True, batchable=True, routable=False),
                Capability(name=BUY, description="buy something",
                           risk=RiskLevel.PRIVILEGED, required_permissions=frozenset({BUY}),
                           spends_money=True, routable=False),
                Capability(name=NEWSLETTER, description="email every client",
                           risk=RiskLevel.PRIVILEGED,
                           required_permissions=frozenset({NEWSLETTER}),
                           requires_approval=True, batchable=True, routable=False),
            ),
        )

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def execute(self, task: Task) -> TaskResult:
        self.ran.append(task.capability)
        return TaskResult(task_id=task.task_id, agent_id="stand", output={"ok": True},
                          evidence=())


# ---------------------------------------------------------------- the allowlist
class AllowlistTests(unittest.TestCase):
    def test_every_registered_capability_batchable_only_if_routine_and_public(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = tdg._app(tmp)      # the full default roster, as the live stack builds it
            try:
                caps = [c for m in app.runtime.executive.registry.manifests()
                        for c in m.capabilities]
            finally:
                app.runtime.cortex.close()
        names = {c.name for c in caps}
        # the money and client capabilities are really in the roster being checked
        for name in ("client.email", "client.quote", "client.deliver", "client.release",
                     "client.find_report", "client.quote_reminder"):
            self.assertIn(name, names)
        # (a product listing may wait only as a listing its adapter confirmed is new)
        batchable = {c.name for c in caps
                     if batch_refusal(c, {}, {"listing": "new"}) is None}
        self.assertEqual(batchable, EXPECTED_BATCHABLE)
        product = next(c for c in caps if c.name == "product.gumroad_publish")
        for context in (None, {}, {"listing": "existing"}, {"listing": "unknown"}):
            self.assertIsNotNone(batch_refusal(product, {"price_cents": 1900}, context))
        for cap in caps:
            words = set(cap.name.replace("_", ".").split("."))
            if cap.spends_money or cap.name.startswith("client.") or words & MONEY_WORDS:
                self.assertIsNotNone(batch_refusal(cap, {}), cap.name)
                self.assertFalse(cap.batchable, cap.name)
            if cap.batchable:
                self.assertFalse(cap.spends_money, cap.name)
                self.assertTrue(cap.requires_approval, cap.name)

    def test_nothing_is_batchable_by_default(self) -> None:
        cap = Capability(name="x.post", description="d", risk=RiskLevel.PRIVILEGED,
                         requires_approval=True)
        self.assertFalse(cap.batchable)
        self.assertEqual(batch_refusal(cap, {}), "not declared batchable")

    def test_a_batchable_capability_cannot_spend_money_or_skip_approval(self) -> None:
        with self.assertRaises(ValueError):
            Capability(name="x.buy", description="d", risk=RiskLevel.PRIVILEGED,
                       spends_money=True, batchable=True)
        with self.assertRaises(ValueError):
            Capability(name="x.post", description="d", risk=RiskLevel.PRIVILEGED,
                       batchable=True)

    def test_every_layer_refuses_on_its_own(self) -> None:
        good = Capability(name="x.post", description="d", risk=RiskLevel.PRIVILEGED,
                          requires_approval=True, batchable=True)
        self.assertIsNone(batch_refusal(good, {"title": "t"}))
        self.assertIsNotNone(batch_refusal(None, {}))
        self.assertIsNotNone(batch_refusal(good, None))
        # a client or money word in the name, even when declared batchable
        for name in ("client.newsletter", "x.refund", "shop.pay", "x.quote_send",
                     "orders.post"):
            cap = Capability(name=name, description="d", risk=RiskLevel.PRIVILEGED,
                             requires_approval=True, batchable=True)
            self.assertIsNotNone(batch_refusal(cap, {}), name)
        # a payload that moves money, as the card's own money line reads it
        for payload in ({"spends_money": True}, {"price": 5}, {"amount": {"amount": 1}},
                        {"spend": "10 USD"}):
            self.assertIsNotNone(batch_refusal(good, payload), payload)
        # anything that only looks like a capability is refused, never trusted
        odd = SimpleNamespace(name="x.post", batchable=True, spends_money=True,
                              requires_approval=True)
        self.assertIsNotNone(batch_refusal(odd, {}))
        self.assertIsNotNone(batch_refusal(SimpleNamespace(name="x.post", batchable=True), {}))

    def test_the_never_batch_words_cover_every_money_word(self) -> None:
        self.assertLessEqual(set(MONEY_WORDS), NEVER_BATCH_WORDS)


# ---------------------------------------------------------------- the settings
class SettingsTests(unittest.TestCase):
    def test_defaults(self) -> None:
        s = DigestSettings.from_environment(None, {})
        self.assertEqual((s.enabled, s.time_text, s.expire_days), (True, "09:00", 7))

    def test_environment_then_file_then_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "discord").mkdir()
            (root / "discord" / "config.json").write_text(json.dumps(
                {"digest_time": "18:15", "digest_expire_days": 3}), encoding="utf-8")
            s = DigestSettings.from_environment(root, {})
            self.assertEqual((s.time_text, s.expire_days), ("18:15", 3))
            s = DigestSettings.from_environment(root, {"PIONIR_DIGEST_TIME": "07:30",
                                                       "PIONIR_DIGEST_EXPIRE_DAYS": "10"})
            self.assertEqual((s.time_text, s.expire_days), ("07:30", 10))

    def test_bad_values_fall_back_and_off_turns_it_off(self) -> None:
        s = DigestSettings.from_environment(None, {"PIONIR_DIGEST_TIME": "25:00",
                                                   "PIONIR_DIGEST_EXPIRE_DAYS": "0"})
        self.assertEqual((s.time_text, s.expire_days), ("09:00", 7))
        self.assertFalse(DigestSettings.from_environment(None, {"PIONIR_DIGEST": "off"}).enabled)

    def test_the_schedule(self) -> None:
        s = DigestSettings(at=time(9, 0))
        self.assertEqual(s.next_digest(DAY1), DAY1.replace(hour=9))
        after = DAY1.replace(hour=10)
        self.assertEqual(s.next_digest(after), (DAY1 + timedelta(days=1)).replace(hour=9))
        self.assertEqual(s.last_slot(DAY1), (DAY1 - timedelta(days=1)).replace(hour=9))
        self.assertEqual(s.last_slot(after), DAY1.replace(hour=9))


# ---------------------------------------------------------------- parking
class BatchCase(GateCase):
    def setUp(self) -> None:
        super().setUp()
        self.stand = _Stand()
        self.app.runtime.register(self.stand)
        self.now = DAY1
        self.app.digest = DigestSettings(enabled=True, at=time(9, 0), expire_days=7)

    def dgate(self, owner: str | None = OWNER, opener: Any = None) -> DiscordGate:
        return DiscordGate.for_app(self.app, self.settings(owner), opener=opener or self.fake,
                                   sleep=self.sleeps.append, clock=lambda: self.now)

    def primed(self, owner: str | None = OWNER) -> DiscordGate:
        """A gate that has already had today's digest (with nothing in it)."""
        gate = self.dgate(owner)
        self.assertTrue(gate.run_once())
        self.assertEqual(self.fake.posts(), [])
        return gate

    def page(self, title: str = "Free JSON formatter") -> str:
        out = self.app.run_task(PAGE, {"title": title, "body": "the page"})
        self.assertEqual(out["status"], "pending_approval")
        self.assertTrue(out.get("batched"), out)
        return out["approval_id"]

    def state(self) -> dict[str, Any]:
        return json.loads(self.settings().state_path.read_text(encoding="utf-8"))

    def cards(self) -> list[dict[str, Any]]:
        cards = self.state()["digest"]["cards"].values()
        return sorted(cards, key=lambda c: (c["date"], c["posted_at"], c["index"]))

    def digest_posts(self) -> list[dict[str, Any]]:
        return [p for p in self.fake.posts() if "Daily digest" in p["content"]]

    def at(self, hour: int, minute: int = 0, days: int = 0) -> None:
        self.now = (DAY1 + timedelta(days=days)).replace(hour=hour, minute=minute)

    def expire(self, approval_id: str) -> None:
        path = self.root / "approvals" / "queue.json"
        rows = json.loads(path.read_text(encoding="utf-8"))
        for row in rows:
            if row["id"] == approval_id:
                row["expires_at"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        path.write_text(json.dumps(rows), encoding="utf-8")


class ParkingTests(BatchCase):
    def test_a_routine_item_is_the_same_record_with_a_digest_date_and_longer_expiry(self):
        aid = self.page()
        row = self.app.approvals.get(aid)
        self.assertTrue(row["batch"])
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["permissions"], [PAGE])
        self.assertIsNotNone(row["digest_date"])
        waited = (datetime.fromisoformat(row["expires_at"])
                  - datetime.fromisoformat(row["created_at"]))
        self.assertEqual(waited, timedelta(days=7))
        self.assertEqual(self.stand.ran, [])

    def test_money_and_clients_are_never_batched(self):
        for capability in (BUY, NEWSLETTER):
            out = self.app.run_task(capability, {"content": "x"})
            self.assertEqual(out["status"], "pending_approval")
            self.assertNotIn("batched", out)
            row = self.app.approvals.get(out["approval_id"])
            self.assertFalse(row["batch"], capability)
            self.assertIsNone(row["digest_date"])
        # a routine capability whose payload moves money is its own card too
        out = self.app.run_task(PAGE, {"title": "t", "price": 5})
        self.assertFalse(self.app.approvals.get(out["approval_id"])["batch"])
        self.assertEqual(self.stand.ran, [])

    def test_batching_off_parks_everything_individually(self):
        self.app.digest = DigestSettings(enabled=False)
        out = self.app.run_task(PAGE, {"title": "t"})
        self.assertFalse(self.app.approvals.get(out["approval_id"])["batch"])

    def test_the_view_shows_batched_items_pending_with_their_digest_date(self):
        aid = self.page()
        view = self.app.approvals_view()
        row = next(r for r in view["pending"] if r["id"] == aid)
        self.assertTrue(row["batch"])
        self.assertRegex(row["digest_date"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(view["digest"]["time"], "09:00")
        self.assertEqual(view["digest"]["batched_pending"], 1)
        self.assertFalse(view["digest"]["requested"])

    def test_the_real_blog_publish_is_batched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token = root / "secrets" / "scrooge-publish-token.txt"
            token.parent.mkdir(parents=True)
            token.write_text(CONTENT_SECRET, encoding="utf-8")
            runtime = build_runtime(content_settings(root, content_url=None))
            runtime.register(ContentAdapter(ContentSettings(base_url="https://api.dokaz.test",
                                                            token_file=token)))
            app = PionirApp(runtime)
            try:
                out = app.run_task("content.publish", draft(), permissions=["content.publish"])
                self.assertTrue(out.get("batched"), out)
                self.assertTrue(app.approvals.get(out["approval_id"])["batch"])
            finally:
                runtime.cortex.close()


# ---------------------------------------------------------------- the digest
class DigestTests(BatchCase):
    def test_items_wait_for_the_digest_time_money_does_not(self):
        gate = self.primed()
        first, second = self.page("Free JSON formatter"), self.page("Regex tester")
        self.at(8, 30)
        gate.run_once()
        self.assertEqual(self.fake.posts(), [])            # routine items wait
        money = self.app.run_task(BUY, {"content": "a domain", "spend": "12 USD"})
        gate.run_once()
        posts = self.fake.posts()
        self.assertEqual(len(posts), 1)                     # money: its own card, at once
        self.assertIn("SPENDS MONEY", posts[0]["content"])
        self.assertEqual(posts[0]["allowed_mentions"]["users"], [OWNER])
        self.assertIn(money["approval_id"], self.state()["messages"])
        self.at(9, 0)
        gate.run_once()
        posts = self.fake.posts()[1:]
        previews, cards = posts[:2], posts[2:]
        self.assertEqual(len(cards), 1)
        for preview in previews:
            self.assertIn("Waits in the daily digest", preview["content"])
            self.assertEqual(preview["allowed_mentions"]["users"], [])   # no ping
        card = cards[0]["content"]
        self.assertEqual(cards[0]["allowed_mentions"]["users"], [OWNER])  # one ping
        self.assertIn("Daily digest", card)
        self.assertIn(f"{NUMBERS[0]} ⏳ waiting", card)
        self.assertIn("Free JSON formatter", card)
        self.assertIn(f"{NUMBERS[1]} ⏳ waiting", card)
        self.assertIn("Regex tester", card)
        for aid in (first, second):
            mid = self.message_id(aid)
            self.assertIn(f"/{CHANNEL}/{mid}>", card)       # links to its full preview
        [entry] = self.cards()
        self.assertEqual(entry["items"], [first, second])
        self.assertEqual(sorted(self.fake.messages[entry["message_id"]]["reactions"]),
                         sorted([NUMBERS[0], NUMBERS[1], APPROVE]))
        day = DAY1.date().isoformat()
        self.assertEqual(self.app.approvals.get(first)["digest_date"], day)
        # once a day: later passes the same day post nothing more
        self.at(15, 0)
        gate.run_once()
        self.assertEqual(len(self.digest_posts()), 1)
        self.assertEqual(self.stand.ran, [])

    def _digest(self, *titles: str) -> tuple[DiscordGate, list[str], dict[str, Any]]:
        gate = self.primed()
        ids = [self.page(t) for t in titles]
        self.at(9, 0)
        gate.run_once()
        return gate, ids, self.cards()[-1]

    def test_a_number_approves_that_item_exactly_once(self):
        gate, (first, second), card = self._digest("One", "Two")
        mid = card["message_id"]
        self.fake.react(mid, NUMBERS[1], STRANGER)
        gate.run_once()
        self.assertEqual(self.app.approvals.get(second)["status"], "pending")
        self.fake.react(mid, NUMBERS[1], OWNER)
        gate.run_once()
        row = self.settle(second)
        self.assertEqual(row["status"], "approved")
        # exactly as its own card would have run it: an approval job, its own permission
        self.assertEqual(self.app.jobs.get(row["task_id"])["kind"], "approval")
        # (plus the batched marker: a restriction its adapter may check, never a power)
        self.assertEqual(self.runs, [(PAGE, {"title": "Two", "body": "the page"},
                                      [PAGE, BATCHED_GRANT])])
        self.assertEqual(self.app.approvals.get(first)["status"], "pending")
        for _ in range(3):
            gate.run_once()
        self.fake.react(mid, APPROVE, OWNER)                # approve-all after: not again
        gate.run_once()
        self.settle(first)
        self.assertEqual([r[1]["title"] for r in self.runs], ["Two", "One"])
        self.assertFalse(self.app.approve(second)["ok"])    # nor from the phone
        gate.run_once()
        self.assertEqual(len(self.runs), 2)
        text = self.fake.content(mid)
        self.assertIn("approved, ran OK", text)
        self.assertIn("Every item on this card is decided", text)
        self.assertIn("APPROVED** by **Ian** in Discord",
                      self.fake.content(self.message_id(second)))

    def test_approve_all_runs_every_waiting_item_once(self):
        gate, ids, card = self._digest("One", "Two", "Three")
        self.fake.react(card["message_id"], APPROVE, OWNER)
        gate.run_once()
        for aid in ids:
            self.assertEqual(self.settle(aid)["status"], "approved")
        for _ in range(3):
            gate.run_once()
        self.assertEqual(sorted(r[1]["title"] for r in self.runs), ["One", "Three", "Two"])

    def test_a_reject_on_the_preview_beats_approve_all_in_the_same_pass(self):
        gate, (first, second), card = self._digest("Keep", "Drop")
        self.fake.react(self.message_id(second), DENY, OWNER)
        self.fake.react(card["message_id"], APPROVE, OWNER)
        gate.run_once()
        self.settle(first)
        self.assertEqual(self.app.approvals.get(second)["status"], "denied")
        self.assertEqual([r[1]["title"] for r in self.runs], ["Keep"])
        gate.run_once()
        self.assertIn("rejected, not run", self.fake.content(card["message_id"]))

    def test_the_owners_cross_on_the_card_approves_nothing(self):
        gate, ids, card = self._digest("One", "Two")
        self.fake.react(card["message_id"], APPROVE, OWNER)
        self.fake.react(card["message_id"], DENY, OWNER)
        gate.run_once()
        gate.run_once()
        self.assertEqual([self.app.approvals.get(a)["status"] for a in ids],
                         ["pending", "pending"])
        self.assertEqual(self.runs, [])

    def test_no_owner_means_nothing_is_approvable_from_the_card(self):
        gate = self.primed(owner=None)
        aid = self.page()
        self.at(9, 0)
        gate.run_once()
        [card] = self.cards()
        self.assertIn("cannot accept answers", self.fake.content(card["message_id"]))
        self.assertEqual(self.fake.messages[card["message_id"]]["reactions"], {})
        self.fake.react(card["message_id"], NUMBERS[0], OWNER)
        self.fake.react(card["message_id"], APPROVE, OWNER)
        gate.run_once()
        self.assertEqual(self.app.approvals.get(aid)["status"], "pending")
        self.assertEqual(self.runs, [])

    def test_undecided_items_carry_over_and_the_old_card_stops_counting(self):
        gate, (first, second), old = self._digest("One", "Two")
        self.fake.react(old["message_id"], NUMBERS[0], OWNER)
        gate.run_once()
        self.settle(first)
        preview = self.message_id(second)
        posts_before = len(self.fake.posts())
        self.at(9, 0, days=1)
        third = self.page("Three")
        gate.run_once()
        day2 = (DAY1 + timedelta(days=1)).date().isoformat()
        new = self.cards()[-1]
        self.assertEqual(new["date"], day2)
        self.assertEqual(new["items"], [second, third])     # carried over, plus the new one
        self.assertEqual(self.app.approvals.get(second)["digest_date"], day2)
        self.assertEqual(self.message_id(second), preview)  # its preview is not re-posted
        self.assertEqual(len(self.fake.posts()) - posts_before, 2)   # third's preview + card
        old_text = self.fake.content(old["message_id"])
        self.assertIn(f"Superseded by the digest of {day2}", old_text)
        self.assertIn("carried over to", old_text)
        # a number on the superseded card no longer counts (numbers changed)
        self.fake.react(old["message_id"], NUMBERS[1], OWNER)
        self.fake.react(old["message_id"], APPROVE, OWNER)
        gate.run_once()
        self.assertEqual(self.app.approvals.get(second)["status"], "pending")
        self.assertEqual(self.app.approvals.get(third)["status"], "pending")
        self.assertEqual(len(self.runs), 1)
        # never silently dropped, never auto-approved: still waiting on the new card
        self.fake.react(new["message_id"], NUMBERS[0], OWNER)
        gate.run_once()
        self.assertEqual(self.settle(second)["status"], "approved")

    def test_an_item_past_its_days_expires_unrun_and_is_reported_once(self):
        gate, (aid,), _card = self._digest("Stale page")
        self.expire(aid)
        self.at(9, 0, days=1)
        gate.run_once()
        row = self.app.approvals.get(aid)
        self.assertEqual((row["status"], row["reason"]), ("denied", "expired"))
        reports = [p["content"] for p in self.fake.posts() if "Expired, not run" in p["content"]]
        self.assertEqual(len(reports), 1)
        self.assertIn("Stale page", reports[0])
        self.assertIn(aid, reports[0])
        self.assertIn("EXPIRED", self.fake.content(self.message_id(aid)))
        self.at(9, 0, days=2)
        gate.run_once()
        reports = [p for p in self.fake.posts() if "Expired, not run" in p["content"]]
        self.assertEqual(len(reports), 1)                   # reported once
        self.fake.react(_card["message_id"], APPROVE, OWNER)
        gate.run_once()
        self.assertEqual(self.runs, [])
        self.assertFalse(self.app.approve(aid)["ok"])

    def test_the_owner_can_ask_for_the_digest_now(self):
        gate = self.primed()
        aid = self.page()
        self.at(8, 10)
        out = self.app.request_digest()
        self.assertTrue(out["ok"])
        self.assertEqual(out["batched_pending"], 1)
        self.assertTrue(self.app.approvals_view()["digest"]["requested"])
        gate.run_once()
        [card] = self.cards()
        self.assertEqual(card["items"], [aid])
        self.assertIn("sent early, as you asked", self.fake.content(card["message_id"]))
        self.assertTrue(read_request(self.root)["answered"])
        self.assertFalse(self.app.approvals_view()["digest"]["requested"])
        gate.run_once()
        self.assertEqual(len(self.digest_posts()), 1)       # answered once
        self.fake.react(card["message_id"], NUMBERS[0], OWNER)
        gate.run_once()
        self.assertEqual(self.settle(aid)["status"], "approved")
        self.assertEqual(len(self.runs), 1)

    def test_more_than_ten_items_go_on_more_cards_each_within_one_message(self):
        gate = self.primed()
        ids = [self.page(f"Free tool number {i:02d}: " + "a very long page title " * 4)
               for i in range(12)]
        self.at(9, 0)
        gate.run_once()
        cards = self.cards()
        self.assertGreaterEqual(len(cards), 2)
        listed = [a for c in cards for a in c["items"]]
        self.assertEqual(listed, ids)                       # every item, once, in order
        for card in cards:
            self.assertLessEqual(len(card["items"]), len(NUMBERS))
            text = self.fake.content(card["message_id"])
            self.assertLessEqual(len(text), MESSAGE_LIMIT)
            self.assertIn(f"of {len(cards)}", text)
        last = cards[-1]
        self.fake.react(last["message_id"], NUMBERS[0], OWNER)
        gate.run_once()
        self.assertEqual(self.settle(last["items"][0])["status"], "approved")
        self.assertEqual(len(self.runs), 1)

    def test_an_item_whose_preview_failed_is_never_approved_blind(self):
        gate = self.primed()
        blind, seen = self.page("Blind"), self.page("Seen")
        self.fake.fail.append(("POST", f"/channels/{CHANNEL}/messages", 400,
                               {"message": "Invalid Form Body", "code": 50035}))
        self.at(9, 0)
        gate.run_once()
        [card] = self.cards()
        self.assertEqual(card["items"], [blind, seen])      # listed, not dropped
        text = self.fake.content(card["message_id"])
        self.assertIn("not approvable here", text)
        self.fake.react(card["message_id"], NUMBERS[0], OWNER)
        self.fake.react(card["message_id"], APPROVE, OWNER)
        gate.run_once()
        self.settle(seen)
        self.assertEqual(self.app.approvals.get(blind)["status"], "pending")
        self.assertEqual([r[1]["title"] for r in self.runs], ["Seen"])
        # the next digest posts its preview and it can be answered there
        self.at(9, 0, days=1)
        gate.run_once()
        new = self.cards()[-1]
        self.assertEqual(new["items"], [blind])
        self.assertIn(blind, self.state()["messages"])
        self.fake.react(new["message_id"], NUMBERS[0], OWNER)
        gate.run_once()
        self.assertEqual(self.settle(blind)["status"], "approved")
        # and the superseded card still shows how it ended
        gate.run_once()
        [line] = [x for x in self.fake.content(card["message_id"]).split("\n")
                  if x.startswith(NUMBERS[0])]
        self.assertIn("approved, ran OK", line)

    def test_batching_turned_off_posts_waiting_items_as_ordinary_cards(self):
        self.primed()
        aid = self.page()
        self.app.digest = DigestSettings(enabled=False)
        gate = self.dgate()
        gate.run_once()
        [post] = self.fake.posts()
        self.assertIn("React ✅ to approve", post["content"])
        self.assertEqual(post["allowed_mentions"]["users"], [OWNER])
        self.assertEqual(self.digest_posts(), [])
        self.fake.react(self.message_id(aid), APPROVE, OWNER)
        gate.run_once()
        self.assertEqual(self.settle(aid)["status"], "approved")


class _CrashOnCard:
    """Discord goes away exactly when the digest card is being posted."""

    def __init__(self, fake: tdg.FakeDiscord) -> None:
        self.fake = fake
        self.armed = True

    def __call__(self, request: Any, timeout: float | None = None) -> Any:
        if self.armed and request.get_method() == "POST" and request.data \
                and "Daily digest" in json.loads(request.data).get("content", ""):
            self.armed = False
            raise urllib.error.URLError("connection reset")
        return self.fake(request, timeout)


class RestartTests(BatchCase):
    def test_a_restart_mid_digest_finishes_it_without_losing_or_doubling(self):
        self.primed()
        ids = [self.page("One"), self.page("Two")]
        self.at(9, 0)
        crashing = self.dgate(opener=_CrashOnCard(self.fake))
        self.assertFalse(crashing.run_once())               # previews out, card lost
        self.assertEqual(len(self.fake.posts()), 2)
        self.assertEqual(self.digest_posts(), [])
        restarted = self.dgate()                            # a fresh process
        self.assertTrue(restarted.run_once())
        self.assertEqual(len(self.fake.posts()), 3)         # just the card: no preview twice
        [card] = self.cards()
        self.assertEqual(card["items"], ids)
        self.assertTrue(self.state()["digest"]["run"]["complete"])
        again = self.dgate()
        again.run_once()
        self.assertEqual(len(self.fake.posts()), 3)         # and no second digest
        self.fake.react(card["message_id"], APPROVE, OWNER)
        again.run_once()
        self.dgate().run_once()                             # another restart, same answer
        for aid in ids:
            self.assertEqual(self.settle(aid)["status"], "approved")
        self.assertEqual(len(self.runs), 2)

    def test_a_restart_keeps_following_the_card(self):
        gate = self.primed()
        aid = self.page()
        self.at(9, 0)
        gate.run_once()
        [card] = self.cards()
        restarted = self.dgate()
        restarted.run_once()
        self.assertEqual(len(self.digest_posts()), 1)
        self.fake.react(card["message_id"], NUMBERS[0], OWNER)
        restarted.run_once()
        self.assertEqual(self.settle(aid)["status"], "approved")
        self.dgate().run_once()
        self.assertEqual(len(self.runs), 1)

    def test_a_deleted_digest_card_is_posted_again(self):
        gate = self.primed()
        aid = self.page()
        self.at(9, 0)
        gate.run_once()
        [card] = self.cards()
        self.fake.delete(card["message_id"])
        gate.run_once()
        [card] = self.cards()
        self.assertIn(card["message_id"], self.fake.messages)
        self.fake.react(card["message_id"], NUMBERS[0], OWNER)
        gate.run_once()
        self.assertEqual(self.settle(aid)["status"], "approved")


if __name__ == "__main__":
    unittest.main()
