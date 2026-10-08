"""The Fiverr gig-idea proposer: ideas come only from measured demand, a few at a time, none
waiting for ever, never twice after a no, and nothing is priced or posted to Fiverr by it.

Each test fails if the rule it names is reverted: an idea proposed with no measured demand (or from
a stale or unreadable ranking), more than MAX_OPEN ideas open at once or two new ones in one run,
an unanswered idea holding its slot for ever (one idea in 15 runs) or reminded more than once, a
skipped idea proposed again, an idea card that claims the demand is Fiverr search volume or leaves
out the delivery gap, a price set without the owner, a reply read twice, or an idea listing that
overclaims what the tool does.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_fiverr_desk import FakePionir

from pionir.adapters import fiverr as adapter
from pionir.crew.fiverr import checks, ideas
from pionir.crew.fiverr.gigs import check_gig
from pionir.crew.fiverr.ideas import IDEAS, GigIdeaProposer
from pionir.crew.fiverr.prices import Proposal
from pionir.crew.registry import WorkerSpec
from pionir.crew.result import Ok
from pionir.crew.worker import WorkContext

T0 = 1_790_000_000.0
GUARD = checks.Guard(markers=("owner-real-name",))


def row(ref: str, score, calls=54, kind="api"):
    return {"kind": kind, "ref": ref, "name": ref, "score": score,
            "inputs": {"api_calls": {"value": calls}, "api_caller_days": {"value": 9},
                       "guide_views": {"value": 4}}}


class IdeaListingTests(unittest.TestCase):
    def test_every_idea_listing_passes_the_same_honesty_check_as_the_gigs(self) -> None:
        for key, idea in IDEAS.items():
            self.assertEqual(check_gig(idea.service, GUARD), [], key)

    def test_the_email_idea_does_not_claim_to_confirm_a_mailbox(self) -> None:
        svc = IDEAS["emailclean"].service
        desc = svc.description.lower()
        faq = " ".join(a for _, a in svc.faq).lower()
        self.assertIn("confirm that one particular mailbox exists", desc)
        self.assertIn("never send anything", desc)
        self.assertIn("can still bounce", faq)

    def test_no_idea_has_an_ai_set_price(self) -> None:
        for idea in IDEAS.values():
            for rule in idea.service.prices.values():
                self.assertIsInstance(rule, Proposal)


class PickTests(unittest.TestCase):
    def test_nothing_measured_proposes_nothing(self) -> None:
        self.assertIsNone(ideas.pick(None, {}))
        self.assertIsNone(ideas.pick([], {}))
        self.assertIsNone(ideas.pick([row("email", 0), row("qr", None), row("barcode", True)],
                                     {}))

    def test_the_best_measured_idea_wins_and_other_kinds_of_row_do_not_count(self) -> None:
        ranking = [row("email", 104, kind="tool"), row("qr", 53), row("barcode", 84)]
        idea, m = ideas.pick(ranking, {})
        self.assertEqual(idea.service.key, "codes")
        self.assertEqual(m["score"], 84)                    # the better of qr and barcode
        idea, _ = ideas.pick([*ranking, row("email", 104)], {})
        self.assertEqual(idea.service.key, "emailclean")

    def test_a_decided_idea_is_never_picked_again(self) -> None:
        ranking = [row("email", 104), row("barcode", 84)]
        for status in ("proposed", "skipped", "listed"):
            idea, _ = ideas.pick(ranking, {"emailclean": {"status": status}})
            self.assertEqual(idea.service.key, "codes", status)

    def test_the_evidence_says_it_is_not_fiverr_demand(self) -> None:
        idea, m = ideas.pick([row("email", 104)], {})
        line = ideas.evidence_line(idea, m)
        self.assertIn("54 API calls", line)
        self.assertIn("NOT Fiverr search volume", line)


class ProposerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._t = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._t.name)
        (self.root / "secrets").mkdir()
        self.state = self.root / "state"
        self.pionir = FakePionir()
        self.worker = GigIdeaProposer(WorkerSpec("fiverr.ideas", "ideas", "fiverr",
                                                 "fiverr_ideas", "fiverr", 21600, "none"))
        self.rank([row("email", 104), row("barcode", 84)])

    def tearDown(self) -> None:
        self._t.cleanup()

    def rank(self, ranking, computed_at=T0) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "demand.json").write_text(json.dumps(
            {"computed_at": computed_at, "ranking": ranking}), encoding="utf-8")

    def go(self, now=T0):
        return self.worker.run(WorkContext(now=now, http=None, secrets_dir=self.root / "secrets",
                                           job=self.pionir.job, state_dir=self.state,
                                           fiverr_dir=self.root / "fiverr"))

    def reply(self, rid, ref, text) -> None:
        self.pionir.replies.append({"reply_id": rid, "key": f"idea:{ref}:v1", "kind": "idea",
                                    "ref": ref, "text": text, "at": "x"})

    def test_one_idea_card_with_its_evidence_caveats_and_listing(self) -> None:
        self.assertIsInstance(self.go(), Ok)
        self.assertEqual(self.pionir.posted("idea:"), ["idea:emailclean:v1"])
        card = self.pionir.cards["idea:emailclean:v1"][0]
        self.assertEqual(card["kind"], "idea")
        self.assertTrue(card["replies"])
        self.assertEqual(card["files"][0], "gigs/emailclean/gig.md")
        body = card["body"]
        self.assertIn("54 API calls", body)
        self.assertIn("NOT Fiverr search volume", body)
        self.assertIn("no automatic delivery", body)
        self.assertIn("four live today", body)
        self.assertIn("NOT SET", body)                      # no price until he sets one
        self.assertTrue((self.root / "fiverr" / "gigs" / "emailclean" / "gig.md").is_file())

    def test_one_new_idea_per_run_and_an_open_one_is_not_reposted(self) -> None:
        self.go()
        self.assertEqual(self.pionir.posted("idea:"), ["idea:emailclean:v1"])
        self.go(T0 + 30_000)                # a second slot is free: the next measured idea
        self.go(T0 + 60_000)
        self.assertEqual(self.pionir.posted("idea:"), ["idea:emailclean:v1", "idea:codes:v1"])

    def test_skip_drops_the_idea_for_good_and_the_next_one_comes(self) -> None:
        self.go()
        self.reply("2001", "emailclean", "skip")
        self.go(T0 + 100)
        self.assertEqual(self.pionir.posted("idea:"), ["idea:emailclean:v1", "idea:codes:v1"])
        self.go(T0 + 200)
        self.assertEqual(len(self.pionir.posted("idea:")), 2)        # reply read once
        self.reply("2002", "codes", "skip")
        self.go(T0 + 400)
        self.go(T0 + 500)
        self.assertEqual(self.pionir.posted("idea:"), ["idea:emailclean:v1", "idea:codes:v1"])

    def test_never_more_than_max_open_ideas_wait_at_once(self) -> None:
        with mock.patch.object(ideas, "MAX_OPEN", 1):
            self.go()
            self.go(T0 + 30_000)
        self.assertEqual(self.pionir.posted("idea:"), ["idea:emailclean:v1"])

    def test_an_unanswered_idea_is_reminded_once_then_expires_freeing_its_slot(self) -> None:
        """Reverted: one unanswered card holds the only slot for ever."""
        day = 86400
        with mock.patch.object(ideas, "MAX_OPEN", 1):
            self.go()
            self.go(T0 + day)                                    # waiting: nothing new
            self.assertEqual(self.pionir.posted("idea-remind:"), [])
            self.go(T0 + ideas.REMIND_AFTER_SECONDS + 60)        # the one reminder
            self.go(T0 + ideas.REMIND_AFTER_SECONDS + 3600)      # ... and not again
            self.assertEqual(self.pionir.posted("idea-remind:"), ["idea-remind:emailclean:v1"])
            note = self.pionir.cards["idea-remind:emailclean:v1"][0]
            self.assertEqual(note["kind"], "note")
            self.assertIn("only reminder", note["body"])
            self.assertEqual(self.pionir.posted("idea:"), ["idea:emailclean:v1"])
            self.rank([row("email", 104), row("barcode", 84)], computed_at=T0 + 2 * day)
            self.go(T0 + ideas.EXPIRE_AFTER_SECONDS + 60)        # expired: the slot is free
        self.assertEqual(self.pionir.posted("idea:"), ["idea:emailclean:v1", "idea:codes:v1"])
        rec = self.worker.load(self.state)
        self.assertEqual(rec["ideas"]["emailclean"]["status"], "expired")
        self.assertEqual(rec["ideas"]["codes"]["status"], "proposed")
        # no message says it expired, and it is not proposed again by itself
        self.assertEqual(len(self.pionir.posted("idea-remind:")), 1)
        self.go(T0 + 10 * day)
        self.assertEqual(len(self.pionir.posted("idea:emailclean")), 1)

    def test_a_late_reply_on_an_expired_idea_still_counts(self) -> None:
        self.go()
        self.go(T0 + ideas.EXPIRE_AFTER_SECONDS + 60)
        self.assertEqual(self.worker.load(self.state)["ideas"]["emailclean"]["status"],
                         "expired")
        self.reply("2010", "emailclean", "redraft")
        self.go(T0 + ideas.EXPIRE_AFTER_SECONDS + 120)
        self.assertIn("idea:emailclean:v2", self.pionir.cards)
        self.assertEqual(self.worker.load(self.state)["ideas"]["emailclean"]["status"],
                         "proposed")

    def test_a_stale_or_missing_ranking_proposes_nothing(self) -> None:
        self.rank([row("email", 104)], computed_at=T0 - 10 * 86400)
        self.assertIsInstance(self.go(), Ok)
        self.assertEqual(self.pionir.cards, {})
        (self.state / "demand.json").unlink()
        self.assertIsInstance(self.go(), Ok)
        self.assertEqual(self.pionir.cards, {})

    def test_a_price_reply_redrafts_with_the_owners_price_only(self) -> None:
        self.go()
        self.reply("2003", "emailclean", "price 20 40 80")
        self.go(T0 + 60)
        body = self.pionir.cards["idea:emailclean:v2"][0]["body"]
        self.assertIn("$20", body)
        self.assertNotIn("NOT SET", body)
        md = (self.root / "fiverr" / "gigs" / "emailclean" / "gig.md").read_text("utf-8")
        self.assertIn("$80", md)

    def test_an_unreadable_reply_changes_nothing_and_says_so(self) -> None:
        self.go()
        self.reply("2004", "emailclean", "make it cheaper")
        self.go(T0 + 60)
        self.assertNotIn("idea:emailclean:v2", self.pionir.cards)
        self.assertTrue(self.pionir.posted("idea-note:emailclean"))

    def test_listed_is_recorded_and_the_delivery_gap_is_stated(self) -> None:
        self.go()
        self.reply("2005", "emailclean", "listed")
        self.go(T0 + 60)
        note = self.pionir.cards["idea-note:emailclean:2005"][0]["body"]
        self.assertIn("cannot deliver this gig on its own", note)
        self.go(T0 + 30_000)                                 # a listed idea is not re-proposed
        self.assertEqual(self.pionir.posted("idea:"), ["idea:emailclean:v1", "idea:codes:v1"])

    def test_a_card_that_did_not_post_is_retried_not_marked_proposed(self) -> None:
        self.pionir.discord_down = True
        self.go()
        self.assertEqual(self.pionir.cards, {})
        self.pionir.discord_down = False
        self.go(T0 + 60)
        self.assertEqual(self.pionir.posted("idea:"), ["idea:emailclean:v1"])

    def test_no_hands_is_not_configured_not_a_crash(self) -> None:
        out = self.worker.run(WorkContext(now=T0, http=None, secrets_dir=None, job=None,
                                          state_dir=self.state, fiverr_dir=self.root / "fiverr"))
        self.assertNotIsInstance(out, Ok)


class AdapterTests(unittest.TestCase):
    def test_the_idea_card_kind_exists_and_takes_replies(self) -> None:
        self.assertIn("idea", adapter.HEADS)
        self.assertIn("idea", adapter.INBOX_KINDS)
        self.assertIn("posts nothing to Fiverr", adapter.HEADS["idea"])


if __name__ == "__main__":
    unittest.main()
