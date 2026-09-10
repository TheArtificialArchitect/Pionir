"""Nyx and Voodoo read-only status adapters: they expose status, redact it, and
route by their own vocabulary. Fake runners feed the real JSON shapes so no
security tool actually runs in the tests."""

import unittest

from pionir.adapters.nyx_status import NyxStatusAdapter, NyxStatusSettings
from pionir.adapters.voodoo_status import VoodooStatusAdapter, VoodooStatusSettings
from pionir.contracts import Task
from pionir.errors import AdapterProtocolError, AdapterUnavailable


class FakeRunner:
    def __init__(self, output: str = "", *, boom: Exception | None = None):
        self.output = output
        self.boom = boom

    def run(self, *, timeout_seconds: int) -> str:
        if self.boom:
            raise self.boom
        return self.output


NYX_STATUS = """{"name":"Nyx","version":"0.1.0","ledger":{"events":168,"integrity":"verified"},
"pending":{"approvals":0,"initiatives":1},"goals":{},"memory":["secret op notes"]}"""

VOODOO_STATUS = """{"scopes":[{"name":"client-lab","reason":"ticket 1842"}],
"active_leases":[{"scope":"client-lab","reason":"ticket 1842"}],
"proton_vpn":{"connected":true,"interface":"wg0"}}"""


class NyxTests(unittest.TestCase):
    def test_redacts_to_a_health_summary(self) -> None:
        adapter = NyxStatusAdapter(NyxStatusSettings(command=("nyx", "status")),
                                   runner=FakeRunner(NYX_STATUS))
        out = adapter.execute(Task("security.nyx_status", {})).output
        self.assertEqual(out["name"], "Nyx")
        self.assertEqual(out["ledger_integrity"], "verified")
        self.assertEqual(out["ledger_events"], 168)
        # memory/goals never leave Nyx
        self.assertNotIn("memory", out)

    def test_refuses_the_wrong_capability(self) -> None:
        adapter = NyxStatusAdapter(NyxStatusSettings(command=("nyx", "status")),
                                   runner=FakeRunner(NYX_STATUS))
        with self.assertRaises(AdapterProtocolError):
            adapter.execute(Task("security.voodoo_status", {}))

    def test_a_dead_command_is_unavailable_not_a_crash(self) -> None:
        adapter = NyxStatusAdapter(NyxStatusSettings(command=("nyx", "status")),
                                   runner=FakeRunner(boom=AdapterUnavailable("gone")))
        with self.assertRaises(AdapterUnavailable):
            adapter.execute(Task("security.nyx_status", {}))


class VoodooTests(unittest.TestCase):
    def test_redacts_scopes_leases_and_vpn(self) -> None:
        adapter = VoodooStatusAdapter(VoodooStatusSettings(command=("voodoo", "status")),
                                      runner=FakeRunner(VOODOO_STATUS))
        out = adapter.execute(Task("security.voodoo_status", {})).output
        self.assertEqual(out["scopes"], ["client-lab"])
        self.assertEqual(out["active_leases"], 1)
        self.assertTrue(out["vpn_connected"])
        # the ticket reason is not passed through
        self.assertNotIn("reason", str(out))

    def test_empty_output_is_a_protocol_error(self) -> None:
        adapter = VoodooStatusAdapter(VoodooStatusSettings(command=("voodoo", "status")),
                                      runner=FakeRunner("   "))
        with self.assertRaises(AdapterProtocolError):
            adapter.execute(Task("security.voodoo_status", {}))


if __name__ == "__main__":
    unittest.main()
