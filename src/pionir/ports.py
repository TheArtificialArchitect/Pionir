"""The port registry (ports.json) and a read-only audit of what is really on each port.

One list, read by the launcher's drift tests, Pionir Desktop's tests, and `pionir doctor`.
The audit answers per port: free / ours and healthy / ours but not answering / warming /
a foreign program (named). It reads listeners and their command lines with PowerShell CIM -
psutil from another shell cannot read the command line of a PowerShell-detached process
(brain 3.22) - and probes the CAPABILITY, not just the port (brain 3.20). It never kills,
starts or changes anything.
"""
from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REGISTRY_PATH = Path(__file__).with_name("ports.json")

FREE = "free"
OURS_HEALTHY = "ours-healthy"
OURS_UNHEALTHY = "ours-unhealthy"
WARMING = "warming"
FOREIGN = "foreign"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Service:
    id: str
    label: str
    ports: tuple[int, ...]
    launchers: tuple[str, ...]
    reserved: bool
    match: re.Pattern[str]
    probe_kind: str
    probe_path: str
    slow_start: bool
    scan: int


@dataclass(frozen=True)
class Listener:
    port: int
    pid: int
    name: str
    command: str


def load_registry(path: Path = REGISTRY_PATH) -> list[Service]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    services = []
    for item in raw["services"]:
        probe = item.get("probe") or {"kind": "tcp"}
        services.append(Service(
            id=item["id"], label=item["label"], ports=tuple(item["ports"]),
            launchers=tuple(item.get("launchers", ())), reserved=bool(item.get("reserved")),
            match=re.compile(item["match"], re.IGNORECASE),
            probe_kind=probe["kind"], probe_path=probe.get("path", "/"),
            slow_start=bool(item.get("slow_start")), scan=int(item.get("scan", 0))))
    return services


def validate(path: Path = REGISTRY_PATH) -> list[str]:
    """Every way the registry can contradict itself. Empty means consistent."""
    problems: list[str] = []
    raw = json.loads(path.read_text(encoding="utf-8"))
    ids: dict[str, int] = {}
    owner: dict[int, str] = {}
    scans: list[tuple[str, int, int]] = []
    for item in raw["services"]:
        sid = item.get("id", "?")
        ids[sid] = ids.get(sid, 0) + 1
        for key in ("label", "ports", "match", "probe"):
            if key not in item:
                problems.append(f"{sid}: missing '{key}'")
        for port in item.get("ports", []):
            if not isinstance(port, int) or not 1024 <= port <= 65535:
                problems.append(f"{sid}: port {port!r} is not an integer in 1024-65535")
            elif port in owner:
                problems.append(f"port {port} is claimed by both {owner[port]} and {sid}")
            else:
                owner[port] = sid
        if item.get("scan") and item.get("ports"):
            scans.append((sid, item["ports"][0], item["ports"][0] + int(item["scan"])))
        try:
            re.compile(item.get("match", ""))
        except re.error as error:
            problems.append(f"{sid}: match is not a regex ({error})")
        kind = (item.get("probe") or {}).get("kind")
        if kind not in ("http", "tcp"):
            problems.append(f"{sid}: probe kind {kind!r} is neither http nor tcp")
    problems += [f"id {sid} appears {n} times" for sid, n in ids.items() if n > 1]
    for i, (a, a0, a1) in enumerate(scans):
        for b, b0, b1 in scans[i + 1:]:
            if a0 < b1 and b0 < a1:
                problems.append(f"{a}'s scan range {a0}-{a1 - 1} overlaps {b}'s {b0}-{b1 - 1}")
    return problems


def reserved_ports(services: Iterable[Service] | None = None) -> list[int]:
    return sorted(p for s in (services or load_registry()) if s.reserved for p in s.ports)


_LISTENERS_SCRIPT = (
    "$ErrorActionPreference='SilentlyContinue';"
    "$p=@{};Get-CimInstance Win32_Process|ForEach-Object{$p[[int]$_.ProcessId]=$_};"
    "@(Get-NetTCPConnection -State Listen|ForEach-Object{"
    "$q=$p[[int]$_.OwningProcess];"
    "[pscustomobject]@{port=$_.LocalPort;pid=$_.OwningProcess;name=$q.Name;"
    "command=(([string]$q.ExecutablePath)+' '+([string]$q.CommandLine))}"
    "})|ConvertTo-Json -Compress")


def read_listeners(run: Callable[..., Any] = subprocess.run) -> list[Listener] | None:
    """Every TCP listener with its owner's command line, or None where that cannot be read
    (not Windows, no PowerShell) - unknown, never 'nothing is listening'."""
    try:
        done = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _LISTENERS_SCRIPT],
                   capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(done, "returncode", 1) != 0:
        return None
    text = (done.stdout or "").strip()
    if not text:
        return []
    data = json.loads(text)
    rows = data if isinstance(data, list) else [data]
    return [Listener(int(r["port"]), int(r["pid"] or 0), str(r.get("name") or ""),
                     str(r.get("command") or "").strip()) for r in rows]


def process_running(service: Service,
                    run: Callable[..., Any] = subprocess.run) -> bool:
    """Is a process matching this service's command line alive, listening or not? A slow
    starter that is alive but not yet listening is warming, not DOWN."""
    script = ("Get-CimInstance Win32_Process|Where-Object{(([string]$_.ExecutablePath)+' '+"
              "([string]$_.CommandLine)) -match $env:PIONIR_PORTS_MATCH -and "
              "$_.ProcessId -ne $PID}|Select-Object -First 1 -ExpandProperty ProcessId")
    env = {**__import__("os").environ, "PIONIR_PORTS_MATCH": service.match.pattern}
    try:
        done = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                   capture_output=True, text=True, timeout=30, env=env)
    except (OSError, subprocess.SubprocessError):
        return False
    return getattr(done, "returncode", 1) == 0 and bool((done.stdout or "").strip())


def http_answers(port: int, path: str, timeout: float = 2.0) -> bool:
    """Does something speak HTTP here? Any status below 500 counts (a 401 is an answer)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout):
            return True
    except urllib.error.HTTPError as error:
        return error.code < 500
    except (OSError, ValueError):
        return False


def audit(services: list[Service] | None = None, *,
          listeners: list[Listener] | None | str = "read",
          probe: Callable[[int, str], bool] = http_answers,
          warming: Callable[[Service], bool] | None = None) -> list[dict[str, Any]]:
    """One row per registered port. `listeners='read'` reads the machine; a list injects one
    (tests); None means unreadable, and every listening port is then `unknown`."""
    services = services if services is not None else load_registry()
    if listeners == "read":
        listeners = read_listeners()
        if warming is None:
            warming = process_running
    rows: list[dict[str, Any]] = []
    for service in services:
        for port in service.ports:
            row: dict[str, Any] = {"id": service.id, "label": service.label, "port": port,
                                   "launchers": list(service.launchers)}
            if listeners is None:
                row.update(state=UNKNOWN, detail="listeners could not be read here")
                rows.append(row)
                continue
            holders = [l for l in listeners if l.port == port]
            if not holders:
                if service.slow_start and warming is not None and warming(service):
                    row.update(state=WARMING, detail="starting; this port opens after a model warm-up")
                else:
                    row.update(state=FREE)
                rows.append(row)
                continue
            own = [h for h in holders if service.match.search(h.command)]
            if not own:
                who = holders[0]
                row.update(state=FOREIGN, pid=who.pid, process=who.name or "unknown",
                           detail=f"held by {who.name or 'an unidentified program'} (pid {who.pid}), "
                                  f"which does not look like {service.label}")
                rows.append(row)
                continue
            answering = (probe(port, service.probe_path) if service.probe_kind == "http"
                         else True)
            row.update(pid=own[0].pid, process=own[0].name,
                       state=OURS_HEALTHY if answering else OURS_UNHEALTHY)
            if not answering:
                row["detail"] = f"listening, but {service.probe_path} does not answer"
            rows.append(row)
    return rows


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Doctor's view: the problems first, counts after."""
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    problems = [r for r in rows if r["state"] in (FOREIGN, OURS_UNHEALTHY)]
    tunnel = [r for r in rows if r["id"] == "tunnel"]
    alerts = [f"PORTS: {r['label']} :{r['port']} - {r['detail']}" for r in problems]
    if any(r["state"] == FOREIGN for r in tunnel):
        alerts.append("PORTS: a program other than the VPS tunnel holds a tunnel port - the "
                      "money feeds will say 'tunnel down' until it is freed")
    return {"counts": counts, "alerts": alerts, "ports": rows}
