"""The port registry (src/pionir/ports.json): one list of every port the estate's stack uses.

What this pins, and what fails it:
  * the registry contradicts itself (a port claimed twice, a duplicate id, a bad regex or probe,
    overlapping scan ranges), or a reserved estate port goes missing from it;
  * the launcher (pionir.ps1) or the Python code names a port that is not in the registry, or
    starts a service on a port the registry gives to a different one (launcher/plan drift);
  * the audit reports a foreign program on a port as "ours" or "up", or a warming service as DOWN;
  * the launcher starts a second copy beside a stack Pionir Desktop owns, treats a foreign
    program on a bridge port as "already up", or kills one on -Stop.

The launcher's own PowerShell functions are lifted out of pionir.ps1 and run for real against
SYNTHETIC process and listener lists - no live process is listed, probed, started or stopped.
(This file names no live address as text: tests/test_hermetic.py forbids it.)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from pionir import ports
from pionir.ports import Listener

ROOT = Path(__file__).resolve().parent.parent
PS1 = ROOT / "pionir.ps1"
SRC = ROOT / "src" / "pionir"

# The numbers Ian named, and the ids that must own them. A change here is a deliberate change
# to the estate's ports, made in ports.json, pionir.ps1 and Pionir Desktop together.
EXPECTED = {
    "dashboard": [8780], "ollama-gate": [8774], "galatea": [8799], "daedalus": [8771],
    "melete": [8770], "crew": [8782], "peter": [8790], "tunnel": [18000, 18001, 18002],
    "ollama": [11434], "bryo-viewer": [8787], "hemera": [8788], "probability": [8791],
    "hearth": [8801], "proteus-relay": [8792],
    "desktop-relay": [8830], "desktop-phone": [8831],
    "desktop-relay-demo": [8840], "desktop-phone-demo": [8841],
}


def _write(directory: str, services: list[dict]) -> Path:
    path = Path(directory) / "ports.json"
    path.write_text(json.dumps({"schema": 1, "services": services}), encoding="utf-8")
    return path


def _svc(sid: str, port_list: list[int], **extra) -> dict:
    return {"id": sid, "label": sid, "ports": port_list, "match": sid,
            "probe": {"kind": "tcp"}, **extra}


class RegistryTests(unittest.TestCase):
    def test_the_shipped_registry_is_consistent(self) -> None:
        self.assertEqual(ports.validate(), [])

    def test_every_estate_port_is_in_it_under_the_right_owner(self) -> None:
        got = {s.id: list(s.ports) for s in ports.load_registry()}
        self.assertEqual(got, EXPECTED)

    def test_the_stack_ports_are_reserved_and_the_movable_companions_are_not(self) -> None:
        reserved = set(ports.reserved_ports())
        for sid in ("dashboard", "galatea", "daedalus", "melete", "crew", "peter", "tunnel",
                    "ollama-gate", "bryo-viewer", "hemera", "probability", "desktop-relay",
                    "desktop-phone", "desktop-relay-demo", "desktop-phone-demo"):
            self.assertTrue(set(EXPECTED[sid]) <= reserved, sid)
        self.assertFalse({8801, 8792} & reserved)         # Hearth and the Proteus relay can move

    def test_a_port_claimed_twice_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            problems = ports.validate(_write(tmp, [_svc("a", [9001]), _svc("b", [9001])]))
        self.assertTrue(any("9001" in p and "both a and b" in p for p in problems), problems)

    def test_a_duplicate_id_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            problems = ports.validate(_write(tmp, [_svc("a", [9001]), _svc("a", [9002])]))
        self.assertTrue(any("appears 2 times" in p for p in problems), problems)

    def test_a_bad_regex_or_probe_or_port_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad = _svc("a", [80], match="(")
            bad["probe"] = {"kind": "smoke-signal"}
            problems = ports.validate(_write(tmp, [bad]))
        text = " | ".join(problems)
        self.assertIn("not a regex", text)
        self.assertIn("neither http nor tcp", text)
        self.assertIn("1024-65535", text)

    def test_overlapping_relay_scan_ranges_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            problems = ports.validate(_write(tmp, [_svc("a", [9100], scan=10),
                                                   _svc("b", [9105], scan=10)]))
        self.assertTrue(any("overlaps" in p for p in problems), problems)

    def test_no_relay_scan_range_runs_over_a_reserved_port(self) -> None:
        # The live relay scans 8830-8839 for a free port; the phone listener (8831) sits inside
        # that range, so the desktop's scan must SKIP registered ports - the registry cannot
        # make that true alone, but it must at least tell every scanner which ports to skip.
        services = ports.load_registry()
        for scanner in (s for s in services if s.scan):
            window = set(range(scanner.ports[0], scanner.ports[0] + scanner.scan))
            others = {p for s in services if s is not scanner for p in s.ports}
            self.assertTrue(window & others or scanner.id.startswith("desktop-relay"), scanner.id)
            self.assertTrue((window & others) <= set(ports.reserved_ports()), scanner.id)


class CodeAgreesWithRegistryTests(unittest.TestCase):
    """A port written in code but not in the registry (or against another service's) is drift."""

    def setUp(self) -> None:
        self.by_id = {s.id: set(s.ports) for s in ports.load_registry()}

    def test_python_defaults_name_the_registered_port(self) -> None:
        from pionir.crew import api as crew_api
        from pionir.ollama_gate import GATE_PORT
        self.assertEqual({crew_api.DEFAULT_PORT}, self.by_id["crew"])
        self.assertEqual({GATE_PORT}, self.by_id["ollama-gate"])
        from pionir.adapters.proteus import TUNNEL_PORTS
        self.assertEqual({p for _n, p, _remote in TUNNEL_PORTS}, self.by_id["tunnel"])
        from pionir.bridge_auth import BRIDGES
        self.assertEqual(BRIDGES["daedalus-token.txt"][1], 8771)
        self.assertEqual(BRIDGES["melete-token.txt"][1], 8770)
        self.assertIn(8771, self.by_id["daedalus"])
        self.assertIn(8770, self.by_id["melete"])

    def test_each_adapters_base_url_names_its_own_registered_port(self) -> None:
        expect = {"adapters/crew.py": "crew", "adapters/daedalus.py": "daedalus",
                  "adapters/galatea.py": "galatea", "adapters/melete.py": "melete"}
        for rel, sid in expect.items():
            text = (SRC / rel).read_text(encoding="utf-8")
            found = {int(p) for p in re.findall(r'base_url: str = "http://127\.0\.0\.1:(\d+)"', text)}
            self.assertEqual(found, self.by_id[sid], rel)
        proteus = (SRC / "adapters" / "proteus.py").read_text(encoding="utf-8")
        self.assertEqual({int(p) for p in re.findall(r'peter_url: str = "http://127\.0\.0\.1:(\d+)"',
                                                     proteus)}, self.by_id["peter"])

    def test_the_dashboard_default_is_the_registered_port(self) -> None:
        cli = (SRC / "cli.py").read_text(encoding="utf-8")
        found = {int(p) for p in re.findall(r'add_argument\("--port", type=int, default=(\d+)\)', cli)}
        self.assertEqual(found, self.by_id["dashboard"])

    def test_the_launcher_names_only_registered_ports(self) -> None:
        lines = PS1.read_text(encoding="utf-8-sig").splitlines()
        text = chr(10).join(ln for ln in lines if not ln.lstrip().startswith("#"))  # usage examples
        registered = {p for ps in self.by_id.values() for p in ps}
        named = {int(p) for p in re.findall(r"127\.0\.0\.1:(\d{4,5})", text)}
        named |= {int(p) for p in re.findall(r"(?:Test-Port|Stop-Port|Claim-Port '\w[\w-]*') (\d{4,5})", text)}
        named |= {int(p) for p in re.findall(r"Pane-Cmd \"[^\"]*:(\d{4,5})\"", text)}
        named |= {int(p) for p in re.findall(r"\$ports \+= (\d{4,5})", text)}
        named |= {int(p) for p in re.findall(r"--port (\d{4,5})|-Port (\d{4,5})", text) for p in p if p}
        self.assertTrue(named, "the launcher names no ports at all - the scan itself broke")
        self.assertEqual(sorted(named - registered), [])

    def test_every_claim_in_the_launcher_names_the_registered_port_of_that_id(self) -> None:
        text = PS1.read_text(encoding="utf-8-sig")
        claims = re.findall(r"Claim-Port '([\w-]+)' (\$Port|\d+)", text)
        self.assertEqual({i for i, _ in claims},
                         {"dashboard", "galatea", "daedalus", "melete", "crew", "peter"})
        for sid, port in claims:
            wanted = 8780 if port == "$Port" else int(port)
            self.assertIn(wanted, self.by_id[sid], sid)

    def test_stop_stops_each_service_by_its_registered_port(self) -> None:
        text = PS1.read_text(encoding="utf-8-sig")
        stops = re.findall(r'Stop-Port (\$Port|\d+) "[^"]+" "([\w-]+)"', text)
        self.assertEqual({i for _, i in stops}, {"dashboard", "galatea", "daedalus", "melete", "crew"})
        for port, sid in stops:
            self.assertIn(8780 if port == "$Port" else int(port), self.by_id[sid], sid)

    def test_the_launcher_default_dashboard_port_is_the_registered_one(self) -> None:
        text = PS1.read_text(encoding="utf-8-sig")
        self.assertEqual({int(p) for p in re.findall(r"\[int\]\$Port = (\d+)", text)},
                         self.by_id["dashboard"])

    def test_the_launcher_waits_for_the_slow_starter_the_registry_names(self) -> None:
        slow = [s for s in ports.load_registry() if s.slow_start]
        self.assertEqual([s.id for s in slow], ["crew"])
        text = PS1.read_text(encoding="utf-8-sig")
        self.assertIn("$ports -contains 8782", text)


# ---- the audit: states per port, by capability ------------------------------------------------

def _listener(port: int, name: str, command: str, pid: int = 4242) -> Listener:
    return Listener(port=port, pid=pid, name=name, command=command)


class AuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.services = ports.load_registry()

    def audit(self, listeners, *, probe=lambda p, path: True, warming=None):
        rows = ports.audit(self.services, listeners=listeners, probe=probe, warming=warming)
        return {r["port"]: r for r in rows}

    def test_nothing_listening_is_free_everywhere(self) -> None:
        rows = self.audit([])
        self.assertEqual({r["state"] for r in rows.values()}, {ports.FREE})

    def test_our_service_that_answers_is_ours_and_healthy(self) -> None:
        rows = self.audit([_listener(8771, "python.exe", "py -m daedalus.server")])
        self.assertEqual(rows[8771]["state"], ports.OURS_HEALTHY)
        self.assertEqual(rows[8771]["pid"], 4242)

    def test_our_service_that_does_not_answer_is_unhealthy_not_up(self) -> None:
        rows = self.audit([_listener(8771, "python.exe", "py -m daedalus.server")],
                          probe=lambda p, path: False)
        self.assertEqual(rows[8771]["state"], ports.OURS_UNHEALTHY)
        self.assertIn("/health", rows[8771]["detail"])

    def test_a_foreign_program_on_a_bridge_port_is_named_never_ours(self) -> None:
        rows = self.audit([_listener(8771, "node.exe", "node C:\\x\\dev-server.js", pid=77)])
        self.assertEqual(rows[8771]["state"], ports.FOREIGN)
        self.assertEqual(rows[8771]["process"], "node.exe")
        self.assertEqual(rows[8771]["pid"], 77)
        self.assertIn("node.exe", rows[8771]["detail"])

    def test_the_probe_is_never_asked_about_a_foreign_listener(self) -> None:
        asked = []
        self.audit([_listener(8780, "node.exe", "node server.js")],
                   probe=lambda p, path: asked.append(p) or True)
        self.assertEqual(asked, [])

    def test_a_slow_starter_that_is_alive_is_warming_not_down(self) -> None:
        rows = self.audit([], warming=lambda svc: svc.id == "crew")
        self.assertEqual(rows[8782]["state"], ports.WARMING)
        self.assertEqual(rows[8771]["state"], ports.FREE)      # only a slow starter can be warming

    def test_a_tcp_only_service_is_healthy_when_it_listens(self) -> None:
        rows = self.audit([_listener(8774, "python.exe", "py -m pionir server --port 8780")],
                          probe=lambda p, path: False)
        self.assertEqual(rows[8774]["state"], ports.OURS_HEALTHY)

    def test_an_unreadable_machine_is_unknown_never_free(self) -> None:
        rows = self.audit(None)
        self.assertEqual({r["state"] for r in rows.values()}, {ports.UNKNOWN})

    def test_the_summary_leads_with_problems_and_shouts_about_the_tunnel(self) -> None:
        rows = ports.audit(self.services, listeners=[
            _listener(18001, "node.exe", "node proxy.js", pid=9),
            _listener(8771, "python.exe", "py -m daedalus.server")], probe=lambda p, path: False)
        out = ports.summary(rows)
        text = "\n".join(out["alerts"])
        self.assertIn("VPS tunnel (loopback ends) :18001", text)
        self.assertIn("tunnel", text.lower())
        self.assertIn("Daedalus :8771", text)
        self.assertEqual(out["counts"][ports.FOREIGN], 1)
        self.assertEqual(out["counts"][ports.OURS_UNHEALTHY], 1)

    def test_a_quiet_machine_raises_no_alert(self) -> None:
        self.assertEqual(ports.summary(ports.audit(self.services, listeners=[]))["alerts"], [])

    def test_the_real_listener_reader_parses_what_powershell_prints(self) -> None:
        class Done:
            returncode = 0
            stdout = json.dumps([{"port": 8771, "pid": 5, "name": "python.exe",
                                  "command": "py -m daedalus.server"}])
        got = ports.read_listeners(run=lambda *a, **k: Done())
        self.assertEqual(got, [Listener(8771, 5, "python.exe", "py -m daedalus.server")])

        class One(Done):
            stdout = json.dumps({"port": 1, "pid": 0, "name": None, "command": None})
        self.assertEqual(ports.read_listeners(run=lambda *a, **k: One())[0].name, "")

        class Failed(Done):
            returncode = 1
        self.assertIsNone(ports.read_listeners(run=lambda *a, **k: Failed()))

        def boom(*a, **k):
            raise OSError("no powershell")
        self.assertIsNone(ports.read_listeners(run=boom))


# ---- the launcher's own functions, run for real over synthetic machines -----------------------

def _lift(text: str, start: str, end: str) -> str:
    return text[text.index(start):text.index(end)]


SCENE_HEAD = r"""
$ErrorActionPreference = 'Stop'
function Enc2([string]$t) { [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($t)) }
function P($id, $parent, $name, $cmd, $exe = '', $born = 100) {
    [pscustomobject]@{ ProcessId = $id; ParentProcessId = $parent; Name = $name; CommandLine = $cmd
                       ExecutablePath = $exe; CreationDate = $born }
}
function L($port, $procId) { [pscustomobject]@{ Port = $port; OwnerPid = $procId } }
$paneBody = "`$env:PIONIR_PANE='1'; `$host.UI.RawUI.WindowTitle='Pionir :8780'; Set-Location 'C:\src\Pionir'; x"
$paneCmd = "powershell -ExecutionPolicy Bypass -EncodedCommand " + (Enc2 $paneBody)
$stopped = New-Object System.Collections.ArrayList
function Stop-Process { param($Id, [switch]$Force, $ErrorAction) [void]$stopped.Add([int]$Id) }
"""

SCENE_MACHINE = r"""
$script:procs = @{}
foreach ($p in @(
    (P 100 1 'explorer.exe' 'C:\Windows\explorer.exe'),
    # a Pionir pane: pane shell -> python dashboard, and a pane crew with no listener yet
    (P 200 100 'powershell.exe' $paneCmd),
    (P 201 200 'python.exe' 'python -m pionir server --port 8780' 'C:\py\python.exe'),
    (P 202 200 'python.exe' 'python -m pionir.crew' 'C:\py\python.exe'),
    # Pionir Desktop: electron (dev, run from its folder) -> python daedalus
    (P 300 100 'electron.exe' '"C:\src\pionir-desktop\node_modules\electron\dist\electron.exe" .' 'C:\src\pionir-desktop\node_modules\electron\dist\electron.exe'),
    (P 301 300 'python.exe' 'python -m daedalus.server' 'C:\py\python.exe'),
    # a foreign program squatting on Galatea's port, and a shell in the desktop folder (not Desktop)
    (P 400 100 'python.exe' 'python -m http.server 8799' 'C:\py\python.exe'),
    (P 500 100 'powershell.exe' 'powershell -NoExit -Command cd C:\src\pionir-desktop'),
    (P 501 500 'node.exe' 'node vite.js' 'C:\nodejs\node.exe')
)) { $script:procs[[int]$p.ProcessId] = $p }
$script:listen = @((L 8780 201), (L 8771 301), (L 8799 400), (L 8774 201), (L 9999 501))
function Test-Answers([int]$p, $spec) { return $script:answers }
$script:answers = $true
"""


def _scene(body: str) -> str:
    text = PS1.read_text(encoding="utf-8-sig")
    functions = _lift(text, "function Test-PionirPane", "function Get-Companions")
    stack = _lift(text, "$registryFile = ", "$script:refused = @()")
    stop = _lift(text, "function Stop-Port", "function Stop-Bryo")
    return (f"$srcDir = '{SRC.parent}'\n" + SCENE_HEAD + functions + stack + stop + SCENE_MACHINE
            + body)


def _run(body: str) -> tuple[list[str], dict]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "scene.ps1"
        path.write_text(_scene(body), encoding="utf-8-sig")
        done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                               "Bypass", "-File", str(path)], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode == 0, done.stdout + done.stderr
    lines = done.stdout.strip().splitlines()
    return lines[:-1], json.loads(lines[-1])


@unittest.skipUnless(os.name == "nt", "runs the launcher's PowerShell")
class LauncherPortStateTests(unittest.TestCase):
    def test_states_by_command_line_not_just_listening(self) -> None:
        _, out = _run(r"""
$r = [ordered]@{}
foreach ($pair in @(@('dashboard', 8780), @('daedalus', 8771), @('galatea', 8799), @('melete', 8770))) {
    $s = Get-PortState $pair[0] $pair[1]; $r[$pair[0]] = @($s.State, [string]$s.Name, [int]$s.OwnerPid)
}
$script:answers = $false
$s = Get-PortState 'daedalus' 8771; $r['daedalus-silent'] = @($s.State)
$r | ConvertTo-Json -Compress""")
        self.assertEqual(out["dashboard"], ["ours-healthy", "python.exe", 201])
        self.assertEqual(out["daedalus"], ["ours-healthy", "python.exe", 301])
        self.assertEqual(out["galatea"], ["foreign", "python.exe", 400])   # listening, not Galatea
        self.assertEqual(out["melete"][0], "free")
        self.assertEqual(out["daedalus-silent"], ["ours-unhealthy"])

    def test_owner_by_ancestry_pane_desktop_or_hand(self) -> None:
        _, out = _run(r"""
@{ pane = (Get-Owner 201); desktop = (Get-Owner 301); squatter = (Get-Owner 400)
   shellInDesktopFolder = (Get-Owner 501) } | ConvertTo-Json -Compress""")
        self.assertEqual(out, {"pane": "pionir.ps1", "desktop": "desktop", "squatter": "hand",
                               "shellInDesktopFolder": "hand"})

    def test_a_recycled_parent_pid_is_not_a_parent(self) -> None:
        _, out = _run(r"""
$script:procs[[int]300] = (P 300 100 'electron.exe' '"C:\src\pionir-desktop\node_modules\electron\dist\electron.exe" .' 'C:\src\pionir-desktop\node_modules\electron\dist\electron.exe' 500)
$script:procs[[int]301] = (P 301 300 'python.exe' 'python -m daedalus.server' 'C:\py\python.exe' 100)
@{ owner = (Get-Owner 301) } | ConvertTo-Json -Compress""")
        self.assertEqual(out["owner"], "hand")      # the "parent" was born after the child

    def test_the_stack_owner_is_desktop_when_any_core_service_is_desktops(self) -> None:
        _, out = _run(r"""
$a = Get-StackOwner 8780
$script:listen = @((L 8780 201), (L 8774 201))
$b = Get-StackOwner 8780
$script:listen = @((L 8780 201))
$script:procs[[int]200] = (P 200 100 'powershell.exe' 'powershell -NoExit')
$c = Get-StackOwner 8780
$script:listen = @()
$d = Get-StackOwner 8780
@{ mixed = $a; panesOnly = $b; handOnly = $c; empty = $d } | ConvertTo-Json -Compress""")
        self.assertEqual(out, {"mixed": "desktop", "panesOnly": "pionir.ps1", "handOnly": "hand",
                               "empty": "none"})

    def test_claim_port_free_up_or_held_and_a_foreign_listener_is_never_up(self) -> None:
        said, out = _run(r"""
$script:refused = @()
$r = [ordered]@{
  free = (Claim-Port 'melete' 8770)
  up = (Claim-Port 'dashboard' 8780)
  upDesktop = (Claim-Port 'daedalus' 8771)
  held = (Claim-Port 'galatea' 8799)
}
$script:answers = $false
$r['silent'] = (Claim-Port 'daedalus' 8771)
$r['refused'] = @($script:refused)
$r | ConvertTo-Json -Compress""")
        self.assertEqual(out["free"], "free")
        self.assertEqual(out["up"], "up")
        self.assertEqual(out["held"], "held")
        self.assertEqual(out["silent"], "up")              # ours, not answering: never started over
        self.assertEqual(len(out["refused"]), 1)
        self.assertIn("galatea", out["refused"][0].lower())
        said = "\n".join(said)
        self.assertIn("is held by python.exe (pid 400)", said)
        self.assertIn("(pid 301, Pionir Desktop)", said)   # a healthy service names its owner
        self.assertIn("(pid 201, this launcher (pionir.ps1))", said)
        self.assertNotIn("Galatea (Moss) already up", said)

    def test_a_slow_starter_alive_without_its_port_is_running_not_down(self) -> None:
        _, out = _run(r"""
@{ crewAlive = (Test-Running (Get-PortSpec 'crew')); meleteAlive = (Test-Running (Get-PortSpec 'melete')) } | ConvertTo-Json -Compress""")
        self.assertEqual(out, {"crewAlive": True, "meleteAlive": False})

    def test_stop_kills_only_what_the_registry_says_is_ours(self) -> None:
        said, out = _run(r"""
function Read-Stack { }       # the synthetic machine stays as built
Stop-Port 8799 "Galatea" "galatea"
Stop-Port 8771 "Daedalus" "daedalus"
Stop-Port 8770 "Melete" "melete"
@{ killed = @($stopped) } | ConvertTo-Json -Compress""")
        self.assertEqual(out["killed"], [301])             # Daedalus only; the squatter is left alone
        said = "\n".join(said)
        self.assertIn("held by python.exe (pid 400), which is not Galatea; left alone", said)

    def test_the_foreign_tunnel_port_is_named_by_its_holder(self) -> None:
        _, out = _run(r"""
$script:listen = @((L 18001 501))
$s = Get-PortState 'tunnel' 18001
$script:procs[[int]701] = (P 701 200 'ssh.exe' 'ssh -N -L 127.0.0.1:18000:127.0.0.1:8000 pionir-tunnel@vps' 'C:\Windows\System32\OpenSSH\ssh.exe')
$script:listen = @((L 18000 701))
$t = Get-PortState 'tunnel' 18000
@{ foreign = @($s.State, $s.Name); tunnel = $t.State } | ConvertTo-Json -Compress""")
        self.assertEqual(out["foreign"], ["foreign", "node.exe"])
        self.assertEqual(out["tunnel"], "ours-healthy")


@unittest.skipUnless(os.name == "nt", "runs the launcher's PowerShell")
class LauncherLogTests(unittest.TestCase):
    def test_the_log_is_written_and_size_capped_with_one_rotation(self) -> None:
        text = PS1.read_text(encoding="utf-8-sig")
        lifted = _lift(text, "$logDir  = ", "function Write-Host")
        with tempfile.TemporaryDirectory() as tmp:
            body = (lifted + "\n$logCap = 400\n"
                    "1..40 | ForEach-Object { Write-Log \"line $_ of the launch, padded out a little\" }\n"
                    "Write-Log '   '\n"
                    "@{ main = (Get-Item $logFile).Length; rotated = (Test-Path \"$logFile.1\");"
                    " third = (Test-Path \"$logFile.2\") } | ConvertTo-Json -Compress\n")
            path = Path(tmp) / "log.ps1"
            path.write_text(body, encoding="utf-8-sig")
            env = {**os.environ, "PIONIR_LAUNCHER_LOG_DIR": str(Path(tmp) / "logs")}
            done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                                   "-ExecutionPolicy", "Bypass", "-File", str(path)],
                                  stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                  timeout=120, env=env, check=False)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            out = json.loads(done.stdout.strip().splitlines()[-1])
            lines = (Path(tmp) / "logs" / "launcher.log").read_text(encoding="utf-8-sig").splitlines()
        self.assertTrue(out["rotated"])
        self.assertFalse(out["third"])                     # one generation only: the cap holds
        self.assertLess(out["main"], 400 + 200)
        self.assertTrue(all(re.match(r"\d{4}-\d\d-\d\dT.* \[\d+\] line \d+", ln) for ln in lines))

    def test_the_launcher_logs_what_it_says_and_where(self) -> None:
        text = PS1.read_text(encoding="utf-8-sig")
        self.assertIn('Join-Path $HOME ".pionir\\logs"', text)
        self.assertIn('"launcher.log"', text)
        self.assertRegex(text, r"function Write-Host \{[\s\S]*?Write-Log \(\[string\]\$Object\)")
        self.assertIn('Write-Log ("launch: "', text)


class LauncherShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = PS1.read_text(encoding="utf-8-sig")

    def test_the_launcher_reads_the_registry_not_a_copy(self) -> None:
        self.assertIn('Join-Path $srcDir "pionir\\ports.json"', self.text)

    def test_no_start_decision_trusts_a_bare_listening_port(self) -> None:
        # the old "if (Test-Port N) { already up }" is what called a foreign program "up"
        decide = _lift(self.text, "$panes = @()", "function Write-Refused")
        self.assertNotIn("Test-Port", decide)
        for sid in ("dashboard", "galatea", "daedalus", "melete", "crew", "peter"):
            self.assertIn(f"Claim-Port '{sid}'", decide)

    def test_a_desktop_owned_stack_is_refused_before_any_pane_is_built(self) -> None:
        refuse = self.text.index("if ($stackOwner -eq 'desktop' -and $panes.Count -gt 0)")
        self.assertLess(refuse, self.text.index("Close-EmptyPanes\n# A new launch"))
        self.assertLess(refuse, self.text.index("Start-Process $wt"))
        block = self.text[refuse:refuse + 1400]
        self.assertIn("Pionir Desktop already owns this stack", block)
        self.assertIn("exit 3", block)
        self.assertNotIn("Stop-", block)                    # refusing stops nothing

    def test_the_tunnel_warning_is_loud_and_names_the_holder(self) -> None:
        block = _lift(self.text, "if (-not $NoTunnel) {", "# Bryo, the observer organism")
        self.assertRegex(block, r"!!! VPS TUNNEL.*-ForegroundColor Red")
        self.assertIn("$tunnelNote", block)
        self.assertIn("$script:refused +=", block)
        self.assertLess(block.index("$tunnelForeign.Count"), block.index('Pane-Cmd "VPS tunnel"'))

    def test_a_crew_that_is_alive_but_not_listening_says_warming(self) -> None:
        self.assertRegex(self.text, r"WARMING :\{0\}.*warm-up")
        self.assertIn("Test-Running $spec", self.text)

    def test_nothing_installs_itself(self) -> None:
        for forbidden in ("Register-ScheduledTask", "schtasks", "New-Service", "CurrentVersion\\Run",
                          "Startup\\"):
            self.assertNotIn(forbidden, self.text)


if __name__ == "__main__":
    unittest.main()
