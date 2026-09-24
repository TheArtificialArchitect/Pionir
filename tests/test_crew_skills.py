"""Skills persist through the agent's own store - the wiring Hearth lost.

Hearth's agent built ``Skills`` and then overwrote it with ``None``; every use
was guarded by ``if self.skills is not None``, so practice was never recorded
and nothing noticed. Hearth's own test drove the class in isolation, which is
exactly why it stayed green. These tests go through the real SQLite store and
back: practice recorded, the store closed, a fresh ``Skills`` built from a
fresh store, and the ability still there - with no explicit save in between,
so they fail if persistence depends on someone remembering to call one.
"""

import unittest
from pathlib import Path

from crew_support import temp_dir

from pionir.crew import log as crewlog
from pionir.crew.memory import Memory
from pionir.crew.skills import Skills, practice_in

T0 = 1_800_000_000


class SkillsPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = temp_dir()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "agents" / "ada.db"

    def test_practice_survives_a_round_trip_through_the_store(self) -> None:
        mem = Memory(self.path, "ada")
        skills = Skills(mem, "ada")
        for i in range(4):
            skills.did("draft_invoice", T0 + i * 60, helped=True, failed=False)
        learned = skills.ability("draft_invoice")
        self.assertGreater(learned, 0.0)
        mem.close()                                   # no save() call: did() must have written

        mem = Memory(self.path, "ada")
        try:
            again = Skills(mem, "ada")
            self.assertEqual(again.practice("draft_invoice"), 4)
            self.assertAlmostEqual(again.ability("draft_invoice"), learned, places=3)
            self.assertGreater(again.affinity("draft_invoice"), 0.0)
            self.assertEqual(practice_in(mem), 4)     # readable from the store alone
            self.assertIn("draft_invoice", again.to_dict())
        finally:
            mem.close()

    def test_a_fresh_agent_starts_with_nothing_recorded(self) -> None:
        mem = Memory(self.path, "ada")
        try:
            self.assertEqual(practice_in(mem), 0)
            self.assertEqual(Skills(mem, "ada").rows, {})
        finally:
            mem.close()

    def test_an_unreadable_record_is_a_lesion_not_a_silent_blank(self) -> None:
        mem = Memory(self.path, "ada")
        try:
            mem.set("skills", ["not", "a", "mapping"])
            before = crewlog.lesions["skills.load.ada"]
            skills = Skills(mem, "ada")
            self.assertEqual(skills.rows, {})
            self.assertEqual(crewlog.lesions["skills.load.ada"], before + 1)
        finally:
            mem.close()


if __name__ == "__main__":
    unittest.main()
