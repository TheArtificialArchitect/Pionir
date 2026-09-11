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

Recall is hybrid when an embedder is supplied: BM25 fused with a local-embedding
cosine ranking by reciprocal rank, then weighted by recency and salience. With no
embedder it is exactly the lexical ranking, unchanged - the embedding path is
fail-open, so a missing or unavailable embedder silently falls back rather than
failing a recall. The embedding model runs in its own process (Ollama,
`nomic-embed-text` ~0.32 GB) and is never the speaking model. Proven on
Psyche/Bram, reimplemented here; Bram's own tree is untouched.
"""

from __future__ import annotations

import array
import functools
import json
import urllib.error
import urllib.request
import math
import re
import sqlite3
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol, Sequence

# Reciprocal-rank fusion constant. Fusion combines lexical and semantic recall by
# rank, not by raw score, because a BM25 score and a cosine similarity live on
# different scales and adding them is meaningless. RRF_K damps the top ranks so a
# memory need not win both lists to place well - appearing high in either counts.
RRF_K = 60

# The kinds a memory can be. Not enforced as an enum - a caller may coin a new
# one - but these are the ones recall weights by default salience for.
KIND_SALIENCE: dict[str, float] = {
    "lesson": 8.0,    # a mistake or correction, meant to be recalled before acting
    "episode": 5.0,   # a consolidated conversation
    "fact": 6.0,      # something durable about a person or the world
    "canon": 6.0,     # something the agent has said is true about itself
    "note": 4.0,      # a passing self-note
    "lookup": 5.0,    # something fetched from the web and worth keeping
    "thought": 3.0,   # an idle interior thought
    "message": 2.0,   # a raw conversation turn, awaiting consolidation into an episode
}
_DEFAULT_SALIENCE = 4.0

# The shared namespace of lessons every bot reads before it acts. Private
# per-bot memory lives in its own namespace; this one is common ground, so a
# mistake learned once is recalled by all of them (docs/ARCHITECTURE_DECISIONS).
LESSONS_NAMESPACE = "lessons"

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
CREATE TABLE IF NOT EXISTS vectors (
    memory_id INTEGER PRIMARY KEY,
    model     TEXT NOT NULL,
    vec       BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_vec_model ON vectors(model);
"""

_WORD = re.compile(r"[a-z0-9']+")
_STOP = frozenset(
    "the a an and or but of to in on at for with is are was were be been it its this "
    "that i you he she we they me him her them my your his our their so if as do did "
    "not no yes just like have has had what when where who how why about from by up out "
    "into then than too very can will would could should".split()
)


def _stem(word: str) -> str:
    """A deliberately conservative inflectional stemmer.

    It bridges the cases the recall eval caught - plurals and third-person -s
    (resources/resource, governs/govern, commits/commit) and -ies/-y
    (replies/reply) - and nothing more. It does NOT touch -ing, -ed, or
    derivational suffixes like -or (governs still will not reach governor),
    because undoubling and derivational rules are where a light stemmer starts
    inventing false matches. The recall eval is the guard: widen this only with a
    probe that shows the widening helps and `recall-check` still green.
    """
    if len(word) <= 3 or word.endswith("ss"):
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("s"):
        return word[:-1]
    return word


def tokens(text: str) -> list[str]:
    return [
        _stem(t)
        for t in _WORD.findall((text or "").lower())
        if t not in _STOP and len(t) > 1
    ]


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
    via: str = ""       # empty for a direct hit; the slug it was linked from otherwise


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


class Embedder(Protocol):
    """Turns text into vectors. Optional: without one, recall is lexical-only.

    `embed` returns one vector per input, or None when embedding is unavailable
    (daemon down, model not pulled, a malformed reply). None is not an error to
    raise - it is the signal to fall back to lexical recall, which is why the
    whole embedding path is fail-open.
    """

    @property
    def model(self) -> str: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]] | None: ...


def _pack(vec: Sequence[float]) -> bytes:
    return array.array("f", vec).tobytes()


def _unpack(blob: bytes) -> array.array:
    out = array.array("f")
    out.frombytes(blob)
    return out


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / math.sqrt(na * nb)


def _synchronized(method):
    """Hold the cortex lock for a whole public method.

    The lock is reentrant, so a guarded method may call another (``remember`` ->
    ``remember_many``) without deadlock, and the whole call - all its statements
    and its commit - is one critical section rather than a race between threads
    sharing the one connection.
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class Cortex:
    """SQLite-backed relevance memory: lexical BM25, optionally fused with a
    semantic embedding recall when an Embedder is supplied. Stdlib only; the
    embedding model, if any, runs in its own process (Ollama) - never in here."""

    def __init__(
        self, path: str | Path, *, now=time.time, embedder: Embedder | None = None
    ) -> None:
        self._now = now
        self.embedder = embedder
        self.path = Path(path)
        if self.path.parent and str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False plus a reentrant lock (see _synchronized): the
        # CLI is single-threaded, but the dashboard's ThreadingHTTPServer touches
        # the store from per-request threads (doctor reads stats, a tripped
        # circuit records a lesson). The lock is held for whole methods, not
        # single statements, so a multi-statement transaction stays atomic
        # instead of interleaving with another thread's write.
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.commit()

    # ------------------------------------------------------------------ write
    @_synchronized
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

    @_synchronized
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
        # Best-effort embedding, in one batched call. It never blocks the write:
        # if the embedder is absent or unavailable the memory is stored without a
        # vector and is still recalled lexically. Un-embedded rows are filled in
        # later by reindex(). This embeds only the new rows - O(new), never a
        # re-score of the whole store (HEAD 3.11).
        self._embed_and_store(ids, [r[3] for r in rows])
        return ids

    def _embed_and_store(self, ids: Sequence[int], texts: Sequence[str]) -> int:
        """Embed these texts and store their vectors. Returns how many landed;
        0 on any failure, which is not an error - recall falls back to lexical."""
        if self.embedder is None or not ids:
            return 0
        try:
            vecs = self.embedder.embed(list(texts))
        except Exception:  # noqa: BLE001 - embedding is fail-open by contract
            return 0
        if not vecs or len(vecs) != len(ids):
            return 0
        model = self.embedder.model
        self._db.executemany(
            "INSERT OR REPLACE INTO vectors(memory_id, model, vec) VALUES(?,?,?)",
            [(mid, model, _pack(vec)) for mid, vec in zip(ids, vecs)],
        )
        self._db.commit()
        return len(ids)

    @_synchronized
    def reindex(self) -> int:
        """Embed every active memory that lacks a vector for the current model,
        in batches. Run after enabling an embedder on an existing store, or after
        changing the embedding model - cosine between two models' vectors is
        meaningless, so a new model rebuilds rather than mixes."""
        if self.embedder is None:
            return 0
        model = self.embedder.model
        rows = list(
            self._db.execute(
                "SELECT m.id AS id, m.text AS text FROM memories m "
                "LEFT JOIN vectors v ON v.memory_id = m.id AND v.model = ? "
                "WHERE m.active = 1 AND v.memory_id IS NULL",
                (model,),
            )
        )
        done = 0
        for start in range(0, len(rows), 64):
            batch = rows[start : start + 64]
            done += self._embed_and_store([r["id"] for r in batch], [r["text"] for r in batch])
        return done

    @_synchronized
    def forget(self, memory_id: int) -> bool:
        """Retire a memory. Soft delete - a one-way hard delete is a door with no
        way back (HEAD 3.2); active=0 keeps it recoverable and out of recall."""
        cur = self._db.execute("UPDATE memories SET active=0 WHERE id=? AND active=1", (memory_id,))
        self._db.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------ read
    @_synchronized
    def get(self, memory_id: int) -> Memory | None:
        row = self._db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        return self._row_to_memory(row) if row else None

    @_synchronized
    def recall(
        self,
        query: str,
        k: int = 8,
        *,
        namespace: str | Sequence[str] | None = None,
        kinds: Sequence[str] | None = None,
        budget_chars: int | None = None,
        expand_links: bool = False,
    ) -> list[Memory]:
        """The heart of it: the memories most relevant to `query`, newest-weighted.

        Lexical BM25 always runs. When an embedder is present and vectors exist,
        a semantic ranking runs too and the two are fused by reciprocal rank, so
        a memory that shares the query's *meaning* but not its words still
        surfaces - the case pure lexical structurally cannot reach. With no
        embedder, or when the semantic side finds nothing, the result is exactly
        the lexical ranking, unchanged.

        `namespace` scopes the recall (one, several, or all); `kinds` filters by
        type. `budget_chars`, if given, trims the result so the injected block
        fits a window budget - trim the recalled block, never the identity ahead
        of it, because a model silently drops the front of an over-length prompt.

        `expand_links` follows one hop of `[[slug]]` links from the direct hits
        and appends the memories they point at - a lesson that references another
        lesson pulls it in too. Expansion stays inside the same namespace scope,
        so it can never surface one bot's private memory in another's recall."""
        q = tokens(query)
        if not q:
            return []
        candidates = self._candidates(namespace, kinds)
        if not candidates:
            return []

        now = self._now()
        lexical = self._bm25_scored(q, candidates)
        semantic = self._semantic_scored(query, candidates)
        if semantic:
            ranked = self._fuse(lexical, semantic, now)
        else:
            ranked = self._weight_lexical(lexical, now)

        out: list[Memory] = []
        used = 0

        def _append(memory: Memory) -> bool:
            nonlocal used
            if budget_chars is not None and used + len(memory.text) > budget_chars and out:
                return False
            out.append(memory)
            used += len(memory.text)
            return True

        for score, row in ranked[: max(k, 0)]:
            if not _append(self._row_to_memory(row, score=score)):
                break

        if expand_links and out:
            have = {m.id for m in out}
            # slug pointed at -> slug of the direct hit that pointed at it (its
            # provenance). First referrer wins if two hits link the same slug.
            referrer: dict[str, str] = {}
            for m in out:
                for slug in m.links:
                    if slug:
                        referrer.setdefault(slug, m.slug or "link")
            if referrer:
                linked = self._by_slugs(list(referrer), namespace, exclude=have)
                for row in sorted(linked, key=lambda r: self._weight(r, now), reverse=True):
                    via = referrer.get(row["slug"], "link")
                    if not _append(
                        self._row_to_memory(row, score=self._weight(row, now), via=via)
                    ):
                        break
        return out

    def _by_slugs(
        self, slugs: Sequence[str], namespace, exclude: set[int]
    ) -> list[sqlite3.Row]:
        """Active memories carrying one of these slugs, inside the same namespace
        scope as the recall (never across it - that would leak a private memory)."""
        where = ["active = 1", f"slug IN ({','.join('?' * len(slugs))})"]
        params: list[Any] = list(slugs)
        if namespace is not None:
            names = [namespace] if isinstance(namespace, str) else list(namespace)
            where.append(f"namespace IN ({','.join('?' * len(names))})")
            params.extend(names)
        rows = self._db.execute(
            "SELECT * FROM memories WHERE " + " AND ".join(where), params
        )
        return [r for r in rows if r["id"] not in exclude]

    def _bm25_scored(
        self, q: list[str], candidates: list[sqlite3.Row]
    ) -> list[tuple[float, sqlite3.Row]]:
        """Raw BM25 score per candidate that matches at least one query term."""
        toks = [tokens(c["text"]) for c in candidates]
        n = len(candidates)
        avgdl = sum(len(t) for t in toks) / n or 1.0
        df: Counter = Counter()
        for t in toks:
            df.update(set(t))
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
            if s > 0:
                scored.append((s, row))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return scored

    def _semantic_scored(
        self, query: str, candidates: list[sqlite3.Row]
    ) -> list[tuple[float, sqlite3.Row]]:
        """Cosine of the query against each candidate's stored vector, for the
        current embedding model only. Empty when there is no embedder, the query
        cannot be embedded, or no candidate has a current-model vector - each of
        which sends recall cleanly back to lexical."""
        if self.embedder is None:
            return []
        try:
            embedded = self.embedder.embed([query])
        except Exception:  # noqa: BLE001 - fail-open
            return []
        if not embedded:
            return []
        qvec = embedded[0]
        by_id = {c["id"]: c for c in candidates}
        if not by_id:
            return []
        placeholders = ",".join("?" * len(by_id))
        rows = self._db.execute(
            f"SELECT memory_id, vec FROM vectors WHERE model = ? "
            f"AND memory_id IN ({placeholders})",
            [self.embedder.model, *by_id.keys()],
        )
        scored: list[tuple[float, sqlite3.Row]] = []
        for r in rows:
            s = _cosine(qvec, _unpack(r["vec"]))
            if s > 0:
                scored.append((s, by_id[r["memory_id"]]))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return scored

    def _weight(self, row: sqlite3.Row, now: float) -> float:
        age_days = max(0.0, (now - row["ts"]) / 86400)
        recency = 0.6 + 0.4 * math.exp(-age_days / 45)
        return recency * (0.5 + row["salience"] / 12)

    def _weight_lexical(
        self, lexical: list[tuple[float, sqlite3.Row]], now: float
    ) -> list[tuple[float, sqlite3.Row]]:
        """The lexical-only path, scored exactly as before: BM25 x recency x
        salience. Kept identical so recall is unchanged where no embedder runs."""
        scored = [(s * self._weight(row, now), row) for s, row in lexical]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return scored

    def _fuse(
        self,
        lexical: list[tuple[float, sqlite3.Row]],
        semantic: list[tuple[float, sqlite3.Row]],
        now: float,
    ) -> list[tuple[float, sqlite3.Row]]:
        """Reciprocal-rank fusion of the two rankings, then the recency/salience
        weight. Rank, not raw score, because BM25 and cosine are not comparable."""
        fused: dict[int, dict] = {}
        for rankings in (lexical, semantic):
            for i, (_, row) in enumerate(rankings):
                slot = fused.setdefault(row["id"], {"row": row, "rrf": 0.0})
                slot["rrf"] += 1.0 / (RRF_K + i + 1)
        scored = [
            (slot["rrf"] * self._weight(slot["row"], now), slot["row"])
            for slot in fused.values()
        ]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return scored

    @_synchronized
    def memories(
        self, namespace: str, *, kind: str | None = None, limit: int = 1000
    ) -> list[Memory]:
        """Active memories in a namespace, oldest first - a plain listing, not a
        relevance recall. Consolidation reads a conversation's raw turns this way,
        in order, rather than by how well they match a query."""
        where = ["active = 1", "namespace = ?"]
        params: list[Any] = [namespace]
        if kind is not None:
            where.append("kind = ?")
            params.append(kind)
        rows = self._db.execute(
            "SELECT * FROM memories WHERE " + " AND ".join(where) + " ORDER BY id ASC LIMIT ?",
            [*params, limit],
        )
        return [self._row_to_memory(r) for r in rows]

    @_synchronized
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
        # Recall is hybrid only where vectors exist for the live model; report
        # coverage honestly so a store that is silently lexical-only is visible
        # rather than mistaken for hybrid (HEAD 3.20).
        model = self.embedder.model if self.embedder is not None else None
        embedded = 0
        if model is not None:
            embedded = self._db.execute(
                "SELECT COUNT(*) FROM vectors v JOIN memories m ON m.id = v.memory_id "
                "WHERE m.active = 1 AND v.model = ?",
                (model,),
            ).fetchone()[0]
        return {
            "total": total,
            "by_kind": by_kind,
            "by_namespace": by_ns,
            "recall": "hybrid" if (model and embedded) else "lexical",
            "embed_model": model,
            "embedded": embedded,
        }

    @_synchronized
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
    def _row_to_memory(row: sqlite3.Row, *, score: float = 0.0, via: str = "") -> Memory:
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
            via=via,
        )

    # ---------------------------------------------------------------- lessons
    @_synchronized
    def record_lesson(
        self,
        text: str,
        *,
        slug: str | None = None,
        links: Sequence[str] = (),
        salience: float = 8.0,
        meta: dict[str, Any] | None = None,
    ) -> int:
        """Write a lesson into the shared `lessons` namespace - a mistake, a
        correction, a "this failed before and here is why". High salience by
        default, because a lesson is meant to outrank ordinary recall when it is
        relevant. Every bot reads this namespace; that is the whole point."""
        return self.remember(
            "lesson",
            text,
            namespace=LESSONS_NAMESPACE,
            salience=salience,
            slug=slug,
            links=links,
            meta=meta,
        )

    @_synchronized
    def lessons_for(self, context: str, k: int = 3) -> list[Memory]:
        """The recall-before-act hook: the lessons relevant to what is about to
        happen. Call it with the intent - the task, the plan, the thing being
        considered - and heed what comes back before doing it. Scoped to the
        shared lessons namespace and link-expanded, so a lesson pulls in the
        related ones it points at."""
        return self.recall(
            context, k=k, namespace=LESSONS_NAMESPACE, expand_links=True
        )


class OllamaEmbedder:
    """Local embeddings through Ollama, stdlib urllib only - no third-party deps.

    Default model `nomic-embed-text`: measured at 0.32 GB of VRAM, co-resident
    with a 12B speaking model on the 12 GB card (Psyche/Bram, 2026-09-09). The
    embedder is tiny; only the speaker is big, which is why semantic recall does
    not cost a second heavyweight lease. The speaking model is never asked to
    embed - a 12B doing it would evict itself from the card between turns.

    Every failure path returns None rather than raising, because the Cortex
    embedding contract is fail-open: None means "fall back to lexical", not
    "the turn failed".
    """

    def __init__(
        self,
        model: str = "nomic-embed-text",
        base_url: str = "http://127.0.0.1:11434",
        timeout_seconds: int = 30,
    ) -> None:
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @property
    def model(self) -> str:
        return self._model

    def embed(self, texts: Sequence[str]) -> list[list[float]] | None:
        items = [t for t in texts]
        if not items:
            return []
        body = json.dumps({"model": self._model, "input": items}).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}/api/embed",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                document = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return None
        vectors = document.get("embeddings") if isinstance(document, dict) else None
        if not isinstance(vectors, list) or len(vectors) != len(items):
            return None
        out: list[list[float]] = []
        for vec in vectors:
            if not isinstance(vec, list) or not vec:
                return None
            out.append([float(x) for x in vec])
        return out
