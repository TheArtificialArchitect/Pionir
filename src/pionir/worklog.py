"""The work log: Ian's hours and pay across the day job and freelance work.

Its own SQLite file (``<state_root>/worklog/worklog.db``) - NOT the memory db. Nothing in
memory, recall, lessons, consolidation, posting or Moss's memory opens it, and this module
opens nothing else. It is financial-personal data: the HTTP door (workapi.py) answers only
the owner's signed clients, and Moss's capability (adapters/work_summary.py) gets aggregates
only - never a note.

Rules that hold everywhere in here:

* Money is integer cents. Never a float. Earnings are ``seconds * rate / 3600`` rounded half
  up in integers, once per job per period (no per-session rounding drift).
* Times are stored as UTC ISO seconds (``2026-03-08T14:30:00Z``) with the local zone's name
  beside each row; the period boundaries (today / week / month / year) are LOCAL midnights,
  so a day is 23 or 25 hours long across a DST change and a session that spans midnight is
  split between the two days.
* Rates are Ian's. There is no default: with no rate a job shows hours and "rate not set".
  No tax figure is ever computed; the per-job "set aside %" is an informational number.
* Data Annotation is under NDA. The log stores a project NAME, times, counts and Ian's own
  generic notes - nothing else. Text is capped, refused when it looks like pasted task
  content, and scrubbed of secrets before it is stored.
* A timer left running is FLAGGED, never silently counted: past ``STALE_HOURS`` it drops out
  of every total and only an explicit end time can close it.
* A session is never removed: delete is soft, and every change leaves a ``changes`` row.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import secretscrub

SCHEMA_VERSION = 1

KINDS = ("freelance", "employment")
LOG_KINDS = ("note", "rubric", "payout")

MAX_SESSION_SECONDS = 16 * 3600      # sanity cap on one session
STALE_HOURS = 12                     # an open timer older than this is flagged, not counted
NOTE_CAP = 600                       # characters in any note, rubric tip or session note
NAME_CAP = 60
MAX_JOBS = 50
MAX_RATE_CENTS = 1_000_000           # $10,000 / hour: a typo guard, not a policy
MAX_PAYOUT_CENTS = 100_000_000       # $1,000,000
MAX_LIST = 500
DEFAULT_LIST = 200
MAX_RANGE_DAYS = 800
FUTURE_SKEW = timedelta(seconds=60)
EARLIEST = datetime(2000, 1, 1, tzinfo=UTC)
WEEKLY_ROLLUP_WEEKS = 12
SESSION_NOTE_LINES = 8

CONFIDENTIALITY = (
    "Data Annotation is under NDA. Write only the project name, times, counts and your own "
    "generic notes - never paste task text, a prompt, a response, or a rubric."
)

# A transcript-shaped line is task content, not a generic note.
_TASK_SHAPE = re.compile(
    r"(^|\n)\s*(response|answer|output|completion)\s*[ab12]?\s*[:\-]|"
    r"(^|\n)\s*(prompt|user|assistant|human|ai|system|instructions?)\s*:|"
    r"\b(response|answer)\s+[ab]\s*:",
    re.IGNORECASE,
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class WorkError(ValueError):
    """A refused work-log operation. ``code`` says why in a word the HTTP door maps to a
    status; the message is safe to show and never carries stored text."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _Unset:
    def __repr__(self) -> str:  # pragma: no cover
        return "UNSET"


UNSET: Any = _Unset()


# --------------------------------------------------------------------------- time

def iso(dt: datetime) -> str:
    """The one stored form: UTC, whole seconds, ``Z``."""
    return dt.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


class Zone:
    """The local zone: an IANA name when configured, else the machine's own."""

    def __init__(self, name: str | None = None) -> None:
        self.name = (name or "").strip() or None
        try:
            self._zi = ZoneInfo(self.name) if self.name else None
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise WorkError("bad_input", f"unknown time zone: {self.name!r}") from exc

    def to_local(self, utc: datetime) -> datetime:
        return utc.astimezone(self._zi) if self._zi else utc.astimezone()

    def label(self, utc: datetime) -> str:
        if self.name:
            return self.name
        local = self.to_local(utc)
        off = local.utcoffset() or timedelta(0)
        minutes = int(off.total_seconds() // 60)
        sign = "+" if minutes >= 0 else "-"
        return f"{local.tzname()} (UTC{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d})"

    def local_to_utc(self, naive: datetime) -> datetime:
        """A wall-clock time to UTC. A time that does not exist (the spring-forward gap)
        is refused; an ambiguous one (fall back) takes its first occurrence."""
        aware = naive.replace(tzinfo=self._zi) if self._zi else naive.astimezone()
        utc = aware.astimezone(UTC)
        if self.to_local(utc).replace(tzinfo=None) != naive:
            raise WorkError("bad_input", "that local time does not exist (the clocks skip forward)")
        return utc

    def day_start(self, day: date) -> datetime:
        try:
            return self.local_to_utc(datetime.combine(day, dtime.min))
        except WorkError:  # a zone whose DST jump swallows midnight
            return self.local_to_utc(datetime.combine(day, dtime(1, 0)))


def parse_ts(value: Any, zone: Zone, *, allow_naive: bool = True) -> datetime:
    """An aware UTC datetime from ISO text (or an aware datetime). Naive text is local time
    when allowed - the CLI's way - and refused otherwise (the HTTP door's)."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise WorkError("bad_input", "time needs a zone")
        return value.astimezone(UTC).replace(microsecond=0)
    if not isinstance(value, str) or not value.strip() or len(value) > 40:
        raise WorkError("bad_input", "time must be ISO text like 2026-03-08T14:30:00Z")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError as exc:
        raise WorkError("bad_input", "time must be ISO text like 2026-03-08T14:30:00Z") from exc
    parsed = parsed.replace(microsecond=0)
    if parsed.tzinfo is None:
        if not allow_naive:
            raise WorkError("bad_input", "time needs a zone (end it with Z or an offset)")
        return zone.local_to_utc(parsed)
    return parsed.astimezone(UTC)


def parse_cents(value: Any, *, what: str = "amount") -> int:
    """Dollars as text ("12.5", "$1,200.00") to integer cents. Decimal, never float; at most
    two decimal places."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise WorkError("bad_input", f"{what} must be a dollar amount like 25.00")
    text = str(value).strip().replace("$", "").replace(",", "")
    if not re.fullmatch(r"\d{1,9}(\.\d{1,2})?", text):
        raise WorkError("bad_input", f"{what} must be a dollar amount like 25.00")
    try:
        return int((Decimal(text) * 100).to_integral_value())
    except InvalidOperation as exc:  # pragma: no cover
        raise WorkError("bad_input", f"{what} must be a dollar amount like 25.00") from exc


def earned_cents(seconds: int, rate_cents: int) -> int:
    """seconds x rate / 3600, half-up, all integers."""
    return (seconds * rate_cents + 1800) // 3600


def hours(seconds: int) -> float:
    """Hours to two places, from an integer count of hundredths (display only)."""
    return ((seconds * 100 + 1800) // 3600) / 100


# --------------------------------------------------------------------------- text

def clean_text(value: Any, *, what: str = "note", required: bool = True, cap: int = NOTE_CAP,
               known: Iterable[str] = ()) -> str:
    """Ian's own words, made safe to store: bounded, not task-shaped, secrets scrubbed."""
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise WorkError("bad_input", f"{what} must be text")
    text = value.strip()
    if not text:
        if required:
            raise WorkError("bad_input", f"{what} is empty")
        return ""
    if len(text) > cap:
        raise WorkError("too_long", f"{what} is over {cap} characters. {CONFIDENTIALITY}")
    if _CONTROL.search(text):
        raise WorkError("bad_input", f"{what} has control characters")
    if text.count("\n") + 1 > SESSION_NOTE_LINES or _TASK_SHAPE.search(text):
        raise WorkError("looks_like_task", f"{what} looks like pasted task content. {CONFIDENTIALITY}")
    return secretscrub.scrub_text(text, known)


def _clean_name(value: Any) -> str:
    if not isinstance(value, str):
        raise WorkError("bad_input", "job name must be text")
    name = " ".join(value.split())
    if not name or len(name) > NAME_CAP:
        raise WorkError("bad_input", f"job name must be 1-{NAME_CAP} characters")
    if _CONTROL.search(name) or name.isdigit():
        raise WorkError("bad_input", "job name cannot be only digits or hold control characters")
    if secretscrub.scrub_text(name) != name:
        raise WorkError("bad_input", "job name looks like a secret")
    if _TASK_SHAPE.search(name):
        raise WorkError("looks_like_task", f"job name looks like task content. {CONFIDENTIALITY}")
    return name


# --------------------------------------------------------------------------- schema

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    kind TEXT NOT NULL CHECK (kind IN ('freelance','employment')),
    hourly_rate_cents INTEGER CHECK (hourly_rate_cents IS NULL OR hourly_rate_cents >= 0),
    currency TEXT NOT NULL DEFAULT 'USD',
    set_aside_pct INTEGER CHECK (set_aside_pct IS NULL OR (set_aside_pct BETWEEN 0 AND 100)),
    active INTEGER NOT NULL DEFAULT 1,
    created_ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    start_ts TEXT NOT NULL,
    end_ts TEXT,
    note TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL CHECK (source IN ('timer','manual')),
    tz TEXT NOT NULL,
    created_ts TEXT NOT NULL,
    updated_ts TEXT NOT NULL,
    deleted_ts TEXT
);
CREATE INDEX IF NOT EXISTS sessions_job_start ON sessions(job_id, start_ts);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_timer_per_job
    ON sessions(job_id) WHERE end_ts IS NULL AND deleted_ts IS NULL;
CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    ts TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('note','rubric','payout')),
    text TEXT NOT NULL DEFAULT '',
    amount_cents INTEGER,
    tz TEXT NOT NULL,
    created_ts TEXT NOT NULL,
    deleted_ts TEXT
);
CREATE INDEX IF NOT EXISTS log_job_ts ON log(job_id, ts);
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    entity TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    before TEXT,
    after TEXT
);
"""


def _apply_schema(con: sqlite3.Connection) -> None:
    con.executescript(_SCHEMA)
    con.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),))


def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(r) if r is not None else None


@dataclass(frozen=True, slots=True)
class Bounds:
    lo: datetime
    hi: datetime


# --------------------------------------------------------------------------- the log

class WorkLog:
    """The work log at one path. A connection per call (the server is threaded); a write is
    one immediate transaction that validates and changes together."""

    def __init__(self, path: Path, *, clock: Any = None, zone: Zone | None = None,
                 known: Callable[[], Iterable[str]] = tuple) -> None:
        self.path = Path(path)
        self._known = known
        self._clock = clock or (lambda: datetime.now(UTC))
        self.zone = zone or Zone(None)

    # ------------------------------------------------------------ plumbing

    def _clean(self, value: Any, **kw: Any) -> str:
        """clean_text, with this server's own secrets (its client tokens) among what is scrubbed."""
        return clean_text(value, known=self._known(), **kw)

    def now(self) -> datetime:
        return self._clock().astimezone(UTC).replace(microsecond=0)

    def exists(self) -> bool:
        return self.path.is_file()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        """A reader. With no file yet it is an empty in-memory db: reading never creates it."""
        if self.exists():
            con = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
        else:
            con = sqlite3.connect(":memory:")
            _apply_schema(con)
        con.row_factory = sqlite3.Row
        try:
            yield con
        finally:
            con.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("PRAGMA journal_mode=WAL")
            _apply_schema(con)
            con.execute("BEGIN IMMEDIATE")
            try:
                yield con
                con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('last_write', ?)",
                            (iso(self.now()),))
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
        finally:
            con.close()

    def _tz(self) -> str:
        return self.zone.label(self.now())

    def _change(self, con: sqlite3.Connection, entity: str, entity_id: int, action: str,
                before: Any = None, after: Any = None) -> None:
        con.execute(
            "INSERT INTO changes(ts, entity, entity_id, action, before, after) VALUES(?,?,?,?,?,?)",
            (iso(self.now()), entity, entity_id, action,
             json.dumps(before, sort_keys=True) if before is not None else None,
             json.dumps(after, sort_keys=True) if after is not None else None))

    def _ts(self, value: Any, *, allow_naive: bool = True) -> datetime:
        return parse_ts(value, self.zone, allow_naive=allow_naive)

    # ------------------------------------------------------------ jobs

    def _job(self, con: sqlite3.Connection, ref: Any) -> sqlite3.Row:
        if isinstance(ref, bool) or ref is None:
            raise WorkError("bad_input", "which job?")
        if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
            row = con.execute("SELECT * FROM jobs WHERE id=?", (int(ref),)).fetchone()
        elif isinstance(ref, str) and ref.strip() and len(ref) <= NAME_CAP * 2:
            row = con.execute("SELECT * FROM jobs WHERE name=? COLLATE NOCASE",
                              (" ".join(ref.split()),)).fetchone()
        else:
            raise WorkError("bad_input", "which job?")
        if row is None:
            raise WorkError("not_found", "no such job")
        return row

    def create_job(self, name: Any, kind: Any = "freelance", *, hourly_rate_cents: Any = None,
                   currency: Any = "USD", set_aside_pct: Any = None) -> dict[str, Any]:
        name = _clean_name(name)
        fields = self._job_fields(kind=kind, hourly_rate_cents=hourly_rate_cents,
                                  currency=currency, set_aside_pct=set_aside_pct)
        with self._write() as con:
            if con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] >= MAX_JOBS:
                raise WorkError("limit", f"at most {MAX_JOBS} jobs")
            if con.execute("SELECT 1 FROM jobs WHERE name=? COLLATE NOCASE", (name,)).fetchone():
                raise WorkError("conflict", "a job with that name exists")
            cur = con.execute(
                "INSERT INTO jobs(name, kind, hourly_rate_cents, currency, set_aside_pct, active,"
                " created_ts) VALUES(?,?,?,?,?,1,?)",
                (name, fields["kind"], fields["hourly_rate_cents"], fields["currency"],
                 fields["set_aside_pct"], iso(self.now())))
            job = self._job_dict(con.execute("SELECT * FROM jobs WHERE id=?", (cur.lastrowid,)).fetchone())
            self._change(con, "job", job["id"], "create", None, {"name": name, **fields})
            return job

    @staticmethod
    def _job_fields(**given: Any) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if "kind" in given:
            if given["kind"] not in KINDS:
                raise WorkError("bad_input", "kind must be freelance or employment")
            out["kind"] = given["kind"]
        if "hourly_rate_cents" in given:
            rate = given["hourly_rate_cents"]
            if rate is not None and (isinstance(rate, bool) or not isinstance(rate, int)
                                     or not 0 <= rate <= MAX_RATE_CENTS):
                raise WorkError("bad_input", "hourly rate must be whole cents from 0 to "
                                f"{MAX_RATE_CENTS}, or unset")
            out["hourly_rate_cents"] = rate
        if "currency" in given:
            cur = given["currency"]
            if not isinstance(cur, str) or not re.fullmatch(r"[A-Za-z]{3}", cur.strip()):
                raise WorkError("bad_input", "currency must be a three-letter code")
            out["currency"] = cur.strip().upper()
        if "set_aside_pct" in given:
            pct = given["set_aside_pct"]
            if pct is not None and (isinstance(pct, bool) or not isinstance(pct, int)
                                    or not 0 <= pct <= 100):
                raise WorkError("bad_input", "set-aside percent must be a whole number 0-100, or unset")
            out["set_aside_pct"] = pct
        return out

    def update_job(self, ref: Any, *, name: Any = UNSET, kind: Any = UNSET,
                   hourly_rate_cents: Any = UNSET, currency: Any = UNSET,
                   set_aside_pct: Any = UNSET, active: Any = UNSET) -> dict[str, Any]:
        given = {k: v for k, v in (("kind", kind), ("hourly_rate_cents", hourly_rate_cents),
                                   ("currency", currency), ("set_aside_pct", set_aside_pct))
                 if v is not UNSET}
        fields = self._job_fields(**given)
        if name is not UNSET:
            fields["name"] = _clean_name(name)
        if active is not UNSET:
            if not isinstance(active, bool):
                raise WorkError("bad_input", "active must be true or false")
            fields["active"] = int(active)
        with self._write() as con:
            job = self._job(con, ref)
            if "name" in fields and con.execute(
                    "SELECT 1 FROM jobs WHERE name=? COLLATE NOCASE AND id<>?",
                    (fields["name"], job["id"])).fetchone():
                raise WorkError("conflict", "a job with that name exists")
            if fields:
                con.execute(f"UPDATE jobs SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                            (*fields.values(), job["id"]))
                self._change(con, "job", job["id"], "update",
                             {k: job[k] for k in fields}, fields)
            return self._job_dict(con.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone())

    @staticmethod
    def _job_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["active"] = bool(d["active"])
        d["rate_set"] = d["hourly_rate_cents"] is not None
        return d

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._read() as con:
            return [self._job_dict(r) for r in con.execute("SELECT * FROM jobs ORDER BY id")]

    # ------------------------------------------------------------ sessions

    def _session_dict(self, row: sqlite3.Row, job_name: str, now: datetime) -> dict[str, Any]:
        d = dict(row)
        start = from_iso(d["start_ts"])
        end = from_iso(d["end_ts"]) if d["end_ts"] else None
        d["job"] = job_name
        d["open"] = end is None
        d["seconds"] = int((end - start).total_seconds()) if end else None
        d["elapsed_seconds"] = int((now - start).total_seconds()) if end is None else d["seconds"]
        d["flagged"] = end is None and (now - start) > timedelta(hours=STALE_HOURS)
        d["deleted"] = d["deleted_ts"] is not None
        return d

    def _validate_interval(self, con: sqlite3.Connection, job_id: int, start: datetime,
                           end: datetime | None, *, exclude: int | None = None) -> None:
        now = self.now()
        if start < EARLIEST:
            raise WorkError("bad_input", "start is before 2000")
        if start > now + FUTURE_SKEW:
            raise WorkError("bad_input", "start is in the future")
        if end is not None:
            if end <= start:
                raise WorkError("bad_input", "end must be after start")
            if end > now + FUTURE_SKEW:
                raise WorkError("bad_input", "end is in the future")
            if (end - start).total_seconds() > MAX_SESSION_SECONDS:
                raise WorkError("too_long", f"a session over {MAX_SESSION_SECONDS // 3600} hours "
                                "is refused - split it, or check the times")
        clash = con.execute(
            "SELECT id, start_ts, end_ts FROM sessions WHERE job_id=? AND deleted_ts IS NULL"
            " AND id IS NOT ? AND start_ts < ? AND (end_ts IS NULL OR end_ts > ?) LIMIT 1",
            (job_id, exclude, iso(end) if end else "9999-12-31T23:59:59Z", iso(start))).fetchone()
        if clash is not None:
            raise WorkError("overlap", f"that overlaps session {clash['id']} of the same job "
                            f"({clash['start_ts']} to {clash['end_ts'] or 'still open'})")

    def start_timer(self, ref: Any, *, note: Any = "", allow_concurrent: bool = False) -> dict[str, Any]:
        note = self._clean(note, what="session note", required=False)
        with self._write() as con:
            job = self._job(con, ref)
            if not job["active"]:
                raise WorkError("conflict", "that job is inactive")
            now = self.now()
            if con.execute("SELECT 1 FROM sessions WHERE job_id=? AND end_ts IS NULL"
                           " AND deleted_ts IS NULL", (job["id"],)).fetchone():
                raise WorkError("already_open", "that job's timer is already running")
            other = con.execute(
                "SELECT j.name FROM sessions s JOIN jobs j ON j.id=s.job_id WHERE s.end_ts IS NULL"
                " AND s.deleted_ts IS NULL AND s.job_id<>? LIMIT 1", (job["id"],)).fetchone()
            if other is not None and not allow_concurrent:
                raise WorkError("other_timer_open", f"the timer for {other['name']!r} is running; "
                                "stop it first, or start this one explicitly alongside it")
            self._validate_interval(con, job["id"], now, None)
            cur = con.execute(
                "INSERT INTO sessions(job_id, start_ts, end_ts, note, source, tz, created_ts,"
                " updated_ts) VALUES(?,?,NULL,?,'timer',?,?,?)",
                (job["id"], iso(now), note, self._tz(), iso(now), iso(now)))
            self._change(con, "session", cur.lastrowid, "start", None,
                         {"job_id": job["id"], "start_ts": iso(now)})
            return self._session_by_id(con, cur.lastrowid)

    def stop_timer(self, ref: Any, *, end: Any = None, note: Any = UNSET) -> dict[str, Any]:
        note_clean = None if note is UNSET else self._clean(note, what="session note", required=False)
        explicit = self._ts(end) if end is not None else None
        with self._write() as con:
            job = self._job(con, ref)
            row = con.execute("SELECT * FROM sessions WHERE job_id=? AND end_ts IS NULL"
                              " AND deleted_ts IS NULL", (job["id"],)).fetchone()
            if row is None:
                raise WorkError("not_open", "that job has no timer running")
            now = self.now()
            start = from_iso(row["start_ts"])
            if explicit is None and (now - start) > timedelta(hours=STALE_HOURS):
                raise WorkError("stale_timer", f"that timer has run {int((now - start).total_seconds() // 3600)}"
                                " hours - it was probably left on. Give the real end time.")
            stop = explicit or now
            self._validate_interval(con, job["id"], start, stop, exclude=row["id"])
            sets = {"end_ts": iso(stop), "updated_ts": iso(now)}
            if note_clean is not None:
                sets["note"] = note_clean
            con.execute(f"UPDATE sessions SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
                        (*sets.values(), row["id"]))
            self._change(con, "session", row["id"], "stop", {"end_ts": None}, {"end_ts": iso(stop)})
            return self._session_by_id(con, row["id"])

    def add_session(self, ref: Any, start: Any, end: Any, note: Any = "", *,
                    allow_naive: bool = True) -> dict[str, Any]:
        s, e = self._ts(start, allow_naive=allow_naive), self._ts(end, allow_naive=allow_naive)
        note = self._clean(note, what="session note", required=False)
        with self._write() as con:
            job = self._job(con, ref)
            self._validate_interval(con, job["id"], s, e)
            now = iso(self.now())
            cur = con.execute(
                "INSERT INTO sessions(job_id, start_ts, end_ts, note, source, tz, created_ts,"
                " updated_ts) VALUES(?,?,?,?,'manual',?,?,?)",
                (job["id"], iso(s), iso(e), note, self._tz(), now, now))
            self._change(con, "session", cur.lastrowid, "create", None,
                         {"job_id": job["id"], "start_ts": iso(s), "end_ts": iso(e)})
            return self._session_by_id(con, cur.lastrowid)

    def edit_session(self, session_id: Any, *, start: Any = UNSET, end: Any = UNSET,
                     note: Any = UNSET, allow_naive: bool = True) -> dict[str, Any]:
        sid = self._int_id(session_id, "session")
        s = self._ts(start, allow_naive=allow_naive) if start is not UNSET else None
        e = self._ts(end, allow_naive=allow_naive) if end is not UNSET else None
        n = self._clean(note, what="session note", required=False) if note is not UNSET else None
        with self._write() as con:
            row = con.execute("SELECT * FROM sessions WHERE id=? AND deleted_ts IS NULL", (sid,)).fetchone()
            if row is None:
                raise WorkError("not_found", "no such session")
            new_start = s or from_iso(row["start_ts"])
            new_end = e or (from_iso(row["end_ts"]) if row["end_ts"] else None)
            self._validate_interval(con, row["job_id"], new_start, new_end, exclude=sid)
            sets: dict[str, Any] = {"start_ts": iso(new_start),
                                    "end_ts": iso(new_end) if new_end else None,
                                    "updated_ts": iso(self.now())}
            if n is not None:
                sets["note"] = n
            con.execute(f"UPDATE sessions SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
                        (*sets.values(), sid))
            self._change(con, "session", sid, "edit",
                         {k: row[k] for k in ("start_ts", "end_ts", "note")},
                         {k: sets[k] for k in ("start_ts", "end_ts", "note") if k in sets})
            return self._session_by_id(con, sid)

    def delete_session(self, session_id: Any) -> dict[str, Any]:
        """Soft: the row stays (out of every total and overlap check), the audit trail says
        what it was."""
        sid = self._int_id(session_id, "session")
        with self._write() as con:
            row = con.execute("SELECT * FROM sessions WHERE id=? AND deleted_ts IS NULL", (sid,)).fetchone()
            if row is None:
                raise WorkError("not_found", "no such session")
            now = iso(self.now())
            con.execute("UPDATE sessions SET deleted_ts=?, updated_ts=? WHERE id=?", (now, now, sid))
            self._change(con, "session", sid, "delete",
                         {k: row[k] for k in ("job_id", "start_ts", "end_ts", "note")}, None)
            return self._session_by_id(con, sid)

    @staticmethod
    def _int_id(value: Any, what: str) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).strip().isdigit() \
                or len(str(value).strip()) > 12:
            raise WorkError("bad_input", f"{what} id must be a number")
        return int(str(value).strip())

    def _session_by_id(self, con: sqlite3.Connection, sid: int) -> dict[str, Any]:
        row = con.execute("SELECT s.*, j.name AS job_name FROM sessions s JOIN jobs j ON j.id=s.job_id"
                          " WHERE s.id=?", (sid,)).fetchone()
        return self._session_dict(row, row["job_name"], self.now())

    def _range(self, frm: Any, to: Any, *, default_days: int) -> tuple[datetime, datetime]:
        now = self.now()
        hi = self._ts(to) if to is not None else now + timedelta(days=1)
        lo = self._ts(frm) if frm is not None else hi - timedelta(days=default_days)
        if lo >= hi:
            raise WorkError("bad_input", "from must be before to")
        if hi - lo > timedelta(days=MAX_RANGE_DAYS):
            raise WorkError("bad_input", f"a range is at most {MAX_RANGE_DAYS} days")
        return lo, hi

    @staticmethod
    def _limit(limit: Any) -> int:
        if limit is None:
            return DEFAULT_LIST
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIST:
            raise WorkError("bad_input", f"limit must be 1-{MAX_LIST}")
        return limit

    def list_sessions(self, job: Any = None, frm: Any = None, to: Any = None, *,
                      include_deleted: bool = False, limit: Any = None) -> list[dict[str, Any]]:
        lo, hi = self._range(frm, to, default_days=35)
        n = self._limit(limit)
        with self._read() as con:
            args: list[Any] = [iso(hi), iso(lo)]
            where = "s.start_ts < ? AND (s.end_ts IS NULL OR s.end_ts > ?)"
            if job is not None:
                where += " AND s.job_id=?"
                args.append(self._job(con, job)["id"])
            if not include_deleted:
                where += " AND s.deleted_ts IS NULL"
            rows = con.execute(f"SELECT s.*, j.name AS job_name FROM sessions s JOIN jobs j"
                               f" ON j.id=s.job_id WHERE {where} ORDER BY s.start_ts DESC, s.id DESC"
                               f" LIMIT ?", (*args, n)).fetchall()
            now = self.now()
            return [self._session_dict(r, r["job_name"], now) for r in rows]

    # ------------------------------------------------------------ log entries

    def add_log(self, ref: Any, kind: Any, text: Any = "", *, amount_cents: Any = None,
                ts: Any = None, allow_naive: bool = True) -> dict[str, Any]:
        if kind not in LOG_KINDS:
            raise WorkError("bad_input", "kind must be note, rubric or payout")
        if kind == "payout":
            if isinstance(amount_cents, bool) or not isinstance(amount_cents, int) \
                    or not 1 <= amount_cents <= MAX_PAYOUT_CENTS:
                raise WorkError("bad_input", "a payout needs an amount in whole cents, more than 0")
            body = self._clean(text, what="payout note", required=False)
        else:
            if amount_cents is not None:
                raise WorkError("bad_input", "only a payout has an amount")
            body = self._clean(text, what="rubric tip" if kind == "rubric" else "note")
        when = self._ts(ts, allow_naive=allow_naive) if ts is not None else self.now()
        if when < EARLIEST or when > self.now() + FUTURE_SKEW:
            raise WorkError("bad_input", "the time is before 2000 or in the future")
        with self._write() as con:
            job = self._job(con, ref)
            cur = con.execute(
                "INSERT INTO log(job_id, ts, kind, text, amount_cents, tz, created_ts)"
                " VALUES(?,?,?,?,?,?,?)",
                (job["id"], iso(when), kind, body, amount_cents, self._tz(), iso(self.now())))
            self._change(con, "log", cur.lastrowid, "create", None,
                         {"job_id": job["id"], "kind": kind, "amount_cents": amount_cents})
            return self._log_dict(con.execute("SELECT l.*, j.name AS job_name FROM log l JOIN jobs j"
                                              " ON j.id=l.job_id WHERE l.id=?", (cur.lastrowid,)).fetchone())

    @staticmethod
    def _log_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["job"] = d.pop("job_name")
        d["deleted"] = d["deleted_ts"] is not None
        return d

    def delete_log(self, log_id: Any) -> dict[str, Any]:
        """Soft, like a session: a mistaken payout is taken out of the totals, not erased."""
        lid = self._int_id(log_id, "log")
        with self._write() as con:
            row = con.execute("SELECT * FROM log WHERE id=? AND deleted_ts IS NULL", (lid,)).fetchone()
            if row is None:
                raise WorkError("not_found", "no such log entry")
            con.execute("UPDATE log SET deleted_ts=? WHERE id=?", (iso(self.now()), lid))
            self._change(con, "log", lid, "delete",
                         {k: row[k] for k in ("job_id", "ts", "kind", "amount_cents")}, None)
            return self._log_dict(con.execute("SELECT l.*, j.name AS job_name FROM log l JOIN jobs j"
                                              " ON j.id=l.job_id WHERE l.id=?", (lid,)).fetchone())

    def list_log(self, job: Any = None, kind: Any = None, frm: Any = None, to: Any = None, *,
                 limit: Any = None) -> list[dict[str, Any]]:
        if kind is not None and kind not in LOG_KINDS:
            raise WorkError("bad_input", "kind must be note, rubric or payout")
        lo, hi = self._range(frm, to, default_days=MAX_RANGE_DAYS - 1)
        n = self._limit(limit)
        with self._read() as con:
            where, args = "l.deleted_ts IS NULL AND l.ts >= ? AND l.ts < ?", [iso(lo), iso(hi)]
            if job is not None:
                where += " AND l.job_id=?"
                args.append(self._job(con, job)["id"])
            if kind is not None:
                where += " AND l.kind=?"
                args.append(kind)
            rows = con.execute(f"SELECT l.*, j.name AS job_name FROM log l JOIN jobs j ON j.id=l.job_id"
                               f" WHERE {where} ORDER BY l.ts DESC, l.id DESC LIMIT ?", (*args, n))
            return [self._log_dict(r) for r in rows.fetchall()]

    # ------------------------------------------------------------ summaries

    def period_bounds(self, at: datetime | None = None) -> dict[str, Bounds]:
        """Local today / week (Monday) / month / year around ``at``, as UTC instants."""
        at = at or self.now()
        z = self.zone
        d = z.to_local(at).date()
        monday = d - timedelta(days=d.weekday())
        nxt_month = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        return {
            "today": Bounds(z.day_start(d), z.day_start(d + timedelta(days=1))),
            "week": Bounds(z.day_start(monday), z.day_start(monday + timedelta(days=7))),
            "month": Bounds(z.day_start(date(d.year, d.month, 1)), z.day_start(nxt_month)),
            "year": Bounds(z.day_start(date(d.year, 1, 1)), z.day_start(date(d.year + 1, 1, 1))),
        }

    def _seconds_in(self, sessions: list[sqlite3.Row], lo: datetime, hi: datetime,
                    now: datetime) -> int:
        """Whole seconds of the sessions inside [lo, hi): a session is clipped to it, an open
        timer counts up to now, and an open timer past STALE_HOURS counts for nothing."""
        total = 0
        for r in sessions:
            start = from_iso(r["start_ts"])
            if r["end_ts"] is None:
                if now - start > timedelta(hours=STALE_HOURS):
                    continue
                end = now
            else:
                end = from_iso(r["end_ts"])
            a, b = max(start, lo), min(end, hi)
            if b > a:
                total += int((b - a).total_seconds())
        return total

    def _period(self, job: sqlite3.Row, sessions: list[sqlite3.Row], payouts: list[sqlite3.Row],
                lo: datetime, hi: datetime, now: datetime) -> dict[str, Any]:
        seconds = self._seconds_in(sessions, lo, hi, now)
        paid = sum(p["amount_cents"] for p in payouts if iso(lo) <= p["ts"] < iso(hi))
        rate = job["hourly_rate_cents"]
        earned = earned_cents(seconds, rate) if rate is not None else None
        out: dict[str, Any] = {"seconds": seconds, "hours": hours(seconds), "earned_cents": earned,
                               "payout_cents": paid}
        if job["kind"] == "freelance":
            # payouts / hours: what the work has actually paid, informational (pay lags work)
            out["effective_hourly_cents"] = ((paid * 3600 + seconds // 2) // seconds
                                             if seconds > 0 and paid > 0 else None)
        if job["set_aside_pct"] is not None:
            base = paid if job["kind"] == "freelance" else (earned or 0)
            out["set_aside_cents"] = (base * job["set_aside_pct"] + 50) // 100
        return out

    def summary(self, at: datetime | None = None) -> dict[str, Any]:
        now = (at or self.now()).astimezone(UTC).replace(microsecond=0)
        bounds = self.period_bounds(now)
        with self._read() as con:
            jobs = con.execute("SELECT * FROM jobs ORDER BY id").fetchall()
            all_sessions = con.execute("SELECT * FROM sessions WHERE deleted_ts IS NULL").fetchall()
            all_payouts = con.execute("SELECT * FROM log WHERE deleted_ts IS NULL AND kind='payout'").fetchall()
        local_today = self.zone.to_local(now).date()
        this_monday = local_today - timedelta(days=local_today.weekday())
        first_monday = this_monday - timedelta(weeks=WEEKLY_ROLLUP_WEEKS - 1)
        out_jobs: list[dict[str, Any]] = []
        timers: list[dict[str, Any]] = []
        totals: dict[str, dict[str, Any]] = {
            p: {"seconds": 0, "hours": 0.0, "earned_cents_by_currency": {}, "unrated_seconds": 0,
                "payout_cents_by_currency": {}} for p in (*bounds, "all")}
        for job in jobs:
            sess = [s for s in all_sessions if s["job_id"] == job["id"]]
            pays = [p for p in all_payouts if p["job_id"] == job["id"]]
            periods = {name: self._period(job, sess, pays, b.lo, b.hi, now) for name, b in bounds.items()}
            periods["all"] = self._period(job, sess, pays, EARLIEST, now + timedelta(days=1), now)
            weekly = []
            for i in range(WEEKLY_ROLLUP_WEEKS):
                wlo_local = first_monday + timedelta(weeks=i)
                lo, hi = self.zone.day_start(wlo_local), self.zone.day_start(wlo_local + timedelta(days=7))
                p = self._period(job, sess, pays, lo, hi, now)
                weekly.append({"week_start": wlo_local.isoformat(), "seconds": p["seconds"],
                               "hours": p["hours"], "earned_cents": p["earned_cents"],
                               "payout_cents": p["payout_cents"]})
            open_row = next((s for s in sess if s["end_ts"] is None), None)
            if open_row is not None:
                start = from_iso(open_row["start_ts"])
                timers.append({"job_id": job["id"], "job": job["name"], "session_id": open_row["id"],
                               "start_ts": open_row["start_ts"],
                               "elapsed_seconds": int((now - start).total_seconds()),
                               "flagged": now - start > timedelta(hours=STALE_HOURS)})
            for name, p in periods.items():
                t = totals[name]
                t["seconds"] += p["seconds"]
                cur = job["currency"]
                if p["payout_cents"]:
                    t["payout_cents_by_currency"][cur] = t["payout_cents_by_currency"].get(cur, 0) + p["payout_cents"]
                if p["earned_cents"] is None:
                    t["unrated_seconds"] += p["seconds"]
                else:
                    t["earned_cents_by_currency"][cur] = t["earned_cents_by_currency"].get(cur, 0) + p["earned_cents"]
            out_jobs.append({"id": job["id"], "name": job["name"], "kind": job["kind"],
                             "currency": job["currency"], "active": bool(job["active"]),
                             "hourly_rate_cents": job["hourly_rate_cents"],
                             "rate_set": job["hourly_rate_cents"] is not None,
                             "set_aside_pct": job["set_aside_pct"],
                             "timer_running": open_row is not None,
                             "periods": periods, "weekly": weekly})
        for t in totals.values():
            t["hours"] = hours(t["seconds"])
        return {
            "generated_at": iso(now),
            "tz": self.zone.label(now),
            "periods": {n: {"from": iso(b.lo), "to": iso(b.hi)} for n, b in bounds.items()},
            "jobs": out_jobs,
            "totals": totals,
            "timers": timers,
            "flags": [{"kind": "stale_timer", **t} for t in timers if t["flagged"]],
        }

    # ------------------------------------------------------------ doctor / capability

    def status(self) -> dict[str, Any]:
        """For doctor: is the db there, how many timers are open, when was it last written.
        Counts and times only - no text."""
        if not self.exists():
            return {"present": False, "jobs": 0, "open_timers": 0, "flagged_timers": 0,
                    "last_write": None}
        now = self.now()
        with self._read() as con:
            jobs = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            opens = con.execute("SELECT start_ts FROM sessions WHERE end_ts IS NULL"
                                " AND deleted_ts IS NULL").fetchall()
            last = con.execute("SELECT value FROM meta WHERE key='last_write'").fetchone()
        return {"present": True, "jobs": jobs, "open_timers": len(opens),
                "flagged_timers": sum(1 for r in opens
                                      if now - from_iso(r["start_ts"]) > timedelta(hours=STALE_HOURS)),
                "last_write": last["value"] if last else None}

    def aggregates(self) -> dict[str, Any]:
        """What Moss may see: hours and estimated earnings per job for today / week / month,
        and whether a timer is running. No note, no rubric, no payout text - by construction:
        this reads only the summary's numbers and the job's own name."""
        s = self.summary()
        return {
            "generated_at": s["generated_at"],
            "tz": s["tz"],
            "jobs": [{
                "job": j["name"], "kind": j["kind"], "currency": j["currency"],
                "rate_set": j["rate_set"], "timer_running": j["timer_running"],
                **{p: {"hours": j["periods"][p]["hours"], "earned_cents": j["periods"][p]["earned_cents"]}
                   for p in ("today", "week", "month")},
            } for j in s["jobs"]],
            "totals": {p: {"hours": s["totals"][p]["hours"],
                           "earned_cents_by_currency": s["totals"][p]["earned_cents_by_currency"]}
                       for p in ("today", "week", "month")},
            "timer_running": bool(s["timers"]),
            "stale_timer": bool(s["flags"]),
        }


def zone_or_local(name: str | None) -> tuple[Zone, str | None]:
    """The configured zone, or the machine's with a note when the name is not a zone: a typo
    in PIONIR_WORK_TZ must not stop Pionir booting, and doctor says it fell back."""
    try:
        return Zone(name), None
    except WorkError as error:
        return Zone(None), str(error)


def worklog_for(settings: Any, *, clock: Any = None,
                known: Callable[[], Iterable[str]] = tuple) -> WorkLog:
    """The work log for a PionirSettings (its zone from PIONIR_WORK_TZ, else the machine's)."""
    return WorkLog(settings.worklog_path, clock=clock, zone=zone_or_local(settings.work_tz)[0],
                   known=known)
