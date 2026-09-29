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
  ping         answers: the client proves a FRESH ssh login still works
  tailscale_install  Tailscale from its official apt repository (Debian/Ubuntu, found in
               /etc/os-release) - no curl|sh; nothing if it is already installed
  tailscale_up / tailscale_status  bring the node up (--ssh=false, a fixed hostname,
               --accept-dns=false) in a one-shot transient unit and report BackendState,
               the login URL Ian clicks (no auth key anywhere), the 100.x address, the name
  fw_prepare   ufw present; the current rules backed up; allow 22/tcp FIRST and 8000-8002
               on tailscale0 - permissions only, nothing is denied or enabled yet
  fw_arm       schedule the automatic revert (a transient systemd timer), THEN deny
               8000-8002 and enable ufw ('default allow incoming' when it was off, so only
               those ports close)
  fw_confirm   called over a FRESH ssh login: cancel the revert
  nginx_disable  take out any enabled nginx site that forwards to 8000-8002 (nginx -t
               first; a failed test puts it back)

Importable for tests: every step is a function of its payload, and every command goes
through _run (tests replace it).
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
PLAIN_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
PROC = "/proc"
ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def emit(obj: dict) -> None:
    print(json.dumps(obj, sort_keys=True))


def _run(argv: list[str], timeout: float = 30, env: dict | None = None) -> tuple[int, str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
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


def fingerprint(value: str) -> str:
    """A short one-way fingerprint of a key, to compare keys without ever showing one."""
    return hashlib.sha256(("pionir-lockdown:" + value).encode("utf-8")).hexdigest()[:16]


def _write_private(path: str, data: bytes, mode: int, uid: int | None, gid: int | None) -> None:
    """Create `path` owner-only from the first byte (O_EXCL, 0600 - never a moment
    readable by others), then give it its final mode and owner."""
    if os.path.lexists(path):
        os.unlink(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.chmod(path, mode & 0o7777)
    if uid is not None and hasattr(os, "chown"):
        os.chown(path, uid, gid)


MIN_DEADLINE_S = 24 * 3600          # a previous key's deadline is never written closer than this
NOW = time.time


def update_env_file(path: str, sets: dict[str, str], keep_previous: dict[str, str],
                    unset: list[str], stamp: str, plain: dict[str, str] | None = None,
                    previous_days: int | None = None) -> dict:
    """Set/unset names in an env file, keeping every other line as it was. The first time
    for this stamp the file is backed up to <path>.bak-pionir-<stamp> (0600, never
    overwritten, so it always holds the content from before this rotation).

    keep_previous {CUR: PREV}: when CUR changes, PREV := the value CUR has NOW - the live
    key, the one the phone holds - always overwriting an older PREV. An empty CUR is
    refused: rotating it would drop the key the phone uses. When CUR already has the new
    value (a re-run) PREV is left as it is. `plain` values are not keys.

    previous_days: each kept previous PREV gets PREV_UNTIL = now + that many days, computed
    HERE on the server's clock when the rotation happens - and recomputed on a re-run
    whenever the deadline on file is missing or less than 24 h ahead, so a run resumed days
    later never writes (or keeps) a deadline that is already past or about to pass."""
    plain = dict(plain or {})
    if not os.path.isfile(path):
        return {"path": path, "error": "missing"}
    for name, value in sets.items():
        if not NAME_RE.fullmatch(name) or not isinstance(value, str) or not VALUE_RE.fullmatch(value):
            return {"path": path, "error": f"refused a malformed value for {name}"}
    for name, value in plain.items():
        if not NAME_RE.fullmatch(name) or not isinstance(value, str) or not PLAIN_RE.fullmatch(value):
            return {"path": path, "error": f"refused a malformed value for {name}"}
    for name in list(unset) + list(keep_previous) + list(keep_previous.values()):
        if not NAME_RE.fullmatch(name):
            return {"path": path, "error": f"refused a malformed name {name!r}"}
    st = os.stat(path)
    with open(path, "rb") as fh:
        raw = fh.read()
    text = raw.decode("utf-8", "replace")
    current = parse_env(text)
    sets = dict(sets)
    for cur, prev in keep_previous.items():
        new = sets.get(cur)
        if new is None or current.get(cur) == new:
            continue                          # not rotating it, or already rotated
        if not current.get(cur):
            return {"path": path, "error": f"{cur} is empty here: refusing to rotate it (the phone's key is unknown)"}
        sets[prev] = current[cur]
    if previous_days is not None:
        if not isinstance(previous_days, int) or not 1 <= previous_days <= 14:
            return {"path": path, "error": "previous_days must be 1-14"}
        now = int(NOW())
        for cur, prev in keep_previous.items():
            has_prev = prev in sets or bool(current.get(prev))
            if not has_prev:
                continue
            name = f"{prev}_UNTIL"
            try:
                on_file = int(current.get(name, ""))
            except ValueError:
                on_file = 0
            if prev in sets or on_file < now + MIN_DEADLINE_S or on_file > now + 14 * 86400:
                sets[name] = str(now + previous_days * 86400)
    sets.update(plain)
    backup = f"{path}.bak-pionir-{stamp}"
    if not os.path.exists(backup):
        _write_private(backup, raw, 0o600, getattr(st, "st_uid", None), getattr(st, "st_gid", None))

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
        _write_private(tmp, ("\n".join(lines) + "\n").encode("utf-8"), st.st_mode,
                       getattr(st, "st_uid", None), getattr(st, "st_gid", None))
        os.replace(tmp, path)
    final = parse_env(_read(path))
    return {"path": path, "backup": backup, "changed": changed, "unset": removed,
            "names": sorted(final),
            "previous_fp": {prev: fingerprint(final[prev]) for prev in keep_previous.values() if final.get(prev)},
            "until": {f"{prev}_UNTIL": final.get(f"{prev}_UNTIL") for prev in keep_previous.values() if final.get(f"{prev}_UNTIL")}}


def step_env(p: dict) -> dict:
    stamp = str(p["stamp"])
    if not re.fullmatch(r"\d{8}T\d{6}Z?", stamp):
        return {"step": "env", "error": "bad stamp"}
    files = [update_env_file(f["path"], f.get("set", {}), f.get("keep_previous", {}),
                             f.get("unset", []), stamp, f.get("plain", {}), f.get("previous_days")) for f in p["files"]]
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
        with open(posixpath.join(PROC, str(int(pid)), "environ"), "rb") as fh:
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


def configured_env(unit: str) -> dict[str, str]:
    """What the unit would start with now: its Environment= lines (drop-ins included, as
    systemctl reports them), then its EnvironmentFiles, which win - systemd's own order."""
    import shlex

    out: dict[str, str] = {}
    try:
        words = shlex.split(_show(unit, "Environment"))
    except ValueError:
        words = []
    for w in words:
        k, sep, v = w.partition("=")
        if sep and k:
            out[k] = v
    for f in env_files_from_show(_show(unit, "EnvironmentFiles")):
        out.update(parse_env(_read(f)))
    return out


def _find_file(root: str, name: str, depth: int = 4) -> str | None:
    if not root.startswith("/"):
        return None
    for d in range(depth + 1):
        hits = sorted(glob.glob(posixpath.join(root, *(["*"] * d), name)))
        if hits:
            return hits[0]
    return None


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
            workdir = _show(unit, "WorkingDirectory")
            if api == "robinhood":
                script = script_from_execstart(_show(unit, "ExecStart"), MARKERS[api], workdir)
                info["script"] = script
                text = _read(script) if script else ""
                info["read_key_support"] = "PRO_RH_READ_KEY" in text
                info["previous_key_support"] = "PRO_RH_API_KEY_PREVIOUS_UNTIL" in text
                info["orders_armed"] = _armed(_show(unit, "MainPID"))
                info["orders_configured"] = configured_env(unit).get("PRO_RH_ORDERS_ENABLED") == "1"
            elif api == "prometheus":
                script = _find_file(workdir, "prometheus/webapp.py") or _find_file(workdir, "webapp.py")
                text = _read(script) if script else ""
                info["script"] = script
                info["read_key_support"] = "PROM_READ_KEY" in text
                info["previous_key_support"] = "PROM_API_KEY_PREVIOUS_UNTIL" in text
            else:
                script = script_from_execstart(_show(unit, "ExecStart"), MARKERS[api], workdir)
                text = _read(script) if script else ""
                info["script"] = script
                info["read_key_support"] = False     # its only key opens GETs only already
                info["previous_key_support"] = "KARKINOS_API_KEY_PREVIOUS_UNTIL" in text
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


SSHD_MAIN = "/etc/ssh/sshd_config"
SSHD_DROPIN = "/etc/ssh/sshd_config.d/60-pionir-tunnel.conf"
BEGIN, END = "# BEGIN pionir-tunnel (tools/vps-lockdown.ps1)", "# END pionir-tunnel"
# What sshd -T must say for the account once the block is in: local forwarding to the
# three ports and nothing else - no remote (-R) listener, no socket forwarding, no X11,
# no tty, no agent, and any session runs /bin/false.
SSHD_WANT = {"allowtcpforwarding": "local", "permitlisten": "none",
             "allowstreamlocalforwarding": "no", "x11forwarding": "no", "permittty": "no",
             "allowagentforwarding": "no", "forcecommand": "/bin/false"}


def sshd_block(user: str, opens: list[str]) -> str:
    return "\n".join([BEGIN, f"Match User {user}",
                      "    AllowTcpForwarding local",
                      "    PermitListen none",
                      "    PermitOpen " + " ".join(opens),
                      "    AllowStreamLocalForwarding no",
                      "    AllowAgentForwarding no",
                      "    X11Forwarding no",
                      "    PermitTTY no",
                      "    ForceCommand /bin/false",
                      END]) + "\n"


def sshd_effective_ok(text: str, opens: list[str]) -> tuple[bool, list[str]]:
    cfg: dict[str, str] = {}
    for line in text.splitlines():
        k, _, v = line.strip().partition(" ")
        cfg.setdefault(k.lower(), v.strip())
    bad = [k for k, v in SSHD_WANT.items() if cfg.get(k, "").lower() != v.lower()]
    if sorted(cfg.get("permitopen", "").split()) != sorted(opens):
        bad.append("permitopen")
    return not bad, bad


def install_sshd_block(user: str, opens: list[str]) -> dict:
    """The server side of the restriction, which authorized_keys cannot express (it has no
    'no remote forwarding' once port-forwarding is on): a Match block for the account -
    in sshd_config.d when sshd_config includes it, else between markers at the END of
    sshd_config. sshd -t first and the effective config (sshd -T -C user=...) must say
    exactly SSHD_WANT, or the old file is put back and nothing is reloaded."""
    root_argv = ["sshd", "-T", "-C", "user=root,host=pionir,addr=127.0.0.1"]
    root_before = _run(root_argv)
    main = _read(SSHD_MAIN)
    use_dropin = bool(re.search(r"(?m)^\s*Include\s+/etc/ssh/sshd_config\.d/\*\.conf\s*$", main))
    target = SSHD_DROPIN if use_dropin else SSHD_MAIN
    before = _read(target) if os.path.exists(target) else None
    block = sshd_block(user, opens)
    if use_dropin:
        new = block
    else:
        stripped = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", "", main, flags=re.S)
        new = stripped.rstrip("\n") + "\n\n" + block
    if before == new:
        changed = False
    else:
        changed = True
        tmp = target + ".pionir-tmp"
        _write_private(tmp, new.encode("utf-8"), 0o644, 0, 0)
        os.replace(tmp, target)

    def put_back() -> None:
        if before is None:
            os.unlink(target)
        else:
            _write_private(target + ".pionir-tmp", before.encode("utf-8"), 0o644, 0, 0)
            os.replace(target + ".pionir-tmp", target)

    if _run(["sshd", "-t"])[0] != 0:
        if changed:
            put_back()
        return {"error": "sshd -t rejected the Match block: put back, nothing reloaded"}
    code, eff = _run(["sshd", "-T", "-C", f"user={user},host=pionir,addr=127.0.0.1"])
    ok, bad = sshd_effective_ok(eff, opens) if code == 0 else (False, ["sshd -T failed"])
    root_after = _run(root_argv)
    if root_before[0] != 0 or root_after != root_before:
        ok, bad = False, bad + ["root's own effective sshd config would change"]
    if not ok:
        if changed:
            put_back()
        return {"error": "sshd would not restrict the account as intended (" + ", ".join(bad) + "): put back, nothing reloaded"}
    if changed:
        if _run(["systemctl", "reload", "ssh"])[0] != 0 and _run(["systemctl", "reload", "sshd"])[0] != 0:
            return {"error": "sshd reload failed (the checked config is in place; reload ssh by hand)"}
    return {"sshd_file": target, "sshd_changed": changed}


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
    sshd = install_sshd_block(user, list(p["opens"]))
    if "error" in sshd:
        return {"step": "tunnel_user", "error": sshd["error"]}
    code, eff = _run(["sshd", "-T", "-C", f"user={user},host=pionir,addr=127.0.0.1"])
    cfg = dict(line_.partition(" ")[::2] for line_ in eff.splitlines())
    allowed = cfg.get("allowusers", "")
    return {"step": "tunnel_user", "user": user, "created": created, "authorized_keys": path,
            "forwarding_allowed": cfg.get("allowtcpforwarding") == "local" if code == 0 else None,
            "allowusers": allowed[:200] or None, **sshd}


# ---- the server file ------------------------------------------------------------------------
def step_deploy(p: dict) -> dict:
    content = base64.b64decode(p["content_b64"])
    if hashlib.sha256(content).hexdigest() != p["sha256"]:
        return {"step": "deploy", "error": "content does not match its sha256"}
    for word in p.get("must_contain", []):
        if word.encode() not in content:
            return {"step": "deploy", "error": f"the new file lacks {word}"}
    path = p.get("path")
    if path and not (str(path).startswith("/") and posixpath.basename(str(path)) == p["name"]):
        if not str(path).replace("\\", "/").endswith("/" + p["name"]):
            return {"step": "deploy", "error": "the target is not the named file"}
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
        running = hashlib.sha256(fh.read()).hexdigest()
    if running == p["sha256"]:
        return {"step": "deploy", "path": path, "deployed": False, "unchanged": True}
    bases = [b for b in p.get("base_sha256", []) if b]
    if bases and running not in bases and not p.get("allow_drift"):
        # the file on the VPS is not the one the reviewed change was made against: putting
        # the new one there would ship every other difference too
        return {"step": "deploy", "error": f"{path} is not the reviewed base version (it has changes the review did not see): nothing replaced", "drift": True}
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
    """p["units"] = {unit: {"keys": [names], "absent": [names], "force": bool}}.

    A unit is restarted when its RUNNING process holds a different value for any of `keys`
    than its env would give it now (or holds an `absent` name, or `force`) - decided by
    comparing values here, never by what changed in one run, and never reported (booleans
    only). Never: a stopped unit is left stopped (try-restart); and a unit whose running
    PRO_RH_ORDERS_ENABLED differs from its configured one is NOT restarted - that restart
    would arm or disarm real-money orders, which is Ian's call, not this script's."""
    results = []
    for unit, want in p["units"].items():
        if not UNIT_RE.fullmatch(unit):
            results.append({"unit": unit, "error": "bad unit"})
            continue
        keys, absent, force = list(want.get("keys", [])), list(want.get("absent", [])), bool(want.get("force"))
        active = _run(["systemctl", "is-active", unit])[1].strip()
        if active != "active":
            results.append({"unit": unit, "state": active or "unknown", "restarted": False, "was_active": False,
                            "note": "not running: left as it is (it reads the new keys when next started)"})
            continue
        pid0 = _show(unit, "MainPID")
        running, conf = _environ_names(pid0), configured_env(unit)
        armed_running = running.get("PRO_RH_ORDERS_ENABLED") == "1"
        armed_conf = conf.get("PRO_RH_ORDERS_ENABLED") == "1"
        stale = force or any(running.get(k) != conf.get(k) for k in keys) or any(k in running for k in absent)
        base = {"unit": unit, "was_active": True, "orders_armed_before": armed_running,
                "orders_armed_configured": armed_conf}
        if armed_running != armed_conf:
            results.append({**base, "state": "active", "restarted": False, "armed_mismatch": True, "stale": stale})
            continue
        if not stale:
            results.append({**base, "state": "active", "restarted": False, "current": True,
                            "orders_armed_after": armed_running, "env_missing": sorted(k for k in keys if not running.get(k)),
                            "env_stale": [], "absent_present": []})
            continue
        _run(["systemctl", "try-restart", unit], timeout=60)
        pid1, state = pid0, ""
        for _ in range(30):
            SLEEP(0.5)
            pid1 = _show(unit, "MainPID")
            state = _run(["systemctl", "is-active", unit])[1].strip()
            if state == "active" and pid1 not in ("", "0", pid0):
                break
        env = _environ_names(pid1)
        conf = configured_env(unit)
        results.append({**base, "state": state, "restarted": state == "active" and pid1 not in ("", "0", pid0),
                        "orders_armed_after": env.get("PRO_RH_ORDERS_ENABLED") == "1" if env else None,
                        "env_missing": sorted(k for k in keys if not env.get(k)),
                        "env_stale": sorted(k for k in keys if env.get(k) != conf.get(k)),
                        "absent_present": sorted(k for k in absent if k in env)})
    return {"step": "restart", "units": results}


def step_restore(p: dict) -> dict:
    """A unit that was running did not come back after its restart: put its files back from
    this stamp's backups and start it again (it was running before we touched it)."""
    stamp, unit = str(p["stamp"]), str(p["unit"])
    if not re.fullmatch(r"\d{8}T\d{6}Z", stamp) or not UNIT_RE.fullmatch(unit):
        return {"step": "restore", "error": "bad stamp or unit"}
    restored = []
    for path in p.get("files", []):
        bak = f"{path}.bak-pionir-{stamp}"
        if os.path.isfile(bak) and os.path.isfile(path):
            st = os.stat(path)
            with open(bak, "rb") as fh:
                data = fh.read()
            tmp = path + ".pionir-tmp"
            _write_private(tmp, data, st.st_mode, getattr(st, "st_uid", None), getattr(st, "st_gid", None))
            os.replace(tmp, path)
            restored.append(path)
    if p.get("was_active", True):
        _run(["systemctl", "restart", unit], timeout=60)
    state = ""
    for _ in range(30):
        SLEEP(0.5)
        state = _run(["systemctl", "is-active", unit])[1].strip()
        if state == "active" or not p.get("was_active", True):
            break
    return {"step": "restore", "unit": unit, "restored": restored, "active": state == "active"}


# ---- D3: is now a safe moment to restart? --------------------------------------------------------
def _http_json(url: str):
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=5) as response:
        return json.loads(response.read(65536))


HTTP_JSON = _http_json


def step_guard(p: dict) -> dict:
    """Busy right now? A trading job running (its service unit active), a timer about to
    fire one (within 15 min), or Prometheus's API with background jobs in flight
    (/api/health jobs_running - unauthenticated, loopback)."""
    busy, due = [], []
    for unit in p.get("services", []):
        if UNIT_RE.fullmatch(unit):
            state = _run(["systemctl", "is-active", unit])[1].strip()
            if state in ("active", "activating", "reloading"):
                busy.append(unit)
    now = NOW()
    for timer in p.get("timers", []):
        if not re.fullmatch(r"[a-z0-9][a-z0-9@_.-]*\.timer", timer):
            continue
        if _run(["systemctl", "is-active", timer])[1].strip() != "active":
            continue
        raw = _run(["systemctl", "show", timer, "-p", "NextElapseUSecRealtime", "--value", "--timestamp=unix"])[1].strip()
        m = re.fullmatch(r"@(\d+)", raw)
        if m and 0 <= int(m.group(1)) - now <= 15 * 60:
            due.append({"timer": timer, "in_s": int(int(m.group(1)) - now)})
    jobs = None
    port = p.get("jobs_port")
    if port:
        try:
            jobs = int(HTTP_JSON(f"http://127.0.0.1:{int(port)}/api/health").get("jobs_running", 0))
        except Exception:  # noqa: BLE001 - not answering: no jobs to lose
            jobs = None
    return {"step": "guard", "busy": busy, "due": due, "jobs_running": jobs,
            "safe": not busy and not due and not jobs}


def step_ping(p: dict) -> dict:
    return {"step": "ping", "ok": True}


# ---- Tailscale ------------------------------------------------------------------------------
OS_RELEASE = "/etc/os-release"
NET_DIR = "/sys/class/net"
TS_KEYRING = "/usr/share/keyrings/tailscale-archive-keyring.gpg"
TS_LIST = "/etc/apt/sources.list.d/tailscale.list"
TS_UP_UNIT = "pionir-tailscale-up"
HOST_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
TS_IP_RE = re.compile(r"100\.\d{1,3}\.\d{1,3}\.\d{1,3}")
SLEEP = time.sleep
WHICH = shutil.which


def _fetch(url: str) -> bytes:
    import urllib.request

    with urllib.request.urlopen(url, timeout=60) as response:   # https, pkgs.tailscale.com only
        return response.read(1_000_000)


def tailscale_repo(os_release_text: str) -> tuple[str, str] | None:
    """(distro, codename) for Tailscale's own apt repository, or None when this is not a
    Debian/Ubuntu it publishes for."""
    osr = parse_env(os_release_text)
    distro, codename = osr.get("ID", ""), osr.get("VERSION_CODENAME", "")
    if distro not in ("ubuntu", "debian") or not re.fullmatch(r"[a-z]{2,20}", codename):
        return None
    return distro, codename


def step_tailscale_install(p: dict) -> dict:
    if WHICH("tailscale"):
        return {"step": "tailscale_install", "installed": True, "already": True,
                "version": _run(["tailscale", "version"])[1].split("\n")[0].strip()}
    repo = tailscale_repo(_read(OS_RELEASE))
    if repo is None:
        return {"step": "tailscale_install", "error": "not a Debian/Ubuntu Tailscale publishes an apt "
                "repository for: install Tailscale by hand (tailscale.com/download), then run again"}
    distro, codename = repo
    base = f"https://pkgs.tailscale.com/stable/{distro}/{codename}"
    key = _fetch(base + ".noarmor.gpg")
    listing = _fetch(base + ".tailscale-keyring.list").decode("utf-8", "replace")
    lines = [ln.strip() for ln in listing.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    want = f"deb [signed-by={TS_KEYRING}] https://pkgs.tailscale.com/stable/{distro} {codename} main"
    if lines != [want] or not key:
        return {"step": "tailscale_install", "error": "the repository files from pkgs.tailscale.com were not what was expected"}
    with open(TS_KEYRING, "wb") as fh:
        fh.write(key)
    os.chmod(TS_KEYRING, 0o644)
    with open(TS_LIST, "w", encoding="ascii", newline="\n") as fh:
        fh.write(want + "\n")
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    if _run(["apt-get", "update"], timeout=600, env=env)[0] != 0:
        return {"step": "tailscale_install", "error": "apt-get update failed"}
    if _run(["apt-get", "install", "-y", "tailscale"], timeout=900, env=env)[0] != 0:
        return {"step": "tailscale_install", "error": "apt-get install tailscale failed"}
    return {"step": "tailscale_install", "installed": True, "already": False, "repo": f"{distro} {codename}",
            "version": _run(["tailscale", "version"])[1].split("\n")[0].strip()}


def tailscale_state() -> dict:
    code, out = _run(["tailscale", "status", "--json"])
    try:
        s = json.loads(out) if out.strip() else {}
    except ValueError:
        s = {}
    me = s.get("Self") or {}
    ips = [ip for ip in (me.get("TailscaleIPs") or []) if TS_IP_RE.fullmatch(str(ip))]
    url = s.get("AuthURL") or None
    if url and not re.fullmatch(r"https://login\.tailscale\.com/[A-Za-z0-9/_-]{1,200}", url):
        url = None                          # only ever show Tailscale's own login link
    mobiles = []
    for peer in (s.get("Peer") or {}).values():
        if str(peer.get("OS", "")).lower() in ("android", "ios"):
            mobiles.append({"name": str(peer.get("HostName") or peer.get("DNSName") or "?")[:64],
                            "os": str(peer.get("OS"))[:16], "key_expiry": peer.get("KeyExpiry") or None})
    return {"state": s.get("BackendState") or ("unknown" if code == 0 else "not running"),
            "key_expiry": me.get("KeyExpiry") or None, "mobiles": mobiles,
            "auth_url": url, "ipv4": ips[0] if ips else None,
            "dns_name": (me.get("DNSName") or "").rstrip(".") or None,
            "hostname": me.get("HostName") or None,
            "interface": os.path.exists(posixpath.join(NET_DIR, "tailscale0"))}


def step_tailscale_status(p: dict) -> dict:
    return {"step": "tailscale_status", **tailscale_state()}


def step_tailscale_up(p: dict) -> dict:
    """Bring the node up, once: `tailscale up` runs in a one-shot transient unit (it waits
    for Ian's login, then exits); nothing is started when the node is already up or already
    waiting for a login. No auth key: Ian clicks the login URL this reports."""
    host = str(p["hostname"])
    if not HOST_RE.fullmatch(host):
        return {"step": "tailscale_up", "error": "bad hostname"}
    st = tailscale_state()
    if st["state"] == "Running" or st["auth_url"]:
        return {"step": "tailscale_up", "started": False, **st}
    running = _run(["systemctl", "is-active", TS_UP_UNIT])[1].strip()
    if running not in ("active", "activating"):
        _run(["systemctl", "reset-failed", TS_UP_UNIT])
        code, _ = _run(["systemd-run", f"--unit={TS_UP_UNIT}", "--collect", "--", "tailscale", "up",
                        "--ssh=false", f"--hostname={host}", "--accept-dns=false"])
        if code != 0:
            return {"step": "tailscale_up", "error": "systemd-run tailscale up failed"}
    for _ in range(30):
        st = tailscale_state()
        if st["state"] == "Running" or st["auth_url"]:
            break
        SLEEP(1)
    return {"step": "tailscale_up", "started": True, **st}


# ---- the firewall, with an automatic revert ----------------------------------------------------
UFW_FILES = ("/etc/ufw/user.rules", "/etc/ufw/user6.rules", "/etc/default/ufw")
BACKUP_ROOT = "/root"
API_RANGE = "8000:8002"
TAILNET_ALLOW = ["allow", "in", "on", "tailscale0", "to", "any", "port", API_RANGE, "proto", "tcp"]
PUBLIC_DENY = ["deny", f"{API_RANGE}/tcp"]
STAMP_RE = re.compile(r"\d{8}T\d{6}Z")
REVERT_UNIT_RE = re.compile(r"pionir-ufw-revert-\d{8}T\d{6}Z")


def _ufw_active() -> bool:
    return "Status: active" in _run(["ufw", "status"])[1]


def _fw_dir(stamp: str) -> str:
    if not STAMP_RE.fullmatch(stamp):
        raise ValueError("bad stamp")
    return posixpath.join(BACKUP_ROOT, f".pionir-ufw-{stamp}")


def rule_order(numbered: str) -> dict:
    """From `ufw status numbered`: where the tailnet allow, the public deny and any other
    rule naming 8000-8002 sit. The tailnet allow must come before the deny, and the deny
    before any other rule for those ports, or the order does not do what it says."""
    pos: dict = {"tailnet_allow": None, "deny": None, "other": []}
    for line in numbered.splitlines():
        m = re.match(r"\s*\[\s*(\d+)\]\s+(.*)$", line)
        if not m or "(v6)" in line:
            continue
        n, rule = int(m.group(1)), m.group(2)
        if not re.search(r"\b800[0-2]\b", rule):
            continue
        if "on tailscale0" in rule and "ALLOW" in rule and pos["tailnet_allow"] is None:
            pos["tailnet_allow"] = n
        elif "DENY" in rule and "8000:8002/tcp" in rule and pos["deny"] is None:
            pos["deny"] = n
        else:
            pos["other"].append(n)
    ta, dn = pos["tailnet_allow"], pos["deny"]
    pos["ok"] = bool(ta and dn and ta < dn and all(o > dn for o in pos["other"]))
    return pos


def step_fw_prepare(p: dict) -> dict:
    """Permissions only - nothing is denied and ufw is not enabled here: back up the rules,
    allow 22/tcp FIRST, then 8000-8002 on tailscale0."""
    bdir = _fw_dir(str(p["stamp"]))
    if not os.path.exists(posixpath.join(NET_DIR, "tailscale0")):
        return {"step": "fw_prepare", "error": "tailscale0 is not up: closing the ports now would cut the phone off"}
    if not WHICH("ufw"):
        env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
        if _run(["apt-get", "install", "-y", "ufw"], timeout=900, env=env)[0] != 0:
            return {"step": "fw_prepare", "error": "ufw is not installed and apt-get install ufw failed"}
    marker = posixpath.join(bdir, "was_active")
    if not os.path.exists(marker):
        os.makedirs(bdir, mode=0o700, exist_ok=True)
        for f in UFW_FILES:
            if os.path.exists(f):
                shutil.copy2(f, posixpath.join(bdir, posixpath.basename(f)))
        with open(marker, "w", encoding="ascii") as fh:
            fh.write("1" if _ufw_active() else "0")
    was_active = _read(marker).strip() == "1"
    rules = []
    for argv in (["ufw", "allow", "22/tcp"], ["ufw", "insert", "1"] + TAILNET_ALLOW):
        rules.append({"rule": " ".join(argv[1:]), "exit": _run(argv)[0]})
    return {"step": "fw_prepare", "backup": bdir, "was_active": was_active, "rules": rules,
            "ok": all(r["exit"] == 0 for r in rules)}


def revert_script(bdir: str, was_active: bool) -> str:
    lines = ["#!/bin/sh", "# Pionir tools/vps-lockdown.ps1: put the firewall back as it was"]
    for f in UFW_FILES:
        lines.append(f'[ -f "{bdir}/{posixpath.basename(f)}" ] && cp -p "{bdir}/{posixpath.basename(f)}" "{f}"')
    lines.append("ufw reload" if was_active else "ufw --force disable")
    return "\n".join(lines) + "\n"


def step_fw_arm(p: dict) -> dict:
    """The revert is scheduled FIRST - if it cannot be, nothing changes. Then the public deny
    goes in right after the tailnet allow, and ufw is enabled if it was off ('default allow
    incoming' first, so every other service stays exactly as reachable as it was)."""
    stamp = str(p["stamp"])
    bdir = _fw_dir(stamp)
    marker = posixpath.join(bdir, "was_active")
    if not os.path.exists(marker):
        return {"step": "fw_arm", "error": "fw_prepare has not run for this stamp"}
    delay = int(p.get("revert_after_s", 300))
    if not 60 <= delay <= 1800:
        return {"step": "fw_arm", "error": "revert_after_s must be 60-1800"}
    was_active = _read(marker).strip() == "1"
    script = posixpath.join(bdir, "revert.sh")
    with open(script, "w", encoding="ascii", newline="\n") as fh:
        fh.write(revert_script(bdir, was_active))
    os.chmod(script, 0o700)
    unit = f"pionir-ufw-revert-{stamp}"
    code, _ = _run(["systemd-run", f"--unit={unit}", f"--on-active={delay}", "/bin/sh", script])
    armed = _run(["systemctl", "is-active", f"{unit}.timer"])[1].strip() == "active"
    if code != 0 or not armed:
        return {"step": "fw_arm", "error": "the automatic revert could not be scheduled: nothing was changed"}
    rules = [{"rule": "insert 2 " + " ".join(PUBLIC_DENY), "exit": _run(["ufw", "insert", "2"] + PUBLIC_DENY)[0]}]
    if not was_active:
        rules.append({"rule": "default allow incoming", "exit": _run(["ufw", "default", "allow", "incoming"])[0]})
        rules.append({"rule": "--force enable", "exit": _run(["ufw", "--force", "enable"])[0]})
    order = rule_order(_run(["ufw", "status", "numbered"])[1])
    return {"step": "fw_arm", "revert_unit": unit, "revert_after_s": delay, "active": _ufw_active(),
            "rules": rules, "order": order, "ok": all(r["exit"] == 0 for r in rules) and order["ok"]}


def step_fw_confirm(p: dict) -> dict:
    """Reached over a FRESH ssh login (the client proves it can still get in): cancel the
    revert. If this is never reached, the timer puts the firewall back by itself."""
    unit = str(p["revert_unit"])
    if not REVERT_UNIT_RE.fullmatch(unit):
        return {"step": "fw_confirm", "error": "bad unit"}
    _run(["systemctl", "stop", f"{unit}.timer"])
    still = _run(["systemctl", "is-active", f"{unit}.timer"])[1].strip() == "active"
    return {"step": "fw_confirm", "cancelled": not still, "active": _ufw_active(),
            "order": rule_order(_run(["ufw", "status", "numbered"])[1])}


# ---- nginx ------------------------------------------------------------------------------------
NGINX_SITES = "/etc/nginx/sites-enabled"
NGINX_TO_API = re.compile(r"proxy_pass\s+https?://(127\.0\.0\.1|localhost|0\.0\.0\.0|\[::1\])?:?800[0-2]\b")


def step_nginx_disable(p: dict) -> dict:
    """Take out every enabled site that forwards to 8000-8002 (moved aside, not deleted);
    nginx -t must pass or they are put back."""
    bdir = posixpath.join(BACKUP_ROOT, f".pionir-nginx-{p['stamp']}")
    if not STAMP_RE.fullmatch(str(p["stamp"])):
        return {"step": "nginx_disable", "error": "bad stamp"}
    sites = [f for f in sorted(g.replace("\\", "/") for g in glob.glob(posixpath.join(NGINX_SITES, "*")))
             if NGINX_TO_API.search(_read(f))]
    if not sites:
        return {"step": "nginx_disable", "disabled": [], "backup": None}
    os.makedirs(bdir, mode=0o700, exist_ok=True)
    moved = []
    for f in sites:
        dest = posixpath.join(bdir, posixpath.basename(f))
        os.rename(f, dest)
        moved.append((f, dest))
    if _run(["nginx", "-t"])[0] != 0:
        for f, dest in moved:
            os.rename(dest, f)
        return {"step": "nginx_disable", "error": "nginx -t failed without those sites: put back, nothing changed"}
    _run(["systemctl", "reload", "nginx"])
    return {"step": "nginx_disable", "disabled": [f for f, _ in moved], "backup": bdir}


STEPS = {"discover": step_discover, "tunnel_user": step_tunnel_user, "deploy": step_deploy,
         "env": step_env, "restart": step_restart, "restore": step_restore, "ping": step_ping,
         "guard": step_guard,
         "tailscale_install": step_tailscale_install, "tailscale_up": step_tailscale_up,
         "tailscale_status": step_tailscale_status, "fw_prepare": step_fw_prepare,
         "fw_arm": step_fw_arm, "fw_confirm": step_fw_confirm, "nginx_disable": step_nginx_disable}


def main() -> None:
    os.umask(0o077)                      # nothing this writes is ever readable by others
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
