"""The one unittest runner a self-improvement patch is judged with.

The same code runs the targeted tests in the build sandbox (as ``pionir-builds``, contained),
in Daedalus's own gate, and in the owner's repo after Ian's yes - so "passes" means the same
thing everywhere. It is run as ``python -I -X utf8 -c RUNNER <result.json> [module ...]`` from
the repo root: ``src`` and ``tests`` on the path (the way CI runs ``unittest discover -s
tests``), the named test modules - or, with none, the whole suite.

It records each failing test's OUTCOME, not just its id: ``failure:<ExceptionType>`` for a test
whose assertion failed and ``error:<ExceptionType>`` for one that crashed or could not even be
loaded (an ImportError is ``error:ImportError``). So a regression test must fail on the old code
by an ASSERTION (an import error reproduces nothing), and the full suite with a change may have
no test failing that main does not fail in exactly the same way.

What the file says comes from a run of model-written code, so it is read with limits and is
evidence for Claude and Ian, never a proof by itself.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

RUNNER = (
    "import json, sys, unittest\n"
    "out, names = sys.argv[1], sys.argv[2:]\n"
    "sys.path[0:0] = ['src', 'tests']\n"
    "loader = unittest.TestLoader()\n"
    "suite = loader.loadTestsFromNames(names) if names else "
    "loader.discover('tests', top_level_dir='tests')\n"
    "res = unittest.TextTestRunner(stream=sys.stderr, verbosity=0).run(suite)\n"
    "def kind(text):\n"
    "    last = [l for l in str(text).strip().splitlines() if l.strip()][-1:] or ['?']\n"
    "    return last[0].split(':', 1)[0].strip()[:80]\n"
    "outcomes = {}\n"
    "for t, text in res.failures: outcomes[t.id()] = 'failure:' + kind(text)\n"
    "for t, text in res.errors: outcomes[t.id()] = 'error:' + kind(text)\n"
    "for t in getattr(res, 'unexpectedSuccesses', []): outcomes[t.id()] = 'error:UnexpectedSuccess'\n"
    "json.dump({'ran': res.testsRun, 'outcomes': outcomes, "
    "'skipped': len(res.skipped), 'ok': res.wasSuccessful()}, open(out, 'w'))\n"
    "sys.exit(0 if res.wasSuccessful() else 1)\n"
)
MAX_RESULT_BYTES = 2_000_000
_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass
class SuiteResult:
    """One run: did it finish, how many ran, and how each failing test failed."""

    finished: bool
    ran: int = 0
    outcomes: dict = field(default_factory=dict)    # test id -> "failure:X" / "error:X"
    skipped: int = 0
    ok: bool = False
    tail: str = ""                      # the last of its output, for the record and the card
    timed_out: bool = False
    busy: bool = False                  # the sandbox was in use by someone else: not run

    @property
    def failed(self) -> list:
        return sorted(self.outcomes)

    def assertion_failures(self, module: str) -> list:
        """The tests of ``module`` that failed by an assertion (not an error)."""
        return sorted(t for t, o in self.outcomes.items()
                      if o.startswith("failure:") and (t == module or t.startswith(module + ".")))

    def errors_in(self, module: str) -> list:
        return sorted(t for t, o in self.outcomes.items() if o.startswith("error:")
                      and (module in t))

    def to_dict(self) -> dict:
        return {"finished": self.finished, "ran": self.ran,
                "outcomes": dict(sorted(self.outcomes.items())[:300]),
                "failed_count": len(self.outcomes), "skipped": self.skipped, "ok": self.ok,
                "timed_out": self.timed_out, "busy": self.busy, "tail": self.tail[-1200:]}

    @classmethod
    def from_dict(cls, doc: dict | None) -> SuiteResult | None:
        if not isinstance(doc, dict):
            return None
        outcomes = doc.get("outcomes") if isinstance(doc.get("outcomes"), dict) else {}
        return cls(bool(doc.get("finished")), int(doc.get("ran") or 0),
                   {str(k): str(v) for k, v in outcomes.items()}, int(doc.get("skipped") or 0),
                   bool(doc.get("ok")), str(doc.get("tail") or ""), bool(doc.get("timed_out")),
                   bool(doc.get("busy")))


def new_failures(base: SuiteResult, head: SuiteResult) -> list:
    """Every test failing with the change that main does not fail in exactly the same way: a
    test main fails by an ImportError and the change fails by an assertion is a NEW failure."""
    return sorted(t for t, o in head.outcomes.items() if base.outcomes.get(t) != o)


def module_name(test_path: str) -> str | None:
    """``tests/test_x.py`` -> ``test_x`` (a module the runner can load), else None."""
    parts = test_path.split("/")
    if len(parts) != 2 or parts[0] != "tests" or not parts[1].endswith(".py"):
        return None
    name = parts[1][:-3]
    return name if name.startswith("test") and _MODULE.fullmatch(name) else None


def argv(python: str, out: Path, modules=()) -> list:
    return [str(python), "-I", "-X", "utf8", "-c", RUNNER, str(out), *modules]


def read_result(out: Path, *, returncode, timed_out: bool, tail: str) -> SuiteResult:
    """What a run left in ``out``; a run that wrote nothing readable did not finish."""
    if timed_out:
        return SuiteResult(False, tail=tail, timed_out=True)
    try:
        raw = Path(out).read_bytes()[:MAX_RESULT_BYTES]
        doc = json.loads(raw.decode("utf-8", "replace"))
    except (OSError, ValueError):
        return SuiteResult(False, tail=tail or f"the run wrote no result (exit {returncode})")
    if not isinstance(doc, dict) or not isinstance(doc.get("outcomes"), dict):
        return SuiteResult(False, tail=tail or "the run's result is not readable")
    outcomes = {str(k)[:300]: str(v)[:100] for k, v in list(doc["outcomes"].items())[:5000]}
    ran = doc.get("ran") if isinstance(doc.get("ran"), int) else 0
    ok = doc.get("ok") is True and not outcomes and returncode == 0
    return SuiteResult(True, ran, outcomes, int(doc.get("skipped") or 0), ok, tail)
