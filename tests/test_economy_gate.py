"""Bolts never authorise anything: the payout path and the approval/money gate are separate.

Each test fails if the rule is reverted: a gate module that imports the economy (so a balance
could be consulted to approve something), a funded ledger that changes what a permission
check says, a collector that writes to the approvals queue, or an economy module that reaches
into the gate it is supposed to stay apart from.
"""
import ast
import json
import tempfile
import unittest
from pathlib import Path

from pionir.contracts import AgentManifest, Capability, Task
from pionir.crew.registry import default_registry
from pionir.economy import collector as collector_mod
from pionir.economy.collector import APPROVAL_CAPABILITIES, Collector
from pionir.economy.ledger import Ledger
from pionir.economy.payouts import RefusalLog
from pionir.errors import PermissionDenied
from pionir.registry import CapabilityRegistry

SRC = Path(__file__).resolve().parents[1] / "src" / "pionir"
GATES = ("approvals.py", "registry.py", "discord_gate.py", "auth.py", "errors.py",
         "runtime.py", "router.py", "batching.py", "scheduler.py", "ollama_gate.py",
         "bridge_auth.py", "signin.py", "contracts.py", "workapi.py", "fiverr.py")
ALLOWED_IMPORTERS = {"crew/bolts.py", "server.py"}


def imports_economy(path: Path) -> list[int]:
    """Line numbers of any import that reaches the economy package."""
    hits = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""] + [f"{node.module or ''}.{a.name}" for a in node.names]
        if any(n.split(".")[-1] == "economy" or ".economy" in n or n.startswith("economy")
               for n in names):
            hits.append(node.lineno)
    return hits


class GateIndependenceTests(unittest.TestCase):
    def test_no_gate_module_imports_the_economy(self) -> None:
        for name in GATES:
            path = SRC / name
            self.assertTrue(path.exists(), name)
            self.assertEqual(imports_economy(path), [], f"{name} imports the economy")

    def test_only_the_worker_and_the_read_only_view_touch_the_economy(self) -> None:
        importers = set()
        for path in SRC.rglob("*.py"):
            rel = path.relative_to(SRC).as_posix()
            if rel.startswith("economy/") or not imports_economy(path):
                continue
            importers.add(rel)
        self.assertEqual(importers, ALLOWED_IMPORTERS)

    def test_the_server_reference_is_the_one_lazy_read_only_view(self) -> None:
        path = SRC / "server.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        top_level = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        self.assertEqual([n for n in top_level
                          if "economy" in (getattr(n, "module", "") or "")], [])
        lines = imports_economy(path)
        self.assertEqual(len(lines), 1)
        source = path.read_text(encoding="utf-8").splitlines()[lines[0] - 1]
        self.assertIn("economy.view import economy_payload", source)

    def test_the_economy_never_imports_the_gate(self) -> None:
        forbidden = {"approvals", "discord_gate", "auth", "registry", "runtime", "router"}
        for path in (SRC / "economy").glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom):
                    mod = (node.module or "").split(".")[-1]
                    if node.level and mod in forbidden:
                        self.fail(f"{path.name} imports {node.module}")
                    if node.level and not node.module:
                        for alias in node.names:
                            self.assertNotIn(alias.name, forbidden, path.name)


class BalanceCannotAuthoriseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def registry(self) -> CapabilityRegistry:
        cap = Capability("device.change", "Change a device",
                         required_permissions=frozenset({"device.write"}))
        reg = CapabilityRegistry()
        reg.register(AgentManifest("support", "1", (cap,)))
        return reg

    def test_a_rich_ledger_does_not_change_a_permission_denial(self) -> None:
        task = Task("device.change", {}, frozenset({"read"}))
        with self.assertRaises(PermissionDenied) as before:
            self.registry().resolve(task)
        ledger = Ledger.in_dir(self.root / "economy")
        for n in range(20):
            ledger.append("support", 100, "work", f"e{n}")
        self.assertEqual(ledger.balance("support"), 2000)
        with self.assertRaises(PermissionDenied) as after:
            self.registry().resolve(task)
        self.assertEqual(str(before.exception), str(after.exception))

    def test_a_task_carrying_bolts_in_its_payload_is_still_denied(self) -> None:
        task = Task("device.change", {"bolts": 10**9, "balance": 10**9, "pay_with": "bolts"},
                    frozenset())
        with self.assertRaises(PermissionDenied):
            self.registry().resolve(task)

    def test_the_collector_only_reads_the_approvals_queue(self) -> None:
        queue = self.root / "queue.json"
        queue.write_text(json.dumps([{"id": "a1", "capability": "content.publish",
                                      "status": "approved"},
                                     {"id": "a2", "capability": "content.publish",
                                      "status": "pending"}]), encoding="utf-8")
        before = queue.read_bytes()
        ledger = Ledger.in_dir(self.root / "economy")
        Collector(ledger, RefusalLog.in_dir(self.root / "economy"), approvals_path=queue,
                  state_dir=self.root, outputs=None).collect()
        self.assertEqual(queue.read_bytes(), before)
        self.assertEqual(ledger.balance("posting.blog"), 10)     # paid for a1, not for a2

    def test_the_collector_has_no_write_path_into_the_gate(self) -> None:
        text = Path(collector_mod.__file__).read_text(encoding="utf-8")
        for needle in ("write_text(self.approvals", "approvals_path, \"w", "os.replace(self.approvals"):
            self.assertNotIn(needle, text)


class AccountTableTests(unittest.TestCase):
    def test_every_approval_account_is_a_real_catalogue_worker(self) -> None:
        ids = set(default_registry().ids())
        for capability, (_kind, account) in APPROVAL_CAPABILITIES.items():
            self.assertIn(account, ids, capability)
        for account in (collector_mod.FIVERR_ACCOUNT, collector_mod.BUILDS_ACCOUNT,
                        collector_mod.API_BUILDER_ACCOUNT):
            self.assertIn(account, ids)


if __name__ == "__main__":
    unittest.main()
