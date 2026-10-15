"""One user of the build sandbox at a time (build_sandbox.SandboxLease).

Everything that runs as the sandbox user starts by killing every process that user has, so a
build Daedalus, the night builds' and the API builder's contained checks, the self-improvement
worker's runs and the video tutorial runner must never overlap. Each holds the sandbox's one
lease while it uses it; whoever finds it held waits (never a failure) and never reaps. These
tests fail if any of them stops taking the lease or reaps without it.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pionir import build_sandbox as bs
from pionir.crew.builds import review
from pionir.video.tutorial import Step, TutorialError, run_step


class Setup:
    def __init__(self, root: Path) -> None:
        self.sandbox_root = root
        self.runs_dir = root / ".runs"
        self.python = Path(sys.executable)
        self.python_dir = self.python.parent
        self.sid = None
        self.node = None
        self.node_tools = None
        self.reaps = 0

    def reap(self):
        self.reaps += 1

    def preflight(self):
        return {}

    def logon(self):
        return None


class LeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._t = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.setup = Setup(Path(self._t.name))

    def tearDown(self) -> None:
        self._t.cleanup()

    def test_one_holder_at_a_time_and_it_names_the_holder(self) -> None:
        with bs.SandboxLease(self.setup, "a Daedalus build"):
            self.assertIn("a Daedalus build", bs.sandbox_busy(self.setup))
            with self.assertRaises(bs.SandboxBusy) as caught:
                bs.SandboxLease(self.setup, "someone else").acquire()
            self.assertIn("a Daedalus build", str(caught.exception))
        self.assertIsNone(bs.sandbox_busy(self.setup))
        with bs.SandboxLease(self.setup, "the next one"):  # released: free again
            pass

    def test_the_night_builds_test_run_waits_and_never_reaps(self) -> None:
        with bs.SandboxLease(self.setup, "the self-improvement worker's test run"):
            got = review.run_tests({"tests/test_x.py": b""}, "python", setup=self.setup)
        self.assertTrue(got.busy)
        self.assertIn("could not start contained", got.tail)
        self.assertEqual(self.setup.reaps, 0)

    def test_the_api_builders_checks_wait_and_never_reap(self) -> None:
        self.setup.node = Path(sys.executable)
        tools = Path(self._t.name) / "node-tools"
        for rel in (bs.TSC_JS, bs.VITEST_MJS):              # node is "set up"
            Path(tools, *rel).parent.mkdir(parents=True, exist_ok=True)
            Path(tools, *rel).write_text("", encoding="utf-8")
        self.setup.node_tools = tools
        self.assertIsNone(bs.node_ready(self.setup))
        with bs.SandboxLease(self.setup, "a Daedalus build"):
            got = bs.run_ts_checks({"src/x.ts": b""}, setup=self.setup)
        self.assertIn("could not start contained", got.tail)
        self.assertIn("busy", got.tail)
        self.assertEqual(self.setup.reaps, 0)

    def test_the_video_tutorial_runner_waits_and_never_reaps(self) -> None:
        step = Step("s1", "a step", ("python", "-c", "print(1)"), timeout=10.0)
        with bs.SandboxLease(self.setup, "a Daedalus build"):
            with self.assertRaisesRegex(TutorialError, "busy"):
                run_step(step, setup=self.setup)
        self.assertEqual(self.setup.reaps, 0)

    def test_a_build_daedalus_holds_it_for_its_whole_life(self) -> None:
        setup = SimpleNamespace(sandbox_root=self.setup.sandbox_root, reap=lambda: None,
                                preflight=lambda: {}, python=Path(sys.executable),
                                state_dir=self.setup.sandbox_root / ".state",
                                runs_dir=self.setup.runs_dir, sid=None)
        launcher = bs.BuildDaedalus(setup, gate=SimpleNamespace(start=lambda: None,
                                                                stop=lambda: None))
        launcher._answers = lambda: True            # fails after the reap: lease let go
        with self.assertRaises(bs.SandboxError):
            launcher.start(not_after=0.0)
        self.assertIsNone(bs.sandbox_busy(self.setup))
        with bs.SandboxLease(self.setup, "the video tutorial runner"):
            reaped = []
            setup.reap = lambda: reaped.append(1)
            with self.assertRaisesRegex(bs.SandboxError, "busy"):
                launcher.start(not_after=0.0)
            self.assertEqual(reaped, [])            # never reaped under the tutorial


if __name__ == "__main__":
    unittest.main()
