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
    # Galatea is opt-in (unset here); Atani, Daedalus and Melete register by
    # default. None is probed at bootstrap, so this builds offline.
    return build_runtime(PionirSettings(state_root=Path(tmp)))


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
