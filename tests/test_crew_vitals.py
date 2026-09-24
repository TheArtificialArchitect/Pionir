"""Does the inertness detector itself actually detect anything?

A checker that never fires looks exactly like a healthy crew. So this drives
the real Vitals class with stand-in agents whose counters we control, and
asserts the warning is RAISED for the inert one, NOT raised for the working
one, and CLEARS the moment the inert one produces - because a check that
cannot clear is the same one-way latch in different clothing. The skill check
runs against a real store, so an agent whose skills were never wired (Hearth's
bug) is caught.
"""

import unittest
from pathlib import Path

from crew_support import temp_dir

from pionir.crew.memory import Memory
from pionir.crew.skills import Skills
from pionir.crew.vitals import REPERTOIRE_AFTER_SECONDS, Vitals

HEALTHY = {"actions_done": 200, "changes_noticed": 50, "thoughts": 60,
           "intentions_formed": 10, "utterances": 12}


class _Mem:
    def __init__(self, counters, people, skills_practice=5) -> None:
        self.c = dict(counters)
        self.p = [dict(x) for x in people]
        self.kv = {"skills": {"draft": {"practice": skills_practice}}}

    def counter(self, k):
        return self.c.get(k, 0)

    def people(self):
        return self.p

    def get(self, k, default=None):
        return self.kv.get(k, default)


class _Affect:
    def __init__(self, pushes) -> None:
        self.pushes = pushes


class _Agent:
    def __init__(self, aid, counters, addressed=9, pushes=300, mem=None) -> None:
        self.id = aid
        self.name = aid.title()
        self.mem = mem or _Mem(counters, [{"addressed": addressed}])
        self.affect = _Affect(pushes)


class _Clock:
    t = 1_800_000_000


class _Sim:
    def __init__(self, agents, age=0.0) -> None:
        self.agents = agents
        self.clock = _Clock()
        self.age = age

    def agent(self, aid):
        return next((a for a in self.agents if a.id == aid), None)

    def age_seconds(self):
        return self.age


class VitalsTests(unittest.TestCase):
    def test_a_mute_agent_is_reported_the_talker_is_not_and_it_clears(self) -> None:
        mute = _Agent("mute", dict(HEALTHY, utterances=0))
        talker = _Agent("talker", HEALTHY)
        v = Vitals(_Sim([mute, talker]))
        self.assertEqual(v.check(force=True), [{"who": "Mute", "check": "speech"}])
        self.assertEqual(v.raised, 1)
        v.check(force=True)
        self.assertEqual(v.raised, 1)                 # said once, not every check
        mute.mem.c["utterances"] = 1
        self.assertEqual(v.check(force=True), [])
        self.assertEqual(v.cleared, 1)

    def test_a_young_agent_is_not_called_broken(self) -> None:
        young = _Agent("young", {"utterances": 0}, addressed=2, pushes=0)
        v = Vitals(_Sim([young]))
        self.assertEqual(v.check(force=True), [])

    def test_every_check_fires_on_its_own_silence(self) -> None:
        cases = {
            "thought": dict(HEALTHY, thoughts=0, intentions_formed=0),
            "feeling": HEALTHY,
            "intention": dict(HEALTHY, intentions_formed=0),
        }
        for check, counters in cases.items():
            agent = _Agent("x", counters, pushes=0 if check == "feeling" else 300)
            found = {c["check"] for c in Vitals(_Sim([agent])).check(force=True)}
            self.assertIn(check, found)

    def test_an_agent_whose_skills_were_never_wired_is_caught_and_clears(self) -> None:
        with temp_dir() as root:
            mem = Memory(Path(root) / "ada.db", "ada")
            try:
                mem.bump("actions_done", 100)
                for k, n in HEALTHY.items():
                    if k != "actions_done":
                        mem.bump(k, n)
                ada = _Agent("ada", {}, mem=mem)
                mem.save_person({"other": "ian", "warmth": 0, "trust": 0, "familiarity": 0,
                                 "grievance": 0, "addressed": 9})
                v = Vitals(_Sim([ada]))
                self.assertEqual(v.check(force=True), [{"who": "Ada", "check": "skill"}])
                Skills(mem, "ada").did("draft", _Clock.t, helped=True, failed=False)
                self.assertEqual(v.check(force=True), [])
            finally:
                mem.close()

    def test_an_action_nobody_ever_does_is_reported_once_old_enough(self) -> None:
        a = _Agent("a", dict(HEALTHY, **{"action.triage": 4}))
        sim = _Sim([a], age=10.0)
        v = Vitals(sim, repertoire=lambda: ["triage", "rearrange"])
        v.check(force=True)
        self.assertEqual(v.dead_actions, set())       # too young to tell
        sim.age = REPERTOIRE_AFTER_SECONDS + 1
        v.check(force=True)
        self.assertEqual(v.dead_actions, {"rearrange"})
        a.mem.c["action.rearrange"] = 1
        v.check(force=True)
        self.assertEqual(v.dead_actions, set())


if __name__ == "__main__":
    unittest.main()
