"""Proteus's control plane: brakes at once, arming only on the owner's yes.

Every ssh here is a fake runner that records the argv it was handed and answers with
canned output - nothing reaches the VPS, Peter or any live port. What is pinned:

- every VPS call is an argv list (``ssh ... user@host <remote>``), never a shell string,
  and the remote command is built from the fixed tables only - a payload that tries to
  smuggle a word in is refused before anything is parked or run;
- brakes (kill switches, stopping timers and services, Robinhood orders off) run at once;
- arming (timers on, kill switches cleared, orders on, live services started, deploys)
  parks as its own card on EVERY call - for Moss, the crew, the dashboard, and an
  in-process caller holding ``proteus.arm`` - runs only after an approval, and the
  adapter itself refuses a task that did not come through ``approve``.
"""

import tempfile
import unittest
from pathlib import Path

from standins import down_url

from pionir.adapters._proc import ProcessResult
from pionir.adapters.proteus import (
    ARM_PERMISSION,
    ARMING,
    CAPABILITIES,
    ProteusAdapter,
    ProteusSettings,
    parse_status,
)
from pionir.batching import OWNER_APPROVED_GRANT, approval_level
from pionir.bootstrap import build_runtime
from pionir.config import PionirSettings
from pionir.contracts import Task
from pionir.errors import AdapterProtocolError
from pionir.server import PionirApp

HOST = "vps.test"

STATUS_OUT = """unit prometheus-api.service active enabled
unit pro-robinhood-api.service active enabled
unit mrcrab-t1.timer active enabled
unit prometheus-entry.timer inactive disabled
kill karkinos absent
kill prometheus present
kill robinhood present
api prometheus 200
api karkinos 404
api robinhood 000
rh_orders 0
rh_dropin absent
signals_age 212
timer Mon 2026-09-29 13:07:00 UTC 14h left - - mrcrab-t1.timer mrcrab@t1.service
"""


class _Ssh:
    """The fake runner: records every argv, answers from a script."""

    def __init__(self, out: str = "", code: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.out, self.code = out, code

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        return ProcessResult(returncode=self.code, stdout=self.out, stderr="")

    @property
    def remotes(self) -> list[str]:
        return [argv[-1] for argv in self.calls]


def _adapter(ssh: _Ssh, tunnel=lambda port: "down", **kw) -> ProteusAdapter:
    settings = ProteusSettings(host=HOST, key_file=Path("C:/keys/proteus_deploy"), **kw)
    return ProteusAdapter(settings, runner=ssh, peter_health=lambda: True,
                          tunnel_health=tunnel)


def _app(tmp: str, ssh: _Ssh) -> PionirApp:
    runtime = build_runtime(PionirSettings(
        state_root=Path(tmp),
        atani_command=("pionir-test-no-such-binary",),
        galatea_url=down_url(),
        galatea_model_id="stub-model",
        embed_model=None,
        daedalus_url=down_url(),
        melete_url=down_url(),
        crew_url=None,
        bryo_status_command=None,
        nyx_status_command=None,
        voodoo_status_command=None,
        evict_to_fit=False,
        proteus_host=None,        # the real adapter (real ssh) is never registered here
    ))
    runtime.register(_adapter(ssh))
    return PionirApp(runtime)


class DeclarationTests(unittest.TestCase):
    def test_arming_is_money_and_brakes_are_not(self) -> None:
        by_name = {c.name: c for c in CAPABILITIES}
        self.assertEqual(ARMING, {"proteus.arm_timer", "proteus.clear_kill", "proteus.rh_orders_on",
                                  "proteus.start_service", "proteus.deploy"})
        for name in ARMING:
            cap = by_name[name]
            self.assertTrue(cap.spends_money and cap.requires_approval, name)
            self.assertEqual(cap.required_permissions, {ARM_PERMISSION})
            self.assertFalse(cap.routable, name)
            # its own card for any caller - even one that holds proteus.arm
            self.assertEqual(approval_level(cap, [ARM_PERMISSION]), "card", name)
        for name in set(by_name) - ARMING:
            self.assertEqual(approval_level(by_name[name]), "auto", name)


    def test_an_approval_marker_is_never_a_permission(self) -> None:
        from pionir.contracts import Capability, RiskLevel
        with self.assertRaises(ValueError):
            Capability(name="x.arm", description="d", risk=RiskLevel.PRIVILEGED,
                       required_permissions=frozenset({OWNER_APPROVED_GRANT}))


class WireTests(unittest.TestCase):
    def test_every_vps_call_is_an_argv_list_to_the_one_host(self) -> None:
        ssh = _Ssh()
        adapter = _adapter(ssh)
        adapter.execute(Task("proteus.kill", {"system": "all"}))
        adapter.execute(Task("proteus.stop_timer", {"timer": "prometheus-entry.timer"}))
        adapter.execute(Task("proteus.logs", {"unit": "mrcrab@t2.service", "lines": 40}))
        for argv in ssh.calls:
            self.assertEqual(argv[:2], ["ssh", "-i"])
            self.assertIn("BatchMode=yes", argv)
            self.assertEqual(argv[-2], f"root@{HOST}")
            self.assertEqual(len(argv), 9)            # never a command spread over argv
        kill, timer, logs = ssh.remotes
        for path in ("/opt/mrcrab/Mr-Crab/controls/KILL", "/root/.pantheon/prometheus/HALT",
                     "/root/.pantheon/ROBINHOOD_KILL"):
            self.assertIn(f"> {path}", kill)
        self.assertEqual(timer, "systemctl disable --now prometheus-entry.timer && "
                                "systemctl is-active prometheus-entry.timer; true")
        self.assertEqual(logs, "journalctl -u mrcrab@t2.service -n 40 --no-pager -o short-iso")

    def test_a_payload_can_only_choose_from_the_tables(self) -> None:
        ssh = _Ssh()
        adapter = _adapter(ssh)
        bad = [
            ("proteus.logs", {"unit": "mrcrab@t2.service; rm -rf /", "lines": 5}),
            ("proteus.logs", {"unit": "sshd.service"}),
            ("proteus.logs", {"unit": "mrcrab@t2.service", "lines": "5; reboot"}),
            ("proteus.logs", {"unit": "mrcrab@t2.service", "lines": 10_000}),
            ("proteus.kill", {"system": "karkinos && reboot"}),
            ("proteus.stop_timer", {"timer": "*.timer"}),
            ("proteus.arm_timer", {"timer": "all"}),              # arming is one at a time
            ("proteus.start_service", {"service": "proteus.service"}),   # the retired one: never
            ("proteus.deploy", {"deploy": "C:/evil.ps1"}),
        ]
        for name, payload in bad:
            # refused as a bad request, by the table check itself - not by some later accident
            with self.assertRaises(AdapterProtocolError, msg=f"{name} {payload}"):
                adapter.validate(Task(name, payload))
        self.assertEqual(ssh.calls, [])

    def test_a_dead_ssh_is_unavailable_not_a_silent_ok(self) -> None:
        adapter = _adapter(_Ssh(code=255))
        with self.assertRaises(Exception) as caught:
            adapter.execute(Task("proteus.kill", {"system": "karkinos"}))
        self.assertIn("ssh", str(caught.exception))

    def test_the_status_read_parses_the_plane(self) -> None:
        doc = parse_status(STATUS_OUT)
        self.assertEqual(doc["units"]["prometheus-entry.timer"], {"active": "inactive",
                                                                   "enabled": "disabled"})
        self.assertEqual(doc["kill_switches"], {"karkinos": False, "prometheus": True,
                                                "robinhood": True})
        self.assertEqual(doc["apis"], {"prometheus": "200", "karkinos": "404", "robinhood": "000"})
        self.assertIs(doc["rh_orders_armed"], False)
        self.assertEqual(doc["vps_signals_age_s"], 212)
        plane = _adapter(_Ssh(STATUS_OUT)).plane()
        self.assertTrue(plane["vps"]["ok"])
        self.assertTrue(plane["peter"]["healthy"])
        self.assertEqual({c["capability"] for c in plane["controls"] if c["arming"]}, ARMING)

    def test_doctor_health_never_touches_the_vps(self) -> None:
        ssh = _Ssh()
        _adapter(ssh).status()
        self.assertEqual(ssh.calls, [])

    def test_orders_off_that_leaves_orders_armed_says_so(self) -> None:
        out = _adapter(_Ssh("rh_orders 1\n")).execute(Task("proteus.rh_orders_off", {})).output
        self.assertFalse(out["ok"])
        self.assertIn("STILL armed", out["error"])
        out = _adapter(_Ssh("rh_orders 0\n")).execute(Task("proteus.rh_orders_off", {})).output
        self.assertTrue(out["ok"])
        self.assertIs(out["rh_orders_armed"], False)


class ArmingGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ssh = _Ssh("rh_orders 1\n")
        self.app = _app(self._tmp.name, self.ssh)

    def tearDown(self) -> None:
        self.app.runtime.cortex.close()
        self._tmp.cleanup()

    ARM = [("proteus.arm_timer", {"timer": "prometheus-entry.timer"}),
           ("proteus.clear_kill", {"system": "robinhood"}),
           ("proteus.rh_orders_on", {}),
           ("proteus.start_service", {"service": "pro-robinhood-api.service"}),
           ("proteus.deploy", {"deploy": "prometheus-running"})]

    def test_moss_the_crew_and_the_dashboard_all_get_a_card_and_nothing_runs(self) -> None:
        for client in ("galatea", "crew", "atani", "dashboard"):
            for name, payload in self.ARM:
                out = self.app.run_task(name, payload, client=client)
                self.assertEqual(out["status"], "pending_approval", f"{client} {name}")
                self.assertNotIn("batched", out)          # never the daily digest
        self.assertEqual(self.ssh.calls, [])

    def test_holding_the_permission_or_naming_the_marker_is_not_a_yes(self) -> None:
        for name, payload in self.ARM:
            out = self.app.run_task(name, payload,
                                    permissions=[ARM_PERMISSION, OWNER_APPROVED_GRANT])
            self.assertEqual(out["status"], "pending_approval", name)
        self.assertEqual(self.ssh.calls, [])

    def test_the_adapter_refuses_arming_that_did_not_come_through_an_approval(self) -> None:
        executive = self.app.runtime.executive
        for _ in range(3):                         # again and again: still refused, and...
            for name, payload in self.ARM:
                out = executive.execute(Task(name, payload, frozenset({ARM_PERMISSION}))).output
                self.assertFalse(out["ok"], name)
                self.assertIn("owner's approval", out["refused"])
        self.assertEqual(self.ssh.calls, [])
        # ...a refusal is not a fault: the brakes behind the same circuit still work
        out = executive.execute(Task("proteus.kill", {"system": "robinhood"})).output
        self.assertTrue(out["ok"])
        self.assertEqual(len(self.ssh.calls), 1)

    def test_an_approval_runs_it_once_and_a_denial_never(self) -> None:
        aid = self.app.run_task("proteus.rh_orders_on", {}, client="galatea")["approval_id"]
        denied = self.app.run_task("proteus.arm_timer", {"timer": "prometheus-entry.timer"},
                                   client="galatea")["approval_id"]
        self.assertTrue(self.app.deny(denied)["ok"])
        done = self.app.approve(aid, wait=10)
        self.assertEqual(done["status"], "approved", done)
        self.assertEqual(len(self.ssh.calls), 1)
        self.assertIn("PRO_RH_ORDERS_ENABLED=1", self.ssh.remotes[0])
        self.assertEqual(self.app.approve(aid, wait=1)["error"]["type"], "AlreadyResolved")
        self.assertEqual(len(self.ssh.calls), 1)

    def test_moss_cannot_approve_her_own_card(self) -> None:
        aid = self.app.run_task("proteus.rh_orders_on", {}, client="galatea")["approval_id"]
        for who in ("galatea", "crew", "atani", "desktop"):
            self.assertFalse(self.app.approve(aid, approver=who)["ok"], who)
        self.assertEqual(self.ssh.calls, [])

    def test_brakes_run_at_once_for_anyone(self) -> None:
        out = self.app.run_task("proteus.kill", {"system": "prometheus"}, client="galatea", wait=10)
        self.assertTrue(out["ok"] and out["result"]["ok"], out)
        out = self.app.run_task("proteus.stop_timer", {"timer": "all"}, client="crew", wait=10)
        self.assertTrue(out["ok"] and out["result"]["ok"], out)
        self.assertEqual(len(self.ssh.calls), 2)
        self.assertIn("/root/.pantheon/prometheus/HALT", self.ssh.remotes[0])
        self.assertIn("prometheus-entry.timer", self.ssh.remotes[1])

    def test_a_bad_arming_request_is_refused_before_it_becomes_a_card(self) -> None:
        out = self.app.run_task("proteus.arm_timer", {"timer": "x; reboot"}, client="galatea")
        self.assertEqual(out["status"], "error")
        self.assertEqual(self.app.approvals_view()["pending"], [])


class ViewTests(unittest.TestCase):
    def test_the_plane_view_reads_in_the_background_and_caches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ssh = _Ssh(STATUS_OUT)
            app = _app(tmp, ssh)
            try:
                clock = [100.0]
                queued = []
                view = app.proteus_view(refresh=queued.append, now=lambda: clock[0])
                self.assertEqual((view["snapshot"], view["refreshing"]), (None, True))
                self.assertEqual(ssh.calls, [])            # the page never waits on the VPS
                queued.pop()()                             # the background read
                view = app.proteus_view(refresh=queued.append, now=lambda: clock[0])
                self.assertEqual(view["snapshot"]["vps"]["kill_switches"]["prometheus"], True)
                self.assertEqual(queued, [])               # fresh: no second read
                clock[0] += 121
                app.proteus_view(refresh=queued.append, now=lambda: clock[0])
                app.proteus_view(refresh=queued.append, now=lambda: clock[0])
                self.assertEqual(len(queued), 1)           # one read at a time
            finally:
                app.runtime.cortex.close()


if __name__ == "__main__":
    unittest.main()
