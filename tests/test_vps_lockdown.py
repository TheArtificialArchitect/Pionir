"""tools\\vps-lockdown.ps1, run for real against a FAKE droplet.

The script runs in Windows PowerShell with ssh, ssh-keygen and gh on PATH replaced by a
recorder (a .cmd that hands its argv and stdin to a Python fake). The fake ssh runs the
real remote half (tools/vps_lockdown/remote.py) for the steps that touch files - env and
deploy - against a temp "VPS" directory, and answers discover / tunnel_user / restart /
firewall with canned facts. The HTTP checks go through the script's temporary tunnel ports,
where stand-in APIs judge keys by the fake VPS's env files as they are at that moment.

Pinned: a dry run touches nothing; -Apply rotates every key over STDIN (never an argv),
never prints a key, keeps the old Robinhood key as PRO_RH_API_KEY_PREVIOUS for the phone,
gives the desktop READ keys only, sets the phone's GitHub secrets over stdin, closes no
public port its consumers still use; a re-run changes nothing; -FinishRotation kills the
old key. The remote half's file logic is tested on its own below.
"""
import base64
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "vps-lockdown.ps1"
REMOTE = ROOT / "tools" / "vps_lockdown" / "remote.py"

spec = importlib.util.spec_from_file_location("lockdown_remote", REMOTE)
remote = importlib.util.module_from_spec(spec)
spec.loader.exec_module(remote)

OLD = {"rh": "oldRobinhoodFullKey_" + "a" * 24, "prom": "oldPrometheusKey_" + "b" * 24,
       "kark": "oldKarkinosKey_" + "c" * 24}
PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyForTests0123456789abcdefghijklmnopq pionir-tunnel"

FAKE = r'''
import base64, importlib.util, json, os, sys, time
tool, argv = sys.argv[1], sys.argv[2:]
def record(stdin):
    with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"tool": tool, "argv": argv, "stdin": stdin}) + "\n")
if tool == "ssh" and "-N" in argv:
    record("")
    time.sleep(float(os.environ.get("FAKE_TUNNEL_SECONDS", "20")))
    sys.exit(0)
stdin = sys.stdin.buffer.read().decode("utf-8").lstrip("\ufeff")
if tool == "gh" and "-f" in argv:
    stdin = open(argv[argv.index("-f") + 1], encoding="utf-8").read()   # the dotenv gh reads
record(stdin)
vps = os.environ["FAKE_VPS"]
if tool == "ssh-keygen":
    if "-F" in argv:
        print("# Host 174.138.35.184 found: line 3")
        print("174.138.35.184 ssh-ed25519 AAAAhostkey")
        sys.exit(0)
    f = argv[argv.index("-f") + 1]
    open(f, "w").write("FAKE PRIVATE KEY\n")
    open(f + ".pub", "w").write(os.environ["FAKE_PUB"] + "\n")
    sys.exit(0)
if tool == "gh":
    sys.exit(int(os.environ.get("FAKE_GH_CODE", "0")))
first, rest = stdin.split("\n", 1)
program = base64.b64decode(first)
if program != open(os.environ["FAKE_REMOTE"], "rb").read():
    print(json.dumps({"error": "not the remote half"}))
    sys.exit(3)
payload = json.loads(rest)
spec = importlib.util.spec_from_file_location("r", os.environ["FAKE_REMOTE"])
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
step = payload["step"]
j = lambda *p: os.path.join(vps, *p)
if step == "discover":
    out = {"step": "discover", "apis": {
        "robinhood": {"unit": "pro-robinhood-api.service", "active": "active", "key_name": "PRO_RH_API_KEY",
                      "key_file": j("rh_api.env"), "env_files": [j("rh_api.env")], "script": j("robinhood_read_api.py"),
                      "read_key_support": False, "orders_armed": False},
        "prometheus": {"unit": "prometheus-api.service", "active": "active", "key_name": "PROM_API_KEY",
                       "key_file": j("pro.env"), "env_files": [j("pro.env")]},
        "karkinos": {"unit": "mrcrab-api.service", "active": "active", "key_name": "KARKINOS_API_KEY",
                     "key_file": j("mrcrab.env"), "env_files": [j("mrcrab.env")]}},
        "tunnel_user": {"exists": False, "authorized_keys": False}, "sshd": {"allowtcpforwarding": "yes"},
        "ufw": "Status: inactive", "listeners": ["0.0.0.0:8000 python3"], "nginx_to_apis": [],
        "tailscale": False, "python": "3.12.3"}
elif step == "env":
    out = r.step_env(payload)
elif step == "deploy":
    payload["path"] = j("robinhood_read_api.py")
    out = r.step_deploy(payload)
elif step == "tunnel_user":
    line = r.authorized_line(payload["pubkey"], payload["opens"])
    open(j("authorized_keys"), "w").write(line)
    out = {"step": "tunnel_user", "user": payload["user"], "created": True, "forwarding_allowed": True,
           "authorized_keys": "/home/%s/.ssh/authorized_keys" % payload["user"], "allowusers": None}
elif step == "restart":
    env = {}
    for name in ("rh_api.env", "pro.env", "mrcrab.env"):
        env.update(r.parse_env(open(j(name)).read()))
    out = {"step": "restart", "units": [
        {"unit": u, "state": "active", "restarted": True, "orders_armed_before": False, "orders_armed_after": False,
         "env_present": sorted(n for n in names if env.get(n)), "env_missing": sorted(n for n in names if not env.get(n))}
        for u, names in payload["units"].items()]}
elif step == "firewall":
    out = {"step": "firewall", "was_active": False, "tailscale": False, "ok": True,
           "rules": [{"rule": "allow 22/tcp", "exit": 0}] + [{"rule": "deny %s/tcp" % p, "exit": 0} for p in payload["ports"]]}
else:
    out = {"error": "unknown step"}
print(json.dumps(out))
'''


class StandIns:
    """The three APIs behind the verify tunnel's ports, judging keys by the fake VPS env files."""

    def __init__(self, vps: Path):
        self.vps = vps
        self.servers = []
        for base in range(18150, 18450, 3):
            try:
                self.servers = [self._serve(base + i, api) for i, api in enumerate(("rh", "prom", "kark"))]
                self.base = base
                return
            except OSError:
                self.close()
        raise RuntimeError("no three free consecutive ports for the stand-ins")

    def env(self, name: str) -> dict:
        return remote.parse_env((self.vps / name).read_text(encoding="utf-8"))

    def _serve(self, port: int, api: str) -> ThreadingHTTPServer:
        outer = self

        class H(BaseHTTPRequestHandler):
            def _send(self, code):
                self.send_response(code)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def _keys(self):
                key = self.headers.get("x-api-key") or ""
                if api == "rh":
                    e = outer.env("rh_api.env")
                    full = {e.get("PRO_RH_API_KEY"), e.get("PRO_RH_API_KEY_PREVIOUS")} - {None, ""}
                    return key in full, key == e.get("PRO_RH_READ_KEY")
                e = outer.env("pro.env" if api == "prom" else "mrcrab.env")
                ok = key == e.get("PROM_API_KEY" if api == "prom" else "KARKINOS_API_KEY")
                return ok, False

            def do_GET(self):
                if self.path in ("/health", "/api/health"):
                    return self._send(200)
                full, read = self._keys()
                return self._send(200 if (full or read) else 401)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                full, _ = self._keys()
                return self._send(400 if full else 401)   # /never {} with a full key: "symbol required"

            def log_message(self, *a):
                pass

        class Server(ThreadingHTTPServer):
            allow_reuse_address = False   # on Windows SO_REUSEADDR lets a second server share the port

        server = Server(("127.0.0.1", port), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def close(self):
        for s in self.servers:
            s.shutdown()
            s.server_close()
        self.servers = []


@unittest.skipUnless(os.name == "nt", "runs the lockdown's PowerShell")
class LockdownRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pionir-lockdown-"))
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        fake = self.tmp / "fake_tool.py"
        fake.write_text(FAKE, encoding="utf-8")
        for tool in ("ssh", "ssh-keygen", "gh"):
            (self.bin / f"{tool}.cmd").write_text(f'@"{sys.executable}" "{fake}" {tool} %*\r\n', encoding="ascii")
        self.vps = self.tmp / "vps"
        self.vps.mkdir()
        (self.vps / "rh_api.env").write_text(f"# the RH API\nPRO_RH_API_KEY={OLD['rh']}\nPRO_RH_ORDERS_ENABLED=0\n", encoding="utf-8")
        (self.vps / "pro.env").write_text(f"PROM_API_KEY={OLD['prom']}\nALPACA_BASE_URL=https://paper-api.alpaca.markets\n", encoding="utf-8")
        (self.vps / "mrcrab.env").write_text(f"export KARKINOS_API_KEY=\"{OLD['kark']}\"\nMRCRAB_MODE=paper\n", encoding="utf-8")
        (self.vps / "robinhood_read_api.py").write_text("OLD = True\n", encoding="utf-8")
        self.server_file = self.tmp / "robinhood_read_api.py"
        self.server_file.write_text("import os\nREAD = os.environ.get('PRO_RH_READ_KEY')\n"
                                    "PREV = os.environ.get('PRO_RH_API_KEY_PREVIOUS')\n", encoding="utf-8")
        self.state = self.tmp / "pionir"
        secrets = self.state / "secrets"
        secrets.mkdir(parents=True)
        (secrets / "proteus-api-key.txt").write_text(OLD["rh"], encoding="utf-8")
        (secrets / "prometheus-api-key.txt").write_text(OLD["prom"], encoding="utf-8")
        (secrets / "karkinos-read-key.txt").write_text(OLD["kark"], encoding="utf-8")
        self.deploy_key = self.tmp / "proteus_deploy"
        self.deploy_key.write_text("FAKE ROOT KEY", encoding="ascii")
        self.log = self.tmp / "log.jsonl"
        self.apis = StandIns(self.vps)
        self.addCleanup(lambda: self.apis.close())

    def run_script(self, *args: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.update({"PATH": str(self.bin) + os.pathsep + env["PATH"], "FAKE_LOG": str(self.log),
                    "FAKE_VPS": str(self.vps), "FAKE_REMOTE": str(REMOTE), "FAKE_PUB": PUB,
                    "FAKE_TUNNEL_SECONDS": "15"})
        return subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
             "-StateRoot", str(self.state), "-DeployKey", str(self.deploy_key), "-ServerFile", str(self.server_file),
             "-VpsHost", "vps.test", "-VerifyPortBase", str(self.apis.base), *args],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300, env=env)

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line]

    def steps(self) -> list[str]:
        out = []
        for c in self.calls():
            if c["tool"] == "ssh" and "-N" not in c["argv"]:
                out.append(json.loads(c["stdin"].split("\n", 1)[1])["step"])
        return out

    def keys(self) -> dict:
        return json.loads((self.state / "vault" / "vps-lockdown.json").read_text(encoding="utf-8-sig"))["keys"]

    def env(self, name: str) -> dict:
        return remote.parse_env((self.vps / name).read_text(encoding="utf-8"))

    def assert_no_key_leaks(self, done: subprocess.CompletedProcess, values) -> None:
        printed = done.stdout + done.stderr
        for v in values:
            self.assertNotIn(v, printed)
            for c in self.calls():
                self.assertFalse(any(v in a for a in c["argv"]), f"a key on {c['tool']}'s command line")

    # ---- the runs -----------------------------------------------------------------------
    def test_a_dry_run_touches_nothing(self) -> None:
        done = self.run_script()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.state / "vault").exists())
        self.assertFalse((self.state / "secrets" / "vps-tunnel-key").exists())
        for step in ("[0]", "[a]", "[b]", "[c]", "[d]", "[e]", "[f]", "[g]", "[h]"):
            self.assertIn(step, done.stdout)
        self.assertIn("DRY RUN", done.stdout)
        self.assertEqual(self.env("rh_api.env")["PRO_RH_API_KEY"], OLD["rh"])

    def test_inspect_only_reads(self) -> None:
        done = self.run_script("-Inspect")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.steps(), ["discover"])
        self.assertFalse((self.state / "vault").exists())
        self.assertIn("pro-robinhood-api.service (active)", done.stdout)

    def test_apply_rotates_everything_over_stdin_and_prints_no_key(self) -> None:
        done = self.run_script("-Apply")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        keys = self.keys()
        for v in keys.values():
            self.assertRegex(v, r"^[A-Za-z0-9_-]{43}$")
        self.assertEqual(len(set(keys.values())), 4)
        self.assertEqual(self.steps(), ["discover", "tunnel_user", "deploy", "env", "restart"])
        # the droplet: rotated, the old RH key kept for the phone, everything else untouched
        rh, prom, kark = self.env("rh_api.env"), self.env("pro.env"), self.env("mrcrab.env")
        self.assertEqual(rh["PRO_RH_API_KEY"], keys["rh_full"])
        self.assertEqual(rh["PRO_RH_READ_KEY"], keys["rh_read"])
        self.assertEqual(rh["PRO_RH_API_KEY_PREVIOUS"], OLD["rh"])
        self.assertEqual(rh["PRO_RH_ORDERS_ENABLED"], "0")
        self.assertEqual(prom, {"PROM_API_KEY": keys["prom"], "ALPACA_BASE_URL": "https://paper-api.alpaca.markets"})
        self.assertEqual(kark, {"KARKINOS_API_KEY": keys["kark"], "MRCRAB_MODE": "paper"})
        self.assertIn("# the RH API", (self.vps / "rh_api.env").read_text(encoding="utf-8"))
        backups = sorted(p.name for p in self.vps.glob("*.bak-pionir-*"))
        self.assertEqual(len(backups), 4)          # three env files and the server file
        rh_backup = next(self.vps.glob("rh_api.env.bak-pionir-*"))
        self.assertIn(OLD["rh"], rh_backup.read_text(encoding="utf-8"))
        self.assertIn("PRO_RH_READ_KEY", (self.vps / "robinhood_read_api.py").read_text(encoding="utf-8"))
        # the tunnel account: forwarding to the three loopback ports and nothing else
        self.assertEqual((self.vps / "authorized_keys").read_text(encoding="ascii"),
                         'restrict,port-forwarding,permitopen="127.0.0.1:8000",permitopen="127.0.0.1:8001",'
                         f'permitopen="127.0.0.1:8002",command="/bin/false" {PUB}\n')
        # the verify checks, as status codes
        for line in ("robinhood GET /status with the READ key", "robinhood POST /never with the READ key (must be refused)",
                     "robinhood GET /status with the new FULL key", "robinhood GET /status with the previous key",
                     "prometheus GET /status with the OLD key (must be refused)", "karkinos GET /status with the new key"):
            self.assertRegex(done.stdout, r"ok\s+" + re.escape(line))
        self.assertNotIn("BAD", done.stdout)
        # this machine: the desktop holds READ keys only; the full key sits in the vault
        secrets, vault = self.state / "secrets", self.state / "vault"
        self.assertEqual((secrets / "proteus-read-key.txt").read_text(encoding="utf-8"), keys["rh_read"])
        self.assertEqual((secrets / "prometheus-api-key.txt").read_text(encoding="utf-8"), keys["prom"])
        self.assertEqual((secrets / "karkinos-read-key.txt").read_text(encoding="utf-8"), keys["kark"])
        self.assertFalse((secrets / "proteus-api-key.txt").exists())
        self.assertEqual((vault / "proteus-api-key.txt").read_text(encoding="utf-8"), keys["rh_full"])
        backup = next(vault.glob("backup-*"))
        self.assertEqual((backup / "proteus-api-key.txt").read_text(encoding="utf-8"), OLD["rh"])
        self.assertTrue((secrets / "vps-tunnel-key").exists())
        keygen = next(c for c in self.calls() if c["tool"] == "ssh-keygen" and "-t" in c["argv"])
        self.assertEqual(keygen["argv"][keygen["argv"].index("-N") + 1], "")   # an empty passphrase, passed
        # the phone: its three GitHub secrets over stdin, then its build
        gh = [c for c in self.calls() if c["tool"] == "gh"]
        secret_calls = [c for c in gh if c["argv"][:2] == ["secret", "set"]]
        self.assertEqual(len(secret_calls), 1)
        self.assertEqual(secret_calls[0]["argv"][2], "-f")
        self.assertEqual(remote.parse_env(secret_calls[0]["stdin"]),
                         {"PRO_RH_API_KEY": keys["rh_full"], "PROM_API_KEY": keys["prom"], "KARKINOS_API_KEY": keys["kark"]})
        self.assertFalse((self.state / "vault" / "phone-secrets.env").exists())   # deleted after gh read it
        self.assertIn(["workflow", "run", "build.yml", "--repo", "PreShotCome/trading-bot-app", "--ref", "main"], [c["argv"] for c in gh])
        # every value went over stdin; none was printed or put on a command line
        ssh_stdin = "".join(c["stdin"] for c in self.calls() if c["tool"] == "ssh")
        for v in keys.values():
            self.assertIn(v, ssh_stdin)
        self.assert_no_key_leaks(done, list(keys.values()) + list(OLD.values()))
        # the remote command line is the one fixed string
        remotes = {c["argv"][-1] for c in self.calls() if c["tool"] == "ssh" and "-N" not in c["argv"]}
        self.assertEqual(remotes, {"python3 -c 'import sys,base64;exec(base64.b64decode(sys.stdin.readline().lstrip(chr(65279))))'"})
        # no public port closed: the phone still uses all three
        self.assertNotIn("firewall", self.steps())
        self.assertIn(":8000 kept open", done.stdout)

    def test_a_rerun_changes_nothing_and_finish_kills_the_old_key(self) -> None:
        first = self.run_script("-Apply")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        keys = self.keys()
        rh_text = (self.vps / "rh_api.env").read_text(encoding="utf-8")
        n_gh = len([c for c in self.calls() if c["tool"] == "gh"])
        again = self.run_script("-Apply")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(self.keys(), keys)
        self.assertEqual((self.vps / "rh_api.env").read_text(encoding="utf-8"), rh_text)
        self.assertIn("changed []", again.stdout)
        self.assertIn("nothing changed since the last run: no restart", again.stdout)
        self.assertEqual(len([c for c in self.calls() if c["tool"] == "gh"]), n_gh)
        self.assertEqual(len(list(self.vps.glob("rh_api.env.bak-pionir-*"))), 1)
        # finish: the previous key is gone and refused
        fin = self.run_script("-FinishRotation", "-Apply")
        self.assertEqual(fin.returncode, 0, fin.stdout + fin.stderr)
        self.assertNotIn("PRO_RH_API_KEY_PREVIOUS", self.env("rh_api.env"))
        self.assertEqual(self.env("rh_api.env")["PRO_RH_API_KEY"], keys["rh_full"])
        self.assertRegex(fin.stdout, r"ok\s+robinhood GET /status with the OLD full key\s+-> 401")
        state = json.loads((self.state / "vault" / "vps-lockdown.json").read_text(encoding="utf-8-sig"))
        self.assertIsNone(state["keys"])
        self.assert_no_key_leaks(fin, list(keys.values()) + list(OLD.values()))
        # and a later run does not rotate again unless asked
        later = self.run_script("-Apply")
        self.assertEqual(later.returncode, 0, later.stdout + later.stderr)
        self.assertIn("keys are not rotated again", later.stdout)
        self.assertEqual(self.env("rh_api.env")["PRO_RH_API_KEY"], keys["rh_full"])

    def test_a_public_port_closes_only_when_its_consumers_moved(self) -> None:
        refused = self.run_script("-Apply", "-SkipPhone", "-ClosePorts", "8000")
        self.assertEqual(refused.returncode, 0, refused.stdout + refused.stderr)
        self.assertNotIn("firewall", self.steps())
        self.assertIn(":8000 kept OPEN: -ClosePorts names it but -PhoneMovedOffPublicPorts was not given", refused.stdout)
        self.assertFalse([c for c in self.calls() if c["tool"] == "gh"])
        moved = self.run_script("-Apply", "-SkipPhone", "-ClosePorts", "8000", "-PhoneMovedOffPublicPorts")
        self.assertEqual(moved.returncode, 0, moved.stdout + moved.stderr)
        fw = [json.loads(c["stdin"].split("\n", 1)[1]) for c in self.calls()
              if c["tool"] == "ssh" and "-N" not in c["argv"] and '"firewall"' in c["stdin"]]
        self.assertEqual([f["ports"] for f in fw], [[8000]])
        bad = self.run_script("-Apply", "-ClosePorts", "22")
        self.assertEqual(bad.returncode, 1)

    def test_a_tunnel_that_does_not_open_fails_the_run(self) -> None:
        self.apis.close()                        # nothing answers on the verify ports
        done = self.run_script("-Apply", "-SkipPhone")
        self.assertEqual(done.returncode, 1)
        self.assertIn("the temporary tunnel (the new restricted key) did not open", done.stdout)
        self.assertIn("FAILED", done.stdout)

    def test_a_check_that_answers_wrong_fails_the_run(self) -> None:
        # a Robinhood API that lets the READ key write: the run must not call that done
        original = StandIns._serve

        def loose(self, port, api):
            server = original(self, port, api)
            if api == "rh":
                handler = server.RequestHandlerClass
                handler.do_POST = lambda h: (h.rfile.read(int(h.headers.get("Content-Length", 0))), h._send(400))[1]
            return server
        self.apis.close()
        StandIns._serve = loose
        try:
            self.apis = StandIns(self.vps)
        finally:
            StandIns._serve = original
        done = self.run_script("-Apply", "-SkipPhone")
        self.assertEqual(done.returncode, 1)
        self.assertRegex(done.stdout, r"BAD\s+robinhood POST /never with the READ key \(must be refused\)\s+-> 400 \(want 401\)")


class ScriptShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = SCRIPT.read_text(encoding="utf-8-sig")
        self.code = "\n".join(line for line in self.text.splitlines() if not line.lstrip().startswith("#"))

    def test_windows_powershell_5_1_safe(self) -> None:
        self.assertNotIn("&&", self.code)
        self.assertNotIn("||", self.code)
        self.assertNotIn("??", self.code)
        self.assertNotRegex(self.code, r"\?\s*[\$\"'\w(].*\s:\s")   # no ternary
        self.assertNotRegex(self.code, r"(?m)^\s*using\s+namespace")

    def test_installs_nothing_that_starts_by_itself(self) -> None:
        for word in ("Register-ScheduledTask", "schtasks", "New-Service", "sc.exe", "CurrentVersion\\Run",
                     "systemctl enable", "crontab", "Startup"):
            self.assertNotIn(word, self.code)

    def test_keys_never_reach_a_command_line_or_the_screen(self) -> None:
        # every Write-Host that is not a fixed banner goes through Scrub (Say/Warn/Bad/Would)
        for line in self.code.splitlines():
            if "Write-Host" in line and "$state.keys" in line:
                self.fail(line)
        self.assertNotRegex(self.code, r"--body")
        self.assertIn('"-N", ""', self.code)      # the passphrase is empty, never a key


class RemoteHalfTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="pionir-remote-"))

    def test_env_update_keeps_every_other_line_and_backs_up_once(self) -> None:
        f = self.dir / "a.env"
        f.write_text('# keep me\nexport A="old-value-000000000000000000000000000"\nB=2\nA=dup\n\nC=3\n', encoding="utf-8")
        new = "N" * 43
        r = remote.update_env_file(str(f), {"A": new}, {"A": "A_PREV"}, [], "20260928T000000Z")
        self.assertEqual(r["changed"], ["A", "A_PREV"])
        text = f.read_text(encoding="utf-8")
        self.assertEqual(text, f"# keep me\nA={new}\nB=2\n\nC=3\nA_PREV=dup\n")
        backup = Path(r["backup"])
        self.assertIn("old-value", backup.read_text(encoding="utf-8"))
        again = remote.update_env_file(str(f), {"A": new}, {"A": "A_PREV"}, [], "20260928T000000Z")
        self.assertEqual(again["changed"], [])
        self.assertIn("old-value", backup.read_text(encoding="utf-8"))   # never overwritten
        gone = remote.update_env_file(str(f), {}, {}, ["A_PREV"], "20260928T000001Z")
        self.assertEqual(gone["unset"], ["A_PREV"])
        self.assertNotIn("A_PREV", f.read_text(encoding="utf-8"))

    def test_env_refuses_junk_and_says_missing(self) -> None:
        f = self.dir / "b.env"
        f.write_text("A=1\n", encoding="utf-8")
        for bad in ("short", "has space " + "x" * 40, "x" * 40 + "\nB=injected"):
            self.assertIn("error", remote.update_env_file(str(f), {"A": bad}, {}, [], "20260928T000000Z"))
        self.assertIn("error", remote.update_env_file(str(f), {"a; rm": "x" * 43}, {}, [], "20260928T000000Z"))
        self.assertEqual(f.read_text(encoding="utf-8"), "A=1\n")
        self.assertEqual(remote.update_env_file(str(self.dir / "none.env"), {}, {}, [], "s")["error"], "missing")
        self.assertIn("error", remote.step_env({"stamp": "x; rm -rf /", "files": []}))

    def test_a_kept_old_value_that_needs_quoting_is_quoted(self) -> None:
        f = self.dir / "c.env"
        f.write_text("K='old key with # and space'\n", encoding="utf-8")
        remote.update_env_file(str(f), {"K": "k" * 43}, {"K": "K_PREV"}, [], "20260928T000000Z")
        self.assertEqual(remote.parse_env(f.read_text(encoding="utf-8"))["K_PREV"], "old key with # and space")

    def test_authorized_line(self) -> None:
        line = remote.authorized_line(PUB, ["127.0.0.1:8000"])
        self.assertEqual(line, f'restrict,port-forwarding,permitopen="127.0.0.1:8000",command="/bin/false" {PUB}\n')
        for pub, opens in ((PUB + "\nssh-rsa AAAA evil", ["127.0.0.1:8000"]), ("ssh-rsa AAAA x", ["127.0.0.1:8000"]),
                           (PUB, ["0.0.0.0:22"]), (PUB, ["127.0.0.1:8000,no-restrict"]), (PUB, [])):
            with self.assertRaises(ValueError):
                remote.authorized_line(pub, opens)

    def test_systemctl_parsing(self) -> None:
        self.assertEqual(remote.env_files_from_show("/opt/prometheus/rh_api.env (ignore_errors=no) /x/y.env (ignore_errors=yes)"),
                         ["/opt/prometheus/rh_api.env", "/x/y.env"])
        exec_start = ("{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 /opt/prometheus/robinhood_read_api.py ; "
                      "ignore_errors=no ; start_time=[n/a] }")
        self.assertEqual(remote.script_from_execstart(exec_start, "robinhood_read_api.py"), "/opt/prometheus/robinhood_read_api.py")
        self.assertEqual(remote.script_from_execstart("argv[]=/usr/bin/python3 robinhood_read_api.py", "robinhood_read_api.py",
                                                      "/opt/prometheus"), "/opt/prometheus/robinhood_read_api.py")
        self.assertIsNone(remote.script_from_execstart("argv[]=/usr/bin/python3 other.py", "robinhood_read_api.py"))

    def test_deploy_checks_what_it_puts_in_place(self) -> None:
        target = self.dir / "robinhood_read_api.py"
        target.write_text("OLD = 1\n", encoding="utf-8")
        good = b"import os\nK = os.environ.get('PRO_RH_READ_KEY')\n"

        def pay(content, **kw):
            p = {"content_b64": base64.b64encode(content).decode(), "sha256": hashlib.sha256(content).hexdigest(),
                 "path": str(target), "stamp": "20260928T000000Z", "must_contain": ["PRO_RH_READ_KEY"]}
            p.update(kw)
            return p
        self.assertIn("error", remote.step_deploy(pay(good, sha256="0" * 64)))
        self.assertIn("error", remote.step_deploy(pay(b"x = 1\n")))
        self.assertIn("error", remote.step_deploy(pay(b"PRO_RH_READ_KEY = (\n")))
        self.assertEqual(target.read_text(encoding="utf-8"), "OLD = 1\n")
        done = remote.step_deploy(pay(good))
        self.assertTrue(done["deployed"])
        self.assertEqual(target.read_bytes(), good)
        self.assertEqual(Path(done["backup"]).read_text(encoding="utf-8"), "OLD = 1\n")
        self.assertTrue(remote.step_deploy(pay(good))["unchanged"])

    def test_restart_never_starts_and_firewall_closes_only_the_api_ports(self) -> None:
        source = REMOTE.read_text(encoding="utf-8")
        body = source[source.index("def step_restart"):source.index("def step_firewall")]
        self.assertIn('"try-restart"', body)
        self.assertNotRegex(body, r'\["systemctl", "(start|restart|enable|reload-or-restart)"')
        self.assertIn("error", remote.step_firewall({"ports": [22]}))
        self.assertIn("error", remote.step_firewall({"ports": []}))
        # nothing the remote half prints can carry a payload value
        self.assertNotRegex(source, r"print\((?!json\.dumps)")


if __name__ == "__main__":
    unittest.main()
