"""The VPS tunnel: the only road from this machine to the droplet's trading APIs.

- scripts\\vps-tunnel.ps1 is run for real against a FAKE ssh on PATH (it records its argv
  and exits): the ssh options, the three loopback forwards, the restricted account, the
  restart-with-backoff loop, and the refusal to run without its key. No real ssh runs.
- the forwards in the script are the adapter's TUNNEL_PORTS, clear of the estate's ports;
- the plane's tunnel section says up / partial / down from a fake probe, and the real
  probe refuses any port that is not a tunnel port (it can never be pointed at the VPS);
- pionir.ps1 opens it as a pane, knows it by command line, and -Stop stops it only when a
  Pionir pane started it.
"""
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from pionir.adapters._proc import ProcessResult
from pionir.adapters.proteus import (
    DEFAULT_HOST,
    TS_READER,
    TUNNEL_PORTS,
    ProteusAdapter,
    ProteusSettings,
    parse_status,
    read_tunnel_health,
    status_script,
    tailnet_key_view,
)

ROOT = Path(__file__).resolve().parent.parent
TUNNEL = ROOT / "scripts" / "vps-tunnel.ps1"
PS1 = ROOT / "pionir.ps1"
# Every port the estate listens on (the brief's list, plus the Desktop's reserved ones and
# genesis's local 8000): no tunnel end may take one.
ESTATE_PORTS = {8000, 8765, 8770, 8771, 8772, 8773, 8774, 8780, 8782, 8787, 8788, 8790, 8791, 8792,
                8799, 8801, 8830, 8831, 8840, 8841, 11434}

FAKE_SSH = "@echo off\r\necho %*>>\"%FAKE_SSH_LOG%\"\r\nexit /b %FAKE_SSH_CODE%\r\n"


def _run_tunnel(tmp: Path, *extra: str, key: bool = True, code: int = 255) -> tuple[subprocess.CompletedProcess, list[str]]:
    bin_dir = tmp / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "ssh.cmd").write_text(FAKE_SSH, encoding="ascii")
    log = tmp / "ssh.log"
    keyfile = tmp / "vps-tunnel-key"
    if key:
        keyfile.write_text("not a real key\n", encoding="ascii")
    env = dict(os.environ)
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    env["FAKE_SSH_LOG"] = str(log)
    env["FAKE_SSH_CODE"] = str(code)
    done = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
         str(TUNNEL), "-KeyFile", str(keyfile), "-VpsHost", "vps.test",
         "-InitialDelaySeconds", "0.05", *extra],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, env=env, check=False)
    lines = log.read_text(encoding="ascii").splitlines() if log.exists() else []
    return done, lines


@unittest.skipUnless(os.name == "nt", "runs the tunnel's PowerShell")
class TunnelScriptTests(unittest.TestCase):
    def test_ssh_is_run_with_the_restricted_account_and_three_loopback_forwards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            done, runs = _run_tunnel(Path(tmp), "-MaxAttempts", "1", code=0)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(len(runs), 1)
        argv = runs[0].split()
        self.assertEqual(argv[0], "-N")
        for opt in ("ExitOnForwardFailure=yes", "ServerAliveInterval=30", "ServerAliveCountMax=3",
                    "BatchMode=yes", "StrictHostKeyChecking=yes", "IdentitiesOnly=yes"):
            self.assertIn(opt, argv)
            self.assertEqual(argv[argv.index(opt) - 1], "-o")
        forwards = [argv[i + 1] for i, a in enumerate(argv) if a == "-L"]
        self.assertEqual(forwards, [f"127.0.0.1:{lp}:127.0.0.1:{rp}" for _, lp, rp in TUNNEL_PORTS])
        self.assertTrue(argv[argv.index("-i") + 1].endswith("vps-tunnel-key"))
        self.assertEqual(argv[-1], "pionir-tunnel@vps.test")    # never root
        self.assertNotIn("root@vps.test", argv)

    def test_a_dropped_tunnel_is_started_again_with_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            done, runs = _run_tunnel(Path(tmp), "-MaxAttempts", "3", code=255)
        self.assertEqual(done.returncode, 255)
        self.assertEqual(len(runs), 3)
        waits = [float(m) for m in re.findall(r"Retrying in ([\d.]+) s", done.stdout)]
        self.assertEqual(len(waits), 2)
        self.assertAlmostEqual(waits[1], waits[0] * 2)            # doubling
        self.assertIn("tunnel DOWN", done.stdout)

    def test_no_key_no_ssh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            done, runs = _run_tunnel(Path(tmp), "-MaxAttempts", "1", key=False)
        self.assertEqual(done.returncode, 2)
        self.assertEqual(runs, [])
        self.assertIn("vps-lockdown.ps1", done.stdout)


class TunnelShapeTests(unittest.TestCase):
    def test_the_script_forwards_exactly_the_adapters_ports(self) -> None:
        text = TUNNEL.read_text(encoding="utf-8-sig")
        forwards = re.findall(r'"(127\.0\.0\.1:\d+:127\.0\.0\.1:\d+)"', text)
        self.assertEqual(forwards, [f"127.0.0.1:{lp}:127.0.0.1:{rp}" for _, lp, rp in TUNNEL_PORTS])
        self.assertEqual({rp for _, _, rp in TUNNEL_PORTS}, {8000, 8001, 8002})
        self.assertIn(f'[string]$VpsHost = "{DEFAULT_HOST}"', text)
        self.assertIn('[string]$User = "pionir-tunnel"', text)

    def test_the_local_ends_are_clear_of_every_estate_port(self) -> None:
        local = {lp for _, lp, _ in TUNNEL_PORTS}
        self.assertEqual(len(local), 3)
        self.assertFalse(local & ESTATE_PORTS)

    def test_the_script_is_foreground_only(self) -> None:
        text = TUNNEL.read_text(encoding="utf-8-sig")
        for word in ("Register-ScheduledTask", "schtasks", "New-Service", "sc.exe",
                     "CurrentVersion\\Run", "Startup"):
            self.assertNotIn(word, text)


class PlaneTunnelTests(unittest.TestCase):
    def _plane(self, probe) -> dict:
        ssh = lambda argv, timeout: ProcessResult(returncode=0, stdout="", stderr="")
        adapter = ProteusAdapter(ProteusSettings(host="vps.test", key_file=Path("C:/k")),
                                 runner=ssh, peter_health=lambda: True, tunnel_health=probe)
        return adapter.plane()["tunnel"]

    def test_up_partial_down(self) -> None:
        self.assertEqual(self._plane(lambda port: "200")["state"], "up")
        self.assertEqual(self._plane(lambda port: "down")["state"], "down")
        mixed = self._plane(lambda port: "200" if port == 18000 else "down")
        self.assertEqual(mixed["state"], "partial")
        self.assertEqual(mixed["apis"]["robinhood"], {"local": f"127.0.0.1:{TUNNEL_PORTS[0][1]}", "remote_port": 8000,
                                                     "health": "200"})

    def test_a_probe_that_raises_is_down_not_a_crash(self) -> None:
        def boom(port):
            raise OSError("x")
        self.assertEqual(self._plane(boom)["state"], "down")

    def test_the_probe_asks_only_the_tunnel_ports_and_sends_no_key(self) -> None:
        asked = []
        self._plane(lambda port: asked.append(port) or "200")
        self.assertEqual(asked, [lp for _, lp, _ in TUNNEL_PORTS])

    def test_the_real_probe_refuses_anything_but_a_tunnel_port(self) -> None:
        for port in (8000, 8001, 8002, 22, 18003):
            with self.assertRaises(ValueError):
                read_tunnel_health(port)
        source = (ROOT / "src" / "pionir" / "adapters" / "proteus.py").read_text(encoding="utf-8")
        body = source[source.index("def read_tunnel_health"):source.index("def _tail")]
        self.assertNotIn("DEFAULT_HOST", body)
        self.assertNotIn("settings.host", body)
        self.assertNotIn("x-api-key", body.lower())


class TailnetKeyExpiryTests(unittest.TestCase):
    """Once the public ports close the tailnet is the phone's only road to STOP: the VPS's
    Tailscale key expiry is shown, and alarms from 14 days ahead."""
    NOW = 1_800_000_000.0

    def _iso(self, days: float) -> str:
        from datetime import UTC, datetime
        return datetime.fromtimestamp(self.NOW + days * 86400, UTC).isoformat().replace("+00:00", "Z")

    def test_the_judgement(self) -> None:
        self.assertEqual(tailnet_key_view("none", self.NOW), {"state": "key expiry disabled", "key_expiry": None, "alarm": False})
        far = tailnet_key_view(self._iso(60), self.NOW)
        self.assertEqual((far["days_left"], far["alarm"]), (60, False))
        self.assertIn("Disable key expiry", far["message"])
        self.assertIn("independent STOP route", far["message"])
        self.assertTrue(tailnet_key_view(self._iso(14), self.NOW)["alarm"])
        self.assertFalse(tailnet_key_view(self._iso(15), self.NOW)["alarm"])
        gone = tailnet_key_view(self._iso(-1), self.NOW)
        self.assertTrue(gone["alarm"])
        self.assertTrue(gone["state"].startswith("EXPIRED"))
        self.assertTrue(tailnet_key_view("garbage", self.NOW)["alarm"])

    def test_the_plane_reads_it_live_and_doctor_from_the_record(self) -> None:
        self.assertIn("ts_key_expiry", status_script(ProteusSettings(host="vps.test")))
        self.assertEqual(parse_status("ts_key_expiry 2027-01-01T00:00:00Z\n")["tailnet_key_expiry"], "2027-01-01T00:00:00Z")
        with tempfile.TemporaryDirectory() as tmp:
            record = Path(tmp) / "vps-tailnet.json"
            settings = ProteusSettings(host="vps.test", key_file=Path("C:/k"), tailnet_file=record)
            out = f"ts_key_expiry {self._iso(10)}\n"
            ssh = lambda argv, timeout: ProcessResult(returncode=0, stdout=out, stderr="")
            adapter = ProteusAdapter(settings, runner=ssh, peter_health=lambda: True,
                                     tunnel_health=lambda port: "200", clock=lambda: self.NOW)
            self.assertTrue(adapter.plane()["tailnet"]["alarm"])
            self.assertIn("not on the tailnet", adapter.status()["tailnet"]["state"])
            # doctor (offline) reads the RECORD, and a record is never shown as fine
            record.write_text(json.dumps({"ipv4": "100.64.0.7", "key_expiry": None}), encoding="utf-8")
            recorded = adapter.status()["tailnet"]
            self.assertEqual(recorded["state"], "UNVERIFIED - key expiry disabled (as recorded by the lockdown); "
                                                "only a live read of the VPS confirms it")
            self.assertFalse(recorded["verified"])
            self.assertIn("unverified", recorded["phone_state"])
            record.write_text(json.dumps({"ipv4": "100.64.0.7", "key_expiry": self._iso(3)}), encoding="utf-8")
            self.assertTrue(adapter.status()["tailnet"]["alarm"])

    def _plane(self, out: str, returncode: int = 0) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            record = Path(tmp) / "vps-tailnet.json"
            record.write_text(json.dumps({"ipv4": "100.64.0.7", "key_expiry": None}), encoding="utf-8")
            settings = ProteusSettings(host="vps.test", key_file=Path("C:/k"), tailnet_file=record)
            ssh = lambda argv, timeout: ProcessResult(returncode=returncode, stdout=out, stderr="")
            adapter = ProteusAdapter(settings, runner=ssh, peter_health=lambda: True,
                                     tunnel_health=lambda port: "200", clock=lambda: self.NOW)
            return adapter.plane()["tailnet"]

    def test_the_phones_key_expiry_alarms_too(self) -> None:
        fine = self._plane("ts_key_expiry none\nts_peer android none pixel\n")
        self.assertEqual((fine["alarm"], fine["phone_alarm"], fine["verified"]), (False, False, True))
        self.assertEqual(fine["phone_state"], "pixel: key expiry disabled")
        soon = self._plane(f"ts_key_expiry none\nts_peer android {self._iso(5)} pixel\n")
        self.assertTrue(soon["alarm"] and soon["phone_alarm"])
        self.assertIn("key expires in 5 days", soon["phone_state"])
        self.assertIn("Your phone (pixel)", soon["message"])
        self.assertIn("the phone (pixel)", soon["message"])
        self.assertIn("independent STOP route", soon["message"])
        self.assertFalse(soon["key_expiry"])  # the VPS's own key is fine; only the phone alarms
        gone = self._plane("ts_key_expiry none\nts_peer ios expired iphone\n")
        self.assertTrue(gone["phone_alarm"])
        self.assertIn("EXPIRED", gone["phone_state"])
        # a far phone key warns without alarming; the VPS alarming does not blame the phone
        far = self._plane(f"ts_key_expiry {self._iso(3)}\nts_peer android {self._iso(90)} pixel\n")
        self.assertTrue(far["alarm"])
        self.assertFalse(far["phone_alarm"])
        # no phone on the tailnet is said, not silently fine: a visible WARNING (not an alarm)
        nobody = self._plane("ts_key_expiry none\n")
        self.assertIn("no phone seen", nobody["phone_state"])
        self.assertIn("STOP route", nobody["phone_warning"])
        self.assertIn("unverifiable", nobody["phone_warning"])
        self.assertIn("independent STOP route", nobody["phone_warning"])
        self.assertFalse(nobody["alarm"] or nobody["phone_alarm"])
        # ... and a phone that IS seen clears it
        self.assertEqual(fine["phone_warning"], "")
        self.assertEqual(far["phone_warning"], "")

    def test_unknown_is_unverified_and_an_alarm_in_plane_and_doctor(self) -> None:
        view = tailnet_key_view("unknown", self.NOW)
        self.assertTrue(view["alarm"])
        self.assertTrue(view["state"].startswith("UNVERIFIED"))
        self.assertIn("independent STOP route", view["message"])
        live = self._plane("ts_key_expiry unknown\n")
        self.assertTrue(live["alarm"])
        self.assertFalse(live["verified"])
        self.assertTrue(live["state"].startswith("UNVERIFIED"), live["state"])
        self.assertTrue(live["message"])
        self.assertIn("unverified", live["phone_state"])
        self.assertEqual(live["phone_warning"], "")  # nothing to say about a phone we could not look for
        # doctor (offline): a record that says unknown alarms too - and is never shown as fine
        with tempfile.TemporaryDirectory() as tmp:
            record = Path(tmp) / "vps-tailnet.json"
            record.write_text(json.dumps({"ipv4": "100.64.0.7", "key_expiry": "unknown"}), encoding="utf-8")
            adapter = ProteusAdapter(ProteusSettings(host="vps.test", key_file=Path("C:/k"), tailnet_file=record),
                                     runner=lambda argv, timeout: ProcessResult(returncode=1, stdout="", stderr=""),
                                     peter_health=lambda: True, tunnel_health=lambda port: "200",
                                     clock=lambda: self.NOW)
            recorded = adapter.status()["tailnet"]
            self.assertTrue(recorded["alarm"])
            self.assertFalse(recorded["verified"])
            self.assertTrue(recorded["state"].startswith("UNVERIFIED"))
            self.assertNotIn("UNVERIFIED - UNVERIFIED", recorded["state"])

    def _reader(self, doc: object) -> list[str]:
        import subprocess
        import sys as _sys
        run = subprocess.run([_sys.executable, "-c", TS_READER], input=json.dumps(doc), capture_output=True, text=True, check=False)
        self.assertEqual(run.returncode, 0, run.stderr)
        return run.stdout.splitlines()

    def test_only_a_running_node_with_a_self_entry_can_report_expiry_disabled(self) -> None:
        peer = {"a": {"HostName": "pixel", "OS": "android"}}
        me = {"HostName": "proteus-vps"}  # no KeyExpiry: expiry disabled - but only if Running
        for name, doc in (
            ("NeedsLogin", {"BackendState": "NeedsLogin", "Self": me, "Peer": peer}),
            ("Stopped", {"BackendState": "Stopped", "Self": me, "Peer": peer}),
            ("Starting", {"BackendState": "Starting", "Self": me, "Peer": peer}),
            ("no state", {"Self": me, "Peer": peer}),
            ("empty", {}),
            ("running without Self", {"BackendState": "Running", "Peer": peer}),
            ("running with an empty Self", {"BackendState": "Running", "Self": {}, "Peer": peer}),
            ("running with a null Self", {"BackendState": "Running", "Self": None, "Peer": peer}),
        ):
            # exactly one line, "unknown", and no peers listed either
            self.assertEqual(self._reader(doc), ["ts_key_expiry unknown"], name)
        live = self._reader({"BackendState": "Running", "Self": me, "Peer": peer})
        self.assertEqual(live, ["ts_key_expiry none", "ts_peer android none pixel"])
        # and a non-JSON answer is a failed read (the remote shell then says unknown)
        import subprocess
        import sys as _sys
        run = subprocess.run([_sys.executable, "-c", TS_READER], input="not json", capture_output=True, text=True, check=False)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("|| echo 'ts_key_expiry unknown'", status_script(ProteusSettings(host="vps.test")))

    def test_a_stopped_or_logged_out_vps_alarms_in_the_plane_end_to_end(self) -> None:
        import subprocess
        import sys as _sys
        for state in ("NeedsLogin", "Stopped"):
            doc = {"BackendState": state, "Self": {"HostName": "proteus-vps"}, "Peer": {}}
            run = subprocess.run([_sys.executable, "-c", TS_READER], input=json.dumps(doc), capture_output=True, text=True, check=False)
            view = self._plane(run.stdout)
            self.assertTrue(view["alarm"], state)
            self.assertFalse(view["verified"], state)
            self.assertNotIn("expiry disabled", view["state"], state)

    def test_a_failed_live_read_falls_back_to_unverified_never_fine(self) -> None:
        for out, rc in (("", 255), ("ts_key_expiry none\n", 255), ("ts_key_expiry unknown\n", 0)):
            view = self._plane(out, rc)
            self.assertFalse(view["verified"], (out, rc))
            self.assertTrue(view["state"].startswith("UNVERIFIED"), view["state"])
            self.assertNotIn("phones", {k for k, v in view.items() if v})

    def test_the_remote_reader_reports_the_vps_and_only_phone_peers(self) -> None:
        import subprocess
        import sys as _sys
        doc = {"BackendState": "Running", "Self": {"HostName": "proteus-vps", "KeyExpiry": "2027-05-05T00:00:00Z"},
               "Peer": {"a": {"HostName": "pixel 7; rm -rf", "OS": "android", "KeyExpiry": "2027-01-02T03:04:05Z"},
                        "b": {"HostName": "desk", "OS": "windows", "KeyExpiry": "2027-01-01T00:00:00Z"},
                        "c": {"HostName": "iph", "OS": "iOS", "Expired": True}}}
        run = subprocess.run([_sys.executable, "-c", TS_READER], input=json.dumps(doc), capture_output=True, text=True, check=False)
        self.assertEqual(run.returncode, 0, run.stderr)
        lines = run.stdout.splitlines()
        self.assertEqual(lines[0], "ts_key_expiry 2027-05-05T00:00:00Z")
        self.assertEqual(sorted(lines[1:]), ["ts_peer android 2027-01-02T03:04:05Z pixel_7__rm_-rf", "ts_peer ios expired iph"])
        peers = parse_status(run.stdout)["tailnet_phones"]
        self.assertEqual([p["name"] for p in peers], ["pixel_7__rm_-rf", "iph"])
        self.assertNotIn("'", TS_READER)  # it sits in single quotes on the remote shell


LAUNCHER_SCENE = r"""
function Enc2([string]$t) { [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($t)) }
function P($id, $parent, $name, $cmd) { [pscustomobject]@{ ProcessId = $id; ParentProcessId = $parent; Name = $name; CommandLine = $cmd } }
$paneCmd = "powershell -ExecutionPolicy Bypass -EncodedCommand " + (Enc2 "`$env:PIONIR_PANE='1'; `$host.UI.RawUI.WindowTitle='VPS tunnel'; x")
$all = @(
  (P 10 1 'powershell.exe' $paneCmd),
  (P 11 10 'powershell.exe' 'powershell -NoProfile -ExecutionPolicy Bypass -File C:\src\Pionir\scripts\vps-tunnel.ps1'),
  (P 12 11 'ssh.exe' 'ssh -N -L 127.0.0.1:18000:127.0.0.1:8000 pionir-tunnel@vps'),
  (P 20 2 'powershell.exe' 'powershell -File C:\src\Pionir\scripts\vps-tunnel.ps1'),
  (P 30 3 'ssh.exe' 'ssh -N -L 18000:localhost:8000 root@vps')
)
@(Get-Companions $all $tunnelMatch | ForEach-Object { @{ pid = $_.Process.ProcessId; ours = $_.Ours } }) | ConvertTo-Json -Compress
"""


class LauncherTests(unittest.TestCase):
    def _text(self) -> str:
        return PS1.read_text(encoding="utf-8-sig")

    def test_the_tunnel_is_a_pane_started_only_with_its_key_and_free_ports(self) -> None:
        text = self._text()
        self.assertIn('Pane-Cmd "VPS tunnel" $root "powershell -NoProfile -ExecutionPolicy Bypass '
                      '-File $tunnelScript" ""', text)
        block = text[text.index("if (-not $NoTunnel) {"):text.index("# Bryo, the observer organism")]
        pane = block.index('Pane-Cmd "VPS tunnel"')
        for guard in ("$tunnelMatch", "@(18000, 18001, 18002 |", "Get-PortState 'tunnel' $_",
                      "$tunnelForeign.Count", "Test-Path $tunnelKey"):
            self.assertLess(block.index(guard), pane, guard)
        self.assertIn('$tunnelKey    = Join-Path $HOME ".pionir\\secrets\\vps-tunnel-key"', text)

    def test_stop_stops_the_tunnel_only_as_the_launchers_own(self) -> None:
        stop = re.search(r"if \(\$Stop\) \{([\s\S]*?)\n\}", self._text()).group(1)
        self.assertIn('Stop-Companion $tunnelMatch "the VPS tunnel"', stop)
        self.assertNotIn("18000", stop)

    @unittest.skipUnless(os.name == "nt", "runs the launcher's PowerShell")
    def test_ownership_by_command_line(self) -> None:
        text = self._text()
        start, end = text.index("$peterMatch = "), text.index("function Stop-Companion")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scene.ps1"
            path.write_text(text[start:end] + LAUNCHER_SCENE, encoding="utf-8-sig")
            done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                                   "Bypass", "-File", str(path)], stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=120, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        rows = json.loads(done.stdout.strip().splitlines()[-1])
        # the wrapper a pane started is ours; one started by hand is not; an ssh is never
        # matched on its own (killing the wrapper's tree takes its ssh along)
        self.assertEqual({r["pid"]: r["ours"] for r in rows}, {11: True, 20: False})


if __name__ == "__main__":
    unittest.main()
