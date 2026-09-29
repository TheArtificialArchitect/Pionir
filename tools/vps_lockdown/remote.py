"""Pionir VPS lockdown - the remote half, run by python3 as root on the droplet.

tools\\vps-lockdown.ps1 sends this file and its payload over ssh STDIN: the first line is
this program (base64), the rest a JSON payload. The remote command line is one fixed
string with no value in it:

    python3 -c 'import sys,base64;exec(base64.b64decode(sys.stdin.readline().lstrip(chr(65279))))'

(a stray BOM - Windows PowerShell's redirected stdin may start with one - is dropped)

so no key ever appears on either machine's process list. Every secret arrives in the
payload and is NEVER printed: each step answers one JSON line of names, paths, states and
counts only.

Steps (payload["step"]):
  discover     read-only: the units that serve the three APIs, their env files and which
               key NAMES each holds, the RH script path and whether it has the read key,
               the tunnel account, sshd forwarding settings, ufw, listeners, nginx, tailscale
  tunnel_user  the unprivileged forwarding-only account and its authorized_keys
  deploy       put a new robinhood_read_api.py in place (backup, compile check, same owner)
  env          set / unset keys in env files (backup first, same owner and mode)
  restart      systemctl try-restart (never STARTS a stopped live-money unit), then verify a
               new MainPID, the armed state unchanged, the expected key names present
  firewall     ufw: keep 22 open, deny the given API ports (tailscale0 still allowed)

Importable for tests: every step is a function of its payload.
"""
from __future__ import annotations

import base64
import glob
import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import time

MARKERS = {"robinhood": "robinhood_read_api.py", "prometheus": "prometheus.webapp",
           "karkinos": "karkinos_read_api.py"}
KEY_NAMES = {"robinhood": "PRO_RH_API_KEY", "prometheus": "PROM_API_KEY",
             "karkinos": "KARKINOS_API_KEY"}
UNIT_DIRS = ("/etc/systemd/system", "/lib/systemd/system", "/usr/lib/systemd/system")
VALUE_RE = re.compile(r"[A-Za-z0-9_-]{32,128}")
NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
UNIT_RE = re.compile(r"[a-z0-9][a-z0-9@_.-]*\.service")
USER_RE = re.compile(r"[a-z][a-z0-9-]{0,30}")
PUBKEY_RE = re.compile(r"ssh-ed25519 [A-Za-z0-9+/]+={0,2}( [A-Za-z0-9@._-]{1,64})?")
OPEN_RE = re.compile(r"127\.0\.0\.1:\d{1,5}")
ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def emit(obj: dict) -> None:
    print(json.dumps(obj, sort_keys=True))


def _run(argv: list[str], timeout: float = 30) -> tuple[int, str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return done.returncode, done.stdout
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


# ---- env files -----------------------------------------------------------------------------
def env_name(line: str) -> str | None:
    if line.lstrip().startswith("#"):
        return None
    m = ENV_LINE.match(line)
    return m.group(1) if m else None


def parse_env(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = ENV_LINE.match(line)
        if not m or line.lstrip().startswith("#"):
            continue
        value = m.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[m.group(1)] = value
    return out


def update_env_file(path: str, sets: dict[str, str], keep_previous: dict[str, str],
                    unset: list[str], stamp: str) -> dict:
    """Set/unset names in an env file, keeping every other line as it was. The first time
    for this stamp the file is backed up to <path>.bak-pionir-<stamp> (never overwritten, so
    it always holds the content from before this rotation). keep_previous {OLD: PREV}: the
    current value of OLD is kept under PREV when OLD changes and PREV is not set yet."""
    if not os.path.isfile(path):
        return {"path": path, "error": "missing"}
    for name, value in sets.items():
        if not NAME_RE.fullmatch(name) or not isinstance(value, str) or not VALUE_RE.fullmatch(value):
            return {"path": path, "error": f"refused a malformed value for {name}"}
    for name in list(unset) + list(keep_previous) + list(keep_previous.values()):
        if not NAME_RE.fullmatch(name):
            return {"path": path, "error": f"refused a malformed name {name!r}"}
    st = os.stat(path)
    text = _read(path)
    current = parse_env(text)
    sets = dict(sets)
    for old, prev in keep_previous.items():
        if current.get(old) and current[old] != sets.get(old, current[old]) and prev not in current:
            sets[prev] = current[old]
    backup = f"{path}.bak-pionir-{stamp}"
    if not os.path.exists(backup):
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)
        if hasattr(os, "chown"):
            os.chown(backup, st.st_uid, st.st_gid)
    def assign(name: str, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.+/=~:-]*", value):   # a kept old value, say
            value = '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
        return f"{name}={value}"

    lines, done = [], set()
    for line in text.splitlines():
        name = env_name(line)
        if name is not None and name in unset:
            continue
        if name is not None and name in sets:
            if name not in done:
                lines.append(assign(name, sets[name]))
                done.add(name)
            continue
        lines.append(line)
    lines += [assign(name, value) for name, value in sets.items() if name not in done]
    changed = sorted(n for n, v in sets.items() if current.get(n) != v)
    removed = sorted(n for n in unset if n in current)
    if changed or removed:
        tmp = f"{path}.pionir-tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
        os.chmod(tmp, st.st_mode & 0o7777)
        if hasattr(os, "chown"):
            os.chown(tmp, st.st_uid, st.st_gid)
        os.replace(tmp, path)
    return {"path": path, "backup": backup, "changed": changed, "unset": removed,
            "names": sorted(set(parse_env(_read(path))))}


def step_env(p: dict) -> dict:
    stamp = str(p["stamp"])
    if not re.fullmatch(r"\d{8}T\d{6}Z?", stamp):
        return {"step": "env", "error": "bad stamp"}
    files = [update_env_file(f["path"], f.get("set", {}), f.get("keep_previous", {}),
                             f.get("unset", []), stamp) for f in p["files"]]
    return {"step": "env", "files": files, "ok": all("error" not in f for f in files)}


# ---- units --------------------------------------------------------------------------------
def env_files_from_show(value: str) -> list[str]:
    """systemctl show -p EnvironmentFiles --value: '/x/a.env (ignore_errors=no)' per file."""
    return re.findall(r"(/[^\s()]+)\s*\(ignore_errors=(?:yes|no)\)", value or "")


def script_from_execstart(value: str, name: str, workdir: str = "") -> str | None:
    """The path of `name` in an ExecStart (systemctl show -p ExecStart --value)."""
    for token in re.split(r"[\s;{}]+", value or ""):
        token = token.strip("'\"")
        if token.endswith("/" + name) or token == name:
            if token.startswith("/"):
                return token
            return posixpath.join(workdir, token) if workdir.startswith("/") else None
    return None


def find_units(unit_dirs=UNIT_DIRS) -> dict[str, str]:
    found: dict[str, str] = {}
    for d in unit_dirs:
        for path in sorted(glob.glob(os.path.join(d, "*.service"))):
            text = _read(path)
            for api, marker in MARKERS.items():
                if api not in found and marker in text:
                    found[api] = os.path.basename(path)
    return found


def _show(unit: str, prop: str) -> str:
    return _run(["systemctl", "show", unit, "-p", prop, "--value"])[1].strip()


def _environ_names(pid: str) -> dict[str, str]:
    try:
        with open(f"/proc/{int(pid)}/environ", "rb") as fh:
            raw = fh.read().split(b"\0")
    except (OSError, ValueError):
        return {}
    out = {}
    for item in raw:
        k, _, v = item.decode("utf-8", "replace").partition("=")
        if k:
            out[k] = v
    return out


def _armed(pid: str) -> bool | None:
    env = _environ_names(pid)
    return None if not env else env.get("PRO_RH_ORDERS_ENABLED") == "1"


def step_discover(p: dict) -> dict:
    units = find_units()
    apis = {}
    for api in MARKERS:
        unit = units.get(api)
        info: dict = {"unit": unit}
        if unit:
            info["active"] = _run(["systemctl", "is-active", unit])[1].strip() or "unknown"
            env_files = env_files_from_show(_show(unit, "EnvironmentFiles"))
            info["env_files"] = env_files
            key = KEY_NAMES[api]
            holders = [f for f in env_files if key in parse_env(_read(f))]
            info["key_name"] = key
            info["key_file"] = holders[0] if holders else None
            info["env_names"] = sorted(set().union(*[set(parse_env(_read(f))) for f in env_files])) if env_files else []
            if api == "robinhood":
                script = script_from_execstart(_show(unit, "ExecStart"), MARKERS[api],
                                               _show(unit, "WorkingDirectory"))
                info["script"] = script
                text = _read(script) if script else ""
                info["read_key_support"] = "PRO_RH_READ_KEY" in text
                info["previous_key_support"] = "PRO_RH_API_KEY_PREVIOUS" in text
                info["orders_armed"] = _armed(_show(unit, "MainPID"))
        apis[api] = info
    user = str(p.get("tunnel_user", "pionir-tunnel"))
    home = f"/home/{user}"
    code, sshd = _run(["sshd", "-T"])
    wanted = ("allowtcpforwarding", "disableforwarding", "allowusers", "allowgroups", "permitopen")
    sshd_cfg = {}
    for line in sshd.splitlines():
        k, _, v = line.partition(" ")
        if k in wanted:
            sshd_cfg[k] = v.strip()[:200]
    ufw = _run(["ufw", "status"])[1].splitlines()
    listeners = []
    for line in _run(["ss", "-ltnp"])[1].splitlines()[1:]:
        cols = line.split()
        if len(cols) >= 4 and re.search(r":(80|443|8000|8001|8002)$", cols[3]):
            proc = re.search(r'users:\(\("([^"]+)"', line)
            listeners.append(f"{cols[3]} {proc.group(1) if proc else '?'}")
    nginx = [f for f in glob.glob("/etc/nginx/sites-enabled/*")
             if re.search(r"127\.0\.0\.1:800[012]|localhost:800[012]|:800[012]\b", _read(f))]
    return {"step": "discover", "apis": apis,
            "tunnel_user": {"exists": os.path.isdir(home),
                            "authorized_keys": os.path.isfile(f"{home}/.ssh/authorized_keys")},
            "sshd": sshd_cfg if code == 0 else {"error": "sshd -T failed"},
            "ufw": ufw[0] if ufw else "ufw: not installed",
            "listeners": listeners, "nginx_to_apis": nginx,
            "tailscale": os.path.exists("/sys/class/net/tailscale0"),
            "python": sys.version.split()[0]}


# ---- the tunnel account --------------------------------------------------------------------
def authorized_line(pubkey: str, opens: list[str]) -> str:
    """Forwarding to the given loopback ports and nothing else: restrict turns off every
    feature (pty, agent, X11, user rc, all forwarding); port-forwarding turns local
    forwarding back on, limited by permitopen; the forced command answers any session."""
    pubkey = pubkey.strip()
    if not PUBKEY_RE.fullmatch(pubkey):
        raise ValueError("not an ssh-ed25519 public key line")
    if not opens or not all(OPEN_RE.fullmatch(o) for o in opens):
        raise ValueError("permitopen must be 127.0.0.1:<port> entries")
    opts = ",".join(["restrict", "port-forwarding"] + [f'permitopen="{o}"' for o in opens]
                    + ['command="/bin/false"'])
    return f"{opts} {pubkey}\n"


def step_tunnel_user(p: dict) -> dict:
    import pwd  # the droplet only

    user = str(p["user"])
    if not USER_RE.fullmatch(user) or user == "root":
        return {"step": "tunnel_user", "error": "bad user name"}
    line = authorized_line(str(p["pubkey"]), list(p["opens"]))
    created = False
    try:
        pw = pwd.getpwnam(user)
    except KeyError:
        shell = "/usr/sbin/nologin" if os.path.exists("/usr/sbin/nologin") else "/bin/false"
        code, _ = _run(["useradd", "--system", "--create-home", "--home-dir", f"/home/{user}",
                        "--shell", shell, user])
        if code != 0:
            return {"step": "tunnel_user", "error": "useradd failed"}
        pw, created = pwd.getpwnam(user), True
    ssh_dir = os.path.join(pw.pw_dir, ".ssh")
    os.makedirs(ssh_dir, exist_ok=True)
    os.chown(ssh_dir, 0, 0)            # root's: the account cannot change its own keys
    os.chmod(ssh_dir, 0o755)
    path = os.path.join(ssh_dir, "authorized_keys")
    tmp = path + ".pionir-tmp"
    with open(tmp, "w", encoding="ascii", newline="\n") as fh:
        fh.write(line)
    os.chown(tmp, 0, 0)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    code, sshd = _run(["sshd", "-T", "-C", f"user={user},host=pionir,addr=127.0.0.1"])
    cfg = dict(line_.partition(" ")[::2] for line_ in sshd.splitlines())
    forwarding = cfg.get("allowtcpforwarding", "yes") in ("yes", "all", "local") and \
        cfg.get("disableforwarding", "no") == "no"
    allowed = cfg.get("allowusers", "")
    return {"step": "tunnel_user", "user": user, "created": created, "authorized_keys": path,
            "forwarding_allowed": forwarding if code == 0 else None,
            "allowusers": allowed[:200] or None}


# ---- the server file ------------------------------------------------------------------------
def step_deploy(p: dict) -> dict:
    content = base64.b64decode(p["content_b64"])
    if hashlib.sha256(content).hexdigest() != p["sha256"]:
        return {"step": "deploy", "error": "content does not match its sha256"}
    for word in p.get("must_contain", []):
        if word.encode() not in content:
            return {"step": "deploy", "error": f"the new file lacks {word}"}
    path = p.get("path")
    if not path:
        unit = p["unit"]
        if not UNIT_RE.fullmatch(unit):
            return {"step": "deploy", "error": "bad unit"}
        path = script_from_execstart(_show(unit, "ExecStart"), p["name"], _show(unit, "WorkingDirectory"))
    if not path or not os.path.isfile(path):
        return {"step": "deploy", "error": "the running script was not found"}
    try:
        compile(content, path, "exec")
    except SyntaxError as error:
        return {"step": "deploy", "error": f"does not compile: line {error.lineno}"}
    with open(path, "rb") as fh:
        if hashlib.sha256(fh.read()).hexdigest() == p["sha256"]:
            return {"step": "deploy", "path": path, "deployed": False, "unchanged": True}
    st = os.stat(path)
    backup = f"{path}.bak-pionir-{p['stamp']}"
    if not os.path.exists(backup):
        shutil.copy2(path, backup)
    tmp = path + ".pionir-tmp"
    with open(tmp, "wb") as fh:
        fh.write(content)
    os.chmod(tmp, st.st_mode & 0o7777)
    if hasattr(os, "chown"):
        os.chown(tmp, st.st_uid, st.st_gid)
    os.replace(tmp, path)
    return {"step": "deploy", "path": path, "backup": backup, "deployed": True}


# ---- restart and verify ---------------------------------------------------------------------
def step_restart(p: dict) -> dict:
    """try-restart each unit (a stopped unit stays stopped - a brake is never undone here),
    then prove it: a new MainPID, active, the orders switch as it was, and the expected key
    NAMES in the running process's environment (the RH API reads env only at import)."""
    results = []
    for unit, expect in p["units"].items():
        if not UNIT_RE.fullmatch(unit):
            results.append({"unit": unit, "error": "bad unit"})
            continue
        active = _run(["systemctl", "is-active", unit])[1].strip()
        if active != "active":
            results.append({"unit": unit, "state": active or "unknown", "restarted": False,
                            "note": "not running: left as it is (it reads the new keys when next started)"})
            continue
        pid0 = _show(unit, "MainPID")
        armed0 = _armed(pid0)
        _run(["systemctl", "try-restart", unit], timeout=60)
        pid1, state = pid0, ""
        for _ in range(30):
            time.sleep(0.5)
            pid1 = _show(unit, "MainPID")
            state = _run(["systemctl", "is-active", unit])[1].strip()
            if state == "active" and pid1 not in ("", "0", pid0):
                break
        env = _environ_names(pid1)
        results.append({"unit": unit, "state": state, "restarted": pid1 not in ("", "0", pid0),
                        "orders_armed_before": armed0, "orders_armed_after": _armed(pid1),
                        "env_present": sorted(n for n in expect if env.get(n)),
                        "env_missing": sorted(n for n in expect if not env.get(n)),
                        "env_absent_ok": sorted(n for n in p.get("absent", {}).get(unit, []) if n not in env)})
    return {"step": "restart", "units": results}


# ---- the firewall ---------------------------------------------------------------------------
def step_firewall(p: dict) -> dict:
    ports = [int(x) for x in p["ports"]]
    if not ports or any(x not in (8000, 8001, 8002) for x in ports):
        return {"step": "firewall", "error": "only 8000-8002 are closed here"}
    if not shutil.which("ufw"):
        return {"step": "firewall", "error": "ufw is not installed"}
    status = _run(["ufw", "status"])[1]
    was_active = "Status: active" in status
    done = []
    for argv in (["ufw", "allow", "22/tcp"],):
        done.append((" ".join(argv[1:]), _run(argv)[0]))
    tailscale = os.path.exists("/sys/class/net/tailscale0")
    for port in ports:
        if tailscale:
            argv = ["ufw", "allow", "in", "on", "tailscale0", "to", "any", "port", str(port), "proto", "tcp"]
            done.append((" ".join(argv[1:]), _run(argv)[0]))
        argv = ["ufw", "deny", f"{port}/tcp"]
        done.append((" ".join(argv[1:]), _run(argv)[0]))
    if not was_active:
        # keep every other service reachable exactly as before: only the named ports close
        done.append(("default allow incoming", _run(["ufw", "default", "allow", "incoming"])[0]))
        done.append(("--force enable", _run(["ufw", "--force", "enable"])[0]))
    return {"step": "firewall", "was_active": was_active, "tailscale": tailscale,
            "rules": [{"rule": r, "exit": c} for r, c in done],
            "ok": all(c == 0 for _, c in done)}


STEPS = {"discover": step_discover, "tunnel_user": step_tunnel_user, "deploy": step_deploy,
         "env": step_env, "restart": step_restart, "firewall": step_firewall}


def main() -> None:
    payload = json.load(sys.stdin)
    step = STEPS.get(payload.get("step"))
    if step is None:
        emit({"error": "unknown step"})
        sys.exit(2)
    try:
        emit(step(payload))
    except Exception as error:  # noqa: BLE001 - name the failure, never echo the payload
        emit({"step": payload.get("step"), "error": f"{type(error).__name__}"})
        sys.exit(1)


if __name__ == "__main__":
    main()
