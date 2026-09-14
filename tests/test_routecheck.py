"""Tests for the routing aim measurement.

A check that reports 12/12 is worth nothing until it has been shown to report
something else, so most of these assert that it catches a router behaving
badly rather than that it agrees with one behaving well.
"""

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from support import offline_scheduler

from pionir import routecheck
from pionir.contracts import AgentManifest, Capability, Task, TaskResult
from pionir.routecheck import ASK, Probe, ProbeResult, RoutingCheck
from pionir.router import IntentRouter, RoutingDecision
from pionir.runtime import Executive


class _Adapter:
    def __init__(self, manifest: AgentManifest) -> None:
        self._manifest = manifest

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def execute(self, task: Task) -> TaskResult:
        return TaskResult(task.task_id, self._manifest.agent_id, {})


def _router() -> IntentRouter:
    executive = Executive(scheduler=offline_scheduler())
    executive.register(
        _Adapter(
            AgentManifest(
                "bryo",
                "1",
                (
                    Capability(
                        name="organism.bryo_status",
                        description="Read Bryo's vitals",
                        routing_hints=frozenset({"bryo", "organism"}),
                    ),
                ),
            )
        )
    )
    executive.register(
        _Adapter(
            AgentManifest(
                "genesis",
                "1",
                (
                    Capability(
                        name="organism.genesis_status",
                        description="Read Genesis counters",
                        routing_hints=frozenset({"genesis", "organism"}),
                    ),
                ),
            )
        )
    )
    return IntentRouter(executive)


def _result(expected: str, actual: str) -> ProbeResult:
    return ProbeResult("r", expected, actual, 1.0, routecheck._outcome(expected, actual))


class OutcomeTests(unittest.TestCase):
    def test_names_the_four_outcomes_apart(self) -> None:
        self.assertEqual(routecheck._outcome("a", "a"), "correct")
        self.assertEqual(routecheck._outcome("a", "b"), "misroute")
        self.assertEqual(routecheck._outcome("a", ASK), "over_ask")
        self.assertEqual(routecheck._outcome(ASK, "b"), "under_ask")

    def test_asking_when_asking_was_right_is_correct_not_a_failure(self) -> None:
        self.assertEqual(routecheck._outcome(ASK, ASK), "correct")


class VerdictTests(unittest.TestCase):
    def _check(self, *results: ProbeResult) -> RoutingCheck:
        return RoutingCheck(datetime.now(UTC).isoformat(), results)

    def test_a_single_under_ask_fails_however_good_the_score_is(self) -> None:
        # Guessing where the router was meant to ask is the one behaviour it
        # exists to prevent, so it is not something a high average can absorb.
        results = [_result("a", "a") for _ in range(19)] + [_result(ASK, "b")]
        check = self._check(*results)
        self.assertEqual(check.accuracy, 0.95)
        self.assertFalse(check.passed)
        self.assertEqual(len(check.under_ask), 1)

    def test_asking_too_often_lowers_the_score_without_being_a_rule_breach(self) -> None:
        check = self._check(_result("a", "a"), _result("b", ASK))
        self.assertEqual(check.accuracy, 0.5)
        self.assertEqual(check.under_ask, ())
        self.assertFalse(check.passed)  # on the score alone

    def test_misroutes_are_counted_separately_from_refusals(self) -> None:
        check = self._check(_result("a", "b"), _result("c", ASK))
        self.assertEqual(len(check.misroutes), 1)
        self.assertEqual(check.under_ask, ())

    def test_a_check_with_no_probes_never_passes(self) -> None:
        # Otherwise skipping every probe would read as a clean bill of health.
        self.assertFalse(self._check().passed)
        self.assertEqual(self._check().accuracy, 0.0)

    def test_nothing_measurable_is_reported_apart_from_measured_and_bad(self) -> None:
        # A registry where every probe was skipped has not been shown to route
        # badly. Reporting that as "failing" would send someone hunting a
        # regression that was never observed.
        self.assertEqual(self._check().status, "not_measurable")
        self.assertEqual(self._check(_result("a", "b")).status, "failing")
        self.assertEqual(self._check(_result("a", "a")).status, "ok")

    def test_passes_only_at_or_above_the_accuracy_floor(self) -> None:
        below = self._check(*([_result("a", "a")] * 5 + [_result("b", ASK)] * 5))
        self.assertEqual(below.accuracy, 0.5)
        self.assertFalse(below.passed)
        at = self._check(*([_result("a", "a")] * 6 + [_result("b", ASK)] * 4))
        self.assertEqual(at.accuracy, 0.6)
        self.assertTrue(at.passed)


class RunTests(unittest.TestCase):
    def test_scores_probes_against_the_live_registry(self) -> None:
        check = routecheck.run(
            _router(),
            (
                Probe("how is bryo doing", "organism.bryo_status", ("organism.bryo_status",)),
                Probe(
                    "organism status",
                    ASK,
                    ("organism.bryo_status", "organism.genesis_status"),
                ),
            ),
        )
        self.assertEqual(check.total, 2)
        self.assertEqual(check.correct, 2)
        self.assertTrue(check.passed)

    def test_catches_a_router_that_stopped_asking(self) -> None:
        # A router that always routes scores perfectly on route probes while
        # having abandoned the behaviour it was built for. Only an ASK probe
        # sees it, which is why the shipped probe set contains them.
        class NeverAsks(IntentRouter):
            def classify(self, request: str) -> RoutingDecision:
                decision = super().classify(request)
                if decision.resolved:
                    return decision
                # Whatever it would have asked about, take the top candidate.
                top = decision.candidates[0].capability if decision.candidates else "x"
                return RoutingDecision(top, decision.confidence, "matched", decision.candidates)

        check = routecheck.run(
            NeverAsks(_router().executive),
            (
                Probe("how is bryo doing", "organism.bryo_status", ("organism.bryo_status",)),
                Probe(
                    "organism status",
                    ASK,
                    ("organism.bryo_status", "organism.genesis_status"),
                ),
            ),
        )
        self.assertEqual(check.correct, 1)  # the route probe still passes
        self.assertEqual(len(check.under_ask), 1)
        self.assertFalse(check.passed)

    def test_catches_a_misroute(self) -> None:
        check = routecheck.run(
            _router(),
            (Probe("how is bryo doing", "organism.genesis_status", ("organism.genesis_status",)),),
        )
        self.assertEqual(len(check.misroutes), 1)
        self.assertFalse(check.passed)

    def test_skips_probes_for_unregistered_specialists_and_says_which(self) -> None:
        # Not configured is not the same as wrong, and scoring it as a failure
        # would make the number meaningless on any partial install.
        check = routecheck.run(
            _router(),
            (Probe("talk to theo", "conversation.theo_reply", ("conversation.theo_reply",)),),
        )
        self.assertEqual(check.total, 0)
        self.assertEqual(len(check.skipped), 1)
        self.assertIn("conversation.theo_reply", check.skipped[0])

    def test_the_shipped_probe_set_covers_asking_as_well_as_routing(self) -> None:
        expectations = {probe.expected for probe in routecheck.default_probes()}
        self.assertIn(ASK, expectations)
        self.assertGreaterEqual(len(expectations - {ASK}), 4)

    def test_the_harness_writes_nothing_to_the_ledger(self) -> None:
        # The ledger is the external vector this probe set is calibrated
        # against, so a harness that wrote to it would be measuring itself and
        # would report the agreement as a pass - a green light manufactured out
        # of a loop, which is worse than not calibrating at all.
        #
        # True today only because run() classifies rather than routes, and the
        # ask path is what records. That is an accident of which method is
        # called, so it is asserted here rather than left to hold by luck.
        router = _router()
        routecheck.run(
            router,
            (
                Probe("how is bryo doing", "organism.bryo_status", ("organism.bryo_status",)),
                Probe(
                    "organism status",
                    ASK,
                    ("organism.bryo_status", "organism.genesis_status"),
                ),
                Probe("photosynthesis in tomato plants", ASK),
            ),
        )
        self.assertEqual(router.executive.audit_sink.events, ())

    def test_a_real_request_does_reach_the_ledger(self) -> None:
        # The counterpart, so the assertion above cannot pass because nothing
        # writes to the ledger under any circumstances.
        router = _router()
        router.route("how is bryo doing")
        self.assertIn(
            "task.routed",
            [event.event_type for event in router.executive.audit_sink.events],
        )

    def test_nothing_is_executed(self) -> None:
        # Measuring aim must never take a GPU lease or call a specialist, so it
        # stays safe to run while the card is busy.
        class Exploding(_Adapter):
            def execute(self, task: Task) -> TaskResult:
                raise AssertionError("route-check must not execute anything")

        executive = Executive(scheduler=offline_scheduler())
        executive.register(
            Exploding(
                AgentManifest(
                    "bryo",
                    "1",
                    (
                        Capability(
                            name="organism.bryo_status",
                            description="Read Bryo's vitals",
                            routing_hints=frozenset({"bryo"}),
                        ),
                    ),
                )
            )
        )
        check = routecheck.run(
            IntentRouter(executive),
            (Probe("how is bryo doing", "organism.bryo_status", ("organism.bryo_status",)),),
        )
        self.assertEqual(check.correct, 1)


class PersistenceTests(unittest.TestCase):
    def test_round_trips_through_disk(self) -> None:
        original = RoutingCheck(datetime.now(UTC).isoformat(), (_result("a", "b"),), ("x",))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "last-check.json"
            routecheck.save(path, original)
            restored = routecheck.load(path)
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored.results, original.results)
        self.assertEqual(restored.skipped, ("x",))

    def test_never_measured_reads_as_none_rather_than_an_empty_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(routecheck.load(Path(directory) / "absent.json"))

    def test_a_corrupt_record_reads_as_never_measured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "last-check.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertIsNone(routecheck.load(path))

    def test_age_is_measured_from_when_the_check_ran(self) -> None:
        taken = datetime.now(UTC) - timedelta(days=45)
        check = RoutingCheck(taken.isoformat(), (_result("a", "a"),))
        self.assertAlmostEqual(check.age_days(), 45.0, places=1)
        self.assertGreater(check.age_days(), routecheck.STALE_DAYS)

    def test_the_record_is_readable_without_this_module(self) -> None:
        document = json.loads(
            RoutingCheck(datetime.now(UTC).isoformat(), (_result("a", "b"),)).to_json()
        )
        self.assertEqual(document["results"][0]["outcome"], "misroute")


def _adapter(agent_id: str, name: str, routable: bool = True) -> _Adapter:
    return _Adapter(
        AgentManifest(
            agent_id, "1",
            (Capability(name=name, description=f"{name} thing", routable=routable),),
        )
    )


class FingerprintTests(unittest.TestCase):
    """The saved check is stale when the routable capability set has changed,
    detected by a fingerprint, not only by its age."""

    def _router_with(self, *adapters: _Adapter) -> IntentRouter:
        executive = Executive(scheduler=offline_scheduler())
        for adapter in adapters:
            executive.register(adapter)
        return IntentRouter(executive)

    def test_the_fingerprint_changes_when_a_capability_is_added(self) -> None:
        one = self._router_with(_adapter("a", "x.one"))
        two = self._router_with(_adapter("a", "x.one"), _adapter("b", "x.two"))
        self.assertNotEqual(
            routecheck.registry_fingerprint(one),
            routecheck.registry_fingerprint(two),
        )

    def test_a_non_routable_capability_does_not_change_the_fingerprint(self) -> None:
        without = self._router_with(_adapter("a", "x.one"))
        with_hidden = self._router_with(
            _adapter("a", "x.one"), _adapter("m", "manager.hidden", routable=False)
        )
        self.assertEqual(
            routecheck.registry_fingerprint(without),
            routecheck.registry_fingerprint(with_hidden),
        )

    def test_the_fingerprint_survives_a_json_round_trip(self) -> None:
        check = routecheck.run(self._router_with(_adapter("a", "x.one")))
        restored = RoutingCheck.from_json(check.to_json())
        self.assertEqual(restored.registry_hash, check.registry_hash)
        self.assertTrue(restored.registry_hash)


class DoctorStalenessTests(unittest.TestCase):
    def test_doctor_marks_a_check_stale_when_the_capability_set_changed(self) -> None:
        from pionir.bootstrap import build_runtime
        from pionir.cli import _doctor
        from pionir.config import PionirSettings

        with tempfile.TemporaryDirectory() as tmp:
            runtime = build_runtime(
                PionirSettings(
                    state_root=Path(tmp),
                    atani_command=("pionir-test-no-such-binary",),
                    bryo_status_command=None, nyx_status_command=None,
                    voodoo_status_command=None, daedalus_url=None,
                    melete_url=None, galatea_url=None, evict_to_fit=False,
                    embed_model=None,
                )
            )
            try:
                check = routecheck.run(IntentRouter(runtime.executive))
                routecheck.save(runtime.settings.routing_check_path, check)
                fresh = _doctor(runtime)["routing_aim"]
                self.assertNotIn("stale", fresh)  # just measured, set unchanged

                runtime.executive.registry.unregister("atani")  # the set changed
                stale = _doctor(runtime)["routing_aim"]
                self.assertIn("stale", stale)
                self.assertIn("capability set", stale["stale"])
            finally:
                runtime.cortex.close()


if __name__ == "__main__":
    unittest.main()
