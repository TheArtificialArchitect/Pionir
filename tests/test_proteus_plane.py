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

import re
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
kill prometheus-robinhood present
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

    def __init__(self, out: str = "", code: int = 0, err: str = "") -> None:
        self.calls: list[list[str]] = []
        self.out, self.code, self.err = out, code, err

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        return ProcessResult(returncode=self.code, stdout=self.out, stderr=self.err)

    @property
    def remotes(self) -> list[str]:
        return [argv[-1] for argv in self.calls]


class FakeVps(_Ssh):
    """A droplet in miniature: it runs the SHAPES of commands the adapter sends (systemctl
    per unit, kill files under existing folders only, the orders drop-in and a restart) and
    answers the read-back the way the real box would. Every exit code is 0, as `; ` chains
    and loops make it - so only the read-back can tell a brake that worked from one that did
    not. Knobs: `stuck` units refuse to change; `dirs` are the folders that exist;
    `envfile_armed` is the unit's own EnvironmentFile setting the switch; `wrapper` makes
    MainPID a shell rather than the python process."""

    DIRS = {"/opt/mrcrab/Mr-Crab/controls", "/root/.pantheon/prometheus", "/root/.pantheon"}

    def __init__(self) -> None:
        super().__init__()
        self.units = {u: ["active", "enabled"] for u in (
            "mrcrab-t1.timer", "mrcrab-research.timer", "mrcrab-t2.timer", "mrcrab-t3.timer",
            "prometheus-api.service", "pro-robinhood-api.service")}
        for t in ("prometheus-scan.timer", "prometheus-entry.timer", "prometheus-review.timer"):
            self.units[t] = ["inactive", "disabled"]
        self.stuck: set[str] = set()
        self.dirs = set(self.DIRS)
        self.readonly: set[str] = set()
        self.files: set[str] = set()
        self.dropin: str | None = None
        self.envfile_armed = False
        self.unset_ignored = False
        self.wrapper = False
        self.env_armed = False

    def _restart(self) -> None:
        if self.units["pro-robinhood-api.service"][0] != "active":
            return
        if self.dropin == "on":
            self.env_armed = True
        elif self.dropin == "off" and not self.unset_ignored:
            self.env_armed = False
        else:
            self.env_armed = self.envfile_armed

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        cmd, out, err = argv[-1], [], []
        for units, verb in re.findall(r'for [ts] in ([^;]+); do systemctl (disable --now|enable --now|stop|start) "\$[ts]"', cmd):
            for u in units.split():
                if u in self.stuck:
                    err.append(f"Failed to {verb.split()[0]} {u}: Job failed")
                    continue
                if verb == "disable --now":
                    self.units[u] = ["inactive", "disabled"]
                elif verb == "enable --now":
                    self.units[u] = ["active", "enabled"]
                elif verb == "stop":
                    self.units[u][0] = "inactive"
                    if u == "pro-robinhood-api.service":
                        self.env_armed = False
                else:
                    self.units[u][0] = "active"
                    if u == "pro-robinhood-api.service":
                        self._restart()
        for parent, path in re.findall(r"if \[ -d (\S+) \]; then printf .*?> (\S+); fi", cmd):
            if parent in self.dirs and parent not in self.readonly:
                self.files.add(path)
        for path in re.findall(r"rm -f (\S+?)(?:;| &&|$)", cmd):
            if path.rsplit("/", 1)[0] not in self.readonly:
                self.files.discard(path)
        if r"\nEnvironment=PRO_RH_ORDERS_ENABLED=1\n' >" in cmd:
            self.dropin = "on"
        if r"\nUnsetEnvironment=PRO_RH_ORDERS_ENABLED\n' >" in cmd:
            self.dropin = "off"
        if "systemctl try-restart" in cmd:
            self._restart()
        for units in re.findall(r"for u in ([^;]+); do printf 'unit", cmd):
            for u in units.split():
                active, enabled = self.units.get(u, ["inactive", ""])
                out.append(f"unit {u} {active} {enabled}")
        for path, name, parent in re.findall(r"if \[ -f (\S+) \]; then echo 'kill (\S+) present'; elif \[ -d (\S+) \]", cmd):
            out.append(f"kill {name} " + ("present" if path in self.files else
                                          "absent" if parent in self.dirs else "noparent"))
        if "MainPID --value" in cmd:
            if self.units["pro-robinhood-api.service"][0] != "active":
                out.append("rh_orders stopped")
            elif self.wrapper:
                out.append("rh_orders unverifiable: MainPID 77 is not the python robinhood_read_api.py process")
            else:
                out.append(f"rh_orders {1 if self.env_armed else 0}")
        return ProcessResult(returncode=0, stdout="\n".join(out) + "\n", stderr="\n".join(err))


def _adapter(ssh: _Ssh, **kw) -> ProteusAdapter:
    settings = ProteusSettings(host=HOST, key_file=Path("C:/keys/proteus_deploy"), **kw)
    return ProteusAdapter(settings, runner=ssh, peter_health=lambda: True)


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
        ssh = FakeVps()
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
            self.assertIn(f"if [ -f {path} ]", kill)          # and read back
        self.assertIn('systemctl disable --now "$t"', timer)
        self.assertIn("systemctl is-active", timer)
        for cmd in (kill, timer):
            self.assertNotIn("; true", cmd)                     # nothing hides a failure
            self.assertNotIn("mkdir", cmd)                      # nothing invents a wrong path
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
                                                "prometheus-robinhood": True})
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



class BrakeVerificationTests(unittest.TestCase):
    """A brake reports success only when the state it read back PROVES it. Every command
    exits 0 here (loops and `;` chains do) - the verdict must come from the read-back."""

    def _run(self, vps, name, payload):
        return _adapter(vps).execute(Task(name, payload)).output

    def test_every_kill_switch_dropped_and_proven(self) -> None:
        vps = FakeVps()
        out = self._run(vps, "proteus.kill", {"system": "all"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(len(vps.files), 3)

    def test_a_kill_switch_whose_folder_is_missing_fails_loudly_and_writes_nothing(self) -> None:
        vps = FakeVps()
        vps.dirs.discard("/root/.pantheon/prometheus")
        out = self._run(vps, "proteus.kill", {"system": "all"})
        self.assertFalse(out["ok"])
        self.assertIn("prometheus kill switch: its folder does not exist", out["error"])
        self.assertNotIn("/root/.pantheon/prometheus/HALT", vps.files)
        self.assertIn("/opt/mrcrab/Mr-Crab/controls/KILL", vps.files)   # the others still dropped

    def test_a_kill_switch_that_did_not_land_is_not_dropped(self) -> None:
        vps = FakeVps()
        vps.readonly.add("/root/.pantheon")               # the folder is there; the write fails
        out = self._run(vps, "proteus.kill", {"system": "prometheus-robinhood"})
        self.assertFalse(out["ok"])
        self.assertIn("wanted present, read absent", out["error"])
        vps.files.add("/opt/mrcrab/Mr-Crab/controls/KILL")
        vps.readonly.add("/opt/mrcrab/Mr-Crab/controls")   # and a clear that does not clear
        out = _adapter(vps).execute(Task("proteus.clear_kill", {"system": "karkinos"},
                                         frozenset({OWNER_APPROVED_GRANT}))).output
        self.assertFalse(out["ok"])
        self.assertIn("wanted absent, read present", out["error"])

    def test_orders_on_is_ok_only_when_the_running_process_has_them(self) -> None:
        vps = FakeVps()
        vps.units["pro-robinhood-api.service"][0] = "inactive"
        out = _adapter(vps).execute(Task("proteus.rh_orders_on", {},
                                         frozenset({OWNER_APPROVED_GRANT}))).output
        self.assertFalse(out["ok"])
        self.assertIn("not armed in a running process", out["error"])
        vps.units["pro-robinhood-api.service"][0] = "active"
        out = _adapter(vps).execute(Task("proteus.rh_orders_on", {},
                                         frozenset({OWNER_APPROVED_GRANT}))).output
        self.assertTrue(out["ok"], out)

    def test_a_timer_that_will_not_stop_is_a_failed_brake_with_the_evidence(self) -> None:
        vps = FakeVps()
        vps.stuck.add("mrcrab-t2.timer")
        out = self._run(vps, "proteus.stop_timer", {"timer": "all"})
        self.assertFalse(out["ok"])
        self.assertIn("mrcrab-t2.timer: still active", out["error"])
        self.assertIn("Failed to disable mrcrab-t2.timer", out["stderr"])   # stderr is kept
        self.assertEqual(vps.units["mrcrab-t1.timer"], ["inactive", "disabled"])   # the rest stopped
        vps.stuck.clear()
        self.assertTrue(self._run(vps, "proteus.stop_timer", {"timer": "all"})["ok"])

    def test_a_disabled_but_running_or_stopped_but_enabled_timer_is_not_stopped(self) -> None:
        for active, enabled, why in (("active", "disabled", "still active"),
                                     ("inactive", "enabled", "still enabled")):
            out = _adapter(_Ssh(f"unit mrcrab-t1.timer {active} {enabled}\n")).execute(
                Task("proteus.stop_timer", {"timer": "mrcrab-t1.timer"})).output
            self.assertFalse(out["ok"], (active, enabled))
            self.assertIn(why, out["error"])

    def test_a_service_that_will_not_stop_is_a_failed_brake(self) -> None:
        vps = FakeVps()
        vps.stuck.add("pro-robinhood-api.service")
        out = self._run(vps, "proteus.stop_service", {"service": "pro-robinhood-api.service"})
        self.assertFalse(out["ok"])
        vps.stuck.clear()
        self.assertTrue(self._run(vps, "proteus.stop_service", {"service": "all"})["ok"])

    def test_no_read_back_is_no_success(self) -> None:
        for name, payload in (("proteus.kill", {"system": "karkinos"}),
                              ("proteus.stop_timer", {"timer": "mrcrab-t1.timer"}),
                              ("proteus.stop_service", {"service": "prometheus-api.service"}),
                              ("proteus.rh_orders_off", {})):
            out = _adapter(_Ssh("")).execute(Task(name, payload)).output
            self.assertFalse(out["ok"], name)
            self.assertIn("NOT VERIFIED", out["error"])

    def test_orders_off_wins_over_the_units_own_environment_file(self) -> None:
        vps = FakeVps()
        vps.envfile_armed = vps.env_armed = True
        out = self._run(vps, "proteus.rh_orders_off", {})
        self.assertTrue(out["ok"], out)
        self.assertIs(out["rh_orders_armed"], False)
        self.assertEqual(vps.dropin, "off")                  # UnsetEnvironment=, not a removal

    def test_orders_off_that_leaves_orders_armed_says_so(self) -> None:
        vps = FakeVps()
        vps.envfile_armed = vps.env_armed = vps.unset_ignored = True
        out = self._run(vps, "proteus.rh_orders_off", {})
        self.assertFalse(out["ok"])
        self.assertIn("STILL ARMED", out["error"])
        self.assertIn("proteus.stop_service", out["error"])

    def test_a_wrapper_as_mainpid_is_cannot_verify_never_off(self) -> None:
        vps = FakeVps()
        vps.wrapper = True
        out = self._run(vps, "proteus.rh_orders_off", {})
        self.assertFalse(out["ok"])
        self.assertIsNone(out["rh_orders_armed"])
        self.assertIn("cannot verify", out["error"])
        self.assertIsNone(parse_status("rh_orders unverifiable: MainPID 7 is a shell\n")["rh_orders_armed"])

    def test_a_stopped_api_places_no_orders(self) -> None:
        vps = FakeVps()
        vps.units["pro-robinhood-api.service"][0] = "inactive"
        self.assertTrue(self._run(vps, "proteus.rh_orders_off", {})["ok"])


class ArmingGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ssh = FakeVps()
        self.app = _app(self._tmp.name, self.ssh)

    def tearDown(self) -> None:
        self.app.runtime.cortex.close()
        self._tmp.cleanup()

    ARM = [("proteus.arm_timer", {"timer": "prometheus-entry.timer"}),
           ("proteus.clear_kill", {"system": "prometheus-robinhood"}),
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
        out = executive.execute(Task("proteus.kill", {"system": "prometheus-robinhood"})).output
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
        self.assertIs(self.ssh.env_armed, True)             # armed - and it was read back
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


class MarkerStripTests(unittest.TestCase):
    """run_task drops every approval.* marker a caller names, so only approve() can say the
    owner said yes. Removing that strip must fail here, not only be caught by parking."""

    class _Probe:
        def __init__(self) -> None:
            from pionir.contracts import AgentManifest, Capability
            self.seen: list[frozenset[str]] = []
            self._manifest = AgentManifest(agent_id="probe", version="test", capabilities=(
                Capability(name="probe.look", description="look", routable=False),))

        @property
        def manifest(self):
            return self._manifest

        def execute(self, task):
            from pionir.contracts import TaskResult
            self.seen.append(task.granted_permissions)
            return TaskResult(task_id=task.task_id, agent_id="probe", output={"ok": True})

    def test_a_caller_named_marker_never_reaches_the_adapter(self) -> None:
        from pionir.batching import APPROVAL_MARKERS
        with tempfile.TemporaryDirectory() as tmp:
            app = _app(tmp, FakeVps())
            probe = self._Probe()
            app.runtime.register(probe)
            try:
                out = app.run_task("probe.look", {}, permissions=["x.read", *sorted(APPROVAL_MARKERS)],
                                   wait=10)
                self.assertTrue(out["ok"], out)
                self.assertEqual(probe.seen, [frozenset({"x.read"})])
            finally:
                app.runtime.cortex.close()
