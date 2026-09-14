"""One subprocess runner for every CLI-backed adapter.

Every adapter that shells out to a specialist had its own copy of the same
``subprocess.run`` call, and each copy carried the same two defects:

* A non-zero exit threw the child's stderr away and reported only the exit
  code, so "exited with code 1" stood where the child had written exactly why.
  ``atani_cli`` already surfaced the reason; the others did not.
* The pipe was decoded as UTF-8, but the children print JSON with
  ``ensure_ascii=False`` through a pipe that Windows opens as cp1252 unless
  ``PYTHONUTF8`` is set. It worked on this machine only because the user's
  environment happens to set ``PYTHONUTF8=1``; a service account or a fresh
  shell would have handed back mojibake or a ``UnicodeEncodeError`` on the
  far side. The environment is pinned per call so the contract does not
  depend on who launched Pionir.

Nothing here judges the exit code: whether non-zero is fatal is a per-command
decision (an Atani executive verdict lives on stdout at exit 2; a Nyx refusal
is a real answer). Callers get the raw result and ``unavailable`` builds the
error message when they decide it is one.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass

from pionir.errors import AdapterProtocolError, AdapterUnavailable

# A specialist's whole answer is one JSON document; anything past this is a
# runaway, not an answer, and is refused before json.loads sees it.
MAX_OUTPUT_CHARS = 2_000_000
STDERR_TAIL_CHARS = 300


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


def child_env() -> dict[str, str]:
    """The parent's environment with UTF-8 stdio forced on the child."""

    return {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def stderr_tail(stderr: str, *, limit: int = STDERR_TAIL_CHARS) -> str:
    """The last non-empty stderr line, capped - the line that usually says why."""

    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    if not lines:
        return ""
    tail = lines[-1]
    return tail if len(tail) <= limit else tail[: limit - 3] + "..."


def unavailable(label: str, result: ProcessResult) -> AdapterUnavailable:
    """A non-zero exit as an AdapterUnavailable that keeps the child's reason."""

    reason = stderr_tail(result.stderr)
    message = f"{label} exited with status {result.returncode}"
    if reason:
        message = f"{message}: {reason}"
    return AdapterUnavailable(message)


def run_process(
    command: Sequence[str],
    *,
    label: str,
    timeout_seconds: float,
    cwd: str | None = None,
    input_text: str | None = None,
    max_output_chars: int = MAX_OUTPUT_CHARS,
) -> ProcessResult:
    """Run one specialist command without a shell and return what it said.

    Raises AdapterUnavailable when the executable is missing or the deadline
    passes, and AdapterProtocolError when stdout exceeds ``max_output_chars``.
    The exit code is returned, not judged.
    """

    try:
        process = subprocess.run(
            list(command),
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            shell=False,
            timeout=timeout_seconds,
            cwd=cwd,
            input=input_text,
            env=child_env(),
        )
    except FileNotFoundError as error:
        raise AdapterUnavailable(f"{label}'s configured executable was not found") from error
    except subprocess.TimeoutExpired as error:
        raise AdapterUnavailable(
            f"{label} did not finish within {timeout_seconds:g} seconds"
        ) from error
    stdout = process.stdout or ""
    if len(stdout) > max_output_chars:
        raise AdapterProtocolError(f"{label}'s output exceeded the size limit")
    return ProcessResult(
        returncode=process.returncode, stdout=stdout, stderr=process.stderr or ""
    )
