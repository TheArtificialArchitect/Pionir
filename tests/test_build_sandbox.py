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
        python = self.root / "python" / "python.exe"      # the copy in the install folder
        python.parent.mkdir(exist_ok=True)
        python.write_bytes(b"MZ")
        doc = {"version": 3, "user": bs.USER, "sid": SID, "python": str(python),
               "sandbox_root": str(sandbox), "daedalus_src": str(src),
               "credential": str(self.root / "c.cred"),
               "low_integrity": True, "secrets_readable": 0}
        doc.update(over)
        self.n = getattr(self, "n", 0) + 1
        path = self.root / f"setup-{self.n}.json"                   # one record per case
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path

    def load(self, record, *, sid=SID, read=lambda _p: "x" * 20, root=None, low=None):
        return bs.load_setup(root or self.root / "daedalus-work", record=record,
                             lookup=lambda _name: sid, read=read,
                             low=low or (lambda _path: True))

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
            "an older setup": dict(record=self.record(version=1)),
            "a v2 setup (before the preflight)": dict(record=self.record(version=2)),
            "Low integrity never proven": dict(record=self.record(low_integrity=None)),
            "Low integrity proven false": dict(record=self.record(low_integrity=False)),
            "secrets readable": dict(record=self.record(secrets_readable=2)),
            "secrets never checked": dict(record=self.record(secrets_readable=None)),
            "interpreter outside the install": dict(record=self.record(python=sys.executable)),
            "daedalus left in C:\\src": dict(
                record=self.record(daedalus_src=r"C:\src\Tech-Support\daedalus")),
            "interpreter not Low": dict(record=self.record(),
                                        low=lambda path: "python" not in str(path)),
            "sandbox not Low": dict(record=self.record(),
                                    low=lambda path: "daedalus-work" not in str(path)),
        }
        for name, kw in cases.items():
            setup, why = self.load(**kw)
            self.assertIsNone(setup, name)
            self.assertTrue(why.startswith(bs.SETUP_HINT), name)
        # and each case fails for ITS reason, not another's
        self.assertIn("account", self.load(record=self.record(), sid=None)[1])
        self.assertIn("credential", self.load(record=self.record(), read=bad_cred)[1])
        self.assertIn("Low", self.load(record=self.record(),
                                        low=lambda path: "python" not in str(path))[1])
        self.assertIn("proven", self.load(record=self.record(low_integrity=False))[1])
        self.assertIn("secrets", self.load(record=self.record(secrets_readable=1))[1])

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

    def test_close_kills_the_tree_before_it_returns(self) -> None:
        proc = bs.spawn([sys.executable, "-c", "import time; time.sleep(120)"], cwd=self.root,
                        env=self.env(), stdout=self.root / "o3.txt",
                        stderr=self.root / "e3.txt", limits=bs.JobLimits(), logon=None)
        started = time.monotonic()
        proc.close()
        self.assertLess(time.monotonic() - started, 3.0)
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


class FakeGate:
    url = "http://127.0.0.1:8773"

    def __init__(self) -> None:
        self.running = False
        self.starts = 0

    def start(self) -> None:
        self.running = True
        self.starts += 1

    def stop(self) -> None:
        self.running = False


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class BuildDaedalusTests(_Case):
    def setup(self):
        sandbox = self.root / "daedalus-work"
        sid = SID
        sandbox.mkdir(exist_ok=True)
        python = Path(sys.executable)
        return SimpleNamespace(user=bs.USER, sid=sid, sandbox_root=sandbox, python=python,
                               python_dir=python.parent, daedalus_src=self.root / "daedalus",
                               state_dir=sandbox / bs.STATE_DIR_NAME,
                               runs_dir=sandbox / bs.RUNS_DIR_NAME,
                               logon=lambda: ("pionir-builds", ".", "pw"))

    def launcher(self, *, health=None, before=False, accepts_wrong=False, wrong_ok=False,
                 strays=None, uncontained=False):
        setup = self.setup()
        self.reaps = []

        def reap():
            self.reaps.append(time.monotonic())
            if strays:
                raise bs.SandboxError(f"processes of pionir-builds survived ({strays})")

        self.preflights = []

        def preflight():
            self.preflights.append(len(spawned))
            if uncontained:
                raise bs.SandboxError("the pionir-builds user is not contained - it runs at "
                                      "integrity 0x2000, not Low")
            return {"integrity": bs.LOW_RID, "readable": [], "listable": []}

        setup.reap = reap
        setup.preflight = preflight
        self.gate = FakeGate()
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
                                    sleep=lambda _s: None, git_dir=Path(r"C:\Git\cmd"),
                                    gate=self.gate)
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

    def test_it_reaches_ollama_only_through_the_gate_and_reaps_around_itself(self) -> None:
        launcher, spawned = self.launcher()
        launcher.start(not_after=time.time() + 3600)
        env = spawned[0]["env"]
        self.assertEqual(env["OLLAMA_HOST"], "http://127.0.0.1:8773")
        self.assertTrue(self.gate.running)
        self.assertEqual(len(self.reaps), 1)                  # before it started
        self.assertEqual(spawned[0]["sid"], launcher.setup.sid)
        self.assertEqual(env["GIT_CEILING_DIRECTORIES"], str(launcher.setup.sandbox_root))
        launcher.stop()
        self.assertFalse(self.gate.running)
        self.assertEqual(len(self.reaps), 2)                  # and after it was killed

    def test_nothing_starts_unless_the_preflight_proves_the_containment(self) -> None:
        launcher, spawned = self.launcher()
        launcher.start(not_after=time.time() + 3600)
        self.assertEqual(self.preflights, [0])               # once, before the spawn
        launcher.stop()
        launcher, spawned = self.launcher(uncontained=True)
        with self.assertRaises(bs.SandboxError) as caught:
            launcher.start(not_after=time.time() + 3600)
        self.assertIn("not Low", str(caught.exception))
        self.assertEqual(spawned, [])
        self.assertFalse(self.gate.running)

    def test_nothing_starts_while_a_stray_survives(self) -> None:
        launcher, spawned = self.launcher(strays="evil.exe")
        with self.assertRaises(bs.SandboxError):
            launcher.start(not_after=time.time() + 3600)
        self.assertEqual(spawned, [])
        self.assertFalse(self.gate.running)

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

    def test_verification_names_config_read_from_anywhere_else(self) -> None:
        repo = self.repo()
        (repo / ".git" / "other").write_text("[user]\n\tname = x\n", encoding="utf-8")
        sandbox_git.prepare(repo)
        with (repo / ".git" / "config").open("a", encoding="utf-8") as f:
            f.write("[include]\n\tpath = other\n")
        with self.assertRaises(sandbox_git.GitError) as caught:
            sandbox_git.verify(repo)
        self.assertIn("reads config from", str(caught.exception))

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
        self.assertEqual(seen["argv"][1:1 + len(sandbox_git.SAFE_ARGS)], list(sandbox_git.SAFE_ARGS))
        self.assertIn(f"core.attributesFile={os.devnull}", seen["argv"])
        self.assertEqual(seen["env"]["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(seen["env"]["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertNotIn("USERPROFILE", seen["env"])


@unittest.skipUnless(WINDOWS, "desktops are Windows")
class DesktopTests(_Case):
    def test_every_process_gets_every_ui_restriction_and_a_private_desktop(self) -> None:
        env = bs.minimal_env(work=self.root / "env",
                             path_dirs=[Path(sys.executable).parent, r"C:\Windows\System32"])
        proc = bs.spawn([sys.executable, "-c", "import time; time.sleep(5)"], cwd=self.root,
                        env=env, stdout=self.root / "o.txt", stderr=self.root / "e.txt",
                        limits=bs.JobLimits(), logon=None)
        try:
            self.assertEqual(bs.job_ui_restrictions(proc), bs.JOB_OBJECT_UILIMIT_ALL)
            self.assertIn("PionirBuilds-", proc._desktop.path)
            self.assertFalse(proc._desktop.path.lower().endswith("\\default"))
        finally:
            proc.close()

    def test_the_process_really_runs_on_the_private_desktop(self) -> None:
        env = bs.minimal_env(work=self.root / "env",
                             path_dirs=[Path(sys.executable).parent, r"C:\Windows\System32"])
        code = ("import ctypes\nfrom ctypes import wintypes as w\n"
                "u = ctypes.WinDLL('user32'); k = ctypes.WinDLL('kernel32')\n"
                "u.GetThreadDesktop.restype = w.HANDLE\n"
                "u.GetUserObjectInformationW.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p,"
                " w.DWORD, ctypes.POINTER(w.DWORD)]\n"
                "d = u.GetThreadDesktop(k.GetCurrentThreadId())\n"
                "buf = ctypes.create_unicode_buffer(256); n = w.DWORD()\n"
                "u.GetUserObjectInformationW(d, 2, buf, 512, ctypes.byref(n))\n"
                "print('DESKTOP', buf.value)\n")
        out = bs.run([sys.executable, "-c", code], cwd=self.root, env=env,
                     out_dir=self.root / "desk", timeout=30, limits=bs.JobLimits(), logon=None)
        name = out.stdout.split("DESKTOP", 1)[1].strip()
        self.assertTrue(name.startswith("PionirBuilds-"), name)

    def test_a_relative_program_is_refused(self) -> None:
        with self.assertRaises(bs.SandboxError):
            bs.spawn(["python", "-c", "1"], cwd=self.root, env={}, stdout=self.root / "o",
                     stderr=self.root / "e", limits=bs.JobLimits(), logon=None)


class ReapTests(_Case):
    def setup(self, *, listing: str, code: int = 0):
        calls = []

        def runner(argv, **kw):
            calls.append((argv, kw))
            if argv[0].endswith("tasklist.exe"):
                return bs.RunResult(code, False, listing, "")
            return bs.RunResult(0, False, "", "")

        setup = SimpleNamespace(user=bs.USER, sid=SID, runs_dir=self.root / ".runs",
                                logon=lambda: ("pionir-builds", ".", "pw"))
        return setup, runner, calls

    def test_everything_of_the_user_is_killed_as_that_user(self) -> None:
        setup, runner, calls = self.setup(
            listing='"tasklist.exe","42","Console","2","8,000 K"\n'
                    '"conhost.exe","43","Console","2","6,000 K"\n')
        self.assertEqual(bs.reap(setup, runner=runner), [])
        kill, listed = calls
        self.assertTrue(kill[0][0].lower().endswith("taskkill.exe"))
        self.assertIn("USERNAME eq pionir-builds", kill[0])
        self.assertIn("/F", kill[0])
        self.assertEqual(kill[1]["logon"], ("pionir-builds", ".", "pw"))
        self.assertEqual(listed[1]["logon"], ("pionir-builds", ".", "pw"))

    def test_a_survivor_is_reported_and_stops_everything(self) -> None:
        for listing, left in (
                ('"evil.exe","7","Console","2","1 K"\n"tasklist.exe","8","C","2","1 K"', ["evil.exe"]),
                ('"conhost.exe","7","C","2","1 K"\n"conhost.exe","9","C","2","1 K"', ["conhost.exe"]),
                ('"taskkill.exe","7","C","2","1 K"', ["taskkill.exe"])):
            setup, runner, _calls = self.setup(listing=listing)
            self.assertEqual(bs.reap(setup, runner=runner), left)
            with self.assertRaises(bs.SandboxError):
                bs.ensure_no_strays(setup, runner=runner)

    def test_a_check_that_fails_is_not_clean(self) -> None:
        setup, runner, _calls = self.setup(listing="", code=1)
        self.assertTrue(bs.reap(setup, runner=runner))


class PreflightReportTests(_Case):
    """What the preflight's report must say before anything of a build runs."""

    def run_with(self, stdout: str, *, code: int = 0, timed_out: bool = False):
        calls = []

        def runner(argv, **kw):
            calls.append((argv, kw))
            return bs.RunResult(None if timed_out else code, timed_out, stdout, "boom")

        secrets = self.root / "secrets"
        secrets.mkdir(exist_ok=True)
        (secrets / "daedalus-token.txt").write_text("t" * 43, encoding="utf-8")
        doc = bs.preflight(Path(r"C:\ProgramData\PionirBuilds\python\python.exe"),
                           work=self.root / "w", logon=("pionir-builds", ".", "pw"), sid=SID,
                           secrets_dir=secrets, runner=runner)
        return doc, calls

    def report(self, **over) -> str:
        body = {"integrity": bs.LOW_RID, "readable": [], "listable": []}
        body.update(over)
        return "noise\nPREFLIGHT " + json.dumps(body) + "\n"

    def test_a_low_process_that_reads_nothing_passes(self) -> None:
        doc, calls = self.run_with(self.report())
        self.assertEqual(bs.preflight_problems(doc), [])
        (argv, kw), = calls
        self.assertEqual(kw["logon"], ("pionir-builds", ".", "pw"))      # AS the user
        self.assertEqual(argv[1:4], ["-I", "-S", "-c"])
        asked = json.loads(argv[5])
        self.assertEqual([Path(f).name for f in asked["files"]], ["daedalus-token.txt"])
        self.assertIn(str(Path.home()), asked["dirs"])
        self.assertEqual(doc["files"], 1)

    def test_anything_short_of_proof_is_a_problem(self) -> None:
        cases = {
            "medium": self.report(integrity=0x2000),
            "untrusted is not low": self.report(integrity=0),
            "no integrity": self.report(integrity=None),
            "a secret read": self.report(readable=[r"C:\Users\x\.pionir\secrets\t.txt"]),
            "the profile listed": self.report(listable=[r"C:\Users\x"]),
            "no report": "nothing here\n",
            "not json": "PREFLIGHT {nope}\n",
        }
        for name, out in cases.items():
            doc, _calls = self.run_with(out)
            self.assertTrue(bs.preflight_problems(doc), name)
        for kw in ({"code": 1}, {"timed_out": True}):
            doc, _calls = self.run_with(self.report(), **kw)
            self.assertTrue(bs.preflight_problems(doc), kw)
        doc, _calls = self.run_with(self.report(), timed_out=True)
        self.assertIn("did not finish", doc["failed"])
        doc, _calls = self.run_with(self.report(integrity=0x2000))
        self.assertIn("not Low", " ".join(bs.preflight_problems(doc)))
        doc, _calls = self.run_with(self.report(readable=["a", "b"]))
        self.assertIn("read 2 of your secrets", " ".join(bs.preflight_problems(doc)))

    def test_require_preflight_raises_unless_proven(self) -> None:
        def runner(argv, **kw):
            return bs.RunResult(0, False, self.report(integrity=0x2000), "")
        with self.assertRaises(bs.SandboxError):
            bs.require_preflight("python.exe", work=self.root / "w", logon=None, sid=None,
                                 secrets_dir=self.root, runner=runner)


@unittest.skipUnless(WINDOWS, "integrity levels are Windows")
class LowIntegrityProbeTests(_Case):
    """The non-admin probe: a copy of this interpreter, labelled Low the way setup labels
    the dedicated one, started through ``spawn`` as THIS user (CreateProcessW - the
    CreateProcessWithLogonW path needs the pionir-builds account, so setup proves that one).
    Only temporary files are touched."""

    def interpreter(self, *, low: bool) -> Path:
        import shutil
        base = Path(sys.base_prefix)
        target = self.root / ("low" if low else "medium")
        target.mkdir()
        for name in ("python.exe", f"python{sys.version_info[0]}{sys.version_info[1]}.dll",
                     "python3.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
            if (base / name).exists():
                shutil.copy2(base / name, target / name)
        (target / "pyvenv.cfg").write_text(f"home = {base}\n", encoding="utf-8")
        exe = target / "python.exe"
        if low:
            done = subprocess.run(["icacls", str(exe), "/setintegritylevel", "low"],
                                  capture_output=True, text=True, check=False)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertTrue(bs.is_low_labelled(exe))
        return exe

    def secrets(self) -> Path:
        folder = self.root / "secrets"
        folder.mkdir()
        (folder / "a-token.txt").write_text("t" * 43, encoding="utf-8")
        return folder

    def test_a_low_labelled_interpreter_runs_at_low_integrity(self) -> None:
        doc = bs.preflight(self.interpreter(low=True), work=self.root / "w", logon=None,
                           sid=None, secrets_dir=self.secrets())
        self.assertEqual(doc.get("integrity"), bs.LOW_RID, doc)
        # Low alone does not hide a file: its DACL does (no-read-up is not on for files),
        # which is why the preflight reads every secret as the sandbox user
        self.assertEqual(len(doc["readable"]), 1)
        self.assertTrue(any("not Low" in p or "secrets" in p
                            for p in bs.preflight_problems(doc)))

    def test_an_unlabelled_interpreter_is_not_low_and_is_refused(self) -> None:
        doc = bs.preflight(self.interpreter(low=False), work=self.root / "w", logon=None,
                           sid=None, secrets_dir=self.secrets())
        self.assertGreaterEqual(doc.get("integrity") or 0, 0x2000, doc)
        self.assertIn("not Low", " ".join(bs.preflight_problems(doc)))


class OllamaGateTests(_Case):
    """The gate in front of Ollama: only the build model's chat and the model listing."""

    def setUp(self) -> None:
        super().setUp()
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.seen = []
        seen = self.seen

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self):
                n = int(self.headers.get("Content-Length") or 0)
                seen.append((self.command, self.path, self.rfile.read(n)))
                data = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_DELETE = _answer

        self.up = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        import threading
        threading.Thread(target=self.up.serve_forever, daemon=True).start()
        self.gate = bs.OllamaGate("qwen3-coder:30b",
                                  upstream=f"http://127.0.0.1:{self.up.server_port}", port=0)
        self.gate.port = 0
        self.gate.start()
        self.gate.port = self.gate._server.server_port

    def tearDown(self) -> None:
        self.gate.stop()
        self.up.shutdown()
        self.up.server_close()
        super().tearDown()

    def call(self, method, path, body=None):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.gate.port, timeout=10)
        data = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=data, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        out = resp.status, resp.read()
        conn.close()
        return out

    def test_the_build_models_chat_and_the_listing_pass(self) -> None:
        self.assertEqual(self.call("POST", "/api/chat", {"model": "qwen3-coder:30b",
                                                         "messages": []})[0], 200)
        self.assertEqual(self.call("POST", "/api/show", {"name": "qwen3-coder:30b"})[0], 200)
        self.assertEqual(self.call("GET", "/api/tags")[0], 200)
        self.assertEqual([m for m, _p, _b in self.seen], ["POST", "POST", "GET"])

    def test_everything_else_is_refused_and_never_reaches_ollama(self) -> None:
        for method, path, body in (
                ("POST", "/api/pull", {"model": "qwen3-coder:30b"}),
                ("DELETE", "/api/delete", {"model": "gemma3:12b"}),
                ("POST", "/api/create", {"model": "qwen3-coder:30b"}),
                ("POST", "/api/copy", {"source": "a", "destination": "b"}),
                ("POST", "/api/push", {"model": "qwen3-coder:30b"}),
                ("POST", "/api/chat", {"model": "gemma3:12b", "messages": []}),
                ("POST", "/api/generate", {"prompt": "no model named"}),
                ("GET", "/api/blobs/sha256:00", None)):
            status, _ = self.call(method, path, body)
            self.assertEqual(status, 403, path)
        self.assertEqual(self.seen, [])


if __name__ == "__main__":
    unittest.main()
