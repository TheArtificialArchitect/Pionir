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

**Research** (``Escalator.research``) is the one other use: a worker that must look
something up on the web (the finder, finder.py) asks Claude through a SEPARATE runner,
``claude_research_runner``, whose command line allows only the web tools
(``RESEARCH_TOOLS``), denies every file, shell and agent tool by name, loads no MCP server,
runs in an EMPTY temporary directory and takes the prompt on stdin (never on the command
line, where a client's words could reach a shell). It spends the same daily cap and the
same division share as an escalation - one slot per attempt, answered or not. Its
refusals are typed (``ClaudeRefusal``) so the worker can tell "the budget is spent, wait"
from "Claude was asked and failed".
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from .log import log
from .result import Err, Ok, Result

# (prompt, timeout_seconds) -> Claude's answer; raises on any failure
Runner = Callable[[str, float], str]

MAX_PROMPT_CHARS = 12000
MAX_ANSWER_CHARS = 4000
MAX_RESEARCH_PROMPT_CHARS = 12000
MAX_RESEARCH_ANSWER_CHARS = 30000

# The research command line. Only the web tools exist and are allowed; everything that could
# read or write a file, run a command or start an agent is denied by name as well (a deny
# rule wins over any allow rule in the owner's settings), no MCP server is loaded
# (--strict-mcp-config with no --mcp-config: no MCP tool exists to call), the owner's user
# settings, hooks, plugins and skills are not loaded, and nothing is asked: what is not
# allowed is refused.
RESEARCH_TOOLS = ("WebSearch", "WebFetch")
RESEARCH_DENIED = ("Bash", "PowerShell", "Edit", "MultiEdit", "Write", "Read", "NotebookEdit",
                   "Glob", "Grep", "Task", "Agent", "Skill")


def research_argv() -> list:
    """The exact ``claude`` command line for a research call. The prompt is NOT on it: it
    goes on stdin."""
    return ["claude", "-p",
            "--output-format", "json",
            "--tools", ",".join(RESEARCH_TOOLS),
            "--allowedTools", ",".join(RESEARCH_TOOLS),
            "--disallowedTools", ",".join(RESEARCH_DENIED),
            "--strict-mcp-config",
            "--setting-sources", "project,local",
            "--permission-mode", "dontAsk",
            "--disable-slash-commands",
            "--no-session-persistence"]


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


def claude_research_runner(prompt: str, timeout: float, *, run=subprocess.run) -> str:
    """One ``claude -p`` web-research call on the owner's Max login (the API key removed),
    with only the web tools, in an empty temporary directory that is deleted afterwards.
    Returns Claude's final answer text; raises on any failure. ``run`` is injected by tests,
    which never start a real ``claude``."""
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    workdir = tempfile.mkdtemp(prefix="pionir-finder-")
    try:
        done = run(research_argv(), input=prompt, capture_output=True, text=True,
                   encoding="utf-8", errors="replace", timeout=timeout, env=env, cwd=workdir,
                   check=False)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    if done.returncode != 0:
        raise RuntimeError(f"claude -p exited {done.returncode}: "
                           f"{(done.stderr or done.stdout or '')[:300]}")
    try:
        doc = json.loads(done.stdout or "")
    except ValueError as exc:
        raise RuntimeError(f"claude -p did not answer JSON: {(done.stdout or '')[:200]}") \
            from exc
    if not isinstance(doc, dict):
        raise RuntimeError("claude -p answered JSON that is not an object")  # noqa: TRY004
    if doc.get("is_error") or doc.get("subtype") not in (None, "success"):
        raise RuntimeError(f"claude -p reported an error ({doc.get('subtype')}): "
                           f"{str(doc.get('result') or '')[:300]}")
    text = doc.get("result")
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("claude -p answered nothing")
    return text.strip()


@dataclass(frozen=True)
class ClaudeRefusal:
    """Why a research call gave no answer. ``kind``: ``off`` (no runner, or a cap of 0),
    ``budget`` (the daily cap or the division's share is spent, or Claude's own usage
    window is - wait), ``unavailable`` (no slot could be reserved; Claude was not asked) or
    ``failed`` (Claude was asked and did not answer; the slot is spent)."""

    kind: str
    message: str

    @property
    def waits(self) -> bool:
        """True when the work should wait rather than count a failed attempt."""
        return self.kind in ("off", "budget", "unavailable")

    def __str__(self) -> str:
        return f"{self.kind}: {self.message}"


# A failed call that is Claude's own usage window or load, not a verdict on the question.
_LIMIT = re.compile(r"(?i)usage limit|rate[ _-]?limit|limit reached|limit will reset|"
                    r"overloaded")


def local_day(t: float) -> str:
    return datetime.fromtimestamp(t).astimezone().strftime("%Y-%m-%d")


class Escalator:
    def __init__(self, store, allocation, *, runner: Runner | None, daily_cap: int,
                 timeout: float = 300.0, clock: Callable[[], float] = time.time,
                 research_runner: Runner | None = None) -> None:
        if daily_cap < 0:
            raise ValueError("the daily Claude cap cannot be negative")
        self.store = store
        self.allocation = allocation
        self.runner = runner
        self.research_runner = research_runner
        self.researched = 0
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

    def research(self, division: str, prompt: str, timeout: float | None = None) -> Result:
        """One restricted web-research call for a worker in ``division``: ``Ok(answer)`` or
        ``Err(ClaudeRefusal)``. Counted against the daily cap and the division's share
        exactly like an escalation (one slot per attempt). Never raises."""
        if self.research_runner is None or self.daily_cap <= 0:
            return Err(ClaudeRefusal("off", "Claude research is off (no research runner, or "
                                            "the daily Claude cap is 0)"))
        now = self._clock()
        try:
            share = self.allocation.cap("claude_escalations", division)
            ok, reason, esc_id = self.store.reserve_escalation(
                day=local_day(now), division=division, t=now, global_cap=self.daily_cap,
                division_cap=min(share, self.daily_cap))
        except Exception as exc:  # noqa: BLE001 - a refusal, in words
            return Err(ClaudeRefusal("unavailable", f"could not reserve a Claude slot: "
                                                    f"{type(exc).__name__}: {exc}"))
        if not ok:
            self.refused += 1
            log.info("research for %s refused: %s", division, reason)
            return Err(ClaudeRefusal("budget", reason))
        digest = hashlib.sha256(prompt.encode()).hexdigest()[:16]
        try:
            answer = str(self.research_runner(prompt[:MAX_RESEARCH_PROMPT_CHARS],
                                              timeout or self.timeout)).strip()
            if not answer:
                raise RuntimeError("an empty answer")
        except Exception as exc:  # noqa: BLE001 - recorded; the slot stays spent
            self.failed += 1
            why = f"{type(exc).__name__}: {exc}"
            self.store.finish_escalation(esc_id, False, f"research: {why}"[:500])
            log.warning("research for %s failed: %s", division, why)
            if _LIMIT.search(why):
                return Err(ClaudeRefusal("budget", f"Claude's usage window is spent "
                                                   f"({why[:200]})"))
            return Err(ClaudeRefusal("failed", f"Claude did not answer: {why[:300]}"))
        self.researched += 1
        self.store.finish_escalation(esc_id, True, f"research prompt {digest}")
        return Ok(answer[:MAX_RESEARCH_ANSWER_CHARS])

    def snapshot(self) -> dict:
        day = local_day(self._clock())
        return {"enabled": self.enabled, "daily_cap": self.daily_cap,
                "used_today": self.store.escalations_on(day), "answered": self.answered,
                "failed": self.failed, "refused": self.refused,
                "researched": self.researched,
                "research_enabled": self.research_runner is not None and self.daily_cap > 0}
