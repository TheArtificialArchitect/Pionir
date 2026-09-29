"""Is anything actually being posted? One view of the whole path, for doctor.

The posting path runs through three processes and a person: the crew's posters draft and
park each post (``crew/blog.py``), Pionir holds a routine public one for the owner's daily
digest (``batching.py``), the Discord gate posts that digest (``discord_gate.py``), the owner
approves, and the capability publishes. Each piece reported "ok" on its own while nothing
reached Instagram or the blog for days (2026-09-28): the blog's six drafts were all blocked,
the Instagram draft landed an hour after the digest, and nothing anywhere said so.

This reads the OUTPUT at each step - when a post was last parked, when the digest last
reached Discord, when a post was last published - and says in ``alerts`` which step has
gone quiet. The crew's own half (each poster's drafts, blocks and submissions, and its
no-output alarm) is its ``/api/health`` ``posting`` and ``alerts``, which doctor shows under
``specialists.crew``. Nothing here calls a network: it reads the queue and the gate's state.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any

# A digest that has not run for this long past its slot has missed a day.
DIGEST_LATE = timedelta(hours=2)


def _when(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return value if value.tzinfo is not None else value.astimezone()


def _ok(result: Any) -> bool | None:
    if not isinstance(result, Mapping):
        return None
    inner = result.get("result")
    if result.get("ok") is False or (isinstance(inner, Mapping) and inner.get("ok") is False):
        return False
    return result.get("ok") is True or None


def digest_capabilities(capabilities: Iterable[Mapping[str, Any]]) -> list[str]:
    """The capabilities that wait for the digest, by the server's own ``approval_level``
    (``cli._capabilities``), never a hand-kept list."""
    return sorted({str(c.get("name")) for c in capabilities if c.get("approval") == "digest"})


def posting_health(*, rows: Iterable[Mapping[str, Any]], capabilities: Iterable[Mapping[str, Any]],
                   digest: Any, gate: Mapping[str, Any] | None,
                   now_local: datetime) -> dict[str, Any]:
    """``rows`` are the approval queue's rows (``ApprovalQueue.recent``), ``digest`` its
    ``DigestSettings``, ``gate`` the Discord gate's ``state()`` (None: not started here)."""
    rows = [dict(r) for r in rows]
    caps = digest_capabilities(capabilities)
    per: dict[str, Any] = {}
    for name in caps:
        mine = [r for r in rows if r.get("capability") == name]
        parked = [r.get("created_at") for r in mine if r.get("created_at")]
        decided = [r for r in mine if r.get("status") not in ("pending", "running")]
        decided.sort(key=lambda r: str(r.get("resolved_at") or ""))
        done = [r for r in decided if r.get("status") == "approved" and _ok(r.get("result"))]
        last = decided[-1] if decided else None
        per[name] = {
            "last_parked_at": max(parked) if parked else None,
            "pending": sum(1 for r in mine if r.get("status") == "pending"),
            "last_decided": ({"id": last.get("id"), "status": last.get("status"),
                              "at": last.get("resolved_at"), "ok": _ok(last.get("result"))}
                             if last else None),
            "last_published_at": done[-1].get("resolved_at") if done else None,
        }
    gate_digest = (gate or {}).get("digest") or {}
    view: dict[str, Any] = {
        "capabilities": per,
        "digest": {
            "enabled": bool(getattr(digest, "enabled", False)),
            "time": getattr(digest, "time_text", None),
            "next": digest.next_digest(now_local).isoformat() if digest is not None else None,
            "last_run_at": gate_digest.get("last_run_at"),
            "last_sent_at": gate_digest.get("last_sent_at"),
            "last_run": gate_digest.get("last_run"),
            "batched_pending": sum(1 for r in rows
                                   if r.get("status") == "pending" and r.get("batch")),
        },
        "discord_gate": ({k: gate.get(k) for k in ("running", "configured", "enabled",
                                                   "channel_id", "accepting_answers",
                                                   "auth_failed", "reason", "last_error",
                                                   "last_ok_at", "errors")}
                         if gate is not None else {"running": False,
                                                   "reason": "not started in this process"}),
    }
    view["alerts"] = _alerts(view, rows, digest, gate, now_local)
    return view


def _alerts(view: dict[str, Any], rows: list, digest: Any, gate: Mapping[str, Any] | None,
            now_local: datetime) -> list[str]:
    alerts: list[str] = []
    on = bool(getattr(digest, "enabled", False))
    if gate is None or not gate.get("running"):
        why = (gate or {}).get("last_error") or (gate or {}).get("reason") or "not started"
        alerts.append(f"the Discord gate is NOT running ({why}): no approval card and no "
                      "digest reaches Discord; approve from the phone page")
    elif gate.get("auth_failed"):
        alerts.append("the Discord gate's token was refused: nothing reaches Discord")
    if on and gate is not None and gate.get("running"):
        last = _when(view["digest"]["last_run_at"])
        slot = digest.last_slot(now_local)
        if (last is None or last < slot) and now_local - slot > DIGEST_LATE:
            alerts.append(f"the daily digest due at {slot.isoformat(timespec='minutes')} has "
                          f"not run (last run: {view['digest']['last_run_at'] or 'never'})")
    today = now_local.date().isoformat()
    late = [r.get("id") for r in rows if r.get("status") == "pending" and r.get("batch")
            and isinstance(r.get("digest_date"), str) and r["digest_date"] < today]
    if late:
        alerts.append(f"{len(late)} batched approval(s) were in an earlier digest and still "
                      f"wait on the owner's answer: {', '.join(str(a) for a in late[:5])}")
    return alerts
