"""Run every division leader against a COPY of a crew's state with the REAL local model.

    PYTHONPATH=src python tools/leader_trial.py                       # the live crew, copied
    PYTHONPATH=src python tools/leader_trial.py --source DIR --runs 5 --out results.json

Why: a leader's report is only worth something if the shared brain (gemma3:12b through
Ollama) actually produces one that passes the checks, and unit tests with a fake model
cannot say how often it does. This asks the real model, the way the crew does - the real
``Brain`` (so the request body, the JSON schema, temperature 0 and Pionir's GPU lock are
exactly the crew's) and the real ``Leader`` - and counts, per division, how many runs
ended as:

- ``model``     - the model's report passed validation and grounding (after at most the
                  one repair retry the leader allows);
- ``fallback``  - the model's words failed, and the leader sent up its deterministic
                  figures-only report instead;
- ``rejected``  - nothing usable reached Moss;
- ``abstained`` / ``error`` - no report was attempted, or the run raised.

It never touches the crew it reads. ``--source`` (default ``~/.pionir/crew``) is only
READ: its ``crew.db`` (with its ``-wal``/``-shm``) is copied into a temporary directory,
and every trial runs on its own fresh copy there. Run k of a division replays the store as
it stood at an earlier moment (the outputs and runs after that moment are removed from the
copy, and every report too, so the leader has something new to say), spread evenly over
the division's history - so five runs are five different briefs, not one brief five times
at temperature 0. The raw model answers are written beside the results for diagnosis.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

from pionir.config import PionirSettings
from pionir.crew.brain import Brain
from pionir.crew.config import CrewBudget, CrewSettings
from pionir.crew.gpu import CardWatch
from pionir.crew.leader import Leader
from pionir.crew.registry import default_registry
from pionir.crew.store import CrewStore


class _Clock:
    t = 0


class _Shim:
    """The little of a running crew the brain reads: its store, clock, lock and pause."""

    def __init__(self, store) -> None:
        self.store = store
        self.clock = _Clock()
        self.lock = threading.Lock()
        self.paused_reason = None
        self.monitor = None


def snapshot(source: Path, dest: Path) -> Path:
    """Copy crew.db and its WAL files, then fold the WAL into the copy (the copy only)."""
    dest.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        src = source / f"crew.db{suffix}"
        if src.exists():
            shutil.copyfile(src, dest / f"crew.db{suffix}")
    db = dest / "crew.db"
    with sqlite3.connect(db) as c:
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return db


def moments(db: Path, division: str, n: int) -> list:
    """n moments spread over the division's history: each just after a worker recorded
    something (latest last), so trial k sees the store as the leader would have seen it
    then. A division that never recorded anything gets one moment: now."""
    with sqlite3.connect(db) as c:
        times = [r[0] for r in c.execute(
            "SELECT DISTINCT r.finished_at FROM runs r WHERE r.division=? AND r.written > 0 "
            "AND r.worker_id NOT LIKE 'leader.%' ORDER BY r.finished_at", (division,))]
    if not times:
        return [time.time()] * n
    if len(times) <= n:
        picks = times
    else:
        picks = [times[round(i * (len(times) - 1) / (n - 1))] for i in range(n)] if n > 1 \
            else [times[-1]]
    while len(picks) < n:
        picks.append(picks[-1])
    return [t + 1.0 for t in picks]


def rewind(db: Path, at: float) -> None:
    """Make the copy look as it did at ``at``, with no report yet (so there is news)."""
    with sqlite3.connect(db) as c:
        c.execute("DELETE FROM outputs WHERE observed_at > ?", (at,))
        c.execute("DELETE FROM runs WHERE finished_at > ?", (at,))
        c.execute("DELETE FROM reports")
        c.execute("DELETE FROM brain_calls")


def classify(result, row: dict | None) -> str:
    if row is None:
        return "error"
    if row["status"] == "abstained":
        return "abstained"
    if row["status"] == "rejected":
        return "rejected"
    return "fallback" if (row.get("provenance") or {}).get("composed") == "figures_only" \
        else "model"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source", type=Path, default=Path.home() / ".pionir" / "crew",
                    help="a crew state dir to READ (its crew.db is copied, never written)")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--divisions", nargs="*", default=None)
    ap.add_argument("--model", default="gemma3:12b")
    ap.add_argument("--out", type=Path, default=None, help="write the results here (JSON)")
    ap.add_argument("--label", default="")
    args = ap.parse_args(argv)

    registry = default_registry()
    divisions = args.divisions or list(registry.division_ids())
    lock_path = PionirSettings.from_environment().gpu_lock_path
    tmp = Path(tempfile.mkdtemp(prefix="leader-trial-"))
    base = snapshot(args.source, tmp / "source")
    results: dict = {"label": args.label, "model": args.model, "source": str(args.source),
                     "divisions": {}}
    try:
        for division in divisions:
            tally: dict = {}
            trials = []
            for k, at in enumerate(moments(base, division, args.runs)):
                state = tmp / f"{division}-{k}"
                state.mkdir()
                db = state / "crew.db"
                shutil.copyfile(base, db)
                rewind(db, at)
                cfg = CrewSettings(state_dir=state, gpu_lock_path=lock_path, model=args.model,
                                   api_port=None, budget=CrewBudget(calls_per_hour=10_000))
                store = CrewStore(db)
                brain = Brain(cfg, _Shim(store), card=CardWatch(lock_path))
                brain.start()
                answers: list = []

                def ask(*a, _brain=brain, _answers=answers, **kw):
                    text, meta, err = _brain.ask(*a, **kw)
                    _answers.append({"text": text, "err": err,
                                     "prompt_tokens": (meta or {}).get("prompt_eval_count"),
                                     "out_tokens": (meta or {}).get("eval_count")})
                    return text, meta, err

                leader = Leader(division, registry, store, ask=ask, model=args.model,
                                clock=lambda _at=at: _at)
                t0 = time.time()
                try:
                    result = leader.run()
                    rows = store.reports(division=division, limit=1)
                    row = rows[0] if rows else None
                finally:
                    brain.stop()
                    store.close()
                outcome = classify(result, row)
                tally[outcome] = tally.get(outcome, 0) + 1
                trial = {"at": at, "outcome": outcome, "seconds": round(time.time() - t0, 1),
                         "calls": len(answers), "answers": answers,
                         "status": row and row["status"], "reason": row and row["reason"],
                         "headline": row and row["headline"], "summary": row and row["summary"],
                         "provenance": row and row["provenance"]}
                trials.append(trial)
                print(f"{division} run {k + 1}: {outcome} ({trial['seconds']} s, "
                      f"{len(answers)} call(s)) {(row or {}).get('reason') or ''}"[:300],
                      flush=True)
            results["divisions"][division] = {"tally": tally, "trials": trials}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\nper division (model / fallback / rejected / abstained / error):")
    for division, d in results["divisions"].items():
        t = d["tally"]
        print(f"  {division:10s} " + " / ".join(str(t.get(k, 0)) for k in
                                              ("model", "fallback", "rejected", "abstained",
                                               "error")))
    if args.out:
        args.out.write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
        print(f"written: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
