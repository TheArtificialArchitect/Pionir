"""Direction down from Moss, reports up to her: compute shares enforced, no money lever,
a bounded digest.

Each test fails if the rule is reverted: the brain letting a division past the share
Moss allocated, a resource other than compute being allocatable, a goal not reaching the
leader's prompt, the digest growing past its bound, or the digest keeping to its bound
by leaving a division out (a division left out reads as one that does not exist: Moss
never saw treasury, watch or fiverr that way).
"""

import inspect
import json
import time
import unittest

from crew_support import temp_dir
from test_crew_fakes import catalogue, make_crew

from pionir.crew import direction as direction_module
from pionir.crew.api import MIN_DIGEST_CHARS
from pionir.crew.direction import DIGEST_CHARS, RESOURCES, Direction
from pionir.crew.figures import Figure
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


class DivisionsTests(_Case):
    def test_each_worker_lists_the_capabilities_it_uses(self) -> None:
        crew = self.crew(divisions={"alpha": [{"name": "a1", "uses": ["x.read", "x.post"]},
                                              {"name": "a2"}]})
        (alpha,) = crew.direction.divisions()
        # each worker also carries its last output; none of these has run, so it is "never"
        outputs = [w.pop("output") for w in alpha["workers"]]
        self.assertEqual([o["state"] for o in outputs], ["never", "never"])
        # and its dispatch state (control.py): never run, nothing held, so "scheduled"
        dispatch = [w.pop("dispatch") for w in alpha["workers"]]
        self.assertEqual([d["state"] for d in dispatch], ["scheduled", "scheduled"])
        self.assertEqual(alpha["workers"], [
            {"id": "alpha.a1", "live": True, "uses": ["x.read", "x.post"]},
            {"id": "alpha.a2", "live": True, "uses": []}])


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

    def test_act_stands_only_on_a_decision_or_an_unwell_worker(self) -> None:
        # live: Contracts said "order desk stalled" / act for days (a stale "get
        # contracts.orders working again" goal) while every worker was healthy and the only
        # order was declined; the map showed it orange the whole time
        crew = self.crew()
        now = time.time()
        for w, d in (("alpha.a1", "alpha"), ("beta.b1", "beta")):
            crew.store.record_attempt(worker_id=w, division=d, started_at=now - 5,
                                      finished_at=now - 1, error=None, written=1)
        crew.store.add_report(division="alpha", written_at=now, status="report", stamp=now,
                              headline="order desk stalled", summary="s", attention="act")
        crew.store.add_report(division="beta", written_at=now, status="report", stamp=now,
                              headline="a decision", summary="s", attention="act",
                              escalation={"question": "raise the price?"})
        crew.store.add_report(division="gamma", written_at=now, status="report", stamp=now,
                              headline="never ran", summary="s", attention="act")
        got = {e["division"]: e for e in crew.direction.digest(max_chars=100_000)["divisions"]}
        # the same decision again on unchanged facts (leader.py reused its escalation): that
        # was already put to Moss, so it grounds no fresh "act"
        crew.store.add_report(division="beta", written_at=now + 1, status="report", stamp=now,
                              headline="a decision", summary="s", attention="act",
                              escalation={"question": "raise the price?", "answer": None,
                                          "unchanged_since": now - 3600})
        repeated = {e["division"]: e
                    for e in crew.direction.digest(max_chars=100_000)["divisions"]}
        self.assertEqual(repeated["beta"]["attention"], "watch")
        self.assertEqual(got["alpha"]["attention"], "watch")
        self.assertIn("named no decision", got["alpha"]["attention_note"])
        self.assertEqual(got["beta"]["attention"], "act")         # a decision put to Moss
        self.assertEqual(got["gamma"]["attention"], "act")        # its worker never succeeded
        self.assertNotIn("attention_note", got["gamma"])

    def test_a_silent_division_is_shown_not_omitted(self) -> None:
        crew = self.crew()
        entries = crew.direction.digest()["divisions"]
        self.assertEqual({e["division"] for e in entries}, {"alpha", "beta", "gamma"})
        self.assertTrue(all(e["status"] == "silent" for e in entries))


# Seven divisions, as the live crew has: (division, attention, priority). Most urgent first
# is attention (act, watch, none), then priority (1 most).
SEVEN = (("contracts", "act", 1), ("products", "act", 2), ("posting", "watch", 1),
         ("builds", "watch", 3), ("fiverr", "none", 1), ("watch", "none", 2),
         ("treasury", "none", 4))
SEVEN_ORDER = [d for d, _a, _p in SEVEN]
ESSENTIAL = {"division", "status", "attention", "headline", "age_s"}


class EveryDivisionTests(_Case):
    """The bound cuts detail in stages, and never a division."""

    def seven(self):
        crew = self.crew(divisions={d: [{"name": "w1"}] for d, _a, _p in SEVEN})
        now = time.time()
        for d, attention, priority in SEVEN:
            crew.direction.set_goal(d, f"{d} goal " + "g" * 400, priority=priority)
            figs = [Figure(i * 100, "usd_cents" if i == 0 else "count",
                           f"{d} measure number {i}", window="now") for i in range(8)]
            crew.store.add_report(division=d, written_at=now - 60, status="report", stamp=now,
                                  headline=f"{d}: " + "the headline says a lot " * 6,
                                  summary=f"{d} summary " + "word " * 240, attention=attention,
                                  figures=figs, escalation={"answer": "claude " * 100})
        return crew

    def full(self, crew):
        full = crew.direction.digest(max_chars=100_000)
        return full, len(json.dumps(full["divisions"]))

    def assert_every_division(self, got, budget=None):
        self.assertEqual([e["division"] for e in got["divisions"]], SEVEN_ORDER)
        for e in got["divisions"]:
            self.assertEqual(e["status"], "report")
            self.assertIn(e["attention"], ("act", "watch", "none"))
        for d in got["dropped"]:
            self.assertEqual(set(d), {"division", "cut"})       # a cut, never a division
            self.assertIn(d["division"], SEVEN_ORDER)
            self.assertTrue(d["cut"])
        self.assertEqual(got["truncated"], bool(got["dropped"]))
        if budget is not None:
            self.assertLessEqual(len(json.dumps(got["divisions"])), budget)

    def assert_cuts_described(self, full, got):
        """``dropped`` says exactly what differs from the full digest, and nothing else."""
        whole = {e["division"]: e for e in full["divisions"]}
        said = {d["division"]: d["cut"] for d in got["dropped"]}
        for e in got["divisions"]:
            f = whole[e["division"]]
            expect = []
            for k, v in f.items():
                if k == "figures":
                    kept = e.get("figures", [])
                    self.assertEqual(kept, v[:len(kept)])       # the first K, in order
                    if len(v) > len(kept):
                        expect.append(f"figures:{len(v) - len(kept)}")
                elif k not in e:
                    expect.append(k)
                elif k in ("age_s", "as_of_age_s"):
                    continue                                # the clock ticked between reads
                elif e[k] != v:
                    self.assertTrue(e[k].endswith("...") and v.startswith(e[k][:-3]))
                    expect.append(f"{k}:shortened")
            self.assertEqual(sorted(said.get(e["division"], [])), sorted(expect), e["division"])

    def test_a_generous_budget_cuts_nothing(self) -> None:
        full, _size = self.full(self.seven())
        self.assert_every_division(full)
        self.assertEqual(full["dropped"], [])
        self.assertFalse(full["truncated"])
        self.assertEqual({len(e["figures"]) for e in full["divisions"]}, {8})

    def test_every_budget_keeps_every_division_and_says_what_it_cut(self) -> None:
        crew = self.seven()
        full, size = self.full(crew)
        for budget in [*range(MIN_DIGEST_CHARS, size + 200, 97), size, size - 1]:
            with self.subTest(budget=budget):
                got = crew.direction.digest(max_chars=budget)
                self.assert_every_division(got, budget)
                self.assertEqual(got["truncated"], budget < size)
                self.assert_cuts_described(full, got)

    def test_detail_goes_in_stages(self) -> None:
        crew = self.seven()
        _full, size = self.full(crew)
        prose = ("text", "claude", "goal")
        seen = set()
        for budget in range(MIN_DIGEST_CHARS, size, 53):
            with self.subTest(budget=budget):
                got = crew.direction.digest(max_chars=budget)
                es = got["divisions"]
                cuts = {c for d in got["dropped"] for c in d["cut"]}
                if any(c.startswith("figures:") for c in cuts):   # (c) after (b), for all
                    seen.add("c")
                    self.assertFalse(any(k in e for e in es for k in prose))
                if "workers" in cuts:                   # (d) after (c) for all
                    seen.add("d")
                    self.assertTrue(all(len(e.get("figures", [])) <= 1 for e in es))
                if {"headline", "headline:shortened"} & cuts:   # (e) after (d) for all
                    seen.add("e")
                    self.assertTrue(all(set(e) <= ESSENTIAL for e in es))
                if cuts & set(prose):                   # (b) after (a) for all
                    seen.add("b")
                    self.assertTrue(all(len(e[k]) <= 80 for e in es for k in prose if k in e))
        self.assertEqual(seen, {"b", "c", "d", "e"})    # the sweep reached every stage

    def test_the_least_urgent_division_gives_up_detail_first(self) -> None:
        crew = self.seven()
        _full, size = self.full(crew)
        got = crew.direction.digest(max_chars=size - 1)
        self.assertEqual(got["dropped"], [{"division": "treasury", "cut": ["text:shortened"]}])

    def test_figures_are_cut_to_the_first_k(self) -> None:
        crew = self.seven()
        full, _size = self.full(crew)
        whole = {e["division"]: e["figures"] for e in full["divisions"]}
        for budget in range(MIN_DIGEST_CHARS, 6000, 131):
            for e in crew.direction.digest(max_chars=budget)["divisions"]:
                kept = e.get("figures")
                if kept is not None:
                    self.assertEqual(kept, whole[e["division"]][:len(kept)])
                    self.assertTrue(kept[0].startswith("$0.00"))  # the leader's first stays

    def test_the_minimum_budget_names_every_division(self) -> None:
        got = self.seven().direction.digest(max_chars=MIN_DIGEST_CHARS)
        self.assert_every_division(got, MIN_DIGEST_CHARS)
        for d in got["dropped"]:
            self.assertIn("text", d["cut"])
            self.assertIn("figures:8", d["cut"])

    def test_a_budget_too_small_even_for_the_floor_still_names_every_division(self) -> None:
        got = self.seven().direction.digest(max_chars=10)   # the API refuses it; the floor holds
        self.assert_every_division(got)
        for e in got["divisions"]:
            self.assertEqual(set(e), {"division", "status", "attention"})
        self.assertTrue(got["truncated"])
        self.assertEqual({d["division"] for d in got["dropped"]}, set(SEVEN_ORDER))
        self.assertLessEqual(len(json.dumps(got["divisions"])), MIN_DIGEST_CHARS)

    def test_the_default_holds_seven_full_divisions(self) -> None:
        """The live seven measured 7,221 characters at full detail; the default is above it
        with headroom, so Moss's hourly read (no budget given) sees every division whole."""
        self.assertGreaterEqual(DIGEST_CHARS, 7_221 * 1.3)


if __name__ == "__main__":
    unittest.main()
