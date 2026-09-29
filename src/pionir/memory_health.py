"""Is the memory engine DOING anything? Its output counter, for doctor.

``cortex.stats()`` says what is IN the store. That is the check that failed on
2026-09-28: doctor showed 800 memories, hybrid recall, 100% embedded - a healthy
looking store - while every one of the 800 was a lesson, 794 of them copies of
the same six, nothing else had ever been written, consolidation had never run,
and the lessons recalled before each task were thrown away. Nothing said so.

This reads what the store has DONE: writes in the last 24 h per namespace (a
lesson recurring counts - it is the mistake being learned again), raw turns
waiting to be folded, when consolidation last folded and last tried, embedding
coverage - and says in ``alerts`` which part has stalled. It compares against the
ledger where it can: tasks failing while no lesson is written means the lesson
writer is broken, not that nothing went wrong. Nothing here writes or calls a
network.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from .consolidate import AUTO_CONSOLIDATE_AT

WINDOW_SECONDS = 86400.0
# This many failed tasks in the window with no lesson written or re-seen: stalled.
LESSON_STALL_FAILURES = 3
# A lesson recurring this often in 24 h is being recalled and not heeded.
RECURRENCE_ALARM = 20
# Raw turns past the fold threshold and waiting longer than this: stalled.
CONSOLIDATION_STALL_SECONDS = 6 * 3600.0
# Below this share of active memories with a vector, recall is partly lexical.
EMBED_COVERAGE_FLOOR = 95.0


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds") if ts else None


def _when(raw: Any) -> float | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).timestamp()


def memory_health(cortex: Any, events: Iterable[Mapping[str, Any]], *, now: float,
                  consolidate_at: int = AUTO_CONSOLIDATE_AT) -> dict[str, Any]:
    """``events`` are recent ledger events (``audit_sink.recent``), newest first."""
    out = cortex.output(window_seconds=WINDOW_SECONDS)
    stats = cortex.stats()
    since = now - WINDOW_SECONDS
    failed = sum(1 for e in events if e.get("event_type") == "task.failed"
                 and (_when(e.get("occurred_at")) or 0.0) >= since)
    view: dict[str, Any] = {
        "writes_24h": out["writes"],
        "last_write_at": _iso(out["last_write_ts"]),
        "task_failures_24h": failed,
        "recurring_lessons": out["recurring_lessons"],
        "raw_turns_pending": {
            ns: {"turns": v["turns"], "oldest_at": _iso(v["oldest_ts"])}
            for ns, v in out["raw_turns_pending"].items()
        },
        "last_consolidation": (
            {**out["last_consolidation"], "at": _iso(out["last_consolidation"]["ts"])}
            if out["last_consolidation"] else None
        ),
        "last_consolidation_attempt": (
            {**out["last_consolidation_attempt"],
             "at": _iso(out["last_consolidation_attempt"]["ts"])}
            if out["last_consolidation_attempt"] else None
        ),
        "embed_coverage_pct": out["embed_coverage_pct"],
    }
    alerts: list[str] = []
    notes: list[str] = []
    if out.get("migration_error"):
        alerts.append(f"MEMORY: the store's migration failed ({out['migration_error']}); "
                      "duplicate lessons were NOT collapsed")
    if failed >= LESSON_STALL_FAILURES and not out["writes"].get("lessons"):
        alerts.append(f"MEMORY: {failed} tasks failed in the last 24 h but no lesson was "
                      "written or re-seen - the lesson writer has stalled")
    for lesson in out["recurring_lessons"]:
        if lesson["recurred_24h"] >= RECURRENCE_ALARM:
            alerts.append(f"MEMORY: lesson #{lesson['id']} recurred {lesson['recurred_24h']} "
                          f"times in 24 h - recalled and handed back, and still repeated: "
                          f"\"{lesson['text'][:120]}\"")
    attempt = out["last_consolidation_attempt"]
    for ns, pending in out["raw_turns_pending"].items():
        waited = now - float(pending["oldest_ts"])
        if pending["turns"] >= consolidate_at and waited > CONSOLIDATION_STALL_SECONDS:
            last = (f"last attempt {attempt['outcome']}"
                    + (f": {attempt['detail']}" if attempt.get("detail") else "")
                    if attempt else "never attempted")
            alerts.append(f"MEMORY: consolidation stalled - {pending['turns']} raw turns in "
                          f"'{ns}' waiting {waited / 3600:.0f} h ({last})")
    if stats.get("embed_model"):
        if stats.get("embed_paused_s", 0) > 0:
            alerts.append(f"MEMORY: the embedder is paused for {stats['embed_paused_s']:.0f} s "
                          f"after failing ({stats.get('last_embed_error') or 'unreachable'}); "
                          "recall is lexical only")
        coverage = out["embed_coverage_pct"]
        if coverage is not None and coverage < EMBED_COVERAGE_FLOOR:
            alerts.append(f"MEMORY: only {coverage}% of memories have a vector; the rest are "
                          "recalled lexically only (they backfill as the embedder answers)")
    if not out["raw_turns_pending"] and out["last_consolidation"] is None:
        notes.append("no raw turns waiting and none ever folded: turns come from the voice's "
                     f"/api/intent exchanges, folded at {consolidate_at}")
    view["alerts"] = alerts
    view["notes"] = notes
    return view
