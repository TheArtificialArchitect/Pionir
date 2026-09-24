"""One agent's private memory: its own SQLite file, nothing shared.

A row exists only because an event this agent perceived, did, was told, felt
or inferred wrote it. Retrieval is lexical + recency + salience + person
match over this file alone. Growth is bounded: episodes older than
``FOLD_AFTER_DAYS`` with low salience fold into per-day summaries; a hard row
cap trims the least salient oldest rows.

Provenance is load-bearing. Every episode says HOW the agent came to know it
(``source``: seen / did / told / heard / inferred / felt / noticed), and the
anti-confabulation critic checks what an agent claims against that column: a
thing it was only told must not come out as a thing it saw. So an unknown
source is refused at the door rather than stored as something the critic
cannot read.

Ported from Hearth with two renames. Hearth's agents were in a ROOM; the
crew's are in a CHANNEL (a conversation, a queue, a project thread), so every
``room`` column and argument is ``channel``. And Hearth's ``game_t`` is ``t``,
wall-clock epoch seconds, because there is no game clock any more.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY, t INTEGER NOT NULL, channel TEXT, kind TEXT NOT NULL,
    actor TEXT, obj TEXT, detail TEXT NOT NULL, text TEXT NOT NULL, salience REAL NOT NULL,
    source TEXT NOT NULL, told_by TEXT, feeling TEXT, people TEXT
);
CREATE INDEX IF NOT EXISTS ix_ep_t ON episodes(t);
CREATE INDEX IF NOT EXISTS ix_ep_kind ON episodes(kind);
CREATE TABLE IF NOT EXISTS beliefs (
    obj TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, seen_t INTEGER NOT NULL,
    channel TEXT,
    PRIMARY KEY (obj, key)
);
CREATE TABLE IF NOT EXISTS people (
    other TEXT PRIMARY KEY, warmth REAL NOT NULL DEFAULT 0, trust REAL NOT NULL DEFAULT 0,
    familiarity REAL NOT NULL DEFAULT 0, grievance REAL NOT NULL DEFAULT 0,
    last_seen_t INTEGER, last_seen_channel TEXT, seen_doing TEXT NOT NULL DEFAULT '{}',
    talks INTEGER NOT NULL DEFAULT 0, channel_seconds REAL NOT NULL DEFAULT 0,
    unanswered INTEGER NOT NULL DEFAULT 0, addressed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS thoughts (
    id INTEGER PRIMARY KEY, t INTEGER NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
    about TEXT, urge REAL NOT NULL, sources TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0,
    topic TEXT NOT NULL DEFAULT '', say TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS intentions (
    id INTEGER PRIMARY KEY, t INTEGER NOT NULL, want TEXT NOT NULL, kind TEXT NOT NULL,
    target TEXT, status TEXT NOT NULL DEFAULT 'open', attempts INTEGER NOT NULL DEFAULT 0,
    result TEXT, done_t INTEGER, next_try_t INTEGER NOT NULL DEFAULT 0, thought_id INTEGER,
    frame TEXT
);
CREATE TABLE IF NOT EXISTS reflections (
    id INTEGER PRIMARY KEY, t INTEGER NOT NULL, day INTEGER NOT NULL, text TEXT NOT NULL,
    episode_ids TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS habits (
    action TEXT NOT NULL, channel TEXT NOT NULL, hour INTEGER NOT NULL,
    count INTEGER NOT NULL DEFAULT 0, reliability REAL NOT NULL DEFAULT 0,
    last_t INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (action, channel, hour)
);
CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY, day INTEGER NOT NULL, kind TEXT NOT NULL, channel TEXT,
    count INTEGER NOT NULL, text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS counters (k TEXT PRIMARY KEY, v INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""

# How an agent came to know a thing. The critic reads this; nothing else may be written.
SOURCES = frozenset({"seen", "did", "told", "heard", "inferred", "felt", "noticed"})

FOLD_AFTER_DAYS = 14
FOLD_SALIENCE_FLOOR = 2.0
EPISODE_CAP = 30000
THOUGHT_CAP = 3000
SUMMARY_CAP = 5000
DAY = 86400

_WORD = re.compile(r"[a-z']+")
_EP_COLS = "id, t, channel, kind, actor, obj, detail, text, salience, source, told_by, feeling, people"
_INTENT_COLS = "id, t, want, kind, target, status, attempts, result, next_try_t, thought_id, frame"


def _tokens(s: str) -> set:
    return set(_WORD.findall((s or "").lower()))


def _day_label(day: int) -> str:
    return datetime.fromtimestamp(day * DAY, UTC).date().isoformat()


class Memory:
    def __init__(self, path: Path, owner: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.owner = owner
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, timeout=5)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._pending = 0

    def _commit(self, force: bool = False) -> None:
        # batch commits: a tick may write several rows; commit at most every 20 writes or on demand
        self._pending += 1
        if force or self._pending >= 20:
            self.conn.commit()
            self._pending = 0

    def flush(self) -> None:
        with self.lock:
            self.conn.commit()
            self._pending = 0

    # ---- counters / kv --------------------------------------------------
    def bump(self, k: str, n: int = 1) -> int:
        with self.lock:
            self.conn.execute(
                "INSERT INTO counters(k, v) VALUES(?, ?) "
                "ON CONFLICT(k) DO UPDATE SET v = v + excluded.v", (k, n))
            self._commit()
            return self.conn.execute("SELECT v FROM counters WHERE k=?", (k,)).fetchone()[0]

    def counter(self, k: str) -> int:
        with self.lock:
            row = self.conn.execute("SELECT v FROM counters WHERE k=?", (k,)).fetchone()
        return row[0] if row else 0

    def counters(self) -> dict:
        with self.lock:
            return dict(self.conn.execute("SELECT k, v FROM counters").fetchall())

    def get(self, k: str, default=None):
        with self.lock:
            row = self.conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, k: str, v) -> None:
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO kv(k, v) VALUES(?, ?)", (k, json.dumps(v)))
            self._commit(force=True)

    # ---- episodes -------------------------------------------------------
    def add_episode(self, t: int, channel: str | None, kind: str, text: str, *,
                    actor: str | None = None, obj: str | None = None, detail: dict | None = None,
                    salience: float = 1.0, source: str = "seen", told_by: str | None = None,
                    feeling: str | None = None, people: list | None = None) -> int:
        if source not in SOURCES:
            raise ValueError(f"unknown provenance {source!r}: an episode is one of {sorted(SOURCES)}")
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO episodes(t, channel, kind, actor, obj, detail, text, salience, source, "
                "told_by, feeling, people) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (t, channel, kind, actor, obj, json.dumps(detail or {}), text, float(salience),
                 source, told_by, feeling, json.dumps(sorted(set(people or [])))),
            )
            self._commit()
            return cur.lastrowid

    def episode_count(self) -> int:
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]

    def recent(self, limit: int = 20, kinds: tuple | None = None, since_t: int | None = None) -> list:
        sql = f"SELECT {_EP_COLS} FROM episodes"
        conds, args = [], []
        if kinds:
            conds.append(f"kind IN ({','.join('?' * len(kinds))})")
            args += list(kinds)
        if since_t is not None:
            conds.append("t >= ?")
            args.append(since_t)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [self._row(r) for r in rows][::-1]

    def _row(self, r) -> dict:
        return {"id": r[0], "t": r[1], "channel": r[2], "kind": r[3], "actor": r[4], "obj": r[5],
                "detail": json.loads(r[6]), "text": r[7], "salience": r[8], "source": r[9],
                "told_by": r[10], "feeling": r[11], "people": json.loads(r[12] or "[]")}

    # columns last_of_kind may match on; a name is spliced into SQL, so it must be one of these
    _MATCHABLE = frozenset({"channel", "actor", "obj", "source", "told_by", "feeling"})

    def last_of_kind(self, kind: str, **match) -> dict | None:
        conds = ["kind = ?"]
        args: list = [kind]
        for k, v in match.items():
            if k not in self._MATCHABLE:
                raise ValueError(f"cannot match episodes on {k!r}")
            conds.append(f"{k} = ?")
            args.append(v)
        with self.lock:
            r = self.conn.execute(
                f"SELECT {_EP_COLS} FROM episodes WHERE {' AND '.join(conds)} "
                "ORDER BY id DESC LIMIT 1", args).fetchone()
        return self._row(r) if r else None

    def recall(self, now_t: int, query: str = "", people: list | None = None,
               channel: str | None = None, limit: int = 8, half_life_days: float = 2.0,
               exclude_kinds: tuple = ()) -> list:
        """Score this agent's own episodes for a situation. Lexical + recency + salience +
        person/channel match."""
        q = _tokens(query)
        people = set(people or [])
        with self.lock:
            rows = self.conn.execute(
                f"SELECT {_EP_COLS} FROM episodes ORDER BY id DESC LIMIT 4000").fetchall()
        scored = []
        hl = max(0.25, half_life_days) * DAY
        for r in rows:
            ep = self._row(r)
            if ep["kind"] in exclude_kinds:
                continue
            age = max(0, now_t - ep["t"])
            rec = math.exp(-age / hl)
            sal = 0.4 + 0.6 * min(ep["salience"], 5.0) / 5.0
            s = sal * (0.15 + 0.85 * rec)
            if people:
                ppl = set(ep["people"]) | ({ep["actor"]} if ep["actor"] else set())
                if ppl & people:
                    s *= 1.6
            if channel and ep["channel"] == channel:
                s *= 1.2
            if q:
                hits = len(q & _tokens(ep["text"]))
                s *= 1.0 + 0.35 * hits
            scored.append((s, ep))
        scored.sort(key=lambda x: -x[0])
        return [ep for _, ep in scored[:limit]]

    # ---- beliefs --------------------------------------------------------
    def belief(self, obj: str, key: str):
        with self.lock:
            r = self.conn.execute(
                "SELECT value, seen_t FROM beliefs WHERE obj=? AND key=?", (obj, key)).fetchone()
        return (json.loads(r[0]), r[1]) if r else (None, None)

    def believe(self, obj: str, key: str, value, seen_t: int, channel: str | None) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO beliefs(obj, key, value, seen_t, channel) VALUES(?,?,?,?,?)",
                (obj, key, json.dumps(value), seen_t, channel))
            self._commit()

    def beliefs_in_channel(self, channel: str) -> dict:
        with self.lock:
            rows = self.conn.execute(
                "SELECT obj, key, value, seen_t FROM beliefs WHERE channel=?", (channel,)).fetchall()
        out: dict = {}
        for obj, key, value, seen_t in rows:
            out.setdefault(obj, {})[key] = (json.loads(value), seen_t)
        return out

    def all_beliefs(self) -> dict:
        with self.lock:
            rows = self.conn.execute(
                "SELECT obj, key, value, seen_t, channel FROM beliefs").fetchall()
        out: dict = {}
        for obj, key, value, seen_t, channel in rows:
            out.setdefault(obj, {})[key] = (json.loads(value), seen_t, channel)
        return out

    # ---- people ---------------------------------------------------------
    def person(self, other: str) -> dict:
        with self.lock:
            r = self.conn.execute(
                "SELECT warmth, trust, familiarity, grievance, last_seen_t, last_seen_channel, "
                "seen_doing, talks, channel_seconds, unanswered, addressed "
                "FROM people WHERE other=?", (other,)).fetchone()
        if not r:
            return {"other": other, "warmth": 0.0, "trust": 0.0, "familiarity": 0.0,
                    "grievance": 0.0, "last_seen_t": None, "last_seen_channel": None,
                    "seen_doing": {}, "talks": 0, "channel_seconds": 0.0,
                    "unanswered": 0, "addressed": 0, "met": False}
        return {"other": other, "warmth": r[0], "trust": r[1], "familiarity": r[2],
                "grievance": r[3], "last_seen_t": r[4], "last_seen_channel": r[5],
                "seen_doing": json.loads(r[6]), "talks": r[7], "channel_seconds": r[8],
                "unanswered": int(r[9]), "addressed": int(r[10]), "met": True}

    def people(self) -> list:
        with self.lock:
            rows = self.conn.execute("SELECT other FROM people").fetchall()
        return [self.person(r[0]) for r in rows]

    def save_person(self, p: dict) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO people(other, warmth, trust, familiarity, grievance, "
                "last_seen_t, last_seen_channel, seen_doing, talks, channel_seconds, unanswered, "
                "addressed) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (p["other"], float(p["warmth"]), float(p["trust"]), float(p["familiarity"]),
                 float(p["grievance"]), p.get("last_seen_t"), p.get("last_seen_channel"),
                 json.dumps(p.get("seen_doing", {})), int(p.get("talks", 0)),
                 float(p.get("channel_seconds", 0.0)),
                 int(p.get("unanswered", 0)), int(p.get("addressed", 0))))
            self._commit()

    # ---- thoughts -------------------------------------------------------
    def add_thought(self, t: int, kind: str, text: str, about: str | None, urge: float,
                    sources: list, topic: str = "", say: str = "") -> int:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO thoughts(t, kind, text, about, urge, sources, topic, say) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (t, kind, text, about, float(urge), json.dumps(sources), topic, say))
            if cur.lastrowid % 200 == 0:
                self.conn.execute(
                    "DELETE FROM thoughts WHERE id NOT IN "
                    "(SELECT id FROM thoughts ORDER BY id DESC LIMIT ?)", (THOUGHT_CAP,))
            self._commit()
            return cur.lastrowid

    def recent_thoughts(self, limit: int = 5, unused_only: bool = False) -> list:
        sql = "SELECT id, t, kind, text, about, urge, sources, used, topic, say FROM thoughts"
        if unused_only:
            sql += " WHERE used = 0"
        sql += " ORDER BY id DESC LIMIT ?"
        with self.lock:
            rows = self.conn.execute(sql, (limit,)).fetchall()
        return [{"id": r[0], "t": r[1], "kind": r[2], "text": r[3], "about": r[4], "urge": r[5],
                 "sources": json.loads(r[6]), "used": r[7], "topic": r[8] or "", "say": r[9] or ""}
                for r in rows]

    def mark_thought_used(self, tid: int) -> None:
        with self.lock:
            self.conn.execute("UPDATE thoughts SET used = 1 WHERE id=?", (tid,))
            self._commit()

    # ---- intentions -----------------------------------------------------
    def open_intentions(self) -> list:
        with self.lock:
            rows = self.conn.execute(
                f"SELECT {_INTENT_COLS} FROM intentions WHERE status='open' ORDER BY id").fetchall()
        return [self._intent(r) for r in rows]

    def _intent(self, r) -> dict:
        return {"id": r[0], "t": r[1], "want": r[2], "kind": r[3], "target": r[4], "status": r[5],
                "attempts": r[6], "result": r[7], "next_try_t": r[8], "thought_id": r[9],
                "frame": r[10]}

    def add_intention(self, t: int, want: str, kind: str, target: str | None,
                      thought_id: int | None, frame: str | None) -> int | None:
        norm = want.strip().lower()
        with self.lock:
            for it in self.open_intentions():
                if it["want"].strip().lower() == norm:
                    return None
            cur = self.conn.execute(
                "INSERT INTO intentions(t, want, kind, target, thought_id, frame) "
                "VALUES(?,?,?,?,?,?)", (t, want, kind, target, thought_id, frame))
            self._commit(force=True)
            return cur.lastrowid

    def note_attempt(self, iid: int, next_try_t: int) -> int:
        with self.lock:
            self.conn.execute(
                "UPDATE intentions SET attempts = attempts + 1, next_try_t = ? WHERE id=?",
                (next_try_t, iid))
            self._commit(force=True)
            return self.conn.execute(
                "SELECT attempts FROM intentions WHERE id=?", (iid,)).fetchone()[0]

    def resolve_intention(self, iid: int, status: str, result: str, done_t: int) -> None:
        """An intention never just disappears: it closes with an outcome (done, tried, or
        let go) and a reason in words, or it stays open."""
        if status not in ("done", "tried", "let_go"):
            raise ValueError("an intention closes with an outcome")
        if not result or len(result.strip()) < 3:
            raise ValueError("an intention closes with a reason")
        with self.lock:
            self.conn.execute(
                "UPDATE intentions SET status=?, result=?, done_t=? WHERE id=?",
                (status, result, done_t, iid))
            self._commit(force=True)

    def recent_intentions(self, limit: int = 10) -> list:
        with self.lock:
            rows = self.conn.execute(
                f"SELECT {_INTENT_COLS} FROM intentions ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
        return [self._intent(r) for r in rows]

    # ---- reflections / habits ------------------------------------------
    def add_reflection(self, t: int, day: int, text: str, episode_ids: list) -> int:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO reflections(t, day, text, episode_ids) VALUES(?,?,?,?)",
                (t, day, text, json.dumps(episode_ids)))
            self._commit(force=True)
            return cur.lastrowid

    def reflections(self, limit: int = 3) -> list:
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, t, day, text, episode_ids FROM reflections ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
        return [{"id": r[0], "t": r[1], "day": r[2], "text": r[3],
                 "episode_ids": json.loads(r[4])} for r in rows]

    def habit_hit(self, action: str, channel: str, hour: int, t: int, relieved: bool) -> None:
        with self.lock:
            r = self.conn.execute(
                "SELECT count, reliability FROM habits WHERE action=? AND channel=? AND hour=?",
                (action, channel, hour)).fetchone()
            count, rel = (r if r else (0, 0.0))
            count += 1
            rel = rel + 0.2 * ((1.0 if relieved else 0.0) - rel)      # EMA, can fall as well as rise
            self.conn.execute(
                "INSERT OR REPLACE INTO habits(action, channel, hour, count, reliability, last_t) "
                "VALUES(?,?,?,?,?,?)", (action, channel, hour, count, rel, t))
            self._commit()

    def habit_bonus(self, action: str, channel: str, hour: int, t: int) -> float:
        with self.lock:
            r = self.conn.execute(
                "SELECT count, reliability, last_t FROM habits "
                "WHERE action=? AND channel=? AND hour IN (?,?,?)",
                (action, channel, (hour - 1) % 24, hour, (hour + 1) % 24)).fetchone()
        if not r:
            return 0.0
        count, rel, last_t = r
        if count < 3:
            return 0.0
        stale = max(0.0, (t - last_t) / DAY - 3)   # unused for more than 3 days: fades
        return max(0.0, min(0.5, 0.1 * math.log1p(count)) * rel * math.exp(-stale / 4))

    def habits(self) -> list:
        with self.lock:
            rows = self.conn.execute(
                "SELECT action, channel, hour, count, reliability, last_t FROM habits "
                "WHERE count >= 3 ORDER BY count DESC LIMIT 12").fetchall()
        return [{"action": r[0], "channel": r[1], "hour": r[2], "count": r[3],
                 "reliability": round(r[4], 2), "last_t": r[5]} for r in rows]

    # ---- bounding -------------------------------------------------------
    def fold(self, now_t: int) -> int:
        """Fold old low-salience episodes into per-day summaries; enforce the hard cap.
        Returns rows removed. Only low-salience rows fold: what mattered stays verbatim,
        with its provenance, for as long as the cap allows."""
        cutoff = now_t - FOLD_AFTER_DAYS * DAY
        removed = 0
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, t, channel, kind FROM episodes WHERE t < ? AND salience < ?",
                (cutoff, FOLD_SALIENCE_FLOOR)).fetchall()
            groups: dict = {}
            for eid, t, channel, kind in rows:
                groups.setdefault((t // DAY, kind, channel), []).append(eid)
            for (day, kind, channel), ids in groups.items():
                where = f"in {channel}" if channel else "with no channel"
                self.conn.execute(
                    "INSERT INTO summaries(day, kind, channel, count, text) VALUES(?,?,?,?,?)",
                    (day, kind, channel, len(ids), f"{_day_label(day)}: {len(ids)} {kind} {where}"))
                self.conn.executemany("DELETE FROM episodes WHERE id=?", [(i,) for i in ids])
                removed += len(ids)
            n = self.conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
            if n > EPISODE_CAP:
                over = n - EPISODE_CAP
                self.conn.execute(
                    "DELETE FROM episodes WHERE id IN "
                    "(SELECT id FROM episodes ORDER BY salience ASC, id ASC LIMIT ?)", (over,))
                removed += over
            self.conn.execute(
                "DELETE FROM summaries WHERE id NOT IN "
                "(SELECT id FROM summaries ORDER BY id DESC LIMIT ?)", (SUMMARY_CAP,))
            self.conn.commit()
            self._pending = 0
            # fold the write-ahead log back into the file so the store's size on disk is the
            # data, not the churn
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return removed

    def summaries(self, limit: int = 20) -> list:
        with self.lock:
            rows = self.conn.execute(
                "SELECT day, kind, channel, count, text FROM summaries ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
        return [{"day": r[0], "kind": r[1], "channel": r[2], "count": r[3], "text": r[4]}
                for r in rows][::-1]

    def size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.path) + suffix)
            if p.exists():
                total += p.stat().st_size
        return total

    def close(self) -> None:
        with self.lock:
            self.conn.commit()
            self.conn.close()
