"""Shared fakes for the crew tests.

Not a test module (unittest discovery looks for ``test*.py``). Nothing here
reaches a real model, a real card or the real Pionir state root: every crew
built by these helpers lives in a temporary directory with its own lock path.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from pionir.crew.config import CrewBudget, CrewSettings


def temp_dir() -> tempfile.TemporaryDirectory:
    # SQLite in WAL mode can hold a file handle a moment past close() on Windows.
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


def settings(root: str | Path, **overrides) -> CrewSettings:
    root = Path(root)
    overrides.setdefault("state_dir", root / "crew")
    overrides.setdefault("gpu_lock_path", root / "resource" / "gpu.lock")
    overrides.setdefault("gpu_poll_seconds", 0.01)
    calls = overrides.pop("calls_per_hour", None)
    if calls is not None:
        overrides["budget"] = CrewBudget(calls_per_hour=calls)
    return CrewSettings(**overrides)


class FakeOllama:
    """Records every POST and answers like /api/chat. ``on_call`` runs inside
    the call, so a test can look at the world while the model is 'thinking'."""

    def __init__(self, reply: str = "hello", on_call=None, error: Exception | None = None) -> None:
        self.reply = reply
        self.on_call = on_call
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url: str, body: dict, timeout: float) -> dict:
        self.calls.append((url, body))
        if self.on_call is not None:
            self.on_call(url, body)
        if self.error is not None:
            raise self.error
        return {"message": {"role": "assistant", "content": self.reply},
                "prompt_eval_count": 11, "eval_count": 3, "total_duration": 1,
                "load_duration": 0}


class ScriptedProbe:
    """A fake lock reader: returns each scripted holder in turn, then None
    (free) for ever after. ``on_probe`` sees how many probes have happened."""

    def __init__(self, held_for: int, holder: dict | None = None, on_probe=None) -> None:
        self.held_for = held_for
        self.holder = holder or {"owner": "pionir", "pid": 1,
                                 "purpose": "daedalus: qwen3-coder:30b"}
        self.on_probe = on_probe
        self.probes = 0

    def __call__(self) -> dict | None:
        self.probes += 1
        if self.on_probe is not None:
            self.on_probe(self.probes)
        return dict(self.holder) if self.probes <= self.held_for else None


class FakeTime:
    """One hand on both the wall clock and the monotonic clock."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.wall = start
        self.mono = 1000.0

    def now(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.wall += seconds
        self.mono += seconds
