"""A fake droplet for the lockdown tests: systemd units (with /proc environments), ufw,
transient timers, Tailscale, sshd, nginx and apt - as far as tools/vps_lockdown/remote.py
uses them - with its state in a JSON file, so it survives across the separate ssh "logins"
a lockdown run makes. `install(remote, root)` points the remote half's paths into `root`
and its command runner at the simulator. Nothing here touches a network or the system.
"""
from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path

RULE_TEXT = {
    ("allow", "22/tcp"): "22/tcp                     ALLOW IN    Anywhere",
    ("allow", "in", "on", "tailscale0", "to", "any", "port", "8000:8002", "proto", "tcp"):
        "8000:8002/tcp on tailscale0 ALLOW IN    Anywhere",
    ("deny", "8000:8002/tcp"): "8000:8002/tcp              DENY IN     Anywhere",
}
TS_LIST_OK = "deb [signed-by=/usr/share/keyrings/tailscale-archive-keyring.gpg] https://pkgs.tailscale.com/stable/ubuntu noble main\n"
UNITS = {
    "pro-robinhood-api.service": ("rh_api.env", "PRO_RH_ORDERS_ENABLED=0"),
    "prometheus-api.service": ("pro.env", ""),
    "mrcrab-api.service": ("mrcrab.env", ""),
}


def _posix(p) -> str:
    return str(p).replace(os.sep, "/")


class Sim:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / "sim.json"

    # ---- state
    def state(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text(encoding="utf-8"))
        return {"ufw_installed": True, "ufw_active": False, "default_in": "deny", "rules": [],
                "timers": {}, "ts_installed": False, "ts_state": "NoState", "ts_polls": 0,
                "ts_polls_to_login": 2, "units": {}, "next_pid": 1000, "log": [],
                "sshd_ok": True, "sshd_effective": "follow", "reloads": 0}

    def save(self, s: dict) -> None:
        self.path.write_text(json.dumps(s, indent=1), encoding="utf-8")

    def set(self, **kw) -> None:
        s = self.state()
        s.update(kw)
        self.save(s)

    def unit(self, name: str, **kw) -> None:
        s = self.state()
        s["units"].setdefault(name, {}).update(kw)
        self.save(s)

    # ---- units and /proc
    def boot_units(self, armed_running: str = "0") -> None:
        """Every unit active, its process started with its env files as they are now."""
        s = self.state()
        for name, (env_file, environment) in UNITS.items():
            u = s["units"].setdefault(name, {})
            u.setdefault("active", True)
            u.setdefault("env_file", _posix(self.root / env_file))
            u.setdefault("environment", environment)
            if u["active"] and "pid" not in u:
                s["next_pid"] += 1
                u["pid"] = s["next_pid"]
                env = self._configured(u)
                if name == "pro-robinhood-api.service":
                    env["PRO_RH_ORDERS_ENABLED"] = armed_running
                self._write_proc(u["pid"], env)
        self.save(s)

    def _configured(self, u: dict) -> dict:
        from importlib import util
        env = {}
        for w in shlex.split(u.get("environment", "")):
            k, _, v = w.partition("=")
            env[k] = v
        text = Path(u["env_file"]).read_text(encoding="utf-8") if Path(u["env_file"]).exists() else ""
        env.update(_parse_env(text))
        return env

    def _write_proc(self, pid: int, env: dict) -> None:
        d = self.root / "proc" / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in env.items()) + b"\0")

    def running_env(self, unit: str) -> dict:
        u = self.state()["units"][unit]
        raw = (self.root / "proc" / str(u["pid"]) / "environ").read_bytes()
        return dict(item.decode().partition("=")[::2] for item in raw.split(b"\0") if item)

    # ---- the command runner remote._run is replaced with
    def run(self, argv, timeout=30, env=None):
        s = self.state()
        s["log"].append(list(argv))
        code, out = self._run(s, list(argv))
        self.save(s)
        return code, out

    def _run(self, s: dict, a: list) -> tuple[int, str]:
        if a[:1] == ["ufw"]:
            return self._ufw(s, a[1:])
        if a[:1] == ["systemd-run"]:
            unit = next(x.split("=", 1)[1] for x in a if x.startswith("--unit="))
            if "tailscale" in a:
                if s.get("ts_fail_up"):
                    return 1, ""
                s["ts_state"] = "NeedsLogin"
                return 0, ""
            if s.get("fail_timer"):
                return 1, ""
            s["timers"][unit] = {"active": True, "script": a[-1],
                                 "on_active": next(x for x in a if x.startswith("--on-active="))}
            return 0, ""
        if a[:2] == ["systemctl", "show"]:
            return self._show(s, a[2], a[4])
        if a[:2] == ["systemctl", "is-active"]:
            name = a[2]
            if name in s.get("sched", {}):
                return 0, "active\n"
            if name.endswith(".timer"):
                t = s["timers"].get(name[:-6])
                return (0, "active\n") if t and t["active"] else (3, "inactive\n")
            u = s["units"].get(name)
            if u:
                return (0, "active\n") if u.get("active") else (3, "failed\n" if u.get("failed") else "inactive\n")
            return 3, "inactive\n"
        if a[:2] == ["systemctl", "try-restart"] or a[:2] == ["systemctl", "restart"]:
            u = s["units"][a[2]]
            if a[1] == "try-restart" and not u.get("active"):
                return 0, ""
            if u.get("fail_restart") and a[1] == "try-restart":
                u["active"], u["failed"] = False, True
                u.pop("pid", None)
                return 1, ""
            s["next_pid"] += 1
            u["pid"], u["active"], u["failed"] = s["next_pid"], True, False
            u["restarts"] = u.get("restarts", 0) + 1
            self._write_proc(u["pid"], self._configured(u))
            return 0, ""
        if a[:2] == ["systemctl", "stop"] and a[2].endswith(".timer"):
            if not s.get("stuck_timer") and a[2][:-6] in s["timers"]:
                s["timers"][a[2][:-6]]["active"] = False
            return 0, ""
        if a[:2] in (["systemctl", "reset-failed"],):
            return 0, ""
        if a[:2] == ["systemctl", "reload"]:
            if a[2] in ("ssh", "sshd"):
                s["reloads"] += 1
            return 0, ""
        if a[:1] == ["apt-get"]:
            if a[-1] == "ufw":
                s["ufw_installed"] = True
            if a[-1] == "tailscale":
                s["ts_installed"] = True
            return 0, ""
        if a[:1] == ["tailscale"]:
            return self._tailscale(s, a[1:])
        if a == ["sshd", "-t"]:
            return (0, "") if s["sshd_ok"] else (255, "")
        if a[:3] == ["sshd", "-T", "-C"]:
            if a[3].startswith("user=root,"):
                base = "permitrootlogin prohibit-password\nallowtcpforwarding yes\n"
                dropin = self.root / "ssh" / "sshd_config.d" / "60-pionir-tunnel.conf"
                if s.get("root_drift") and dropin.exists():
                    base += "permittty no\n"
                return 0, base
            return 0, self._sshd_effective(s)
        if a == ["sshd", "-T"]:
            return 0, "allowtcpforwarding yes\n"
        if a == ["nginx", "-t"]:
            return (1, "") if s.get("nginx_bad") else (0, "")
        return 127, ""

    def _show(self, s: dict, unit: str, prop: str) -> tuple[int, str]:
        if prop == "NextElapseUSecRealtime":
            return 0, f"@{s.get('sched', {}).get(unit, 0)}\n"
        u = s["units"].get(unit, {})
        if prop == "MainPID":
            return 0, f"{u.get('pid', 0)}\n"
        if prop == "Environment":
            return 0, u.get("environment", "") + "\n"
        if prop == "EnvironmentFiles":
            return 0, f"{u['env_file']} (ignore_errors=no)\n" if u.get("env_file") else "\n"
        if prop == "ExecStart":
            if unit == "pro-robinhood-api.service":
                script = _posix(self.root / "robinhood_read_api.py")
                return 0, f"{{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 {script} ; ignore_errors=no }}\n"
            return 0, "{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 x.py }\n"
        if prop == "WorkingDirectory":
            return 0, _posix(self.root) + "\n"
        return 0, "\n"

    def _sshd_effective(self, s: dict) -> str:
        if s["sshd_effective"] != "follow":
            return s["sshd_effective"]
        text = ""
        for f in (self.root / "ssh" / "sshd_config.d" / "60-pionir-tunnel.conf", self.root / "ssh" / "sshd_config"):
            if f.exists():
                text += f.read_text(encoding="utf-8")
        m = re.search(r"Match User pionir-tunnel\n((?:    .*\n)+)", text)
        if not m:
            return "allowtcpforwarding yes\npermitlisten any\n"
        out = []
        for line in m.group(1).splitlines():
            k, _, v = line.strip().partition(" ")
            out.append(f"{k.lower()} {v}")
        return "\n".join(out) + "\n"

    def _ufw(self, s: dict, a: list) -> tuple[int, str]:
        if a == ["status"]:
            return 0, "Status: active\n" if s["ufw_active"] else "Status: inactive\n"
        if a == ["status", "numbered"]:
            if not s["ufw_active"]:
                return 0, "Status: inactive\n"
            lines = ["Status: active", "", "     To                         Action      From",
                     "     --                         ------      ----"]
            lines += [f"[{i:2d}] {r}" for i, r in enumerate(s["rules"], 1)]
            return 0, "\n".join(lines) + "\n"
        if a == ["default", "allow", "incoming"]:
            s["default_in"] = "allow"
            return 0, ""
        if a == ["--force", "enable"]:
            s["ufw_active"] = True
            return 0, ""
        if a == ["--force", "disable"]:
            s["ufw_active"] = False
            return 0, ""
        if a[:1] == ["insert"]:
            pos, rule = int(a[1]), RULE_TEXT.get(tuple(a[2:]))
            if rule is None or pos < 1 or pos > len(s["rules"]) + 1 or not s["rules"]:
                return 1, "ERROR: Invalid position\n"
            if rule not in s["rules"]:
                s["rules"].insert(pos - 1, rule)
            return 0, ""
        rule = RULE_TEXT.get(tuple(a))
        if rule is None:
            return 1, "ERROR\n"
        if rule not in s["rules"]:
            s["rules"].append(rule)
        return 0, ""

    def _tailscale(self, s: dict, a: list) -> tuple[int, str]:
        if a == ["version"]:
            return 0, "1.90.0\n"
        if a == ["status", "--json"]:
            if s["ts_state"] == "NeedsLogin":
                s["ts_polls"] += 1
                if s["ts_polls"] > s["ts_polls_to_login"]:
                    s["ts_state"] = "Running"
                    (self.root / "net").mkdir(exist_ok=True)
                    (self.root / "net" / "tailscale0").write_text("up")
            doc = {"BackendState": s["ts_state"], "Self": {}, "Peer": {
                "nodekey:phone": {"HostName": "pixel", "OS": "android", **({"KeyExpiry": s["phone_expiry"]} if s.get("phone_expiry") else {})},
                "nodekey:pc": {"HostName": "desktop", "OS": "windows", "KeyExpiry": "2027-01-01T00:00:00Z"}}}
            if s["ts_state"] == "NeedsLogin":
                doc["AuthURL"] = "https://login.tailscale.com/a/pionirtest123"
            if s["ts_state"] == "Running":
                doc["Self"] = {"HostName": "proteus-vps", "DNSName": "proteus-vps.tail1234.ts.net.",
                               "TailscaleIPs": ["100.64.0.7", "fd7a:115c:a1e0::7"]}
                if s.get("ts_key_expiry"):
                    doc["Self"]["KeyExpiry"] = s["ts_key_expiry"]
            return 0, json.dumps(doc)
        return 1, ""

    def which(self, name: str):
        s = self.state()
        if name == "ufw" and s["ufw_installed"]:
            return "/usr/sbin/ufw"
        if name == "tailscale" and s["ts_installed"]:
            return "/usr/bin/tailscale"
        return None

    def rules(self) -> list:
        return self.state()["rules"]

    def commands(self) -> list:
        return self.state()["log"]


def _parse_env(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$", line)
        if not m or line.lstrip().startswith("#"):
            continue
        v = m.group(2).strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[m.group(1)] = v
    return out


def install(remote, root) -> Sim:
    root = Path(root)
    sim = Sim(root)
    for d in ("root", "etc/ufw", "etc/default", "net", "sites-enabled", "keyrings", "apt", "proc",
              "ssh/sshd_config.d"):
        (root / d).mkdir(parents=True, exist_ok=True)
    for f, text in (("etc/ufw/user.rules", "# user rules\n"), ("etc/ufw/user6.rules", "# user6 rules\n"),
                    ("etc/default/ufw", "DEFAULT_INPUT_POLICY=\"DROP\"\n"),
                    ("ssh/sshd_config", "Include /etc/ssh/sshd_config.d/*.conf\nPermitRootLogin prohibit-password\n")):
        if not (root / f).exists():
            (root / f).write_text(text, encoding="utf-8")
    if not (root / "os-release").exists():
        (root / "os-release").write_text('ID=ubuntu\nVERSION_CODENAME=noble\nPRETTY_NAME="Ubuntu 24.04"\n', encoding="utf-8")
    remote._run = sim.run
    remote.WHICH = sim.which
    remote.SLEEP = lambda s: None
    remote.PROC = _posix(root / "proc")
    remote.BACKUP_ROOT = _posix(root / "root")
    remote.UFW_FILES = tuple(_posix(root / f) for f in ("etc/ufw/user.rules", "etc/ufw/user6.rules", "etc/default/ufw"))
    remote.NET_DIR = _posix(root / "net")
    remote.OS_RELEASE = str(root / "os-release")
    remote.TS_KEYRING = "/usr/share/keyrings/tailscale-archive-keyring.gpg"   # the name the list must carry
    remote.TS_LIST = str(root / "apt" / "tailscale.list")
    remote.NGINX_SITES = _posix(root / "sites-enabled")
    remote.SSHD_MAIN = _posix(root / "ssh" / "sshd_config")
    remote.SSHD_DROPIN = _posix(root / "ssh" / "sshd_config.d" / "60-pionir-tunnel.conf")

    def fetch(url: str) -> bytes:
        assert re.fullmatch(r"https://pkgs\.tailscale\.com/stable/ubuntu/noble\.(noarmor\.gpg|tailscale-keyring\.list)", url), url
        return b"\x99fakekey" if url.endswith(".gpg") else TS_LIST_OK.encode()
    remote._fetch = fetch
    remote.HTTP_JSON = lambda url: {"status": "ok", "jobs_running": sim.state().get("jobs_running", 0)}
    keyring = root / "keyrings" / "tailscale-archive-keyring.gpg"
    real_open, real_chmod = open, os.chmod

    def redirect_open(path, *a, **k):
        if str(path) == remote.TS_KEYRING:
            path = keyring
        return real_open(path, *a, **k)
    remote.open = redirect_open

    class _Os:
        def __getattr__(self, name):
            return getattr(os, name)

        @staticmethod
        def chmod(path, mode):
            if str(path) == remote.TS_KEYRING:
                path = keyring
            return real_chmod(path, mode)

        @staticmethod
        def chown(*a, **k):          # Windows has none; the droplet's own is exercised for real
            return None
    remote.os = _Os()
    return sim
