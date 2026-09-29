"""Proteus - Ian's trading system - as a Pionir organ: its control plane on the VPS.

The droplet (174.138.35.184, root over ``~/proteus_deploy``) runs:

- **Karkinos** (Mr-Crab, Alpaca PAPER): ``mrcrab-{t1,research,t2,t3}.timer`` fire
  ``mrcrab@<i>.service`` as user mrcrab; its kill switch is ``controls/KILL`` in the repo
  (``/opt/mrcrab/Mr-Crab/controls/KILL``); read API on :8002.
- **Prometheus** (``prometheus-api.service`` on :8001, Alpaca paper; the
  ``prometheus-{scan,entry,review}.timer`` runners install DISABLED). The entry timer BUYS
  on Robinhood - LIVE money. Kill switches: ``HALT`` in its state dir
  (``/root/.pantheon/prometheus/HALT``) and ``ROBINHOOD_KILL`` (``/root/.pantheon/ROBINHOOD_KILL``),
  both read by Prometheus's own Python - the system name for the second is
  ``prometheus-robinhood``, because it stops PROMETHEUS's Robinhood entries and nothing else.
- **Robinhood API** (``pro-robinhood-api.service`` on :8000, LIVE): ``POST /buy /sell
  /liquidate`` are armed only by ``PRO_RH_ORDERS_ENABLED=1`` in the service's environment, read
  once at import. It never reads ROBINHOOD_KILL: its brake is ``proteus.rh_orders_off`` (the
  variable unset, the unit restarted, the running process's environment read back).

Two live-money paths, modelled apart: Prometheus -> Robinhood entries (entry timer, HALT,
ROBINHOOD_KILL) and the Robinhood API's own order routes (PRO_RH_ORDERS_ENABLED).

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
RH_UNIT = "pro-robinhood-api.service"
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
        # Prometheus's own Robinhood entries (pantheon brokers/robinhood.py read it). The live
        # Robinhood API (trading-bot-app) never reads it: its brake is rh_orders_off.
        "prometheus-robinhood": "/root/.pantheon/ROBINHOOD_KILL",
    })
    # local deploy scripts, by name - run only on an approval
    deploys: Mapping[str, str] = field(default_factory=lambda: {
        "prometheus-droplet": r"C:\src\pantheon\scripts\Deploy-PrometheusToDroplet.ps1",
        "prometheus-running": r"C:\src\pantheon\bots\prometheus\scripts\Deploy-PrometheusRunning.ps1",
    })
    # the local side, read by status
    peter_url: str = "http://127.0.0.1:8790"
    peter_signals: Path = Path(r"C:\src\The-Web\data\signals.json")
    peter_vault: Path = Path(r"C:\src\The-Web\data\vault.sqlite")

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
# Every command that CHANGES something ends by reading back the state it was meant to leave
# (a `unit`, `kill` or `rh_orders` line per thing touched), and the adapter judges success from
# those lines alone - never from an exit code, and never with a `; true` that hides a failure.
# A brake that cannot prove it braked reports ok:false with the evidence and ssh's stderr.
def unit_report(names: Sequence[str]) -> str:
    return (f"for u in {' '.join(names)}; do printf 'unit %s %s %s\\n' \"$u\" "
            "\"$(systemctl is-active \"$u\" 2>/dev/null)\" \"$(systemctl is-enabled \"$u\" 2>/dev/null)\"; done")


def kill_report(settings: ProteusSettings, systems: Sequence[str]) -> str:
    parts = []
    for name in systems:
        path = settings.kill_files[name]
        parent = path.rsplit("/", 1)[0]
        parts.append(f"if [ -f {path} ]; then echo 'kill {name} present'; "
                     f"elif [ -d {parent} ]; then echo 'kill {name} absent'; "
                     f"else echo 'kill {name} noparent'; fi")
    return "; ".join(parts)


# The Robinhood API reads PRO_RH_ORDERS_ENABLED once, at import, in ITS OWN process. So the only
# true answer is that process's environment: MainPID must be the python running
# robinhood_read_api.py (a wrapper shell without exec would be MainPID and prove nothing -
# then it is "unverifiable", never "off").
RH_CHECK = (
    f"pid=$(systemctl show -p MainPID --value {RH_UNIT} 2>/dev/null); "
    "if [ -z \"$pid\" ] || [ \"$pid\" = 0 ]; then echo 'rh_orders stopped'; "
    "elif ! tr '\\0' ' ' < /proc/$pid/cmdline 2>/dev/null | grep -q 'python.*robinhood_read_api\\.py'; then "
    "echo \"rh_orders unverifiable: MainPID $pid is not the python robinhood_read_api.py process\"; "
    "elif [ ! -r /proc/$pid/environ ]; then echo \"rh_orders unverifiable: /proc/$pid/environ is not readable\"; "
    "else printf 'rh_orders %s\\n' \"$(tr '\\0' '\\n' < /proc/$pid/environ | grep -c '^PRO_RH_ORDERS_ENABLED=1$')\"; fi"
)
RH_DROPIN_REPORT = (
    f"if grep -qs '^UnsetEnvironment=PRO_RH_ORDERS_ENABLED$' {RH_DROPIN}; then echo 'rh_dropin off'; "
    f"elif grep -qs '^Environment=PRO_RH_ORDERS_ENABLED=1$' {RH_DROPIN}; then echo 'rh_dropin on'; "
    "else echo 'rh_dropin absent'; fi"
)


def status_script(settings: ProteusSettings) -> str:
    units = [_unit(u, STATUS_UNITS) for u in STATUS_UNITS]
    apis = " ".join(f"{name}={port}" for name, port in READ_APIS)
    return (
        f"{unit_report(units)}; "
        f"{kill_report(settings, sorted(settings.kill_files))}; "
        f"for a in {apis}; do n=${{a%%=*}}; p=${{a#*=}}; printf 'api %s %s\\n' \"$n\" "
        "\"$(curl -s -o /dev/null -m 5 -w '%{http_code}' http://127.0.0.1:$p/health)\"; done; "
        f"{RH_CHECK}; {RH_DROPIN_REPORT}; "
        f"if [ -e {SIGNALS_REMOTE} ]; then printf 'signals_age %s\\n' \"$(( $(date +%s) - $(stat -c %Y {SIGNALS_REMOTE}) ))\"; "
        "else echo 'signals_age none'; fi; "
        "systemctl list-timers --all --no-pager --no-legend 'mrcrab-*' 'prometheus-*' 2>/dev/null | sed 's/^/timer /'"
    )


def logs_command(unit: str, lines: int) -> str:
    return f"journalctl -u {_unit(unit, LOG_UNITS)} -n {int(lines)} --no-pager -o short-iso"


def kill_command(settings: ProteusSettings, systems: Sequence[str]) -> str:
    # The parent must already exist: creating it (mkdir -p) would turn a WRONG path into a
    # file nobody reads and a brake that "worked". Each switch is tried even if one fails.
    parts = []
    for name in systems:
        path = settings.kill_files[name]
        parent = path.rsplit("/", 1)[0]
        parts.append(f"if [ -d {parent} ]; then printf '%s\\n' \"stopped by Pionir $(date -u +%FT%TZ)\" > {path}; fi")
    return "; ".join(parts) + "; " + kill_report(settings, systems)


def clear_kill_command(settings: ProteusSettings, systems: Sequence[str]) -> str:
    return "; ".join(f"rm -f {settings.kill_files[name]}" for name in systems) + "; " + kill_report(settings, systems)


def timers_command(verb: str, timers: Sequence[str]) -> str:
    assert verb in ("disable", "enable")
    names = [_unit(t, TRADING_TIMERS) for t in timers]
    # one at a time: a unit that fails must not keep the others from being stopped
    return f"for t in {' '.join(names)}; do systemctl {verb} --now \"$t\"; done; {unit_report(names)}"


def service_command(verb: str, services: Sequence[str]) -> str:
    assert verb in ("stop", "start")
    allowed = SERVICES if verb == "stop" else STARTABLE
    names = [_unit(s, allowed) for s in services]
    return f"for s in {' '.join(names)}; do systemctl {verb} \"$s\"; done; {unit_report(names)}"


def rh_orders_command(on: bool) -> str:
    # One drop-in, either way. ON sets the variable; OFF uses UnsetEnvironment=, which systemd
    # applies AFTER Environment= and EnvironmentFile= - so it wins even over a unit whose own
    # EnvironmentFile arms it. Then the unit restarts (try-restart: never STARTS a stopped
    # live-money service) and the running process's own environment is read back.
    line = "Environment=PRO_RH_ORDERS_ENABLED=1" if on else "UnsetEnvironment=PRO_RH_ORDERS_ENABLED"
    return (f"mkdir -p {RH_DROPIN.rsplit('/', 1)[0]} && "
            f"printf '[Service]\\n{line}\\n' > {RH_DROPIN} && systemctl daemon-reload && "
            f"systemctl try-restart {RH_UNIT}; sleep 3; {RH_CHECK}; {RH_DROPIN_REPORT}")


def parse_status(text: str) -> dict[str, Any]:
    units: dict[str, dict[str, str]] = {}
    kills: dict[str, bool | None] = {}
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
            # noparent: the switch's folder is missing - a wrong path, never "off"
            kills[name] = True if state == "present" else False if state == "absent" else None
            if state != "present" and state != "absent":
                out.setdefault("kill_errors", []).append(f"{name}: its folder does not exist")
        elif head == "api":
            name, _, code = rest.partition(" ")
            apis[name] = code or "000"
        elif head == "rh_orders":
            if rest == "stopped":
                out["rh_orders_armed"] = False
                out["rh_orders_note"] = "the Robinhood API is not running: no order can be placed"
            elif rest in ("0", "1"):
                out["rh_orders_armed"] = rest == "1"
            else:
                out["rh_orders_armed"] = None
                out["rh_orders_note"] = rest[:200] or "unknown"
        elif head == "rh_dropin":
            out["rh_orders_dropin"] = rest
        elif head == "signals_age":
            out["vps_signals_age_s"] = int(rest) if rest.isdigit() else None
        elif head == "timer":
            timers.append(rest[:200])
    return out


def verify(capability: str, targets: Sequence[str], text: str) -> tuple[bool, list[str]]:
    """Did the change take? Judged ONLY from the state lines the command read back.
    Returns (ok, mismatches) - every target not left as asked is a mismatch."""
    doc = parse_status(text)
    units, kills = doc["units"], doc["kill_switches"]
    bad: list[str] = []
    for target in targets:
        if capability in ("proteus.kill", "proteus.clear_kill"):
            want = capability == "proteus.kill"
            if target not in kills:
                bad.append(f"{target} kill switch: nothing was read back")
            elif kills[target] is None:
                bad.append(f"{target} kill switch: its folder does not exist ({'not written' if want else 'unknown'})")
            elif kills[target] is not want:
                bad.append(f"{target} kill switch: wanted {'present' if want else 'absent'}, "
                           f"read {'present' if kills[target] else 'absent'}")
            continue
        u = units.get(target)
        if u is None:
            bad.append(f"{target}: no state was read back")
            continue
        active, enabled = u["active"], u["enabled"]
        if capability in ("proteus.stop_timer", "proteus.stop_service"):
            if active not in ("inactive", "failed"):
                bad.append(f"{target}: still {active}")
            if capability == "proteus.stop_timer" and enabled.startswith("enabled"):
                bad.append(f"{target}: still {enabled} (it would come back after a reboot)")
        elif capability in ("proteus.arm_timer", "proteus.start_service"):
            if active != "active":
                bad.append(f"{target}: {active}, not active")
            if capability == "proteus.arm_timer" and enabled != "enabled":
                bad.append(f"{target}: {enabled}, not enabled")
    armed = doc.get("rh_orders_armed")
    if capability == "proteus.rh_orders_off" and armed is not False:
        bad.append("Robinhood orders: " + ("STILL ARMED in the running process" if armed
                                          else f"cannot verify - {doc.get('rh_orders_note', 'no report')}"))
    if capability == "proteus.rh_orders_on" and armed is not True:
        bad.append("Robinhood orders: not armed in a running process - "
                   + str(doc.get("rh_orders_note") or "the running process does not have it set"))
    return (not bad), bad


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
         "Prometheus's ROBINHOOD_KILL, or all (the Robinhood API's brake is rh_orders_off)",
         risk=_BRAKE),
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
                 clock: Callable[[], float] = time.time) -> None:
        self.settings = settings or ProteusSettings()
        self._run = runner
        self._peter_health = peter_health or self._read_peter_health
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

    def _plan(self, task: Task) -> tuple[str, Any, list[str]]:
        """What this task would do: ("ssh", remote, targets) / ("local", argv, []) /
        ("status", None, []). ``targets`` are what the read-back must show changed.
        Raises AdapterProtocolError for anything outside the tables."""
        p, s, name = task.payload, self.settings, task.capability
        if name == "proteus.status":
            return "status", None, []
        if name == "proteus.logs":
            unit = p.get("unit")
            if not isinstance(unit, str):
                raise AdapterProtocolError(f"say which unit: one of {', '.join(LOG_UNITS)}")
            lines = p.get("lines", 80)
            if isinstance(lines, bool) or not isinstance(lines, int) or not 1 <= lines <= MAX_LOG_LINES:
                raise AdapterProtocolError(f"lines must be a whole number from 1 to {MAX_LOG_LINES}")
            return "ssh", logs_command(unit, lines), []
        if name == "proteus.kill":
            systems = _choice(p, "system", sorted(s.kill_files), everything=True)
            return "ssh", kill_command(s, systems), systems
        if name == "proteus.clear_kill":
            systems = _choice(p, "system", sorted(s.kill_files))
            return "ssh", clear_kill_command(s, systems), systems
        if name == "proteus.stop_timer":
            timers = _choice(p, "timer", TRADING_TIMERS, everything=True)
            return "ssh", timers_command("disable", timers), timers
        if name == "proteus.arm_timer":
            timers = _choice(p, "timer", TRADING_TIMERS)
            return "ssh", timers_command("enable", timers), timers
        if name == "proteus.stop_service":
            services = _choice(p, "service", SERVICES, everything=True)
            return "ssh", service_command("stop", services), services
        if name == "proteus.start_service":
            services = _choice(p, "service", STARTABLE)
            return "ssh", service_command("start", services), services
        if name == "proteus.rh_orders_off":
            return "ssh", rh_orders_command(False), []
        if name == "proteus.rh_orders_on":
            return "ssh", rh_orders_command(True), []
        if name == "proteus.deploy":
            which = _choice(p, "deploy", sorted(s.deploys))[0]
            script = s.deploys[which]
            return "local", ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                             "Bypass", "-File", script], []
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
        kind, what, targets = self._plan(task)
        if kind == "status":
            return self._result(task, self.plane())
        if kind == "local":
            result = self._run(what, float(self.settings.deploy_timeout_seconds))
            return self._result(task, {"ok": result.returncode == 0, "exit_code": result.returncode,
                                       "output": _tail(result.stdout), "stderr": _tail(result.stderr, 2000)})
        result = self._ssh(what)
        output: dict[str, Any] = {"exit_code": result.returncode, "output": _tail(result.stdout),
                                  "stderr": _tail(result.stderr, 2000)}
        if task.capability == "proteus.logs":
            output["ok"] = result.returncode == 0
            return self._result(task, output)
        # A change is ok only when the state it read back says so - the exit code is kept as
        # evidence, never as the verdict.
        ok, mismatches = verify(task.capability, targets, result.stdout)
        output["ok"] = ok
        if task.capability in ("proteus.rh_orders_on", "proteus.rh_orders_off"):
            doc = parse_status(result.stdout)
            output["rh_orders_armed"] = doc.get("rh_orders_armed")
            if doc.get("rh_orders_note"):
                output["rh_orders_note"] = doc["rh_orders_note"]
        if not ok:
            output["error"] = "NOT VERIFIED: " + "; ".join(mismatches)
            if task.capability == "proteus.rh_orders_off":
                output["error"] += (" - to take orders away now, stop the service "
                                    "(proteus.stop_service service=pro-robinhood-api.service)")
        return self._result(task, output)

    def _result(self, task: Task, output: Mapping[str, Any]) -> TaskResult:
        return TaskResult(task_id=task.task_id, agent_id="proteus", output=output,
                          evidence=(f"proteus:{task.capability}",))

    def status(self) -> dict[str, Any]:
        """The health contract ``pionir doctor`` calls on every adapter: offline on purpose
        (no ssh, no port) - doctor runs often and in tests. ``plane()`` is the real read."""
        return {"host": self.settings.host, "key_file_present": self.settings.key_path.exists(),
                "capabilities": len(CAPABILITIES), "arming": sorted(ARMING)}

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
                        "signals_age_s": _age(self.settings.peter_signals, self._clock()),
                        **peter_facts(self.settings.peter_signals, self.settings.peter_vault)}
        if self.local_status is not None:
            try:
                doc.update(dict(self.local_status()))
            except Exception as error:  # noqa: BLE001 - the local side must not sink the read
                doc["local_error"] = f"{type(error).__name__}: {error}"[:200]
        doc["controls"] = [{"capability": c.name, "arming": c.name in ARMING,
                            "description": c.description} for c in CAPABILITIES]
        return doc


def peter_facts(signals: Path, vault: Path) -> dict[str, Any]:
    """What The Web (Peter) last produced, read from his own files - never his app (its
    /api/state rebuilds every brief and is far too heavy to poll):

    - ``last_cycle_at``: the ``as_of`` of data/signals.json, written at the end of each
      collect/journal/P&L cycle;
    - ``signals``: how many subjects carry derived signals, how many a news read calls
      bullish or bearish, and the newest such read (subject, direction, conviction, summary);
    - ``pnl``: each brain's newest PAPER book mark from the vault's ledger, opened read-only.

    Each part fails soft: a missing or unreadable file leaves that part out, with a reason."""
    out: dict[str, Any] = {}
    try:
        doc = json.loads(signals.read_text(encoding="utf-8"))
        subjects = {**(doc.get("equities") or {}), **(doc.get("crypto") or {})}
        directional = 0
        newest: dict[str, Any] | None = None
        for name, block in subjects.items():
            news = block.get("news") if isinstance(block, dict) else None
            if not isinstance(news, dict) or news.get("direction") not in ("bullish", "bearish"):
                continue
            directional += 1
            at = str(news.get("observed_at") or news.get("valid_at") or "")
            if newest is None or at > newest["at"]:
                newest = {"subject": name, "direction": news["direction"],
                          "conviction": news.get("conviction"), "at": at,
                          "summary": str(news.get("summary") or "")[:200]}
        out["last_cycle_at"] = doc.get("as_of")
        out["signals"] = {"subjects": len(subjects), "equities": len(doc.get("equities") or {}),
                          "crypto": len(doc.get("crypto") or {}), "directional": directional,
                          "latest": newest}
    except (OSError, ValueError, AttributeError) as error:
        out["signals_error"] = f"{type(error).__name__}: {error}"[:200]
    try:
        import sqlite3

        uri = "file:" + vault.as_posix() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            rows = con.execute(
                "SELECT p.brain_id, p.at, p.equity, p.positions, b.seeded FROM pnl p "
                "LEFT JOIN books b ON b.brain_id = p.brain_id "
                "WHERE p.id IN (SELECT max(id) FROM pnl GROUP BY brain_id) ORDER BY p.brain_id"
            ).fetchall()
        finally:
            con.close()
        books = []
        for brain, at, equity, positions, seeded in rows:
            eq, seed = float(equity), float(seeded or 0) or None
            books.append({"brain": brain, "at": at, "equity": round(eq, 2), "positions": int(positions),
                          "return_pct": round((eq - seed) / seed * 100, 2) if seed else None})
        out["pnl"] = books
    except Exception as error:  # noqa: BLE001 - a locked or missing vault is shown, never raised
        out["pnl_error"] = f"{type(error).__name__}: {error}"[:200]
    return out


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
           "kill_command", "logs_command", "parse_status", "rh_orders_command", "ssh_argv",
           "status_script", "timers_command"]
