"""The crew member: purpose, skills, the cast as data, temperament as behaviour, and a
crew with no real work saying so.

Each test fails if the behaviour it names is reverted: purpose relieved by a step that
changed nothing (or by anything but progress), skills unwired the way Hearth unwired
them, a cast that is code rather than data, temperaments that differ only in words, or
an idle crew that stays quiet about it.
"""

import json
import unittest
from dataclasses import replace
from pathlib import Path

from crew_support import temp_dir
from test_crew_kit import Crew, SendKind, World, done, member

from pionir.crew import projects
from pionir.crew.actions import BY_NAME
from pionir.crew.cast import CAST_PATH, load_cast
from pionir.crew.drives import Drives
from pionir.crew.memory import Memory
from pionir.crew.projects import Kind, Project
from pionir.crew.skills import Skills, practice_in


class PurposeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = World(target=10)
        self.kind = SendKind(self.world)
        self.crew = Crew([member("ada", ["outreach"])], kinds=[self.kind])
        self.addCleanup(self.crew.close)
        self.ada = self.crew["ada"]
        self.crew.give_project(self.ada, self.kind)
        self.ada.drives.value["purpose"] = 0.2

    def test_a_step_that_changed_nothing_relieves_nothing(self) -> None:
        before = self.ada.drives.value["purpose"]
        self.ada.note_project_step(self.crew.sim, 0.3, 0.3, "tried the list again")
        self.assertEqual(self.ada.drives.value["purpose"], before)
        self.assertEqual(self.ada.mem.counter("project_steps"), 0)
        self.assertEqual(self.ada.mem.counter("project_steps_empty"), 1)
        self.assertEqual(self.ada.mem.recent(5, kinds=("did_own",)), [])

    def test_real_progress_relieves_purpose(self) -> None:
        before = self.ada.drives.value["purpose"]
        self.crew.tick(5)
        self.ada.note_project_step(self.crew.sim, 0.3, 0.4, "sent one follow-up")
        self.assertGreater(self.ada.drives.value["purpose"], before)
        self.assertEqual(self.ada.project.last_progress_t, self.crew.t)
        self.assertEqual(len(self.ada.mem.recent(5, kinds=("did_own",))), 1)

    def test_purpose_has_no_other_door(self) -> None:
        d = Drives(self.ada.t)
        with self.assertRaises(ValueError):
            d.apply({"purpose": 0.1})
        d.apply({"social": 0.1, "stimulation": 0.1})        # the others are relieved normally

    def test_progress_is_read_from_the_world_every_time(self) -> None:
        self.assertEqual(self.ada.project_progress(self.crew.sim), 0.0)
        self.world.sent = 5                                   # the world moves, not the project
        self.assertEqual(self.ada.project_progress(self.crew.sim), 0.5)
        self.assertNotIn("progress", self.ada.project.to_dict())

    def test_a_stale_project_is_given_up_after_real_time_without_progress(self) -> None:
        self.crew.time.advance(self.crew.cfg.project_stale_seconds + 1)
        self.ada.maybe_take_something_on(self.crew.sim)
        self.assertIsNone(self.ada.project)
        self.assertEqual(self.ada.mem.counter("projects_abandoned"), 1)
        self.assertGreater(projects.sourness(self.ada, self.kind.key, self.crew.t), 0.9)


class SkillsWiringTests(unittest.TestCase):
    """Hearth built Skills and then set it to None; nothing noticed for the life of the
    house. Here it is built once and a real tick practises it, into the store."""

    def test_skills_are_built_once_and_practice_persists_through_a_real_tick(self) -> None:
        world = World()
        kind = SendKind(world)

        def answer(payload):
            world.sent += 1
            return done({"answer": "sent"})

        crew = Crew([member("ada", ["outreach"])], kinds=[kind],
                    answers={"outreach.send": answer})
        ada = crew["ada"]
        skills = ada.skills
        self.assertIsInstance(skills, Skills)
        crew.give_project(ada, kind)
        crew.work_one_step(ada)
        crew.tick(3)
        self.assertIs(ada.skills, skills)                     # never replaced
        self.assertGreater(ada.skills.practice("work_on"), 0)
        self.assertGreater(practice_in(ada.mem), 0)           # written through to the store
        path = ada.mem.path
        crew.sim.stop()                                       # closes the store for real
        mem = Memory(Path(path), "ada")
        try:
            self.assertGreater(Skills(mem, "ada").practice("work_on"), 0)
        finally:
            mem.close()
            crew.close()

    def test_the_hearth_pattern_is_not_in_the_agent(self) -> None:
        src = (Path(projects.__file__).parent / "agent.py").read_text(encoding="utf-8")
        self.assertNotIn("skills is not None", src)
        self.assertNotIn("self.skills = None", src)


class CastTests(unittest.TestCase):
    def test_the_cast_loads_from_data(self) -> None:
        cast = {m.id: m for m in load_cast()}
        self.assertEqual(set(cast), {"scrooge", "skopos"})
        self.assertEqual(cast["scrooge"].channels, ("outreach", "ops"))
        self.assertEqual(cast["skopos"].channels, ("prospecting", "outreach"))
        self.assertNotIn("moss", cast)                        # the task master is not crew

    def test_adding_an_agent_is_adding_an_entry(self) -> None:
        doc = json.loads(CAST_PATH.read_text(encoding="utf-8"))
        extra = json.loads(json.dumps(doc["agents"][0]))
        extra.update(id="hermes", name="Hermes", channels=["content"])
        doc["agents"].append(extra)
        with temp_dir() as root:
            path = Path(root) / "cast.json"
            path.write_text(json.dumps(doc), encoding="utf-8")
            cast = {m.id: m for m in load_cast(path)}
        self.assertEqual(cast["hermes"].channels, ("content",))

    def test_a_number_no_code_reads_is_refused(self) -> None:
        doc = json.loads(CAST_PATH.read_text(encoding="utf-8"))
        doc["agents"][0]["temperament"]["sleep_depth"] = 0.5
        with temp_dir() as root:
            path = Path(root) / "cast.json"
            path.write_text(json.dumps(doc), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_cast(path)


class _Stall(Kind):
    """Never done, never moves: work_on is always on offer."""
    key = "stall"
    title = "a long job"

    def progress(self, agent, crew) -> float:
        return 0.0


class _Suits(Kind):
    def __init__(self, key, fn) -> None:
        self.key = key
        self.title = key
        self._fn = fn

    def suits(self, agent) -> float:
        return self._fn(agent.t)

    def progress(self, agent, crew) -> float:
        return 0.0


class TemperamentTests(unittest.TestCase):
    """Scrooge and Skopos differ in numbers, and the numbers differ in what they do."""

    def setUp(self) -> None:
        self.crew = Crew(load_cast(), kinds=[_Stall()])
        self.addCleanup(self.crew.close)

    def work_share(self, agent, n: int = 3000) -> float:
        sim = self.crew.sim
        agent.project = Project("stall", self.crew.t)
        works = 0
        for _ in range(n):
            agent.drives.value["purpose"] = 0.3             # the same deficit for both
            agent._choose(sim)
            works += agent.action_name == "work_on"
            agent.action = agent.gen = agent.action_name = None
        return works / n

    def test_the_two_temperaments_choose_measurably_differently(self) -> None:
        scrooge = self.work_share(self.crew["scrooge"])
        skopos = self.work_share(self.crew["skopos"])
        # Scrooge's initiative weighs unmet purpose more heavily and a lower impulsivity
        # makes the choice sharper; Skopos is likelier to sit and watch
        self.assertGreater(scrooge - skopos, 0.05)

    def test_impulsivity_alone_loosens_the_choice(self) -> None:
        # The comparison above cannot catch a broken choice temperature: Scrooge
        # and Skopos also differ in initiative, which separates them on its own,
        # and a mutation fixing the temperature to a constant survived it. So
        # here nothing moves but impulsivity - the SAME agent, the same deficit,
        # the same offer. If impulsivity stopped setting the temperature, the two
        # shares would be equal and this would fail.
        agent = self.crew["scrooge"]
        base = agent.t
        try:
            agent.t = replace(base, impulsivity=0.0)
            steady = self.work_share(agent)
            agent.t = replace(base, impulsivity=1.0)
            rash = self.work_share(agent)
        finally:
            agent.t = base
        self.assertGreater(steady - rash, 0.1)

    def test_the_same_offer_draws_them_to_different_work(self) -> None:
        sim = self.crew.sim
        sim.kinds = [_Suits("research", lambda t: t.curiosity * t.perception),
                     _Suits("outreach", lambda t: t.initiative * t.order_sensitivity)]
        picks: dict = {}
        for aid in ("scrooge", "skopos"):
            a = self.crew[aid]
            picks[aid] = sum(projects.choose(a, sim).key == "research" for _ in range(2000))
        self.assertGreater(picks["skopos"], 1400)
        self.assertLess(picks["scrooge"], 800)

    def test_drives_are_set_by_the_numbers(self) -> None:
        sc, sk = self.crew["scrooge"].drives, self.crew["skopos"].drives
        self.assertGreater(sc.weight["purpose"], sk.weight["purpose"])
        self.assertGreater(sc.weight["social"], sk.weight["social"])
        self.assertGreater(sk.weight["stimulation"], sc.weight["stimulation"])


class LoudWhenIdleTests(unittest.TestCase):
    """A crew with no real work must be LOUD, not silently idle."""

    def test_no_achievable_project_is_reported_and_clears_when_one_is_taken(self) -> None:
        crew = Crew([member("ada", ["outreach"])], kinds=[])
        self.addCleanup(crew.close)
        ada = crew["ada"]
        for _ in range(6):
            ada.drives.value["purpose"] = 0.0
            ada.maybe_take_something_on(crew.sim)
        self.assertIn({"who": "Ada", "check": "project"}, crew.sim.vitals.check(force=True))
        crew.sim.kinds = [_Stall()]
        ada.maybe_take_something_on(crew.sim)
        self.assertIsNotNone(ada.project)
        self.assertNotIn({"who": "Ada", "check": "project"},
                         crew.sim.vitals.check(force=True))

    def test_an_agent_that_only_ever_idles_is_reported_by_real_ticks(self) -> None:
        crew = Crew([member("ada", ["outreach"])], kinds=[])
        self.addCleanup(crew.close)
        crew.tick(260, seconds=121)                           # each tick ends one idle spell
        ada = crew["ada"]
        self.assertGreaterEqual(ada.mem.counter("choices"), 200)
        self.assertEqual(ada.mem.counter("actions_done"), 0)   # idling is not doing
        report = crew.sim.vitals.check(force=True)
        self.assertIn({"who": "Ada", "check": "work"}, report)
        self.assertIn({"who": "Ada", "check": "project"}, report)

    def test_real_work_clears_the_idle_warning(self) -> None:
        world = World()
        kind = SendKind(world)

        def answer(payload):
            world.sent += 1
            return done({"answer": "sent"})

        crew = Crew([member("ada", ["outreach"])], kinds=[kind],
                    answers={"outreach.send": answer})
        self.addCleanup(crew.close)
        ada = crew["ada"]
        ada.mem.bump("choices", 250)
        self.assertIn({"who": "Ada", "check": "work"}, crew.sim.vitals.check(force=True))
        crew.give_project(ada, kind)
        crew.work_one_step(ada)
        self.assertNotIn({"who": "Ada", "check": "work"}, crew.sim.vitals.check(force=True))

    def test_the_idle_action_is_honest_about_itself(self) -> None:
        self.assertIn("stay", BY_NAME)


if __name__ == "__main__":
    unittest.main()
