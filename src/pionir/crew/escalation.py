"""A leader's way to ask Claude a hard question - rarely, capped, through the owner's Max.

The local model distils; now and then a judgment is beyond it. A leader may then ask
Claude, through the owner's Max subscription via the CLI (``claude -p``). Never the
Anthropic API: the owner's standing rule is no paid API, and the runner strips
``ANTHROPIC_API_KEY`` from the child's environment so the CLI cannot fall back to one.

The cap is hard and small (``cfg.claude_daily_cap``, default 10 a day across ALL
leaders), because each ``claude -p`` loads the full Claude Code system prompt, and about
505 such calls once exhausted a whole 5-hour usage window. Moss divides the cap between
divisions (direction.Allocation, ``claude_escalations``); a division may not exceed its
share. Both caps are checked and the slot taken in ONE store transaction
(``reserve_escalation``), and every ATTEMPT counts - a call that failed still cost usage.

The runner is injected: tests never reach Claude.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import time
from collections.abc import Callable
from datetime import datetime

from .log import log
from .result import Err, Ok, Result

# (prompt, timeout_seconds) -> Claude's answer; raises on any failure
Runner = Callable[[str, float], str]

MAX_PROMPT_CHARS = 12000
MAX_ANSWER_CHARS = 4000


def claude_cli_runner(prompt: str, timeout: float) -> str:
    """``claude -p`` on the owner's Max login. Never the API: the key is removed."""
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    done = subprocess.run(["claude", "-p", prompt], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, env=env,
                          check=False)
    if done.returncode != 0:
        raise RuntimeError(f"claude -p exited {done.returncode}: {(done.stderr or '')[:300]}")
    text = (done.stdout or "").strip()
    if not text:
        raise RuntimeError("claude -p answered nothing")
    return text


def local_day(t: float) -> str:
    return datetime.fromtimestamp(t).astimezone().strftime("%Y-%m-%d")


class Escalator:
    def __init__(self, store, allocation, *, runner: Runner | None, daily_cap: int,
                 timeout: float = 300.0, clock: Callable[[], float] = time.time) -> None:
        if daily_cap < 0:
            raise ValueError("the daily Claude cap cannot be negative")
        self.store = store
        self.allocation = allocation
        self.runner = runner
        self.daily_cap = int(daily_cap)
        self.timeout = timeout
        self._clock = clock
        self.refused = 0
        self.failed = 0
        self.answered = 0

    @property
    def enabled(self) -> bool:
        return self.runner is not None and self.daily_cap > 0

    def escalate(self, division: str, question: str, context: str) -> Result:
        """-> Ok(answer) or Err(reason in words). Never raises."""
        if self.runner is None:
            return Err("escalation is off (no Claude runner configured)")
        now = self._clock()
        day = local_day(now)
        try:
            share = self.allocation.cap("claude_escalations", division)
            ok, reason, esc_id = self.store.reserve_escalation(
                day=day, division=division, t=now, global_cap=self.daily_cap,
                division_cap=min(share, self.daily_cap))
        except Exception as exc:  # noqa: BLE001 - a refusal, in words
            return Err(f"could not reserve an escalation: {type(exc).__name__}: {exc}")
        if not ok:
            self.refused += 1
            log.info("escalation for %s refused: %s", division, reason)
            return Err(reason)
        prompt = (
            f"You are advising the {division} division of a small autonomous business crew. "
            "Answer the question below in at most 150 words, plainly. Use ONLY the facts "
            "given; do not state any figure or name that is not in them.\n\n"
            f"QUESTION: {question.strip()[:1000]}\n\nFACTS:\n{context}"
        )[:MAX_PROMPT_CHARS]
        digest = hashlib.sha256(prompt.encode()).hexdigest()[:16]
        try:
            answer = str(self.runner(prompt, self.timeout)).strip()
            if not answer:
                raise RuntimeError("an empty answer")
        except Exception as exc:  # noqa: BLE001 - recorded; the slot stays spent
            self.failed += 1
            self.store.finish_escalation(esc_id, False, f"{type(exc).__name__}: {exc}")
            log.warning("escalation for %s failed: %s", division, exc)
            return Err(f"Claude did not answer: {type(exc).__name__}: {exc}")
        self.answered += 1
        self.store.finish_escalation(esc_id, True, f"prompt {digest}")
        return Ok(answer[:MAX_ANSWER_CHARS])

    def snapshot(self) -> dict:
        day = local_day(self._clock())
        return {"enabled": self.enabled, "daily_cap": self.daily_cap,
                "used_today": self.store.escalations_on(day), "answered": self.answered,
                "failed": self.failed, "refused": self.refused}
