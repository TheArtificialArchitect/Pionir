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

import io
import json
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
from support import use_long_tempdir

use_long_tempdir()

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


class FakeLauncher:
    """The build Daedalus's launcher: records starts and stops; ``fail`` makes it not come
    up. The job client is the FakeDaedalus the test hands in."""

    base_url = "http://127.0.0.1:8772"

    def __init__(self, *, fail: str | None = None) -> None:
        self.fail = fail
        self.starts: list = []
        self.stops = 0
        self.running = False
        self.stopped_while_polled = None

    def start(self, *, not_after):
        self.starts.append(not_after)
        if self.fail:
            raise RuntimeError(self.fail)
        self.running = True
        return "per-launch-token"

    def stop(self) -> None:
        self.stops += 1
        self.running = False


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

    def adapter(self, client, *, clock=None, wall=T0, launcher=None, configured=True,
                sleep=None, owner_open=False, compat=False, galatea=False,
                **settings) -> DaedalusAdapter:
        clock = clock or Clock()
        self.launcher = launcher or FakeLauncher()
        self.tokens: list = []

        def build_client(base, token):
            self.tokens.append((base, token))
            return client

        return DaedalusAdapter(DaedalusSettings(sandbox_root=str(self.root),
                                                poll_interval_seconds=1.0, **settings),
                               client=client, sleep=sleep or (lambda _s: None),
                               monotonic=clock.mono, clock=lambda: wall,
                               setup=lambda: (object(), None) if configured else (
                                   None, r"not configured: run tools\setup-build-sandbox.ps1"),
                               launcher=lambda _setup: self.launcher,
                               build_client=build_client,
                               owner_open=lambda: owner_open, auth_compat=lambda: compat,
                               galatea_open=lambda: galatea)

    def build(self, repo, **over) -> Task:
        payload = {"intent": "build the product in BRIEF.md", "repo": str(repo),
                   "budget_seconds": 2700, "not_after": T0 + 2700,
                   "build_id": "cron-explain-1-abcd1234"}
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

    @unittest.skipUnless(os.name == "nt", "drive letters are Windows")
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

    def test_a_work_tree_redirect_is_refused(self) -> None:
        repo = self.make_sandbox()
        with (repo / ".git" / "config").open("a", encoding="utf-8") as f:
            f.write("\tworktree = C:/src/Pionir\n")
        self.assertIn("core.worktree", sandbox_repo_problem(str(repo), str(self.root)))

    def test_a_path_that_would_expand_is_refused_for_that_reason(self) -> None:
        for bad in ("~\\cron-explain", str(self.root) + "\\%USERNAME%",
                    str(self.root) + "\\$x"):
            self.assertIn("expand", sandbox_repo_problem(bad, str(self.root)), bad)

    def test_a_repo_that_resolves_elsewhere_is_refused_even_past_the_link_check(self) -> None:
        # defence in depth: were a link to slip past the reparse check, the path must still
        # resolve to itself
        from unittest import mock
        if os.name != "nt":
            self.skipTest("junctions are Windows")
        import _winapi
        live = self.tmp / "live-repo"
        subprocess.run(["git", "init", "-q", str(live)], check=True)
        _winapi.CreateJunction(str(live), str(self.root / "linked-repo"))
        with mock.patch("pionir.adapters.daedalus.is_reparse", return_value=False):
            problem = sandbox_repo_problem(str(self.root / "linked-repo"), str(self.root))
        self.assertIn("resolves somewhere else", problem)

    def test_a_marker_for_another_repo_is_refused(self) -> None:
        repo = self.make_sandbox()
        (repo / ".git" / SANDBOX_MARKER).write_text('{"slug": "csv-to-ics"}', encoding="utf-8")
        self.assertIn("does not name this repo", sandbox_repo_problem(str(repo), str(self.root)))

    def test_a_repo_sharing_another_repos_objects_is_refused(self) -> None:
        repo = self.make_sandbox()
        (repo / ".git" / "commondir").write_text(r"C:\src\Pionir\.git", encoding="utf-8")
        self.assertIn("linked worktree", sandbox_repo_problem(str(repo), str(self.root)))

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
        with self.assertRaises(AdapterTimeout):
            adapter.execute(self.build(repo, budget_seconds=120))
        calls = [(c[0], c[1]) for c in client.calls]
        cancel = calls.index(("POST", "/jobs/job-7/cancel"))
        self.assertEqual(calls[cancel + 1], ("GET", "/jobs/job-7"))   # waited for the stop
        self.assertEqual(self.launcher.stops, 1)                      # then killed it

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


class Flaky(FakeDaedalus):
    """Polls that fail ``failures`` times (Daedalus restarting, a dropped socket), or raise
    something unexpected once (``boom``), before the job finishes."""

    def __init__(self, *, failures=0, boom=False, on_poll=None, **kw) -> None:
        super().__init__(**kw)
        self.failures = failures
        self.boom = boom
        self.on_poll = on_poll

    def get(self, path, *, timeout_seconds=None):
        if self.on_poll is not None:
            self.on_poll()
        if self.boom and not self.cancelled:
            self.boom = False
            raise RuntimeError("something unexpected")
        if self.failures > 0 and not self.cancelled:
            self.failures -= 1
            self.calls.append(("GET", path, None, timeout_seconds))
            raise AdapterUnavailable("connection refused")
        return super().get(path, timeout_seconds=timeout_seconds)


class ContainmentTests(_Case):
    """The build runs on its OWN Daedalus, started as the sandbox user and killed after."""

    def test_without_the_sandbox_user_nothing_is_started(self) -> None:
        repo = self.make_sandbox()
        client = FakeDaedalus()
        out = self.adapter(client, configured=False).execute(self.build(repo)).output
        self.assertIs(out["ok"], False)
        self.assertTrue(out["not_configured"])
        self.assertIn("setup-build-sandbox", out["refused"])
        self.assertEqual(self.launcher.starts, [])
        self.assertEqual(client.calls, [])

    def test_no_build_while_the_owners_daedalus_takes_tokenless_jobs(self) -> None:
        repo = self.make_sandbox()
        client = FakeDaedalus()
        out = self.adapter(client, owner_open=True).execute(self.build(repo)).output
        self.assertTrue(out["not_configured"])
        self.assertIn("without a token", out["refused"])
        self.assertEqual(self.launcher.starts, [])

    def test_no_build_while_auth_compat_is_on(self) -> None:
        repo = self.make_sandbox()
        out = self.adapter(FakeDaedalus(), compat=True).execute(self.build(repo)).output
        self.assertTrue(out["not_configured"])
        self.assertIn("PIONIR_AUTH_COMPAT=off", out["refused"])
        self.assertEqual(self.launcher.starts, [])

    def test_no_build_while_galatea_serves_a_tokenless_loopback_caller(self) -> None:
        repo = self.make_sandbox()
        out = self.adapter(FakeDaedalus(), galatea=True).execute(self.build(repo)).output
        self.assertTrue(out["not_configured"])
        self.assertIn("Galatea", out["refused"])
        self.assertEqual(self.launcher.starts, [])

    def test_galatea_is_open_unless_she_refuses_a_tokenless_caller(self) -> None:
        import urllib.error
        from pionir.adapters.daedalus import galatea_open

        def answers(code):
            def opener(req, timeout=None):
                self.assertNotIn("Authorization", req.headers)
                self.assertNotIn("X-galatea-token", req.headers)
                self.assertEqual(req.get_method(), "GET")
                if code is None:
                    raise urllib.error.URLError("refused")
                if code == 200:
                    return io.BytesIO(b"{}")
                raise urllib.error.HTTPError(req.full_url, code, "x", {}, None)
            return opener

        url = "http://127.0.0.1:65001"
        self.assertFalse(galatea_open(url, opener=answers(401)))
        self.assertFalse(galatea_open(url, opener=answers(403)))
        self.assertFalse(galatea_open(url, opener=answers(None)))
        for code in (200, 404, 500):
            self.assertTrue(galatea_open(url, opener=answers(code)), code)

    def test_the_owners_daedalus_is_open_unless_it_says_401(self) -> None:
        import urllib.error
        from pionir.adapters.daedalus import owner_daedalus_open

        def answers(code):
            def opener(req, timeout=None):
                self.assertNotIn("Authorization", req.headers)
                if code is None:
                    raise urllib.error.URLError("refused")
                if code == 200:
                    return io.BytesIO(b"{}")
                raise urllib.error.HTTPError(req.full_url, code, "x", {}, None)
            return opener

        self.assertFalse(owner_daedalus_open("http://127.0.0.1:8771", opener=answers(401)))
        self.assertFalse(owner_daedalus_open("http://127.0.0.1:8771", opener=answers(None)))
        for code in (404, 200, 422, 500):
            self.assertTrue(owner_daedalus_open("http://127.0.0.1:8771", opener=answers(code)))

    def test_the_build_uses_its_own_daedalus_and_token_then_kills_it(self) -> None:
        repo = self.make_sandbox()
        solve_side = FakeDaedalus()
        build_side = FakeDaedalus({"state": "done", "result": {"ok": True}})
        adapter = self.adapter(solve_side)
        adapter._build_client = lambda base, token: (self.tokens.append((base, token))
                                                     or build_side)
        adapter.execute(self.build(repo))
        self.assertEqual(solve_side.calls, [])              # the owner's Daedalus: untouched
        self.assertEqual(self.tokens, [("http://127.0.0.1:8772", "per-launch-token")])
        self.assertEqual(self.launcher.starts, [T0 + 2700])  # killed at the window's end
        self.assertEqual(self.launcher.stops, 1)
        self.assertFalse(self.launcher.running)

    def test_a_daedalus_that_will_not_start_is_waiting_and_killed(self) -> None:
        repo = self.make_sandbox()
        adapter = self.adapter(FakeDaedalus(), launcher=FakeLauncher(fail="port taken"))
        with self.assertRaises(AdapterUnavailable):
            adapter.execute(self.build(repo))
        self.assertEqual(self.launcher.stops, 1)

    def test_the_repos_git_config_is_rewritten_before_the_job(self) -> None:
        from pionir import sandbox_git
        repo = self.make_sandbox()
        (repo / ".git" / "config").write_text(
            "[core]\n\tfsmonitor = calc.exe\n[include]\n\tpath = ../evil\n", encoding="utf-8")
        (repo / ".git" / "hooks" / "post-commit").write_text("#!/bin/sh\n", encoding="utf-8")
        adapter = self.adapter(FakeDaedalus())
        adapter._sanitize = sandbox_git.sanitize
        adapter.execute(self.build(repo))
        self.assertEqual((repo / ".git" / "config").read_text(encoding="utf-8"),
                         sandbox_git.CANONICAL)
        self.assertEqual(list((repo / ".git" / "hooks").iterdir()), [])

    def test_a_repo_that_cannot_be_made_canonical_is_refused(self) -> None:
        repo = self.make_sandbox()
        adapter = self.adapter(FakeDaedalus())

        def refuse(_repo):
            raise RuntimeError("git reads config from 'file:C:/evil'")

        adapter._sanitize = refuse
        with self.assertRaises(AdapterProtocolError):
            adapter.execute(self.build(repo))
        self.assertEqual(self.launcher.starts, [])

    def test_a_few_failed_polls_are_tolerated(self) -> None:
        repo = self.make_sandbox()
        client = Flaky(failures=3, detail={"state": "done", "result": {"ok": True}})
        out = self.adapter(client).execute(self.build(repo)).output
        self.assertIs(out["ok"], True)
        self.assertFalse(client.cancelled)

    def test_daedalus_gone_for_good_is_cancelled_waited_on_and_killed(self) -> None:
        repo = self.make_sandbox()
        client = Flaky(failures=100)
        with self.assertRaises(AdapterUnavailable):
            self.adapter(client).execute(self.build(repo))
        self.assertTrue(client.cancelled)
        self.assertEqual(self.launcher.stops, 1)

    def test_any_error_while_waiting_cancels_and_kills(self) -> None:
        repo = self.make_sandbox()
        client = Flaky(boom=True, running_polls=5)
        with self.assertRaises(RuntimeError):
            self.adapter(client).execute(self.build(repo))
        self.assertTrue(client.cancelled)
        self.assertEqual(self.launcher.stops, 1)

    def test_the_lease_is_never_released_before_the_daedalus_is_killed(self) -> None:
        # execute() holds the lease; the kill must have happened before it returns
        repo = self.make_sandbox()
        adapter = self.adapter(FakeDaedalus(forever=True), clock=Clock(step=100.0))
        with self.assertRaises(AdapterTimeout):
            adapter.execute(self.build(repo, budget_seconds=120))
        self.assertEqual(self.launcher.stops, 1)
        self.assertFalse(self.launcher.running)

    def test_the_job_in_flight_is_on_disk_and_a_restart_cancels_a_solve_it_left(self) -> None:
        repo = self.make_sandbox()
        ledger = self.tmp / "jobs.json"
        seen = []
        client = Flaky(on_poll=lambda: seen.append(json.loads(ledger.read_text())["jobs"]),
                       detail={"state": "done", "result": {"ok": True}})
        adapter = self.adapter(client, state_file=str(ledger))
        adapter.execute(self.build(repo))
        self.assertEqual(seen[0]["build:cron-explain-1-abcd1234"]["job_id"], "job-7")
        self.assertEqual(json.loads(ledger.read_text())["jobs"], {})
        # a previous Pionir died with a solve in flight: the next start cancels it first
        ledger.write_text(json.dumps({"jobs": {"solve:old-9": {"kind": "solve",
                                                                "job_id": "old-9"}},
                                      "cancelled": []}))
        owner = FakeDaedalus()
        fresh = self.adapter(owner, state_file=str(ledger))
        fresh.execute(self.build(repo, build_id="cron-explain-2-abcd1234"))
        self.assertIn(("POST", "/jobs/old-9/cancel"), [(c[0], c[1]) for c in owner.calls])
        self.assertEqual(json.loads(ledger.read_text())["jobs"], {})

    def test_a_cancelled_build_never_starts(self) -> None:
        from pionir.adapters.daedalus import CANCEL
        repo = self.make_sandbox()
        client = FakeDaedalus()
        adapter = self.adapter(client)
        adapter.execute(Task(CANCEL, {"build_id": "cron-explain-1-abcd1234"}))
        out = adapter.execute(self.build(repo)).output
        self.assertIs(out["started"], False)
        self.assertEqual(self.launcher.starts, [])
        self.assertEqual(client.calls, [])

    def test_a_running_build_is_cancelled_and_killed(self) -> None:
        from pionir.adapters.daedalus import CANCEL
        repo = self.make_sandbox()
        holder = {}

        def cancel_from_outside():
            if not holder.get("done"):
                holder["done"] = True
                holder["adapter"].execute(Task(CANCEL, {"build_id": "cron-explain-1-abcd1234"}))

        client = Flaky(on_poll=cancel_from_outside, running_polls=3)
        holder["adapter"] = self.adapter(client)
        holder["adapter"].execute(self.build(repo))
        self.assertTrue(client.cancelled)
        self.assertGreaterEqual(self.launcher.stops, 1)

    def test_a_build_without_its_window_or_id_is_refused(self) -> None:
        repo = self.make_sandbox()
        adapter = self.adapter(FakeDaedalus())
        for over in ({"not_after": None}, {"build_id": None}, {"build_id": "BAD ID"}):
            payload = {k: v for k, v in self.build(repo, **over).payload.items()
                       if v is not None}
            with self.assertRaises(AdapterProtocolError, msg=str(over)):
                adapter.validate(Task(BUILD, payload))


class CrewGrantTests(unittest.TestCase):
    """Pionir itself: the crew's grant runs a sandbox build unparked, and nothing else."""

    def test_only_the_crew_and_only_the_build_skip_parking(self) -> None:
        from pionir.bootstrap import build_runtime
        from pionir.config import PionirSettings
        from pionir.server import PionirApp
        from standins import down_url
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp) / "daedalus-work"
            root.mkdir()
            sandbox.create(root, entry(), year=2026, created_at=T0)
            runtime = build_runtime(PionirSettings(
                state_root=Path(tmp), atani_command=("pionir-test-no-such-binary",),
                galatea_url=None, embed_model=None, daedalus_url=down_url(),
                melete_url=None, crew_url=None, bryo_status_command=None,
                nyx_status_command=None, voodoo_status_command=None, evict_to_fit=False,
                daedalus_sandbox_root=str(root)))
            # never the real sandbox user from a test, whatever this machine has set up
            runtime.adapters["daedalus"]._setup = lambda: (None, "not configured (test)")
            app = PionirApp(runtime)
            try:
                payload = {"intent": "build it", "repo": str(root / "cron-explain"),
                           "budget_seconds": 2700, "not_after": 4_000_000_000.0,
                           "build_id": "cron-explain-1-abcd1234"}
                # the crew's grant unlocks the build (it would run, not park) - checked
                # without running it: a run would take the real GPU lease
                crew = sorted(app.auth.grant("crew").permissions_for(BUILD))
                self.assertEqual(crew, [BUILD_PERMISSION])
                self.assertFalse(app._needs_approval(BUILD, crew))
                self.assertEqual(sorted(app.auth.grant("crew").permissions_for(
                    "coding.daedalus_solve")), [])
                for client in ("galatea", "atani", "dashboard"):
                    parked = app.run_task(BUILD, payload, client=client, wait=30)
                    self.assertEqual(parked["status"], "pending_approval", client)
                solve = app.run_task("coding.daedalus_solve",
                                     {"content": "x", "repo": str(root / "cron-explain")},
                                     client="crew", wait=30)
                self.assertEqual(solve["status"], "pending_approval")
            finally:
                runtime.cortex.close()


if __name__ == "__main__":
    unittest.main()
