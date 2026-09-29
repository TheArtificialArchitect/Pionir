"""Proteus - Ian's trading system - as a Pionir organ: its control plane on the VPS.

The droplet (174.138.35.184, root over ``~/proteus_deploy``) runs:

- **Karkinos** (Mr-Crab, Alpaca PAPER): ``mrcrab-{t1,research,t2,t3}.timer`` fire
  ``mrcrab@<i>.service`` as user mrcrab; its kill switch is ``controls/KILL`` in the repo
  (``/opt/mrcrab/Mr-Crab/controls/KILL``); read API on :8002.
- **Prometheus** (``prometheus-api.service`` on :8001, Alpaca paper; the
  ``prometheus-{scan,entry,review}.timer`` runners install DISABLED). The entry timer BUYS
  on Robinhood - LIVE money. Kill switches: ``HALT`` in its state dir
  (``/root/.pantheon/prometheus/HALT``) and ``ROBINHOOD_KILL`` (``/root/.pantheon/ROBINHOOD_KILL``).
- **Robinhood API** (``pro-robinhood-api.service`` on :8000, LIVE): ``POST /buy /sell
  /liquidate`` are armed only by ``PRO_RH_ORDERS_ENABLED=1`` in the service's environment.

Two kinds of capability, and the line between them is the whole point:

- **Brakes run at once** (auto): read the plane (``proteus.status``, ``proteus.logs``) and
  stop it - drop a kill switch, disable a timer, stop a service, take the Robinhood
  order switch off. Stopping trading is always safe to do without asking.
- **Arming never runs without Ian's yes**: enabling a trading timer, clearing a kill
  switch, turning Robinhood orders on, starting a live-money service, deploying. Each is
  ``spends_money`` (so PRIVILEGED, parked on EVERY call as its own card, never batched,
  never routed) and the adapter refuses to run one unless the task carries the grant only
  ``PionirApp.approve`` adds (``OWNER_APPROVED_GRANT``) - holding ``proteus.arm`` is not
  enough, so no path from Moss, the crew or any client reaches the wire without a human.

This machine reaches the three read APIs ONLY through the SSH tunnel (scripts\\vps-tunnel.ps1,
a pane of the stack: loopback 18000-18002 -> the droplet's 8000-8002, on a restricted key that
can do nothing but forward). ``plane()`` reports it (``tunnel``: each API's unauthenticated
/health through its loopback end); nothing here ever uses the public plain-HTTP ports.
tools\\vps-lockdown.ps1 sets the tunnel up and rotates the keys. Once it has closed 8000-8002
to the internet, the phone reaches the APIs only over the tailnet - so the plane (and doctor,
from the lockdown's record) reports the VPS's Tailscale key expiry and raises an ALARM from 14
days ahead. The brakes here run over ssh :22 and never need the tailnet: an independent STOP
route.

Everything that crosses to the VPS is an argv list (``ssh -i KEY -o BatchMode=yes ...
root@HOST <remote>``) run without a local shell; the remote command is assembled only from
the fixed tables below (unit names, kill-file paths) and integers, never from a request's
text. A payload chooses a KEY in a table - it never supplies a word that is executed.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pionir.adapters._proc import ProcessResult, run_process
from pionir.batching import OWNER_APPROVED_GRANT
from pionir.contracts import AgentManifest, Capability, RiskLevel, Task, TaskResult
from pionir.errors import AdapterProtocolError, AdapterUnavailable

DEFAULT_HOST = "174.138.35.184"
ARM_PERMISSION = "proteus.arm"

_UNIT_RE = re.compile(r"^[a-z0-9][a-z0-9@_.-]*\.(service|timer)$")
_PATH_RE = re.compile(r"^/[A-Za-z0-9_./-]+$")

# ---- the fixed tables: the only words that ever reach the remote shell ----------------------
# Timers that fire trading runs, by system.
KARKINOS_TIMERS = ("mrcrab-t1.timer", "mrcrab-research.timer", "mrcrab-t2.timer",
                   "mrcrab-t3.timer")
PROMETHEUS_TIMERS = ("prometheus-scan.timer", "prometheus-entry.timer", "prometheus-review.timer")
TRADING_TIMERS = KARKINOS_TIMERS + PROMETHEUS_TIMERS
# Long-running services. The Robinhood API is live money; prometheus-api is Pro's app API.
SERVICES = ("prometheus-api.service", "pro-robinhood-api.service")
# What start_service may start (both serve live-money order routes) - never the retired
# proteus.service, which is hard-killed and stays that way.
STARTABLE = SERVICES
# Units whose journal proteus.logs may read.
LOG_UNITS = (
    "mrcrab@t1.service", "mrcrab@research.service", "mrcrab@t2.service", "mrcrab@t3.service",
    "prometheus-api.service", "prometheus-scan.service", "prometheus-entry.service",
    "prometheus-review.service", "pro-robinhood-api.service", "proteus.service",
) + TRADING_TIMERS
# Every unit status reports on.
STATUS_UNITS = SERVICES + TRADING_TIMERS + ("proteus.service",)
# Read APIs on the droplet's loopback: (name, port). /health is unauthenticated and says
# nothing about an account - status reads no key and sends none over the wire.
READ_APIS = (("prometheus", 8001), ("karkinos", 8002), ("robinhood", 8000))
# The SSH tunnel (scripts\vps-tunnel.ps1, a pane of the stack): (name, this machine's
# loopback port, the droplet's loopback port). The ONLY way anything here reaches those
# APIs - never the public plain-HTTP ports. 18000-18002: clear of every estate port (8000
# here is genesis's). tests/test_vps_tunnel.py pins these against the tunnel script.
TUNNEL_PORTS = (("robinhood", 18000, 8000), ("prometheus", 18001, 8001), ("karkinos", 18002, 8002))
TUNNEL_HOST = "127.0.0.1"
RH_UNIT ="pro-robinhood-api.service"
RH_DROPIN = "/etc/systemd/system/pro-robinhood-api.service.d/pionir-orders.conf"
SIGNALS_REMOTE = "/opt/mrcrab/signals.json"
MAX_LOG_LINES = 500
MAX_OUTPUT = 60_000


@dataclass(frozen=True, slots=True)
class ProteusSettings:
    host: str = DEFAULT_HOST
    user: str = "root"
    key_file: Path | None = None          # None: ~/proteus_deploy
    connect_timeout_seconds: int = 15
    timeout_seconds: int = 90
    deploy_timeout_seconds: int = 1200
    # kill switches by system (the paths the bots read on the droplet)
    kill_files: Mapping[str, str] = field(default_factory=lambda: {
        "karkinos": "/opt/mrcrab/Mr-Crab/controls/KILL",
        "prometheus": "/root/.pantheon/prometheus/HALT",
        "robinhood": "/root/.pantheon/ROBINHOOD_KILL",
    })
    # local deploy scripts, by name - run only on an approval
    deploys: Mapping[str, str] = field(default_factory=lambda: {
        "prometheus-droplet": r"C:\src\pantheon\scripts\Deploy-PrometheusToDroplet.ps1",
        "prometheus-running": r"C:\src\pantheon\bots\prometheus\scripts\Deploy-PrometheusRunning.ps1",
    })
    # the local side, read by status
    # what tools\vps-lockdown.ps1 recorded about the VPS on the tailnet (not secret)
    tailnet_file: Path = field(default_factory=lambda: Path.home() / ".pionir" / "config" / "vps-tailnet.json")
    peter_url: str = "http://127.0.0.1:8790"
    peter_signals: Path = Path(r"C:\src\The-Web\data\signals.json")

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9.-]+", self.host) or not re.fullmatch(r"[a-z_][a-z0-9_-]*", self.user):
            raise ValueError("the Proteus host and user must be plain names")
        for name, path in self.kill_files.items():
            if not _PATH_RE.fullmatch(path):
                raise ValueError(f"kill file for {name} must be a plain absolute path")
        if not self.peter_url.startswith("http://127.0.0.1:"):
            raise ValueError("Peter is read on loopback only")

    @property
    def key_path(self) -> Path:
        return self.key_file if self.key_file is not None else Path.home() / "proteus_deploy"


Runner = Callable[[Sequence[str], float], ProcessResult]


def real_runner(argv: Sequence[str], timeout: float) -> ProcessResult:
    """No shell, empty stdin (ssh never reads the server's), bounded output."""
    return run_process(argv, label=argv[0], timeout_seconds=timeout, input_text="",
                       max_output_chars=MAX_OUTPUT * 4)


def ssh_argv(settings: ProteusSettings, remote: str) -> list[str]:
    # BatchMode: never hang on a password/passphrase prompt (Windows OpenSSH falls back to
    # one when the key's ACL is too open - the relay stalled that way for a day once).
    return ["ssh", "-i", str(settings.key_path), "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={settings.connect_timeout_seconds}",
            f"{settings.user}@{settings.host}", remote]


def _unit(name: str, allowed: Sequence[str]) -> str:
    if name not in allowed or not _UNIT_RE.fullmatch(name):
        raise AdapterProtocolError(f"{name!r} is not one of {', '.join(allowed)}")
    return name


def _choice(payload: Mapping[str, Any], key: str, allowed: Sequence[str], *,
            everything: bool = False) -> list[str]:
    raw = payload.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise AdapterProtocolError(f"say which {key}: one of {', '.join(allowed)}"
                                   + (" or all" if everything else ""))
    raw = raw.strip()
    if everything and raw == "all":
        return list(allowed)
    if raw not in allowed:
        raise AdapterProtocolError(f"{raw!r} is not one of {', '.join(allowed)}"
                                   + (" or all" if everything else ""))
    return [raw]


# ---- the remote commands: constants, or built only from the tables above -------------------
def status_script(settings: ProteusSettings) -> str:
    units = " ".join(_unit(u, STATUS_UNITS) for u in STATUS_UNITS)
    kills = " ".join(f"{name}={path}" for name, path in sorted(settings.kill_files.items()))
    apis = " ".join(f"{name}={port}" for name, port in READ_APIS)
    return (
        f"for u in {units}; do printf 'unit %s %s %s\\n' \"$u\" "
        "\"$(systemctl is-active \"$u\" 2>/dev/null)\" \"$(systemctl is-enabled \"$u\" 2>/dev/null)\"; done; "
        f"for k in {kills}; do n=${{k%%=*}}; f=${{k#*=}}; "
        "if [ -e \"$f\" ]; then echo \"kill $n present\"; else echo \"kill $n absent\"; fi; done; "
        f"for a in {apis}; do n=${{a%%=*}}; p=${{a#*=}}; printf 'api %s %s\\n' \"$n\" "
        "\"$(curl -s -o /dev/null -m 5 -w '%{http_code}' http://127.0.0.1:$p/health)\"; done; "
        f"pid=$(systemctl show -p MainPID --value {RH_UNIT} 2>/dev/null); "
        "if [ -n \"$pid\" ] && [ \"$pid\" != 0 ] && [ -r /proc/$pid/environ ]; then "
        "printf 'rh_orders %s\\n' \"$(tr '\\0' '\\n' < /proc/$pid/environ | grep -c '^PRO_RH_ORDERS_ENABLED=1$')\"; "
        "else echo 'rh_orders unknown'; fi; "
        f"if [ -e {RH_DROPIN} ]; then echo 'rh_dropin present'; else echo 'rh_dropin absent'; fi; "
        f"if [ -e {SIGNALS_REMOTE} ]; then printf 'signals_age %s\\n' \"$(( $(date +%s) - $(stat -c %Y {SIGNALS_REMOTE}) ))\"; "
        "else echo 'signals_age none'; fi; "
        # the VPS's Tailscale node key: once the public ports close, the tailnet is the phone's
        # only road to the APIs - an expiring key would cut it (none = expiry disabled)
        "if command -v tailscale >/dev/null 2>&1; then printf 'ts_key_expiry %s\\n' \"$(tailscale status --json 2>/dev/null "
        "| python3 -c 'import json,sys; s=(json.load(sys.stdin).get(\"Self\") or {}); print(s.get(\"KeyExpiry\") or \"none\")' "
        "2>/dev/null || echo unknown)\"; else echo 'ts_key_expiry absent'; fi; "
        "systemctl list-timers --all --no-pager --no-legend 'mrcrab-*' 'prometheus-*' 2>/dev/null | sed 's/^/timer /'"
    )


def logs_command(unit: str, lines: int) -> str:
    return f"journalctl -u {_unit(unit, LOG_UNITS)} -n {int(lines)} --no-pager -o short-iso"


def kill_command(settings: ProteusSettings, systems: Sequence[str]) -> str:
    parts = []
    for name in systems:
        path = settings.kill_files[name]
        parent = path.rsplit("/", 1)[0]
        parts.append(f"mkdir -p {parent} && printf '%s\\n' \"stopped by Pionir $(date -u +%FT%TZ)\" > {path} "
                     f"&& echo 'kill {name} present'")
    return " && ".join(parts)


def clear_kill_command(settings: ProteusSettings, systems: Sequence[str]) -> str:
    return " && ".join(f"rm -f {settings.kill_files[name]} && echo 'kill {name} absent'"
                       for name in systems)


def timers_command(verb: str, timers: Sequence[str]) -> str:
    assert verb in ("disable", "enable")
    names = " ".join(_unit(t, TRADING_TIMERS) for t in timers)
    return f"systemctl {verb} --now {names} && systemctl is-active {names}; true"


def service_command(verb: str, services: Sequence[str]) -> str:
    assert verb in ("stop", "start")
    allowed = SERVICES if verb == "stop" else STARTABLE
    names = " ".join(_unit(s, allowed) for s in services)
    return f"systemctl {verb} {names}; systemctl is-active {names}; true"


def rh_orders_command(on: bool) -> str:
    check = (f"pid=$(systemctl show -p MainPID --value {RH_UNIT}); "
             "if [ -n \"$pid\" ] && [ \"$pid\" != 0 ]; then printf 'rh_orders %s\\n' "
             "\"$(tr '\\0' '\\n' < /proc/$pid/environ | grep -c '^PRO_RH_ORDERS_ENABLED=1$')\"; "
             "else echo 'rh_orders stopped'; fi")
    if on:
        return (f"mkdir -p {RH_DROPIN.rsplit('/', 1)[0]} && "
                f"printf '[Service]\\nEnvironment=PRO_RH_ORDERS_ENABLED=1\\n' > {RH_DROPIN} && "
                f"systemctl daemon-reload && systemctl try-restart {RH_UNIT} && sleep 2; {check}")
    # try-restart: restart only if it is running - taking orders off never STARTS a stopped
    # live-money service.
    return (f"rm -f {RH_DROPIN} && systemctl daemon-reload && systemctl try-restart {RH_UNIT} "
            f"&& sleep 2; {check}")


def parse_status(text: str) -> dict[str, Any]:
    units: dict[str, dict[str, str]] = {}
    kills: dict[str, bool] = {}
    apis: dict[str, str] = {}
    timers: list[str] = []
    out: dict[str, Any] = {"units": units, "kill_switches": kills, "apis": apis, "timers": timers}
    for line in text.splitlines():
        head, _, rest = line.partition(" ")
        rest = rest.strip()
        if head == "unit":
            bits = rest.split()
            if bits and bits[0] in STATUS_UNITS:
                units[bits[0]] = {"active": bits[1] if len(bits) > 1 else "unknown",
                                  "enabled": bits[2] if len(bits) > 2 else "unknown"}
        elif head == "kill":
            name, _, state = rest.partition(" ")
            kills[name] = state == "present"
        elif head == "api":
            name, _, code = rest.partition(" ")
            apis[name] = code or "000"
        elif head == "rh_orders":
            out["rh_orders_armed"] = None if rest in ("unknown", "stopped") else rest.strip() not in ("", "0")
        elif head == "rh_dropin":
            out["rh_orders_dropin"] = rest == "present"
        elif head == "ts_key_expiry":
            out["tailnet_key_expiry"] = rest[:40] or "unknown"
        elif head == "signals_age":
            out["vps_signals_age_s"] = int(rest) if rest.isdigit() else None
        elif head == "timer":
            timers.append(rest[:200])
    return out


# ---- the adapter ------------------------------------------------------------------------------
_BRAKE = RiskLevel.REVERSIBLE_WRITE


def _cap(name: str, description: str, *, arming: bool = False,
         risk: RiskLevel = RiskLevel.READ_ONLY) -> Capability:
    if arming:
        return Capability(name=name, description=description, risk=RiskLevel.PRIVILEGED,
                          required_permissions=frozenset({ARM_PERMISSION}),
                          spends_money=True, routable=False)
    return Capability(name=name, description=description, risk=risk, routable=False)


CAPABILITIES = (
    _cap("proteus.status", "Proteus trading plane: VPS units, timers, kill switches, read APIs, "
         "Robinhood order switch, and Peter and his relay here"),
    _cap("proteus.logs", "the last lines of one allowlisted Proteus unit's journal on the VPS"),
    _cap("proteus.kill", "drop a trading kill switch: Karkinos KILL, Prometheus HALT, "
         "ROBINHOOD_KILL, or all", risk=_BRAKE),
    _cap("proteus.stop_timer", "disable and stop a Karkinos or Prometheus trading timer, or all",
         risk=_BRAKE),
    _cap("proteus.stop_service", "stop the Prometheus API or the Robinhood API service", risk=_BRAKE),
    _cap("proteus.rh_orders_off", "take Robinhood real-money orders off (PRO_RH_ORDERS_ENABLED)",
         risk=_BRAKE),
    _cap("proteus.arm_timer", "ARM a trading timer (prometheus-entry buys on Robinhood with "
         "real money)", arming=True),
    _cap("proteus.clear_kill", "clear a trading kill switch so the bot may trade again", arming=True),
    _cap("proteus.rh_orders_on", "turn Robinhood real-money orders ON (PRO_RH_ORDERS_ENABLED=1)",
         arming=True),
    _cap("proteus.start_service", "start a live-money Proteus service (Robinhood API, Prometheus API)",
         arming=True),
    _cap("proteus.deploy", "deploy Prometheus to the droplet with its own Deploy script", arming=True),
)
ARMING = frozenset(c.name for c in CAPABILITIES if c.spends_money)


class ProteusAdapter:
    """Brakes at once, arming only on the owner's approval. See the module docstring."""

    def __init__(self, settings: ProteusSettings | None = None, *, runner: Runner = real_runner,
                 peter_health: Callable[[], bool | None] | None = None,
                 local_status: Callable[[], Mapping[str, Any]] | None = None,
                 tunnel_health: Callable[[int], str] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.settings = settings or ProteusSettings()
        self._run = runner
        self._peter_health = peter_health or self._read_peter_health
        self._tunnel_health = tunnel_health or read_tunnel_health
        # set by the server once its Ollama gate is up: the gate and relay, as it sees them
        self.local_status = local_status
        self._clock = clock
        self._manifest = AgentManifest(agent_id="proteus", version="pionir/proteus-plane",
                                       capabilities=CAPABILITIES)

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    # ---- the wire
    def _ssh(self, remote: str, *, timeout: float | None = None) -> ProcessResult:
        argv = ssh_argv(self.settings, remote)
        result = self._run(argv, float(timeout or self.settings.timeout_seconds))
        if result.returncode == 255:
            raise AdapterUnavailable(f"ssh to {self.settings.host} failed: "
                                     f"{(result.stderr or '').strip()[-200:]}")
        return result

    def _read_peter_health(self) -> bool | None:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(f"{self.settings.peter_url}/api/health", timeout=3) as response:
                return json.loads(response.read(4096)).get("ok") is True
        except (urllib.error.URLError, OSError, ValueError):
            return False

    # ---- checks, before anything is parked (PionirApp._validation_error)
    def validate(self, task: Task) -> None:
        self._plan(task)

    def _plan(self, task: Task) -> tuple[str, Any]:
        """What this task would do: ("ssh", remote) / ("local", argv) / ("status", None).
        Raises AdapterProtocolError for anything outside the tables."""
        p, s, name = task.payload, self.settings, task.capability
        if name == "proteus.status":
            return "status", None
        if name == "proteus.logs":
            unit = p.get("unit")
            if not isinstance(unit, str):
                raise AdapterProtocolError(f"say which unit: one of {', '.join(LOG_UNITS)}")
            lines = p.get("lines", 80)
            if isinstance(lines, bool) or not isinstance(lines, int) or not 1 <= lines <= MAX_LOG_LINES:
                raise AdapterProtocolError(f"lines must be a whole number from 1 to {MAX_LOG_LINES}")
            return "ssh", logs_command(unit, lines)
        if name == "proteus.kill":
            return "ssh", kill_command(s, _choice(p, "system", sorted(s.kill_files), everything=True))
        if name == "proteus.clear_kill":
            return "ssh", clear_kill_command(s, _choice(p, "system", sorted(s.kill_files)))
        if name == "proteus.stop_timer":
            return "ssh", timers_command("disable", _choice(p, "timer", TRADING_TIMERS, everything=True))
        if name == "proteus.arm_timer":
            return "ssh", timers_command("enable", _choice(p, "timer", TRADING_TIMERS))
        if name == "proteus.stop_service":
            return "ssh", service_command("stop", _choice(p, "service", SERVICES, everything=True))
        if name == "proteus.start_service":
            return "ssh", service_command("start", _choice(p, "service", STARTABLE))
        if name == "proteus.rh_orders_off":
            return "ssh", rh_orders_command(False)
        if name == "proteus.rh_orders_on":
            return "ssh", rh_orders_command(True)
        if name == "proteus.deploy":
            which = _choice(p, "deploy", sorted(s.deploys))[0]
            script = s.deploys[which]
            return "local", ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                             "Bypass", "-File", script]
        raise AdapterProtocolError(f"Proteus has no capability {name}")

    # ---- run
    def execute(self, task: Task) -> TaskResult:
        if task.capability in ARMING and OWNER_APPROVED_GRANT not in task.granted_permissions:
            # Holding proteus.arm is not enough: only PionirApp.approve adds this grant, after
            # the owner said yes on a card. Anything else that reaches here is refused - as a
            # refusal (not a fault), so a burst of them never opens the circuit that the
            # BRAKES share: stopping trading must keep working.
            return self._result(task, {"ok": False, "refused": (
                f"{task.capability} arms live trading: it runs only from the owner's approval")})
        kind, what = self._plan(task)
        if kind == "status":
            return self._result(task, self.plane())
        if kind == "local":
            result = self._run(what, float(self.settings.deploy_timeout_seconds))
            return self._result(task, {"ok": result.returncode == 0, "exit_code": result.returncode,
                                       "output": _tail(result.stdout), "error": _tail(result.stderr, 2000)})
        result = self._ssh(what)
        output: dict[str, Any] = {"ok": result.returncode == 0, "exit_code": result.returncode,
                                  "output": _tail(result.stdout)}
        if result.returncode != 0:
            output["error"] = _tail(result.stderr, 2000)
        if task.capability in ("proteus.rh_orders_on", "proteus.rh_orders_off"):
            armed = parse_status(result.stdout).get("rh_orders_armed")
            output["rh_orders_armed"] = armed
            if task.capability == "proteus.rh_orders_off" and armed:
                # verify, do not assume: the switch is set somewhere Pionir's drop-in does
                # not reach (the unit's own EnvironmentFile wins over a drop-in)
                output["ok"] = False
                output["error"] = ("orders are STILL armed: PRO_RH_ORDERS_ENABLED is set outside "
                                   "Pionir's drop-in. Stop the service (proteus.stop_service "
                                   "service=pro-robinhood-api.service) to take orders away now.")
            if task.capability == "proteus.rh_orders_on" and armed is not True:
                output["ok"] = False
                output["error"] = ("orders are not armed now: the service is stopped (the "
                                   "drop-in is in place, so it arms when next started) or its "
                                   "own EnvironmentFile overrides the drop-in")
        return self._result(task, output)

    def _result(self, task: Task, output: Mapping[str, Any]) -> TaskResult:
        return TaskResult(task_id=task.task_id, agent_id="proteus", output=output,
                          evidence=(f"proteus:{task.capability}",))

    def status(self) -> dict[str, Any]:
        """The health contract ``pionir doctor`` calls on every adapter: offline on purpose
        (no ssh, no port) - doctor runs often and in tests. ``plane()`` is the real read."""
        return {"host": self.settings.host, "key_file_present": self.settings.key_path.exists(),
                "capabilities": len(CAPABILITIES), "arming": sorted(ARMING),
                "tailnet": self._recorded_tailnet()}

    def _recorded_tailnet(self) -> dict[str, Any]:
        """The VPS's tailnet key expiry as the lockdown recorded it (offline; the plane reads
        it live)."""
        try:
            doc = json.loads(self.settings.tailnet_file.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {"state": "not on the tailnet (tools\\vps-lockdown.ps1 -Apply puts it there)", "alarm": False}
        return tailnet_key_view(doc.get("key_expiry") or "none", self._clock(), recorded=True)

    def plane(self) -> dict[str, Any]:
        """The whole plane in one read: one ssh round trip plus the local side."""
        doc: dict[str, Any] = {"host": self.settings.host,
                               "checked_at": _iso(self._clock())}
        try:
            result = self._ssh(status_script(self.settings))
            doc["vps"] = {"ok": result.returncode == 0, **parse_status(result.stdout)}
            if result.returncode != 0:
                doc["vps"]["error"] = _tail(result.stderr, 400)
        except (AdapterUnavailable, AdapterProtocolError) as error:
            doc["vps"] = {"ok": False, "error": str(error)[:400]}
        doc["peter"] = {"healthy": self._peter_health(), "url": self.settings.peter_url,
                        "signals_age_s": _age(self.settings.peter_signals, self._clock())}
        doc["tunnel"] = self.tunnel()
        live = (doc.get("vps") or {}).get("tailnet_key_expiry")
        doc["tailnet"] = (tailnet_key_view(live, self._clock()) if live not in (None, "unknown")
                          else self._recorded_tailnet())
        if self.local_status is not None:
            try:
                doc.update(dict(self.local_status()))
            except Exception as error:  # noqa: BLE001 - the local side must not sink the read
                doc["local_error"] = f"{type(error).__name__}: {error}"[:200]
        doc["controls"] = [{"capability": c.name, "arming": c.name in ARMING,
                            "description": c.description} for c in CAPABILITIES]
        return doc

    def tunnel(self) -> dict[str, Any]:
        """The SSH tunnel as this machine sees it: each API's unauthenticated /health through
        its loopback port (no key is read or sent). "down" means that port does not answer -
        the tunnel pane is not up; nothing ever falls back to the public port."""
        apis: dict[str, Any] = {}
        for name, local, remote in TUNNEL_PORTS:
            try:
                code = str(self._tunnel_health(local))
            except Exception as error:  # noqa: BLE001 - a probe must not sink the plane read
                code = f"down ({type(error).__name__})"
            apis[name] = {"local": f"{TUNNEL_HOST}:{local}", "remote_port": remote, "health": code}
        up = all(a["health"] == "200" for a in apis.values())
        state = "up" if up else ("down" if all(a["health"].startswith("down") for a in apis.values())
                                 else "partial")
        return {"state": state, "apis": apis}


TAILNET_ALARM_DAYS = 14
STOP_ROUTE_NOTE = ("Pionir's brakes (proteus.kill, stop_timer, stop_service, rh_orders_off) go over ssh :22 - "
                   "an independent STOP route that does not need the tailnet")


def tailnet_key_view(expiry: str | None, now: float, *, recorded: bool = False) -> dict[str, Any]:
    """The VPS's Tailscale node-key expiry, judged: 'none' is expiry disabled (as it must be
    once the tailnet is the phone's only road); a date is a warning, and an ALARM from
    TAILNET_ALARM_DAYS days ahead."""
    from datetime import datetime

    where = " (as recorded by the lockdown)" if recorded else ""
    if expiry in (None, "", "none"):
        return {"state": "key expiry disabled" + where, "key_expiry": None, "alarm": False}
    if expiry == "absent":
        return {"state": "Tailscale is not installed on the VPS", "key_expiry": None, "alarm": False}
    try:
        when = datetime.fromisoformat(str(expiry).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return {"state": f"key expiry unreadable: {str(expiry)[:40]}", "key_expiry": None, "alarm": True}
    days = int((when - now) // 86400)
    alarm = days <= TAILNET_ALARM_DAYS
    return {"state": ("EXPIRED" if days < 0 else f"key expires in {days} days") + where,
            "key_expiry": str(expiry), "days_left": days, "alarm": alarm,
            "message": ("the VPS's Tailscale key expires " + str(expiry) + ": after the public ports close the "
                        "tailnet is the phone's only road to STOP - disable key expiry for proteus-vps "
                        "(https://login.tailscale.com/admin/machines -> proteus-vps -> ... -> Disable key "
                        "expiry). " + STOP_ROUTE_NOTE + ".")}


def read_tunnel_health(local_port: int) -> str:
    """GET http://127.0.0.1:<local_port>/health through the tunnel: the HTTP status as text,
    or "down" when nothing answers. Loopback only, no proxy, no key, no redirect."""
    if not any(local_port == lp for _, lp, _ in TUNNEL_PORTS):
        raise ValueError(f"{local_port} is not a tunnel port")

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args: Any, **kwargs: Any) -> None:
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(f"http://{TUNNEL_HOST}:{local_port}/health", timeout=4) as response:
            return str(response.status)
    except urllib.error.HTTPError as error:
        return str(error.code)
    except (urllib.error.URLError, OSError):
        return "down"


def _tail(text: str | None, limit: int = MAX_OUTPUT) -> str:
    text = text or ""
    return text if len(text) <= limit else "..." + text[-limit:]


def _iso(ts: float) -> str:
    from datetime import UTC, datetime
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


def _age(path: Path, now: float) -> int | None:
    try:
        return max(0, int(now - path.stat().st_mtime))
    except OSError:
        return None


__all__ = ["ARMING", "ARM_PERMISSION", "CAPABILITIES", "ProteusAdapter", "ProteusSettings",
           "TUNNEL_PORTS", "kill_command", "logs_command", "parse_status", "read_tunnel_health",
           "rh_orders_command", "ssh_argv", "status_script", "timers_command"]
