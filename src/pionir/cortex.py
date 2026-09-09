"""A retrieval-first memory engine — the fix for a small context window.

Pionir's previous `memory.py` is a namespaced key-value store: exact-key lookup
with permission grants, wired into nothing. This is the other kind of memory, and
the one that matters for a conversation — you do not remember by asking for a key
you already know, you remember by relevance to what is in front of you now.

The design is Galatea's, proven: keep everything in SQLite, and each turn recall
the handful of memories relevant to the current message and inject only those.
The live window holds the last few turns plus what was recalled; the rest waits
in the store and comes back when it is needed. Done right the window size stops
mattering — a thing said weeks ago can surface without ever having sat in context.

Generalised here for a system of bots rather than one person:

* Every memory carries a `namespace`, so many streams (the voice, a specialist,
  a research pass) write into one store and recall sorts across all of them by
  relevance. A recall can be scoped to one namespace or range over several.
* Memories can link to each other by `slug` (the `[[name]]` idiom), so a recalled
  memory can pull in what it points at. Stored now; one-hop expansion is a hook.
* Writes are batched. Re-scoring the whole store on every single write was right
  for a dream writing five rows and took hours for an import of twelve thousand
  (HEAD 3.11). Recall scores lazily at query time over a candidate set, so a write
  is one INSERT and nothing else.

Recall is lexical (BM25 + recency), with no embedding model and so no second
model on the card. Semantic recall is a documented future addition, not a
rewrite: `recall()` already ranks a candidate set, and an embedding pass would
re-rank the same set. Do not add it until measured recall quality asks for it.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

# The kinds a memory can be. Not enforced as an enum - a caller may coin a new
# one - but these are the ones recall weights by default salience for.
KIND_SALIENCE: dict[str, float] = {
    "episode": 5.0,   # a consolidated conversation
    "fact": 6.0,      # something durable about a person or the world
    "canon": 6.0,     # something the agent has said is true about itself
    "note": 4.0,      # a passing self-note
    "lookup": 5.0,    # something fetched from the web and worth keeping
    "thought": 3.0,   # an idle interior thought
}
_DEFAULT_SALIENCE = 4.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL    NOT NULL,
    namespace TEXT    NOT NULL DEFAULT 'shared',
    kind      TEXT    NOT NULL,
    text      TEXT    NOT NULL,
    salience  REAL    NOT NULL,
    slug      TEXT,
    links     TEXT    NOT NULL DEFAULT '[]',
    meta      TEXT    NOT NULL DEFAULT '{}',
    active    INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_mem_ns   ON memories(namespace, active);
CREATE INDEX IF NOT EXISTS ix_mem_kind ON memories(kind, active);
CREATE INDEX IF NOT EXISTS ix_mem_slug ON memories(slug);
"""

_WORD = re.compile(r"[a-z0-9']+")
_STOP = frozenset(
    "the a an and or but of to in on at for with is are was were be been it its this "
    "that i you he she we they me him her them my your his our their so if as do did "
    "not no yes just like have has had what when where who how why about from by up out "
    "into then than too very can will would could should".split()
)


def tokens(text: str) -> list[str]:
    return [t for t in _WORD.findall((text or "").lower()) if t not in _STOP and len(t) > 1]


@dataclass(frozen=True, slots=True)
class Memory:
    id: int
    ts: float
    namespace: str
    kind: str
    text: str
    salience: float
    slug: str | None = None
    links: tuple[str, ...] = ()
    meta: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0  # set by recall; 0 outside a recall


@dataclass(frozen=True, slots=True)
class NewMemory:
    """One memory to write. `salience` None means take the kind's default."""

    kind: str
    text: str
    namespace: str = "shared"
    salience: float | None = None
    slug: str | None = None
    links: Sequence[str] = ()
    meta: dict[str, Any] | None = None


class Cortex:
    """SQLite-backed relevance memory. Stdlib only; no model, no external service."""

    def __init__(self, path: str | Path, *, now=time.time) -> None:
        self._now = now
        self.path = Path(path)
        if self.path.parent and str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path))
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.commit()

    # ------------------------------------------------------------------ write
    def remember(
        self,
        kind: str,
        text: str,
        *,
        namespace: str = "shared",
        salience: float | None = None,
        slug: str | None = None,
        links: Sequence[str] = (),
        meta: dict[str, Any] | None = None,
    ) -> int:
        """Write one memory, return its id. One INSERT; nothing is re-scored."""
        ids = self.remember_many(
            [NewMemory(kind, text, namespace, salience, slug, list(links), meta)]
        )
        return ids[0]

    def remember_many(self, items: Iterable[NewMemory]) -> list[int]:
        """Batch write. The whole point of batching lives here: one transaction,
        no per-row indexing work, so importing twelve thousand rows is twelve
        thousand INSERTs and one commit, not a re-score each time (HEAD 3.11)."""
        rows = []
        for m in items:
            text = (m.text or "").strip()
            if not text:
                raise ValueError("a memory needs text")
            sal = m.salience if m.salience is not None else KIND_SALIENCE.get(m.kind, _DEFAULT_SALIENCE)
            rows.append(
                (
                    self._now(),
                    m.namespace,
                    m.kind,
                    text,
                    float(sal),
                    m.slug,
                    json.dumps(list(m.links)),
                    json.dumps(m.meta or {}),
                )
            )
        # One transaction, one commit - the batching that matters. execute() in a
        # loop rather than executemany() because executemany does not report a
        # rowid, and the caller needs the ids back; the per-row Python overhead is
        # nothing against the single fsync the one commit saves.
        sql = (
            "INSERT INTO memories(ts,namespace,kind,text,salience,slug,links,meta) "
            "VALUES(?,?,?,?,?,?,?,?)"
        )
        ids = [self._db.execute(sql, row).lastrowid for row in rows]
        self._db.commit()
        return ids

    def forget(self, memory_id: int) -> bool:
        """Retire a memory. Soft delete - a one-way hard delete is a door with no
        way back (HEAD 3.2); active=0 keeps it recoverable and out of recall."""
        cur = self._db.execute("UPDATE memories SET active=0 WHERE id=? AND active=1", (memory_id,))
        self._db.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------ read
    def get(self, memory_id: int) -> Memory | None:
        row = self._db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        return self._row_to_memory(row) if row else None

    def recall(
        self,
        query: str,
        k: int = 8,
        *,
        namespace: str | Sequence[str] | None = None,
        kinds: Sequence[str] | None = None,
        budget_chars: int | None = None,
    ) -> list[Memory]:
        """The heart of it: the memories most relevant to `query`, newest-weighted.

        Scored lazily over the candidate set - BM25 with a recency multiplier and
        the memory's own salience. `namespace` scopes the recall (one, several, or
        all); `kinds` filters by type. `budget_chars`, if given, trims the result
        so the injected block fits a window budget - trim the recalled block, never
        the identity ahead of it, because a model silently drops the front of an
        over-length prompt (Galatea, docs/PHASE0 lineage)."""
        q = tokens(query)
        if not q:
            return []
        candidates = self._candidates(namespace, kinds)
        if not candidates:
            return []

        toks = [tokens(c["text"]) for c in candidates]
        n = len(candidates)
        avgdl = sum(len(t) for t in toks) / n or 1.0
        df: Counter = Counter()
        for t in toks:
            df.update(set(t))

        now = self._now()
        k1, b = 1.5, 0.75
        scored: list[tuple[float, sqlite3.Row]] = []
        for row, t in zip(candidates, toks):
            if not t:
                continue
            tf = Counter(t)
            s = 0.0
            for term in q:
                f = tf.get(term)
                if not f:
                    continue
                idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
                s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * len(t) / avgdl))
            if s <= 0:
                continue
            age_days = max(0.0, (now - row["ts"]) / 86400)
            recency = 0.6 + 0.4 * math.exp(-age_days / 45)
            scored.append((s * recency * (0.5 + row["salience"] / 12), row))

        scored.sort(key=lambda pair: pair[0], reverse=True)

        out: list[Memory] = []
        used = 0
        for score, row in scored[: max(k, 0)]:
            if budget_chars is not None and used + len(row["text"]) > budget_chars and out:
                break
            out.append(self._row_to_memory(row, score=score))
            used += len(row["text"])
        return out

    def stats(self) -> dict[str, Any]:
        """Counts, so a caller can prove the store is not empty - the wired-but-inert
        check (HEAD 3.1): a memory system that recalls nothing looks identical to one
        that is working until you look at whether anything is in it."""
        total = self._db.execute("SELECT COUNT(*) FROM memories WHERE active=1").fetchone()[0]
        by_kind = {
            row["kind"]: row["c"]
            for row in self._db.execute(
                "SELECT kind, COUNT(*) AS c FROM memories WHERE active=1 GROUP BY kind"
            )
        }
        by_ns = {
            row["namespace"]: row["c"]
            for row in self._db.execute(
                "SELECT namespace, COUNT(*) AS c FROM memories WHERE active=1 GROUP BY namespace"
            )
        }
        return {"total": total, "by_kind": by_kind, "by_namespace": by_ns}

    def close(self) -> None:
        self._db.close()

    # --------------------------------------------------------------- internal
    def _candidates(
        self, namespace: str | Sequence[str] | None, kinds: Sequence[str] | None
    ) -> list[sqlite3.Row]:
        where = ["active=1"]
        params: list[Any] = []
        if namespace is not None:
            names = [namespace] if isinstance(namespace, str) else list(namespace)
            where.append(f"namespace IN ({','.join('?' * len(names))})")
            params.extend(names)
        if kinds is not None:
            ks = list(kinds)
            where.append(f"kind IN ({','.join('?' * len(ks))})")
            params.extend(ks)
        sql = "SELECT * FROM memories WHERE " + " AND ".join(where)
        return list(self._db.execute(sql, params))

    @staticmethod
    def _row_to_memory(row: sqlite3.Row, *, score: float = 0.0) -> Memory:
        return Memory(
            id=row["id"],
            ts=row["ts"],
            namespace=row["namespace"],
            kind=row["kind"],
            text=row["text"],
            salience=row["salience"],
            slug=row["slug"],
            links=tuple(json.loads(row["links"])),
            meta=json.loads(row["meta"]),
            score=score,
        )
