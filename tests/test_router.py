"""Tests for intent routing.

The router's value is what it refuses to do: guess between close candidates,
route on vocabulary shared by a whole family, substitute a reachable specialist
for a denied one, or reach a decision that leaves no trace. Those refusals are
what is asserted here, rather than the scoring arithmetic, which is an
implementation detail and free to change.
"""

import unittest

from pionir.contracts import AgentManifest, Capability, Task, TaskResult
from pionir.errors import PermissionDenied, RoutingAmbiguous
from pionir.router import IntentRouter
from pionir.runtime import Executive
from pionir.scheduler import ModelLeaseScheduler


class _Adapter:
    """A specialist that answers instantly and records what it was asked."""

    def __init__(self, manifest: AgentManifest) -> None:
        self._manifest = manifest
        self.calls: list[Task] = []

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def execute(self, task: Task) -> TaskResult:
        self.calls.append(task)
        return TaskResult(task.task_id, self._manifest.agent_id, {"ok": True})


def _capability(name: str, description: str, **kwargs: object) -> Capability:
    return Capability(name=name, description=description, **kwargs)  # type: ignore[arg-type]


def _executive() -> Executive:
    # No GPU probing: these tests are about classification, and must not depend
    # on what the developer's card is doing.
    return Executive(scheduler=ModelLeaseScheduler(vram_probe=None, residency_probe=None))


def _estate() -> tuple[Executive, dict[str, _Adapter]]:
    """A registry shaped like the real one: two organisms and two talkers."""

    adapters = {
        "bryo": _Adapter(
            AgentManifest(
                "bryo",
                "1",
                (
                    _capability(
                        "organism.bryo_status",
                        "Read Bryo's current vitals and lineage snapshot",
                        routing_hints=frozenset({"bryo", "terrarium", "organism"}),
                    ),
                ),
            )
        ),
        "genesis": _Adapter(
            AgentManifest(
                "genesis",
                "1",
                (
                    _capability(
                        "organism.genesis_status",
                        "Read Genesis health and life-loop counters",
                        routing_hints=frozenset({"genesis", "organism"}),
                    ),
                ),
            )
        ),
        "theo": _Adapter(
            AgentManifest(
                "theo",
                "1",
                (
                    _capability(
                        "conversation.theo_reply",
                        "A bounded, conversation-only Theo reply",
                        routing_hints=frozenset({"theo", "talk", "chat"}),
                    ),
                ),
            )
        ),
        "atani": _Adapter(
            AgentManifest(
                "atani",
                "1",
                (
                    _capability(
                        "reasoning.atani_answer",
                        "Atani's bounded default conversational reasoning",
                        required_permissions=frozenset({"atani.chat"}),
                        routing_hints=frozenset({"atani", "reason", "chat"}),
                    ),
                ),
            )
        ),
    }
    executive = _executive()
    for adapter in adapters.values():
        executive.register(adapter)
    return executive, adapters


def _routed_events(executive: Executive) -> list[str]:
    return [
        event.detail or ""
        for event in executive.audit_sink.events  # type: ignore[attr-defined]
        if event.event_type == "task.routed"
    ]


class ClassificationTests(unittest.TestCase):
    def test_routes_a_request_naming_one_specialist(self) -> None:
        executive, _ = _estate()
        decision = IntentRouter(executive).classify("how is bryo doing today")
        self.assertEqual(decision.capability, "organism.bryo_status")
        self.assertEqual(decision.reason, "matched")

    def test_asks_rather_than_guessing_between_two_equal_candidates(self) -> None:
        # "organism status" describes both organisms exactly as well. Picking one
        # would be a coin toss dressed up as a decision.
        executive, _ = _estate()
        decision = IntentRouter(executive).classify("organism status")
        self.assertIsNone(decision.capability)
        self.assertLess(decision.confidence, 0.6)
        self.assertIn("organism.bryo_status", decision.options)
        self.assertIn("organism.genesis_status", decision.options)

    def test_asks_when_nothing_registered_matches(self) -> None:
        executive, _ = _estate()
        decision = IntentRouter(executive).classify("photosynthesis in tomato plants")
        self.assertIsNone(decision.capability)
        self.assertEqual(decision.reason, "no_match")
        self.assertEqual(decision.confidence, 0.0)
        # It still offers the catalogue, so the question is answerable.
        self.assertTrue(decision.options)

    def test_asks_when_only_shared_family_vocabulary_matched(self) -> None:
        # One capability has a longer description and would win on sheer volume
        # of generic words. Volume is not evidence.
        executive = _executive()
        executive.register(
            _Adapter(
                AgentManifest(
                    "a",
                    "1",
                    (_capability("organism.a_status", "Read organism status now"),),
                )
            )
        )
        executive.register(
            _Adapter(
                AgentManifest(
                    "b",
                    "1",
                    (
                        _capability(
                            "organism.b_status",
                            "Read organism status, read organism state, organism",
                        ),
                    ),
                )
            )
        )
        decision = IntentRouter(executive).classify("organism status")
        self.assertIsNone(decision.capability)
        self.assertEqual(decision.reason, "not_distinctive")

    def test_the_same_request_always_classifies_the_same_way(self) -> None:
        executive, _ = _estate()
        router = IntentRouter(executive)
        first = router.classify("talk to theo")
        for _ in range(5):
            self.assertEqual(router.classify("talk to theo"), first)

    def test_a_higher_confidence_floor_turns_a_route_into_a_question(self) -> None:
        executive, _ = _estate()
        self.assertTrue(IntentRouter(executive).classify("bryo vitals").resolved)
        # A request only one capability answers scores 1.0 by construction, so
        # the floor has to be tested against one that two of them match.
        strict = IntentRouter(executive, confidence_floor=1.0)
        decision = strict.classify("bryo organism")
        self.assertIsNone(decision.capability)
        self.assertEqual(decision.reason, "ambiguous")

    def test_registering_a_specialist_makes_it_routable_with_no_edit_here(self) -> None:
        # The claim in the module docstring: routing follows the registry.
        executive, _ = _estate()
        router = IntentRouter(executive)
        self.assertFalse(router.classify("kairos clip").resolved)
        executive.register(
            _Adapter(
                AgentManifest(
                    "kairos",
                    "1",
                    (
                        _capability(
                            "media.kairos_clip",
                            "Cut a highlight clip",
                            routing_hints=frozenset({"kairos", "clip", "highlight"}),
                        ),
                    ),
                )
            )
        )
        self.assertEqual(router.classify("kairos clip").capability, "media.kairos_clip")

    def test_a_two_capability_registry_can_still_route(self) -> None:
        # Regression: distinctiveness was "used by fewer than half the
        # capabilities", and fewer than half of two is fewer than one, so a term
        # naming exactly one of two capabilities failed the test and nothing
        # could ever be routed. Invisible against the four- and eight-capability
        # registries everything else here uses.
        executive = _executive()
        for name in ("alpha", "beta"):
            executive.register(
                _Adapter(
                    AgentManifest(
                        name,
                        "1",
                        (_capability(f"organism.{name}_status", f"Read {name} vitals"),),
                    )
                )
            )
        decision = IntentRouter(executive).classify("alpha vitals")
        self.assertEqual(decision.capability, "organism.alpha_status")

    def test_an_empty_registry_asks_instead_of_failing_obscurely(self) -> None:
        decision = IntentRouter(_executive()).classify("anything at all")
        self.assertEqual(decision.reason, "no_capabilities")
        self.assertIn("No specialists are registered", decision.question())


class AuditTests(unittest.TestCase):
    def test_a_routed_task_records_its_confidence(self) -> None:
        executive, _ = _estate()
        IntentRouter(executive).route("how is bryo doing today")
        details = _routed_events(executive)
        self.assertEqual(len(details), 1)
        self.assertIn("outcome=route", details[0])
        self.assertIn("capability=organism.bryo_status", details[0])
        self.assertIn("confidence=", details[0])

    def test_a_refusal_to_guess_is_recorded_as_a_routing_decision(self) -> None:
        # Without this the ledger shows only confident routes, and "how often
        # could it not tell?" has no evidence behind it.
        executive, _ = _estate()
        with self.assertRaises(RoutingAmbiguous):
            IntentRouter(executive).route("organism status")
        details = _routed_events(executive)
        self.assertEqual(len(details), 1)
        self.assertIn("outcome=ask", details[0])
        self.assertIn("reason=", details[0])
        self.assertIn("options=organism.bryo_status,organism.genesis_status", details[0])

    def test_a_route_the_registry_refuses_is_still_recorded(self) -> None:
        executive, _ = _estate()
        with self.assertRaises(PermissionDenied):
            IntentRouter(executive).route("ask atani to reason about this")
        details = _routed_events(executive)
        self.assertEqual(len(details), 1)
        self.assertIn("refused=PermissionDenied", details[0])
        self.assertIn("capability=reasoning.atani_answer", details[0])

    def test_the_audit_detail_never_carries_the_request_text(self) -> None:
        # The ledger is metadata-only by design; routing is not the place to
        # start putting user content into it.
        executive, _ = _estate()
        secret = "zork9 pineapple submarine"
        with self.assertRaises(RoutingAmbiguous):
            IntentRouter(executive).route(secret)
        IntentRouter(executive).route(f"bryo vitals {secret}", granted_permissions=())
        for detail in _routed_events(executive):
            for word in secret.split():
                self.assertNotIn(word, detail)


class PermissionTests(unittest.TestCase):
    def test_a_denied_route_is_not_substituted_for_a_reachable_one(self) -> None:
        # Atani's chat capability needs a permission and Theo's does not. The
        # helpful-looking move is to answer via Theo instead; that turns a
        # refusal into a silent misroute and is exactly what must not happen.
        executive, adapters = _estate()
        with self.assertRaises(PermissionDenied):
            IntentRouter(executive).route("ask atani to reason about this")
        self.assertEqual(adapters["theo"].calls, [])
        self.assertEqual(adapters["atani"].calls, [])

    def test_granting_the_permission_lets_the_same_request_through(self) -> None:
        executive, adapters = _estate()
        decision, result = IntentRouter(executive).route(
            "ask atani to reason about this", granted_permissions={"atani.chat"}
        )
        self.assertEqual(decision.capability, "reasoning.atani_answer")
        self.assertEqual(result.agent_id, "atani")
        self.assertEqual(len(adapters["atani"].calls), 1)


class ExecutionTests(unittest.TestCase):
    def test_the_request_reaches_the_specialist_as_its_payload(self) -> None:
        executive, adapters = _estate()
        IntentRouter(executive).route("talk to theo about the weather")
        (task,) = adapters["theo"].calls
        self.assertEqual(task.payload["content"], "talk to theo about the weather")

    def test_the_question_names_the_candidate_routes(self) -> None:
        executive, _ = _estate()
        with self.assertRaises(RoutingAmbiguous) as caught:
            IntentRouter(executive).route("organism status")
        message = str(caught.exception)
        self.assertIn("organism.bryo_status", message)
        self.assertIn("organism.genesis_status", message)
        self.assertIsNotNone(caught.exception.decision)


if __name__ == "__main__":
    unittest.main()
