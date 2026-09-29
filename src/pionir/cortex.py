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
import hashlib
import json
import logging
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
from typing import Any, Callable, Iterable, Protocol, Sequence

from . import secretscrub

_log = logging.getLogger(__name__)

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

# The cosine a lesson needs, with no shared word, to count as relevant to what is
# about to happen. Measured on the live store 2026-09-28 (nomic-embed-text): the
# best UNRELATED lesson for any capability scored 0.47-0.57; the related ones
# 0.60-0.87, and those also shared a word.
LESSON_MIN_SIMILARITY = 0.6

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
CREATE TABLE IF NOT EXISTS consolidations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL    NOT NULL,
    namespace  TEXT    NOT NULL,
    outcome    TEXT    NOT NULL,
    episode_id INTEGER,
    folded     INTEGER NOT NULL DEFAULT 0,
    detail     TEXT,
    chunk_start INTEGER
);
"""

# A lesson is shared with every bot, so it holds at most this much text, scrubbed.
LESSON_MAX_CHARS = 300
# Retention (days): folded or poisoned raw turns are DELETED this long after they
# were written; a distilled fact expires this long after it was distilled.
TURN_RETENTION_DAYS = 14.0
FACT_RETENTION_DAYS = 180.0
# How many pre-migration backups to keep beside the store.
_KEEP_BACKUPS = 3

# Schema/data migrations, tracked in PRAGMA user_version. 1 = collapse duplicate
# lessons (the 2026-09-28 flood: 800 live lessons, 6 distinct texts).
_SCHEMA_VERSION = 1

# How long a burst of recurrences of one lesson is counted together (seconds).
_BURST_SECONDS = 86400.0

_ID_RUN = re.compile(r"\b(?:[0-9a-f]{8,}(?:-[0-9a-f]{4,})*|\d{5,})\b")
_SPACE = re.compile(r"\s+")


_HTTP_CODE = re.compile(r"\bHTTP\s*(\d{3})\b", re.IGNORECASE)
_RETURN_CODE = re.compile(r"\breturn\s?code\W{0,3}(-?\d+)", re.IGNORECASE)


def failure_shape(message: str | None, error_type: str | None = None) -> str:
    """The structural facts of a failure - its error class and any HTTP status or
    return code - and NONE of its free text. A failure message can echo the
    request that caused it (a person's words, a secret) and a lesson is shared
    with every bot and handed back to every caller, so a lesson built from a
    failure is built from this, never from the message itself."""
    parts = [error_type] if error_type else []
    text = message or ""
    if (code := _HTTP_CODE.search(text)) is not None:
        parts.append(f"HTTP {code.group(1)}")
    if (code := _RETURN_CODE.search(text)) is not None:
        parts.append(f"returncode {code.group(1)}")
    return ", ".join(parts) or "no detail"


def scrub_lesson(text: str, known: Iterable[str] = ()) -> str:
    """A lesson's text as it may be stored: secrets redacted, one line, capped."""
    one_line = _SPACE.sub(" ", (text or "").strip())
    clean = secretscrub.scrub_text(one_line, known)
    return clean if len(clean) <= LESSON_MAX_CHARS else clean[: LESSON_MAX_CHARS - 1] + "…"


def lesson_key(text: str) -> str:
    """What makes two lessons the same lesson: the text, case- and space-folded,
    with long id-like runs (event ids, uuids, timestamps) blanked. Short numbers
    stay - HTTP 400 and HTTP 500 are different lessons."""
    folded = _SPACE.sub(" ", (text or "").strip().casefold())
    return _ID_RUN.sub("#", folded)

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


def bm25_scored(q: list[str], candidates: list[Any]) -> list[tuple[float, Any]]:
    """Raw BM25 score per candidate row that matches at least one query term. Pure: a
    function of the rows given, so a reader on its own connection ranks exactly as
    ``Cortex.recall`` does (pionir/library.py) without taking the live store's lock."""
    toks = [tokens(c["text"]) for c in candidates]
    n = len(candidates)
    if not n:
        return []
    avgdl = sum(len(t) for t in toks) / n or 1.0
    df: Counter = Counter()
    for t in toks:
        df.update(set(t))
    k1, b = 1.5, 0.75
    scored: list[tuple[float, Any]] = []
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


def recency_weight(row: Any, now: float) -> float:
    age_days = max(0.0, (now - row["ts"]) / 86400)
    recency = 0.6 + 0.4 * math.exp(-age_days / 45)
    return recency * (0.5 + row["salience"] / 12)


def weight_lexical(lexical: list[tuple[float, Any]], now: float) -> list[tuple[float, Any]]:
    """The lexical-only ranking: BM25 x recency x salience."""
    scored = [(s * recency_weight(row, now), row) for s, row in lexical]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored


def fuse(lexical: list[tuple[float, Any]], semantic: list[tuple[float, Any]],
         now: float) -> list[tuple[float, Any]]:
    """Reciprocal-rank fusion of the two rankings, then the recency/salience weight."""
    fused: dict[int, dict] = {}
    for rankings in (lexical, semantic):
        for i, (_, row) in enumerate(rankings):
            slot = fused.setdefault(row["id"], {"row": row, "rrf": 0.0})
            slot["rrf"] += 1.0 / (RRF_K + i + 1)
    scored = [(slot["rrf"] * recency_weight(slot["row"], now), slot["row"])
              for slot in fused.values()]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored


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
        self, path: str | Path, *, now=time.time, embedder: Embedder | None = None,
        turn_retention_days: float = TURN_RETENTION_DAYS,
        fact_retention_days: float = FACT_RETENTION_DAYS,
    ) -> None:
        self._now = now
        self.embedder = embedder
        self.turn_retention_days = float(turn_retention_days)
        self.fact_retention_days = float(fact_retention_days)
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
        # Fail-open is not fail-silent: every swallowed embedding failure is
        # counted and its last reason kept, so stats() and doctor can say why
        # recall went lexical instead of it just quietly happening.
        self.embed_failures = 0
        self.last_embed_error: str | None = None
        self.migration_error: str | None = None
        self.migration_backup: str | None = None
        self._migrate()
        try:
            self.purge()
        except Exception:  # noqa: BLE001 - retention is housekeeping, never fatal
            _log.warning("cortex retention purge failed at open", exc_info=True)

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

    def _embed_failed(self, why: str, *, log: bool = True) -> None:
        self.embed_failures += 1
        self.last_embed_error = why[:300]
        if log:
            _log.warning("embedding failed (%s); recall stays lexical for it", why)

    def _embed_and_store(
        self, ids: Sequence[int], texts: Sequence[str], *, backfill: bool = True
    ) -> int:
        """Embed these texts and store their vectors. Returns how many landed;
        0 on any failure, which is not an error - recall falls back to lexical -
        but it is counted and logged (stats: embed_failures, last_embed_error).

        On success it also backfills a small batch of older rows that have no
        vector (written while the embedder was down). Without this they stayed
        lexical-only until someone ran `pionir reindex-memory` by hand, and
        coverage would decay silently after every Ollama outage."""
        if self.embedder is None or not ids:
            return 0
        try:
            vecs = self.embedder.embed(list(texts))
        except Exception as error:  # noqa: BLE001 - embedding is fail-open by contract
            self._embed_failed(f"{type(error).__name__}: {error}")
            return 0
        if not vecs or len(vecs) != len(ids):
            paused = getattr(self.embedder, "paused_for", None)
            is_paused = callable(paused) and paused() > 0
            # A paused embedder already logged its outage once; do not log per write.
            self._embed_failed(
                "embedder paused" if is_paused else "embedder returned no usable vectors",
                log=not is_paused,
            )
            return 0
        model = self.embedder.model
        self._db.executemany(
            "INSERT OR REPLACE INTO vectors(memory_id, model, vec) VALUES(?,?,?)",
            [(mid, model, _pack(vec)) for mid, vec in zip(ids, vecs)],
        )
        self._db.commit()
        if backfill:
            missing = list(
                self._db.execute(
                    "SELECT m.id AS id, m.text AS text FROM memories m "
                    "LEFT JOIN vectors v ON v.memory_id = m.id AND v.model = ? "
                    "WHERE m.active = 1 AND v.memory_id IS NULL ORDER BY m.id DESC LIMIT 32",
                    (model,),
                )
            )
            if missing:
                self._embed_and_store(
                    [r["id"] for r in missing], [r["text"] for r in missing], backfill=False
                )
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
            done += self._embed_and_store(
                [r["id"] for r in batch], [r["text"] for r in batch], backfill=False
            )
        return done

    @_synchronized
    def forget(self, memory_id: int) -> bool:
        """Retire a memory. Soft delete - a one-way hard delete is a door with no
        way back (HEAD 3.2); active=0 keeps it recoverable and out of recall."""
        cur = self._db.execute("UPDATE memories SET active=0 WHERE id=? AND active=1", (memory_id,))
        self._db.commit()
        return cur.rowcount > 0

    @_synchronized
    def fold(
        self,
        namespace: str,
        ids: Sequence[int],
        episode: str,
        facts: Sequence[str] = (),
        *,
        meta: dict[str, Any] | None = None,
    ) -> tuple[int, list[int]]:
        """Consolidate: write one episode (+ facts) and retire the folded turns in
        ONE transaction. Either the episode exists and the turns are gone from
        recall, or nothing changed - never an episode with its turns still live,
        or turns retired with no episode to show for them. Returns
        (episode_id, fact_ids)."""
        text = (episode or "").strip()
        if not text:
            raise ValueError("an episode needs text")
        kept = [f.strip() for f in facts if f and f.strip()]
        sql = (
            "INSERT INTO memories(ts,namespace,kind,text,salience,slug,links,meta) "
            "VALUES(?,?,?,?,?,?,?,?)"
        )
        now = self._now()
        # Provenance: every episode and fact says where it came from - which
        # namespace, which turns - and a fact says when it expires (purge()).
        source = {"source": "consolidation", "namespace": namespace,
                  "from_turns": [min(ids), max(ids)] if ids else [], **(meta or {})}
        try:
            episode_id = self._db.execute(
                sql,
                (now, namespace, "episode", text,
                 float(KIND_SALIENCE.get("episode", _DEFAULT_SALIENCE)),
                 None, "[]", json.dumps(source)),
            ).lastrowid
            fact_meta = json.dumps({**source, "episode_id": episode_id,
                                    "expires_ts": now + self.fact_retention_days * 86400})
            fact_ids = [
                self._db.execute(
                    sql,
                    (now, namespace, "fact", fact,
                     float(KIND_SALIENCE.get("fact", _DEFAULT_SALIENCE)), None, "[]",
                     fact_meta),
                ).lastrowid
                for fact in kept
            ]
            if ids:
                marks = ",".join("?" * len(ids))
                self._db.execute(
                    f"UPDATE memories SET active=0, "
                    f"meta=json_set(meta, '$.folded_into', ?) "
                    f"WHERE namespace=? AND kind='message' AND active=1 "
                    f"AND id IN ({marks})",
                    [episode_id, namespace, *ids],
                )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        # Vectors are best-effort and outside the transaction, as in remember_many.
        self._embed_and_store([episode_id, *fact_ids], [text, *kept])
        return int(episode_id), [int(i) for i in fact_ids]

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
        min_similarity: float | None = None,
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
        so it can never surface one bot's private memory in another's recall.

        `min_similarity` makes the result a relevance answer rather than a ranked
        list: a memory then qualifies only on a lexical hit or a cosine of at least
        this much. Without it the semantic side scores EVERY candidate above zero,
        so a recall always returns k memories however unrelated they are."""
        q = tokens(query)
        if not q:
            return []
        candidates = self._candidates(namespace, kinds)
        if not candidates:
            return []

        now = self._now()
        lexical = self._bm25_scored(q, candidates)
        semantic = self._semantic_scored(query, candidates)
        if min_similarity is not None:
            semantic = [(s, row) for s, row in semantic if s >= min_similarity]
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
        return bm25_scored(q, candidates)

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
        except Exception as error:  # noqa: BLE001 - fail-open, but counted and logged
            self._embed_failed(f"query: {type(error).__name__}: {error}")
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
        return recency_weight(row, now)

    def _weight_lexical(
        self, lexical: list[tuple[float, sqlite3.Row]], now: float
    ) -> list[tuple[float, sqlite3.Row]]:
        """The lexical-only path, scored exactly as before: BM25 x recency x
        salience. Kept identical so recall is unchanged where no embedder runs."""
        return weight_lexical(lexical, now)

    def _fuse(
        self,
        lexical: list[tuple[float, sqlite3.Row]],
        semantic: list[tuple[float, sqlite3.Row]],
        now: float,
    ) -> list[tuple[float, sqlite3.Row]]:
        """Reciprocal-rank fusion of the two rankings, then the recency/salience
        weight. Rank, not raw score, because BM25 and cosine are not comparable."""
        return fuse(lexical, semantic, now)

    @_synchronized
    def memories(
        self, namespace: str, *, kind: str | None = None, limit: int | None = 1000
    ) -> list[Memory]:
        """Active memories in a namespace, oldest first - a plain listing, not a
        relevance recall. Consolidation reads a conversation's raw turns this way,
        in order, rather than by how well they match a query. ``limit=None``
        lists them all."""
        where = ["active = 1", "namespace = ?"]
        params: list[Any] = [namespace]
        if kind is not None:
            where.append("kind = ?")
            params.append(kind)
        rows = self._db.execute(
            "SELECT * FROM memories WHERE " + " AND ".join(where) + " ORDER BY id ASC LIMIT ?",
            [*params, -1 if limit is None else limit],
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
            # >0 while the embedder is paused after a timeout: recall is lexical only
            "embed_paused_s": round(paused(), 1) if callable(paused := getattr(
                self.embedder, "paused_for", None)) else 0.0,
            # Swallowed-but-counted embedding failures since this process started.
            "embed_failures": self.embed_failures,
            "last_embed_error": self.last_embed_error,
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
        known: Iterable[str] = (),
    ) -> int:
        """Write a lesson into the shared `lessons` namespace - a mistake, a
        correction, a "this failed before and here is why". High salience by
        default, because a lesson is meant to outrank ordinary recall when it is
        relevant. Every bot reads this namespace; that is the whole point.

        A lesson already known (same `lesson_key`) is not written again: the
        existing one's recurrence count goes up and its id is returned. Every
        failure used to insert a fresh row - 2026-09-28 the store held 800 live
        lessons with 6 distinct texts (398 copies each of two fiverr.ack
        failures), which drowned every recall in copies of one mistake. The
        count is itself the signal: a lesson that keeps recurring is a mistake
        being recalled and not heeded (doctor alerts on it).

        Every bot reads a lesson and callers get it back, so whatever the writer
        passed is scrubbed (pionir/secretscrub.py, plus `known` secret values) and
        capped at LESSON_MAX_CHARS here, whoever the writer is."""
        clean = scrub_lesson(text, known)
        if not clean:
            raise ValueError("a memory needs text")
        key = lesson_key(clean)
        for row in self._db.execute(
            "SELECT id, text, meta FROM memories "
            "WHERE namespace=? AND kind='lesson' AND active=1 ORDER BY id",
            (LESSONS_NAMESPACE,),
        ):
            if lesson_key(row["text"]) == key:
                self._recur(row["id"], json.loads(row["meta"] or "{}"))
                return int(row["id"])
        return self.remember(
            "lesson",
            clean,
            namespace=LESSONS_NAMESPACE,
            salience=salience,
            slug=slug,
            links=links,
            meta={**(meta or {}), "seen": 1, "last_seen": self._now(),
                  "burst_start": self._now(), "burst": 0},
        )

    def _recur(self, memory_id: int, meta: dict[str, Any]) -> None:
        """One more sighting of a known lesson: total count, last seen, and
        `burst` - recurrences within 24 h of `burst_start` (what doctor's
        recurrence alarm reads)."""
        now = self._now()
        meta["seen"] = int(meta.get("seen", 1)) + 1
        meta["last_seen"] = now
        if now - float(meta.get("burst_start", 0.0)) > _BURST_SECONDS:
            meta["burst_start"], meta["burst"] = now, 1
        else:
            meta["burst"] = int(meta.get("burst", 0)) + 1
        self._db.execute("UPDATE memories SET meta=? WHERE id=?", (json.dumps(meta), memory_id))
        self._db.commit()

    @_synchronized
    def lessons_for(self, context: str, k: int = 3) -> list[Memory]:
        """The recall-before-act hook: the lessons relevant to what is about to
        happen. Call it with the intent - the task, the plan, the thing being
        considered - and heed what comes back before doing it. Scoped to the
        shared lessons namespace and link-expanded, so a lesson pulls in the
        related ones it points at.

        Relevance-floored: a lesson comes back only on a shared word or a strong
        semantic match (LESSON_MIN_SIMILARITY). Unfloored, the embedding side made
        every lesson a candidate, so every task was handed three lessons - a
        client.orders poll was 'reminded' of fiverr.ack failures 1,600 times."""
        return self.recall(
            context, k=k, namespace=LESSONS_NAMESPACE, expand_links=True,
            min_similarity=LESSON_MIN_SIMILARITY,
        )

    # ---------------------------------------------------------- consolidation
    @_synchronized
    def note_consolidation(
        self, namespace: str, outcome: str, *, episode_id: int | None = None,
        folded: int = 0, detail: str | None = None, chunk_start: int | None = None,
    ) -> None:
        """Log one consolidation attempt - folded, declined, deferred, failed,
        poisoned - so 'does consolidation ever run?' has an answer in the store."""
        self._db.execute(
            "INSERT INTO consolidations(ts,namespace,outcome,episode_id,folded,detail,"
            "chunk_start) VALUES(?,?,?,?,?,?,?)",
            (self._now(), namespace, outcome, episode_id, int(folded),
             (detail or "")[:300] or None, chunk_start),
        )
        self._db.commit()

    @_synchronized
    def chunk_failures(self, namespace: str, chunk_start: int) -> tuple[int, float | None]:
        """(failed or declined attempts at the chunk starting at this turn, when
        the last one was) - what the Consolidator's backoff and poisoning read."""
        row = self._db.execute(
            "SELECT COUNT(*), MAX(ts) FROM consolidations WHERE namespace=? AND chunk_start=? "
            "AND outcome IN ('failed','declined')",
            (namespace, chunk_start),
        ).fetchone()
        return int(row[0]), row[1]

    @_synchronized
    def poison(self, namespace: str, ids: Sequence[int], reason: str) -> int:
        """Retire raw turns that failed to fold too often: out of every later
        chunk (so one bad chunk cannot block the rest forever) and out of recall,
        marked `poisoned`, and deleted with the folded ones at retention."""
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        cur = self._db.execute(
            f"UPDATE memories SET active=0, meta=json_set(meta, '$.poisoned', ?) "
            f"WHERE namespace=? AND kind='message' AND active=1 AND id IN ({marks})",
            [reason[:200], namespace, *ids],
        )
        self._db.commit()
        return cur.rowcount

    @_synchronized
    def purge(self) -> dict[str, int]:
        """Retention, as DELETEs (not retirement): folded or poisoned raw turns
        older than turn_retention_days, and facts past their expires_ts - with
        their vectors. Unfolded turns are kept: they are still waiting."""
        now = self._now()
        cutoff = now - self.turn_retention_days * 86400
        turns = [r[0] for r in self._db.execute(
            "SELECT id FROM memories WHERE kind='message' AND active=0 AND ts < ? AND "
            "(json_extract(meta, '$.folded_into') IS NOT NULL "
            " OR json_extract(meta, '$.poisoned') IS NOT NULL)", (cutoff,))]
        facts = [r[0] for r in self._db.execute(
            "SELECT id FROM memories WHERE kind='fact' "
            "AND json_extract(meta, '$.expires_ts') IS NOT NULL "
            "AND json_extract(meta, '$.expires_ts') < ?", (now,))]
        gone = turns + facts
        for start in range(0, len(gone), 500):
            batch = gone[start:start + 500]
            marks = ",".join("?" * len(batch))
            self._db.execute(f"DELETE FROM vectors WHERE memory_id IN ({marks})", batch)
            self._db.execute(f"DELETE FROM memories WHERE id IN ({marks})", batch)
        self._db.commit()
        if gone:
            _log.info("cortex retention: deleted %d raw turns, %d expired facts",
                      len(turns), len(facts))
        return {"turns": len(turns), "facts": len(facts)}

    # ----------------------------------------------------------------- output
    @_synchronized
    def output(self, *, window_seconds: float = 86400.0) -> dict[str, Any]:
        """What the store has actually DONE lately, as opposed to what is in it:
        writes per namespace in the window (new rows plus lesson recurrences),
        the last write, raw turns waiting to be folded, the last consolidation,
        and embedding coverage. Counts, for doctor's wired-but-inert alarms."""
        now = self._now()
        since = now - window_seconds
        writes: dict[str, int] = {
            row["namespace"]: row["c"]
            for row in self._db.execute(
                # A row retired as a duplicate is counted as its lesson's recurrence
                # below, not again here; a turn retired by folding still counts.
                "SELECT namespace, COUNT(*) AS c FROM memories WHERE ts >= ? "
                "AND json_extract(meta, '$.merged_into') IS NULL GROUP BY namespace",
                (since,),
            )
        }
        recurring: list[dict[str, Any]] = []
        last_write = self._db.execute("SELECT MAX(ts) FROM memories").fetchone()[0]
        for row in self._db.execute(
            "SELECT id, text, meta FROM memories WHERE kind='lesson' AND active=1"
        ):
            meta = json.loads(row["meta"] or "{}")
            seen_at = float(meta.get("last_seen") or 0.0)
            burst = int(meta.get("burst") or 0)
            if seen_at < since or burst < 1:
                continue
            # A recurrence is a write too: it is the lesson being learned again.
            last_write = max(last_write or 0.0, seen_at)
            writes[LESSONS_NAMESPACE] = writes.get(LESSONS_NAMESPACE, 0) + burst
            recurring.append({"id": row["id"], "recurred_24h": burst,
                              "seen": int(meta.get("seen", 1)), "text": row["text"][:160]})
        recurring.sort(key=lambda r: r["recurred_24h"], reverse=True)
        pending = {
            row["namespace"]: {"turns": row["c"], "oldest_ts": row["oldest"]}
            for row in self._db.execute(
                "SELECT namespace, COUNT(*) AS c, MIN(ts) AS oldest FROM memories "
                "WHERE kind='message' AND active=1 GROUP BY namespace"
            )
        }
        last_folded = self._db.execute(
            "SELECT ts, namespace, episode_id, folded FROM consolidations "
            "WHERE outcome='folded' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        last_attempt = self._db.execute(
            "SELECT ts, namespace, outcome, detail FROM consolidations ORDER BY id DESC LIMIT 1"
        ).fetchone()
        poisoned = self._db.execute(
            "SELECT COUNT(*) FROM consolidations WHERE outcome='poisoned' AND ts >= ?",
            (since,),
        ).fetchone()[0]
        active = self._db.execute("SELECT COUNT(*) FROM memories WHERE active=1").fetchone()[0]
        coverage = None
        if self.embedder is not None:
            embedded = self._db.execute(
                "SELECT COUNT(*) FROM vectors v JOIN memories m ON m.id = v.memory_id "
                "WHERE m.active = 1 AND v.model = ?",
                (self.embedder.model,),
            ).fetchone()[0]
            coverage = round(100.0 * embedded / active, 1) if active else 100.0
        return {
            "window_s": window_seconds,
            "writes": writes,
            "last_write_ts": last_write,
            "recurring_lessons": recurring[:5],
            "raw_turns_pending": pending,
            "last_consolidation": dict(last_folded) if last_folded else None,
            "last_consolidation_attempt": dict(last_attempt) if last_attempt else None,
            "poisoned_chunks": poisoned,
            "embed_coverage_pct": coverage,
            "migration_error": self.migration_error,
        }

    # ------------------------------------------------------------- migrations
    def _migrate(self) -> None:
        """Bring an existing store up to _SCHEMA_VERSION. Never fatal: a store
        that cannot migrate still opens and serves, and the reason is kept in
        `migration_error` for doctor to shout about."""
        if str(self.path) == ":memory:":
            self._db.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            return
        try:
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version < 1:
                self._dedupe_lessons()
                self._db.execute("PRAGMA user_version=1")
                self._db.commit()
        except Exception as error:  # noqa: BLE001 - surfaced, never fatal
            self._db.rollback()
            self.migration_error = f"{type(error).__name__}: {error}"[:300]
            _log.error("cortex migration failed, store left as it was: %s", self.migration_error)

    def _dedupe_lessons(self) -> int:
        """Migration 1: collapse duplicate lessons into the oldest of each group.

        Reversible twice over: the whole store is copied to a backup file first
        (sqlite's online backup, consistent while another process has it open),
        and the duplicates are only retired (active=0, meta.merged_into=<kept
        id>), never deleted - `undo_lesson_dedupe()` puts them back."""
        groups: dict[str, list[sqlite3.Row]] = {}
        for row in self._db.execute(
            "SELECT id, ts, text, meta FROM memories "
            "WHERE namespace=? AND kind='lesson' AND active=1 ORDER BY id",
            (LESSONS_NAMESPACE,),
        ):
            groups.setdefault(lesson_key(row["text"]), []).append(row)
        dupes = {key: rows for key, rows in groups.items() if len(rows) > 1}
        if not dupes:
            return 0
        self.migration_backup = str(self._backup("pre-lesson-dedupe"))
        retired = 0
        for rows in dupes.values():
            keep, rest = rows[0], rows[1:]
            meta = json.loads(keep["meta"] or "{}")
            last = max(r["ts"] for r in rows)
            # The recurrences in the last day of the flood stay counted, so doctor
            # still says the mistake was repeating rather than going quiet about it.
            recent = [r["ts"] for r in rows if r["ts"] >= last - _BURST_SECONDS]
            meta.update({"seen": len(rows), "last_seen": last,
                         "burst_start": min(recent), "burst": len(recent) - 1,
                         "deduped": len(rest)})
            self._db.execute("UPDATE memories SET meta=? WHERE id=?",
                             (json.dumps(meta), keep["id"]))
            for r in rest:
                rmeta = json.loads(r["meta"] or "{}")
                rmeta["merged_into"] = keep["id"]
                self._db.execute("UPDATE memories SET active=0, meta=? WHERE id=?",
                                 (json.dumps(rmeta), r["id"]))
                retired += 1
        _log.warning("cortex: collapsed %d duplicate lessons into %d (backup %s)",
                     retired, len(dupes), self.migration_backup)
        return retired

    def _backup(self, tag: str) -> Path:
        """Copy the store (sqlite online backup) to `<stem>.<tag>-<ts>.db`. A copy
        whose bytes match the newest existing one is not kept twice, and only the
        newest _KEEP_BACKUPS are kept - a migration that keeps failing (the store
        locked by the server at every start) must not stack a 3.5 MB copy per start."""
        pattern = f"{self.path.stem}.{tag}-*{self.path.suffix}"

        def stamp(p: Path) -> int:
            try:
                return int(p.stem.rsplit("-", 1)[1])
            except (IndexError, ValueError):
                return 0

        existing = sorted(self.path.parent.glob(pattern), key=stamp)
        fresh = self.path.with_name(f"{self.path.stem}.{tag}-{int(self._now())}{self.path.suffix}")
        target = sqlite3.connect(str(fresh))
        try:
            self._db.backup(target)
        finally:
            target.close()
        digest = hashlib.sha256(fresh.read_bytes()).hexdigest()
        newest = existing[-1] if existing else None
        if (newest is not None and newest != fresh
                and hashlib.sha256(newest.read_bytes()).hexdigest() == digest):
            fresh.unlink()
            return newest
        kept = sorted({*existing, fresh}, key=stamp)
        for old in kept[:-_KEEP_BACKUPS]:
            try:
                old.unlink()
            except OSError:
                _log.warning("could not remove old backup %s", old)
        return fresh

    @_synchronized
    def undo_lesson_dedupe(self) -> int:
        """Reverse migration 1: re-activate every lesson it retired. Returns how
        many came back. (The pre-migration backup file is the other way back.)"""
        rows = list(self._db.execute(
            "SELECT id, meta FROM memories WHERE kind='lesson' AND active=0 "
            "AND json_extract(meta, '$.merged_into') IS NOT NULL"
        ))
        for r in rows:
            meta = json.loads(r["meta"])
            meta.pop("merged_into", None)
            self._db.execute("UPDATE memories SET active=1, meta=? WHERE id=?",
                             (json.dumps(meta), r["id"]))
        self._db.commit()
        return len(rows)


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

    Fail-open must also fail FAST. When Ollama cannot answer (2026-09-26: its
    scheduler wedged for 75 minutes waiting on an eviction that never finished),
    every call waited out the full timeout - every Pionir task took 30 s, and Moss's
    20 s business reads all failed. So a transport failure pauses the embedder:
    calls go straight to lexical for a cooldown that doubles with each failure in a
    row (60 s up to 15 min), and the first success clears it. The way back is
    automatic - the first call after the cooldown tries Ollama again.
    """

    BACKOFF_FIRST_SECONDS = 60.0
    BACKOFF_MAX_SECONDS = 900.0

    def __init__(
        self,
        model: str = "nomic-embed-text",
        base_url: str = "http://127.0.0.1:11434",
        timeout_seconds: int = 30,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._clock = clock
        self._pause_lock = threading.Lock()
        self._paused_until = 0.0
        self._backoff = 0.0

    def paused_for(self) -> float:
        """Seconds until the embedder is tried again (0 when it is live)."""
        with self._pause_lock:
            return max(0.0, self._paused_until - self._clock())

    def _pause(self, error: BaseException) -> None:
        with self._pause_lock:
            self._backoff = min(self.BACKOFF_MAX_SECONDS,
                                self._backoff * 2 or self.BACKOFF_FIRST_SECONDS)
            self._paused_until = self._clock() + self._backoff
            backoff = self._backoff
        _log.warning("embedder %s unreachable (%s); lexical recall only for %.0f s",
                     self._model, error, backoff)

    def _clear(self) -> None:
        with self._pause_lock:
            if self._backoff:
                _log.info("embedder %s answering again", self._model)
            self._backoff = 0.0
            self._paused_until = 0.0

    @property
    def model(self) -> str:
        return self._model

    def embed(self, texts: Sequence[str]) -> list[list[float]] | None:
        items = [t for t in texts]
        if not items:
            return []
        if self.paused_for() > 0:
            return None
        body = json.dumps({"model": self._model, "input": items}).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}/api/embed",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                raw = response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            self._pause(error)
            return None
        self._clear()
        try:
            document = json.loads(raw.decode("utf-8"))
        except ValueError:
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
