"""The crew's one record: every run, every output, every report, every model call.

One SQLite file. Five decisions, each paid for somewhere in this estate before:

**Every attempt is a row** (``runs``), successful or not, and including the ones that
returned nothing. Health is derived from this table rather than from the absence of
outputs, because absence is exactly what a quiet source and a dead worker have in
common. ``record_attempt`` writes the outputs AND the run row in one transaction, so a
run can never be recorded without what it produced or vice versa (Peter's vault).

**Silent success is first-class.** A run that succeeded and wrote nothing is counted
(``Health.silent_streak``). It is the shape of "runs but produces nothing", the
estate's most expensive failure, and it reads as green everywhere else.

**Outputs are idempotent on the fact** (``INSERT OR IGNORE`` on ``output_id``), and
their figures are TYPED (figures.py). An output a model helped write is ``derived`` and
is never offered as backing for a report.

**One writer.** Workers run concurrently (pool.py), so writes would otherwise arrive
from many threads at once. Every write is handed to ONE writer thread over a queue and
the caller waits for it to commit; the store never sees two writers, and a read after a
write always sees it. Reads use their own connection (WAL lets them run beside the
writer). ``write_threads`` records every thread that ever executed a write statement,
from SQLite's own trace hook - it must only ever hold the writer.

**No money lever.** There is no table or column here through which anything could
allocate or spend real money. Revenue appears only as figures a worker READ.
"""
from __future__ import annotations

import json
import queue
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path

from .figures import Figure
from .worker import STALE_AFTER_CADENCES, ErrorKind, Output, WorkerError

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pauses (
    id INTEGER PRIMARY KEY, stopped_real REAL NOT NULL, resumed_real REAL NOT NULL,
    t INTEGER NOT NULL, reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brain_calls (
    id INTEGER PRIMARY KEY, real_t REAL NOT NULL, t INTEGER NOT NULL, agent TEXT NOT NULL,
    division TEXT, purpose TEXT NOT NULL, prompt_tokens INTEGER, out_tokens INTEGER,
    seconds REAL, ok INTEGER NOT NULL, note TEXT
);
CREATE INDEX IF NOT EXISTS ix_calls_real ON brain_calls(real_t);
CREATE INDEX IF NOT EXISTS ix_calls_div ON brain_calls(division, real_t);

-- Every attempt, not every success: what tells a dead worker from a quiet one.
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT, worker_id TEXT NOT NULL, division TEXT NOT NULL,
    started_at REAL NOT NULL, finished_at REAL NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('ok', 'err')),
    error_kind TEXT, error_message TEXT, yielded INTEGER NOT NULL DEFAULT 0,
    written INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_runs_worker ON runs(worker_id, run_id);

CREATE TABLE IF NOT EXISTS outputs (
    output_id TEXT PRIMARY KEY, worker_id TEXT NOT NULL, division TEXT NOT NULL,
    kind TEXT NOT NULL, valid_at REAL NOT NULL, observed_at REAL NOT NULL,
    payload TEXT NOT NULL, figures TEXT NOT NULL, entities TEXT NOT NULL,
    provenance TEXT NOT NULL, derived INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_out_div_kind ON outputs(division, kind, observed_at);
CREATE INDEX IF NOT EXISTS ix_out_worker ON outputs(worker_id, observed_at);

-- What leaders send up: a report, an abstention, or a report that was rejected.
CREATE TABLE IF NOT EXISTS reports (
    report_id INTEGER PRIMARY KEY AUTOINCREMENT, division TEXT NOT NULL,
    written_at REAL NOT NULL, status TEXT NOT NULL
        CHECK (status IN ('report', 'abstained', 'rejected')),
    stamp REAL, headline TEXT NOT NULL, summary TEXT NOT NULL, attention TEXT NOT NULL,
    figures TEXT NOT NULL, routine TEXT NOT NULL, blocking INTEGER NOT NULL,
    reason TEXT NOT NULL, provenance TEXT NOT NULL, escalation TEXT
);
CREATE INDEX IF NOT EXISTS ix_reports_div ON reports(division, report_id);

-- Direction down from Moss: a division's goal and priority, and compute shares.
CREATE TABLE IF NOT EXISTS directions (
    id INTEGER PRIMARY KEY, t REAL NOT NULL, division TEXT NOT NULL, goal TEXT NOT NULL,
    priority INTEGER NOT NULL, by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS allocations (
    id INTEGER PRIMARY KEY, t REAL NOT NULL, resource TEXT NOT NULL, shares TEXT NOT NULL,
    by TEXT NOT NULL
);
-- Every Claude escalation ATTEMPT (each one costs usage, answered or not).
CREATE TABLE IF NOT EXISTS escalations (
    id INTEGER PRIMARY KEY, t REAL NOT NULL, day TEXT NOT NULL, division TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'ok', 'failed')), note TEXT
);
CREATE INDEX IF NOT EXISTS ix_esc_day ON escalations(day, division);
"""

CALL_CAP = 20000
PAUSE_CAP = 500
RUN_CAP = 100_000
OUTPUTS_PER_WORKER = 2000
REPORT_CAP = 5000
PRUNE_EVERY = 200

_WRITE_VERBS = ("INSERT", "UPDATE", "DELETE", "REPLACE")


@dataclass(frozen=True)
class Health:
    """A worker's (or leader's) liveness, as a value to reason about - not a colour."""

    worker_id: str
    cadence_seconds: int
    attempts: int
    last_success_at: float | None
    last_attempt_at: float | None
    consecutive_failures: int
    is_stale: bool
    last_outcome: str | None
    last_error_kind: str | None
    last_error: str | None
    last_written: int
    silent_streak: int          # ok runs in a row that wrote nothing

    @property
    def has_never_succeeded(self) -> bool:
        """The state a brand-new worker and a permanently broken one share. Worth its own
        name: wired, tested and never once producing a row is this estate's single most
        expensive failure, and it reads identically to one that is merely young."""
        return self.last_success_at is None

    @property
    def silent_success(self) -> bool:
        """It succeeded, and produced nothing. Not an error - and also exactly what a
        frozen source looks like; a run of them is what tells the two apart."""
        return self.last_outcome == "ok" and self.last_written == 0

    @property
    def not_configured(self) -> bool:
        return self.last_error_kind == ErrorKind.NOT_CONFIGURED

    @property
    def not_wired(self) -> bool:
        return self.last_error_kind == ErrorKind.NOT_WIRED

    def to_dict(self) -> dict:
        return {"worker_id": self.worker_id, "attempts": self.attempts,
                "last_success_at": self.last_success_at, "last_attempt_at": self.last_attempt_at,
                "consecutive_failures": self.consecutive_failures, "stale": self.is_stale,
                "never_succeeded": self.has_never_succeeded,
                "silent_success": self.silent_success, "silent_streak": self.silent_streak,
                "last_error_kind": self.last_error_kind, "last_error": self.last_error}


def _output_row(r) -> Output:
    return Output(output_id=r[0], worker_id=r[1], division=r[2], kind=r[3], valid_at=r[4],
                  observed_at=r[5], payload=json.loads(r[6]),
                  figures=tuple(Figure.from_dict(f) for f in json.loads(r[7])),
                  entities=tuple(json.loads(r[8])), provenance=json.loads(r[9]))


_OUT_COLS = ("output_id, worker_id, division, kind, valid_at, observed_at, payload, figures, "
             "entities, provenance")
_REPORT_COLS = ("report_id", "division", "written_at", "status", "stamp", "headline",
                "summary", "attention", "figures", "routine", "blocking", "reason",
                "provenance", "escalation")


def _report_row(r) -> dict:
    d = dict(zip(_REPORT_COLS, r))
    d["figures"] = json.loads(d["figures"])
    d["routine"] = json.loads(d["routine"])
    d["blocking"] = bool(d["blocking"])
    d["provenance"] = json.loads(d["provenance"])
    d["escalation"] = json.loads(d["escalation"]) if d["escalation"] else None
    return d


class CrewStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._wconn = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
        self._wconn.execute("PRAGMA journal_mode=WAL")
        self._wconn.execute("PRAGMA synchronous=NORMAL")
        self._wconn.executescript(SCHEMA)
        self._wconn.commit()
        self._rconn = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
        self._rlock = threading.Lock()
        # Every thread that ever executed a write statement, on EITHER connection, as seen
        # by SQLite's own trace hook. With one writer this is exactly {writer}.
        self.write_threads: set = set()
        self._wconn.set_trace_callback(self._traced)
        self._rconn.set_trace_callback(self._traced)
        self._q: queue.Queue = queue.Queue()
        self._closed = False
        self._writer = threading.Thread(target=self._write_loop, name="pionir-crew-store-writer",
                                        daemon=True)
        self._writer.start()
        self.writes = 0

    # ---- the one writer ---------------------------------------------------
    def _traced(self, statement: str) -> None:
        if statement.lstrip().upper().startswith(_WRITE_VERBS):
            self.write_threads.add(threading.get_ident())

    @property
    def writer_ident(self) -> int | None:
        return self._writer.ident

    def _write_loop(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            fn, fut = item
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                with self._wconn:            # one transaction: commit, or roll back
                    result = fn(self._wconn)
                self.writes += 1
                fut.set_result(result)
            except BaseException as exc:  # noqa: BLE001 - handed back to the caller
                fut.set_exception(exc)

    def _write(self, fn: Callable[[sqlite3.Connection], object]):
        """Run ``fn(conn)`` on the writer thread, in a transaction, and wait for it."""
        if threading.get_ident() == self._writer.ident:
            return fn(self._wconn)
        if self._closed:
            raise RuntimeError("the crew store is closed")
        fut: Future = Future()
        self._q.put((fn, fut))
        return fut.result()

    def _read(self, sql: str, args: Sequence = ()) -> list:
        with self._rlock:
            return self._rconn.execute(sql, tuple(args)).fetchall()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._q.put(None)
        self._writer.join(timeout=10)
        with self._rlock:
            self._rconn.close()
        self._wconn.close()

    # ---- meta, pauses -------------------------------------------------------
    def get(self, k: str, default=None):
        rows = self._read("SELECT v FROM meta WHERE k=?", (k,))
        return json.loads(rows[0][0]) if rows else default

    def set(self, k: str, v) -> None:
        self._write(lambda c: c.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)",
                                        (k, json.dumps(v))))

    def checkpoint(self, t: int, extra: dict | None = None,
                   saved_real: float | None = None) -> None:
        items = {"t": t, "saved_real": time.time() if saved_real is None else saved_real,
                 **(extra or {})}

        def fn(c):
            for k, v in items.items():
                c.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", (k, json.dumps(v)))
        self._write(fn)

    def record_pause(self, stopped_real: float, resumed_real: float, t: int, reason: str) -> None:
        def fn(c):
            c.execute("INSERT INTO pauses(stopped_real, resumed_real, t, reason) VALUES(?,?,?,?)",
                      (stopped_real, resumed_real, t, reason))
            c.execute("DELETE FROM pauses WHERE id NOT IN "
                      "(SELECT id FROM pauses ORDER BY id DESC LIMIT ?)", (PAUSE_CAP,))
        self._write(fn)

    def last_pause(self) -> dict | None:
        rows = self._read("SELECT stopped_real, resumed_real, t, reason FROM pauses "
                          "ORDER BY id DESC LIMIT 1")
        if not rows:
            return None
        r = rows[0]
        return {"stopped_real": r[0], "resumed_real": r[1], "t": r[2], "reason": r[3],
                "seconds": r[1] - r[0]}

    # ---- the brain's ledger -------------------------------------------------
    def add_call(self, t: int, agent: str, purpose: str, prompt_tokens: int | None,
                 out_tokens: int | None, seconds: float, ok: bool, note: str | None = None,
                 division: str | None = None) -> None:
        def fn(c):
            cur = c.execute(
                "INSERT INTO brain_calls(real_t, t, agent, division, purpose, prompt_tokens, "
                "out_tokens, seconds, ok, note) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (time.time(), t, agent, division, purpose, prompt_tokens, out_tokens, seconds,
                 1 if ok else 0, note))
            if cur.lastrowid % 500 == 0:
                c.execute("DELETE FROM brain_calls WHERE id NOT IN "
                          "(SELECT id FROM brain_calls ORDER BY id DESC LIMIT ?)", (CALL_CAP,))
        self._write(fn)

    def calls_last_hour(self, division: str | None = None, now: float | None = None) -> int:
        since = (time.time() if now is None else now) - 3600
        if division is None:
            return self._read("SELECT COUNT(*) FROM brain_calls WHERE real_t > ?", (since,))[0][0]
        return self._read("SELECT COUNT(*) FROM brain_calls WHERE division=? AND real_t > ?",
                          (division, since))[0][0]

    def calls_since(self, since_t: int) -> dict:
        """Calls and tokens per caller since ``since_t`` (e.g. local midnight)."""
        rows = self._read(
            "SELECT agent, COUNT(*), COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(out_tokens),0) "
            "FROM brain_calls WHERE t >= ? GROUP BY agent", (since_t,))
        return {r[0]: {"calls": r[1], "prompt_tokens": r[2], "out_tokens": r[3]} for r in rows}

    # ---- runs and outputs ---------------------------------------------------
    def record_attempt(self, *, worker_id: str, division: str, started_at: float,
                       finished_at: float, error: WorkerError | None,
                       outputs: Iterable[Output] = (), written: int | None = None) -> int:
        """Record one attempt and what it produced, in one transaction. Call this on EVERY
        attempt: not calling it on the failure path is the same bug as swallowing the
        exception, one layer down. Returns how many outputs were new. ``written`` is for
        a leader, whose product is a report row rather than outputs."""
        override = written
        outputs = list(outputs)
        rows = [(o.output_id, o.worker_id, o.division, o.kind, o.valid_at, o.observed_at,
                 json.dumps(o.payload, sort_keys=True, default=str),
                 json.dumps([f.to_dict() for f in o.figures]), json.dumps(list(o.entities)),
                 json.dumps(o.provenance, sort_keys=True, default=str), 1 if o.derived else 0)
                for o in outputs]

        def fn(c):
            before = c.total_changes
            if rows:
                c.executemany(f"INSERT OR IGNORE INTO outputs ({_OUT_COLS}, derived) "
                              "VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
            written = c.total_changes - before if override is None else int(override)
            cur = c.execute(
                "INSERT INTO runs (worker_id, division, started_at, finished_at, outcome, "
                "error_kind, error_message, yielded, written) VALUES (?,?,?,?,?,?,?,?,?)",
                (worker_id, division, started_at, finished_at, "err" if error else "ok",
                 str(error.kind) if error else None, error.message[:1000] if error else None,
                 len(rows), written))
            if cur.lastrowid % PRUNE_EVERY == 0:
                c.execute("DELETE FROM runs WHERE run_id <= ?", (cur.lastrowid - RUN_CAP,))
                c.execute("DELETE FROM outputs WHERE worker_id=? AND output_id NOT IN (SELECT "
                          "output_id FROM outputs WHERE worker_id=? ORDER BY observed_at DESC "
                          "LIMIT ?)", (worker_id, worker_id, OUTPUTS_PER_WORKER))
            return written
        return self._write(fn)

    def last_attempts(self) -> dict:
        """When each worker was last ATTEMPTED - a failing one must still be paced."""
        return {r[0]: r[1] for r in self._read(
            "SELECT worker_id, MAX(finished_at) FROM runs GROUP BY worker_id")}

    def health(self, cadences: dict, now: float) -> list:
        """Per-worker liveness. ``cadences`` comes from the registry, so a worker that has
        never once run is still reported (never succeeded) rather than being absent."""
        out = []
        for wid, cadence in sorted(cadences.items()):
            last_ok, last_any, attempts = self._read(
                "SELECT MAX(CASE WHEN outcome='ok' THEN finished_at END), MAX(finished_at), "
                "COUNT(*) FROM runs WHERE worker_id=?", (wid,))[0]
            fails = self._read(
                "SELECT COUNT(*) FROM runs WHERE worker_id=? AND run_id > COALESCE("
                "(SELECT MAX(run_id) FROM runs WHERE worker_id=? AND outcome='ok'), 0)",
                (wid, wid))[0][0]
            silent = self._read(
                "SELECT COUNT(*) FROM runs WHERE worker_id=? AND outcome='ok' AND written=0 "
                "AND run_id > COALESCE((SELECT MAX(run_id) FROM runs WHERE worker_id=? "
                "AND (written > 0 OR outcome='err')), 0)", (wid, wid))[0][0]
            last = self._read("SELECT outcome, error_kind, error_message, written FROM runs "
                              "WHERE worker_id=? ORDER BY run_id DESC LIMIT 1", (wid,))
            outcome, ekind, emsg, written = last[0] if last else (None, None, None, 0)
            deadline = now - cadence * STALE_AFTER_CADENCES
            out.append(Health(wid, int(cadence), int(attempts), last_ok, last_any, int(fails),
                              last_ok is None or last_ok < deadline, outcome, ekind, emsg,
                              int(written or 0), int(silent)))
        return out

    def read_outputs(self, *, division: str | None = None, worker_id: str | None = None,
                     kind: str | None = None, since: float | None = None,
                     real_only: bool = False, limit: int = 100) -> list:
        """Newest sighting first. ``limit`` is mandatory-by-default: a leader pulls what it
        asks for; it never drinks the store."""
        where, args = ["1=1"], []
        for col, val in (("division", division), ("worker_id", worker_id), ("kind", kind)):
            if val is not None:
                where.append(f"{col} = ?")
                args.append(val)
        if since is not None:
            where.append("observed_at > ?")
            args.append(since)
        if real_only:
            where.append("derived = 0")
        args.append(limit)
        rows = self._read(f"SELECT {_OUT_COLS} FROM outputs WHERE {' AND '.join(where)} "
                          "ORDER BY observed_at DESC, valid_at DESC, output_id DESC LIMIT ?", args)
        return [_output_row(r) for r in rows]

    def output_kinds(self, division: str) -> list:
        return [r[0] for r in self._read(
            "SELECT DISTINCT kind FROM outputs WHERE division=? ORDER BY kind", (division,))]

    def count_outputs(self, division: str | None = None) -> int:
        if division is None:
            return self._read("SELECT COUNT(*) FROM outputs")[0][0]
        return self._read("SELECT COUNT(*) FROM outputs WHERE division=?", (division,))[0][0]

    def runs(self, worker_id: str, limit: int = 20) -> list:
        rows = self._read("SELECT started_at, finished_at, outcome, error_kind, error_message, "
                          "yielded, written FROM runs WHERE worker_id=? ORDER BY run_id DESC "
                          "LIMIT ?", (worker_id, limit))
        keys = ("started_at", "finished_at", "outcome", "error_kind", "error_message",
                "yielded", "written")
        return [dict(zip(keys, r)) for r in rows]

    # ---- reports ------------------------------------------------------------
    def add_report(self, *, division: str, written_at: float, status: str, stamp: float | None,
                   headline: str = "", summary: str = "", attention: str = "none",
                   figures: Iterable[Figure] = (), routine: Iterable[str] = (),
                   blocking: bool = False, reason: str = "", provenance: dict | None = None,
                   escalation: dict | None = None) -> int:
        args = (division, written_at, status, stamp, headline, summary, attention,
                json.dumps([f.to_dict() for f in figures]), json.dumps(list(routine)),
                1 if blocking else 0, reason, json.dumps(provenance or {}, default=str),
                json.dumps(escalation) if escalation is not None else None)

        def fn(c):
            cur = c.execute(
                "INSERT INTO reports (division, written_at, status, stamp, headline, summary, "
                "attention, figures, routine, blocking, reason, provenance, escalation) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", args)
            if cur.lastrowid % PRUNE_EVERY == 0:
                c.execute("DELETE FROM reports WHERE report_id <= ?", (cur.lastrowid - REPORT_CAP,))
            return cur.lastrowid
        return self._write(fn)

    def reports(self, *, division: str | None = None, limit: int = 20,
                statuses: Sequence[str] | None = None) -> list:
        where, args = ["1=1"], []
        if division is not None:
            where.append("division = ?")
            args.append(division)
        if statuses:
            where.append(f"status IN ({','.join('?' * len(statuses))})")
            args.extend(statuses)
        args.append(limit)
        rows = self._read(f"SELECT {', '.join(_REPORT_COLS)} FROM reports WHERE "
                          f"{' AND '.join(where)} ORDER BY report_id DESC LIMIT ?", args)
        return [_report_row(r) for r in rows]

    def last_considered_at(self, division: str) -> float | None:
        """When this division's leader last wrote anything at all (report, abstention or
        rejection): nothing newer than this is nothing new."""
        return self._read("SELECT MAX(written_at) FROM reports WHERE division=?",
                          (division,))[0][0]

    # ---- direction from Moss ------------------------------------------------
    def set_direction(self, *, division: str, goal: str, priority: int, by: str, t: float) -> None:
        self._write(lambda c: c.execute(
            "INSERT INTO directions (t, division, goal, priority, by) VALUES (?,?,?,?,?)",
            (t, division, goal, priority, by)))

    def directions(self) -> dict:
        rows = self._read("SELECT d.division, d.goal, d.priority, d.by, d.t FROM directions d "
                          "JOIN (SELECT division, MAX(id) m FROM directions GROUP BY division) x "
                          "ON d.id = x.m")
        return {r[0]: {"goal": r[1], "priority": r[2], "by": r[3], "t": r[4]} for r in rows}

    def set_allocation(self, *, resource: str, shares: dict, by: str, t: float) -> None:
        self._write(lambda c: c.execute(
            "INSERT INTO allocations (t, resource, shares, by) VALUES (?,?,?,?)",
            (t, resource, json.dumps(shares, sort_keys=True), by)))

    def allocations(self) -> dict:
        rows = self._read("SELECT a.resource, a.shares, a.by, a.t FROM allocations a JOIN "
                          "(SELECT resource, MAX(id) m FROM allocations GROUP BY resource) x "
                          "ON a.id = x.m")
        return {r[0]: {"shares": json.loads(r[1]), "by": r[2], "t": r[3]} for r in rows}

    # ---- Claude escalations -------------------------------------------------
    def reserve_escalation(self, *, day: str, division: str, t: float, global_cap: int,
                           division_cap: int) -> tuple:
        """Check both caps and take a slot in ONE writer transaction, so two leaders can
        never both take the last one. -> (ok, reason, id)."""
        def fn(c):
            used = c.execute("SELECT COUNT(*) FROM escalations WHERE day=?", (day,)).fetchone()[0]
            if used >= global_cap:
                return False, f"the daily Claude cap of {global_cap} is used up ({used} today)", None
            mine = c.execute("SELECT COUNT(*) FROM escalations WHERE day=? AND division=?",
                             (day, division)).fetchone()[0]
            if mine >= division_cap:
                why = (f"{division}'s share of the Claude cap is {division_cap} a day and "
                       f"{mine} are used")
                return False, why, None
            cur = c.execute("INSERT INTO escalations (t, day, division, status) "
                            "VALUES (?,?,?,'running')", (t, day, division))
            return True, "", cur.lastrowid
        return self._write(fn)

    def finish_escalation(self, esc_id: int, ok: bool, note: str = "") -> None:
        self._write(lambda c: c.execute("UPDATE escalations SET status=?, note=? WHERE id=?",
                                        ("ok" if ok else "failed", note[:500], esc_id)))

    def escalations_on(self, day: str, division: str | None = None) -> int:
        if division is None:
            return self._read("SELECT COUNT(*) FROM escalations WHERE day=?", (day,))[0][0]
        return self._read("SELECT COUNT(*) FROM escalations WHERE day=? AND division=?",
                          (day, division))[0][0]
