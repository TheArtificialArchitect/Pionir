"""tools\\vps-lockdown.ps1, run for real against a FAKE droplet (tests/vpsfake.py).

The script runs in Windows PowerShell with ssh, ssh-keygen and gh on PATH replaced by a
recorder (a .cmd handing argv and stdin to a Python fake). The fake ssh runs the REAL
remote half (tools/vps_lockdown/remote.py) against the simulated droplet - systemd units
with /proc environments, ufw, transient timers, Tailscale, sshd, nginx - except `discover`
and the pwd-bound part of `tunnel_user`, which it answers from the simulator's facts. The
HTTP checks go through the script's temporary tunnel ports, where stand-in APIs judge keys
by the fake droplet's env files as they are at that moment. The server file comes from a
real temp git repository, pinned by commit and sha256.

Pinned: dry run and -Inspect change nothing; -Apply rotates over STDIN only, prints and
passes no key, keeps the phone's LIVE key as the previous one with a deadline, rotates a
bot's key only where its server has a grace window, restarts only stale units and never
one whose orders switch would flip, restores a Robinhood API that does not come back,
ships keys to this machine and the phone only after verification, puts the VPS on the
tailnet (login link shown, no auth key) and points the phone's build at it; -FinishRotation
asks Ian first, kills every previous key, then closes 8000-8002 with ssh proven between
each firewall step and an automatic revert that only a fresh login cancels.
"""
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import vpsfake

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "vps-lockdown.ps1"
REMOTE = ROOT / "tools" / "vps_lockdown" / "remote.py"


def load_remote():
    spec = importlib.util.spec_from_file_location(f"lockdown_remote_{time.monotonic_ns()}", REMOTE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


remote = load_remote()

OLD = {"rh": "oldRobinhoodFullKey_" + "a" * 24, "prom": "oldPrometheusKey_" + "b" * 24,
       "kark": "oldKarkinosKey_" + "c" * 24}
PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyForTests0123456789abcdefghijklmnopq pionir-tunnel"
SERVER = ("import os\nREAD = os.environ.get('PRO_RH_READ_KEY')\n"
          "UNTIL = os.environ.get('PRO_RH_API_KEY_PREVIOUS_UNTIL')\n").encode()
REMOTE_CMD = "python3 -c 'import sys,base64;exec(base64.b64decode(sys.stdin.readline().lstrip(chr(65279))))'"
TAILNET_ALLOW = ("allow", "in", "on", "tailscale0", "to", "any", "port", "8000:8002", "proto", "tcp")

FAKE = r'''
import importlib.util, json, os, sys, time, base64
tool, argv = sys.argv[1], sys.argv[2:]
def record(stdin):
    with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"tool": tool, "argv": argv, "stdin": stdin}) + "\n")
sys.path.insert(0, os.environ["FAKE_TESTS"])
import vpsfake
vps = os.environ["FAKE_VPS"]
sim = vpsfake.Sim(vps)
if tool == "ssh" and "-N" in argv:
    record("")
    if "-R" in argv and not os.environ.get("FAKE_ALLOW_R"):
        sys.exit(255)                     # "remote port forwarding failed"
    time.sleep(float(os.environ.get("FAKE_TUNNEL_SECONDS", "20")))
    sys.exit(0)
stdin = sys.stdin.buffer.read().decode("utf-8").lstrip("﻿")
if tool == "gh" and "-f" in argv:
    stdin = open(argv[argv.index("-f") + 1], encoding="utf-8").read()   # the dotenv gh reads
record(stdin)
if tool == "ssh-keygen":
    if "-F" in argv:
        print("# Host found: line 3")
        print("vps.test ssh-ed25519 AAAAhostkey")
        sys.exit(0)
    f = argv[argv.index("-f") + 1]
    open(f, "w").write("FAKE PRIVATE KEY\n")
    open(f + ".pub", "w").write(os.environ["FAKE_PUB"] + "\n")
    sys.exit(0)
if tool == "gh":
    sys.exit(int(os.environ.get("FAKE_GH_CODE", "0")))
st = sim.state()
if st.get("ssh_dead_after") and st["ssh_dead_after"] in st.get("log_steps", []):
    sys.exit(255)                         # nobody can log in any more
first, rest = stdin.split("\n", 1)
if base64.b64decode(first) != open(os.environ["FAKE_REMOTE"], "rb").read():
    print(json.dumps({"error": "not the remote half"})); sys.exit(3)
payload = json.loads(rest)
spec = importlib.util.spec_from_file_location("r", os.environ["FAKE_REMOTE"])
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
vpsfake.install(r, vps)
step = payload["step"]
s = sim.state()
s.setdefault("log_steps", []).append(step)
sim.save(s)
if step == "discover":
    st = sim.state()
    def api(key, unit, extra):
        u = st["units"].get(unit, {})
        env_file = u.get("env_file")
        return {"unit": unit, "active": "active" if u.get("active") else "inactive", "key_name": key,
                "key_file": env_file, "env_files": [env_file], **extra}
    rh_unit = "pro-robinhood-api.service"
    running = sim.running_env(rh_unit) if st["units"][rh_unit].get("active") else {}
    conf = sim._configured(st["units"][rh_unit])
    out = {"step": "discover", "apis": {
        "robinhood": api("PRO_RH_API_KEY", rh_unit, {
            "script": os.path.join(vps, "robinhood_read_api.py").replace(os.sep, "/"),
            "read_key_support": False, "previous_key_support": False,
            "orders_armed": running.get("PRO_RH_ORDERS_ENABLED") == "1" if running else None,
            "orders_configured": conf.get("PRO_RH_ORDERS_ENABLED") == "1"}),
        "prometheus": api("PROM_API_KEY", "prometheus-api.service", {
            "read_key_support": st.get("prom_read", True), "previous_key_support": st.get("prom_prev", True)}),
        "karkinos": api("KARKINOS_API_KEY", "mrcrab-api.service", {
            "read_key_support": False, "previous_key_support": st.get("kark_prev", True)})},
        "tunnel_user": {"exists": False, "authorized_keys": False}, "sshd": {"allowtcpforwarding": "yes"},
        "ufw": "Status: inactive", "listeners": ["0.0.0.0:8000 python3"],
        "nginx_to_apis": ["/etc/nginx/sites-enabled/trading-bot"], "tailscale": False, "python": "3.12.3"}
elif step == "tunnel_user":
    line = r.authorized_line(payload["pubkey"], payload["opens"])
    open(os.path.join(vps, "authorized_keys"), "w").write(line)
    sshd = r.install_sshd_block(payload["user"], list(payload["opens"]))
    if "error" in sshd:
        out = {"step": "tunnel_user", "error": sshd["error"]}
    else:
        out = {"step": "tunnel_user", "user": payload["user"], "created": True, "forwarding_allowed": True,
               "authorized_keys": "/home/%s/.ssh/authorized_keys" % payload["user"], "allowusers": None, **sshd}
else:
    if step == "deploy":                  # a Windows temp path is no absolute POSIX ExecStart path
        payload["path"] = os.path.join(vps, "robinhood_read_api.py").replace(os.sep, "/")
    out = r.STEPS[step](payload)
print(json.dumps(out))
'''


def until_alive(env: dict, name: str) -> bool:
    try:
        deadline = int(env.get(name + "_UNTIL", ""))
    except ValueError:
        return False
    return time.time() < deadline <= time.time() + 14 * 24 * 3600


class StandIns:
    """The three APIs behind the verify tunnel's ports, judging keys by the fake env files."""

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

    def judge(self, api: str, key: str, write: bool) -> bool:
        if api == "rh":
            e, full, read = self.env("rh_api.env"), "PRO_RH_API_KEY", "PRO_RH_READ_KEY"
        elif api == "prom":
            e, full, read = self.env("pro.env"), "PROM_API_KEY", "PROM_READ_KEY"
        else:
            e, full, read = self.env("mrcrab.env"), "KARKINOS_API_KEY", None
        ok = {e.get(full)}
        if until_alive(e, full + "_PREVIOUS"):
            ok.add(e.get(full + "_PREVIOUS"))
        ok.discard(None)
        ok.discard("")
        if key in ok:
            return True
        return (not write) and bool(read) and bool(e.get(read)) and key == e.get(read)

    def _serve(self, port: int, api: str) -> ThreadingHTTPServer:
        outer = self

        class H(BaseHTTPRequestHandler):
            def _send(self, code):
                self.send_response(code)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def do_GET(self):
                if self.path in ("/health", "/api/health"):
                    return self._send(200)
                return self._send(200 if outer.judge(api, self.headers.get("x-api-key") or "", False) else 401)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                return self._send(400 if outer.judge(api, self.headers.get("x-api-key") or "", True) else 401)

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
class LockdownRuns(unittest.TestCase):
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
        self.sim = vpsfake.Sim(self.vps)
        vpsfake.install(load_remote(), self.vps)      # lays out the fake droplet's directories
        (self.vps / "sites-enabled" / "trading-bot").write_text("server { listen 80; location / { proxy_pass http://127.0.0.1:8000; } }\n", encoding="utf-8")
        (self.vps / "sites-enabled" / "other").write_text("server { listen 80; root /var/www; }\n", encoding="utf-8")
        self.sim.boot_units()
        # the pinned server file: a real git repo, a real commit
        self.repo = self.tmp / "tba"
        (self.repo / "server").mkdir(parents=True)
        (self.repo / "server" / "robinhood_read_api.py").write_bytes(SERVER)
        git = ["git", "-C", str(self.repo), "-c", "user.email=t@t", "-c", "user.name=t", "-c", "core.autocrlf=false"]
        subprocess.run(git[:3] + ["init", "-q"], check=True)
        subprocess.run(git + ["add", "."], check=True)
        subprocess.run(git + ["commit", "-q", "-m", "x"], check=True)
        self.commit = subprocess.run(git[:3] + ["rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        self.sha = hashlib.sha256(SERVER).hexdigest()
        self.state = self.tmp / "pionir"
        secrets = self.state / "secrets"
        secrets.mkdir(parents=True)
        (secrets / "proteus-api-key.txt").write_text(OLD["rh"], encoding="utf-8")
        (secrets / "prometheus-api-key.txt").write_text(OLD["prom"], encoding="utf-8")
        (secrets / "karkinos-read-key.txt").write_text(OLD["kark"], encoding="utf-8")
        self.deploy_key = self.tmp / "proteus_deploy"
        self.deploy_key.write_text("FAKE ROOT KEY", encoding="ascii")
        self.log = self.tmp / "log.jsonl"
        self.extra_env = {}
        self.apis = StandIns(self.vps)
        self.addCleanup(lambda: self.apis.close())

    def run_script(self, *args: str, stdin: str = "", sha: str | None = None) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.update({"PATH": str(self.bin) + os.pathsep + env["PATH"], "FAKE_LOG": str(self.log),
                    "FAKE_VPS": str(self.vps), "FAKE_REMOTE": str(REMOTE), "FAKE_PUB": PUB,
                    "FAKE_TESTS": str(Path(__file__).resolve().parent), "FAKE_TUNNEL_SECONDS": "15"})
        env.update(self.extra_env)
        return subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
             "-StateRoot", str(self.state), "-DeployKey", str(self.deploy_key), "-ServerRepo", str(self.repo),
             "-ServerCommit", self.commit, "-ServerSha256", sha or self.sha, "-VpsHost", "vps.test",
             "-VerifyPortBase", str(self.apis.base), "-PollSeconds", "0", "-SkipTailnetProbe", *args],
            input=stdin, capture_output=True, text=True, timeout=400, env=env)

    # ---- reading what happened
    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line]

    def steps(self) -> list[str]:
        return [json.loads(c["stdin"].split("\n", 1)[1])["step"] for c in self.calls()
                if c["tool"] == "ssh" and "-N" not in c["argv"]]

    def keys(self) -> dict:
        return json.loads((self.state / "vault" / "vps-lockdown.json").read_text(encoding="utf-8-sig"))["keys"]

    def env(self, name: str) -> dict:
        return remote.parse_env((self.vps / name).read_text(encoding="utf-8"))

    def assert_no_leaks(self, done, values) -> None:
        printed = done.stdout + done.stderr
        for v in values:
            if not v:
                continue
            self.assertNotIn(v, printed)
            for c in self.calls():
                self.assertFalse(any(v in a for a in c["argv"]), f"a key on {c['tool']}'s command line")

    def apply_ok(self, *extra) -> subprocess.CompletedProcess:
        done = self.run_script("-Apply", *extra)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return done

    # ---- the runs --------------------------------------------------------------------------
    def test_a_dry_run_touches_nothing(self) -> None:
        done = self.run_script()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.state / "vault").exists())
        for step in ("[0]", "[a]", "[b]", "[t]", "[c]", "[d]", "[e]", "[f]", "[p]", "[g]", "[h]"):
            self.assertIn(step, done.stdout)
        self.assertEqual(self.env("rh_api.env")["PRO_RH_API_KEY"], OLD["rh"])

    def test_inspect_only_reads(self) -> None:
        done = self.run_script("-Inspect")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.steps(), ["discover"])
        self.assertFalse((self.state / "vault").exists())

    def test_apply_rotates_over_stdin_verifies_then_ships_and_puts_the_vps_on_the_tailnet(self) -> None:
        done = self.apply_ok()
        keys = self.keys()
        self.assertEqual(sorted(keys), ["kark", "prom", "prom_read", "rh_full", "rh_read"])
        for v in keys.values():
            self.assertRegex(v, r"^[A-Za-z0-9_-]{43}$")
        steps = self.steps()
        self.assertEqual(steps[:4], ["discover", "tunnel_user", "tailscale_install", "tailscale_up"])
        self.assertLess(steps.index("deploy"), steps.index("env"))
        self.assertLess(steps.index("env"), steps.index("restart"))
        self.assertNotIn("restore", steps)
        # the tailnet: the login link shown, no auth key anywhere, the address recorded
        self.assertIn("https://login.tailscale.com/a/pionirtest123", done.stdout)
        up = next(c for c in self.sim.commands() if c[:1] == ["systemd-run"] and "tailscale" in c)
        self.assertIn("--ssh=false", up)
        self.assertIn("--hostname=proteus-vps", up)
        self.assertFalse(any("authkey" in a or "auth-key" in a for a in up))
        tailnet = json.loads((self.state / "config" / "vps-tailnet.json").read_text(encoding="utf-8-sig"))
        self.assertEqual((tailnet["ipv4"], tailnet["dns_name"]), ("100.64.0.7", "proteus-vps.tail1234.ts.net"))
        # the droplet: rotated; the phone's LIVE key kept as previous, with a deadline
        rh, prom, kark = self.env("rh_api.env"), self.env("pro.env"), self.env("mrcrab.env")
        self.assertEqual((rh["PRO_RH_API_KEY"], rh["PRO_RH_READ_KEY"], rh["PRO_RH_API_KEY_PREVIOUS"]),
                         (keys["rh_full"], keys["rh_read"], OLD["rh"]))
        deadline = int(rh["PRO_RH_API_KEY_PREVIOUS_UNTIL"])
        self.assertTrue(time.time() + 6.9 * 86400 < deadline < time.time() + 7.1 * 86400)
        self.assertEqual(rh["PRO_RH_ORDERS_ENABLED"], "0")
        self.assertEqual((prom["PROM_API_KEY"], prom["PROM_API_KEY_PREVIOUS"], prom["PROM_READ_KEY"]),
                         (keys["prom"], OLD["prom"], keys["prom_read"]))
        self.assertEqual((kark["KARKINOS_API_KEY"], kark["KARKINOS_API_KEY_PREVIOUS"], kark["MRCRAB_MODE"]),
                         (keys["kark"], OLD["kark"], "paper"))
        self.assertIn("# the RH API", (self.vps / "rh_api.env").read_text(encoding="utf-8"))
        self.assertEqual((self.vps / "robinhood_read_api.py").read_bytes(), SERVER)
        # every unit restarted onto its new keys, the orders switch untouched
        for unit, name, value in (("pro-robinhood-api.service", "PRO_RH_API_KEY", keys["rh_full"]),
                                  ("prometheus-api.service", "PROM_READ_KEY", keys["prom_read"]),
                                  ("mrcrab-api.service", "KARKINOS_API_KEY", keys["kark"])):
            self.assertEqual(self.sim.running_env(unit)[name], value)
        self.assertEqual(self.sim.running_env("pro-robinhood-api.service")["PRO_RH_ORDERS_ENABLED"], "0")
        self.assertIn("the key kept for the phone is the one this machine had", done.stdout)
        self.assertIn("-R refused", done.stdout)
        # the tunnel account: forwarding only, and sshd's Match block says so
        conf = (self.vps / "ssh" / "sshd_config.d" / "60-pionir-tunnel.conf").read_text(encoding="utf-8")
        for line in ("Match User pionir-tunnel", "AllowTcpForwarding local", "PermitListen none",
                     "PermitOpen 127.0.0.1:8000 127.0.0.1:8001 127.0.0.1:8002", "PermitTTY no",
                     "ForceCommand /bin/false", "AllowStreamLocalForwarding no", "X11Forwarding no"):
            self.assertIn(line, conf)
        self.assertEqual(self.sim.state()["reloads"], 1)
        # the checks, as status codes
        for line in ("robinhood GET /status with the READ key", "robinhood POST /never with the READ key (must be refused)",
                     "robinhood GET /status with the phone's current key", "prometheus POST /pause with the READ key (must be refused)",
                     "prometheus GET /status with the phone's current key", "karkinos GET /status with the phone's current key"):
            self.assertRegex(done.stdout, r"ok\s+" + re.escape(line))
        self.assertNotIn("BAD", done.stdout)
        # this machine: READ keys only where the desktop reads; the full ones in the vault
        secrets, vault = self.state / "secrets", self.state / "vault"
        self.assertEqual((secrets / "proteus-read-key.txt").read_text(encoding="utf-8"), keys["rh_read"])
        self.assertEqual((secrets / "prometheus-read-key.txt").read_text(encoding="utf-8"), keys["prom_read"])
        self.assertEqual((secrets / "karkinos-read-key.txt").read_text(encoding="utf-8"), keys["kark"])
        self.assertFalse((secrets / "proteus-api-key.txt").exists())
        self.assertFalse((secrets / "prometheus-api-key.txt").exists())
        self.assertEqual((vault / "proteus-api-key.txt").read_text(encoding="utf-8"), keys["rh_full"])
        self.assertEqual((vault / "prometheus-api-key.txt").read_text(encoding="utf-8"), keys["prom"])
        # the phone: its secrets from a deleted vault file, the tailnet address, then its build
        gh = [c for c in self.calls() if c["tool"] == "gh"]
        argvs = [c["argv"] for c in gh]
        secret = next(c for c in gh if c["argv"][:3] == ["secret", "set", "-f"])
        self.assertEqual(remote.parse_env(secret["stdin"]),
                         {"PRO_RH_API_KEY": keys["rh_full"], "PROM_API_KEY": keys["prom"], "KARKINOS_API_KEY": keys["kark"]})
        self.assertFalse((vault / "phone-secrets.env").exists())
        var = ["variable", "set", "VPS_HOST", "--body", "100.64.0.7", "--repo", "PreShotCome/trading-bot-app"]
        run = ["workflow", "run", "build.yml", "--repo", "PreShotCome/trading-bot-app", "--ref", "main"]
        self.assertLess(argvs.index(var), argvs.index(run))
        # nothing leaked; the remote command line is the one fixed string; no port closed
        self.assert_no_leaks(done, list(keys.values()) + list(OLD.values()))
        self.assertEqual({c["argv"][-1] for c in self.calls() if c["tool"] == "ssh" and "-N" not in c["argv"]}, {REMOTE_CMD})
        self.assertFalse(any(s.startswith("fw_") for s in self.steps()))
        self.assertFalse(self.sim.state()["ufw_active"])

    def test_a_bot_without_a_grace_window_keeps_its_key(self) -> None:
        self.sim.set(prom_prev=False, prom_read=False, kark_prev=False)
        done = self.apply_ok()
        keys = self.keys()
        self.assertEqual(self.env("pro.env")["PROM_API_KEY"], OLD["prom"])
        self.assertNotIn("PROM_READ_KEY", self.env("pro.env"))
        self.assertEqual(self.env("mrcrab.env")["KARKINOS_API_KEY"], OLD["kark"])
        self.assertIn("PROM_API_KEY is NOT rotated", done.stdout)
        secret = next(c for c in self.calls() if c["tool"] == "gh" and c["argv"][:2] == ["secret", "set"])
        self.assertEqual(remote.parse_env(secret["stdin"]), {"PRO_RH_API_KEY": keys["rh_full"]})
        self.assertFalse((self.state / "secrets" / "prometheus-read-key.txt").exists())
        self.assertEqual((self.state / "secrets" / "karkinos-read-key.txt").read_text(encoding="utf-8"), OLD["kark"])

    def test_a_rerun_changes_nothing(self) -> None:
        self.apply_ok()
        keys = self.keys()
        rh_text = (self.vps / "rh_api.env").read_text(encoding="utf-8")
        restarts = {u: v.get("restarts", 0) for u, v in self.sim.state()["units"].items()}
        n_gh = len([c for c in self.calls() if c["tool"] == "gh"])
        again = self.apply_ok()
        self.assertEqual(self.keys(), keys)
        self.assertEqual((self.vps / "rh_api.env").read_text(encoding="utf-8"), rh_text)
        self.assertEqual({u: v.get("restarts", 0) for u, v in self.sim.state()["units"].items()}, restarts)
        self.assertIn("already running with its current keys (no restart)", again.stdout)
        self.assertEqual(len([c for c in self.calls() if c["tool"] == "gh"]), n_gh)
        self.assertEqual(self.env("rh_api.env")["PRO_RH_API_KEY_PREVIOUS"], OLD["rh"])   # still the phone's

    def test_an_armed_mismatch_stops_before_anything_changes(self) -> None:
        # running armed, configured off: any restart would disarm
        self.sim.set(units={})
        self.sim.boot_units(armed_running="1")
        done = self.run_script("-Apply")
        self.assertEqual(done.returncode, 1)
        self.assertIn("real-money orders are ARMED in the RUNNING Robinhood API but off", done.stdout)
        self.assertEqual(self.steps(), ["discover"])
        self.assertEqual(self.env("rh_api.env")["PRO_RH_API_KEY"], OLD["rh"])

    def test_a_robinhood_api_that_does_not_come_back_is_restored_and_nothing_ships(self) -> None:
        self.sim.unit("pro-robinhood-api.service", fail_restart=True)
        done = self.run_script("-Apply")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("restore", self.steps())
        self.assertIn("did not come back after its restart", done.stdout)
        self.assertEqual(self.env("rh_api.env")["PRO_RH_API_KEY"], OLD["rh"])           # env put back
        self.assertEqual((self.vps / "robinhood_read_api.py").read_text(encoding="utf-8"), "OLD = True\n")
        self.assertTrue(self.sim.state()["units"]["pro-robinhood-api.service"]["active"])
        self.assertTrue((self.state / "secrets" / "proteus-api-key.txt").exists())       # nothing shipped
        self.assertFalse((self.state / "secrets" / "proteus-read-key.txt").exists())
        self.assertFalse([c for c in self.calls() if c["tool"] == "gh"])

    def test_an_empty_live_key_is_never_rotated(self) -> None:
        (self.vps / "rh_api.env").write_text("PRO_RH_API_KEY=\nPRO_RH_ORDERS_ENABLED=0\n", encoding="utf-8")
        done = self.run_script("-Apply")
        self.assertEqual(done.returncode, 1)
        self.assertIn("PRO_RH_API_KEY is empty here", done.stdout)
        self.assertNotIn("restart", self.steps())

    def test_the_server_file_is_pinned(self) -> None:
        done = self.run_script("-Apply", sha="0" * 64)
        self.assertEqual(done.returncode, 1)
        self.assertIn("does not have the pinned sha256", done.stdout)
        self.assertEqual(self.calls(), [])

    def test_a_listener_the_tunnel_key_can_open_fails_the_run(self) -> None:
        self.extra_env = {"FAKE_ALLOW_R": "1"}
        done = self.run_script("-Apply")
        self.assertEqual(done.returncode, 1)
        self.assertIn("COULD open a listener on the VPS", done.stdout)
        self.assertFalse((self.state / "secrets" / "proteus-read-key.txt").exists())

    def test_a_check_that_answers_wrong_fails_the_run_and_nothing_ships(self) -> None:
        # a Robinhood API that lets the READ key write: never called done
        real = self.apis.judge
        self.apis.judge = lambda api, key, write: True if (api == "rh" and write) else real(api, key, write)
        done = self.run_script("-Apply")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertRegex(done.stdout, r"BAD\s+robinhood POST /never with the READ key \(must be refused\)\s+-> 400 \(want 401\)")
        self.assertFalse((self.state / "secrets" / "proteus-read-key.txt").exists())
        self.assertFalse([c for c in self.calls() if c["tool"] == "gh"])

    # ---- -FinishRotation ------------------------------------------------------------------------
    def test_finish_asks_first(self) -> None:
        self.apply_ok()
        done = self.run_script("-FinishRotation", "-Apply", stdin="no\n")
        self.assertEqual(done.returncode, 1)
        self.assertIn("not confirmed: nothing was changed", done.stdout)
        self.assertIn("PRO_RH_API_KEY_PREVIOUS", self.env("rh_api.env"))
        self.assertFalse(any(s.startswith("fw_") for s in self.steps()))

    def test_finish_kills_every_previous_key_then_closes_the_ports_safely(self) -> None:
        self.apply_ok()
        keys = self.keys()
        done = self.run_script("-FinishRotation", "-Apply", stdin="yes\n")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        for f, gone in (("rh_api.env", "PRO_RH_API_KEY_PREVIOUS"), ("pro.env", "PROM_API_KEY_PREVIOUS"),
                        ("mrcrab.env", "KARKINOS_API_KEY_PREVIOUS")):
            self.assertNotIn(gone, self.env(f))
            self.assertNotIn(gone + "_UNTIL", self.env(f))
        self.assertNotIn("PRO_RH_API_KEY_PREVIOUS", self.sim.running_env("pro-robinhood-api.service"))
        self.assertRegex(done.stdout, r"ok\s+robinhood GET /status with the OLD full key\s+-> 401")
        # the firewall: ssh proven between every step, the revert armed before anything closes
        tail = [s for s in self.steps() if s in ("fw_prepare", "ping", "fw_arm", "fw_confirm", "nginx_disable")]
        self.assertEqual(tail, ["fw_prepare", "ping", "fw_arm", "ping", "fw_confirm", "nginx_disable"])
        cmds = [" ".join(c) for c in self.sim.commands()]

        def first(needle):
            return next(i for i, c in enumerate(cmds) if needle in c)
        self.assertLess(first("ufw allow 22/tcp"), first("systemd-run --unit=pionir-ufw-revert"))
        self.assertLess(first("systemd-run --unit=pionir-ufw-revert"), first("ufw insert 2 deny"))
        self.assertLess(first("ufw default allow incoming"), first("ufw --force enable"))
        st = self.sim.state()
        self.assertTrue(st["ufw_active"])
        self.assertEqual(st["default_in"], "allow")
        self.assertEqual(st["rules"][:2], [vpsfake.RULE_TEXT[TAILNET_ALLOW], vpsfake.RULE_TEXT[("deny", "8000:8002/tcp")]])
        self.assertFalse(any(t["active"] for t in st["timers"].values()))            # the revert, cancelled
        self.assertFalse((self.vps / "sites-enabled" / "trading-bot").exists())       # nginx -> :8000 moved aside
        self.assertTrue((self.vps / "sites-enabled" / "other").exists())
        self.assertIn("confirmed by you", done.stdout)
        self.assert_no_leaks(done, list(keys.values()) + list(OLD.values()))
        again = self.run_script("-FinishRotation", "-Apply", stdin="yes\n")
        self.assertEqual(again.returncode, 0, again.stdout)
        self.assertEqual(len([s for s in self.steps() if s == "fw_arm"]), 1)

    def test_a_lost_ssh_after_the_firewall_change_leaves_the_revert_to_run(self) -> None:
        self.apply_ok()
        self.sim.set(ssh_dead_after="fw_arm")
        done = self.run_script("-FinishRotation", "-Apply", stdin="yes\n")
        self.assertEqual(done.returncode, 1)
        self.assertIn("a fresh ssh login FAILED after the firewall change", done.stdout)
        self.assertNotIn("fw_confirm", self.steps())
        timer = next(iter(self.sim.state()["timers"].values()))
        self.assertTrue(timer["active"])                                               # it will put ufw back

    def test_keep_public_ports(self) -> None:
        self.apply_ok()
        done = self.run_script("-FinishRotation", "-Apply", "-KeepPublicPorts", stdin="yes\n")
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertFalse(any(s.startswith("fw_") or s == "nginx_disable" for s in self.steps()))


class ScriptShape(unittest.TestCase):
    def setUp(self) -> None:
        self.text = SCRIPT.read_text(encoding="utf-8-sig")
        self.code = "\n".join(line for line in self.text.splitlines() if not line.lstrip().startswith("#"))

    def test_windows_powershell_5_1_safe(self) -> None:
        self.assertNotIn("&&", self.code)
        self.assertNotIn("||", self.code)
        self.assertNotIn("??", self.code)
        self.assertNotRegex(self.code, r"(?m)^\s*using\s+namespace")

    def test_installs_nothing_that_starts_by_itself_here(self) -> None:
        for word in ("Register-ScheduledTask", "schtasks", "New-Service", "sc.exe", "CurrentVersion\\Run", "Startup"):
            self.assertNotIn(word, self.code)

    def test_no_key_is_printed_or_passed(self) -> None:
        for line in self.code.splitlines():
            if "Write-Host" in line and "$state.keys" in line:
                self.fail(line)
        self.assertNotRegex(self.code, r'"secret", "set"[^\n]*--body')
        self.assertIn('"-N", ""', self.code)


class RemoteHalf(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="pionir-remote-"))
        self.r = load_remote()
        self.sim = vpsfake.install(self.r, self.dir)

    # ---- env files
    def test_previous_is_always_the_live_key_and_an_empty_one_is_refused(self) -> None:
        f = self.dir / "a.env"
        live = "live-value-00000000000000000000000000000"
        f.write_text(f"# keep\nK={live}\nK_PREV=stale-value-000000000000000000000000000\nB=2\n", encoding="utf-8")
        new = "N" * 43
        r = self.r.update_env_file(str(f), {"K": new}, {"K": "K_PREV"}, [], "20260928T000000Z", {"K_PREV_UNTIL": "1800000000"})
        env = self.r.parse_env(f.read_text(encoding="utf-8"))
        self.assertEqual((env["K"], env["K_PREV"], env["K_PREV_UNTIL"], env["B"]), (new, live, "1800000000", "2"))
        self.assertEqual(r["previous_fp"]["K_PREV"], self.r.fingerprint(live))
        # a re-run: K is already new, so the phone's key stays as previous
        self.r.update_env_file(str(f), {"K": new}, {"K": "K_PREV"}, [], "20260928T000001Z")
        self.assertEqual(self.r.parse_env(f.read_text(encoding="utf-8"))["K_PREV"], live)
        g = self.dir / "b.env"
        g.write_text("K=\n", encoding="utf-8")
        self.assertIn("is empty here", self.r.update_env_file(str(g), {"K": new}, {"K": "K_PREV"}, [], "20260928T000000Z")["error"])
        self.assertEqual(g.read_text(encoding="utf-8"), "K=\n")

    def test_env_backups_once_and_junk_refused(self) -> None:
        f = self.dir / "c.env"
        f.write_text("A=old-value-000000000000000000000000000000\n", encoding="utf-8")
        r = self.r.update_env_file(str(f), {"A": "n" * 43}, {}, [], "20260928T000000Z")
        backup = Path(r["backup"])
        self.assertIn("old-value", backup.read_text(encoding="utf-8"))
        self.r.update_env_file(str(f), {"A": "m" * 43}, {}, [], "20260928T000000Z")
        self.assertIn("old-value", backup.read_text(encoding="utf-8"))
        for bad in ("short", "has space " + "x" * 40, "x" * 40 + "\nB=injected"):
            self.assertIn("error", self.r.update_env_file(str(f), {"A": bad}, {}, [], "20260928T000000Z"))
        self.assertIn("error", self.r.update_env_file(str(f), {}, {}, [], "s", {"U": "1; rm -rf /"}))
        self.assertIn("error", self.r.step_env({"stamp": "x; rm -rf /", "files": []}))

    def test_files_are_written_owner_only_first(self) -> None:
        source = REMOTE.read_text(encoding="utf-8")
        body = source[source.index("def _write_private"):source.index("def update_env_file")]
        self.assertIn("os.O_EXCL", body)
        self.assertIn("0o600)", body)
        self.assertLess(body.index("os.write(fd"), body.index("os.chmod(path"))
        self.assertIn("os.umask(0o077)", source)

    # ---- sshd
    def test_the_sshd_match_block_is_checked_before_it_counts(self) -> None:
        opens = ["127.0.0.1:8000", "127.0.0.1:8001", "127.0.0.1:8002"]
        self.assertEqual(self.r.install_sshd_block("pionir-tunnel", opens), {"sshd_file": self.r.SSHD_DROPIN, "sshd_changed": True})
        self.assertEqual(self.sim.state()["reloads"], 1)
        self.assertEqual(self.r.install_sshd_block("pionir-tunnel", opens)["sshd_changed"], False)
        # sshd -t rejects it: put back, not reloaded
        Path(self.r.SSHD_DROPIN).unlink()
        self.sim.set(sshd_ok=False)
        self.assertIn("error", self.r.install_sshd_block("pionir-tunnel", opens))
        self.assertFalse(Path(self.r.SSHD_DROPIN).exists())
        # the effective config would still allow -R: put back, not reloaded
        self.sim.set(sshd_ok=True, sshd_effective="allowtcpforwarding yes\npermitlisten any\n")
        self.assertIn("permitlisten", self.r.install_sshd_block("pionir-tunnel", opens)["error"])
        self.assertFalse(Path(self.r.SSHD_DROPIN).exists())
        self.assertEqual(self.sim.state()["reloads"], 1)

    def test_without_an_include_the_block_goes_at_the_end_of_sshd_config(self) -> None:
        Path(self.r.SSHD_MAIN).write_text("PermitRootLogin prohibit-password\n", encoding="utf-8")
        opens = ["127.0.0.1:8000"]
        self.assertEqual(self.r.install_sshd_block("pionir-tunnel", opens)["sshd_file"], self.r.SSHD_MAIN)
        self.r.install_sshd_block("pionir-tunnel", opens)
        text = Path(self.r.SSHD_MAIN).read_text(encoding="utf-8")
        self.assertTrue(text.startswith("PermitRootLogin prohibit-password\n"))
        self.assertEqual(text.count("Match User pionir-tunnel"), 1)
        self.assertTrue(text.rstrip().endswith(self.r.END))

    def test_systemctl_parsing(self) -> None:
        self.assertEqual(self.r.env_files_from_show("/opt/prometheus/rh_api.env (ignore_errors=no) /x/y.env (ignore_errors=yes)"),
                         ["/opt/prometheus/rh_api.env", "/x/y.env"])
        exec_start = ("{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 /opt/prometheus/robinhood_read_api.py ; "
                      "ignore_errors=no ; start_time=[n/a] }")
        self.assertEqual(self.r.script_from_execstart(exec_start, "robinhood_read_api.py"), "/opt/prometheus/robinhood_read_api.py")
        self.assertEqual(self.r.script_from_execstart("argv[]=/usr/bin/python3 robinhood_read_api.py", "robinhood_read_api.py",
                                                      "/opt/prometheus"), "/opt/prometheus/robinhood_read_api.py")
        self.assertIsNone(self.r.script_from_execstart("argv[]=/usr/bin/python3 other.py", "robinhood_read_api.py"))

    def test_authorized_line(self) -> None:
        self.assertEqual(self.r.authorized_line(PUB, ["127.0.0.1:8000"]),
                         f'restrict,port-forwarding,permitopen="127.0.0.1:8000",command="/bin/false" {PUB}\n')
        for p, opens in ((PUB + "\nssh-rsa AAAA evil", ["127.0.0.1:8000"]), ("ssh-rsa AAAA x", ["127.0.0.1:8000"]),
                         (PUB, ["0.0.0.0:22"]), (PUB, [])):
            with self.assertRaises(ValueError):
                self.r.authorized_line(p, opens)

    # ---- restart
    def _units(self, armed="0"):
        for name, text in (("rh_api.env", "PRO_RH_API_KEY=a\nPRO_RH_ORDERS_ENABLED=0\n"), ("pro.env", "PROM_API_KEY=b\n"),
                           ("mrcrab.env", "KARKINOS_API_KEY=c\n")):
            (self.dir / name).write_text(text, encoding="utf-8")
        self.sim.boot_units(armed_running=armed)

    def test_restart_only_what_is_stale_and_never_start_a_stopped_unit(self) -> None:
        self._units()
        want = {"pro-robinhood-api.service": {"keys": ["PRO_RH_API_KEY"]}, "prometheus-api.service": {"keys": ["PROM_API_KEY"]}}
        out = {u["unit"]: u for u in self.r.step_restart({"units": want})["units"]}
        self.assertTrue(out["pro-robinhood-api.service"]["current"])
        (self.dir / "pro.env").write_text("PROM_API_KEY=new\n", encoding="utf-8")
        out = {u["unit"]: u for u in self.r.step_restart({"units": want})["units"]}
        self.assertTrue(out["prometheus-api.service"]["restarted"])
        self.assertEqual(out["prometheus-api.service"]["env_stale"], [])
        self.assertTrue(out["pro-robinhood-api.service"].get("current"))
        self.assertEqual(self.sim.running_env("prometheus-api.service")["PROM_API_KEY"], "new")
        self.sim.unit("mrcrab-api.service", active=False)
        (self.dir / "mrcrab.env").write_text("KARKINOS_API_KEY=new\n", encoding="utf-8")
        out = self.r.step_restart({"units": {"mrcrab-api.service": {"keys": ["KARKINOS_API_KEY"]}}})["units"][0]
        self.assertFalse(out["was_active"])
        self.assertFalse(self.sim.state()["units"]["mrcrab-api.service"]["active"])
        source = REMOTE.read_text(encoding="utf-8")
        body = source[source.index("def step_restart"):source.index("def step_restore")]
        self.assertIn('"try-restart"', body)
        self.assertNotRegex(body, r'\["systemctl", "(start|restart|enable|reload-or-restart)"')

    def test_restart_never_flips_the_orders_switch(self) -> None:
        self._units(armed="1")                 # running armed, configured off
        out = self.r.step_restart({"units": {"pro-robinhood-api.service": {"keys": ["PRO_RH_API_KEY"], "force": True}}})["units"][0]
        self.assertTrue(out["armed_mismatch"])
        self.assertFalse(out["restarted"])
        self.assertEqual(self.sim.state()["units"]["pro-robinhood-api.service"].get("restarts", 0), 0)

    def test_restore_puts_the_files_back_and_starts_the_unit(self) -> None:
        self._units()
        f = self.dir / "rh_api.env"
        self.r.update_env_file(str(f), {"PRO_RH_API_KEY": "n" * 43}, {"PRO_RH_API_KEY": "PRO_RH_API_KEY_PREVIOUS"}, [], "20260928T000000Z")
        self.sim.unit("pro-robinhood-api.service", active=False, failed=True)
        out = self.r.step_restore({"stamp": "20260928T000000Z", "unit": "pro-robinhood-api.service",
                                   "files": [str(f).replace(os.sep, "/")]})
        self.assertTrue(out["active"])
        self.assertEqual(self.r.parse_env(f.read_text(encoding="utf-8"))["PRO_RH_API_KEY"], "a")

    # ---- the firewall
    def test_prepare_only_permits_and_arm_schedules_the_revert_first(self) -> None:
        (Path(self.r.NET_DIR) / "tailscale0").write_text("up")
        out = self.r.step_fw_prepare({"stamp": "20260928T000000Z"})
        self.assertTrue(out["ok"])
        cmds = [" ".join(c) for c in self.sim.commands()]
        self.assertFalse(any("deny" in c or "enable" in c for c in cmds))
        self.assertEqual(cmds[-2:], ["ufw allow 22/tcp", "ufw insert 1 allow in on tailscale0 to any port 8000:8002 proto tcp"])
        self.sim.set(fail_timer=True)
        self.assertIn("nothing was changed", self.r.step_fw_arm({"stamp": "20260928T000000Z"})["error"])
        self.assertFalse(any("deny" in " ".join(c) or "enable" in " ".join(c) for c in self.sim.commands()))
        self.sim.set(fail_timer=False)
        arm = self.r.step_fw_arm({"stamp": "20260928T000000Z", "revert_after_s": 120})
        self.assertTrue(arm["ok"], arm)
        self.assertTrue(arm["order"]["ok"])
        revert = Path(self.r.BACKUP_ROOT) / ".pionir-ufw-20260928T000000Z" / "revert.sh"
        self.assertIn("ufw --force disable", revert.read_text(encoding="ascii"))     # it was off before
        self.assertTrue(self.r.step_fw_confirm({"revert_unit": arm["revert_unit"]})["cancelled"])
        self.assertIn("error", self.r.step_fw_confirm({"revert_unit": "sshd.service"}))

    def test_nothing_closes_without_the_tailnet_or_a_prepare(self) -> None:
        self.assertIn("tailscale0 is not up", self.r.step_fw_prepare({"stamp": "20260928T000000Z"})["error"])
        self.assertIn("fw_prepare has not run", self.r.step_fw_arm({"stamp": "20260928T000000Z"})["error"])
        with self.assertRaises(ValueError):
            self.r.step_fw_arm({"stamp": "x; rm -rf /"})

    def test_rule_order(self) -> None:
        good = ("[ 1] 8000:8002/tcp on tailscale0 ALLOW IN    Anywhere\n[ 2] 8000:8002/tcp DENY IN Anywhere\n"
                "[ 3] 22/tcp ALLOW IN Anywhere\n[ 4] 8000/tcp ALLOW IN Anywhere\n")
        self.assertTrue(self.r.rule_order(good)["ok"])
        bad = ("[ 1] 8000/tcp ALLOW IN Anywhere\n[ 2] 8000:8002/tcp on tailscale0 ALLOW IN Anywhere\n"
               "[ 3] 8000:8002/tcp DENY IN Anywhere\n")
        self.assertFalse(self.r.rule_order(bad)["ok"])
        self.assertFalse(self.r.rule_order("[ 1] 8000:8002/tcp DENY IN Anywhere\n")["ok"])

    def test_nginx_sites_to_the_apis_are_moved_aside_only_if_nginx_still_passes(self) -> None:
        site = Path(self.r.NGINX_SITES) / "tb"
        site.write_text("location / { proxy_pass http://127.0.0.1:8000; }\n", encoding="utf-8")
        self.sim.set(nginx_bad=True)
        self.assertIn("error", self.r.step_nginx_disable({"stamp": "20260928T000000Z"}))
        self.assertTrue(site.exists())
        self.sim.set(nginx_bad=False)
        out = self.r.step_nginx_disable({"stamp": "20260928T000000Z"})
        self.assertEqual(len(out["disabled"]), 1)
        self.assertFalse(site.exists())

    # ---- Tailscale
    def test_tailscale_install_uses_its_own_apt_repository(self) -> None:
        out = self.r.step_tailscale_install({})
        self.assertEqual((out["installed"], out["already"], out["repo"]), (True, False, "ubuntu noble"))
        self.assertEqual(Path(self.r.TS_LIST).read_text(encoding="ascii"), vpsfake.TS_LIST_OK)
        self.assertTrue(self.r.step_tailscale_install({})["already"])
        Path(self.r.OS_RELEASE).write_text("ID=arch\n", encoding="utf-8")
        self.sim.set(ts_installed=False)
        self.assertIn("error", self.r.step_tailscale_install({}))
        self.assertEqual(self.r.tailscale_repo("ID=debian\nVERSION_CODENAME=bookworm\n"), ("debian", "bookworm"))
        # a repository list that points anywhere else, or is unsigned, is refused and not written
        Path(self.r.OS_RELEASE).write_text("ID=ubuntu\nVERSION_CODENAME=noble\n", encoding="utf-8")
        Path(self.r.TS_LIST).unlink()
        for listing in (b"deb https://evil.example/ubuntu noble main\n",
                        b"deb [trusted=yes] https://pkgs.tailscale.com/stable/ubuntu noble main\n"):
            self.r._fetch = lambda url, listing=listing: b"key" if url.endswith(".gpg") else listing
            self.assertIn("not what was expected", self.r.step_tailscale_install({})["error"])
            self.assertFalse(Path(self.r.TS_LIST).exists())
        self.assertIsNone(self.r.tailscale_repo("ID=ubuntu\nVERSION_CODENAME=no ble\n"))

    def test_tailscale_up_starts_once_and_never_with_a_key(self) -> None:
        out = self.r.step_tailscale_up({"hostname": "proteus-vps"})
        self.assertEqual(out["auth_url"], "https://login.tailscale.com/a/pionirtest123")
        starts = [c for c in self.sim.commands() if c[:1] == ["systemd-run"]]
        self.assertEqual(len(starts), 1)
        self.assertIn("--ssh=false", starts[0])
        self.assertFalse(any("auth-key" in a or "authkey" in a for a in starts[0]))
        self.r.step_tailscale_up({"hostname": "proteus-vps"})       # waiting for the login: not started again
        self.assertEqual(len([c for c in self.sim.commands() if c[:1] == ["systemd-run"]]), 1)
        self.assertIn("error", self.r.step_tailscale_up({"hostname": "Bad Host"}))


if __name__ == "__main__":
    unittest.main()
