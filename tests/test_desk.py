"""The Approval Desk (pionir/desk.py): one list of everything waiting on the owner.

Each test names the rule it guards and fails if that rule is reverted: an approval's money
read differently by the desk than by its Discord card, an FYI item counted as open, money
or a client approved as part of a group, a reply typed on the desk parsed differently from
the same reply on Discord, a stale card kept open for ever (or reported twice), and a quote
card left open after its order was declined with no reply to it.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from test_approvals import _app
from test_quote_paid import ORDER, _Gate, an_order

from pionir.adapters.builds import store_for as build_store
from pionir.approvals import ApprovalQueue
from pionir.desk import PARKED, Desk, approval_fields, bulk_ok, card_lane, with_fields
from pionir.discord_gate import DiscordGate, money_line
from pionir.fiverr import store_for as fiverr_store
from pionir.quotes import QUOTE, QuoteCardStore, QuoteSettings, build_quote, parse_reply
from pionir.server import _desk_status

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
OWNER_ID = "187403262008229888"
_SNOW = iter(range(1_000_000_000_000_000_000, 1_100_000_000_000_000_000, 1000))


def _ago(days: float) -> str:
    return (NOW - timedelta(days=days)).isoformat()


def _card(store: Any, key: str, *, kind: str | None, days: float, open_: bool = True,
          replies: dict | None = None, **extra: Any) -> None:
    def put(doc: dict) -> None:
        doc["cards"][key] = {"key": key, "kind": kind, "ref": key.split(":")[1 if ":" in key else 0],
                             "message_ids": [str(next(_SNOW))], "open": open_,
                             "replies": replies or {}, "posted_at": _ago(days), **extra}
    store.update(put)


def _answered() -> dict:
    return {str(next(_SNOW)): {"state": "recorded", "at": _ago(9), "by": OWNER_ID,
                               "text": "redraft"}}


QUOTE_PAYLOAD = {"order_id": ORDER, "to": "cy@example.com", "quote_ref": "discord-123456",
                 "total_cents": 120_000, "deposit_cents": 60_000, "days": 10,
                 "subject": "s", "body_text": "b"}
POST_PAYLOAD = {"headline": "Ship it", "points": ["a", "b"], "caption": "c"}


class FieldTests(unittest.TestCase):
    def test_each_stream_its_money_and_its_nearness_to_money(self) -> None:
        self.assertEqual(approval_fields(QUOTE, QUOTE_PAYLOAD),
                         {"amount_cents": 120_000, "stream": "client", "client_facing": True,
                          "proximity": 0})
        self.assertEqual(approval_fields("social.instagram_post", POST_PAYLOAD),
                         {"amount_cents": None, "stream": "instagram", "client_facing": False,
                          "proximity": 2})
        for capability, stream, near in (
                ("etsy.create_draft_listing", "etsy", 1), ("printify.publish", "etsy", 1),
                ("product.gumroad_publish", "gumroad", 1), ("apify.publish", "marketplace", 1),
                ("chrome.publish_update", "marketplace", 1), ("content.publish", "blog", 2),
                ("content.crosspost_devto", "devto", 2),
                ("content.newsletter_send", "newsletter", 2),
                ("apibuild.verify", "apibuild", 3), ("improve.apply", "improve", 3),
                ("security.nyx_run", "other", 3), ("client.testimonial_publish", "client", 2)):
            got = approval_fields(capability, {})
            self.assertEqual((got["stream"], got["proximity"]), (stream, near), capability)

    def test_the_amount_is_the_cards_own_reading(self) -> None:
        self.assertEqual(approval_fields("x.buy", {"price": "$1,250.5"})["amount_cents"], 125_050)
        self.assertEqual(approval_fields("x.buy", {"spend": {"amount": 12, "currency": "usd"}})
                         ["amount_cents"], 1200)
        # money whose amount is not a plain number: none, but still money (never bulk)
        self.assertIsNone(approval_fields("x.y", {"currency": "USD"})["amount_cents"])
        self.assertFalse(bulk_ok({"capability": "x.y", "payload": {"currency": "USD"}}))

    def test_a_parked_rows_own_fields_never_change_its_cards_money_line(self) -> None:
        # the derived amount_cents sits on the row; the card must still name the payload key
        with tempfile.TemporaryDirectory() as tmp:
            q = ApprovalQueue(Path(tmp) / "q.json")
            aid = q.enqueue(QUOTE, QUOTE_PAYLOAD, ["client.quote"], "quote")
            row = q.get(aid)
        self.assertEqual(row["amount_cents"], 120_000)
        self.assertIn("(`total_cents`)", money_line(row))

    def test_an_old_row_gets_its_fields_when_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "q.json"
            old = {"id": "a1b2c3d4e5f6", "created_at": datetime.now(UTC).isoformat(),
                   "capability": "etsy.create_draft_listing",
                   "payload": {"title": "Mug", "price": 18}, "permissions": [],
                   "summary": "etsy", "requester": "crew", "status": "pending"}
            path.write_text(json.dumps([old]), encoding="utf-8")
            (row,) = ApprovalQueue(path).pending()
            self.assertEqual((row["stream"], row["amount_cents"], row["client_facing"],
                              row["proximity"]), ("etsy", 1800, False, 1))
            # derived on read, not written back: the file is as it was
            self.assertNotIn("stream", json.loads(path.read_text(encoding="utf-8"))[0])
        self.assertEqual(with_fields(old)["stream"], "etsy")


class _DeskCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.queue = ApprovalQueue(self.root / "approvals" / "queue.json")
        self.now = NOW
        self.desk = Desk(self.root, self.queue, clock=lambda: self.now, environ={})
        self.quotes = QuoteCardStore.for_state_root(self.root)
        self.fiverr = fiverr_store(self.root)
        self.builds = build_store(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class LaneAndSortTests(_DeskCase):
    def test_lanes(self) -> None:
        _card(self.builds, "builds:night:2026-10-13", kind="night", days=1)
        _card(self.builds, "builds:backlog:x:0", kind="backlog", days=1)
        _card(self.builds, "builds:shelved:tool", kind="shelved", days=1, open_=False)
        _card(self.fiverr, "gig:data:v2", kind="gig", days=1)
        _card(self.fiverr, "unreadable:12", kind="note", days=1, open_=False)
        _card(self.quotes, ORDER, kind=None, days=1, order_id=ORDER, package="Custom")
        self.queue.enqueue("social.instagram_post", POST_PAYLOAD, ["p"], "post")
        view = self.desk.view()
        lanes = {i["id"]: i["lane"] for i in view["items"]}
        self.assertEqual(lanes, {
            "build:builds:night:2026-10-13": "fyi",      # the nightly report is news now
            "build:builds:backlog:x:0": "reply", "build:builds:shelved:tool": "fyi",
            "fiverr:gig:data:v2": "reply", "fiverr:unreadable:12": "fyi",
            f"quote:{ORDER}": "reply",
            next(i["id"] for i in view["items"] if i["source"] == "approval"): "decide"})
        self.assertEqual(view["counts"], {"decide": 1, "reply": 3, "fyi": 3})
        self.assertEqual([i["lane"] for i in self.desk.view(lane="fyi")["items"]], ["fyi"] * 3)
        self.assertEqual(self.desk.view(lane="fyi")["counts"], view["counts"])

    def test_answered_closed_and_old_news_are_not_on_the_desk(self) -> None:
        _card(self.fiverr, "gig:data:v1", kind="gig", days=1, replies=_answered())
        _card(self.fiverr, "gig:web:v1", kind="gig", days=1, open_=False, status=PARKED)
        _card(self.builds, "builds:shelved:old", kind="shelved", days=8, open_=False)
        _card(self.fiverr, "order:42", kind="order", days=1, open_=False)   # posted asking nothing
        self.assertEqual([i["id"] for i in self.desk.view()["items"]], ["fiverr:order:42"])
        self.assertEqual(card_lane("fiverr", {"kind": "order", "open": False}), "fyi")

    def test_sorted_by_lane_then_nearness_to_money_then_oldest_first(self) -> None:
        self.queue.enqueue("social.instagram_post", POST_PAYLOAD, ["p"], "post 1")
        self.queue.enqueue("etsy.create_draft_listing", {"title": "Mug"}, ["p"], "etsy")
        self.queue.enqueue("social.instagram_post", POST_PAYLOAD, ["p"], "post 2")
        self.queue.enqueue(QUOTE, QUOTE_PAYLOAD, ["p"], "quote")
        self.queue.enqueue("improve.apply", {"title": "fix"}, ["p"], "improve")
        _card(self.builds, "builds:backlog:x:0", kind="backlog", days=2)
        _card(self.quotes, ORDER, kind=None, days=1)
        _card(self.builds, "builds:night:2026-10-13", kind="night", days=1)
        got = [(i["lane"], i["summary"] if i["source"] == "approval" else i["id"])
               for i in self.desk.view()["items"]]
        self.assertEqual(got, [
            ("decide", "quote"), ("decide", "etsy"), ("decide", "post 1"),
            ("decide", "post 2"), ("decide", "improve"),
            ("reply", f"quote:{ORDER}"), ("reply", "build:builds:backlog:x:0"),
            ("fyi", "build:builds:night:2026-10-13")])

    def test_the_item_shape(self) -> None:
        aid = self.queue.enqueue(QUOTE, QUOTE_PAYLOAD, ["client.quote"], "a quote")
        (item,) = self.desk.view()["items"]
        self.assertEqual(set(item), {"id", "ref", "source", "lane", "title", "summary", "stream",
                                     "capability", "amount_cents", "client_facing", "bulk_ok",
                                     "group", "created_at", "expires_at", "preview", "detail",
                                     "reply_hint"})
        self.assertEqual((item["id"], item["ref"], item["amount_cents"], item["bulk_ok"],
                          item["group"]), (f"approval:{aid}", aid, 120_000, False,
                                           "client:client.quote"))
        self.assertIn("MONEY INVOLVED", item["detail"]["money_line"])


class BulkTests(unittest.TestCase):
    def setUp(self) -> None:
        self._t = tempfile.TemporaryDirectory()
        self.app = _app(self._t.name)
        q = self.app.approvals
        self.posts = [q.enqueue("social.instagram_post", POST_PAYLOAD, ["p"], f"post {n}")
                      for n in range(2)]
        self.money = q.enqueue("social.instagram_post", {**POST_PAYLOAD, "spend": 5}, ["p"],
                               "boosted")
        self.client = q.enqueue("client.email", {"order_id": ORDER, "to": "a@b.c"}, ["p"],
                                "email")

    def tearDown(self) -> None:
        self.app.runtime.cortex.close()
        self._t.cleanup()

    def pending(self) -> set[str]:
        return {r["id"] for r in self.app.approvals.pending()}

    def test_money_or_a_client_in_a_group_refuses_the_whole_group(self) -> None:
        self.assertTrue(all(bulk_ok(self.app.approvals.get(a)) for a in self.posts))
        for odd in (self.money, self.client):
            out = self.app.approve_group([*self.posts, odd], approver="dashboard")
            self.assertEqual(out["error"]["type"], "Conflict", out)
            self.assertEqual(_desk_status(out), 409)
            self.assertEqual(self.pending(), {*self.posts, self.money, self.client})

    def test_a_group_that_is_not_all_pending_approves_nothing(self) -> None:
        self.app.deny(self.posts[1])
        out = self.app.approve_group(self.posts, approver="dashboard")
        self.assertEqual(_desk_status(out), 409)
        self.assertIn(self.posts[0], self.pending())

    def test_only_the_owners_surfaces_and_never_what_it_parked(self) -> None:
        self.assertEqual(_desk_status(self.app.approve_group(self.posts, approver="crew")), 403)
        mine = self.app.approvals.enqueue("social.instagram_post", POST_PAYLOAD, ["p"], "own",
                                          requester="phone")
        out = self.app.approve_group([self.posts[0], mine], approver="phone")
        self.assertEqual(out["error"]["type"], "SelfApproval")
        self.assertIn(self.posts[0], self.pending())

    def test_a_routine_group_is_approved_item_by_item(self) -> None:
        out = self.app.approve_group([f"approval:{a}" for a in self.posts], approver="dashboard")
        self.assertEqual([r["approval_id"] for r in out["results"]], self.posts)
        for result in out["results"]:
            self.assertTrue(self.app.jobs.wait(result["task_id"], 30))
        self.assertFalse(set(self.posts) & self.pending())
        # each was claimed: a second group of the same ids is refused whole
        self.assertEqual(_desk_status(self.app.approve_group(self.posts)), 409)


class ExpiryTests(_DeskCase):
    def test_a_reply_card_unanswered_for_a_week_is_parked_and_listed_once(self) -> None:
        _card(self.fiverr, "gig:data:v2", kind="gig", days=8)
        _card(self.quotes, ORDER, kind=None, days=18)
        _card(self.fiverr, "gig:data:v1", kind="gig", days=20, replies=_answered())
        _card(self.builds, "builds:backlog:x:0", kind="backlog", days=3)
        _card(self.builds, "builds:night:2026-10-01", kind="night", days=13)
        sent: list[tuple[str, str]] = []

        def notify(title: str, body: str) -> bool:
            sent.append((title, body))
            return True

        self.desk.tick(notify)
        cards = {**self.fiverr.read()["cards"], **self.quotes.read()["cards"],
                 **self.builds.read()["cards"]}
        self.assertEqual({k for k, c in cards.items() if c.get("status") == PARKED},
                         {"gig:data:v2", ORDER})
        self.assertTrue(cards["gig:data:v2"]["open"])      # parked, never deaf
        self.assertTrue(cards["gig:data:v1"]["open"])            # answered: left alone
        self.assertTrue(cards["builds:night:2026-10-01"]["open"])  # news: never "parked"
        self.assertTrue(cards["builds:backlog:x:0"]["open"])
        (title, body), = sent
        self.assertIn("2 parked", title)
        self.assertIn(f"order {ORDER}", body)
        self.assertIn("data (v2)", body)
        # the same week: a second tick, a new Desk (a restart), more parked cards - no note
        self.now += timedelta(days=2)
        _card(self.fiverr, "idea:codes:v1", kind="idea", days=8)
        self.desk._last_sweep = -float("inf")
        self.desk.tick(notify)
        Desk(self.root, self.queue, clock=lambda: self.now, environ={}).tick(notify)
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.fiverr.read()["cards"]["idea:codes:v1"]["status"], PARKED)
        # a week on: one note with only what is new
        self.now += timedelta(days=6)
        self.assertTrue(self.desk.weekly_summary(notify))
        self.assertEqual(len(sent), 2)
        self.assertIn("1 parked", sent[1][0])
        self.assertNotIn("data (v2)", sent[1][1])

    def test_a_failed_note_is_tried_again_and_marks_nothing(self) -> None:
        _card(self.fiverr, "gig:data:v2", kind="gig", days=8)
        self.desk.sweep()
        self.assertFalse(self.desk.weekly_summary(lambda _t, _b: False))
        self.assertTrue(self.desk.weekly_summary(lambda _t, _b: True))

    def test_the_days_are_the_environments(self) -> None:
        _card(self.fiverr, "gig:data:v2", kind="gig", days=4)
        Desk(self.root, self.queue, clock=lambda: self.now, environ={}).sweep()
        self.assertTrue(self.fiverr.read()["cards"]["gig:data:v2"]["open"])
        desk = Desk(self.root, self.queue, clock=lambda: self.now,
                    environ={"PIONIR_CARD_STALE_DAYS": "3"})
        self.assertEqual(desk.sweep(), ["fiverr:gig:data:v2"])


class DismissTests(_DeskCase):
    def test_fyi_is_archived_and_a_reply_card_parked_by_hand(self) -> None:
        _card(self.builds, "builds:night:2026-10-13", kind="night", days=1)
        _card(self.fiverr, "gig:data:v2", kind="gig", days=1)
        self.assertEqual(self.desk.dismiss("build:builds:night:2026-10-13")["status"], "archived")
        self.assertEqual(self.desk.dismiss("fiverr:gig:data:v2")["status"], PARKED)
        self.assertEqual(self.desk.view()["items"], [])
        self.assertEqual(self.fiverr.read()["cards"]["gig:data:v2"]["status"], PARKED)
        # the owner's own choice is not something "still waiting on you"
        self.assertEqual(self.desk.parked_unlisted(), [])
        self.assertEqual(self.desk.dismiss("fiverr:gig:data:v2")["error"]["type"],
                         "AlreadyClosed")
        aid = self.queue.enqueue("social.instagram_post", POST_PAYLOAD, ["p"], "post")
        self.assertEqual(self.desk.dismiss(f"approval:{aid}")["error"]["type"],
                         "NotDismissable")
        self.assertEqual(self.desk.dismiss("fiverr:nope")["error"]["type"], "NotFound")
        with self.assertRaises(ValueError):
            self.desk.dismiss("nonsense")


# ---- the reply: one reading, Discord or desk ------------------------------------------------
class DeskReplyTests(_Gate):
    def setUp(self) -> None:
        super().setUp()
        self.app.gate = self.gate()

    def test_a_price_typed_on_the_desk_parks_the_same_quote_as_on_discord(self) -> None:
        head = self.card()
        out = self.app.desk_reply(f"quote:{ORDER}", "$1,200 10 days", approver="dashboard")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["state"], "submitted")
        (row,) = self.quotes_pending()
        self.assertEqual(out["approval_id"], row["id"])
        # exactly the payload the Discord path builds for the same words and reply id
        expected = build_quote(an_order(), parse_reply("$1,200 10 days"),
                               QuoteSettings.from_environment(), out["reply_id"])
        self.assertEqual(row["payload"], expected)
        self.assertIn("✍️ Read: **$1,200**", out["answer"])
        reply = QuoteCardStore.for_state_root(self.state).read()["cards"][ORDER]["replies"][
            out["reply_id"]]
        self.assertEqual((reply["via"], reply["by"]), ("desk", self.settings().owner))
        # nothing went to Discord, and nothing to the client: the quote waits for its own yes
        self.assertFalse([b for m, p, b in self.discord.calls if m == "POST"
                          and isinstance(b, dict) and b.get("message_reference")])
        self.assertEqual(self.scrooge.quotes(), [])
        self.assertEqual(self.approve(row["id"])["status"], "approved")
        (sent,) = self.scrooge.quotes()
        self.assertEqual(sent["quote_ref"], f"discord-{out['reply_id']}")
        self.assertTrue(head)

    def test_the_same_words_on_discord_park_the_same_payload(self) -> None:
        head = self.card()
        mine = self.discord.reply(head, "$1,200 10 days")
        self.app.gate.run_once()
        (discord_row,) = self.quotes_pending()
        self.app.deny(discord_row["id"])
        out = self.app.desk_reply(f"quote:{ORDER}", "$1,200 10 days", approver="dashboard")
        (desk_row,) = self.quotes_pending()
        def strip(p: dict) -> dict:
            return {k: v for k, v in p.items() if k != "quote_ref"}

        self.assertEqual(strip(desk_row["payload"]), strip(discord_row["payload"]))
        self.assertEqual(discord_row["payload"]["quote_ref"], f"discord-{mine}")
        self.assertEqual(desk_row["payload"]["quote_ref"], f"discord-{out['reply_id']}")

    def test_an_unreadable_price_is_refused_the_same_way(self) -> None:
        self.card()
        out = self.app.desk_reply(f"quote:{ORDER}", "$100,000 2 years", approver="dashboard")
        self.assertFalse(out["ok"])
        self.assertEqual((out["state"], out["why"]), ("unparsed", "that is not a price"))
        self.assertIn("couldn't read that as a price", out["answer"])
        self.assertEqual(self.quotes_pending(), [])

    def test_only_the_owners_surfaces_and_only_open_cards(self) -> None:
        self.card()
        self.assertEqual(self.app.desk_reply(f"quote:{ORDER}", "$350", approver="crew")
                         ["error"]["type"], "Forbidden")
        self.assertEqual(self.app.desk_reply("quote:ffffffffffff", "$350", approver="phone")
                         ["error"]["type"], "NotFound")
        QuoteCardStore.for_state_root(self.state).update(
            lambda d: d["cards"][ORDER].update(open=False, status="order_declined"))
        self.assertEqual(self.app.desk_reply(f"quote:{ORDER}", "$350", approver="phone")
                         ["error"]["type"], "Closed")
        self.app.gate = None
        self.assertEqual(self.app.desk_reply(f"quote:{ORDER}", "$350")["error"]["type"],
                         "Unavailable")

    def test_the_gate_runs_the_desk_housekeeping(self) -> None:
        self.assertEqual(DiscordGate.for_app(self.app, self.settings())._desk_tick,
                         self.app.desk_tick)


class LateReplyTests(_DeskCase):
    """A parked card is off the desk, never deaf: Ian's late words are always recorded."""

    def _discord_reply(self, key: str, text: str) -> str:
        from pionir.fiverr import FiverrReplies

        card = self.fiverr.read()["cards"][key]
        reply_id = str(next(_SNOW))
        FiverrReplies(self.fiverr).consider(
            lambda *_a, **_k: {"id": "1"}, OWNER_ID, "chan",
            {"id": reply_id, "author": {"id": OWNER_ID}, "content": text,
             "message_reference": {"message_id": card["message_ids"][0]}})
        return reply_id

    def test_a_late_reply_on_a_parked_card_is_recorded_and_un_parks_it(self) -> None:
        _card(self.fiverr, "idea:codes:v1", kind="idea", days=8)
        self.assertEqual(self.desk.sweep(), ["fiverr:idea:codes:v1"])
        self.assertEqual(self.desk.view()["counts"]["reply"], 0)
        reply_id = self._discord_reply("idea:codes:v1", "redraft")
        card = self.fiverr.read()["cards"]["idea:codes:v1"]
        self.assertEqual(card["replies"][reply_id]["state"], "recorded")
        # what ideas.py reads: the owner's late reply counts
        from pionir.fiverr import owner_replies
        self.assertEqual([r["text"] for r in owner_replies(self.fiverr, OWNER_ID, "idea")],
                         ["redraft"])
        # answered now: not parked (not in the weekly note), not on the desk, not re-parked
        self.assertEqual(self.desk.parked_unlisted(), [])
        self.assertEqual(self.desk.view()["items"], [])
        self.assertEqual(self.desk.sweep(), [])

    def test_a_dismissed_card_still_hears_a_late_reply(self) -> None:
        _card(self.fiverr, "gig:data:v2", kind="gig", days=1)
        self.desk.dismiss("fiverr:gig:data:v2")
        reply_id = self._discord_reply("gig:data:v2", "price 10 20 30")
        self.assertEqual(self.fiverr.read()["cards"]["gig:data:v2"]["replies"][reply_id]
                         ["state"], "recorded")


class QuoteCardClosesTests(_Gate):
    def test_a_declined_order_closes_its_card_with_no_reply_to_it(self) -> None:
        # the live bug: order 7c5ee61e3ae7 was declined after an unreadable reply, and its
        # card stayed open, because only a NEW reply ever looked at the order again
        head = self.card()
        self.discord.reply(head, "$100,000 2 years")
        gate = self.gate()
        gate.run_once()
        card = QuoteCardStore.for_state_root(self.state).read()["cards"][ORDER]
        self.assertTrue(card["open"])
        self.scrooge.orders[0]["status"] = "declined"
        gate._quotes._reconciled = -float("inf")
        gate.run_once()
        card = QuoteCardStore.for_state_root(self.state).read()["cards"][ORDER]
        self.assertFalse(card["open"])
        self.assertEqual(card["status"], "order_declined")

    def test_an_order_still_waiting_keeps_its_card_and_an_unreadable_one_is_left_alone(
            self) -> None:
        self.card()
        gate = self.gate()
        gate.run_once()
        self.assertTrue(QuoteCardStore.for_state_root(self.state).read()["cards"][ORDER]["open"])

        def broken(_oid: str) -> Any:
            raise OSError("Scrooge is down")

        gate._quotes._get_order = broken
        gate._quotes._reconciled = -float("inf")
        with self.assertLogs("pionir.quotes", "WARNING"):
            gate._quotes.reconcile({ORDER: {}})
        self.assertTrue(QuoteCardStore.for_state_root(self.state).read()["cards"][ORDER]["open"])


if __name__ == "__main__":
    unittest.main()


# ---- over HTTP: the routes and who may use them ---------------------------------------------
class DeskOverHttp(unittest.TestCase):
    def setUp(self) -> None:
        import threading
        from http.server import ThreadingHTTPServer

        from test_auth_gate import _runtime

        from pionir.server import PionirApp, _make_handler

        self._tmp = tempfile.TemporaryDirectory()
        self.app = PionirApp(_runtime(self._tmp.name))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self.app))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        q = self.app.approvals
        self.posts = [q.enqueue("social.instagram_post", POST_PAYLOAD, ["p"], f"post {n}")
                      for n in range(2)]
        self.quote = q.enqueue(QUOTE, QUOTE_PAYLOAD, ["client.quote"], "quote")
        _card(build_store(self.app.runtime.settings.state_root), "builds:night:2026-10-13",
              kind="night", days=0)

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.app.runtime.cortex.close()
        self._tmp.cleanup()

    def call(self, method: str, path: str, body: dict | None = None,
             client: str | None = None) -> tuple[int, dict]:
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=30)
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if client is not None:
            headers["Authorization"] = f"Bearer {self.app.auth.tokens[client]}"
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = conn.getresponse()
        document = json.loads(response.read() or b"{}")
        conn.close()
        return response.status, document

    def test_get_desk_and_its_filters(self) -> None:
        status, doc = self.call("GET", "/api/desk")
        self.assertEqual(status, 200)
        self.assertEqual(doc["counts"], {"decide": 3, "reply": 0, "fyi": 1})
        self.assertEqual(doc["items"][0]["ref"], self.quote)        # client money first
        _, doc = self.call("GET", "/api/desk?lane=decide&stream=instagram")
        self.assertEqual({i["ref"] for i in doc["items"]}, set(self.posts))
        self.assertEqual(self.call("GET", "/api/desk?lane=nope")[0], 400)

    def test_the_posts_are_the_owners_surfaces_only(self) -> None:
        for path, body in (("/api/desk/dismiss", {"id": "build:builds:night:2026-10-13"}),
                           ("/api/desk/reply", {"id": "quote:c0c0c0c0c0c0", "text": "$350"}),
                           ("/api/approvals/approve_group", {"ids": self.posts})):
            self.assertEqual(self.call("POST", path, body)[0], 401, path)
            self.assertEqual(self.call("POST", path, body, "crew")[0], 403, path)
        self.assertEqual(len(self.app.approvals.pending()), 3)
        status, out = self.call("POST", "/api/approvals/approve_group",
                                {"ids": [*self.posts, self.quote]}, "dashboard")
        self.assertEqual((status, out["error"]["type"]), (409, "Conflict"))
        self.assertEqual(len(self.app.approvals.pending()), 3)
        status, out = self.call("POST", "/api/desk/dismiss",
                                {"id": "build:builds:night:2026-10-13"}, "phone")
        self.assertEqual((status, out["status"]), (200, "archived"))
        # no Discord gate in this process: a reply has no recorder, and says so
        status, out = self.call("POST", "/api/desk/reply",
                                {"id": "quote:c0c0c0c0c0c0", "text": "$350"}, "dashboard")
        self.assertEqual((status, out["error"]["type"]), (503, "Unavailable"))
