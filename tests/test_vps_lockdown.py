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
SERVER = (b"import os\nREAD = os.environ.get('PRO_RH_READ_KEY')\n"
          b"UNTIL = os.environ.get('PRO_RH_API_KEY_PREVIOUS_UNTIL')\n")
OLD_SERVER = b"OLD = True\n"
WEBAPP_BASE = b"# prometheus webapp, main\nKEY = 'PROM_API_KEY'\n"
WEBAPP_NEW = b"# prometheus webapp, read-key\nKEYS = ('PROM_API_KEY', 'PROM_READ_KEY', 'PROM_API_KEY_PREVIOUS_UNTIL')\n"
SUNDAY = "2026-09-27T16:00:00Z"            # outside US market hours
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
            "script": os.path.join(vps, "prometheus", "webapp.py").replace(os.sep, "/") if os.path.exists(os.path.join(vps, "prometheus", "webapp.py")) else None,
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
    if step == "deploy" and payload["name"] == "robinhood_read_api.py":   # a Windows temp path is no POSIX ExecStart path
        payload["path"] = os.path.join(vps, "robinhood_read_api.py").replace(os.sep, "/")
    out = r.STEPS[step](payload)
if step == "env" and sim.state().get("orders_flip_after_env"):
    _f = sim.state()["units"]["pro-robinhood-api.service"]["env_file"]      # somebody arms the orders switch
    _t = open(_f, encoding="utf-8").read()                                 # while the rotation runs
    open(_f, "w", encoding="utf-8").write(_t.replace("PRO_RH_ORDERS_ENABLED=0", "PRO_RH_ORDERS_ENABLED=1"))
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
        (self.vps / "robinhood_read_api.py").write_bytes(OLD_SERVER)
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
        self.now_args = ["-NowUtc", SUNDAY]
        # a pantheon repo with the reviewed webapp.py (the fake VPS runs main's, or none)
        self.pantheon = self.tmp / "pantheon"
        web = self.pantheon / "bots" / "prometheus" / "src" / "prometheus"
        web.mkdir(parents=True)
        pg = ["git", "-C", str(self.pantheon), "-c", "user.email=t@t", "-c", "user.name=t", "-c", "core.autocrlf=false"]
        subprocess.run(pg[:3] + ["init", "-q"], check=True)
        heads = []
        # history: Z (an older webapp.py), A (the last real deploy: main's webapp.py), B (an
        # undeployed commit that leaves webapp.py alone), C (the reviewed change)
        for subject, name, body in (("Z older webapp", "webapp.py", b"# older\n"),
                                    ("A last deploy", "webapp.py", WEBAPP_BASE),
                                    ("B undeployed thing", "notes.txt", b"b\n"),
                                    ("C the reviewed change", "webapp.py", WEBAPP_NEW)):
            (web / name).write_bytes(body)
            subprocess.run(pg + ["add", "."], check=True)
            subprocess.run(pg + ["commit", "-q", "-m", subject], check=True)
            heads.append(subprocess.run(pg[:3] + ["rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip())
        self.prom_older, self.prom_last, _, self.prom_commit = heads
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
             "-VerifyPortBase", str(self.apis.base), "-PollSeconds", "0", "-SkipTailnetProbe",
             "-ServerBaseSha256", hashlib.sha256(OLD_SERVER).hexdigest(), "-PantheonRepo", str(self.pantheon),
             "-PromCommit", self.prom_commit, "-PromSha256", hashlib.sha256(WEBAPP_NEW).hexdigest(),
             "-PromBaseSha256", hashlib.sha256(WEBAPP_BASE).hexdigest(), *self.now_args, *args],
            input=stdin, capture_output=True, text=True, timeout=400, env=env, check=False)

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
        self.assert_rolled_back(done)

    def test_a_check_that_answers_wrong_fails_the_run_and_nothing_ships(self) -> None:
        # a Robinhood API that lets the READ key write: never called done
        real = self.apis.judge
        self.apis.judge = lambda api, key, write: True if (api == "rh" and write) else real(api, key, write)
        done = self.run_script("-Apply")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertRegex(done.stdout, r"BAD\s+robinhood POST /never with the READ key \(must be refused\)\s+-> 400 \(want 401\)")
        self.assertFalse((self.state / "secrets" / "proteus-read-key.txt").exists())
        self.assertFalse([c for c in self.calls() if c["tool"] == "gh"])
        self.assert_rolled_back(done)

    def assert_rolled_back(self, done) -> None:
        """ANY [e] failure: every env file back to before the rotation, the units on it."""
        self.assertIn("the rotation is rolled back on the VPS", done.stdout)
        for f, name, old in (("rh_api.env", "PRO_RH_API_KEY", OLD["rh"]), ("pro.env", "PROM_API_KEY", OLD["prom"]),
                             ("mrcrab.env", "KARKINOS_API_KEY", OLD["kark"])):
            self.assertEqual(self.env(f)[name], old, f)
            self.assertNotIn(name + "_PREVIOUS", self.env(f))
        self.assertEqual(self.sim.running_env("pro-robinhood-api.service")["PRO_RH_API_KEY"], OLD["rh"])
        self.assertTrue(self.apis.judge("rh", OLD["rh"], True))            # the phone's key works

    # ---- D1: a run resumed days later never writes a dead deadline -------------------------------
    def test_a_run_resumed_days_later_computes_its_deadline_then(self) -> None:
        self.sim.set(ts_polls_to_login=10 ** 6)
        stopped = self.run_script("-Apply", "-LoginWaitMinutes", "0")
        self.assertEqual(stopped.returncode, 1)
        self.assertIn("no login within", stopped.stdout)
        self.assertEqual(self.env("rh_api.env")["PRO_RH_API_KEY"], OLD["rh"])     # stopped at [t]: nothing rotated
        state = json.loads((self.state / "vault" / "vps-lockdown.json").read_text(encoding="utf-8-sig"))
        self.assertNotIn("previous_until", state)                                 # no deadline decided at [a]
        self.sim.set(ts_polls_to_login=0, ts_state="NoState", ts_polls=0)
        self.apply_ok()
        until = int(self.env("rh_api.env")["PRO_RH_API_KEY_PREVIOUS_UNTIL"])
        self.assertTrue(time.time() + 6.9 * 86400 < until < time.time() + 7.1 * 86400)

    def test_a_deadline_less_than_a_day_ahead_is_recomputed_on_a_rerun(self) -> None:
        self.apply_ok()
        f = self.vps / "rh_api.env"
        env = self.env("rh_api.env")
        near = str(int(time.time()) + 3600)
        f.write_text(f.read_text(encoding="utf-8").replace(env["PRO_RH_API_KEY_PREVIOUS_UNTIL"], near), encoding="utf-8")
        again = self.apply_ok()
        until = int(self.env("rh_api.env")["PRO_RH_API_KEY_PREVIOUS_UNTIL"])
        self.assertTrue(time.time() + 6.9 * 86400 < until)
        self.assertEqual(self.sim.running_env("pro-robinhood-api.service")["PRO_RH_API_KEY_PREVIOUS_UNTIL"], str(until))
        self.assertRegex(again.stdout, r"ok\s+robinhood GET /status with the phone's current key")

    # ---- D3: one reviewed file onto its reviewed base; restarts only at a quiet moment ---------------
    def test_the_prometheus_auth_change_is_deployed_as_one_file_onto_mains(self) -> None:
        (self.vps / "prometheus").mkdir()
        (self.vps / "prometheus" / "webapp.py").write_bytes(WEBAPP_BASE)
        self.sim.set(prom_prev=False, prom_read=False)            # the VPS runs main's webapp.py
        self.apply_ok()
        self.assertEqual((self.vps / "prometheus" / "webapp.py").read_bytes(), WEBAPP_NEW)
        self.assertEqual(self.env("pro.env")["PROM_API_KEY"], self.keys()["prom"])        # now rotated, with grace
        self.assertEqual(self.sim.state()["units"]["prometheus-api.service"].get("restarts"), 1)

    def test_a_drifted_file_on_the_vps_is_never_replaced(self) -> None:
        (self.vps / "prometheus").mkdir()
        (self.vps / "prometheus" / "webapp.py").write_bytes(b"# someone's undeployed-elsewhere edit\n")
        self.sim.set(prom_prev=False, prom_read=False)
        done = self.apply_ok()
        self.assertEqual((self.vps / "prometheus" / "webapp.py").read_bytes(), b"# someone's undeployed-elsewhere edit\n")
        self.assertIn("is not main's version", done.stdout)
        self.assertEqual(self.env("pro.env")["PROM_API_KEY"], OLD["prom"])
        (self.vps / "robinhood_read_api.py").write_bytes(b"DRIFTED = 1\n")
        fresh = self.run_script("-Apply", "-NewRotation")
        self.assertEqual(fresh.returncode, 1)
        self.assertIn("is not the reviewed base version", fresh.stdout)
        self.assertEqual((self.vps / "robinhood_read_api.py").read_bytes(), b"DRIFTED = 1\n")

    def test_no_restart_while_the_market_is_open_or_a_job_runs(self) -> None:
        self.now_args = ["-NowUtc", "2026-09-29T15:00:00Z"]        # Tuesday 11:00 in New York
        done = self.run_script("-Apply")
        self.assertEqual(done.returncode, 1)
        self.assertIn("the US market is open", done.stdout)
        self.assertIn("next safe 2026-09-29 20:00:00Z", done.stdout)
        self.assertNotIn("deploy", self.steps())
        self.assertEqual(self.env("rh_api.env")["PRO_RH_API_KEY"], OLD["rh"])
        self.now_args = ["-NowUtc", SUNDAY]
        for setup, words in ((lambda: self.sim.unit("prometheus-execute.service", active=True), "prometheus-execute.service is running now"),
                             (lambda: self.sim.set(sched={"prometheus-entry.timer": int(time.time()) + 300}), "prometheus-entry.timer fires in"),
                             (lambda: self.sim.set(jobs_running=2), "2 background job(s) in flight")):
            self.sim.set(units={}, sched={}, jobs_running=0)
            self.sim.boot_units()
            setup()
            done = self.run_script("-Apply")
            self.assertEqual(done.returncode, 1, words)
            self.assertIn(words, done.stdout)
            self.assertEqual(self.env("rh_api.env")["PRO_RH_API_KEY"], OLD["rh"])
        forced = self.run_script("-Apply", "-Force")
        self.assertEqual(forced.returncode, 0, forced.stdout)
        self.assertIn("-Force: restarting anyway", forced.stdout)

    # ---- review 3, finding 2: the guard fails CLOSED -----------------------------------------------------
    def test_a_guard_that_cannot_tell_blocks_the_restart(self) -> None:
        far = int(time.time()) + 6 * 3600
        for setup, words in (
                (lambda: self.sim.set(show_fail=True), "its next run could not be read"),
                (lambda: self.sim.set(show_fail=True), "busctl: exit 1 'Failed to get property: Access denied'"),
                (lambda: self.sim.set(show_junk=True), "show: exit 0 'NextElapseUSecRealtime=Tue 2026-09-29 13:15:00 CEST"),
                (lambda: self.sim.set(health_down=True), "did not say how many jobs are in flight"),
                (lambda: self.sim.set(health_junk=True), "did not say how many jobs are in flight")):
            with self.subTest(words=words):
                self.sim.set(units={}, sched={"prometheus-scan.timer": far}, jobs_running=0, systemd=255,
                             show_fail=False, show_junk=False, busctl_fail=False, health_down=False, health_junk=False)
                self.sim.boot_units()
                setup()
                done = self.run_script("-Apply")
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn("cannot tell if it is quiet", done.stdout)
                self.assertIn(words, done.stdout)
                self.assertEqual(self.env("rh_api.env")["PRO_RH_API_KEY"], OLD["rh"])    # nothing changed
                self.assertNotIn("env", self.steps())
        # -Force is the owner's explicit override
        forced = self.run_script("-Apply", "-Force")
        self.assertEqual(forced.returncode, 0, forced.stdout)

    def test_a_stopped_prometheus_api_has_no_jobs_to_lose_and_no_timers_need_no_clock(self) -> None:
        # no active timer: an old systemd is irrelevant; the Prometheus API not running: no health check to fail
        self.sim.set(systemd=232, health_down=True)
        self.sim.unit("prometheus-api.service", active=False)
        done = self.run_script("-Apply")
        self.assertNotIn("cannot tell if it is quiet", done.stdout)
        self.assertIn("a quiet moment to restart", done.stdout)

    # ---- review 3, finding 4: what the VPS's Prometheus may be missing -------------------------------
    def test_the_undeployed_pantheon_commits_are_listed(self) -> None:
        dry = self.run_script()
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        for h in ("2a6fe2a", "0ad778e", "46aa96d", "1e52cd3"):                  # embedded (this repo has none of them)
            self.assertIn(h, dry.stdout)
        self.assertIn("the list embedded in this script", dry.stdout)
        # computed from git when the repo can say: last deploy A .. the reviewed base (C's parent B)
        git = self.run_script("-PromLastDeployed", self.prom_last)
        self.assertEqual(git.returncode, 0, git.stdout + git.stderr)
        self.assertIn("B undeployed thing", git.stdout)
        self.assertIn("from git", git.stdout)
        self.assertNotIn("1e52cd3", git.stdout)
        self.assertNotIn("A last deploy", git.stdout)
        self.assertIn("which none of these change", git.stdout)
        # ... and it says when one of them changes webapp.py itself
        older = self.run_script("-PromLastDeployed", self.prom_older)
        self.assertIn("A last deploy", older.stdout)
        self.assertIn("some of these change webapp.py itself", older.stdout)
        # the drift warning points at the same list
        (self.vps / "prometheus").mkdir(exist_ok=True)
        (self.vps / "prometheus" / "webapp.py").write_bytes(b"# not main's\n")
        self.sim.set(prom_prev=False, prom_read=False)
        drift = self.run_script("-Apply", "-PromLastDeployed", self.prom_last)
        self.assertIn("is not main's version", drift.stdout)
        self.assertGreaterEqual(drift.stdout.count("B undeployed thing"), 2)

    # ---- review 3, findings 1 and 5: ONE rollback rule for all three APIs -------------------------------
    def test_a_late_restart_failure_rolls_all_three_back_together(self) -> None:
        for failing in ("pro-robinhood-api.service", "prometheus-api.service", "mrcrab-api.service"):
            with self.subTest(failing=failing):
                self.sim.unit(failing, fail_restart=True)
                done = self.run_script("-Apply")
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn("did not come back after its restart", done.stdout)
                self.assert_rolled_back(done)
                self.assertIn("for all three APIs", done.stdout)
                self.assertEqual(self.sim.running_env("prometheus-api.service")["PROM_API_KEY"], OLD["prom"])
                self.assertEqual(self.sim.running_env("mrcrab-api.service")["KARKINOS_API_KEY"], OLD["kark"])
                for unit in ("pro-robinhood-api.service", "prometheus-api.service", "mrcrab-api.service"):
                    self.assertTrue(self.sim.state()["units"][unit]["active"], unit)
                self.assertFalse((self.state / "secrets" / "proteus-read-key.txt").exists())   # nothing shipped
                self.sim.unit(failing, fail_restart=False)

    def restarted(self, unit: str) -> bool:
        return any(len(c) >= 3 and c[0] == "systemctl" and c[1] in ("restart", "try-restart") and c[2] == unit
                   for c in self.sim.commands())

    def test_an_env_file_error_after_d_rolls_every_file_back_and_restarts_nothing(self) -> None:
        # the Robinhood file is refused (its live key is empty) but Prometheus's and Karkinos's were rotated
        (self.vps / "rh_api.env").write_text("PRO_RH_API_KEY=\nPRO_RH_ORDERS_ENABLED=0\n", encoding="utf-8")
        done = self.run_script("-Apply")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("PRO_RH_API_KEY is empty here", done.stdout)
        self.assertIn("an env file was not updated as intended", done.stdout)
        self.assertIn("rolled back on the VPS for all three APIs", done.stdout)
        self.assertIn("restore", self.steps())
        self.assertNotIn("restart", self.steps())
        for f, name, old in (("pro.env", "PROM_API_KEY", OLD["prom"]), ("mrcrab.env", "KARKINOS_API_KEY", OLD["kark"])):
            self.assertEqual(self.env(f)[name], old, f)
            self.assertNotIn(name + "_PREVIOUS", self.env(f), f)
            self.assertNotIn(name + "_PREVIOUS_UNTIL", self.env(f), f)
        for unit in ("pro-robinhood-api.service", "prometheus-api.service", "mrcrab-api.service"):
            self.assertFalse(self.restarted(unit), unit)                 # the rollback restarts nothing here either
        self.assertFalse((self.state / "secrets" / "proteus-read-key.txt").exists())
        self.assertIn("Nothing was written here", done.stdout)

    def test_an_env_file_error_after_the_keys_shipped_fails_forward(self) -> None:
        self.apply_ok()
        keys = self.keys()
        (self.vps / "rh_api.env").write_text("PRO_RH_API_KEY=\nPRO_RH_ORDERS_ENABLED=0\n", encoding="utf-8")
        before = self.steps().count("restore")
        done = self.run_script("-Apply")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("failed forward", done.stdout)
        self.assertNotIn("rolled back on the VPS", done.stdout)
        self.assertEqual(self.steps().count("restore"), before)
        self.assertEqual(self.env("pro.env")["PROM_API_KEY"], keys["prom"])          # the new keys stay
        self.assertEqual(self.env("mrcrab.env")["KARKINOS_API_KEY"], keys["kark"])
        self.assertEqual(self.env("pro.env")["PROM_API_KEY_PREVIOUS"], OLD["prom"])  # with the previous ones

    def test_an_armed_mismatch_after_d_rolls_back_without_touching_the_orders_switch(self) -> None:
        rh = "pro-robinhood-api.service"
        pid = self.sim.state()["units"][rh]["pid"]
        self.sim.set(orders_flip_after_env=True)        # Ian arms the orders after the preflight, before the restart
        done = self.run_script("-Apply")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("real-money orders differ", done.stdout)
        self.assertIn("rolled back on the VPS for all three APIs", done.stdout)
        # Ian is told, at the restore step, that a rollback never disarms orders the live file had armed
        self.assertIn("a rollback NEVER disarms real-money orders", done.stdout)
        for f, name, old in (("rh_api.env", "PRO_RH_API_KEY", OLD["rh"]), ("pro.env", "PROM_API_KEY", OLD["prom"]),
                             ("mrcrab.env", "KARKINOS_API_KEY", OLD["kark"])):
            self.assertEqual(self.env(f)[name], old, f)
            self.assertNotIn(name + "_PREVIOUS", self.env(f), f)
        # Ian's decision survives the rollback ...
        self.assertEqual(self.env("rh_api.env")["PRO_RH_ORDERS_ENABLED"], "1")
        # ... and the Robinhood API was never restarted (a restart would have ARMED real-money orders)
        self.assertFalse(self.restarted(rh))
        self.assertEqual(self.sim.state()["units"][rh]["pid"], pid)
        self.assertEqual(self.sim.running_env(rh)["PRO_RH_ORDERS_ENABLED"], "0")
        self.assertEqual(self.sim.running_env("prometheus-api.service")["PROM_API_KEY"], OLD["prom"])
        self.assertTrue(self.sim.state()["units"]["prometheus-api.service"]["active"])
        self.assertFalse((self.state / "secrets" / "proteus-read-key.txt").exists())

    def test_an_armed_mismatch_after_the_keys_shipped_fails_forward(self) -> None:
        self.apply_ok()
        keys = self.keys()
        rh = "pro-robinhood-api.service"
        pid = self.sim.state()["units"][rh]["pid"]
        before = self.steps().count("restore")
        self.sim.set(orders_flip_after_env=True)
        done = self.run_script("-Apply")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("real-money orders differ", done.stdout)
        self.assertIn("failed forward", done.stdout)
        self.assertEqual(self.steps().count("restore"), before)
        self.assertEqual(self.env("rh_api.env")["PRO_RH_API_KEY"], keys["rh_full"])
        self.assertEqual(self.env("rh_api.env")["PRO_RH_API_KEY_PREVIOUS"], OLD["rh"])
        self.assertEqual(self.sim.state()["units"][rh]["pid"], pid)

    def test_once_the_keys_are_shipped_a_failure_never_rolls_them_back(self) -> None:
        self.apply_ok()
        keys = self.keys()
        near = str(int(time.time()) + 3600)
        f = self.vps / "rh_api.env"
        f.write_text(f.read_text(encoding="utf-8").replace(self.env("rh_api.env")["PRO_RH_API_KEY_PREVIOUS_UNTIL"], near), encoding="utf-8")
        self.sim.unit("pro-robinhood-api.service", fail_restart=True)       # the refresh restart fails
        before = self.steps().count("restore")
        done = self.run_script("-Apply")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("failed forward", done.stdout)
        self.assertNotIn("rolled back on the VPS", done.stdout)
        self.assertEqual(self.steps().count("restore"), before)             # no restore step at all
        rh = self.env("rh_api.env")
        self.assertEqual(rh["PRO_RH_API_KEY"], keys["rh_full"])              # the phone's new key is still THE key
        self.assertEqual(rh["PRO_RH_API_KEY_PREVIOUS"], OLD["rh"])           # and the old one still valid
        self.assertTrue(int(rh["PRO_RH_API_KEY_PREVIOUS_UNTIL"]) > time.time() + 6.9 * 86400)   # the deadline refreshed
        self.assertEqual(self.env("pro.env")["PROM_API_KEY"], keys["prom"])
        self.assertEqual(self.env("mrcrab.env")["KARKINOS_API_KEY"], keys["kark"])
        self.assertTrue((self.state / "secrets" / "proteus-read-key.txt").exists())

    def test_either_mark_alone_is_enough_to_forbid_a_rollback(self) -> None:
        """Only the phone's mark, only this machine's mark: no rollback. Neither: the rollback."""
        self.apply_ok()
        state_file = self.state / "vault" / "vps-lockdown.json"
        full = json.loads(state_file.read_text(encoding="utf-8-sig"))
        self.assertTrue(full["done"]["phone"] and full["done"]["local"])
        for keep, forward in ((("phone",), True), (("local",), True), ((), False)):
            with self.subTest(marks=keep):
                doc = json.loads(state_file.read_text(encoding="utf-8-sig"))
                for mark in ("phone", "local"):
                    doc["done"].pop(mark, None)
                    if mark in keep:
                        doc["done"][mark] = True
                state_file.write_text(json.dumps(doc), encoding="utf-8")
                env = self.env("rh_api.env")
                near = str(int(time.time()) + 3600)
                f = self.vps / "rh_api.env"
                f.write_text(f.read_text(encoding="utf-8").replace(env["PRO_RH_API_KEY_PREVIOUS_UNTIL"], near), encoding="utf-8")
                self.sim.unit("pro-robinhood-api.service", fail_restart=True)
                done = self.run_script("-Apply")
                self.assertEqual(done.returncode, 1, done.stdout)
                if forward:
                    self.assertIn("failed forward", done.stdout, done.stdout[-1800:])
                    self.assertNotIn("rolled back on the VPS", done.stdout)
                    self.assertEqual(self.env("rh_api.env")["PRO_RH_API_KEY"], full["keys"]["rh_full"])
                else:
                    self.assertIn("rolled back on the VPS for all three APIs", done.stdout)
                    self.assertEqual(self.env("rh_api.env")["PRO_RH_API_KEY"], OLD["rh"])
                self.sim.unit("pro-robinhood-api.service", fail_restart=False, active=True, failed=False)
                self.sim.boot_units()                                  # a process again (the failed one had none)

    def test_a_failed_verification_after_the_keys_shipped_fails_forward_too(self) -> None:
        self.apply_ok()
        keys = self.keys()
        real = self.apis.judge
        self.apis.judge = lambda api, key, write: True if (api == "rh" and write) else real(api, key, write)
        done = self.run_script("-Apply")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("failed forward", done.stdout)
        self.assertNotIn("restore", self.steps())
        self.assertEqual(self.env("rh_api.env")["PRO_RH_API_KEY"], keys["rh_full"])
        self.assertEqual(self.env("rh_api.env")["PRO_RH_API_KEY_PREVIOUS"], OLD["rh"])

    # ---- D4: root's sshd config must not move ---------------------------------------------------------
    def test_a_match_block_that_would_change_roots_sshd_config_is_put_back(self) -> None:
        self.sim.set(root_drift=True)
        done = self.run_script("-Apply")
        self.assertEqual(done.returncode, 1)
        self.assertIn("root's own effective sshd config would change", done.stdout)
        self.assertFalse((self.vps / "ssh" / "sshd_config.d" / "60-pionir-tunnel.conf").exists())
        self.assertEqual(self.sim.state()["reloads"], 0)

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

    def test_no_port_closes_while_a_tailnet_key_can_expire(self) -> None:
        self.apply_ok()
        for field, words in (("ts_key_expiry", "the VPS (proteus-vps): its key expires"), ("phone_expiry", "your android (pixel): its key expires")):
            self.sim.set(ts_key_expiry=None, phone_expiry=None)
            self.sim.set(**{field: "2027-03-01T00:00:00Z"})
            done = self.run_script("-FinishRotation", "-Apply", stdin="yes\n")
            self.assertEqual(done.returncode, 1, done.stdout)
            self.assertIn(words, done.stdout)
            self.assertIn("Disable key expiry", done.stdout)
            self.assertIn("independent STOP route", done.stdout)
            self.assertFalse(any(s.startswith("fw_") for s in self.steps()))
            self.assertFalse(self.sim.state()["ufw_active"])
        self.sim.set(ts_key_expiry=None, phone_expiry=None)
        ok = self.run_script("-FinishRotation", "-Apply", stdin="yes\n")
        self.assertEqual(ok.returncode, 0, ok.stdout)
        self.assertIn("key expiry is off for the VPS and your phone", ok.stdout)

    def test_finish_waits_for_a_quiet_moment_too(self) -> None:
        self.apply_ok()
        self.now_args = ["-NowUtc", "2026-09-29T15:00:00Z"]        # Tuesday 11:00 in New York
        done = self.run_script("-FinishRotation", "-Apply", stdin="yes\n")
        self.assertEqual(done.returncode, 1)
        self.assertIn("the US market is open", done.stdout)
        self.assertIn("PRO_RH_API_KEY_PREVIOUS", self.env("rh_api.env"))           # nothing restarted or removed
        self.assertFalse(any(s.startswith("fw_") for s in self.steps()))

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

    def test_the_deadline_is_set_at_rotation_and_never_left_near(self) -> None:
        f = self.dir / "d.env"
        f.write_text("K=live-value-00000000000000000000000000000\n", encoding="utf-8")
        self.r.NOW = lambda: 1_800_000_000
        self.r.update_env_file(str(f), {"K": "N" * 43}, {"K": "K_PREV"}, [], "20260928T000000Z", previous_days=7)
        self.assertEqual(self.r.parse_env(f.read_text(encoding="utf-8"))["K_PREV_UNTIL"], str(1_800_000_000 + 7 * 86400))
        # a re-run six days later: 1 day left - kept
        self.r.NOW = lambda: 1_800_000_000 + 5 * 86400
        self.r.update_env_file(str(f), {"K": "N" * 43}, {"K": "K_PREV"}, [], "20260928T000000Z", previous_days=7)
        self.assertEqual(self.r.parse_env(f.read_text(encoding="utf-8"))["K_PREV_UNTIL"], str(1_800_000_000 + 7 * 86400))
        # eight days later: past - recomputed from now, never written dead
        self.r.NOW = lambda: 1_800_000_000 + 8 * 86400
        self.r.update_env_file(str(f), {"K": "N" * 43}, {"K": "K_PREV"}, [], "20260928T000000Z", previous_days=7)
        self.assertEqual(self.r.parse_env(f.read_text(encoding="utf-8"))["K_PREV_UNTIL"], str(1_800_000_000 + 15 * 86400))
        self.assertIn("error", self.r.update_env_file(str(f), {"K": "N" * 43}, {"K": "K_PREV"}, [], "20260928T000000Z", previous_days=30))

    def test_deploy_only_onto_the_reviewed_base(self) -> None:
        import base64
        target = self.dir / "webapp.py"
        new = b"KEY = 'PROM_READ_KEY'\n"

        def pay(**kw):
            p = {"content_b64": base64.b64encode(new).decode(), "sha256": hashlib.sha256(new).hexdigest(), "name": "webapp.py",
                 "path": str(target).replace(os.sep, "/"), "stamp": "20260928T000000Z", "must_contain": ["PROM_READ_KEY"],
                 "base_sha256": [hashlib.sha256(b"BASE\n").hexdigest()]}
            p.update(kw)
            return p
        target.write_bytes(b"DRIFT\n")
        self.assertTrue(self.r.step_deploy(pay())["drift"])
        self.assertEqual(target.read_bytes(), b"DRIFT\n")
        self.assertTrue(self.r.step_deploy(pay(allow_drift=True))["deployed"])
        target.write_bytes(b"BASE\n")
        self.assertTrue(self.r.step_deploy(pay())["deployed"])
        self.assertIn("error", self.r.step_deploy(pay(path="/etc/passwd")))

    def test_the_guard(self) -> None:
        self.r.NOW = lambda: 1_800_000_000
        self.sim.unit("prometheus-execute.service", active=True)
        self.sim.set(sched={"mrcrab-t2.timer": 1_800_000_000 + 600, "prometheus-scan.timer": 1_800_000_000 + 3600}, jobs_running=1)
        g = self.r.step_guard({"services": ["prometheus-execute.service", "prometheus-scan.service"],
                               "timers": ["mrcrab-t2.timer", "prometheus-scan.timer", "prometheus-entry.timer"], "jobs_port": 8001})
        self.assertEqual((g["busy"], [d["timer"] for d in g["due"]], g["jobs_running"], g["safe"]),
                         (["prometheus-execute.service"], ["mrcrab-t2.timer"], 1, False))
        self.sim.set(units={}, sched={}, jobs_running=0)
        self.assertTrue(self.r.step_guard({"services": ["prometheus-execute.service"], "timers": ["mrcrab-t2.timer"], "jobs_port": 8001})["safe"])

    def test_the_guard_fails_closed(self) -> None:
        """Not knowing is not idle: an unreadable timer or an unanswered health check is unsafe."""
        self.r.NOW = lambda: 1_800_000_000
        far = 1_800_000_000 + 6 * 3600                 # the 13:15 Prometheus scan: outside the NY window
        ask = {"services": [], "timers": ["prometheus-scan.timer"], "jobs_port": 8001, "jobs_unit": "prometheus-api.service"}
        self.sim.set(sched={"prometheus-scan.timer": far}, jobs_running=0)
        self.sim.boot_units()
        ok = self.r.step_guard(ask)
        self.assertEqual((ok["safe"], ok["unknown"], ok["jobs_running"]), (True, [], 0))
        for name, flags in (("show errors", {"show_fail": True}),
                            ("unparseable date", {"show_junk": True}), ("health down", {"health_down": True}),
                            ("health without jobs_running", {"health_junk": True})):
            self.sim.set(systemd=255, show_fail=False, show_junk=False, health_down=False, health_junk=False)
            self.sim.set(**flags)
            g = self.r.step_guard(ask)
            self.assertFalse(g["safe"], name)
            self.assertEqual(len(g["unknown"]), 1, (name, g["unknown"]))
        # a Prometheus API that is not running has no jobs to lose: not unknown
        self.sim.set(systemd=255, show_fail=False, show_junk=False, health_down=True)
        self.sim.unit("prometheus-api.service", active=False)
        g = self.r.step_guard(ask)
        self.assertEqual((g["safe"], g["unknown"], g["jobs_running"]), (True, [], 0))
        # no active timer: nothing to read, however old systemd is
        self.sim.set(sched={}, systemd=232, show_fail=True)
        self.assertTrue(self.r.step_guard(ask)["safe"])
        # a systemd whose bus is unreachable but whose `show` answers unix stamps (or a UTC date) is read
        self.sim.set(sched={"prometheus-scan.timer": far}, systemd=249, show_fail=False, busctl_fail=True)
        self.assertTrue(self.r.step_guard(ask)["safe"])

    # ---- the timer clock: read on any systemd, fail closed, due within 15 minutes ------------------------
    T0 = 1_800_000_000                                  # 2027-01-15 08:00:00 UTC (a Friday)

    @staticmethod
    def utc_date(epoch: int, zone: str = "UTC") -> str:
        return time.strftime("%a %Y-%m-%d %H:%M:%S", time.gmtime(epoch)) + " " + zone

    def guard_runner(self, answers: dict, calls: list | None = None):
        """A `_run` for the guard: `answers` maps a marker in the command to (code, out). Anything
        else is 127. The timer is active; systemctl --version says 249."""
        def run(argv, timeout=30, env=None, merge_stderr=False):
            line = " ".join(argv)
            if calls is not None:
                calls.append(line)
            if argv[:2] == ["systemctl", "is-active"]:
                return (0, "active\n") if argv[2].endswith(".timer") else (3, "inactive\n")
            if argv == ["systemctl", "--version"]:
                return 0, "systemd 249 (249.11-0ubuntu3.12)\n+PAM\n"
            for marker, ans in answers.items():
                if marker in line:
                    return ans(line) if callable(ans) else ans
            return 127, ""
        return run

    def guard_one(self, answers: dict, now: int | None = None, mono_now: float = 1000.0, timer="prometheus-scan.timer"):
        r = load_remote()
        r._run = self.guard_runner(answers)
        r.NOW = lambda: self.T0 if now is None else now
        r.MONO_CLOCKS = (lambda: mono_now,)
        return r.step_guard({"services": [], "timers": [timer]})

    def busctl_answers(self, realtime_usec: int, mono_usec: int = 0) -> dict:
        return {"busctl": lambda line: (0, f"t {realtime_usec if line.endswith('Realtime') else mono_usec}\n")}

    def show_answer(self, realtime: str, mono: str = "0"):
        return (0, f"NextElapseUSecRealtime={realtime}\nNextElapseUSecMonotonic={mono}\n")

    def test_the_guard_reads_a_timer_on_every_systemd(self) -> None:
        soon, later = self.T0 + 10 * 60, self.T0 + 3 * 3600
        fail = (1, "")
        cases = [
            # (name, answers, expected due in_s or None, expected unknown count)
            ("busctl, raw usec, due", self.busctl_answers(soon * 1_000_000), 600, 0),
            ("busctl, raw usec, later", self.busctl_answers(later * 1_000_000), None, 0),
            ("busctl, unset", self.busctl_answers(0), None, 0),
            ("systemd 245: show prints a UTC date, due", {"busctl": fail, "systemctl show": self.show_answer(self.utc_date(soon))}, 600, 0),
            ("systemd 245: show prints a UTC date, later", {"busctl": fail, "systemctl show": self.show_answer(self.utc_date(later))}, None, 0),
            ("systemd 249: show prints unix stamps, due", {"busctl": fail, "systemctl show": self.show_answer(f"@{soon}")}, 600, 0),
            ("systemd 249: only --timestamp=utc is honoured", {"busctl": fail, "systemctl show": lambda line: (
                self.show_answer(self.utc_date(soon)) if "--timestamp=utc" in line else self.show_answer("Fri 09:15 CET"))}, 600, 0),
            ("systemd 252: raw microseconds from show", {"busctl": fail, "systemctl show": self.show_answer(str(soon * 1_000_000))}, 600, 0),
            ("show: fractional UTC date (us style)", {"busctl": fail, "systemctl show": self.show_answer(self.utc_date(soon).replace(" UTC", ".250000 UTC"))}, 600, 0),
            ("show: n/a", {"busctl": fail, "systemctl show": self.show_answer("n/a")}, None, 0),
            ("show: empty", {"busctl": fail, "systemctl show": self.show_answer("")}, None, 0),
            # fails closed
            ("everything errors", {"busctl": fail, "systemctl show": fail}, None, 1),
            ("a zone abbreviation is never guessed", {"busctl": fail, "systemctl show": self.show_answer(self.utc_date(soon, "CEST"))}, None, 1),
            ("a wrong weekday is not a date", {"busctl": fail, "systemctl show": self.show_answer(self.utc_date(soon).replace("Fri", "Mon"))}, None, 1),
            ("junk", {"busctl": fail, "systemctl show": self.show_answer("soonish")}, None, 1),
            ("a property missing", {"busctl": fail, "systemctl show": (0, f"NextElapseUSecRealtime=@{soon}\n")}, None, 1),
            ("show exits non-zero with a good-looking answer", {"busctl": fail, "systemctl show": (1, f"NextElapseUSecRealtime=@{soon}\nNextElapseUSecMonotonic=0\n")}, None, 1),
            ("busctl answers a string, not a uint64", {"busctl": (0, 's "x"\n'), "systemctl show": fail}, None, 1),
            ("a stamp far outside any plausible date", {"busctl": fail, "systemctl show": self.show_answer("@5")}, None, 1),
        ]
        for name, answers, in_s, unknown in cases:
            with self.subTest(name):
                g = self.guard_one(answers)
                self.assertEqual(len(g["unknown"]), unknown, g)
                self.assertEqual([d["in_s"] for d in g["due"]], [] if in_s is None else [in_s], g)
                self.assertEqual(g["safe"], in_s is None and not unknown, g)

    def test_the_due_window_is_fifteen_minutes_with_a_grace_for_an_elapsed_timer(self) -> None:
        def at(delta: int):
            return self.guard_one(self.busctl_answers((self.T0 + delta) * 1_000_000))
        self.assertEqual([d["in_s"] for d in at(14 * 60 + 59)["due"]], [14 * 60 + 59])
        self.assertEqual(at(15 * 60 + 1)["due"], [])
        self.assertEqual([d["in_s"] for d in at(15 * 60)["due"]], [900])
        self.assertEqual([d["in_s"] for d in at(0)["due"]], [0])
        self.assertEqual([d["in_s"] for d in at(-1)["due"]], [-1])       # elapsed, maybe not yet fired (AccuracySec)
        self.assertEqual([d["in_s"] for d in at(-299)["due"]], [-299])
        self.assertEqual(at(-301)["due"], [])                    # long past: fired (or never will); its service is the busy check's
        self.assertTrue(at(15 * 60 + 1)["safe"])
        self.assertFalse(at(14 * 60 + 59)["safe"])

    def test_a_monotonic_only_timer_is_judged_too(self) -> None:
        fail = (1, "")
        def mono(delta_s: int, boot_s: float = 1000.0, rt: str = "", via: str = "busctl"):
            usec = int((boot_s + delta_s) * 1_000_000)
            if via == "busctl":
                answers = self.busctl_answers(0, usec)
            else:
                answers = {"busctl": fail, "systemctl show": self.show_answer(rt, str(usec))}
            return self.guard_one(answers, mono_now=boot_s)
        for via in ("busctl", "show"):
            with self.subTest(via):
                self.assertEqual([d["in_s"] for d in mono(14 * 60 + 59, via=via)["due"]], [14 * 60 + 59])
                self.assertEqual(mono(15 * 60 + 1, via=via)["due"], [])
                self.assertEqual([d["in_s"] for d in mono(-30, via=via)["due"]], [-30])
                self.assertEqual(mono(-3600, via=via)["due"], [])
                self.assertFalse(mono(60, via=via)["safe"])
        # both triggers: the sooner one counts (calendar far, monotonic near - and the reverse)
        far = self.T0 + 5 * 3600
        both = {"busctl": (lambda line: (0, f"t {far * 1_000_000}\n") if line.endswith("Realtime")
                           else (0, f"t {int((1000 + 300) * 1e6)}\n"))}
        self.assertEqual([d["in_s"] for d in self.guard_one(both)["due"]], [300])
        both = {"busctl": (lambda line: (0, f"t {(self.T0 + 120) * 1_000_000}\n") if line.endswith("Realtime")
                           else (0, f"t {int((1000 + 5 * 3600) * 1e6)}\n"))}
        self.assertEqual([d["in_s"] for d in self.guard_one(both)["due"]], [120])
        # a formatted timespan for the monotonic trigger cannot be read exactly: closed, not idle
        g = self.guard_one({"busctl": fail, "systemctl show": self.show_answer("", "3h 12min 4.5s")})
        self.assertEqual((len(g["unknown"]), g["safe"]), (1, False))
        # the timer clocks: due on EITHER of boottime / monotonic
        r = load_remote()
        r._run = self.guard_runner(self.busctl_answers(0, int((1000 + 300) * 1e6)))
        r.NOW = lambda: self.T0
        r.MONO_CLOCKS = (lambda: 1000.0 + 290 * 24 * 3600, lambda: 1000.0)     # boottime far ahead (suspended), monotonic not
        self.assertEqual([d["in_s"] for d in r.step_guard({"services": [], "timers": ["a.timer"]})["due"]], [300])

    def test_an_unreadable_timer_says_why_with_exit_codes_and_the_first_output(self) -> None:
        g = self.guard_one({"busctl": (1, "Failed to get property NextElapseUSecRealtime: No such interface\n"),
                            "systemctl show": (0, "NextElapseUSecRealtime=" + "x" * 200 + "\n")})
        (msg,) = g["unknown"]
        self.assertIn("prometheus-scan.timer: its next run could not be read", msg)
        self.assertIn("busctl: exit 1 'Failed to get property NextElapseUSecRealtime: No such interface'"[:70], msg)
        self.assertIn("show: exit 0 'NextElapseUSecRealtime=" + "x" * 37 + "'", msg)         # first 60 characters only
        self.assertNotIn("x" * 38, msg)
        self.assertIn("show utc: exit 0", msg)
        self.assertIn("show unix: exit 0", msg)
        self.assertIn("systemd 249", msg)

    def test_the_guard_asks_the_bus_first_and_pins_utc_and_english(self) -> None:
        calls, envs = [], []
        r = load_remote()
        base = self.guard_runner(self.busctl_answers((self.T0 + 7200) * 1_000_000), calls)
        def spy(argv, timeout=30, env=None, merge_stderr=False):
            envs.append((argv[0], env, merge_stderr))
            return base(argv, timeout, env, merge_stderr)
        r._run = spy
        r.NOW = lambda: self.T0
        r.MONO_CLOCKS = (lambda: 1000.0,)
        r.step_guard({"services": [], "timers": ["mrcrab-t1.timer"]})
        self.assertTrue(any("busctl get-property org.freedesktop.systemd1 /org/freedesktop/systemd1/unit/mrcrab_2dt1_2etimer "
                            "org.freedesktop.systemd1.Timer NextElapseUSecRealtime" in c for c in calls), calls)
        (_, env, merged), = [e for e in envs if e[0] == "busctl"][:1]
        self.assertEqual((env["TZ"], env["LC_ALL"], merged), ("UTC", "C", True))
        self.assertFalse(any(c.startswith("systemctl show") for c in calls))            # the bus answered: nothing else asked
        self.assertEqual(r._bus_path("prometheus-scan.timer"), "/org/freedesktop/systemd1/unit/prometheus_2dscan_2etimer")

    def test_run_can_merge_stderr_into_the_answer(self) -> None:
        r = load_remote()
        code, out = r._run([sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"], merge_stderr=True)
        self.assertEqual((code, sorted(out.split())), (3, ["err", "out"]))
        self.assertEqual(r._run([sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"]), (0, "out\n"))

    def test_tailscale_reports_key_expiry_of_the_vps_and_the_phones(self) -> None:
        self.sim.set(ts_state="Running", ts_key_expiry="2027-01-01T00:00:00Z", phone_expiry="2027-02-01T00:00:00Z")
        st = self.r.tailscale_state()
        self.assertEqual(st["key_expiry"], "2027-01-01T00:00:00Z")
        self.assertEqual(st["mobiles"], [{"name": "pixel", "os": "android", "key_expiry": "2027-02-01T00:00:00Z"}])
        self.sim.set(ts_key_expiry=None, phone_expiry=None)
        st = self.r.tailscale_state()
        self.assertEqual((st["key_expiry"], st["mobiles"][0]["key_expiry"]), (None, None))

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
        # root's own effective config would move: put back, not reloaded
        self.sim.set(sshd_effective="follow", root_drift=True)
        self.assertIn("root's own effective sshd config would change", self.r.install_sshd_block("pionir-tunnel", opens)["error"])
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

    def test_restore_keeps_a_live_orders_switch_and_can_restart_nothing(self) -> None:
        self._units()
        f = self.dir / "rh_api.env"
        self.r.update_env_file(str(f), {"PRO_RH_API_KEY": "n" * 43}, {"PRO_RH_API_KEY": "PRO_RH_API_KEY_PREVIOUS"}, [], "20260928T000000Z")
        f.write_text(f.read_text(encoding="utf-8").replace("PRO_RH_ORDERS_ENABLED=0", "PRO_RH_ORDERS_ENABLED=1"), encoding="utf-8")
        pid = self.sim.state()["units"]["pro-robinhood-api.service"]["pid"]
        out = self.r.step_restore({"stamp": "20260928T000000Z", "unit": "pro-robinhood-api.service", "was_active": False,
                                   "files": [str(f).replace(os.sep, "/")], "keep_live": ["PRO_RH_ORDERS_ENABLED"]})
        env = self.r.parse_env(f.read_text(encoding="utf-8"))
        self.assertEqual((env["PRO_RH_API_KEY"], env["PRO_RH_ORDERS_ENABLED"]), ("a", "1"))
        self.assertNotIn("PRO_RH_API_KEY_PREVIOUS", env)
        self.assertTrue(out["restored"])
        self.assertEqual(self.sim.state()["units"]["pro-robinhood-api.service"]["pid"], pid)
        self.assertFalse(any(c[:2] == ["systemctl", "restart"] for c in self.sim.commands()))
        # nothing moved: the restore is byte-for-byte the backup (even CRLF, even no final newline),
        # and a name absent live stays absent
        f.write_bytes(b"PRO_RH_API_KEY=a\r\nPRO_RH_ORDERS_ENABLED=0")
        self.r.update_env_file(str(f), {"PRO_RH_API_KEY": "m" * 43}, {"PRO_RH_API_KEY": "PRO_RH_API_KEY_PREVIOUS"}, [], "20260928T000001Z")
        self.r.step_restore({"stamp": "20260928T000001Z", "unit": "pro-robinhood-api.service", "was_active": False,
                             "files": [str(f).replace(os.sep, "/")], "keep_live": ["PRO_RH_ORDERS_ENABLED", "NOT_SET_ANYWHERE"]})
        self.assertEqual(f.read_bytes(), Path(str(f) + ".bak-pionir-20260928T000001Z").read_bytes())

    def test_restore_never_rewrites_a_py_file_that_has_an_env_looking_line(self) -> None:
        self._units()
        posix = lambda f: str(f).replace(os.sep, "/")
        stamp = "20260928T000002Z"
        py = self.dir / "robinhood_read_api.py"
        line = "PRO_RH_ORDERS_ENABLED=1 is set in the environment to arm real-money orders."
        old = f'"""The read API.\n{line}\n"""\nimport os\nORDERS = os.environ.get("PRO_RH_ORDERS_ENABLED") == "1"\n'
        py.write_text(old, encoding="utf-8", newline="")
        Path(str(py) + ".bak-pionir-" + stamp).write_text(old, encoding="utf-8", newline="")
        # a later pin reflowed that docstring line, so the live .py no longer has a line that looks like an assignment
        py.write_text(old.replace(line, "Real-money orders are armed by setting the environment."), encoding="utf-8", newline="")
        env = self.dir / "rh_api.env"
        self.r.update_env_file(str(env), {"PRO_RH_API_KEY": "n" * 43}, {"PRO_RH_API_KEY": "PRO_RH_API_KEY_PREVIOUS"}, [], stamp)
        env.write_text(env.read_text(encoding="utf-8").replace("PRO_RH_ORDERS_ENABLED=0", "PRO_RH_ORDERS_ENABLED=1"), encoding="utf-8")
        out = self.r.step_restore({"stamp": stamp, "unit": "pro-robinhood-api.service", "was_active": False,
                                   "files": [posix(env), posix(py)], "keep_live": ["PRO_RH_ORDERS_ENABLED"]})
        self.assertEqual(out["restored"], [posix(env), posix(py)])
        self.assertEqual(py.read_bytes(), old.encode("utf-8"))          # byte for byte: no live line appended
        compile(py.read_text(encoding="utf-8"), str(py), "exec")       # and the restored server still compiles
        # the env file, through the same call, still keeps the live switch
        self.assertEqual(self.r.parse_env(env.read_text(encoding="utf-8"))["PRO_RH_ORDERS_ENABLED"], "1")
        # and a .py whose docstring line the live file still has is no different: never rewritten
        py.write_text(old.replace("The read API.", "The read API, later pin."), encoding="utf-8", newline="")
        self.r.step_restore({"stamp": stamp, "unit": "pro-robinhood-api.service", "was_active": False,
                             "files": [posix(py)], "keep_live": ["PRO_RH_ORDERS_ENABLED"]})
        self.assertEqual(py.read_bytes(), old.encode("utf-8"))

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
