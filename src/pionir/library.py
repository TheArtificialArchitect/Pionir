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
  write, and a missing file is refused rather than created. Ranked search goes through
  ``Cortex.recall`` (the same recall the bots use), which reads and never writes.
* **The owner's surfaces only.** A request must be authenticated - signed (Pionir
  Desktop, pionir/auth.py request_sig) or the dashboard's session - as a client in
  ``READERS``. Unlike /api/state, loopback alone is not enough: a bot's private memory
  is the owner's to read, not every local process's.
* **Bounded.** Every argument is shape-checked; every query is parameterised, LIMITed
  and served off an index; a search is at most ``MAX_QUERY`` characters.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

# The owner's surfaces. Fixed in code (like auth.APPROVERS): a grants file cannot add one.
READERS = frozenset({"desktop", "dashboard"})

MAX_QUERY = 200
MAX_LIMIT = 100
DEFAULT_LIMIT = 30
MAX_RELATED = 20
PREVIEW_CHARS = 280
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
def overview(path: str | Path, cortex: Any = None) -> dict[str, Any]:
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
    stats: dict[str, Any] = {}
    if cortex is not None:
        try:
            stats = cortex.stats()
        except Exception:  # noqa: BLE001 - the counts above stand without it
            stats = {}
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
        "recall": stats.get("recall", "lexical"),
        "embedded": stats.get("embedded", 0),
    }


def entries(path: str | Path, query: Mapping[str, list[str]], cortex: Any = None) -> dict[str, Any]:
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
        if cortex is None:
            raise BadRequest("ranked recall needs the memory engine; use mode=text")
        hits = cortex.recall(q, k=limit, namespace=namespace,
                             kinds=[kind] if kind else None)
        rows = [{"id": m.id, "ts": m.ts, "namespace": m.namespace, "kind": m.kind,
                 "text": m.text, "salience": m.salience, "slug": m.slug, "active": 1}
                for m in hits]
        return {"entries": [_summary(r, score=m.score) for r, m in zip(rows, hits)],
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
          store: Callable[[], tuple[Path | None, Any]]) -> tuple[int, dict[str, Any]]:
    """(status, document) for one GET under /api/library/. ``client``/``refused`` are the
    caller as auth.py identified it; ``store`` gives (the cortex file, the Cortex)."""
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
    path, cortex = store()
    if path is None:
        return 503, {"error": "no memory store", "reason": "this Pionir has no cortex file"}
    try:
        if path_info == "/api/library/overview":
            if query:
                raise BadRequest("overview takes no arguments")
            return 200, overview(path, cortex)
        if path_info == "/api/library/entries":
            return 200, entries(path, query, cortex)
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
