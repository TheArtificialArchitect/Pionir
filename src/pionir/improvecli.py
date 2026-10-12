"""``pionir improve ...``: the self-improvement ledger, and Ian's way back.

    python -m pionir improve status      every candidate, its state and why, and the counters
    python -m pionir improve undo <id>   git revert of the commit that applied <id>

Plain text out, exit 0 on success and 1 on any error. No runtime, no server, no network, no
model: it reads the crew worker's ledger (``<state>/crew/workers/builds.improve.json``) and the
candidate's files in the improve folder, and ``undo`` runs ``git revert`` in the Pionir repo -
only on a clean ``main`` that still contains that commit. It never pushes.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from .config import PionirSettings
from .selfimprove import apply as applier
from .selfimprove import store


def add_parsers(commands: Any) -> None:
    improve = commands.add_parser(
        "improve", help="the self-improvement worker's candidates; undo an applied one",
        description="The crew's builds.improve worker proposes fixes to Pionir; each reaches "
                    "you as an approval card and is applied only on your yes.")
    sub = improve.add_subparsers(dest="improve_command", required=True)
    sub.add_parser("status", help="every candidate, its state and why, and the counters")
    undo = sub.add_parser("undo", help="git revert the commit that applied a candidate")
    undo.add_argument("id", help="the candidate id, e.g. imp-20261012-ab12")


def _when(t) -> str:
    try:
        return datetime.fromtimestamp(float(t)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "-"


def merged_state(cand: dict, improve_dir) -> tuple:
    """(state, why) of a candidate, with the outcome files folded in - so status is right
    even before the worker's next run has read them."""
    state, why = cand.get("state") or "?", cand.get("why") or ""
    cid = cand.get("id")
    try:
        where = store.folder(improve_dir, cid)
    except store.StoreError:
        return state, why
    undone = store.read_json(where / store.UNDONE)
    if undone and undone.get("undone"):
        return "undone", f"reverted by {str(undone.get('revert_commit') or '')[:10]}"
    outcome = store.read_json(where / store.OUTCOME)
    if outcome and state in ("awaiting_owner", "applied"):
        if outcome.get("applied") and outcome.get("reverted"):
            return "undone", str(outcome.get("why") or "reverted after its tests failed")
        if outcome.get("applied"):
            return "applied", f"commit {str(outcome.get('commit') or '')[:10]}"
        if outcome.get("stale"):
            return "stale", str(outcome.get("why") or "")
    return state, why


def crew_state_dir(settings: PionirSettings) -> Path:
    """The crew's state folder, as the crew itself finds it (crew/config.py)."""
    raw = os.environ.get("PIONIR_CREW_STATE_DIR", "").strip()
    return Path(raw) if raw else settings.state_root / "crew"


def status(settings: PionirSettings, out: TextIO) -> int:
    path = store.ledger_path(crew_state_dir(settings))
    ledger = store.read_json(path)
    if ledger is None:
        print(f"No self-improvement ledger yet: the builds.improve worker has not run ({path}).",
              file=out)
        return 0
    counts = ledger.get("counts") if isinstance(ledger.get("counts"), dict) else {}
    print(f"Proposals made: {int(counts.get('proposals') or 0)}  applied: "
          f"{int(counts.get('applied') or 0)}  denied: {int(counts.get('denied') or 0)}  "
          f"shelved: {int(counts.get('shelved') or 0)}", file=out)
    if ledger.get("amber"):
        print(f"AMBER: {ledger['amber']}", file=out)
    cands = sorted((c for c in (ledger.get("candidates") or {}).values() if isinstance(c, dict)),
                   key=lambda c: float(c.get("created_at") or 0), reverse=True)
    if not cands:
        print("No candidates yet.", file=out)
    for c in cands:
        state, why = merged_state(c, settings.improve_path)
        ev = c.get("evidence") if isinstance(c.get("evidence"), dict) else {}
        print(f"{c.get('id')}  {state:<14} {_when(c.get('created_at'))}  "
              f"{ev.get('worker', '?')}: {str(ev.get('reason') or '')[:90]}", file=out)
        if why:
            print(f"    {str(why)[:200]}", file=out)
    return 0


def undo(settings: PionirSettings, cid: str, out: TextIO, *, git=applier.real_git) -> int:
    try:
        where = store.folder(settings.improve_path, cid)
    except store.StoreError as exc:
        print(f"error: {exc}", file=out)
        return 1
    outcome = store.read_json(where / store.OUTCOME)
    if not outcome or not outcome.get("applied") or not outcome.get("commit"):
        print(f"error: {cid} was never applied (no applied outcome in {where})", file=out)
        return 1
    if outcome.get("reverted"):
        print(f"{cid} was already reverted automatically "
              f"({str(outcome.get('revert_commit') or '')[:10]}).", file=out)
        return 0
    already = store.read_json(where / store.UNDONE)
    if already and already.get("undone"):
        print(f"{cid} is already undone ({str(already.get('revert_commit') or '')[:10]}).",
              file=out)
        return 0
    done = applier.undo(settings.pionir_repo_path, str(outcome["commit"]), cid, git=git)
    if not done.get("undone"):
        print(f"error: {done.get('why')}", file=out)
        return 1
    store.write_json(where / store.UNDONE, {**done, "at": datetime.now().timestamp()})
    print(f"Undone: {cid} - commit {str(outcome['commit'])[:10]} reverted by "
          f"{done['revert_commit'][:10]} on main of {settings.pionir_repo_path}. Nothing was "
          "pushed; restart Pionir and the crew when you want the old behaviour back.", file=out)
    return 0


def run(args: argparse.Namespace, settings: PionirSettings, *, out: TextIO = sys.stdout,
        git=applier.real_git) -> int:
    if args.improve_command == "status":
        return status(settings, out)
    if args.improve_command == "undo":
        return undo(settings, args.id, out, git=git)
    print(f"error: unknown improve command {args.improve_command!r}", file=out)
    return 1
