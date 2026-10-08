"""The posting workers' one way to Claude: a single draft when the local model keeps failing.

The owner's rules hold whole: ``claude -p`` on his Max login with the Anthropic API key removed
(escalation.claude_env), never the API; no tools at all (the review runner: Claude reads the
prompt and answers); capped. Three caps, all of which must allow it:

- the worker's own: ``claude_drafts_per_day`` (blog.DailyPoster; 0 turns it off), counted in
  its record whether or not the call worked;
- the crew's daily Claude cap and the posting division's share of it (``Escalator._capped``,
  the same slot every escalation, research call and review takes - so the owner's crew-wide
  off switch, ``claude_daily_cap = 0``, stops this too);
- Claude's own usage window (a refusal for that is reported as such).

It goes through the escalator the crew wired into the worker's context (``ctx.review``, a
``partial`` of ``Escalator.review``), with the worker's model (``DEFAULT_CLAUDE_MODEL``, a
pinned Sonnet-class id) bound to the escalator's own review runner - so a test crew's fake
runner is what a test reaches, and a context with no Claude wiring reaches nothing. When the
context's ``review`` is not the escalator's (an unknown shape), it is called as it is, without
a model: still capped by whoever provided it.

What comes back is only words. The worker assembles, mends by rule and checks them exactly as
it does the local model's, and the owner approves the post like any other.
"""
from __future__ import annotations

from functools import partial

from .escalation import (
    DEFAULT_CLAUDE_MODEL,
    MAX_RESEARCH_ANSWER_CHARS,
    MAX_SITE_PROMPT_CHARS,
    _takes_model,
    clean_model,
)
from .leader import parse_json_object
from .log import log
from .result import Err, Ok, Result

__all__ = ["DEFAULT_CLAUDE_MODEL", "ask", "clean_model", "parse_json_object"]
LABEL = "post draft"


def ask(review, division: str, prompt: str, timeout: float, model: str | None) -> Result:
    """One capped Claude call: ``Ok(answer text)`` or ``Err(why, in words)``. Never raises."""
    if review is None:
        return Err("no Claude in this context")
    esc = getattr(getattr(review, "func", None), "__self__", None)
    capped = getattr(esc, "_capped", None)
    runner = getattr(esc, "review_runner", None)
    try:
        if callable(capped):
            if runner is None:
                return Err("Claude reviews are off in this crew (no review runner)")
            if model and _takes_model(runner):
                runner = partial(runner, model=model)
            got = capped(division, runner, LABEL, prompt, timeout, MAX_SITE_PROMPT_CHARS,
                         MAX_RESEARCH_ANSWER_CHARS)
        else:
            got = review(prompt, timeout)
    except Exception as exc:  # noqa: BLE001 - the caller records it; nothing is raised
        log.warning("claudedraft: the call failed: %s: %s", type(exc).__name__, exc)
        return Err(f"{type(exc).__name__}: {exc}")
    if isinstance(got, Err):
        refusal = got.error
        return Err(f"{getattr(refusal, 'kind', 'refused')}: "
                   f"{getattr(refusal, 'message', None) or refusal}")
    return Ok(str(got.value))
