"""Channels replace Hearth's rooms: who can talk, and who hears what was said.

Fails if spatial co-location creeps back in any form: an agent outside a channel
perceiving (or remembering, or learning from) a line said in it, or two agents with no
channel in common managing to start a conversation.
"""

import unittest

from test_crew_kit import Crew, member

from pionir.crew.conversation import shared_channels


class ChannelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.crew = Crew([
            member("ada", ["outreach"]),
            member("bram", ["outreach"]),
            member("cyra", ["prospecting"]),
            member("dee", ["prospecting", "outreach"]),
        ])
        self.addCleanup(self.crew.close)
        self.talk = self.crew.sim.talk

    def heard(self, aid: str) -> list:
        return self.crew[aid].mem.recent(50, kinds=("heard_say",))

    def test_a_line_is_perceived_only_by_members_of_its_channel(self) -> None:
        ada, bram = self.crew["ada"], self.crew["bram"]
        self.assertTrue(self.talk.start(ada, bram, "test", "Speak to Bram."))
        conv = ada.conversation
        self.assertEqual(conv.channel, "outreach")
        self.talk.say(ada, conv, "The pricing page is live now.", "test")
        self.assertEqual(len(self.heard("bram")), 1)            # addressed
        self.assertEqual(len(self.heard("dee")), 1)             # a member, overhearing
        self.assertEqual(self.heard("cyra"), [])                # not a member: nothing
        cyra = self.crew["cyra"].mem
        self.assertEqual(cyra.people(), [])                     # nor met anyone by it
        self.assertEqual(cyra.counter("news_taken_in"), 0)      # nor learned from it
        self.assertEqual(self.crew.sim.store.recent_utterances()[-1]["channel"], "outreach")

    def test_a_line_heard_in_a_channel_is_hearsay_from_its_speaker(self) -> None:
        ada, bram = self.crew["ada"], self.crew["bram"]
        self.talk.start(ada, bram, "test", "Speak to Bram.")
        self.talk.say(ada, ada.conversation, "The pricing page is live now.", "test")
        ep = self.heard("bram")[0]
        self.assertEqual((ep["source"], ep["told_by"], ep["channel"]), ("told", "ada", "outreach"))

    def test_no_shared_channel_means_no_conversation(self) -> None:
        ada, cyra = self.crew["ada"], self.crew["cyra"]
        self.assertEqual(shared_channels(ada, cyra), [])
        self.assertFalse(self.talk.start(ada, cyra, "test", "Speak to Cyra."))
        self.assertIsNone(ada.conversation)
        self.assertIsNone(cyra.conversation)
        self.assertEqual(self.talk.refused_no_channel, 1)
        self.assertEqual(self.crew.sim.brain.waiting(), [])    # no words were even asked for

    def test_the_gate_never_opens_across_channels_however_long_they_sit(self) -> None:
        crew = Crew([member("ada", ["outreach"], sociability=1.0, initiative=1.0, reticence=0.0),
                     member("cyra", ["prospecting"], sociability=1.0, initiative=1.0,
                            reticence=0.0)])
        self.addCleanup(crew.close)
        for a in crew.sim.agents:
            a.drives.value["social"] = 0.0                      # desperate for company
        crew.tick(3000)
        self.assertEqual(crew.sim.talk.started, 0)
        self.assertEqual(crew.sim.store.count_utterances(), 0)

    def test_colleagues_who_share_a_channel_do_get_talking(self) -> None:
        crew = Crew([member("ada", ["outreach"], sociability=1.0, initiative=1.0, reticence=0.0),
                     member("bram", ["outreach"], sociability=1.0, initiative=1.0,
                            reticence=0.0)])
        self.addCleanup(crew.close)
        for a in crew.sim.agents:
            a.drives.value["social"] = 0.0
        crew.tick(3000)
        self.assertGreater(crew.sim.talk.started, 0)            # the gate is not simply shut

    def test_a_conversation_is_held_in_a_channel_both_belong_to(self) -> None:
        dee, cyra = self.crew["dee"], self.crew["cyra"]
        self.assertTrue(self.talk.start(dee, cyra, "test", "Speak to Cyra.",
                                        channel="outreach"))   # cyra is not in #outreach
        self.assertEqual(dee.conversation.channel, "prospecting")


if __name__ == "__main__":
    unittest.main()
