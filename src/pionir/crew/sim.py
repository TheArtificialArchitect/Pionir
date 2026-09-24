"""The tick loop, the checkpoint, and the honest pause.

One thread runs one tick per ``tick_seconds`` of real time. There is no game
clock and no time compression: the crew does real work, so a tick is a real
step and ``clock.t`` is the time on the wall. There is no world to tick either
- each tick just lets every agent act, then perceive, in a rotating order.

Honesty about time the crew was not working, kept from Hearth:

- If the loop finds it has fallen far behind (the machine slept, the process
  was suspended), it does NOT catch up. It records a pause and carries on.
  Work that was not done did not happen.
- Ordinary lag is not replayed either. Hearth made up a few missed ticks,
  because each was a game second that would otherwise be lost; on the wall
  clock a missed tick loses nothing, and replaying it would only make every
  agent act twice in the same instant. Skipped steps are counted, not run.
- A restart records "process was not running" for the gap since the last
  checkpoint, and an operator ``pause()`` is recorded when it ends. Nothing
  ticks while paused, and nothing is made up afterwards.

Nothing starts itself: the thread runs only after ``start()``.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable

from .clock import WallClock
from .log import lesion_snapshot, log, safe
from .store import CrewStore
from .vitals import Vitals

STALL_SECONDS = 10.0        # behind by more than this: it was a pause, not lag
FIRST_CHECKPOINT_SECONDS = 60


class Sim:
    def __init__(self, cfg, *, clock: WallClock | None = None,
                 monotonic: Callable[[], float] = time.monotonic,
                 repertoire: Callable[[], Iterable[str]] | None = None) -> None:
        self.cfg = cfg
        self.state_dir = cfg.state_dir
        self.tick_seconds = cfg.tick_seconds
        # A tick longer than the stall line would read every ordinary tick as a pause.
        self.stall_seconds = max(STALL_SECONDS, 3 * cfg.tick_seconds)
        self.clock = clock or WallClock()
        self._monotonic = monotonic
        self.store = CrewStore(cfg.state_dir / "crew.db")
        self.agents: list = []
        self._by_id: dict = {}
        self.brain = None
        self.monitor = None
        self.vitals = Vitals(self, repertoire=repertoire)   # is anybody producing anything?
        self.ticks = 0                        # this process
        self.skipped_ticks = 0                # steps fallen behind by and deliberately not replayed
        self.dt = 0.0                         # real seconds since the previous tick
        self._last_tick_real: float | None = None
        self.started_real = time.time()
        self.born_real = 0.0
        self.paused_seconds_total = 0.0
        self.last_pause: dict | None = None
        self.paused_reason: str | None = None
        self._paused_since = 0.0
        self.checkpoints = 0
        self.stopping = False
        self._next_checkpoint_t = 0
        self._last: float | None = None       # monotonic time the tick cadence is measured from
        self._last_wall = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="pionir-crew-sim", daemon=True)
        self.lock = threading.RLock()         # held during a tick and during snapshot()
        self._restore()

    # ---- lifecycle ------------------------------------------------------
    def _restore(self) -> None:
        saved_t = self.store.get("t")
        now = self.clock.now
        if saved_t is None:
            self.born_real = now
            self.store.set("born_real", now)
            log.info("new crew at %s", self.clock)
        else:
            self.born_real = float(self.store.get("born_real", now))
            saved_real = float(self.store.get("saved_real", now))
            gap = max(0.0, now - saved_real)
            self.store.record_pause(saved_real, now, self.clock.t, "process was not running")
            self.paused_seconds_total = float(self.store.get("paused_total", 0.0)) + gap
            self.store.set("paused_total", self.paused_seconds_total)
            log.info("crew resumed at %s after %.0f s not running", self.clock, gap)
        self.last_pause = self.store.last_pause()
        # first checkpoint soon after a new start, so a crash in the opening minutes loses little
        self._next_checkpoint_t = self.clock.t + (
            FIRST_CHECKPOINT_SECONDS if saved_t is None else int(self.cfg.checkpoint_seconds))

    def age_seconds(self) -> float:
        return max(0.0, self.clock.now - self.born_real)

    def add_agent(self, agent) -> None:
        self.agents.append(agent)
        self._by_id[agent.id] = agent

    def agent(self, agent_id: str):
        return self._by_id.get(agent_id)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        if self.stopping:
            return                  # once: a second stop would checkpoint into a closed store
        self.stopping = True        # viewers stop asking before the stores close
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=15)
        # the brain's callbacks take our lock and write our store: stop it before closing either
        for part, name in ((self.brain, "brain"), (self.monitor, "monitor")):
            if part is not None:
                safe(f"sim.stop.{name}", part.stop)
        with self.lock:
            if self.paused_reason is not None:
                self._close_pause()
            self.checkpoint("shutdown")
            for a in self.agents:
                if hasattr(a, "close"):
                    safe(f"agent.{a.id}.close", a.close)
        self.store.close()

    # ---- pausing --------------------------------------------------------
    def pause(self, reason: str = "paused by operator") -> None:
        """Stop ticking until ``resume``. The pause is recorded when it ends, with its
        real length; nothing is made up afterwards."""
        with self.lock:
            if self.paused_reason is not None:
                return
            self.paused_reason = reason
            self._paused_since = self.clock.now
            log.info("crew paused: %s", reason)

    def resume(self) -> None:
        with self.lock:
            if self.paused_reason is None:
                return
            seconds = self._close_pause()
            # measure the cadence from now: no catch-up, and no false "suspended" stall
            self._last = self._monotonic()
            self._last_wall = self.clock.now
            log.info("crew resumed after %.0f s paused", seconds)

    def _close_pause(self) -> float:
        stopped, resumed = self._paused_since, self.clock.now
        self.store.record_pause(stopped, resumed, self.clock.t, self.paused_reason or "paused")
        self.paused_seconds_total += max(0.0, resumed - stopped)
        self.store.set("paused_total", self.paused_seconds_total)
        self.last_pause = self.store.last_pause()
        self.paused_reason = None
        return resumed - stopped

    # ---- the loop -------------------------------------------------------
    def step(self) -> int:
        """One look at the time: run a tick if one is due. Returns ticks run (0 or 1).
        The thread is only this in a loop, so tests drive it with a fake clock."""
        now = self._monotonic()
        if self._last is None:
            self._last, self._last_wall = now, self.clock.now
            return 0
        if self.paused_reason is not None:
            return 0
        behind = now - self._last
        if behind >= self.stall_seconds:
            # a pause, not lag: record it, do not work it
            self.store.record_pause(self._last_wall, self.clock.now, self.clock.t,
                                    "process was suspended")
            self.paused_seconds_total += behind
            self.store.set("paused_total", self.paused_seconds_total)
            self.last_pause = self.store.last_pause()
            log.warning("stall of %.1f s treated as a pause at %s", behind, self.clock)
            self._last, self._last_wall = now, self.clock.now
            return 0
        due = int(behind / self.tick_seconds)
        if due <= 0:
            return 0
        self.skipped_ticks += due - 1
        with self.lock:
            safe("sim.tick", self.tick)
        self._last += due * self.tick_seconds      # keep the cadence; the remainder carries
        self._last_wall = self.clock.now
        return 1

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self.step():
                self._stop.wait(min(self.tick_seconds / 4, 0.1))

    def tick(self) -> None:
        now = self.clock.now
        self.dt = (now - self._last_tick_real) if self._last_tick_real is not None \
            else self.tick_seconds
        self._last_tick_real = now
        self.ticks += 1
        # agents act in a rotating order so nobody is always first
        n = len(self.agents)
        if n:
            start = self.ticks % n
            order = self.agents[start:] + self.agents[:start]
            for a in order:
                safe(f"agent.{a.id}.act", lambda a=a: a.act(self))
            # perception happens after everyone has acted, over this tick's events
            for a in order:
                if hasattr(a, "perceive"):
                    safe(f"agent.{a.id}.perceive", lambda a=a: a.perceive(self))
            safe("vitals", self.vitals.check)
        if self.clock.t >= self._next_checkpoint_t:
            self.checkpoint("periodic")

    def checkpoint(self, reason: str) -> None:
        extra = {"paused_total": self.paused_seconds_total}
        for a in self.agents:
            if hasattr(a, "checkpoint"):
                safe(f"agent.{a.id}.checkpoint", a.checkpoint)
        self.store.checkpoint(self.clock.t, extra, saved_real=self.clock.now)
        self.checkpoints += 1
        self._next_checkpoint_t = self.clock.t + int(self.cfg.checkpoint_seconds)
        if reason != "periodic":
            log.info("checkpoint (%s) at %s", reason, self.clock)

    # ---- for the viewer -------------------------------------------------
    def snapshot(self) -> dict:
        with self.lock:
            return {
                "t": self.clock.t,
                "hhmm": self.clock.hhmm(),
                "tick_seconds": self.tick_seconds,
                "ticks": self.ticks,
                "skipped_ticks": self.skipped_ticks,
                "checkpoints": self.checkpoints,
                "uptime_real": round(time.time() - self.started_real),
                "age_seconds": round(self.age_seconds()),
                "paused": self.paused_reason,
                "paused_total": round(self.paused_seconds_total),
                "last_pause": self.last_pause,
                "agents": {a.id: (a.snapshot(self) if hasattr(a, "snapshot") else {})
                           for a in self.agents},
                "lesions": lesion_snapshot(),
                "vitals": self.vitals.to_dict(),
                "brain": self.brain.snapshot() if self.brain else None,
                "budget": self.monitor.latest if self.monitor else None,
            }
