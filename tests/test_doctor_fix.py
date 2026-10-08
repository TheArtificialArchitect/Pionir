"""``pionir doctor --fix``: safe, reversible fixes applied; everything else a command for Ian.

Real files in temp folders, an injected process list (what CIM would return) and an injected
launcher: no live lock, pid file, pane or process of the stack is read or touched. Each test
fails if its rule is reverted: a lock removed while git runs (or while it is young, or not
empty), a fix applied without --fix, a pid file of a LIVE Bryo moved, a dead pane "fixed"
beside Pionir Desktop or without a port that answers again, or an admin step done instead
of printed.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pionir import doctor_fix as df
from pionir import ports
from pionir.cli import _parser as build_parser

NOW = 1_800_000_000.0
OLD = NOW - 3600


def row(sid: str, port: int, state: str) -> dict:
    return {"id": sid, "label": sid.title(), "port": port, "state": state}


UP = ports.OURS_HEALTHY
STACK = [row("dashboard", 8780, UP), row("galatea", 8799, UP), row("daedalus", 8771, UP),
         row("melete", 8770, UP), row("crew", 8782, UP), row("peter", 8790, UP)]
PROCS = [{"pid": 10, "name": "powershell.exe", "command": "powershell -EncodedCommand x"},
         {"pid": 11, "name": "python.exe", "command": "python -m pionir.crew"}]


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._t = tempfile.TemporaryDirectory()
        self.root = Path(self._t.name)
        self.repo = self.root / "repo"
        (self.repo / ".git" / "worktrees" / "agent").mkdir(parents=True)
        self.state = self.root / "state"
        for d in ("logs", "crew", "builds", "products"):
            (self.state / d).mkdir(parents=True)
        self.terrarium = self.root / "terrarium"
        self.marker = self.root / "Pionir" / "stopping"

    def tearDown(self) -> None:
        self._t.cleanup()

    def stale(self, path: Path, text: str = "", age: float = 3600) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        os.utime(path, (NOW - age, NOW - age))
        return path

    def run_fix(self, apply=True, procs=PROCS, rows=STACK, **kw) -> dict:
        return df.run(apply=apply, state_root=self.state, rows=rows, bridge_report={},
                      procs=procs, repos=[self.repo], terrarium=self.terrarium,
                      marker=self.marker, now=NOW, **kw)

    @staticmethod
    def find(report, check) -> list:
        return [f for f in report["findings"] if f["check"] == check]


class GitLockTests(_Case):
    def test_a_stale_empty_lock_is_moved_aside_with_its_undo(self) -> None:
        lock = self.stale(self.repo / ".git" / "index.lock")
        wt = self.stale(self.repo / ".git" / "worktrees" / "agent" / "index.lock")
        report = self.run_fix()
        self.assertFalse(lock.exists())
        self.assertFalse(wt.exists())
        aside = list((self.repo / ".git").glob("index.lock.stale-*"))
        self.assertEqual(len(aside), 1)                       # moved, never deleted
        f = self.find(report, "git_lock")
        self.assertEqual([x["status"] for x in f], [df.FIXED, df.FIXED])
        self.assertIn("Move-Item", f[0]["undo"])

    def test_without_fix_nothing_is_touched(self) -> None:
        lock = self.stale(self.repo / ".git" / "index.lock")
        report = self.run_fix(apply=False)
        self.assertTrue(lock.exists())
        self.assertEqual(self.find(report, "git_lock")[0]["status"], df.WOULD_FIX)

    def test_never_while_git_runs_while_young_or_when_not_empty(self) -> None:
        lock = self.stale(self.repo / ".git" / "index.lock")
        git = PROCS + [{"pid": 20, "name": "git.exe", "command": "git.exe commit -m x"}]
        self.assertEqual(self.find(self.run_fix(procs=git), "git_lock")[0]["status"],
                         df.SKIPPED)
        self.assertEqual(self.find(self.run_fix(procs=None), "git_lock")[0]["status"],
                         df.SKIPPED)                          # unknown is not "no git"
        self.assertTrue(lock.exists())
        self.stale(lock, age=60)                              # one minute old
        self.assertEqual(self.find(self.run_fix(), "git_lock")[0]["status"], df.SKIPPED)
        self.stale(lock, text="half-written index")
        f = self.find(self.run_fix(), "git_lock")[0]
        self.assertEqual(f["status"], df.FOR_IAN)
        self.assertIn("git status", f["command"])
        self.assertTrue(lock.exists())


class BryoPidTests(_Case):
    def test_a_pid_file_of_a_gone_or_recycled_process_is_moved_aside(self) -> None:
        pid = self.stale(self.terrarium / "state" / "bryo.pid", "4242")
        report = self.run_fix()
        self.assertFalse(pid.exists())
        self.assertIn("names no process", self.find(report, "bryo_pid")[0]["detail"])
        self.stale(pid, "11")                                 # pid 11 is the crew, not Bryo
        report = self.run_fix()
        self.assertFalse(pid.exists())
        self.assertIn("recycled", self.find(report, "bryo_pid")[0]["detail"])

    def test_a_live_bryo_keeps_his_pid_file(self) -> None:
        pid = self.stale(self.terrarium / "state" / "bryo.pid", "30")
        live = PROCS + [{"pid": 30, "name": "python.exe", "command": "python -m bryo"}]
        self.assertEqual(self.find(self.run_fix(procs=live), "bryo_pid"), [])
        self.assertTrue(pid.exists())


class MarkerAndDirTests(_Case):
    def test_a_stop_marker_left_behind_is_removed_but_not_during_a_stop(self) -> None:
        self.stale(self.marker, "2026-10-07")
        stopping = PROCS + [{"pid": 40, "name": "powershell.exe",
                             "command": "powershell -File C:\\src\\Pionir\\pionir.ps1 -Stop"}]
        self.assertEqual(self.find(self.run_fix(procs=stopping), "stop_marker")[0]["status"],
                         df.SKIPPED)
        self.assertTrue(self.marker.exists())
        self.assertEqual(self.find(self.run_fix(), "stop_marker")[0]["status"], df.FIXED)
        self.assertFalse(self.marker.exists())

    def test_missing_state_folders_are_created(self) -> None:
        (self.state / "logs").rmdir()
        report = self.run_fix()
        self.assertTrue((self.state / "logs").is_dir())
        self.assertEqual(self.find(report, "missing_dir")[0]["status"], df.FIXED)


class DeadPaneTests(_Case):
    def dead(self, *ids) -> list:
        return [row(r["id"], r["port"], ports.FREE) if r["id"] in ids else r for r in STACK]

    def test_a_dead_pane_is_restarted_by_the_launcher_for_only_that_group(self) -> None:
        launched: list = []
        audits = iter([self.dead("crew"), STACK])
        report = self.run_fix(rows=self.dead("crew"),
                              launch=lambda args: launched.append(args) or 0,
                              reaudit=lambda: next(audits))
        self.assertEqual(len(launched), 1)
        args = launched[0]
        self.assertNotIn("-NoCrew", args)                     # the dead one is started
        for flag in ("-NoVoice", "-NoSpecialists", "-NoPeter", "-NoBryo", "-NoTunnel",
                     "-NoBrowser"):
            self.assertIn(flag, args)                         # nothing else is
        f = self.find(report, "dead_pane")[0]
        self.assertEqual(f["status"], df.FIXED)
        self.assertIn("answering on its port", f["detail"])

    def test_a_restart_that_never_answers_on_its_port_is_a_failure(self) -> None:
        clock = iter(range(0, 1000, 50))
        report = df.check_panes(self.dead("galatea"), PROCS, apply=True,
                                launch=lambda args: 0, reaudit=lambda: self.dead("galatea"),
                                sleep=lambda s: None, clock=lambda: next(clock))
        self.assertEqual(report[0].status, df.FAILED)
        self.assertIn("pionir.ps1", report[0].command)

    def test_never_beside_desktop_never_a_whole_stack_never_without_fix(self) -> None:
        launched: list = []
        desktop = PROCS + [{"pid": 50, "name": "Pionir Desktop.exe",
                            "command": "C:\\Users\\Ian\\AppData\\Local\\Programs\\pionir-desktop"
                                       "\\Pionir Desktop.exe"}]
        f = self.find(self.run_fix(rows=self.dead("crew"), procs=desktop,
                                   launch=launched.append), "dead_pane")[0]
        self.assertEqual(f["status"], df.FOR_IAN)
        self.assertIn("Desktop", f["detail"])
        f = self.find(self.run_fix(rows=self.dead(*[r["id"] for r in STACK]),
                                   launch=launched.append), "dead_pane")[0]
        self.assertEqual(f["status"], df.FOR_IAN)
        self.assertTrue(f["command"].startswith("cd "))
        f = self.find(self.run_fix(apply=False, rows=self.dead("crew"),
                                   launch=launched.append), "dead_pane")[0]
        self.assertEqual(f["status"], df.WOULD_FIX)
        self.assertEqual(launched, [])


class ForIanTests(_Case):
    def test_admin_and_decisions_are_printed_with_the_cd_never_done(self) -> None:
        rows = STACK + [{"id": "peter", "label": "Peter", "port": 8790, "state": ports.FOREIGN,
                         "pid": 77, "detail": "held by node.exe (pid 77)"}]
        report = df.run(apply=True, state_root=self.state, rows=rows,
                        bridge_report={"daedalus": {"warning": "no token"}}, procs=PROCS,
                        repos=[self.repo], terrarium=self.terrarium, marker=self.marker,
                        now=NOW, sandbox_why=lambda: "not configured: run "
                                                     "tools\\setup-build-sandbox.ps1")
        sandbox = self.find(report, "build_sandbox")[0]
        self.assertEqual(sandbox["status"], df.FOR_IAN)
        self.assertTrue(sandbox["command"].startswith(f"cd {df.PIONIR_ROOT}; "))
        self.assertIn("ADMINISTRATOR", sandbox["command"])
        self.assertIn("ProcessId=77", self.find(report, "foreign_port")[0]["command"])
        self.assertIn("-Stop", self.find(report, "open_bridge")[0]["command"])

    def test_a_failing_check_never_hides_the_others(self) -> None:
        report = df.run(apply=True, state_root=self.state, rows=STACK, bridge_report={},
                        procs=PROCS, repos=[self.repo], terrarium=self.terrarium,
                        marker=self.marker, now=NOW,
                        sandbox_why=lambda: (_ for _ in ()).throw(OSError("boom")))
        self.assertEqual(self.find(report, "for_ian")[0]["status"], df.FAILED)


class CliTests(unittest.TestCase):
    def test_doctor_takes_fix(self) -> None:
        self.assertTrue(build_parser().parse_args(["doctor", "--fix"]).fix)
        self.assertFalse(build_parser().parse_args(["doctor"]).fix)

    @unittest.skipUnless(os.name == "nt", "reads processes with PowerShell CIM")
    def test_processes_are_read_with_cim_and_their_command_lines(self) -> None:
        procs = df.list_processes()
        self.assertIsNotNone(procs)
        me = next(p for p in procs if p["pid"] == os.getpid())
        self.assertIn("python", me["command"].lower())

    def test_doctor_fix_runs_end_to_end_on_a_throwaway_state_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "PIONIR_STATE_ROOT": tmp, "PYTHONPATH": str(
                Path(__file__).resolve().parent.parent / "src"),
                "PIONIR_TERRARIUM_DIR": str(Path(tmp) / "no-terrarium"),
                "LOCALAPPDATA": tmp, "PIONIR_DAEDALUS_SANDBOX": str(Path(tmp) / "sandbox")}
            done = subprocess.run([sys.executable, "-m", "pionir", "doctor"], env=env,
                                  capture_output=True, text=True, timeout=300, check=False)
            self.assertIn('"fixes"', done.stdout, done.stderr[-2000:])
            self.assertIn('"applied": false', done.stdout)


if __name__ == "__main__":
    unittest.main()
