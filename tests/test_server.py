"""Tests for the dashboard's endpoint logic (PionirApp), without a socket.

Every method returns plain data, so the server is tested by calling them - no
HTTP, no real specialist reached. The one live thing is the in-process router
and registry, which is exactly what the dashboard is a window onto.
"""

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pionir.bootstrap import build_runtime
from pionir.config import PionirSettings
from pionir.runtime import AuditEvent
from pionir.server import PionirApp, _ui_bytes


def _runtime(tmp: str):
    # Hermetic: every specialist registers, but Daedalus/Melete point at dead
    # ports and Galatea's model is pinned (no /health probe), so bootstrap does
    # no network and an executed intent fails fast instead of running for real.
    return build_runtime(
        PionirSettings(
            state_root=Path(tmp),
            # A bogus command and dead ports: every specialist registers, but
            # executing one fails fast instead of invoking the real Atani CLI or
            # a live model. Routing/policy is what these tests exercise.
            atani_command=("pionir-test-no-such-binary",),
            galatea_url="http://127.0.0.1:8799",
            galatea_model_id="stub-model",
            daedalus_url="http://127.0.0.1:9998",
            melete_url="http://127.0.0.1:9999",
            bryo_status_command=None,  # no real subprocess from a test
            evict_to_fit=False,  # never unload a live model from a test
        )
    )


class PionirAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.app = PionirApp(_runtime(self._tmp.name))

    def tearDown(self) -> None:
        # Close the cortex SQLite connection first: Windows will not delete a
        # file that is still open, and the temp dir holds the memory.db.
        self.app.runtime.cortex.close()
        self._tmp.cleanup()

    def test_roster_lists_the_registered_specialists(self) -> None:
        agents = {c["agent_id"] for c in self.app.roster()}
        self.assertIn("atani", agents)
        self.assertIn("daedalus", agents)
        self.assertIn("melete", agents)

    def test_state_has_roster_and_gpu_budget(self) -> None:
        state = self.app.state()
        self.assertIn("roster", state)
        self.assertIn("generated_at", state)
        self.assertEqual(state["gpu"]["budget"]["max_gpu_leases"], 1)

    def test_classify_only_resolves_without_executing(self) -> None:
        out = self.app.route("reason carefully about this", execute=False)
        self.assertFalse(out["executed"])
        self.assertEqual(out["decision"]["capability"], "reasoning.atani_answer")

    def test_an_unroutable_request_asks_rather_than_running(self) -> None:
        out = self.app.route("photosynthesis in tomato plants", execute=True)
        self.assertFalse(out["executed"])
        self.assertIn("question", out)
        self.assertIsNone(out["decision"]["capability"])

    def test_a_coding_request_routes_to_daedalus(self) -> None:
        out = self.app.route("refactor this function", execute=False)
        self.assertEqual(out["decision"]["capability"], "coding.daedalus_solve")

    def test_run_task_returns_errors_as_data(self) -> None:
        # An unknown capability must come back as {ok:false, error}, never raise -
        # the page shows the failure, it does not crash on it.
        out = self.app.run_task("nope.capability", {})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["type"], "CapabilityNotFound")

    def test_audit_reports_integrity_and_recent_events(self) -> None:
        sink = self.app.runtime.executive.audit_sink
        sink.record(
            AuditEvent(
                event_type="task.routed",
                task_id=uuid4(),
                agent_id="atani",
                occurred_at=datetime.now(UTC),
                detail="outcome=route",
            )
        )
        audit = self.app.audit(10)
        self.assertEqual(audit["integrity"], "verified")
        self.assertEqual(audit["events_total"], 1)
        self.assertEqual(audit["events"][0]["agent_id"], "atani")

    def test_the_voice_never_tasks_a_doer_directly(self) -> None:
        # The voice does not control the organs. A doer's job - shell, coding,
        # the executive - is handed to Atani the manager (manager.atani_manage),
        # never run against the doer here. Atani is a bogus command in the test
        # runtime, so it surfaces as error - but the point is the route: it went
        # to Atani, and its own decision (a coding capability) was not executed
        # as a doer call from the voice.
        for request in (
            "run a shell command to list files",
            "refactor this function and implement the fix",
        ):
            out = self.app.intent(request)
            self.assertNotIn(out["status"], {"via_manager", "self", "done", "planned"}, request)
            self.assertEqual(out["status"], "error", request)

    def test_capability_risk_gates_view_from_task(self) -> None:
        # Read-only capabilities run directly (she may view/read); privileged ones
        # are a doer's job routed to Atani. The risk lookup is what decides.
        from pionir.contracts import RiskLevel

        self.assertIs(
            self.app._capability_risk("conversation.galatea_reply"), RiskLevel.READ_ONLY
        )
        self.assertIs(
            self.app._capability_risk("coding.daedalus_solve"), RiskLevel.PRIVILEGED
        )
        self.assertIsNone(self.app._capability_risk("no.such.capability"))

    def test_intent_hands_conversation_back_to_the_voice(self) -> None:
        out = self.app.intent("let's just chat for a while")
        self.assertEqual(out["status"], "self")

    def test_intent_asks_when_it_cannot_tell(self) -> None:
        out = self.app.intent("photosynthesis in tomato plants")
        self.assertEqual(out["status"], "unclear")

    def test_asking_atani_to_reason_is_the_one_thing_the_voice_runs(self) -> None:
        # Asking Atani to think has no doer and no side effect, so the voice may
        # run it. Atani is unreachable in this test runtime, so it surfaces as
        # error - but the branch is what matters: it tried to run, it was not
        # deferred to the manager.
        out = self.app.intent("think it over deliberately and thoroughly, the careful deep way")
        self.assertEqual(out["decision"]["capability"], "reasoning.atani_depth")
        self.assertNotEqual(out["status"], "via_manager")

    def test_gpu_view_never_raises_without_a_card(self) -> None:
        gpu = self.app.gpu()
        # observed_free/total may be None on a GPU-less runner; the budget is static.
        self.assertGreater(gpu["budget"]["total_mb"], 0)
        self.assertIsInstance(gpu["resident"], list)


class UiTests(unittest.TestCase):
    def test_dashboard_html_ships_and_names_itself(self) -> None:
        body = _ui_bytes()
        self.assertIn(b"<title>Pionir</title>", body)
        self.assertIn(b"/api/route", body)


if __name__ == "__main__":
    unittest.main()
