"""Putting a crew together: the sim, the one brain, the hands, the talk, the agents.

``build`` wires everything and starts NOTHING - no thread runs and no model or Pionir
call is made until a caller starts the parts (``__main__`` does, in the foreground).
Every outside dependency is injectable: the model's POST, Pionir's client, the card
watch and both clocks, so a test crew never reaches a network.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from pathlib import Path

from .actions import repertoire
from .agent import Agent
from .brain import Brain, Post, http_post_json
from .cast import load_cast
from .clock import WallClock
from .conversation import Talk
from .gpu import CardWatch
from .hands import Hands, PionirClient
from .sim import Sim


def build(cfg, *, cast: list | None = None, cast_path: Path | None = None,
          kinds: Iterable = (), post: Post = http_post_json, client=None,
          card: CardWatch | None = None, clock: WallClock | None = None,
          monotonic: Callable[[], float] = time.monotonic,
          now: Callable[[], float] = time.time) -> Sim:
    """A whole, unstarted crew. ``cast`` (a list of cast.Member) wins over ``cast_path``;
    with neither, the default ``cast.json`` is loaded. ``client`` defaults to a loopback
    PionirClient at ``cfg.pionir_url`` - constructed, never called until a Job runs."""
    members = cast if cast is not None else load_cast(cast_path)
    sim = Sim(cfg, clock=clock, monotonic=monotonic, repertoire=repertoire)
    sim.kinds = list(kinds)
    sim.brain = Brain(cfg, sim, card=card, post=post, now=now)
    sim.hands = Hands(cfg, sim, client if client is not None else PionirClient(cfg.pionir_url),
                      now=monotonic)
    sim.talk = Talk(sim)
    for m in members:
        sim.add_agent(Agent(m, sim, cfg.state_dir))
    return sim


def channels(sim) -> dict:
    """Workstream -> the agents in it, from the cast."""
    out: dict = {}
    for a in sim.agents:
        for c in a.channels:
            out.setdefault(c, []).append(a.name)
    return out
