"""Direction down from Moss, reports up to her: compute shares enforced, no money lever,
a bounded digest.

Each test fails if the rule is reverted: the brain letting a division past the share
Moss allocated, a resource other than compute being allocatable, a goal not reaching the
leader's prompt, or the digest growing past its bound.
"""

import inspect
import json
import time
import unittest

from crew_support import temp_dir
from test_crew_fakes import catalogue, make_crew

from pionir.crew import direction as direction_module
from pionir.crew.direction import RESOURCES, Direction
from pionir.crew.leader import Leader

DIVISIONS = {"alpha": [{"name": "a1"}], "beta": [{"name": "b1"}], "gamma": [{"name": "c1"}]}


class _Case(unittest.TestCase):
    def crew(self, divisions=DIVISIONS, **kw):
        tmp = temp_dir()
        self.addCleanup(tmp.cleanup)
        crew = make_crew(tmp.name, cat=catalogue(divisions), **kw)
        self.addCleanup(crew.stop)
        return crew

    def ask(self, crew, division):
        return crew.brain.request(f"leader.{division}", "distil", "s", "u", {},
                                  lambda *a: None, division=division)


class ComputeAllocationTests(_Case):
    def test_a_divisions_share_of_the_brain_is_enforced(self) -> None:
        crew = self.crew(calls_per_hour=20)
        caps = crew.direction.allocate("model_calls", {"alpha": 0.1})
        self.assertEqual(caps["caps"]["alpha"], 2)
        self.assertEqual(caps["caps"]["beta"], 9)             # the rest, split evenly
        crew.store.add_call(0, "leader.alpha", "distil", 1, 1, 0.1, True, division="alpha")
        self.assertIsNotNone(self.ask(crew, "alpha"))         # 1 used + this one = 2
        self.assertIsNone(self.ask(crew, "alpha"))            # its share is spent
        self.assertEqual(crew.brain.throttled_by["alpha"], 1)
        self.assertIn("alpha's share", crew.brain.last_refusal)
        self.assertIsNotNone(self.ask(crew, "beta"))          # another division is unaffected

    def test_queued_calls_count_against_the_share(self) -> None:
        crew = self.crew(calls_per_hour=10)
        crew.direction.allocate("model_calls", {"alpha": 0.2})
        self.assertIsNotNone(self.ask(crew, "alpha"))
        self.assertIsNotNone(self.ask(crew, "alpha"))
        self.assertIsNone(self.ask(crew, "alpha"))            # two waiting already = its cap

    def test_until_moss_allocates_the_ceiling_is_pooled(self) -> None:
        crew = self.crew(calls_per_hour=3)
        self.assertEqual(crew.direction.compute()["model_calls"]["caps"]["alpha"], 3)
        for _ in range(3):
            self.assertIsNotNone(self.ask(crew, "alpha"))
        self.assertIsNone(self.ask(crew, "alpha"))

    def test_bad_allocations_are_refused(self) -> None:
        crew = self.crew()
        for resource, shares in (("model_calls", {"alpha": 0.7, "beta": 0.5}),
                                 ("model_calls", {"nope": 0.1}),
                                 ("model_calls", {"alpha": -0.1}),
                                 ("model_calls", {"alpha": True}),
                                 ("model_calls", {})):
            with self.assertRaises(ValueError, msg=shares):
                crew.direction.allocate(resource, shares)


class NoMoneyTests(_Case):
    def test_only_compute_can_be_allocated(self) -> None:
        self.assertEqual(set(RESOURCES), {"model_calls", "claude_escalations"})
        crew = self.crew()
        for resource in ("money", "usd", "usd_cents", "spend", "budget", "ad_spend", "revenue"):
            with self.assertRaises(ValueError) as caught:
                crew.direction.allocate(resource, {"alpha": 0.5})
            self.assertIn("no money lever", str(caught.exception))

    def test_mosss_api_has_no_money_lever(self) -> None:
        money = ("money", "spend", "pay", "usd", "dollar", "cash", "purchase", "buy", "fund")
        public = [n for n, _ in inspect.getmembers(Direction) if not n.startswith("_")]
        self.assertEqual(sorted(public), ["allocate", "compute", "digest", "divisions",
                                          "reports", "set_goal"])
        for name in public + list(RESOURCES):
            self.assertFalse(any(m in name.lower() for m in money), name)
        # and the package has no module that could move money
        src = inspect.getsource(direction_module)
        self.assertNotIn("stripe", src.lower())


class GoalTests(_Case):
    def test_a_goal_reaches_the_leaders_prompt(self) -> None:
        crew = self.crew()
        crew.direction.set_goal("alpha", "Find out why sales stalled.", priority=1)
        crew.dispatcher.dispatch(only=["alpha.a1"], wait=True)
        seen: list = []

        def ask(agent_id, purpose, messages, options, *, fmt=None, division=None):
            seen.append(messages[1]["content"])
            return None, {}, "refused: test"

        Leader("alpha", crew.registry, crew.store, ask=ask).run()
        self.assertIn("GOAL from Moss (priority 1): Find out why sales stalled.", seen[0])

    def test_goal_validation(self) -> None:
        crew = self.crew()
        for division, goal, priority in (("nope", "x", 3), ("alpha", "", 3),
                                         ("alpha", "x", 0), ("alpha", "x" * 501, 3)):
            with self.assertRaises(ValueError):
                crew.direction.set_goal(division, goal, priority=priority)


class DigestTests(_Case):
    def test_the_digest_is_bounded_and_most_urgent_first(self) -> None:
        crew = self.crew()
        now = time.time()
        for d, attention in (("alpha", "none"), ("beta", "act"), ("gamma", "watch")):
            crew.store.add_report(division=d, written_at=now, status="report", stamp=now,
                                  headline=f"{d} headline", summary="word " * 400,
                                  attention=attention)
        full = crew.direction.digest(max_chars=100_000)
        self.assertEqual([e["division"] for e in full["divisions"]], ["beta", "gamma", "alpha"])
        small = crew.direction.digest(max_chars=1200)
        self.assertLessEqual(len(json.dumps(small["divisions"])), 1200)
        self.assertTrue(small["truncated"])

    def test_a_silent_division_is_shown_not_omitted(self) -> None:
        crew = self.crew()
        entries = crew.direction.digest()["divisions"]
        self.assertEqual({e["division"] for e in entries}, {"alpha", "beta", "gamma"})
        self.assertTrue(all(e["status"] == "silent" for e in entries))


if __name__ == "__main__":
    unittest.main()
