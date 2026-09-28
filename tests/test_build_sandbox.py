"""The build sandbox: the contained user's setup record, its credential, and processes run
in a kill-on-close job object with their output in files.

Each test fails if the rule it names is reverted: a half-set-up sandbox taken as set up, a
credential that is not DPAPI for this account accepted, a process tree that outlives its
timeout or its job, a child that escapes the process limit, output through a pipe that can
hang, the owner's environment handed to generated code, or a build Daedalus that reaches
beyond the sandbox, keeps its state or worktrees outside it, or does not require its token.
The processes here run as THIS user (``logon=None``) - the job object is the same one the
``pionir-builds`` user's processes are put in.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace

from pionir import build_sandbox as bs
from pionir import sandbox_git

WINDOWS = os.name == "nt"
SID = "S-1-5-21-1-2-3-1009"


def pid_alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True,
                         text=True, check=False).stdout
    return str(pid) in out


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._t = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._t.name)

    def tearDown(self) -> None:
        self._t.cleanup()


@unittest.skipUnless(WINDOWS, "the sandbox is Windows")
class SetupRecordTests(_Case):
    def record(self, **over) -> Path:
        sandbox = self.root / "daedalus-work"
        sandbox.mkdir(exist_ok=True)
        src = self.root / "daedalus"
        (src / "daedalus").mkdir(parents=True, exist_ok=True)
        (src / "daedalus" / "server.py").write_text("", encoding="utf-8")
        doc = {"user": bs.USER, "sid": SID, "python": sys.executable,
               "sandbox_root": str(sandbox), "daedalus_src": str(src),
               "credential": str(self.root / "c.cred")}
        doc.update(over)
        path = self.root / f"setup-{time.monotonic_ns()}.json"     # one record per case
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path

    def load(self, record, *, sid=SID, read=lambda _p: "x" * 20, root=None):
        return bs.load_setup(root or self.root / "daedalus-work", record=record,
                             lookup=lambda _name: sid, read=read)

    def test_a_complete_setup_is_ready(self) -> None:
        setup, why = self.load(self.record())
        self.assertIsNone(why)
        self.assertEqual(setup.user, "pionir-builds")
        self.assertEqual(setup.state_dir, self.root / "daedalus-work" / ".daedalus-state")

    def test_every_missing_piece_is_not_configured(self) -> None:
        def bad_cred(_p):
            raise bs.SandboxError("CryptUnprotectData failed")

        cases = {
            "no record": dict(record=self.root / "none.json"),
            "no account": dict(record=self.record(), sid=None),
            "another account": dict(record=self.record(), sid="S-1-5-21-9"),
            "no interpreter": dict(record=self.record(python=str(self.root / "nope.exe"))),
            "another sandbox": dict(record=self.record(), root=self.root / "elsewhere"),
            "no daedalus": dict(record=self.record(daedalus_src=str(self.root / "x"))),
            "bad credential": dict(record=self.record(), read=bad_cred),
            "not our user": dict(record=self.record(user="someone")),
        }
        for name, kw in cases.items():
            setup, why = self.load(**kw)
            self.assertIsNone(setup, name)
            self.assertTrue(why.startswith(bs.SETUP_HINT), name)
        # and each case fails for ITS reason, not another's
        self.assertIn("account", self.load(record=self.record(), sid=None)[1])
        self.assertIn("credential", self.load(record=self.record(), read=bad_cred)[1])

    def test_the_credential_is_dpapi_for_this_account(self) -> None:
        cred = self.root / "c.cred"
        blob = bs.dpapi_protect("a-random-password-for-tests-0001".encode("utf-16-le"))
        cred.write_text(blob.hex(), encoding="ascii")
        self.assertEqual(bs.read_password(cred), "a-random-password-for-tests-0001")
        cred.write_text("not hex at all", encoding="ascii")
        with self.assertRaises(bs.SandboxError):
            bs.read_password(cred)
        cred.write_text((b"\x01\x00\x00\x00" + b"\x00" * 60).hex(), encoding="ascii")
        with self.assertRaises(bs.SandboxError):
            bs.read_password(cred)


@unittest.skipUnless(WINDOWS, "job objects are Windows")
class JobObjectTests(_Case):
    def env(self):
        return bs.minimal_env(work=self.root / "env",
                              path_dirs=[Path(sys.executable).parent, r"C:\Windows\System32"])

    def run_py(self, code: str, *, timeout: float = 30, limits=None):
        return bs.run([sys.executable, "-c", code], cwd=self.root, env=self.env(),
                      out_dir=self.root / f"out{time.monotonic_ns()}", timeout=timeout,
                      limits=limits or bs.JobLimits(), logon=None)

    def test_output_goes_to_files_and_a_flood_cannot_hang_it(self) -> None:
        # 5 MB to stdout: a pipe nobody reads would block the child for ever
        out = self.run_py("import sys\nsys.stdout.write('x' * 5_000_000)\nprint('END')")
        self.assertFalse(out.timed_out)
        self.assertEqual(out.returncode, 0)
        self.assertTrue(out.stdout.rstrip().endswith("END"))

    def test_a_timeout_kills_the_whole_tree(self) -> None:
        pids = self.root / "pids.txt"
        code = ("import subprocess, sys, time\n"
                "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
                f"open(r'{pids}', 'w').write(str(c.pid))\n"
                "time.sleep(120)\n")
        started = time.monotonic()
        out = self.run_py(code, timeout=4)
        self.assertTrue(out.timed_out)
        self.assertLess(time.monotonic() - started, 30)
        child = int(pids.read_text())
        time.sleep(0.5)
        self.assertFalse(pid_alive(child), "a grandchild outlived the timeout")

    def test_the_process_limit_holds(self) -> None:
        code = ("import subprocess, sys\nok = 0\n"
                "for _ in range(12):\n"
                "    try:\n"
                "        subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)'])\n"
                "        ok += 1\n"
                "    except OSError:\n"
                "        pass\n"
                "print('STARTED', ok)\n")
        out = self.run_py(code, limits=bs.JobLimits(active_processes=4))
        started = int(out.stdout.split("STARTED")[1].split()[0])
        self.assertLessEqual(started, 3)

    def test_the_memory_limit_holds(self) -> None:
        out = self.run_py("b = bytearray(400 * 1024 * 1024)\nprint('ALLOCATED')",
                          limits=bs.JobLimits(job_memory_mb=128))
        self.assertNotIn("ALLOCATED", out.stdout)
        self.assertNotEqual(out.returncode, 0)

    def test_closing_the_job_kills_everything_in_it(self) -> None:
        proc = bs.spawn([sys.executable, "-c", "import time; time.sleep(120)"], cwd=self.root,
                        env=self.env(), stdout=self.root / "o.txt", stderr=self.root / "e.txt",
                        limits=bs.JobLimits(), logon=None)
        self.assertTrue(proc.alive())
        proc.close()
        time.sleep(0.5)
        self.assertFalse(pid_alive(proc.pid))

    def test_letting_go_of_the_job_alone_kills_everything_in_it(self) -> None:
        # kill-on-close: if Pionir dies (its handles close), nothing it started survives
        proc = bs.spawn([sys.executable, "-c", "import time; time.sleep(120)"], cwd=self.root,
                        env=self.env(), stdout=self.root / "o2.txt",
                        stderr=self.root / "e2.txt", limits=bs.JobLimits(), logon=None)
        self.assertTrue(proc.alive())
        bs._k32.CloseHandle(proc._job)             # no TerminateJobObject: only the close
        proc._closed = True
        time.sleep(0.5)
        self.assertFalse(pid_alive(proc.pid))
        bs._k32.CloseHandle(proc._process)

    def test_the_child_sees_none_of_the_owners_environment(self) -> None:
        os.environ["PIONIR_TEST_SECRET_TOKEN"] = "must-not-leak"
        try:
            out = self.run_py("import os, json; print(json.dumps(dict(os.environ)))")
        finally:
            del os.environ["PIONIR_TEST_SECRET_TOKEN"]
        env = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertNotIn("PIONIR_TEST_SECRET_TOKEN", env)
        work = str(self.root / "env")
        for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP"):
            self.assertTrue(env[key].startswith(work), key)
        self.assertEqual(env["PATH"].split(os.pathsep)[0], str(Path(sys.executable).parent))


class FakeProc:
    def __init__(self) -> None:
        self.closed = False
        self.pid = 4242

    def alive(self) -> bool:
        return not self.closed

    def close(self) -> None:
        self.closed = True


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class BuildDaedalusTests(_Case):
    def setup(self):
        sandbox = self.root / "daedalus-work"
        sandbox.mkdir(exist_ok=True)
        python = Path(sys.executable)
        return SimpleNamespace(user=bs.USER, sandbox_root=sandbox, python=python,
                               python_dir=python.parent, daedalus_src=self.root / "daedalus",
                               state_dir=sandbox / bs.STATE_DIR_NAME,
                               runs_dir=sandbox / bs.RUNS_DIR_NAME,
                               logon=lambda: ("pionir-builds", ".", "pw"))

    def launcher(self, *, health=None, before=False, accepts_wrong=False, wrong_ok=False):
        setup = self.setup()
        state = {"up": before}
        spawned: list = []
        health = health or {"ok": True, "policy": "full",
                            "roots": [str(setup.sandbox_root.resolve())],
                            "state_dir": str(setup.state_dir)}

        def opener(req, timeout=None):
            if not state["up"]:
                raise urllib.error.URLError("refused")
            if req.full_url.endswith("/health"):
                return FakeResponse(json.dumps(health).encode())
            auth = req.headers.get("Authorization", "")
            if wrong_ok and auth != f"Bearer {spawned[0]['env']['DAEDALUS_TOKEN']}":
                return FakeResponse(b"{}")
            if auth == f"Bearer {spawned[0]['env']['DAEDALUS_TOKEN']}" or accepts_wrong:
                raise urllib.error.HTTPError(req.full_url, 404, "no job", {}, None)
            raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, None)

        def spawner(argv, **kw):
            spawned.append({"argv": argv, **kw})
            state["up"] = True
            return FakeProc()

        launcher = bs.BuildDaedalus(setup, spawner=spawner, opener=opener,
                                    sleep=lambda _s: None, git_dir=Path(r"C:\Git\cmd"))
        return launcher, spawned

    def test_it_runs_as_the_sandbox_user_confined_to_the_sandbox(self) -> None:
        launcher, spawned = self.launcher()
        token = launcher.start(not_after=time.time() + 3600)
        try:
            (call,) = spawned
            env = call["env"]
            sandbox = str(launcher.setup.sandbox_root)
            self.assertEqual(call["logon"], ("pionir-builds", ".", "pw"))
            self.assertEqual(call["argv"][1:], ["-m", "daedalus.server"])
            self.assertEqual(env["DAEDALUS_PORT"], "8772")
            self.assertEqual(env["DAEDALUS_TOKEN"], token)
            self.assertGreaterEqual(len(token), 40)
            self.assertEqual(env["DAEDALUS_WORKSPACE_ROOTS"], sandbox)
            self.assertEqual(env["DAEDALUS_POLICY"], "full")
            # its state, its temp folder (where Daedalus makes its worktrees), its home and
            # its git config all inside the sandbox
            for key in ("DAEDALUS_STATE_DIR", "TEMP", "TMP", "HOME", "USERPROFILE",
                        "APPDATA", "LOCALAPPDATA", "GIT_CONFIG_GLOBAL"):
                self.assertTrue(env[key].startswith(sandbox + os.sep), key)
            self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
            gitconfig = Path(env["GIT_CONFIG_GLOBAL"]).read_text(encoding="utf-8")
            self.assertIn("fsmonitor = false", gitconfig)
            self.assertIn(sandbox.replace("\\", "/") + "/*", gitconfig)
            self.assertNotIn("PIONIR", " ".join(env))           # none of the owner's settings
            self.assertEqual(env["PATH"].split(os.pathsep)[0], str(launcher.setup.python_dir))
            self.assertEqual(call["cwd"], launcher.setup.daedalus_src)
        finally:
            launcher.stop()
        self.assertIsNone(launcher.proc)

    def test_it_is_killed_when_its_window_ends_whatever_happens(self) -> None:
        launcher, spawned = self.launcher()
        launcher.kill_grace = 0.0
        launcher.start(not_after=time.time() - 10)           # the timer fires at once
        proc = launcher.proc
        for _ in range(50):
            if proc.closed:
                break
            time.sleep(0.05)
        self.assertTrue(proc.closed)

    def test_something_already_on_the_port_is_never_trusted(self) -> None:
        launcher, spawned = self.launcher(before=True)
        with self.assertRaises(bs.SandboxError):
            launcher.start(not_after=time.time() + 3600)
        self.assertEqual(spawned, [])

    def test_a_daedalus_reaching_beyond_the_sandbox_is_stopped(self) -> None:
        good = self.setup()
        roots = [str(good.sandbox_root.resolve())]
        state = str(good.state_dir)
        for bad, why in (({"roots": [r"C:\src"], "state_dir": state}, "reaches"),
                         ({"roots": [*roots, r"C:\src\Pionir"], "state_dir": state},
                          "reaches"),
                         ({"roots": roots, "state_dir": r"C:\Users\someone\.daedalus"},
                          "state outside"),
                         ({"roots": roots, "state_dir": state, "policy": "branch"},
                          "policy")):
            launcher, _spawned = self.launcher(health={"ok": True, "policy": "full", **bad})
            with self.assertRaises(bs.SandboxError) as caught:
                launcher.start(not_after=time.time() + 3600)
            self.assertIn(why, str(caught.exception))
            self.assertIsNone(launcher.proc)                  # killed, not left running

    def test_a_daedalus_that_does_not_require_its_token_is_stopped(self) -> None:
        for kw in ({"accepts_wrong": True}, {"wrong_ok": True}):
            launcher, _spawned = self.launcher(**kw)
            with self.assertRaises(bs.SandboxError) as caught:
                launcher.start(not_after=time.time() + 3600)
            self.assertIn("token", str(caught.exception))
            self.assertIsNone(launcher.proc)


class SandboxGitTests(_Case):
    def repo(self) -> Path:
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        return repo

    def test_the_config_is_rewritten_and_the_hooks_emptied(self) -> None:
        repo = self.repo()
        (repo / ".git" / "config").write_text(
            "[core]\n\tfsmonitor = calc.exe\n[include]\n\tpath = ../../evil\n"
            "[filter \"x\"]\n\tclean = calc.exe\n[remote \"origin\"]\n\turl = x\n",
            encoding="utf-8")
        (repo / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\nexit 1\n")
        (repo / ".git" / "config.worktree").write_text("[core]\n\tfsmonitor = calc.exe\n")
        sandbox_git.sanitize(repo)
        self.assertEqual((repo / ".git" / "config").read_text(encoding="utf-8"),
                         sandbox_git.CANONICAL)
        self.assertEqual(list((repo / ".git" / "hooks").iterdir()), [])
        self.assertFalse((repo / ".git" / "config.worktree").exists())

    def test_verification_catches_anything_but_the_canonical_config(self) -> None:
        repo = self.repo()
        sandbox_git.sanitize(repo)
        with (repo / ".git" / "config").open("a", encoding="utf-8") as f:
            f.write("[alias]\n\tst = !calc.exe\n")
        with self.assertRaises(sandbox_git.GitError):
            sandbox_git.verify(repo)

    def test_our_git_never_runs_the_repos_fsmonitor_or_hooks(self) -> None:
        repo = self.repo()
        flag = self.root / "RAN"
        cmd = f'"{sys.executable}" -c "open(r\'{flag}\', \'w\').write(\'x\')"'
        (repo / ".git" / "config").write_text(
            f"[core]\n\tfsmonitor = {cmd.replace(chr(92), chr(92) * 2)}\n", encoding="utf-8")
        (repo / "a.txt").write_text("a")
        sandbox_git.run_git(["status", "--porcelain"], repo)
        sandbox_git.run_git(["add", "-A"], repo)
        self.assertFalse(flag.exists())

    def test_every_call_is_made_the_safe_way(self) -> None:
        seen = {}

        def run(argv, **kw):
            seen.update(argv=argv, env=kw["env"])
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        sandbox_git.run_git(["status"], self.root, run=run)
        self.assertEqual(seen["argv"][1:5], list(sandbox_git.SAFE_ARGS))
        self.assertEqual(seen["env"]["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(seen["env"]["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertNotIn("USERPROFILE", seen["env"])


if __name__ == "__main__":
    unittest.main()
