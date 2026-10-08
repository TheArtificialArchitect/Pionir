"""``pionir doctor --fix``: the problems doctor finds that have a SAFE, reversible fix.

Doctor only reported. This applies a fix where the fix is mechanical, reversible and needs
nothing of the owner's - and for everything else prints the exact PowerShell for Ian (with
its ``cd``), never doing it itself:

- a stale ``.git/index.lock`` (0 bytes, older than ``LOCK_MINUTES``, and no git process
  running anywhere) - git refuses every commit while it is there, which is one way a build
  "committed nothing". It is MOVED ASIDE (``index.lock.stale-<time>``), never deleted.
- Bryo's ``state/bryo.pid`` naming a process that is gone or is not Bryo (a hard kill
  leaves it; a recycled pid then refuses every new Bryo as "another Bryo is alive"). Moved
  aside the same way.
- pionir.ps1's stop marker left behind (no ``-Stop`` running, older than ``MARKER_MINUTES``):
  while it is there a crashed pane closes instead of being restarted. Removed (the next
  launch removes it anyway).
- a state folder the stack expects that is missing: created (empty; removing it undoes it).
- a dead pane while the rest of the pionir.ps1 stack is up: the launcher's own mechanism -
  ``pionir.ps1 -NoBrowser`` with a ``-NoX`` flag for every group that is NOT dead, so it
  starts only what died - then verified by PORT, not by counting processes.

Never fixed here (a command for Ian instead): anything that needs administrator rights (the
build sandbox setup), money, a secret, or a decision (a foreign program on our port, a stack
Pionir Desktop owns, a bridge running without its token, starting a stack that is not up).

Processes are read with PowerShell CIM (psutil cannot read the command line of a
PowerShell-detached process). If they cannot be read, nothing that depends on them is
touched: unknown is never "nothing is running".
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import ports

LOCK_MINUTES = 15.0
MARKER_MINUTES = 10.0
VERIFY_SECONDS = 90.0

FIXED = "fixed"
WOULD_FIX = "would_fix"          # doctor without --fix: what --fix would do
FOR_IAN = "for_ian"              # needs admin, money, a secret or a decision
FAILED = "failed"
SKIPPED = "skipped"              # a fix that is not safe right now (git is running, ...)

PIONIR_ROOT = Path(__file__).resolve().parents[2]
CD = f"cd {PIONIR_ROOT}"

# the pionir.ps1 groups a dead pane can be restarted by: (service ids, the flag that skips them)
_GROUPS = (
    (("galatea",), "-NoVoice"),
    (("daedalus", "melete"), "-NoSpecialists"),
    (("crew",), "-NoCrew"),
    (("peter",), "-NoPeter"),
)
_CORE = ("dashboard", "galatea", "daedalus", "melete", "crew", "peter")
_BRYO = re.compile(r"-m\s+bryo(\s|$)")
_DESKTOP = re.compile(r"(?i)pionir[ _-]?desktop")
_STOPPING = re.compile(r"(?i)pionir\.ps1.*\s-Stop\b")


@dataclass
class Finding:
    check: str
    status: str
    detail: str
    command: str | None = None          # the exact PowerShell for Ian, when it is his to do
    undo: str | None = None             # how to put it back, for a fix that was applied
    extra: dict = field(default_factory=dict)


# ---- the machine, read-only --------------------------------------------------------------
_PROCS = ("@(Get-CimInstance Win32_Process|ForEach-Object{[pscustomobject]@{pid=[int]$_.ProcessId;"
          "name=[string]$_.Name;command=(([string]$_.ExecutablePath)+' '+([string]$_.CommandLine))}})"
          "|ConvertTo-Json -Compress")


def list_processes(run: Callable[..., Any] = subprocess.run) -> list[dict] | None:
    """Every process with its command line (CIM), or None if they cannot be read."""
    try:
        done = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _PROCS],
                   capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(done, "returncode", 1) != 0:
        return None
    try:
        data = json.loads((done.stdout or "").strip() or "[]")
    except ValueError:
        return None
    rows = data if isinstance(data, list) else [data]
    return [{"pid": int(r.get("pid") or 0), "name": str(r.get("name") or ""),
             "command": str(r.get("command") or "")} for r in rows if isinstance(r, dict)]


def _git_running(procs: list[dict]) -> bool:
    return any(p["name"].lower() in ("git.exe", "git") or
               re.search(r"(?i)(^|[\\/\s])git(\.exe)?\s", p["command"]) for p in procs)


def _aside(path: Path, now: float) -> Path:
    stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime(now))
    target = path.with_name(f"{path.name}.stale-{stamp}")
    n = 1
    while target.exists():                 # never over an earlier one: each stays undoable
        n += 1
        target = path.with_name(f"{path.name}.stale-{stamp}-{n}")
    path.rename(target)
    return target


# ---- the checks ----------------------------------------------------------------------------
def git_lock_paths(repos: list[Path]) -> list[Path]:
    found: list[Path] = []
    for repo in repos:
        git = repo / ".git"
        if not git.is_dir():
            continue
        for lock in [git / "index.lock", *git.glob("worktrees/*/index.lock")]:
            if lock.is_file():
                found.append(lock)
    return found


def estate_repos(extra: list[Path] = ()) -> list[Path]:
    """Pionir, every git repo beside it (C:\\src\\*), the specialists' folders, and every
    build sandbox repo - discovered, not listed."""
    repos = {PIONIR_ROOT}
    parent = PIONIR_ROOT.parent
    try:
        repos.update(p for p in parent.iterdir() if (p / ".git").is_dir())
    except OSError:
        pass
    for env, default in (("PIONIR_DAEDALUS_DIR", r"C:\src\Tech-Support\daedalus"),
                         ("PIONIR_MELETE_DIR", r"C:\src\Tech-Support\melete")):
        repos.add(Path(os.environ.get(env) or default))
    sandbox = os.environ.get("PIONIR_DAEDALUS_SANDBOX", "").strip()
    if not sandbox:
        from .build_sandbox import default_sandbox_root
        sandbox = default_sandbox_root()
    try:
        repos.update(p for p in Path(sandbox).iterdir() if (p / ".git").is_dir())
    except OSError:
        pass
    repos.update(extra)
    return sorted(repos)


def check_git_locks(repos: list[Path], procs: list[dict] | None, *, now: float,
                    apply: bool) -> list[Finding]:
    out: list[Finding] = []
    for lock in git_lock_paths(repos):
        st = lock.stat()
        age = (now - st.st_mtime) / 60
        where = str(lock)
        if st.st_size != 0:
            out.append(Finding("git_lock", FOR_IAN, f"{where} is not empty ({st.st_size} bytes): "
                               "a git command may have died mid-write; look before removing it",
                               command=f"cd {lock.parent.parent}; git status; "
                                       f"Remove-Item '{where}'"))
            continue
        if age < LOCK_MINUTES:
            out.append(Finding("git_lock", SKIPPED, f"{where} is {age:.0f} min old: a git "
                               "command may still be using it"))
            continue
        if procs is None:
            out.append(Finding("git_lock", SKIPPED, f"{where}: processes could not be read, so "
                               "a running git cannot be ruled out"))
            continue
        if _git_running(procs):
            out.append(Finding("git_lock", SKIPPED, f"{where}: a git process is running; "
                               "left alone"))
            continue
        if not apply:
            out.append(Finding("git_lock", WOULD_FIX, f"{where}: stale ({age:.0f} min, 0 bytes, "
                               "no git running) - --fix moves it aside"))
            continue
        try:
            moved = _aside(lock, now)
        except OSError as exc:
            out.append(Finding("git_lock", FAILED, f"{where} could not be moved aside "
                               f"({type(exc).__name__}: {exc})",
                               command=f"Remove-Item '{where}'"))
            continue
        out.append(Finding("git_lock", FIXED, f"{where}: stale ({age:.0f} min, 0 bytes, no git "
                           "running) - moved aside", undo=f"Move-Item '{moved}' '{where}'"))
    return out


def check_bryo_pid(terrarium: Path, procs: list[dict] | None, *, now: float,
                   apply: bool) -> list[Finding]:
    pidfile = terrarium / "state" / "bryo.pid"
    if not pidfile.is_file():
        return []
    try:
        pid = int(pidfile.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        pid = 0
    if procs is None:
        return [Finding("bryo_pid", SKIPPED, f"{pidfile}: processes could not be read")]
    owner = next((p for p in procs if p["pid"] == pid), None)
    if owner is not None and _BRYO.search(owner["command"]):
        return []                                           # Bryo is alive and holds it
    why = ("names no process" if owner is None else
           f"names pid {pid}, which is {owner['name']}, not Bryo (a recycled pid)")
    if not apply:
        return [Finding("bryo_pid", WOULD_FIX, f"{pidfile} {why}: every new Bryo is refused as "
                        "'another Bryo is alive' - --fix moves it aside")]
    try:
        moved = _aside(pidfile, now)
    except OSError as exc:
        return [Finding("bryo_pid", FAILED, f"{pidfile} could not be moved aside ({exc})",
                        command=f"Remove-Item '{pidfile}'")]
    return [Finding("bryo_pid", FIXED, f"{pidfile} {why} - moved aside",
                    undo=f"Move-Item '{moved}' '{pidfile}'")]


def check_stop_marker(marker: Path, procs: list[dict] | None, *, now: float,
                      apply: bool) -> list[Finding]:
    if not marker.is_file():
        return []
    age = (now - marker.stat().st_mtime) / 60
    if age < MARKER_MINUTES:
        return [Finding("stop_marker", SKIPPED, f"{marker} is {age:.0f} min old: a stop may "
                        "be under way")]
    if procs is None:
        return [Finding("stop_marker", SKIPPED, f"{marker}: processes could not be read")]
    if any(_STOPPING.search(p["command"]) for p in procs):
        return [Finding("stop_marker", SKIPPED, f"{marker}: pionir.ps1 -Stop is running")]
    if not apply:
        return [Finding("stop_marker", WOULD_FIX, f"{marker} was left by a stop {age:.0f} min "
                        "ago: while it is there a crashed pane closes instead of restarting - "
                        "--fix removes it")]
    try:
        marker.unlink()
    except OSError as exc:
        return [Finding("stop_marker", FAILED, f"{marker} could not be removed ({exc})",
                        command=f"Remove-Item '{marker}'")]
    return [Finding("stop_marker", FIXED, f"{marker} (a stop {age:.0f} min ago) removed: "
                    "crashed panes restart again")]


def check_dirs(dirs: list[Path], *, apply: bool) -> list[Finding]:
    out: list[Finding] = []
    for d in dirs:
        if d.is_dir():
            continue
        if not apply:
            out.append(Finding("missing_dir", WOULD_FIX, f"{d} is missing - --fix creates it"))
            continue
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            out.append(Finding("missing_dir", FAILED, f"{d} could not be created ({exc})",
                               command=f"New-Item -ItemType Directory -Force '{d}'"))
            continue
        out.append(Finding("missing_dir", FIXED, f"{d} created (empty)",
                           undo=f"Remove-Item '{d}'"))
    return out


def _launcher_args(dead: set[str]) -> list[str]:
    flags = ["-NoBrowser", "-NoBryo", "-NoTunnel"]
    for ids, flag in _GROUPS:
        if not dead.intersection(ids):
            flags.append(flag)
    return flags


def check_panes(rows: list[dict], procs: list[dict] | None, *, apply: bool,
                launch: Callable[[list[str]], int] | None = None,
                reaudit: Callable[[], list[dict]] | None = None,
                sleep: Callable[[float], None] = time.sleep,
                clock: Callable[[], float] = time.monotonic) -> list[Finding]:
    core = [r for r in rows if r["id"] in _CORE]
    if not core or any(r["state"] == ports.UNKNOWN for r in core):
        return []
    up = [r for r in core if r["state"] in (ports.OURS_HEALTHY, ports.OURS_UNHEALTHY,
                                            ports.WARMING)]
    dead = [r for r in core if r["state"] == ports.FREE]
    if not dead:
        return []
    names = ", ".join(f"{r['label']} :{r['port']}" for r in dead)
    if not up:
        return [Finding("dead_pane", FOR_IAN, f"the stack is not running ({names} all down); "
                        "starting it is yours to decide", command=f"{CD}; .\\pionir.ps1")]
    if procs is None:
        return [Finding("dead_pane", SKIPPED, f"down: {names}; processes could not be read, "
                        "so who owns the stack is unknown")]
    if any(_DESKTOP.search(p["command"]) and re.search(r"(?i)(electron|pionir desktop)\.exe",
                                                        p["command"]) for p in procs):
        return [Finding("dead_pane", FOR_IAN, f"down: {names}. Pionir Desktop runs the stack: "
                        "press Restart on it in Desktop's Bridges tab (pionir.ps1 never starts "
                        "a second copy beside Desktop)")]
    args = _launcher_args({r["id"] for r in dead})
    cmd = f"{CD}; .\\pionir.ps1 {' '.join(args)}"
    if not apply:
        return [Finding("dead_pane", WOULD_FIX, f"down while the rest is up: {names} - --fix "
                        f"runs the launcher for only those: {cmd}")]
    if launch is None:
        launch = _run_launcher
    code = launch(args)
    if reaudit is None:
        return [Finding("dead_pane", FIXED if code == 0 else FAILED,
                        f"ran pionir.ps1 {' '.join(args)} (exit {code}) for {names}",
                        command=None if code == 0 else cmd)]
    # verify by PORT, not by counting processes
    deadline = clock() + VERIFY_SECONDS
    want = {(r["id"], r["port"]) for r in dead}
    still = want
    while True:
        now_rows = reaudit()
        still = {(r["id"], r["port"]) for r in now_rows if (r["id"], r["port"]) in want
                 and r["state"] not in (ports.OURS_HEALTHY, ports.WARMING)}
        if not still or clock() >= deadline:
            break
        sleep(3.0)
    if still:
        left = ", ".join(f"{i} :{p}" for i, p in sorted(still))
        return [Finding("dead_pane", FAILED, f"ran pionir.ps1 {' '.join(args)} (exit {code}), "
                        f"but {left} still does not answer; read its pane", command=cmd)]
    return [Finding("dead_pane", FIXED, f"{names} restarted through pionir.ps1 "
                    f"{' '.join(args)} and answering on its port again")]


def _run_launcher(args: list[str]) -> int:
    done = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                           str(PIONIR_ROOT / "pionir.ps1"), *args],
                          cwd=str(PIONIR_ROOT), timeout=300, check=False)
    return done.returncode


def for_ian(rows: list[dict], bridge_report: dict, sandbox_why: str | None) -> list[Finding]:
    """What doctor saw that is never fixed automatically: the exact command, with its cd."""
    out: list[Finding] = []
    for r in rows:
        if r["state"] == ports.FOREIGN:
            out.append(Finding("foreign_port", FOR_IAN, f"{r['label']} :{r['port']} - "
                               f"{r.get('detail')}. Whether to stop that program is your call.",
                               command=f"Get-CimInstance Win32_Process -Filter \"ProcessId="
                                       f"{r.get('pid')}\" | Select-Object ProcessId,Name,"
                                       "CommandLine"))
    if any(isinstance(v, dict) and "warning" in v for v in (bridge_report or {}).values()):
        out.append(Finding("open_bridge", FOR_IAN, "a bridge takes jobs without its token; "
                           "restarting the stack gives it one",
                           command=f"{CD}; .\\pionir.ps1 -Stop; .\\pionir.ps1"))
    if sandbox_why:
        out.append(Finding("build_sandbox", FOR_IAN, f"night builds and the API builder do "
                           f"nothing until this runs once, as administrator: {sandbox_why}",
                           command=f"{CD}; .\\tools\\setup-build-sandbox.ps1   "
                                   "# in an ADMINISTRATOR PowerShell"))
    return out


def run(*, apply: bool, state_root: Path | None, rows: list[dict], bridge_report: dict,
        procs: list[dict] | None | str = "read", repos: list[Path] | None = None,
        terrarium: Path | None = None, marker: Path | None = None,
        sandbox_why: Callable[[], str | None] | None = None,
        launch: Callable[[list[str]], int] | None = None,
        reaudit: Callable[[], list[dict]] | None = None,
        now: float | None = None) -> dict:
    """Every check, applied (``apply``) or only described. Each finding says what it did, or
    the command for Ian."""
    now = time.time() if now is None else now
    if procs == "read":
        procs = list_processes()
    if repos is None:
        repos = estate_repos()
    if terrarium is None:
        terrarium = Path(os.environ.get("PIONIR_TERRARIUM_DIR") or r"C:\src\terrarium")
    if marker is None:
        marker = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Pionir" / "stopping"
    dirs = [] if state_root is None else [
        state_root, state_root / "logs", state_root / "crew", state_root / "builds",
        state_root / "products"]
    findings: list[Finding] = []
    for name, step in (
            ("git_lock", lambda: check_git_locks(repos, procs, now=now, apply=apply)),
            ("bryo_pid", lambda: check_bryo_pid(terrarium, procs, now=now, apply=apply)),
            ("stop_marker", lambda: check_stop_marker(marker, procs, now=now, apply=apply)),
            ("missing_dir", lambda: check_dirs(dirs, apply=apply)),
            ("dead_pane", lambda: check_panes(rows, procs, apply=apply, launch=launch,
                                              reaudit=reaudit)),
            ("for_ian", lambda: for_ian(rows, bridge_report,
                                        sandbox_why() if sandbox_why else None))):
        try:
            findings.extend(step())
        except Exception as exc:  # noqa: BLE001 - one check failing never hides the others
            findings.append(Finding(name, FAILED, f"the check itself failed: "
                                    f"{type(exc).__name__}: {exc}"))
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.status] = counts.get(f.status, 0) + 1
    return {"applied": apply, "counts": counts,
            "processes_readable": procs is not None,
            "findings": [{k: v for k, v in asdict(f).items() if v not in (None, {}, [])}
                         for f in findings]}
