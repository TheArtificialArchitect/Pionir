"""``python -m pionir.crew``: run the crew in the foreground until Ctrl+C.

Its own process - not a service, not a scheduled task, nothing that starts itself. It
prints every division and which of its workers are LIVE and which are placeholders (and
any live one that is not configured), runs until interrupted, and on Ctrl+C stops every
part, takes a final checkpoint and says so.

``--once`` runs every worker once and every leader once, prints what happened and exits.
``--no-claude`` turns Claude escalation off for this run.
"""
from __future__ import annotations

import argparse
import sys
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from . import log as crewlog
from .budget import Monitor, boot_check
from .config import CrewSettings
from .result import Err
from .runtime import build


def describe(crew, out: Callable[[str], None] = print) -> None:
    """Which workers are live and which are placeholders - said up front, every start."""
    cfg = crew.cfg
    live = [w for w in crew.registry.all() if w.live]
    out(f"crew: {len(crew.registry)} worker(s) in {len(crew.leaders)} division(s): "
        f"{len(live)} live, {len(crew.registry) - len(live)} placeholder(s)")
    claude = (f"up to {cfg.claude_daily_cap}/day" if crew.escalator.enabled else "OFF")
    out(f"crew: brain {cfg.model} at {cfg.ollama_url} ({cfg.budget.calls_per_hour} calls/hour); "
        f"Claude escalations {claude}; Pionir at {cfg.pionir_url}")
    for d in crew.registry.divisions():
        out(f"  {d.title} ({d.division_id}), leader every {d.leader_cadence_seconds} s:")
        for w in crew.registry.workers_in(d.division_id):
            if not w.live:
                out(f"    {w.worker_id:24} PLACEHOLDER - {w.readiness(cfg.secrets_dir)}")
                continue
            ready = w.readiness(cfg.secrets_dir)
            state = f"LIVE but {ready}" if ready else "LIVE"
            out(f"    {w.worker_id:24} {state} (every {w.cadence_seconds} s via {w.provider})")
    out(f"crew: state in {cfg.state_dir}; Ctrl+C stops it")


def run(crew, *, stop: threading.Event | None = None, out: Callable[[str], None] = print,
        monitor: bool = True) -> int:
    """Start every part of a built crew, wait for ``stop`` (or Ctrl+C), stop cleanly."""
    stop = stop or threading.Event()
    describe(crew, out)
    if monitor:
        crew.monitor = Monitor(crew.cfg, crew.store)
        crew.monitor.start()
    crew.start()
    out("crew: started")
    try:
        while not stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        out("crew: Ctrl+C - stopping")
    finally:
        crew.stop()
        out("crew: stopped cleanly (final checkpoint taken)")
    return 0


def once(crew, out: Callable[[str], None] = print) -> int:
    describe(crew, out)
    try:
        got = crew.run_once()
        w = got["workers"]
        out(f"workers: {w.attempted} run, {w.succeeded} ok, {w.failed} failed, "
            f"{w.written} new row(s){' - SILENT SUCCESS' if w.silent_success else ''}")
        for e in w.errors:
            out(f"  {e}")
        for division, result in got["leaders"].items():
            if isinstance(result, Err):
                out(f"  leader.{division}: {result.error}")
            else:
                v = result.value
                text = getattr(v, "headline", None) or getattr(v, "reason", "")
                out(f"  leader.{division}: {type(v).__name__.lower()}: {text}")
    finally:
        crew.stop()
    return 0


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pionir.crew",
                                 description="Run Pionir's crew in the foreground until Ctrl+C.")
    ap.add_argument("--catalogue", type=Path, default=None,
                    help="a catalogue file to use instead of the packaged catalogue.json")
    ap.add_argument("--no-boot-check", action="store_true",
                    help="skip warming and measuring the model before starting")
    ap.add_argument("--no-claude", action="store_true",
                    help="turn Claude escalation off for this run")
    ap.add_argument("--once", action="store_true",
                    help="run every worker and every leader once, then exit")
    args = ap.parse_args(argv)
    cfg = CrewSettings.from_environment()
    if args.catalogue is not None:
        cfg = replace(cfg, catalogue_path=args.catalogue)
    if args.no_claude:
        cfg = replace(cfg, claude_daily_cap=0)
    crewlog.setup(cfg.state_dir / "crew.log", console=True)
    bad = crewlog.check_source()
    if bad:
        print("crew: refusing to start, mangled source: " + "; ".join(bad), file=sys.stderr)
        return 2
    if not args.no_boot_check:
        verdict = boot_check(cfg)
        for w in verdict["warnings"]:
            print(f"crew: {w}")
        if not verdict["ok"]:
            for r in verdict["reasons"]:
                print(f"crew: cannot start: {r}", file=sys.stderr)
            return 1
    crew = build(cfg)
    if args.once:
        crew.brain.start()
        crew.hands.start()
        return once(crew)
    return run(crew)


if __name__ == "__main__":
    raise SystemExit(main())
