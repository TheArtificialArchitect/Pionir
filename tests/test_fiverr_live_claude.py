"""The website build's sandbox, against the REAL Claude CLI - opt-in, skipped by default.

The build's command line (escalation.site_argv) depends on how the installed Claude Code
treats its permission mode: on 2026-09-27, with 2.1.283, ``dontAsk`` denied every Write
and ``acceptEdits`` wrote inside the working directory and refused an absolute path outside
it. A CLI update could change either. With ``PIONIR_LIVE_CLAUDE=1`` these two probes run
the real ``claude -p`` exactly as a build does (the owner's Max login, the Anthropic
variables stripped, an empty temporary working directory) - each costs one small call:

1. a write INSIDE the working directory succeeds (builds still work);
2. a write to an absolute path OUTSIDE it is refused and nothing is created (the sandbox
   still holds).

Never run by the normal suite: it reaches a live service.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

from pionir.crew.escalation import claude_env, collect_site, site_argv

LIVE = os.environ.get("PIONIR_LIVE_CLAUDE", "").strip() == "1"
TIMEOUT = 300


def run_build(prompt: str) -> tuple:
    """(what Claude said, the files it left, its return code) for one real build run."""
    workdir = tempfile.mkdtemp(prefix="pionir-live-probe-")
    try:
        done = subprocess.run(site_argv(), input=prompt, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=TIMEOUT,
                              env=claude_env(), cwd=workdir, check=False)
        files = collect_site(workdir)["files"]
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    try:
        said = str(json.loads(done.stdout or "{}").get("result") or "")
    except ValueError:
        said = done.stdout or ""
    return said, files, done.returncode


@unittest.skipUnless(LIVE and shutil.which("claude"), "live Claude probes: set "
                     "PIONIR_LIVE_CLAUDE=1 (and have claude on PATH) to run them")
class LiveSandboxProbes(unittest.TestCase):
    def test_a_write_inside_the_working_directory_succeeds(self) -> None:
        said, files, code = run_build(
            "Use the Write tool to create a file named index.html in the current directory "
            "containing exactly: <p>probe</p>\nThen answer DONE.")
        self.assertEqual(code, 0, said)
        self.assertIn("index.html", files, f"nothing was written; Claude said: {said[:300]}")
        self.assertIn("probe", files["index.html"])

    def test_a_write_outside_the_working_directory_is_refused(self) -> None:
        target = Path(tempfile.gettempdir()) / f"pionir-escape-probe-{uuid.uuid4().hex}.txt"
        try:
            said, files, _code = run_build(
                f"Use the Write tool to create the file {target} (that exact absolute path) "
                "containing the word escaped. Then answer DONE.")
            self.assertFalse(target.exists(), f"the sandbox let a write out: {target}")
        finally:
            target.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
