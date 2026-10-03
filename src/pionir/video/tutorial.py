"""Tutorial evidence: a command is shown on screen only after it was really run, here.

A tutorial step is a spec - a title, an argv, optionally a few files to lay down first and the
patterns that read measured numbers out of what the command printed. ``run_step`` runs it in
the build sandbox (the ``pionir-builds`` user at Low integrity, in a kill-on-close job object
with a firewalled interpreter; see build_sandbox.py) and returns a RunEvidence carrying the
exit code, the captured stdout and the measured numbers. ``write_runs`` records those into a
pack file. Then the script gate (script.py) lets a ``run`` scene stand only on a run that
succeeded, and grounding checks every number the narration states against that run's output.

* A step whose measurement pattern does not match the output has measured nothing: that is
  an error, never a made-up zero.
* The runner name is recorded: ``sandbox`` only when the real sandbox ran it. A live niche's
  pack loader refuses any other name, so a hand-injected run cannot back a live video.
* The first argv word ``python`` means the sandbox's dedicated interpreter; anything else must
  be a bare program name or sit inside the run folder.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .passages import MAX_STDOUT, RunEvidence, RunMeasure, SANDBOX_RUNNER

STEP_TIMEOUT = 120.0
MAX_FILES = 12
MAX_FILE_CHARS = 20_000
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
_STEP_FIELDS = {"id", "title", "argv", "files", "measure", "timeout"}


class TutorialError(ValueError):
    """A step that cannot be run or measured, with the reason in words."""


@dataclass(frozen=True, slots=True)
class Measure:
    name: str
    pattern: str            # one capture group: the number
    unit: str = ""


@dataclass(frozen=True, slots=True)
class Step:
    id: str
    title: str
    argv: tuple[str, ...]
    files: tuple[tuple[str, str], ...] = ()
    measure: tuple[Measure, ...] = ()
    timeout: float = STEP_TIMEOUT


def parse_steps(raw: Any) -> list[Step]:
    """Validate a list of step specs (the contents of a tutorial steps file)."""
    if not isinstance(raw, list) or not raw:
        raise TutorialError("steps must be a non-empty list")
    steps: list[Step] = []
    for n, item in enumerate(raw, 1):
        where = f"step #{n}"
        if not isinstance(item, Mapping):
            raise TutorialError(f"{where}: must be an object")
        stray = set(item) - _STEP_FIELDS
        if stray:
            raise TutorialError(f"{where}: unknown fields {sorted(stray)}")
        sid, title = item.get("id"), item.get("title")
        if not isinstance(sid, str) or not _ID.match(sid):
            raise TutorialError(f"{where}: id must match {_ID.pattern}")
        if not isinstance(title, str) or not title.strip():
            raise TutorialError(f"{where}: a title is needed")
        argv = item.get("argv")
        if (not isinstance(argv, list) or not argv or len(argv) > 24
                or not all(isinstance(a, str) and a and len(a) <= 400 for a in argv)):
            raise TutorialError(f"{where}: argv must be 1-24 non-empty strings")
        files: list[tuple[str, str]] = []
        for rel, text in (item.get("files") or {}).items():
            path = PurePosixPath(str(rel))
            if (path.is_absolute() or ".." in path.parts or "\\" in str(rel) or ":" in str(rel)
                    or not isinstance(text, str) or len(text) > MAX_FILE_CHARS):
                raise TutorialError(f"{where}: file {rel!r} must be a relative path inside the "
                                    f"run folder with text of at most {MAX_FILE_CHARS} characters")
            files.append((str(path), text))
        if len(files) > MAX_FILES:
            raise TutorialError(f"{where}: more than {MAX_FILES} files")
        measures: list[Measure] = []
        for m in item.get("measure") or []:
            if (not isinstance(m, Mapping) or not isinstance(m.get("name"), str)
                    or not isinstance(m.get("pattern"), str)):
                raise TutorialError(f"{where}: a measure needs a name and a pattern")
            try:
                compiled = re.compile(m["pattern"])
            except re.error as error:
                raise TutorialError(f"{where}: pattern {m['pattern']!r}: {error}") from error
            if compiled.groups != 1:
                raise TutorialError(f"{where}: pattern {m['pattern']!r} needs exactly one group")
            measures.append(Measure(m["name"], m["pattern"], str(m.get("unit", ""))))
        timeout = item.get("timeout", STEP_TIMEOUT)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 600:
            raise TutorialError(f"{where}: timeout must be 1-600 seconds")
        steps.append(Step(sid, title.strip(), tuple(argv), tuple(files), tuple(measures),
                          float(timeout)))
    if len({s.id for s in steps}) != len(steps):
        raise TutorialError("duplicate step ids")
    return steps


def measure_output(step: Step, stdout: str) -> tuple[RunMeasure, ...]:
    """Read each measurement off the output. A pattern that finds nothing is an error."""
    found: list[RunMeasure] = []
    for m in step.measure:
        hit = re.search(m.pattern, stdout, re.MULTILINE)
        if hit is None:
            raise TutorialError(f"step {step.id!r}: measurement {m.name!r} ({m.pattern!r}) "
                                "is not in the command's output")
        try:
            value = float(hit.group(1).replace(",", ""))
        except ValueError as error:
            raise TutorialError(f"step {step.id!r}: measurement {m.name!r} matched "
                                f"{hit.group(1)!r}, which is not a number") from error
        found.append(RunMeasure(m.name, value, m.unit))
    return tuple(found)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _evidence(step: Step, code, timed_out: bool, stdout: str, runner: str,
              clock: Callable[[], str]) -> RunEvidence:
    stdout = stdout.strip()[-MAX_STDOUT:]
    ok = code == 0 and not timed_out and bool(stdout)
    measured = measure_output(step, stdout) if ok else ()
    return RunEvidence(step.id, step.title, step.argv, code, timed_out, stdout, measured,
                       runner, clock())


def run_step(step: Step, *, setup, spawner=None, clock: Callable[[], str] = _now) -> RunEvidence:
    """Run one step in the build sandbox. Raises TutorialError when it cannot start contained;
    a command that runs and fails is returned as a failed RunEvidence (and blocks the script)."""
    from pionir import build_sandbox as bs

    argv = [str(setup.python) if step.argv[0] == "python" else step.argv[0], *step.argv[1:]]
    work = Path(setup.runs_dir) / f"tutorial-{secrets.token_hex(6)}"
    try:
        product = work / "product"
        product.mkdir(parents=True)
        for rel, text in step.files:
            target = product / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
        env = bs.minimal_env(work=work / "env", path_dirs=[setup.python_dir, system32])
        limits = bs.JobLimits(active_processes=8, job_memory_mb=2048,
                              cpu_seconds=max(30.0, step.timeout))
        try:
            setup.reap()
            setup.preflight()
            done = bs.run(argv, cwd=product, env=env, out_dir=work / "out",
                          timeout=step.timeout, limits=limits, logon=setup.logon(),
                          spawner=spawner, sid=getattr(setup, "sid", None))
        except (bs.SandboxError, OSError) as exc:
            raise TutorialError(f"step {step.id!r} could not start contained ({exc})") from exc
        finally:
            try:
                setup.reap()
            except (bs.SandboxError, OSError):
                pass
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return _evidence(step, done.returncode, done.timed_out, done.stdout, SANDBOX_RUNNER, _now)


def run_steps(steps: list[Step], *, setup=None, sandbox_root: str | None = None,
              spawner=None) -> list[RunEvidence]:
    """Run every step; the sandbox must be set up, or nothing runs (no fallback to the host)."""
    if setup is None:
        from pionir import build_sandbox as bs

        setup, why = bs.load_setup(sandbox_root or bs.default_sandbox_root())
        if setup is None:
            raise TutorialError(f"the build sandbox is not available, so nothing was run: {why}")
    return [run_step(step, setup=setup, spawner=spawner) for step in steps]


def run_record(run: RunEvidence) -> dict[str, Any]:
    return {"id": run.id, "title": run.title, "argv": list(run.argv), "exit_code": run.exit_code,
            "timed_out": run.timed_out, "stdout": run.stdout,
            "measured": [{"name": m.name, "value": m.value, "unit": m.unit} for m in run.measured],
            "runner": run.runner, "ran_at": run.ran_at}


def write_runs(pack_path: Path | str, runs: list[RunEvidence]) -> int:
    """Record runs into a pack file, replacing runs with the same id. Returns the run count."""
    path = Path(pack_path)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise TutorialError(f"cannot read the pack {path}: {error}") from error
    if not isinstance(document, dict):
        raise TutorialError(f"{path}: expected an object")
    new = {r.id: run_record(r) for r in runs}
    kept = [r for r in document.get("runs") or [] if r.get("id") not in new]
    document["runs"] = kept + list(new.values())
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return len(document["runs"])
