"""The daily digest, after an adversarial review: five holes and the tests that close them.

1. a reject on an item's preview that lands after the preview was read, but before the
   digest card is, still beats an approve-all on the card;
2. an item whose image never reached the owner (an Instagram card, a product cover) is
   never approved from the digest - not by its number, not by approve-all;
3. a part of a digest Discord keeps refusing (a 403 on an edit or on the expired report)
   is given up and the owner told, instead of wedging every later digest;
4. a Gumroad listing is batched only as a NEW listing: an update of an existing one (and
   so any price change) is its own card showing "price X -> Y", and a "new" listing that
   exists by the time it runs is refused; the money line reads any *_cents and currency;
5. the digest-request file is written through a tmp of its own, under a lock, so an
   answer can never land on a newer request.
"""
from __future__ import annotations

import json
import threading
from datetime import time, timedelta
from typing import Any
from unittest import mock

import test_approval_batching as tab
import test_gumroad_products as tgp
import test_instagram_post as tig
from test_discord_gate import API, APPROVE, CHANNEL, DENY, OWNER, TOKEN

from pionir import batching
from pionir.batching import BATCHED_GRANT, DigestSettings, batch_refusal, read_request
from pionir.discord_gate import (
    DIGEST_PART_TRIES,
    NUMBERS,
    DiscordGate,
    DiscordGateSettings,
    money_line,
    render_request,
)

DAY1 = tab.DAY1


class _Wrap:
    """The fake Discord, with a hook that runs just before one kind of call is answered."""

    def __init__(self, fake: Any, hook: Any) -> None:
        self.fake = fake
        self.hook = hook

    def __call__(self, request: Any, timeout: float | None = None) -> Any:
        self.hook(request)
        return self.fake(request, timeout)


def _refuse(fake: Any, request: Any) -> None:
    fake._error(request.full_url, 403, {"message": "Missing Permissions", "code": 50013})


# ---------------------------------------------------------------- 1. reject mid-poll
class RejectMidPollTests(tab.BatchCase):
    def test_a_reject_landing_after_its_preview_was_read_still_wins(self):
        gate = self.primed()
        keep, drop = self.page("Keep"), self.page("Drop")
        self.at(9, 0)
        gate.run_once()
        [card] = self.cards()
        card_path = f"/channels/{CHANNEL}/messages/{card['message_id']}"
        drop_preview = self.message_id(drop)
        fired = []

        def land(request: Any) -> None:
            # both reactions arrive while the gate is between the previews and the card
            if not fired and request.get_method() == "GET" \
                    and request.full_url == API + card_path:
                fired.append(True)
                self.fake.react(drop_preview, DENY, OWNER)
                self.fake.react(card["message_id"], APPROVE, OWNER)

        racing = self.dgate(opener=_Wrap(self.fake, land))
        racing.run_once()
        self.assertTrue(fired)
        self.settle(keep)
        self.assertEqual([r[1]["title"] for r in self.runs], ["Keep"])
        self.assertEqual(self.app.approvals.get(drop)["status"], "denied")
        for _ in range(2):
            racing.run_once()
        self.assertEqual(len(self.runs), 1)


# ---------------------------------------------------------------- 2. unseen image
class UnseenImageTests(tig._Case):
    def setUp(self) -> None:
        super().setUp()
        self.app.digest = DigestSettings(enabled=True, at=time(9, 0), expire_days=7)
        self.now = DAY1
        self.runs: list[str] = []

        def execute(capability, payload, granted, *, deferrable=False):
            self.runs.append(capability)
            return {"ok": True, "agent_id": "instagram", "result": {"ok": True},
                    "evidence": []}

        self.app._execute_task = execute  # type: ignore[method-assign]
        self.fake = tig.FakeDiscordFiles()
        token_file = self.root / "secrets" / "discord-bot-token.txt"
        token_file.write_text(TOKEN, encoding="utf-8")
        self.gate = DiscordGate.for_app(
            self.app, DiscordGateSettings(state_root=self.root, channel_id=CHANNEL,
                                          owner_user_id=OWNER, token_file=token_file,
                                          api_base=API, poll_seconds=0.01),
            opener=self.fake, sleep=lambda _s: None, clock=lambda: self.now)

    def state(self) -> dict[str, Any]:
        path = self.root / "discord" / "gate.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_an_item_whose_image_did_not_attach_is_never_approved_from_the_digest(self):
        self.assertTrue(self.gate.run_once())             # today's (empty) digest
        out = self.app.run_task(tig.POST, tig.post(), permissions=[tig.POST])
        self.assertTrue(out.get("batched"), out)
        aid = out["approval_id"]
        self.fake.refuse_files = (403, {"message": "Missing Permissions", "code": 50013})
        self.now = DAY1.replace(hour=9)
        self.gate.run_once()
        [card] = self.state()["digest"]["cards"].values()
        self.assertIn("did not attach", self.fake.content(card["message_id"]))
        preview = self.state()["messages"][aid]
        self.assertTrue(preview["attach_failed"])
        self.fake.react(card["message_id"], NUMBERS[0], OWNER)
        self.fake.react(card["message_id"], APPROVE, OWNER)
        self.fake.react(preview["message_id"], APPROVE, OWNER)
        for _ in range(3):
            self.gate.run_once()
        self.assertEqual(self.app.approvals.get(aid)["status"], "pending")
        self.assertEqual(self.runs, [])
        # the next digest posts it again; with its image attached it can be approved
        self.fake.refuse_files = None
        uploads = len(self.fake.uploads)
        self.now = DAY1.replace(hour=9) + timedelta(days=1)
        self.gate.run_once()
        self.assertEqual(len(self.fake.uploads), uploads + 1)
        again = self.state()["messages"][aid]
        self.assertNotEqual(again["message_id"], preview["message_id"])
        self.assertNotIn("attach_failed", again)
        new = max(self.state()["digest"]["cards"].values(), key=lambda c: c["date"])
        self.assertNotIn("did not attach", self.fake.content(new["message_id"]))
        self.fake.react(new["message_id"], NUMBERS[0], OWNER)
        self.gate.run_once()
        row = self.app.approvals.get(aid)
        self.assertTrue(self.app.jobs.wait(row["task_id"], 30))
        self.assertEqual(self.app.approvals.get(aid)["status"], "approved")
        self.assertEqual(self.runs, [tig.POST])


# ---------------------------------------------------------------- 3. never wedged
class NeverWedgedTests(tab.BatchCase):
    def test_a_refused_supersede_edit_is_given_up_and_the_owner_told(self):
        gate = self.primed()
        first = self.page("First")
        self.at(9, 0)
        gate.run_once()
        [old] = self.cards()
        old_path = f"/channels/{CHANNEL}/messages/{old['message_id']}"

        def refuse(request: Any) -> None:
            if request.get_method() == "PATCH" and request.full_url == API + old_path:
                _refuse(self.fake, request)

        stuck = self.dgate(opener=_Wrap(self.fake, refuse))
        self.at(9, 0, days=1)
        for _ in range(DIGEST_PART_TRIES + 1):
            stuck.run_once()
        state = self.state()["digest"]
        self.assertTrue(state["run"]["complete"])
        self.assertTrue(any("supersede" in p["problem"] for p in state["problems"]))
        alerts = [p for p in self.fake.posts() if "hit a problem" in p["content"]]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["allowed_mentions"]["users"], [OWNER])
        # the old card stopped counting even though its edit never went through
        self.fake.react(old["message_id"], NUMBERS[0], OWNER)
        stuck.run_once()
        self.assertEqual(self.app.approvals.get(first)["status"], "pending")
        # and the digests go on: an early one he asks for is answered
        self.at(10, 0, days=1)
        self.app.request_digest()
        stuck.run_once()
        self.assertTrue(read_request(self.root)["answered"])
        self.assertEqual(self.cards()[-1]["items"], [first])
        self.assertTrue(self.cards()[-1].get("requested"))

    def test_a_refused_expired_report_is_not_repeated_and_does_not_wedge(self):
        gate = self.primed()
        stale = [self.page(f"Stale page {i:02d} " + "with a long title " * 3)
                 for i in range(40)]
        for aid in stale:
            self.expire(aid)

        def refuse(request: Any) -> None:
            # the report's first message goes out; every later one is refused
            if request.get_method() == "POST" and request.data \
                    and json.loads(request.data).get("content", "").startswith("•"):
                _refuse(self.fake, request)

        stuck = self.dgate(opener=_Wrap(self.fake, refuse))
        self.at(9, 0)
        for _ in range(DIGEST_PART_TRIES + 1):
            stuck.run_once()
        heads = [p for p in self.fake.posts() if "Expired, not run" in p["content"]]
        self.assertEqual(len(heads), 1)                   # never posted twice
        state = self.state()["digest"]
        self.assertTrue(state["run"]["complete"])
        self.assertTrue(state["problems"])
        # reported: exactly the rows the posted message named - the rest were never shown
        told = [a for a in stale if f"id `{a}`" in heads[0]["content"]]
        self.assertTrue(0 < len(told) < len(stale))
        self.assertEqual(sorted(state["reported_expired"]), sorted(told))
        self.at(9, 0, days=1)
        fresh = self.page("Fresh")
        gate = self.dgate()
        gate.run_once()
        self.assertEqual(self.cards()[-1]["items"], [fresh])
        # ...so the next digest, with Discord taking it, reports the others - once
        later = "\n".join(p["content"] for p in self.fake.posts()[len(heads):]
                          if "Stale page" in p["content"])
        for aid in stale:
            self.assertEqual(aid in later, aid not in told, aid)
        self.assertEqual(sorted(self.state()["digest"]["reported_expired"]), sorted(stale))

    def test_a_report_whose_first_part_is_given_up_reports_nobody(self):
        gate = self.primed()
        stale = [self.page(f"Stale {i}") for i in range(3)]
        for aid in stale:
            self.expire(aid)

        def refuse(request: Any) -> None:
            if request.get_method() == "POST" and request.data \
                    and "Expired, not run" in json.loads(request.data).get("content", ""):
                _refuse(self.fake, request)

        stuck = self.dgate(opener=_Wrap(self.fake, refuse))
        self.at(9, 0)
        for _ in range(DIGEST_PART_TRIES + 1):
            stuck.run_once()
        state = self.state()["digest"]
        self.assertTrue(state["run"]["complete"])
        self.assertEqual(state["reported_expired"], [])     # none of them was shown
        self.at(9, 0, days=1)
        gate.run_once()
        [report] = [p for p in self.fake.posts() if "Expired, not run" in p["content"]]
        for aid in stale:
            self.assertIn(aid, report["content"])


# ---------------------------------------------------------------- a card not whole
LONG_BODY = " ".join(f"step-{i:04d}" for i in range(900))      # a preview of several messages


def _refuse_continuations(fake: Any, armed: list[bool], once: bool = False):
    def hook(request: Any) -> None:
        # a message continuing a split card reopens its code block
        if armed[0] and request.get_method() == "POST" and request.data \
                and json.loads(request.data).get("content", "").startswith("```"):
            if once:
                armed[0] = False
            _refuse(fake, request)
    return hook


class IncompleteCardTests(tab.BatchCase):
    def test_a_preview_missing_a_part_is_never_approved_and_is_posted_whole_later(self):
        self.primed()
        aid = self.app.run_task(tab.PAGE, {"title": "Long", "body": LONG_BODY})["approval_id"]
        armed = [True]
        stuck = self.dgate(opener=_Wrap(self.fake, _refuse_continuations(self.fake, armed)))
        self.at(9, 0)
        for _ in range(DIGEST_PART_TRIES + 1):
            stuck.run_once()
        preview = self.state()["messages"][aid]
        self.assertIs(preview["complete"], False)
        [card] = self.cards()
        self.assertIn("did not arrive whole", self.fake.content(card["message_id"]))
        self.fake.react(card["message_id"], NUMBERS[0], OWNER)
        self.fake.react(card["message_id"], APPROVE, OWNER)
        self.fake.react(preview["message_id"], APPROVE, OWNER)
        for _ in range(2):
            stuck.run_once()
        self.assertEqual(self.app.approvals.get(aid)["status"], "pending")
        self.assertEqual(self.runs, [])
        armed[0] = False                                  # Discord takes it again
        self.at(9, 0, days=1)
        stuck.run_once()
        again = self.state()["messages"][aid]
        self.assertIs(again["complete"], True)
        self.assertNotEqual(again["message_id"], preview["message_id"])
        new = self.cards()[-1]
        self.assertNotIn("did not arrive whole", self.fake.content(new["message_id"]))
        self.fake.react(new["message_id"], NUMBERS[0], OWNER)
        stuck.run_once()
        self.assertEqual(self.settle(aid)["status"], "approved")
        self.assertEqual(len(self.runs), 1)

    def test_an_individual_card_missing_a_part_is_posted_again_before_any_answer(self):
        self.app.digest = DigestSettings(enabled=False)
        aid = self.park({"content": LONG_BODY})
        armed = [True]
        gate = self.dgate(opener=_Wrap(self.fake, _refuse_continuations(self.fake, armed,
                                                                        once=True)))
        gate.run_once()
        first = self.state()["messages"][aid]
        self.assertIs(first["complete"], False)
        self.fake.react(first["message_id"], APPROVE, OWNER)
        gate.run_once()                                   # posted again, whole
        again = self.state()["messages"][aid]
        self.assertIs(again["complete"], True)
        self.assertNotEqual(again["message_id"], first["message_id"])
        self.assertEqual(self.app.approvals.get(aid)["status"], "pending")
        self.assertEqual(self.runs, [])
        self.fake.react(again["message_id"], APPROVE, OWNER)
        gate.run_once()
        self.assertEqual(self.settle(aid)["status"], "approved")


# ---------------------------------------------------------------- 4. money on listings
class MoneyLineTests(tab.unittest.TestCase):
    def test_any_cents_amount_or_currency_is_money(self):
        for payload in ({"price_cents": 500}, {"amount_cents": 1}, {"deposit_cents": 900},
                        {"refund_total_cents": 2}, {"currency": "eur"},
                        {"payout_currency": "usd"}):
            line = money_line({"capability": "x.post", "payload": payload})
            self.assertIsNotNone(line, payload)
        self.assertIsNone(money_line({"capability": "x.post", "payload": {"title": "t"}}))
        # a key the caller accounted for is not counted again - every other one still is
        self.assertIsNone(money_line({"capability": "x.post", "payload": {"price_cents": 5}},
                                     allowed={"price_cents"}))
        self.assertIsNotNone(money_line({"capability": "x.post",
                                         "payload": {"price_cents": 5, "fee_cents": 1}},
                                        allowed={"price_cents"}))

    def test_a_batchable_item_carrying_money_is_its_own_card(self):
        cap = tab.Capability(name="x.post", description="d", risk=tab.RiskLevel.PRIVILEGED,
                             requires_approval=True, batchable=True)
        for payload in ({"total_cents": 5}, {"currency": "usd"}):
            self.assertIsNotNone(batch_refusal(cap, payload), payload)


class ListingTests(tgp._Case):
    def setUp(self) -> None:
        super().setUp()
        self.app.digest = DigestSettings(enabled=True, at=time(9, 0), expire_days=7)

    def test_a_card_that_says_new_listing_never_updates_one_even_unbatched(self):
        self.app.digest = DigestSettings(enabled=False)
        out = self.park()
        row = self.app.approvals.get(out["approval_id"])
        self.assertFalse(row["batch"])
        self.assertIn("A NEW LISTING", render_request(row, OWNER))
        self.world.add(custom_permalink=tgp.SLUG, published=True, price=500)
        row = self.approve(out)
        self.assertEqual(row["status"], "approved_failed")
        self.assertIn("needs its own card", row["result"]["result"]["refused"])
        self.assertEqual(set(self.world.steps()), {"list"})   # nothing was changed
        self.assertEqual(self.world.product(tgp.SLUG)["price"], 500)

    def test_a_new_listing_waits_for_the_digest(self):
        out = self.park()
        row = self.app.approvals.get(out["approval_id"])
        self.assertTrue(row["batch"])
        self.assertEqual(row["context"], {"listing": "new"})
        self.assertIn("A NEW LISTING", render_request(row, OWNER))

    def test_an_update_of_a_live_listing_is_its_own_card_showing_the_price_change(self):
        self.world.add(custom_permalink=tgp.SLUG, published=True, price=500)
        out = self.park()
        self.assertNotIn("batched", out)
        row = self.app.approvals.get(out["approval_id"])
        self.assertFalse(row["batch"])
        self.assertEqual(row["context"]["listing"], "existing")
        text = render_request(row, OWNER)
        self.assertIn("UPDATES A LIVE LISTING", text)
        self.assertIn("price $5.00 → $19.00", text)
        # an unchanged price is still an update: still its own card
        self.world.product(tgp.SLUG)["price"] = 1900
        again = self.app.approvals.get(self.park()["approval_id"])
        self.assertFalse(again["batch"])
        self.assertIn("price unchanged", render_request(again, OWNER))

    def test_gumroad_unreachable_when_parked_is_its_own_card(self):
        self.world.down = True
        row = self.app.approvals.get(self.park()["approval_id"])
        self.assertFalse(row["batch"])
        self.assertEqual(row["context"]["listing"], "unknown")
        self.assertIn("could not be checked", render_request(row, OWNER))

    def test_a_new_listing_that_exists_by_the_time_it_runs_is_refused_untouched(self):
        out = self.park()
        self.assertTrue(self.app.approvals.get(out["approval_id"])["batch"])
        self.world.add(custom_permalink=tgp.SLUG, published=True, price=500)
        row = self.approve(out)
        self.assertEqual(row["status"], "approved_failed")
        self.assertIn("needs its own card", row["result"]["result"]["refused"])
        self.assertEqual(set(self.world.steps()), {"list"})   # nothing was changed
        self.assertEqual(self.world.product(tgp.SLUG)["price"], 500)

    def test_the_batched_grant_only_restricts(self):
        # the same publish, approved as its own card, may update what exists
        self.world.add(custom_permalink=tgp.SLUG, published=False, price=500)
        out = self.adapter.execute(tab.Task(tgp.PUBLISH, self.payload(),
                                            frozenset({tgp.PUBLISH})))
        self.assertIs(out.output["ok"], True, out.output)
        refused = self.adapter.execute(tab.Task(tgp.PUBLISH, self.payload(),
                                                frozenset({tgp.PUBLISH, BATCHED_GRANT})))
        self.assertIs(refused.output["ok"], False)


# ---------------------------------------------------------------- 5. the request file
class RequestFileTests(tab.unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tab.tempfile.TemporaryDirectory()
        self.root = tab.Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_every_write_has_a_tmp_of_its_own(self):
        seen: list[str] = []
        real = batching.atomic.replace

        def spy(src: Any, dst: Any) -> None:
            seen.append(str(src))
            real(src, dst)

        with mock.patch.object(batching.atomic, "replace", spy):
            first = batching.request_digest(self.root)
            batching.answer_request(self.root, first["id"])
            batching.request_digest(self.root)
        self.assertEqual(len(seen), 3)
        self.assertEqual(len(set(seen)), 3)
        self.assertNotIn(str(batching.request_path(self.root).with_suffix(".tmp")), seen)

    def test_an_answer_never_lands_on_a_newer_request(self):
        first = batching.request_digest(self.root)
        real = batching._write
        results: list[dict[str, Any]] = []
        others: list[threading.Thread] = []

        def slow_write(path: Any, data: Any) -> None:
            if data.get("answered") is True and not others:
                # the owner asks again while the gate is answering the first request
                other = threading.Thread(
                    target=lambda: results.append(batching.request_digest(self.root)))
                others.append(other)
                other.start()
                other.join(0.3)
            real(path, data)

        with mock.patch.object(batching, "_write", slow_write):
            batching.answer_request(self.root, first["id"])
            others[0].join(5)
        [second] = results
        self.assertNotEqual(second["id"], first["id"])      # a request of its own
        current = read_request(self.root)
        self.assertEqual(current["id"], second["id"])
        self.assertFalse(current["answered"])               # still open for the gate


if __name__ == "__main__":
    tab.unittest.main()
