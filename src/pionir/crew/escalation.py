"""A leader's way to ask Claude a hard question - rarely, capped, through the owner's Max.

The local model distils; now and then a judgment is beyond it. A leader may then ask
Claude, through the owner's Max subscription via the CLI (``claude -p``). Never the
Anthropic API: the owner's standing rule is no paid API, and the runner strips
``ANTHROPIC_API_KEY`` (and ``ANTHROPIC_AUTH_TOKEN``, ``ANTHROPIC_BASE_URL``: ``claude_env``)
from the child's environment so the CLI cannot fall back to one.

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

**Website builds** (``Escalator.build_site``, the Fiverr desk's website worker) run through
``claude_site_runner``: ``--restricted`` (file tools confined to the working directory, the
owner's settings ignored), ONLY the ``Write`` tool (``--tools Write``), every other tool
denied by name, no MCP server, in an EMPTY temporary directory, the prompt on stdin. Claude
can write files there and do nothing else - no reading, no shell, no web. The runner returns
what was written (bounded: ``MAX_SITE_FILES``, ``MAX_SITE_FILE_BYTES``; a symlink or anything
that is not a regular file is reported, never followed) and deletes the directory; the
worker then validates every byte (crew/fiverr/site.py) - nothing Claude wrote is trusted.
**Reviews** (``Escalator.review``) run through ``claude_review_runner`` with NO tools at all.
Both spend the same cap and share as research.
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
MAX_SITE_PROMPT_CHARS = 60000
MAX_SITE_ANSWER_CHARS = 1_500_000
MAX_SITE_FILES = 20
MAX_SITE_FILE_BYTES = 200_000
MAX_SITE_BYTES = 600_000

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
    env = claude_env()
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
    env = claude_env()
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


# Never the API, only the owner's Max login through the CLI: every variable that could point
# the CLI at an API key, a bearer token or another endpoint is removed from its environment.
STRIPPED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")


def claude_env() -> dict:
    """The environment every ``claude -p`` here runs with: this process's, minus
    ``STRIPPED_ENV``."""
    return {k: v for k, v in os.environ.items() if k.upper() not in STRIPPED_ENV}


# The website build's command line: Write only, confined to the (empty) working directory.
#
# Its permission mode is acceptEdits, NOT dontAsk. Probed live on 2026-09-27 with Claude
# Code 2.1.283 (Max login, the Anthropic variables stripped): with --permission-mode
# dontAsk plus --allowedTools Write(./**) (or Write(**), or without Edit/MultiEdit in the
# deny list) Claude answered "Write call blocked: don't ask mode denies Write" and wrote
# NOTHING - every build would have failed. With acceptEdits and everything else unchanged
# (--restricted, --tools Write, the deny list, strict MCP, no session persistence), the
# write inside the working directory succeeded, and a write to an absolute path outside it
# (%TEMP%\pionir-escape-probe.txt) was refused ("outside my working directory") and not
# created. acceptEdits accepts file edits inside the working directories only; --tools
# Write means Write is the only tool that exists; the allow rule stays, naming only paths
# under the working directory, so it widens nothing. tests/test_fiverr_live_claude.py
# repeats both probes when PIONIR_LIVE_CLAUDE=1, so a CLI change that breaks either is seen.
SITE_TOOLS = ("Write",)
SITE_ALLOWED = ("Write(./**)",)
SITE_MODE = "acceptEdits"
REVIEW_MODE = "dontAsk"             # no tools at all: nothing to accept or ask about
SITE_DENIED = ("Bash", "PowerShell", "Edit", "MultiEdit", "Read", "NotebookEdit", "Glob",
               "Grep", "Task", "Agent", "Skill", "WebSearch", "WebFetch")
REVIEW_DENIED = SITE_DENIED + SITE_TOOLS


def _locked_argv(tools: tuple, denied: tuple, allowed: tuple = (), *,
                 mode: str = REVIEW_MODE) -> list:
    return ["claude", "-p",
            "--output-format", "json",
            "--restricted",
            "--tools", ",".join(tools),
            *(["--allowedTools", ",".join(allowed)] if allowed else []),
            "--disallowedTools", ",".join(denied),
            "--strict-mcp-config",
            "--permission-mode", mode,
            "--disable-slash-commands",
            "--no-session-persistence"]


def site_argv() -> list:
    """The exact ``claude`` command line for a website build. The prompt goes on stdin."""
    return _locked_argv(SITE_TOOLS, SITE_DENIED, SITE_ALLOWED, mode=SITE_MODE)


def review_argv() -> list:
    """The exact ``claude`` command line for a review: no tools at all (``--tools ""``)."""
    return _locked_argv((), REVIEW_DENIED)


def _final_text(done) -> str:
    """Claude's final answer from a ``--output-format json`` run, or RuntimeError."""
    if done.returncode != 0:
        raise RuntimeError(f"claude -p exited {done.returncode}: "
                           f"{(done.stderr or done.stdout or '')[:300]}")
    try:
        doc = json.loads(done.stdout or "")
    except ValueError as exc:
        said = (done.stdout or "")[:200]
        raise RuntimeError(f"claude -p did not answer JSON: {said}") from exc
    if not isinstance(doc, dict):
        raise RuntimeError("claude -p answered JSON that is not an object")  # noqa: TRY004
    if doc.get("is_error") or doc.get("subtype") not in (None, "success"):
        raise RuntimeError(f"claude -p reported an error ({doc.get('subtype')}): "
                           f"{str(doc.get('result') or '')[:300]}")
    text = doc.get("result")
    return text.strip() if isinstance(text, str) else ""


def collect_site(workdir: str) -> dict:
    """What a build left in its working directory: ``{"files": {relative/path: text},
    "problems": [...]}``. Only regular files are read, each at most MAX_SITE_FILE_BYTES,
    at most MAX_SITE_FILES and MAX_SITE_BYTES in all; anything else is a problem, reported
    and not read (a link is never followed)."""
    files: dict = {}
    problems: list = []
    total = 0
    for root, dirs, names in os.walk(workdir, followlinks=False):
        for d in list(dirs):
            if os.path.islink(os.path.join(root, d)):
                problems.append(f"{d!r} is a link, not a folder")
                dirs.remove(d)
        for name in sorted(names):
            full = os.path.join(root, name)
            rel = os.path.relpath(full, workdir).replace(os.sep, "/")
            if os.path.islink(full) or not os.path.isfile(full):
                problems.append(f"{rel!r} is not a regular file")
                continue
            if len(files) >= MAX_SITE_FILES:
                problems.append(f"more than {MAX_SITE_FILES} files were written")
                return {"files": files, "problems": problems}
            size = os.path.getsize(full)
            if size > MAX_SITE_FILE_BYTES or total + size > MAX_SITE_BYTES:
                problems.append(f"{rel!r} is too big ({size:,} bytes)")
                continue
            with open(full, "rb") as handle:
                raw = handle.read(MAX_SITE_FILE_BYTES + 1)
            total += len(raw)
            try:
                files[rel] = raw.decode("utf-8")
            except UnicodeDecodeError:
                problems.append(f"{rel!r} is not UTF-8 text")
    return {"files": files, "problems": problems}


def claude_site_runner(prompt: str, timeout: float, *, run=subprocess.run) -> str:
    """One website build on the owner's Max login (the API key removed): ``site_argv`` in an
    empty temporary directory, deleted afterwards. Returns JSON ``{"files", "problems",
    "said"}``; raises on any failure. ``run`` is injected by tests."""
    env = claude_env()
    workdir = tempfile.mkdtemp(prefix="pionir-site-")
    try:
        done = run(site_argv(), input=prompt, capture_output=True, text=True,
                   encoding="utf-8", errors="replace", timeout=timeout, env=env, cwd=workdir,
                   check=False)
        said = _final_text(done)
        got = collect_site(workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    got["said"] = said[:500]
    return json.dumps(got, ensure_ascii=False)


def claude_review_runner(prompt: str, timeout: float, *, run=subprocess.run) -> str:
    """One review on the owner's Max login with NO tools, in an empty temporary directory.
    Returns Claude's answer text; raises on any failure."""
    env = claude_env()
    workdir = tempfile.mkdtemp(prefix="pionir-review-")
    try:
        done = run(review_argv(), input=prompt, capture_output=True, text=True,
                   encoding="utf-8", errors="replace", timeout=timeout, env=env, cwd=workdir,
                   check=False)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    text = _final_text(done)
    if not text:
        raise RuntimeError("claude -p answered nothing")
    return text


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
                 research_runner: Runner | None = None, site_runner: Runner | None = None,
                 review_runner: Runner | None = None) -> None:
        if daily_cap < 0:
            raise ValueError("the daily Claude cap cannot be negative")
        self.store = store
        self.allocation = allocation
        self.runner = runner
        self.research_runner = research_runner
        self.site_runner = site_runner
        self.review_runner = review_runner
        self.researched = 0
        self.built = 0
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
        return self._capped(division, self.research_runner, "research", prompt, timeout,
                            MAX_RESEARCH_PROMPT_CHARS, MAX_RESEARCH_ANSWER_CHARS)

    def build_site(self, division: str, prompt: str, timeout: float | None = None) -> Result:
        """One website build (``claude_site_runner``: the Write tool only, confined to an
        empty temporary directory): ``Ok(json of the files written)`` or
        ``Err(ClaudeRefusal)``. The same cap and share as research. Never raises."""
        return self._capped(division, self.site_runner, "site build", prompt, timeout,
                            MAX_SITE_PROMPT_CHARS, MAX_SITE_ANSWER_CHARS)

    def review(self, division: str, prompt: str, timeout: float | None = None) -> Result:
        """One review call with NO tools at all (``claude_review_runner``): Claude reads what
        it is given and answers. The same cap and share. Never raises."""
        return self._capped(division, self.review_runner, "review", prompt, timeout,
                            MAX_SITE_PROMPT_CHARS, MAX_RESEARCH_ANSWER_CHARS)

    def _capped(self, division: str, runner, label: str, prompt: str, timeout,
                max_prompt: int, max_answer: int) -> Result:
        if runner is None or self.daily_cap <= 0:
            return Err(ClaudeRefusal("off", f"Claude {label} is off (no {label} runner, or "
                                            "the daily Claude cap is 0)"
                                     if label != "research" else
                                     "Claude research is off (no research runner, or "
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
            log.info("%s for %s refused: %s", label, division, reason)
            return Err(ClaudeRefusal("budget", reason))
        digest = hashlib.sha256(prompt.encode()).hexdigest()[:16]
        try:
            answer = str(runner(prompt[:max_prompt], timeout or self.timeout)).strip()
            if not answer:
                raise RuntimeError("an empty answer")
        except Exception as exc:  # noqa: BLE001 - recorded; the slot stays spent
            self.failed += 1
            why = f"{type(exc).__name__}: {exc}"
            self.store.finish_escalation(esc_id, False, f"{label}: {why}"[:500])
            log.warning("%s for %s failed: %s", label, division, why)
            if _LIMIT.search(why):
                return Err(ClaudeRefusal("budget", f"Claude's usage window is spent "
                                                   f"({why[:200]})"))
            return Err(ClaudeRefusal("failed", f"Claude did not answer: {why[:300]}"))
        if label == "research":
            self.researched += 1
        else:
            self.built += 1
        self.store.finish_escalation(esc_id, True, f"{label} prompt {digest}")
        return Ok(answer[:max_answer])

    def snapshot(self) -> dict:
        day = local_day(self._clock())
        return {"enabled": self.enabled, "daily_cap": self.daily_cap,
                "used_today": self.store.escalations_on(day), "answered": self.answered,
                "failed": self.failed, "refused": self.refused,
                "researched": self.researched, "built_or_reviewed": self.built,
                "research_enabled": self.research_runner is not None and self.daily_cap > 0}
