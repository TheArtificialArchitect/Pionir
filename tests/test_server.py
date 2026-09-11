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
            galatea_url="http://127.0.0.1:8799",
            galatea_model_id="stub-model",
            daedalus_url="http://127.0.0.1:9998",
            melete_url="http://127.0.0.1:9999",
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

    def test_intent_gates_shell_and_the_executive_to_approval(self) -> None:
        # The voice must NOT be able to run Melete's shell or the Atani executive
        # on her own - these act on the world. They come back needs_approval and
        # are not executed.
        shell = self.app.intent("run a shell command to list files")
        self.assertEqual(shell["status"], "needs_approval")
        self.assertEqual(shell["decision"]["capability"], "tools.melete_invoke")
        plan = self.app.intent("run this versioned plan through the executive")
        self.assertEqual(plan["status"], "needs_approval")

    def test_intent_hands_conversation_back_to_the_voice(self) -> None:
        out = self.app.intent("let's just chat for a while")
        self.assertEqual(out["status"], "self")

    def test_intent_asks_when_it_cannot_tell(self) -> None:
        out = self.app.intent("photosynthesis in tomato plants")
        self.assertEqual(out["status"], "unclear")

    def test_a_coding_intent_takes_the_dryrun_path_not_approval(self) -> None:
        # "write code..." must route to Daedalus in dry_run, never to the
        # needs_approval gate. Daedalus is unreachable in this test runtime, so it
        # surfaces as error - but the point is the branch: it tried to run
        # plan-only Daedalus, it did not refuse as a world-changing action.
        out = self.app.intent("refactor this function and implement the fix")
        self.assertEqual(out["decision"]["capability"], "coding.daedalus_solve")
        self.assertNotEqual(out["status"], "needs_approval")
        self.assertIn(out["status"], {"planned", "error"})

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
