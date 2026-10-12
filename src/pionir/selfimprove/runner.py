"""The one unittest runner a self-improvement patch is judged with.

The same code runs the targeted tests in the build sandbox (as ``pionir-builds``, contained),
in Daedalus's own gate, and in the owner's repo after Ian's yes - so "passes" means the same
thing everywhere. It is run as ``python -I -X utf8 -c RUNNER <result.json> [module ...]`` from
the repo root: ``src`` and ``tests`` on the path (the way CI runs ``unittest discover -s
tests``), the named test modules - or, with none, the whole suite - and the ids of every failed
or erroring test written to ``result.json``, so two runs can be compared test by test (the base
suite may fail where the sandbox lacks something; a patch may add no NEW failure).

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
    "bad = [t.id() for t, _ in res.failures] + [t.id() for t, _ in res.errors]\n"
    "bad += [t.id() for t in getattr(res, 'unexpectedSuccesses', [])]\n"
    "json.dump({'ran': res.testsRun, 'failed': sorted(set(bad)), "
    "'skipped': len(res.skipped), 'ok': res.wasSuccessful()}, open(out, 'w'))\n"
    "sys.exit(0 if res.wasSuccessful() else 1)\n"
)
MAX_RESULT_BYTES = 2_000_000
_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass
class SuiteResult:
    """One run: did it finish, how many ran, which failed. ``failed`` holds test ids."""

    finished: bool
    ran: int = 0
    failed: list = field(default_factory=list)
    skipped: int = 0
    ok: bool = False
    tail: str = ""                      # the last of its output, for the record and the card
    timed_out: bool = False

    def to_dict(self) -> dict:
        return {"finished": self.finished, "ran": self.ran, "failed": self.failed[:200],
                "failed_count": len(self.failed), "skipped": self.skipped, "ok": self.ok,
                "timed_out": self.timed_out, "tail": self.tail[-1200:]}

    @classmethod
    def from_dict(cls, doc: dict | None) -> SuiteResult | None:
        if not isinstance(doc, dict):
            return None
        return cls(bool(doc.get("finished")), int(doc.get("ran") or 0),
                   [str(t) for t in doc.get("failed") or []], int(doc.get("skipped") or 0),
                   bool(doc.get("ok")), str(doc.get("tail") or ""), bool(doc.get("timed_out")))


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
    if not isinstance(doc, dict) or not isinstance(doc.get("failed"), list):
        return SuiteResult(False, tail=tail or "the run's result is not readable")
    failed = sorted({str(t)[:300] for t in doc["failed"][:5000]})
    ran = doc.get("ran") if isinstance(doc.get("ran"), int) else 0
    ok = doc.get("ok") is True and not failed and returncode == 0
    return SuiteResult(True, ran, failed, int(doc.get("skipped") or 0), ok, tail)
