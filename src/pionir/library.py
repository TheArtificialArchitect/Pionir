"""The Library: Pionir's own memory (the cortex vault), read - never written.

Pionir Desktop's Library tab shows the owner what the bots remember: the shared
``lessons`` namespace every bot reads before it acts, each bot's private namespace,
the consolidated episodes and the facts drawn from them. These are its only doors:

    GET /api/library/overview                  counts per namespace and kind, last write
    GET /api/library/entries?namespace=&kind=&q=&mode=recall|text&before=&limit=&retired=
    GET /api/library/entry?id=                 one memory in full, what it links to,
                                               what links to it, and its slug-siblings

Three rules, all enforced here:

* **Read-only, structurally.** Every read opens its OWN connection to the cortex file
  with ``mode=ro`` and ``PRAGMA query_only`` - so no statement from this module can
  write, and a missing file is refused rather than created. Ranked search ranks the way
  ``Cortex.recall`` does (the same BM25 / fusion functions, pionir/cortex.py) but on
  that read-only connection - never under the live store's lock, so an owner's search
  can never hold up a bot's recall or a write. A query embedding, when an embedder is
  up, is asked for OUTSIDE any lock with a short timeout; slow or down, it is BM25.
* **The owner's surfaces only.** A request must be authenticated - signed (Pionir
  Desktop, pionir/auth.py request_sig) or the dashboard's session - as a client in
  ``READERS``. Unlike /api/state, loopback alone is not enough: a bot's private memory
  is the owner's to read, not every local process's.
* **Bounded.** Every argument is shape-checked; every query is parameterised, LIMITed
  and served off an index; a search is at most ``MAX_QUERY`` characters.
* **Scrubbed.** Every answer passes pionir/secretscrub.py (the shared pattern set, and
  this server's own client tokens) before it leaves.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from . import secretscrub
from .cortex import _cosine, _unpack, bm25_scored, fuse, tokens, weight_lexical

# The owner's surfaces. Fixed in code (like auth.APPROVERS): a grants file cannot add one.
READERS = frozenset({"desktop", "dashboard"})

MAX_QUERY = 200
MAX_LIMIT = 100
DEFAULT_LIMIT = 30
MAX_RELATED = 20
PREVIEW_CHARS = 280
# How long an owner's search waits for a query embedding before ranking by BM25 alone.
EMBED_TIMEOUT = 2.0
_NAME = re.compile(r"[A-Za-z0-9_.:@-]{1,64}")
_SLUG = re.compile(r"[^\x00-\x1f]{1,200}")


class BadRequest(ValueError):
    """An argument the library refuses; its message is for the caller."""


# ---- read-only access --------------------------------------------------------------------
def open_ro(path: str | Path) -> sqlite3.Connection:
    """A connection that cannot write: the file opened ``mode=ro`` (a missing file is an
    error, never created) and ``query_only`` on top, so even a PRAGMA cannot turn it back."""
    p = Path(path)
    if str(p) == ":memory:" or not p.is_file():
        raise FileNotFoundError(f"no memory store at {p}")
    db = sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0,
                         check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only = ON")
    return db


@contextmanager
def _reading(path: str | Path) -> Iterator[sqlite3.Connection]:
    """A read-only connection for one request, closed after it (never held open)."""
    db = open_ro(path)
    try:
        yield db
    finally:
        db.close()


def _json(raw: Any, default: Any) -> Any:
    try:
        value = json.loads(raw) if isinstance(raw, str) else default
    except ValueError:
        return default
    return value if isinstance(value, type(default)) else default


def _summary(row: Mapping[str, Any], *, score: float | None = None, via: str = "") -> dict[str, Any]:
    text = str(row["text"] or "")
    out = {
        "id": int(row["id"]),
        "ts": float(row["ts"]),
        "namespace": row["namespace"],
        "kind": row["kind"],
        "preview": text if len(text) <= PREVIEW_CHARS else text[: PREVIEW_CHARS - 1] + "…",
        "chars": len(text),
        "salience": float(row["salience"]),
        "slug": row["slug"],
        "active": bool(row["active"]),
    }
    if score is not None:
        out["score"] = round(score, 4)
    if via:
        out["via"] = via
    return out


def _full(row: Mapping[str, Any]) -> dict[str, Any]:
    out = _summary(row)
    out.pop("preview")
    out["text"] = str(row["text"] or "")
    out["links"] = [str(s) for s in _json(row["links"], []) if isinstance(s, str)][:MAX_RELATED]
    out["meta"] = _json(row["meta"], {})
    return out


# ---- arguments ---------------------------------------------------------------------------
def _one(query: Mapping[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if not values:
        return None
    if len(values) > 1:
        raise BadRequest(f"{key} given more than once")
    return values[0]


def _name(query: Mapping[str, list[str]], key: str) -> str | None:
    value = _one(query, key)
    if value is None or value == "":
        return None
    if not _NAME.fullmatch(value):
        raise BadRequest(f"{key} is not a name")
    return value


def _int(query: Mapping[str, list[str]], key: str, *, low: int, high: int) -> int | None:
    value = _one(query, key)
    if value is None or value == "":
        return None
    if not re.fullmatch(r"[0-9]{1,12}", value):
        raise BadRequest(f"{key} must be a whole number")
    number = int(value)
    if not low <= number <= high:
        raise BadRequest(f"{key} is out of range")
    return number


# ---- the three reads ---------------------------------------------------------------------
def overview(path: str | Path, embedder: Any = None) -> dict[str, Any]:
    """What is in the store: per namespace (count, kinds, last write), per kind, the total,
    how many are retired, and whether recall is hybrid. All GROUP BYs on indexed columns."""
    p = Path(path)
    with _reading(p) as db:
        namespaces: dict[str, dict[str, Any]] = {}
        for row in db.execute(
            "SELECT namespace, kind, COUNT(*) AS c, MAX(ts) AS last FROM memories "
            "WHERE active = 1 GROUP BY namespace, kind"
        ):
            slot = namespaces.setdefault(row["namespace"], {"namespace": row["namespace"],
                                                            "count": 0, "last_ts": None,
                                                            "kinds": {}})
            slot["count"] += row["c"]
            slot["kinds"][row["kind"]] = row["c"]
            if row["last"] is not None and (slot["last_ts"] is None or row["last"] > slot["last_ts"]):
                slot["last_ts"] = float(row["last"])
        kinds: dict[str, int] = {}
        for slot in namespaces.values():
            for kind, count in slot["kinds"].items():
                kinds[kind] = kinds.get(kind, 0) + count
        totals = db.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(active = 0), 0) AS retired, MAX(ts) AS last "
            "FROM memories").fetchone()
        linked = db.execute("SELECT COUNT(*) FROM memories WHERE active = 1 AND links != '[]'"
                            ).fetchone()[0]
        # hybrid only where vectors exist for the live model - counted here, not through
        # Cortex.stats(), which would take the live store's lock
        model = getattr(embedder, "model", None)
        embedded = 0
        if model:
            embedded = db.execute(
                "SELECT COUNT(*) FROM vectors v JOIN memories m ON m.id = v.memory_id "
                "WHERE m.active = 1 AND v.model = ?", (model,)).fetchone()[0]
    return {
        "path": str(p),
        "size_bytes": p.stat().st_size,
        "total": int(totals["n"]) - int(totals["retired"]),
        "retired": int(totals["retired"]),
        "last_ts": None if totals["last"] is None else float(totals["last"]),
        "linked": int(linked),
        "namespaces": sorted(namespaces.values(), key=lambda s: (s["namespace"] != "lessons",
                                                                 -s["count"], s["namespace"])),
        "kinds": dict(sorted(kinds.items(), key=lambda kv: -kv[1])),
        "recall": "hybrid" if (model and embedded) else "lexical",
        "embedded": int(embedded),
    }


def query_vector(embedder: Any, text: str, timeout: float = EMBED_TIMEOUT) -> list[float] | None:
    """The query's embedding, asked for outside any lock and waited on for ``timeout``
    seconds at most; None (rank by BM25) when there is no embedder, it is paused after
    a failure, it is slow, or it answers nothing usable. The call itself runs in a
    daemon thread: a wedged Ollama costs the owner at most ``timeout``, never the call."""
    if embedder is None:
        return None
    paused = getattr(embedder, "paused_for", None)
    if callable(paused) and paused() > 0:
        return None
    box: list[Any] = []

    def ask() -> None:
        try:
            box.append(embedder.embed([text]))
        except Exception:  # noqa: BLE001 - fail-open, like the cortex
            box.append(None)

    worker = threading.Thread(target=ask, name="pionir-library-embed", daemon=True)
    worker.start()
    worker.join(timeout)
    if not box or not box[0]:
        return None
    vec = box[0][0]
    return vec if isinstance(vec, list) and vec else None


def ranked(db: sqlite3.Connection, query: str, k: int, *, namespace: str | None,
           kind: str | None, embedder: Any = None, now: float | None = None,
           timeout: float = EMBED_TIMEOUT) -> list[tuple[float, Any]]:
    """Recall-ranked memories, as ``Cortex.recall`` ranks them (BM25, fused with cosine
    when a query embedding and stored vectors exist), computed on this read-only
    connection - the live store's lock is never taken."""
    q = tokens(query)
    if not q:
        return []
    where, params = ["active = 1"], []
    if namespace:
        where.append("namespace = ?")
        params.append(namespace)
    if kind:
        where.append("kind = ?")
        params.append(kind)
    candidates = db.execute("SELECT * FROM memories WHERE " + " AND ".join(where),
                            params).fetchall()
    if not candidates:
        return []
    now = time.time() if now is None else now
    lexical = bm25_scored(q, candidates)
    semantic: list[tuple[float, Any]] = []
    vec = query_vector(embedder, query, timeout)
    if vec is not None:
        by_id = {c["id"]: c for c in candidates}
        for r in db.execute("SELECT memory_id, vec FROM vectors WHERE model = ?",
                            (embedder.model,)):
            row = by_id.get(r["memory_id"])
            if row is not None:
                score = _cosine(vec, _unpack(r["vec"]))
                if score > 0:
                    semantic.append((score, row))
        semantic.sort(key=lambda pair: pair[0], reverse=True)
    return (fuse(lexical, semantic, now) if semantic else weight_lexical(lexical, now))[:k]


def entries(path: str | Path, query: Mapping[str, list[str]], embedder: Any = None, *,
            embed_timeout: float = EMBED_TIMEOUT) -> dict[str, Any]:
    """A page of memories, newest first (keyset paging on id: ``before`` is the last id of
    the previous page). With ``q``: ``mode=recall`` ranks by the bots' own recall (BM25,
    hybrid when vectors exist), ``mode=text`` finds the words as written, newest first."""
    namespace = _name(query, "namespace")
    kind = _name(query, "kind")
    before = _int(query, "before", low=1, high=2**62)
    limit = _int(query, "limit", low=1, high=MAX_LIMIT) or DEFAULT_LIMIT
    retired = _one(query, "retired") in ("1", "true")
    q = (_one(query, "q") or "").strip()
    if len(q) > MAX_QUERY:
        raise BadRequest(f"a search is at most {MAX_QUERY} characters")
    if any(ord(c) < 32 for c in q):
        raise BadRequest("a search is plain text")
    mode = _one(query, "mode") or "recall"
    if mode not in ("recall", "text"):
        raise BadRequest("mode is recall or text")

    if q and mode == "recall":
        with _reading(path) as db:
            hits = ranked(db, q, limit, namespace=namespace, kind=kind, embedder=embedder,
                          timeout=embed_timeout)
        return {"entries": [_summary(row, score=score) for score, row in hits],
                "next_before": None, "mode": "recall"}

    where: list[str] = [] if retired else ["active = 1"]
    params: list[Any] = []
    if namespace:
        where.append("namespace = ?")
        params.append(namespace)
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if before:
        where.append("id < ?")
        params.append(before)
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("text LIKE ? ESCAPE '\\'")
        params.append(f"%{escaped}%")
    sql = ("SELECT id, ts, namespace, kind, text, salience, slug, active FROM memories"
           + (" WHERE " + " AND ".join(where) if where else "")
           + " ORDER BY id DESC LIMIT ?")
    with _reading(path) as db:
        rows = db.execute(sql, [*params, limit + 1]).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    return {"entries": [_summary(r) for r in rows],
            "next_before": int(rows[-1]["id"]) if more and rows else None,
            "mode": "text" if q else "browse"}


def entry(path: str | Path, query: Mapping[str, list[str]]) -> dict[str, Any] | None:
    """One memory in full, and its neighbours in the link graph - each kept inside its own
    namespace, as recall's link expansion is (a private memory never surfaces through
    another namespace's link): what it links to (``[[slug]]``), what links to it, and
    the other memories that share its slug."""
    memory_id = _int(query, "id", low=1, high=2**62)
    if memory_id is None:
        raise BadRequest("id is required")
    cols = "id, ts, namespace, kind, text, salience, slug, active"
    with _reading(path) as db:
        row = db.execute(f"SELECT {cols}, links, meta FROM memories WHERE id = ?",
                         (memory_id,)).fetchone()
        if row is None:
            return None
        full = _full(row)
        ns = row["namespace"]
        links_to: list[dict[str, Any]] = []
        slugs = [s for s in full["links"] if _SLUG.fullmatch(s)]
        if slugs:
            marks = ",".join("?" * len(slugs))
            for r in db.execute(
                f"SELECT {cols} FROM memories WHERE active = 1 AND namespace = ? "
                f"AND slug IN ({marks}) AND id != ? ORDER BY id DESC LIMIT ?",
                [ns, *slugs, memory_id, MAX_RELATED],
            ):
                links_to.append(_summary(r, via=str(r["slug"])))
        linked_from: list[dict[str, Any]] = []
        siblings: list[dict[str, Any]] = []
        slug = row["slug"]
        if slug:
            # links is a JSON list of strings: match the quoted slug, parameterised
            needle = "%" + json.dumps(slug).replace("\\", "\\\\").replace("%", "\\%") \
                .replace("_", "\\_") + "%"
            for r in db.execute(
                f"SELECT {cols}, links FROM memories WHERE active = 1 AND namespace = ? "
                f"AND id != ? AND links LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT ?",
                (ns, memory_id, needle, MAX_RELATED * 4),
            ):
                if slug in _json(r["links"], []):
                    linked_from.append(_summary(r))
                if len(linked_from) >= MAX_RELATED:
                    break
            siblings = [_summary(r) for r in db.execute(
                f"SELECT {cols} FROM memories WHERE active = 1 AND namespace = ? AND slug = ? "
                f"AND id != ? ORDER BY id DESC LIMIT ?", (ns, slug, memory_id, MAX_RELATED))]
            same_slug = db.execute(
                "SELECT COUNT(*) FROM memories WHERE active = 1 AND namespace = ? AND slug = ?",
                (ns, slug)).fetchone()[0]
        else:
            same_slug = 0
    full["links_to"] = links_to
    full["linked_from"] = linked_from
    full["same_slug"] = siblings
    full["same_slug_total"] = int(same_slug)
    return full


# ---- the HTTP door -----------------------------------------------------------------------
def serve(path_info: str, query_string: str, *, client: str | None, refused: str | None,
          store: Callable[[], tuple[Path | None, Any]],
          known: Callable[[], Iterable[str]] = tuple) -> tuple[int, dict[str, Any]]:
    """(status, document) for one GET under /api/library/. ``client``/``refused`` are the
    caller as auth.py identified it; ``store`` gives (the cortex file, its embedder or
    None); ``known`` gives values that are secrets here (this server's client tokens).
    Every answer is scrubbed (pionir/secretscrub.py) before it is returned."""
    if refused is not None:
        return 401, {"error": "unauthorized", "reason": refused}
    if client is None:
        return 401, {"error": "unauthorized", "reason": "the library needs the owner's signed client"}
    if client not in READERS:
        return 403, {"error": "forbidden", "reason": f"{client} may not read the library"}
    try:
        query = parse_qs(query_string, keep_blank_values=True, max_num_fields=16,
                         strict_parsing=False)
    except ValueError:
        return 400, {"error": "bad query"}
    path, embedder = store()
    if path is None:
        return 503, {"error": "no memory store", "reason": "this Pionir has no cortex file"}
    status, document = _read(path_info, query, path, embedder)
    return status, secretscrub.scrub_value(document, known())


def _read(path_info: str, query: Mapping[str, list[str]], path: Path,
          embedder: Any) -> tuple[int, dict[str, Any]]:
    try:
        if path_info == "/api/library/overview":
            if query:
                raise BadRequest("overview takes no arguments")
            return 200, overview(path, embedder)
        if path_info == "/api/library/entries":
            return 200, entries(path, query, embedder)
        if path_info == "/api/library/entry":
            found = entry(path, query)
            return (200, found) if found is not None else (404, {"error": "not found"})
    except BadRequest as error:
        return 400, {"error": "bad request", "reason": str(error)}
    except FileNotFoundError:
        return 503, {"error": "no memory store", "reason": "the cortex file is not there yet"}
    except sqlite3.Error as error:
        return 503, {"error": "store unavailable", "reason": type(error).__name__}
    return 404, {"error": "not found"}
