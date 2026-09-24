"""Persistence for the crew as a whole: one SQLite file, written atomically.

Each agent's own memory is its own file (memory.py); this one holds only what
is shared - the checkpoint meta, the pause record, the transcript of what was
said in which channel, and the ledger of every model call, which is what the
hourly ceiling is counted against.

Nothing is worked for time the process was not running; the pause is recorded
in ``pauses`` so a viewer can show it. Times are wall-clock epoch seconds
(``t``); Hearth's ``game_t`` is gone with the game clock, and its ``room`` is
now ``channel``.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pauses (
    id INTEGER PRIMARY KEY, stopped_real REAL NOT NULL, resumed_real REAL NOT NULL,
    t INTEGER NOT NULL, reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS utterances (
    id INTEGER PRIMARY KEY, t INTEGER NOT NULL, real_t REAL NOT NULL,
    speaker TEXT NOT NULL, channel TEXT NOT NULL, target TEXT, text TEXT NOT NULL,
    conv_id INTEGER, frame TEXT
);
CREATE TABLE IF NOT EXISTS brain_calls (
    id INTEGER PRIMARY KEY, real_t REAL NOT NULL, t INTEGER NOT NULL, agent TEXT NOT NULL,
    purpose TEXT NOT NULL, prompt_tokens INTEGER, out_tokens INTEGER, seconds REAL,
    ok INTEGER NOT NULL, note TEXT
);
CREATE INDEX IF NOT EXISTS ix_utt_t ON utterances(t);
CREATE INDEX IF NOT EXISTS ix_calls_real ON brain_calls(real_t);
"""

UTTERANCE_CAP = 20000     # rows kept; older are deleted (each agent keeps its own memory anyway)
CALL_CAP = 20000
PAUSE_CAP = 500


class CrewStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, timeout=5)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def get(self, k: str, default=None):
        with self.lock:
            row = self.conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, k: str, v) -> None:
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", (k, json.dumps(v)))
            self.conn.commit()

    def checkpoint(self, t: int, extra: dict | None = None,
                   saved_real: float | None = None) -> None:
        """``saved_real`` is the crew clock's reading, so the gap a restart records is
        measured on the same clock that will measure the resume."""
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('t', ?)", (json.dumps(t),))
            cur.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('saved_real', ?)",
                        (json.dumps(time.time() if saved_real is None else saved_real),))
            for k, v in (extra or {}).items():
                cur.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", (k, json.dumps(v)))
            self.conn.commit()

    def record_pause(self, stopped_real: float, resumed_real: float, t: int, reason: str) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO pauses(stopped_real, resumed_real, t, reason) VALUES(?,?,?,?)",
                (stopped_real, resumed_real, t, reason),
            )
            self.conn.execute(
                "DELETE FROM pauses WHERE id NOT IN (SELECT id FROM pauses ORDER BY id DESC LIMIT ?)",
                (PAUSE_CAP,))
            self.conn.commit()

    def last_pause(self) -> dict | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT stopped_real, resumed_real, t, reason FROM pauses ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        return {"stopped_real": row[0], "resumed_real": row[1], "t": row[2], "reason": row[3],
                "seconds": row[1] - row[0]}

    def pauses(self, limit: int = 20) -> list:
        with self.lock:
            rows = self.conn.execute(
                "SELECT stopped_real, resumed_real, t, reason FROM pauses ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
        return [{"stopped_real": r[0], "resumed_real": r[1], "t": r[2], "reason": r[3],
                 "seconds": r[1] - r[0]} for r in rows][::-1]

    def add_utterance(self, t: int, speaker: str, channel: str, target: str | None, text: str,
                      conv_id: int | None, frame: str | None) -> int:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO utterances(t, real_t, speaker, channel, target, text, conv_id, frame) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (t, time.time(), speaker, channel, target, text, conv_id, frame),
            )
            if cur.lastrowid % 500 == 0:
                self.conn.execute(
                    "DELETE FROM utterances WHERE id NOT IN "
                    "(SELECT id FROM utterances ORDER BY id DESC LIMIT ?)", (UTTERANCE_CAP,))
            self.conn.commit()
            return cur.lastrowid

    def recent_utterances(self, limit: int = 60) -> list:
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, t, speaker, channel, target, text, conv_id, frame FROM utterances "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        keys = ("id", "t", "speaker", "channel", "target", "text", "conv_id", "frame")
        return [dict(zip(keys, r)) for r in rows][::-1]

    def count_utterances(self, speaker: str | None = None) -> int:
        with self.lock:
            if speaker:
                return self.conn.execute(
                    "SELECT COUNT(*) FROM utterances WHERE speaker=?", (speaker,)).fetchone()[0]
            return self.conn.execute("SELECT COUNT(*) FROM utterances").fetchone()[0]

    def add_call(self, t: int, agent: str, purpose: str, prompt_tokens: int | None,
                 out_tokens: int | None, seconds: float, ok: bool, note: str | None = None) -> None:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO brain_calls(real_t, t, agent, purpose, prompt_tokens, out_tokens, "
                "seconds, ok, note) VALUES(?,?,?,?,?,?,?,?,?)",
                (time.time(), t, agent, purpose, prompt_tokens, out_tokens, seconds,
                 1 if ok else 0, note),
            )
            if cur.lastrowid % 500 == 0:
                self.conn.execute(
                    "DELETE FROM brain_calls WHERE id NOT IN "
                    "(SELECT id FROM brain_calls ORDER BY id DESC LIMIT ?)", (CALL_CAP,))
            self.conn.commit()

    def calls_last_hour(self) -> int:
        with self.lock:
            return self.conn.execute(
                "SELECT COUNT(*) FROM brain_calls WHERE real_t > ?",
                (time.time() - 3600,)).fetchone()[0]

    def calls_since(self, since_t: int) -> dict:
        """Calls and tokens per agent since ``since_t`` (e.g. local midnight)."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT agent, COUNT(*), COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(out_tokens),0) "
                "FROM brain_calls WHERE t >= ? GROUP BY agent", (since_t,)).fetchall()
        return {r[0]: {"calls": r[1], "prompt_tokens": r[2], "out_tokens": r[3]} for r in rows}

    def close(self) -> None:
        with self.lock:
            self.conn.commit()
            self.conn.close()
