"""``python -m pionir.crew``: run the crew in the foreground until Ctrl+C.

Its own process - not a service, not a scheduled task, nothing that starts itself. It
prints what it started, runs until interrupted, and on Ctrl+C stops every part, takes a
final checkpoint and says so. The crew reaches Pionir over loopback HTTP (as Moss does),
so Pionir must be up for any Job to run; if it is not, every job is recorded as an
attempt that got no answer, never as work done.
"""
from __future__ import annotations

import argparse
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from . import log as crewlog
from .budget import Monitor, boot_check
from .config import CrewSettings
from .crew import build, channels


def run(sim, *, stop: threading.Event | None = None, out: Callable[[str], None] = print,
        monitor: bool = True) -> int:
    """Start every part of a built crew, wait for ``stop`` (or Ctrl+C), stop cleanly."""
    stop = stop or threading.Event()
    cfg = sim.cfg
    out(f"crew: {len(sim.agents)} agent(s), model {cfg.model} at {cfg.ollama_url}, "
        f"Pionir at {cfg.pionir_url}")
    for a in sim.agents:
        hours = "always" if a.t.always_on else f"{a.t.work_start:05.2f}-{a.t.work_end:05.2f}"
        out(f"  {a.name}: {a.role} [{', '.join('#' + c for c in a.channels)}; hours {hours}]")
    for ch, names in sorted(channels(sim).items()):
        out(f"  #{ch}: {', '.join(names)}")
    if not sim.kinds:
        out("crew: NO project kinds are on this crew yet (real ones arrive with the organ "
            "adapters). The agents will find nothing achievable and the vitals will say so, "
            "loudly - that is the truth, not a fault.")
    out(f"crew: state in {cfg.state_dir}; Ctrl+C stops it")
    if monitor:
        sim.monitor = Monitor(cfg, sim.store)
        sim.monitor.start()
    sim.brain.start()
    sim.hands.start()
    sim.start()
    out("crew: started")
    try:
        while not stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        out("crew: Ctrl+C - stopping")
    finally:
        sim.stop()
        out("crew: stopped cleanly (final checkpoint taken)")
    return 0


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pionir.crew",
                                 description="Run Pionir's crew in the foreground until Ctrl+C.")
    ap.add_argument("--cast", type=Path, default=None,
                    help="a cast file to use instead of the packaged cast.json")
    ap.add_argument("--no-boot-check", action="store_true",
                    help="skip warming and measuring the model before starting")
    args = ap.parse_args(argv)
    cfg = CrewSettings.from_environment()
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
    sim = build(cfg, cast_path=args.cast)
    return run(sim)


if __name__ == "__main__":
    raise SystemExit(main())
