"""Money never moves without a human yes.

Ian's rule, 2026-09-25: "Moss is not allowed to spend any actual money without
asking." The approval gate only sees what gets parked, and until now an action
was parked only when it was PRIVILEGED *and* arrived without its permission - so
a spending capability registered with the wrong risk, or called by something that
already held its permission, would have spent money silently. These tests pin the
two structural guarantees that close that: a spending capability must be
PRIVILEGED, and it parks on every call regardless of permissions.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from standins import down_url

from pionir.bootstrap import build_runtime
from pionir.config import PionirSettings
from pionir.contracts import AgentManifest, Capability, RiskLevel, Task, TaskResult
from pionir.server import PionirApp


class _Shop:
    """A stand-in for any capability that spends money. Counts what actually ran."""

    def __init__(self) -> None:
        self.ran = 0
        self._manifest = AgentManifest(
            agent_id="shop",
            version="test",
            capabilities=(
                Capability(
                    name="shop.buy",
                    description="buy something with real money",
                    risk=RiskLevel.PRIVILEGED,
                    required_permissions=frozenset({"shop.buy"}),
                    spends_money=True,
                    routable=False,
                ),
            ),
        )

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def execute(self, task: Task) -> TaskResult:
        self.ran += 1
        return TaskResult(task_id=task.task_id, agent_id="shop", output={"bought": True}, evidence=())


def _app(tmp: str) -> tuple[PionirApp, _Shop]:
    runtime = build_runtime(
        PionirSettings(
            state_root=Path(tmp),
            atani_command=("pionir-test-no-such-binary",),
            galatea_url=down_url(),
            galatea_model_id="stub-model",
            embed_model=None,  # never reach the live Ollama embedder from a test
            daedalus_url=down_url(),
            melete_url=down_url(),
            bryo_status_command=None,
            nyx_status_command=None,
            voodoo_status_command=None,
            evict_to_fit=False,
        )
    )
    shop = _Shop()
    runtime.register(shop)
    return PionirApp(runtime), shop


class DefinitionTests(unittest.TestCase):
    def test_a_spending_capability_must_be_privileged(self) -> None:
        for risk in (RiskLevel.READ_ONLY, RiskLevel.REVERSIBLE_WRITE):
            with self.assertRaises(ValueError, msg=risk):
                Capability(name="x.buy", description="d", risk=risk, spends_money=True)

    def test_privileged_spending_is_allowed_to_exist(self) -> None:
        Capability(name="x.buy", description="d", risk=RiskLevel.PRIVILEGED, spends_money=True)

    def test_capabilities_do_not_spend_money_by_default(self) -> None:
        self.assertFalse(Capability(name="x.read", description="d").spends_money)


class GateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.app, self.shop = _app(self._tmp.name)

    def tearDown(self) -> None:
        self.app.runtime.cortex.close()
        self._tmp.cleanup()

    def test_spending_parks_even_when_the_permission_is_already_granted(self) -> None:
        # This is the bypass the rule closes: holding the permission is how every other
        # privileged action skips the gate. Money must not be able to.
        out = self.app.run_task("shop.buy", {"amount": 12.5}, permissions=["shop.buy"])
        self.assertEqual(out["status"], "pending_approval")
        self.assertEqual(self.shop.ran, 0)

    def test_spending_parks_without_the_permission_too(self) -> None:
        out = self.app.run_task("shop.buy", {"amount": 12.5})
        self.assertEqual(out["status"], "pending_approval")
        self.assertEqual(self.shop.ran, 0)

    def test_it_runs_once_after_a_human_approves_and_a_second_yes_cannot_spend_twice(self) -> None:
        aid = self.app.run_task("shop.buy", {"amount": 12.5}, permissions=["shop.buy"])["approval_id"]
        res = self.app.approve(aid)
        self.assertTrue(res["ok"])
        self.assertTrue(self.app.jobs.wait(res["task_id"], 30))
        self.assertEqual(self.shop.ran, 1)
        self.assertFalse(self.app.approve(aid)["ok"])       # already resolved
        self.assertEqual(self.shop.ran, 1)                  # money moved exactly once

    def test_a_denial_never_spends(self) -> None:
        aid = self.app.run_task("shop.buy", {"amount": 12.5}, permissions=["shop.buy"])["approval_id"]
        self.assertTrue(self.app.deny(aid)["ok"])
        self.assertEqual(self.shop.ran, 0)


if __name__ == "__main__":
    unittest.main()
