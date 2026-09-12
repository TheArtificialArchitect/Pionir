"""Nyx/Voodoo as taskable tools: an allowlisted action, passed as argv (never a
shell), exposed as a PRIVILEGED capability so it lands in the approval gate.
"""
import unittest

from pionir.adapters.nyx_status import NyxStatusAdapter, NyxStatusSettings
from pionir.adapters.voodoo_status import VoodooStatusAdapter, VoodooStatusSettings
from pionir.contracts import RiskLevel, Task
from pionir.errors import AdapterProtocolError

# echoes its argv as JSON, so a test can prove the exact command that was built
ECHO = ("python", "-c", "import sys,json;print(json.dumps(sys.argv[1:]))")
STATUS = ("python", "-c", "print('{}')")


def _nyx(run: bool = True) -> NyxStatusAdapter:
    return NyxStatusAdapter(NyxStatusSettings(
        command=STATUS, run_prefix=ECHO if run else (), run_actions=("scan", "research")))


def _voodoo(run: bool = True) -> VoodooStatusAdapter:
    return VoodooStatusAdapter(VoodooStatusSettings(
        command=STATUS, run_prefix=ECHO if run else (), run_actions=("defend", "hunt")))


class RunCapabilityTests(unittest.TestCase):
    def test_run_is_privileged_and_needs_its_permission(self):
        for adapter, cap_name, perm in ((_nyx(), "security.nyx_run", "nyx.run"),
                                        (_voodoo(), "security.voodoo_run", "voodoo.run")):
            caps = {c.name: c for c in adapter.manifest.capabilities}
            self.assertIn(cap_name, caps)
            self.assertIs(caps[cap_name].risk, RiskLevel.PRIVILEGED)
            self.assertIn(perm, caps[cap_name].required_permissions)

    def test_no_run_capability_without_a_prefix(self):
        self.assertNotIn("security.nyx_run", {c.name for c in _nyx(run=False).manifest.capabilities})
        self.assertNotIn("security.voodoo_run", {c.name for c in _voodoo(run=False).manifest.capabilities})


class RunActionTests(unittest.TestCase):
    def test_allowed_action_is_passed_as_argv(self):
        out = _nyx().run_action({"action": "scan", "args": ["example.com", "--fast"]})
        self.assertTrue(out["ok"])
        self.assertEqual(out["output"], ["scan", "example.com", "--fast"])
        self.assertEqual(out["action"], "scan example.com --fast")

    def test_disallowed_action_is_refused(self):
        with self.assertRaises(AdapterProtocolError):
            _nyx().run_action({"action": "rm", "args": ["-rf", "/"]})
        with self.assertRaises(AdapterProtocolError):
            _voodoo().run_action({"action": "shell", "args": []})

    def test_execute_dispatches_run_and_status(self):
        res = _nyx().execute(Task("security.nyx_run", {"action": "research", "args": ["http://x"]}, frozenset()))
        self.assertEqual(res.agent_id, "nyx")
        self.assertEqual(res.output["output"], ["research", "http://x"])
        # the read-only status capability still works on the same adapter
        self.assertEqual(_voodoo().execute(Task("security.voodoo_status", {}, frozenset())).agent_id, "voodoo")

    def test_args_must_be_a_list(self):
        with self.assertRaises(AdapterProtocolError):
            _voodoo().run_action({"action": "defend", "args": "posture"})


if __name__ == "__main__":
    unittest.main()
