"""coding.daedalus_build: Daedalus may write ONLY in a sandbox repo the Builds worker made.

Daedalus's live policy commits a passing change onto whatever branch a repo is on, it reaches
every repo under C:\\src, and an EMPTY repo falls back to its own protected repository. So the
crew's build capability is fenced by the adapter itself, whatever permission a caller claims:
exactly ``<sandbox_root>\\<slug>``, a real fresh repo carrying the worker's marker, nothing
else. Each test fails if the rule it names is reverted: an empty repo sent (to either
capability), a live repo, a ``..`` path, a link or junction, another drive or a clone reached,
a build given the solve's 600 s instead of its own budget, a build started after its window,
or the job's branch/commit/gate hidden from the caller. Daedalus is a fake; git is real, in
temporary folders.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from pionir.adapters._http import HttpStatusError
from pionir.adapters.daedalus import (
    BUILD,
    BUILD_PERMISSION,
    SANDBOX_MARKER,
    SOLVE,
    AdapterTimeout,
    DaedalusAdapter,
    DaedalusSettings,
    sandbox_repo_problem,
)
from pionir.contracts import RiskLevel, Task
from pionir.crew.builds import backlog, sandbox
from pionir.errors import AdapterProtocolError, AdapterUnavailable

T0 = 1_800_000_000.0


def entry(slug="cron-explain") -> dict:
    return dict(next(e for e in backlog.SEED if e["slug"] == slug))


class FakeDaedalus:
    """POST /jobs, GET /jobs/<id>, POST /jobs/<id>/cancel - the job stays running for
    ``running_polls`` polls, then finishes with ``detail``."""

    def __init__(self, detail=None, *, running_polls=0, forever=False) -> None:
        self.detail = detail or {"state": "done", "result": {"ok": True}}
        self.running_polls = running_polls
        self.forever = forever
        self.calls: list = []
        self.cancelled = False

    def post(self, path, payload, *, timeout_seconds=None):
        self.calls.append(("POST", path, dict(payload), timeout_seconds))
        if path == "/jobs":
            return {"job": {"id": "job-7"}}
        if path.endswith("/cancel"):
            self.cancelled = True
            return {"ok": True}
        raise AssertionError(path)

    def get(self, path, *, timeout_seconds=None):
        self.calls.append(("GET", path, None, timeout_seconds))
        if self.cancelled and not self.forever:
            return {"job": {"id": "job-7", "state": "cancelled"}}
        if self.forever or self.running_polls > 0:
            self.running_polls -= 1
            return {"job": {"id": "job-7", "state": "running"}}
        return {"job": {"id": "job-7", **self.detail}}


class Clock:
    def __init__(self, start=0.0, step=10.0) -> None:
        self.t = start
        self.step = step

    def mono(self) -> float:
        self.t += self.step
        return self.t


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._t = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._t.name)
        self.root = self.tmp / "daedalus-work"
        self.root.mkdir()

    def tearDown(self) -> None:
        self._t.cleanup()

    def make_sandbox(self, slug="cron-explain") -> Path:
        sandbox.create(self.root, entry(slug), year=2026, created_at=T0)
        return self.root / slug

    def adapter(self, client, *, clock=None, wall=T0, **settings) -> DaedalusAdapter:
        clock = clock or Clock()
        return DaedalusAdapter(DaedalusSettings(sandbox_root=str(self.root),
                                                poll_interval_seconds=1.0, **settings),
                               client=client, sleep=lambda _s: None, monotonic=clock.mono,
                               clock=lambda: wall)

    def build(self, repo, **over) -> Task:
        payload = {"intent": "build the product in BRIEF.md", "repo": str(repo),
                   "budget_seconds": 2700, "not_after": T0 + 2700}
        payload.update(over)
        return Task(BUILD, payload, frozenset({BUILD_PERMISSION}))


class SandboxRuleTests(_Case):
    """The rule itself: exactly a marked, fresh repo directly inside the sandbox root."""

    def test_a_fresh_sandbox_repo_passes(self) -> None:
        repo = self.make_sandbox()
        self.assertIsNone(sandbox_repo_problem(str(repo), str(self.root)))

    def test_an_empty_repo_is_refused(self) -> None:
        for empty in ("", "   ", None):
            self.assertIn("no repo", sandbox_repo_problem(empty, str(self.root)))

    def test_a_live_repo_outside_the_workspace_is_refused(self) -> None:
        live = self.tmp / "Pionir"
        live.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=live, check=True)
        self.assertIn("not directly inside", sandbox_repo_problem(str(live), str(self.root)))
        # and the real ones, by path alone: nothing outside the workspace is reachable
        for path in (r"C:\src\Pionir", r"C:\src\Tech-Support", r"C:\src\Nyx.Voodoo"):
            self.assertIsNotNone(sandbox_repo_problem(path, str(self.root)))

    def test_a_live_repo_copied_into_the_workspace_is_refused(self) -> None:
        # a clone of a live repo, even under a slug-shaped name and carrying a marker
        clone = self.root / "pionir"
        clone.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=clone, check=True)
        self.assertIn("not a sandbox repo", sandbox_repo_problem(str(clone), str(self.root)))
        (clone / ".git" / SANDBOX_MARKER).write_text('{"slug": "pionir"}', encoding="utf-8")
        subprocess.run(["git", "remote", "add", "origin", "https://example.invalid/p.git"],
                       cwd=clone, check=True)
        self.assertIn("has a remote", sandbox_repo_problem(str(clone), str(self.root)))

    def test_a_dot_dot_path_is_refused(self) -> None:
        repo = self.make_sandbox()
        sneaky = f"{self.root}{os.sep}..{os.sep}{self.root.name}{os.sep}{repo.name}"
        self.assertIn("'..'", sandbox_repo_problem(sneaky, str(self.root)))
        self.assertIn("'..'", sandbox_repo_problem(f"{repo}{os.sep}..{os.sep}..",
                                                   str(self.root)))

    def test_a_relative_unc_or_expanding_path_is_refused(self) -> None:
        self.make_sandbox()
        for bad in ("cron-explain", r"\\server\share\cron-explain", "~/cron-explain",
                    r"%USERPROFILE%\cron-explain"):
            self.assertIsNotNone(sandbox_repo_problem(bad, str(self.root)), bad)

    def test_another_drive_is_refused(self) -> None:
        self.make_sandbox()
        drive = os.path.splitdrive(str(self.root))[0].upper()
        other = "D:" if drive != "D:" else "E:"
        tail = os.path.splitdrive(str(self.root))[1]
        path = f"{other}{tail}{os.sep}cron-explain"
        self.assertIn("not directly inside", sandbox_repo_problem(path, str(self.root)))

    @unittest.skipUnless(os.name == "nt", "Windows paths are case-insensitive")
    def test_case_is_ignored_on_windows_both_ways(self) -> None:
        repo = self.make_sandbox()
        upper = str(self.root).upper() + os.sep + repo.name
        self.assertIsNone(sandbox_repo_problem(upper, str(self.root)))
        self.assertIsNone(sandbox_repo_problem(str(repo), str(self.root).lower()))
        # the slug itself is a slug: an upper-case name is not one this worker made
        self.assertIsNotNone(sandbox_repo_problem(str(repo).upper(), str(self.root)))

    def test_a_link_or_junction_into_a_live_repo_is_refused(self) -> None:
        live = self.tmp / "live"
        live.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=live, check=True)
        (live / ".git" / SANDBOX_MARKER).write_text('{"slug": "linked"}', encoding="utf-8")
        link = self.root / "linked"
        made = False
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(live), str(link))
            made = True
        else:
            try:
                os.symlink(live, link, target_is_directory=True)
                made = True
            except OSError:
                pass
        if not made:
            self.skipTest("no link could be made here")
        self.assertIn("link or junction", sandbox_repo_problem(str(link), str(self.root)))

    def test_a_symlink_is_refused_where_one_can_be_made(self) -> None:
        live = self.tmp / "live2"
        live.mkdir()
        try:
            os.symlink(live, self.root / "symlinked", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks need a privilege here")
        self.assertIn("link or junction",
                      sandbox_repo_problem(str(self.root / "symlinked"), str(self.root)))

    def test_a_workspace_that_is_itself_a_junction_is_refused(self) -> None:
        if os.name != "nt":
            self.skipTest("junctions are Windows")
        import _winapi
        real = self.tmp / "real-root"
        real.mkdir()
        fake_root = self.tmp / "junction-root"
        _winapi.CreateJunction(str(real), str(fake_root))
        self.assertIn("itself a link", sandbox_repo_problem(str(fake_root / "x-slug"),
                                                            str(fake_root)))

    def test_a_linked_worktree_is_refused(self) -> None:
        repo = self.make_sandbox()
        wt = self.root / "wt-copy"
        subprocess.run(["git", "worktree", "add", "-q", str(wt)], cwd=repo, check=True,
                       capture_output=True)
        self.assertIn("no .git folder", sandbox_repo_problem(str(wt), str(self.root)))

    def test_a_name_that_is_not_a_slug_is_refused(self) -> None:
        self.assertIn("not a sandbox repo's name",
                      sandbox_repo_problem(str(self.root / "Has Spaces"), str(self.root)))


class BuildCapabilityTests(_Case):
    def test_the_build_is_privileged_by_its_own_narrow_grant_and_not_routable(self) -> None:
        caps = {c.name: c for c in DaedalusAdapter(client=FakeDaedalus()).manifest.capabilities}
        self.assertEqual(caps[BUILD].risk, RiskLevel.PRIVILEGED)
        self.assertEqual(caps[BUILD].required_permissions, frozenset({BUILD_PERMISSION}))
        self.assertFalse(caps[BUILD].routable)
        self.assertTrue(caps[BUILD].model.exclusive_card)          # it takes the GPU lease
        # the solve keeps its own permission, which nothing holds
        self.assertEqual(caps[SOLVE].required_permissions, frozenset({"daedalus.solve"}))
        self.assertTrue(caps[SOLVE].routable)

    def test_a_repo_outside_the_sandbox_is_refused_before_daedalus_is_called(self) -> None:
        client = FakeDaedalus()
        adapter = self.adapter(client)
        for repo in ("", r"C:\src\Pionir", str(self.root / ".." / "x"),
                     str(self.root / "no-such-slug")):
            with self.assertRaises(AdapterProtocolError):
                adapter.validate(self.build(repo))
            with self.assertRaises(AdapterProtocolError):
                adapter.execute(self.build(repo))
        self.assertEqual(client.calls, [])

    def test_an_empty_repo_is_refused_for_a_solve_too(self) -> None:
        client = FakeDaedalus()
        adapter = self.adapter(client)
        for payload in ({"content": "fix it"}, {"content": "fix it", "repo": "  "}):
            task = Task(SOLVE, payload, frozenset({"daedalus.solve"}))
            with self.assertRaises(AdapterProtocolError):
                adapter.validate(task)
            with self.assertRaises(AdapterProtocolError):
                adapter.execute(task)
        self.assertEqual(client.calls, [])

    def test_a_build_rejects_unknown_fields_and_bad_budgets(self) -> None:
        repo = self.make_sandbox()
        adapter = self.adapter(FakeDaedalus())
        for over in ({"dry_run": True}, {"budget_seconds": 30}, {"budget_seconds": 99999},
                     {"budget_seconds": True}, {"not_after": "tonight"}):
            with self.assertRaises(AdapterProtocolError, msg=str(over)):
                adapter.validate(self.build(repo, **over))

    def test_the_build_runs_on_its_own_budget_not_the_solves_600_seconds(self) -> None:
        repo = self.make_sandbox()
        # still running after 1000 s of polls (10 s each): a solve would have been cancelled
        client = FakeDaedalus({"state": "done", "result": {"ok": True, "commit": "abc1234"}},
                              running_polls=100)
        out = self.adapter(client).execute(self.build(repo)).output
        self.assertFalse(client.cancelled)
        self.assertIs(out["ok"], True)
        posted = client.calls[0][2]
        self.assertEqual(posted["repo"], os.path.abspath(str(repo)))
        self.assertIs(posted["dry_run"], False)

    def test_the_deadline_is_the_windows_end_when_that_comes_first(self) -> None:
        repo = self.make_sandbox()
        client = FakeDaedalus(forever=True)
        clock = Clock(step=10.0)
        adapter = self.adapter(client, clock=clock)
        with self.assertRaises(AdapterTimeout) as caught:
            adapter.execute(self.build(repo, not_after=T0 + 300))     # 5 minutes left
        self.assertTrue(client.cancelled)
        self.assertEqual(caught.exception.job_id, "job-7")
        # cancelled at ~300 s, well before the 2700 s budget, and it waited for the stop
        polls = [c for c in client.calls if c[0] == "GET"]
        self.assertLess(len(polls), 60)
        self.assertIn("/jobs/job-7/cancel", [c[1] for c in client.calls])

    def test_a_cancelled_build_is_waited_on_under_the_lease(self) -> None:
        repo = self.make_sandbox()
        client = FakeDaedalus()
        client.forever = False
        client.running_polls = 10 ** 6
        adapter = self.adapter(client, clock=Clock(step=100.0))
        with self.assertRaises(AdapterTimeout) as caught:
            adapter.execute(self.build(repo, budget_seconds=120))
        self.assertIn("stopped: cancelled", str(caught.exception))

    def test_a_build_whose_window_closed_before_it_started_is_not_sent(self) -> None:
        repo = self.make_sandbox()
        client = FakeDaedalus()
        out = self.adapter(client, wall=T0 + 3000).execute(self.build(repo)).output
        self.assertIs(out["ok"], False)
        self.assertIs(out["started"], False)
        self.assertIn("refused", out)
        self.assertEqual(client.calls, [])

    def test_the_outcome_surfaces_branch_commit_gate_and_files(self) -> None:
        repo = self.make_sandbox()
        detail = {"state": "done", "branch": "main", "commit": "c0ffee1", "files": ["a.py"],
                  "passed": True, "landed": True, "stage_failed": None,
                  "result": {"ok": True, "commit": "c0ffee1",
                             "gate": {"passed": True, "landed": True, "stage_failed": None,
                                      "reason": "all stages passed"}}}
        result = self.adapter(FakeDaedalus(detail)).execute(self.build(repo))
        out = result.output
        self.assertEqual(out["branch"], "main")
        self.assertEqual(out["commit"], "c0ffee1")
        self.assertEqual(out["files"], ["a.py"])
        self.assertIs(out["passed"], True)
        self.assertIs(out["landed"], True)
        self.assertEqual(out["gate_reason"], "all stages passed")
        self.assertIn("daedalus:build", result.evidence)
        self.assertIn("daedalus:commit:c0ffee1", result.evidence)

    def test_a_failed_gate_names_its_stage(self) -> None:
        repo = self.make_sandbox()
        detail = {"state": "error", "result": {"ok": False, "error": "tests failed",
                                               "gate": {"passed": False, "landed": False,
                                                        "stage_failed": "G2-tests",
                                                        "reason": "2 failing"}}}
        out = self.adapter(FakeDaedalus(detail)).execute(self.build(repo)).output
        self.assertIs(out["ok"], False)
        self.assertEqual(out["stage_failed"], "G2-tests")
        self.assertEqual(out["gate_reason"], "2 failing")

    def test_a_build_never_falls_back_to_the_blocking_solve(self) -> None:
        repo = self.make_sandbox()

        class Old(FakeDaedalus):
            def post(self, path, payload, *, timeout_seconds=None):
                if path == "/jobs":
                    raise HttpStatusError("not found", status=404)
                raise AssertionError(f"{path} must not be called")

        with self.assertRaises(AdapterUnavailable):
            self.adapter(Old()).execute(self.build(repo))


if __name__ == "__main__":
    unittest.main()
