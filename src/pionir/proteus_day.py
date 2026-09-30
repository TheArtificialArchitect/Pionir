"""Day P/L for the trading accounts: today's balance now, minus the balance at today's open.

The open is recorded ONCE per trading day, per account: the first successful read of the
account value after 09:30 America/New_York. It lives in a small state file
(``~/.pionir/state/proteus-day-open.json``), written atomically, and a recorded open is never
overwritten - not by a later read, a restart, or a second process. The day is the New York
calendar date (``zoneinfo``, so a DST change moves nothing: 09:30 is 09:30 wall time in both
EST and EDT, and the key is the local date, not 24 h arithmetic).

Unknown is not zero. Every path that lacks either number says "unknown" and why:

- no read of the account now (tunnel down, no read key, soft failure, empty book) -> unknown;
- a weekend, or before 09:30 ET -> unknown (there is no open yet);
- after 16:00 ET with no open recorded (Pionir was off all session) -> unknown: an open
  taken after the close would make every day P/L a fake 0;
- an unreadable state file -> unknown, and the file is set aside (never silently reused);
  if it cannot even be set aside, the view says so - it never claims a move that did not happen.

A first read that comes late in the session (Pionir started at noon) IS recorded, and the view
says ``since`` when it was taken: the P/L is "since 12:03", never dressed up as since the open.
Exchange holidays are not modelled: on one the first read simply records an unchanged balance.

Read-only against the accounts: the real reader GETs Prometheus's ``/status`` and the Robinhood
API's ``/portfolio`` on the SSH tunnel's loopback end with the READ keys, and nothing else. It is
injectable, and off by default: a Pionir built without it (every test) reads no account.

The read key is sent only to the SSH tunnel: before each read the reader asks the OS who holds the
loopback port and sends nothing unless every listener on it is ``ssh.exe``. Any other program on
18000/18001 would otherwise be handed the key (the /health probe elsewhere sends none). If the
holder cannot be determined the key stays home and the account reads unknown, with the reason.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import math
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import time as dtime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pionir import atomic

LOG = logging.getLogger("pionir.proteus_day")

TZ_NAME = "America/New_York"
OPEN_AT = dtime(9, 30)
CLOSE_AT = dtime(16, 0)
KEEP_DAYS = 8
ACCOUNTS = ("prometheus", "robinhood")

# (account, tunnel loopback port, path, read-key file under the secrets folder)
READ_ROUTES = (
    ("prometheus", 18001, "/status", "prometheus-read-key.txt"),
    ("robinhood", 18000, "/portfolio", "proteus-read-key.txt"),
)
LOOPBACK = "127.0.0.1"


@dataclass(frozen=True, slots=True)
class Read:
    """One account's value right now, in cents - or why there is none."""
    cents: int | None
    why: str | None = None


AccountReader = Callable[[], Mapping[str, Read]]


def _zone() -> ZoneInfo:
    return ZoneInfo(TZ_NAME)


def _local(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=UTC).astimezone(_zone())


def _clock_words(moment: datetime) -> str:
    return moment.strftime("%H:%M ET")


def session(ts: float) -> tuple[str, str]:
    """(New York date key, phase) for an instant. phase: weekend / pre-open / open / closed."""
    local = _local(ts)
    key = local.date().isoformat()
    if local.weekday() >= 5:
        return key, "weekend"
    at = local.timetz().replace(tzinfo=None)
    if at < OPEN_AT:
        return key, "pre-open"
    if at >= CLOSE_AT:
        return key, "closed"
    return key, "open"


class DayOpenStore:
    """The recorded opens: ``{"v":1,"days":{"2026-09-29":{"prometheus":{"open_cents":..,
    "opened_at":..,"opened_local":..}}}}``. Only this class writes the file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.problem: str | None = None
        # the plane refresh and a proteus.status task can read at once: one writer of the open
        self._lock = threading.RLock()

    def load(self) -> dict[str, dict[str, Any]]:
        """The days on file. An unreadable file is set aside once and read as empty, and
        ``problem`` says so (a view built from it says the day's open may have been lost)."""
        self.problem = None
        try:
            text = self.path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return {}
        except OSError as error:
            self.problem = f"the day-open file cannot be read ({type(error).__name__})"
            return {}
        try:
            doc = json.loads(text)
            days = doc["days"]
            if not isinstance(days, dict) or not all(isinstance(v, dict) for v in days.values()):
                raise ValueError("days is not a map of maps")
        except (ValueError, KeyError, TypeError):
            self._set_aside()
            return {}
        return days

    def _set_aside(self) -> None:
        try:
            aside = self.path.with_suffix(self.path.suffix + ".corrupt")
            atomic.replace(self.path, aside)
        except OSError as error:
            LOG.warning("the unreadable day-open file %s could not be set aside: %s", self.path, error)
            self.problem = (f"the day-open file is unreadable and could NOT be set aside "
                            f"({type(error).__name__}); today's open may be lost")
            return
        self.problem = "the day-open file was unreadable and was set aside; today's open may have been lost"

    def opened(self, day: str, account: str) -> dict[str, Any] | None:
        entry = self.load().get(day, {}).get(account)
        if isinstance(entry, dict) and isinstance(entry.get("open_cents"), int) \
                and not isinstance(entry.get("open_cents"), bool):
            return entry
        return None

    def record(self, day: str, account: str, cents: int, ts: float) -> dict[str, Any]:
        """Record ``account``'s open for ``day`` unless one is already there; returns the
        entry that stands (the existing one when there was one - never overwritten)."""
        with self._lock:
            return self._record(day, account, cents, ts)

    def _record(self, day: str, account: str, cents: int, ts: float) -> dict[str, Any]:
        days = self.load()
        if self.problem:
            # an unreadable file may hold today's open: writing now could destroy it
            raise OSError(self.problem)
        existing = days.get(day, {}).get(account)
        if isinstance(existing, dict) and isinstance(existing.get("open_cents"), int):
            return existing
        local = _local(ts)
        entry = {"open_cents": int(cents),
                 "opened_at": datetime.fromtimestamp(ts, tz=UTC).isoformat(timespec="seconds"),
                 "opened_local": local.isoformat(timespec="seconds")}
        days.setdefault(day, {})[account] = entry
        for stale in sorted(days)[:-KEEP_DAYS]:
            days.pop(stale, None)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic.write_text(self.path, json.dumps({"v": 1, "days": days}, indent=1, sort_keys=True))
        return entry


def _unknown(why: str, day: str) -> dict[str, Any]:
    return {"state": "unknown", "why": why, "trading_day": day}


def day_view(store: DayOpenStore, reads: Mapping[str, Read], ts: float,
             accounts: tuple[str, ...] = ACCOUNTS) -> dict[str, Any]:
    """Per account: ``{"state": "known", "pl_cents", "open_cents", "now_cents", "since", ...}``
    or ``{"state": "unknown", "why"}``. Records a missing open when this is the first good
    read of an open session. Never raises."""
    day, phase = session(ts)
    out: dict[str, Any] = {}
    for account in accounts:
        try:
            out[account] = _one(store, account, reads.get(account), day, phase, ts)
        except Exception as error:  # noqa: BLE001 - one account's trouble must not sink the plane
            LOG.warning("day P/L for %s failed: %s", account, error)
            out[account] = _unknown(f"day P/L failed ({type(error).__name__})", day)
    return out


def _one(store: DayOpenStore, account: str, read: Read | None, day: str, phase: str,
         ts: float) -> dict[str, Any]:
    if phase == "weekend":
        return _unknown("weekend: no trading day", day)
    entry = store.opened(day, account)
    if entry is None and store.problem:
        return _unknown(store.problem, day)
    if read is None or read.cents is None:
        why = (read.why if read is not None and read.why else "the balance was not read")
        return _unknown(f"now unknown: {why}"[:200], day)
    if entry is None:
        if phase == "pre-open":
            return _unknown("before the 09:30 ET open: no open balance yet", day)
        if phase == "closed":
            return _unknown("no open balance was recorded today (Pionir did not read it during "
                            "the session)", day)
        entry = store.record(day, account, read.cents, ts)
    open_cents = int(entry["open_cents"])
    view: dict[str, Any] = {"state": "known", "trading_day": day, "open_cents": open_cents,
                            "now_cents": read.cents, "pl_cents": read.cents - open_cents,
                            "opened_at": entry.get("opened_at")}
    late = _late_words(entry)
    if late:
        view["since"] = late
    return view


def _late_words(entry: Mapping[str, Any]) -> str | None:
    """"12:03 ET" when the open was taken more than 5 minutes after 09:30 ET, else None."""
    try:
        moment = datetime.fromisoformat(str(entry.get("opened_local")))
    except ValueError:
        return None
    at = moment.timetz().replace(tzinfo=None)
    minutes = (at.hour * 60 + at.minute) - (OPEN_AT.hour * 60 + OPEN_AT.minute)
    return _clock_words(moment) if minutes > 5 else None


# ---- the real reader (read keys, tunnel loopback, GET only)
def _cents(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value.replace(",", "").lstrip("$"))
        except ValueError:
            return None
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return round(float(value) * 100)


def _soft(body: Mapping[str, Any]) -> str | None:
    """A 200 that is really a failure ({"error"} or a soft "note")."""
    for key in ("error", "note"):
        text = body.get(key)
        if isinstance(text, str) and text:
            return text[:120]
    return None


def value_of(account: str, body: Any) -> Read:
    """The account value in a /status (Prometheus) or /portfolio (Robinhood) body: unknown on a
    soft failure, and unknown - never $0 - on an all-zero book."""
    if not isinstance(body, dict):
        return Read(None, "the API answered something that is not a JSON object")
    soft = _soft(body)
    if soft:
        return Read(None, f"the API says: {soft}")
    if account == "robinhood":
        value, spare = body.get("total_value"), body.get("buying_power")
        positions = body.get("positions")
        empty = value in (0, 0.0) and spare in (0, 0.0) and not (isinstance(positions, list) and positions)
    else:
        value, spare = body.get("equity"), body.get("buying_power")
        empty = value in (0, 0.0) and spare in (0, 0.0) and body.get("position_count") in (0, None)
    if empty:
        return Read(None, "the upstream returned an empty book (all zeros), which is unknown, not $0")
    cents = _cents(value)
    if cents is None or cents <= 0:
        return Read(None, "the API reported no usable account value")
    return Read(cents)


def _key(secrets: Path, name: str) -> str | None:
    try:
        text = (secrets / name).read_text(encoding="utf-8-sig").strip()
    except OSError:
        return None
    return text if text and "\n" not in text else None


NO_LISTENER = ""
TUNNEL_IMAGES = ("ssh.exe", "ssh")


def _hidden() -> dict[str, Any]:
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def listener_owner(port: int, *, run: Callable[..., Any] = subprocess.run,
                   platform: str | None = None) -> str | None:
    """Who listens on loopback ``port``: the process image names (comma-joined, sorted), ``""``
    when nothing listens, or None when that cannot be determined (not Windows, a command failed,
    an unreadable answer). EVERY listener on the port counts, whatever address it bound - a
    wildcard listener also receives connections to 127.0.0.1. Read-only; never raises."""
    if (platform or sys.platform) != "win32":
        return None
    try:
        net = run(["netstat", "-ano"], capture_output=True, text=True, timeout=8, **_hidden())
        if net.returncode != 0:
            return None
        pids: set[str] = set()
        for line in net.stdout.splitlines():
            cols = line.split()
            # TCP  <local>  <foreign>  LISTENING  <pid>: a listener has an all-zero foreign
            # address (matched on that, not on the word LISTENING, which is localised)
            if len(cols) >= 5 and cols[0].upper() == "TCP" and cols[2] in ("0.0.0.0:0", "[::]:0")                     and cols[1].rpartition(":")[2] == str(port):
                pids.add(cols[-1])
        names: set[str] = set()
        for pid in sorted(pids):
            if not pid.isdigit():
                return None
            task = run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                       capture_output=True, text=True, timeout=8, **_hidden())
            rows = [r for r in csv.reader(io.StringIO(task.stdout)) if len(r) >= 2 and r[1] == pid]
            if task.returncode != 0 or not rows:
                return None
            names.add(rows[0][0].lower())
        return ",".join(sorted(names))
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _not_the_tunnel(port: int, owner: str | None) -> str | None:
    """Why the key must not be sent to ``port`` - None when its only listener is the SSH tunnel."""
    if owner is None:
        return f"cannot tell who holds port {port}, so the read key was not sent"
    if owner == NO_LISTENER:
        return "the SSH tunnel is down (nothing answered)"
    if not all(name in TUNNEL_IMAGES for name in owner.split(",")):
        return f"port {port} is held by {owner[:60]}, not the SSH tunnel, so the read key was not sent"
    return None


def real_account_reader(secrets: Path, *, timeout: float = 6.0,
                        owner_of: Callable[[int], str | None] = listener_owner) -> AccountReader:
    """The reader Pionir runs on the live box: each account through its tunnel port, GET only.
    The key goes only to a port whose every listener is ssh.exe (``owner_of``)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def read() -> dict[str, Read]:
        found: dict[str, Read] = {}
        for account, port, path, key_name in READ_ROUTES:
            key = _key(secrets, key_name)
            if key is None:
                found[account] = Read(None, f"no read key ({key_name}) on this machine")
                continue
            refused = _not_the_tunnel(port, owner_of(port))
            if refused:
                found[account] = Read(None, refused)
                continue
            request = urllib.request.Request(f"http://{LOOPBACK}:{port}{path}", method="GET",
                                             headers={"x-api-key": key})
            try:
                with opener.open(request, timeout=timeout) as response:
                    body = json.loads(response.read(1_000_000))
            except urllib.error.HTTPError as error:
                found[account] = Read(None, f"the read API answered HTTP {error.code}")
                continue
            except (urllib.error.URLError, OSError):
                found[account] = Read(None, "the SSH tunnel is down (nothing answered)")
                continue
            except ValueError:
                found[account] = Read(None, "the read API's answer was not JSON")
                continue
            found[account] = value_of(account, body)
        return found

    return read
