"""A shelved build's one second life: why it was shelved, and whether that is worth one retry.

Ten nights of builds (2026-10-02..07) staged nothing: every product was shelved after its
build and its one repair, for one of three reasons - it ran out of its budget (twice, the
same way), Daedalus said its gate passed but committed nothing, or the tests failed. Each of
those has a concrete next move the first two attempts did not try, so a product shelved for
one of them gets exactly ONE more attempt, shaped by the reason:

- ``timeout`` - a bigger budget (``second_life_budget_minutes``, default 150; never past the
  window or the adapter's 3-hour cap) and a tighter scope in the prompt.
- ``nothing_committed`` - a sharper prompt that names the failure: no new commit, no build.
- ``tests_failed`` - one fix attempt with the failing test output fed back.

Anything else (a review that rejected it twice, a review that could not be completed, a
build that could not be staged, Pionir refusing it, a lost job) is terminal at once, and so
is a product whose second life is spent. Either way the record says why (``terminal_why``).

Nothing here weakens a gate: a second life is one more Daedalus attempt, and what it builds
goes through exactly the same checks, Claude's review and the owner's approval on the shelf.
The way back is the record itself: a product's ``second_life`` key is what marks it used;
removing the product from the record (or ``remove``/``add`` on the backlog card) is the reset.
"""
from __future__ import annotations

import re

TIMEOUT = "timeout"
NOTHING_COMMITTED = "nothing_committed"
TESTS_FAILED = "tests_failed"
KINDS = (TIMEOUT, NOTHING_COMMITTED, TESTS_FAILED)

# the shelf reasons worker.py writes, newest wording first
_TIMEOUT = re.compile(r"(?i)did not finish in its \d+-minute budget|did not finish within \d+ "
                      r"seconds")
_NOTHING = re.compile(r"(?i)committed nothing")
_TESTS = re.compile(r"(?i)tests? (?:FAILED|failing|failed)|the tests did not finish|"
                    r"G2-tests|No module named pytest")
_CLAUDE = re.compile(r"(?i)^rejected by Claude\b")

MAX_TEST_OUTPUT = 2500


def classify(p: dict) -> tuple[str | None, str]:
    """``(kind, label)``: the second-life kind for a shelved product, or None and why not."""
    why = str(p.get("shelved_why") or "")
    here = int(p.get("slice") or 1)
    last = next((a for a in reversed(p.get("attempts") or [])
                 if int(a.get("slice") or 1) == here and a.get("outcome")
                 not in (None, "not_started", "parked")), {})
    if _CLAUDE.search(why):
        return None, "Claude's review rejected it after its repair"
    if last.get("outcome") == "timed_out" or _TIMEOUT.search(why):
        return TIMEOUT, "it ran out of time"
    if _NOTHING.search(why):
        return NOTHING_COMMITTED, "Daedalus reported a pass but committed nothing"
    stage = str(last.get("stage_failed") or "")
    if _TESTS.search(why) or "test" in stage.lower():
        return TESTS_FAILED, "its tests failed"
    if "review could not be completed" in why:
        return None, "Claude's review could not be completed"
    if "could not be staged" in why:
        return None, "it was approved but could not be staged"
    if "refused" in why:
        return None, "Pionir refused the build"
    if "was lost" in why:
        return None, "the job was lost"
    return None, "its shelf reason is not one a retry can fix"


def plan(kind: str, *, budget_minutes: int) -> str:
    """One line for the owner: what the second life will do."""
    if kind == TIMEOUT:
        return f"one more attempt with {budget_minutes} minutes and a tighter scope"
    if kind == NOTHING_COMMITTED:
        return "one more attempt with a prompt that names the empty commit"
    return "one fix attempt with the failing test output fed back"


def preface(kind: str, p: dict, *, minutes: int, verify: str) -> str:
    """The paragraph a second-life job's intent starts with."""
    why = str((p.get("second_life") or {}).get("why") or p.get("shelved_why") or "")[:600]
    if kind == TIMEOUT:
        return (f"SECOND (AND LAST) CHANCE for this product. Earlier attempts ran out of time "
                f"and nothing from them was kept ({why}). This time you have about {minutes} "
                "minutes. Keep the scope tight: the smallest working version of each feature, "
                "in the order listed, each with one real test; no extras, no refactoring, no "
                "re-reading files you already read. Get the tests passing early and commit.")
    if kind == NOTHING_COMMITTED:
        return ("SECOND (AND LAST) CHANCE for this product. The last attempt reported that its "
                f"gate passed but it COMMITTED NOTHING NEW ({why}). A job that leaves no new "
                "commit is a failure however good its work was. Make every change below as "
                "real edits to the files, run the tests with the command given, and make sure "
                "the changes land as a NEW commit on the current branch before you stop.")
    output = str(p.get("last_test_output") or "").strip()[-MAX_TEST_OUTPUT:]
    if not output:
        output = why
    return ("SECOND (AND LAST) CHANCE for this product: one fix attempt. Its tests FAILED. "
            f"This is the test output:\n```\n{output}\n```\nFind the cause (the code, an import "
            "path, or a test that is wrong about the brief), fix it, and keep everything that "
            f"already works. Every test must pass with: {verify}")
