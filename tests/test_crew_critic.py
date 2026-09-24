"""The critic: nothing an agent says may assert what its own store does not hold.

Each test fails if the check it names is reverted: an own claim with nothing behind it,
a figure nobody showed the agent (the one that would hurt most - confabulated money or
results), a name it never came across, a colleague it never met. And the reverse: the
business vocabulary Hearth's OUTSIDE regex banned (work, job, money, office, weekdays)
now passes, because it is the whole job.
"""

import unittest

from test_crew_kit import Crew, member


class CriticTests(unittest.TestCase):
    def setUp(self) -> None:
        self.crew = Crew([member("ada", ["outreach"]), member("bram", ["outreach"]),
                          member("cyra", ["prospecting"])])
        self.addCleanup(self.crew.close)
        self.ada = self.crew["ada"]
        self.critic = self.crew.sim.talk.critic

    def ep(self, kind, text, source, **kw):
        return self.ada.mem.add_episode(self.crew.t, "outreach", kind, text, source=source, **kw)

    # ---- own claims ----------------------------------------------------------
    def test_an_unbacked_own_claim_is_dropped(self) -> None:
        self.assertIsNone(self.critic(self.ada, "I sent the follow-up emails this morning."))
        self.assertEqual(self.ada.mem.counter("false_own_claim"), 1)

    def test_an_own_claim_backed_by_work_pionir_ran_is_kept(self) -> None:
        self.ep("did", "you had Pionir send the follow-up emails (outreach.send) and it ran",
                "did")
        self.assertEqual(self.critic(self.ada, "I sent the follow-up emails this morning."),
                         "I sent the follow-up emails this morning.")

    def test_a_job_waiting_on_approval_does_not_back_i_sent_it(self) -> None:
        self.ep("job_pending", "asked Pionir to send the follow-up emails (outreach.send); it "
                               "is waiting on Ian's approval and has NOT run", "did")
        self.ep("tried", "tried to send the follow-up emails (outreach.send) but it failed",
                "did")
        self.assertIsNone(self.critic(self.ada, "I sent the follow-up emails."))

    # ---- figures -------------------------------------------------------------
    def test_an_unbacked_figure_is_dropped(self) -> None:
        self.assertIsNone(self.critic(self.ada, "We made $470 this week."))
        self.assertIsNone(self.critic(self.ada, "That got us 12 replies."))
        self.assertEqual(self.ada.mem.counter("unbacked_figure"), 2)

    def test_a_figure_matching_a_real_seen_result_is_kept(self) -> None:
        self.ep("result", "Pionir reported for read the ledger: revenue this week $470.00",
                "seen")
        self.assertEqual(self.critic(self.ada, "We made $470 this week."),
                         "We made $470 this week.")

    def test_a_structured_figure_from_a_result_backs_a_claim(self) -> None:
        self.ep("result", "Pionir reported for count replies: done", "seen",
                detail={"figures": [12.0]})
        self.assertIsNotNone(self.critic(self.ada, "That got us 12 replies."))

    def test_a_figure_only_done_or_told_is_not_a_seen_figure(self) -> None:
        # did: an intent carrying a number is not a result; told: hearsay stated as fact
        self.ep("did", "you had Pionir send 30 emails (outreach.send) and it ran", "did")
        self.ep("heard_say", 'Bram said to me in #outreach: "we got 12 replies"', "told",
                told_by="bram")
        self.assertIsNone(self.critic(self.ada, "We got 12 replies."))
        self.assertIsNone(self.critic(self.ada, "Great, 30 emails went out."))

    def test_hearsay_figure_passes_only_when_said_as_hearsay(self) -> None:
        self.ada.meet(self.crew["bram"], "outreach")
        self.ep("heard_say", 'Bram said to me in #outreach: "we got 12 replies"', "told",
                told_by="bram")
        self.assertIsNotNone(self.critic(self.ada, "Bram said we got 12 replies."))
        self.assertIsNone(self.critic(self.ada, "Bram said we got 40 replies."))
        self.assertEqual(self.ada.mem.counter("unbacked_figure"), 1)

    def test_durations_and_clock_times_are_not_result_claims(self) -> None:
        self.assertIsNotNone(self.critic(self.ada, "I will look again in 2 hours, around 14:30."))

    # ---- vocabulary ------------------------------------------------------------
    def test_business_words_are_no_longer_dropped(self) -> None:
        line = ("The money from this job is the work that matters; the office opens on Monday "
                "and I want the pricing ready.")
        self.assertEqual(self.critic(self.ada, line), line)

    # ---- names -----------------------------------------------------------------
    def test_an_unmet_capitalised_entity_is_dropped(self) -> None:
        self.assertIsNone(self.critic(self.ada, "We should pitch it to Acme next."))
        self.assertEqual(self.ada.mem.counter("unknown_entity"), 1)

    def test_an_entity_the_agent_actually_encountered_is_kept(self) -> None:
        self.ep("result", "Pionir reported for check sales: one sale on Gumroad", "seen")
        self.assertIsNotNone(self.critic(self.ada, "We should list it on Gumroad again."))

    def test_a_name_only_in_the_agents_own_speech_does_not_vouch_for_itself(self) -> None:
        self.ep("said", 'said to Bram in #outreach: "we should pitch it to Acme"', "did")
        self.assertIsNone(self.critic(self.ada, "We should pitch it to Acme next."))

    def test_an_unmet_colleague_name_is_dropped_and_passes_once_met(self) -> None:
        cyra = self.crew["cyra"]
        self.assertIsNone(self.critic(self.ada, "Maybe Cyra has seen the list."))
        self.assertEqual(self.ada.mem.counter("unmet_name"), 1)
        self.ada.meet(cyra, "outreach")
        self.assertIsNotNone(self.critic(self.ada, "Maybe Cyra has seen the list."))


if __name__ == "__main__":
    unittest.main()
