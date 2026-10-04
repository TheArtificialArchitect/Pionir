"""Node in the build sandbox: the optional node keys of the setup record, and ``run_ts_checks``
(``tsc --noEmit`` then ``vitest run``, contained).

Nothing here touches the real sandbox user, the real node, the network or ``~/.pionir``: the
record, node and tools are made in a temporary folder, the firewall is a fake, and processes are
a fake spawner (the very ``spawner=`` seam ``build_sandbox.run`` has). Only the junction and
tree-removal tests make real junctions - between two temporary folders, as this user.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pionir import build_sandbox as bs
from support import use_long_tempdir

use_long_tempdir()

WINDOWS = os.name == "nt"
SID = "S-1-5-21-1-2-3-1009"
RULE = "Pionir builds - block outbound - node.exe"
NOT_LOOPBACK = ["0.0.0.0-126.255.255.255", "128.0.0.0-255.255.255.255"]

TSC_OK = ""
TSC_ERRORS = ("src/index.ts(3,7): error TS2322: Type 'string' is not assignable to type "
              "'number'.\nsrc/index.ts(9,1): error TS2304: Cannot find name 'Request'.\n")
VITEST_OK = (
    "\n RUN  v3.2.7 C:/src/daedalus-work/.runs/run-ab12/product\n\n"
    " \u2713 test/index.test.ts (5 tests) 4ms\n\n"
    " Test Files  1 passed (1)\n"
    "      Tests  5 passed (5)\n"
    "   Start at  10:22:11\n"
    "   Duration  412ms (transform 30ms, setup 0ms, collect 40ms, tests 4ms, "
    "environment 0ms, prepare 80ms)\n")
VITEST_FAIL = (
    "\n RUN  v3.2.7 C:/product\n\n"
    " \u276f test/index.test.ts (5 tests | 1 failed) 7ms\n"
    "   \u00d7 returns 404 for a missing key 3ms\n\n"
    " FAIL  test/index.test.ts > returns 404 for a missing key\n"
    "AssertionError: expected 200 to be 404\n\n"
    " Test Files  1 failed (1)\n"
    "      Tests  1 failed | 4 passed (5)\n"
    "   Start at  10:22:11\n   Duration  400ms\n")
VITEST_NONE = ("\n RUN  v3.2.7 C:/product\n\n"
               "No test files found, exiting with code 1\n\n"
               "filter: \n include: **/*.{test,spec}.?(c|m)[jt]s?(x)\n")


def make_install(root: Path, *, tools: bool = True) -> SimpleNamespace:
    """An install folder the way the setup script leaves it, with a fake node.exe and fake
    tools (real folders and files, no real node)."""
    install = root / "install"
    node = install / "node" / "node.exe"
    node.parent.mkdir(parents=True)
    node.write_bytes(b"MZ")
    node_tools = install / "node-tools"
    if tools:
        modules = node_tools / "node_modules"
        for rel in ("typescript/lib/tsc.js", "typescript/package.json", "vitest/vitest.mjs",
                    "vitest/package.json", "@cloudflare/workers-types/index.d.ts",
                    "@cloudflare/workers-types/package.json", ".bin/tsc.cmd",
                    ".package-lock.json", "vite/dist/index.js"):
            path = modules / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("// " + rel, encoding="utf-8")
    return SimpleNamespace(install=install, node=node, tools=node_tools)


def good_rule(node) -> dict:
    return {"enabled": "True", "direction": "Outbound", "action": "Block",
            "program": str(node), "remote": list(NOT_LOOPBACK)}


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._t = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._t.name)

    def tearDown(self) -> None:
        self._t.cleanup()


class NodeRecordTests(_Case):
    """``load_setup`` and the optional node keys of the record."""

    def setUp(self) -> None:
        super().setUp()
        self.n = 0
        self.inst = make_install(self.root)
        self.fw = {RULE: [good_rule(self.inst.node)]}

    def record(self, node_keys="all", **over) -> Path:
        sandbox = self.root / "daedalus-work"
        sandbox.mkdir(exist_ok=True)
        src = self.root / "daedalus"
        (src / "daedalus").mkdir(parents=True, exist_ok=True)
        (src / "daedalus" / "server.py").write_text("", encoding="utf-8")
        python = self.root / "python" / "python.exe"
        python.parent.mkdir(exist_ok=True)
        python.write_bytes(b"MZ")
        doc = {"version": 3, "user": bs.USER, "sid": SID, "python": str(python),
               "sandbox_root": str(sandbox), "daedalus_src": str(src),
               "credential": str(self.root / "c.cred"),
               "low_integrity": True, "secrets_readable": 0}
        if node_keys == "all":
            doc.update({"node": str(self.inst.node), "node_tools": str(self.inst.tools),
                        "node_firewall_rules": [RULE], "node_version": "v22.12.0",
                        "node_typescript": "5.9.3", "node_vitest": "3.2.7",
                        "node_workers_types": "5.20260907.1"})
        doc.update(over)
        for key in [k for k, v in doc.items() if v == "<drop>"]:
            del doc[key]
        self.n += 1
        path = self.root / f"setup-{self.n}.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path

    def load(self, record, *, low=None, firewall=None):
        return bs.load_setup(
            self.root / "daedalus-work", record=record, lookup=lambda _n: SID,
            read=lambda _p: "x" * 20, low=low or (lambda _p: True),
            firewall=firewall or (lambda name: self.fw.get(name, [])))

    def test_a_record_without_node_still_loads_with_node_none(self) -> None:
        for kw in (dict(node_keys="none"),):
            setup, why = self.load(self.record(**kw))
            self.assertIsNone(why)
            self.assertIsNone(setup.node)
            self.assertIsNone(setup.node_tools)
            self.assertIsNone(setup.node_dir)
        self.assertEqual(bs.RECORD_VERSION, 3)

    def test_a_record_without_node_never_reads_the_firewall(self) -> None:
        def boom(_name):
            raise AssertionError("the firewall was read")

        setup, why = self.load(self.record(node_keys="none"), firewall=boom)
        self.assertIsNone(why)
        self.assertIsNone(setup.node)

    def test_a_complete_node_record_is_accepted(self) -> None:
        asked = []

        def firewall(name):
            asked.append(name)
            return self.fw[name]

        setup, why = self.load(self.record(), firewall=firewall)
        self.assertIsNone(why)
        self.assertEqual(setup.node, self.inst.node)
        self.assertEqual(setup.node_tools, self.inst.tools)
        self.assertEqual(setup.node_dir, self.inst.node.parent)
        self.assertEqual(asked, [RULE])
        self.assertEqual(setup.python.name, "python.exe")      # everything else is as before

    def test_any_node_key_makes_the_whole_of_it_required(self) -> None:
        keys = {"node": str(self.inst.node), "node_tools": str(self.inst.tools),
                "node_firewall_rules": [RULE]}
        for kept in keys:
            partial = {k: v for k, v in keys.items() if k == kept}
            setup, why = self.load(self.record(node_keys="none", **partial))
            self.assertIsNone(setup, kept)
            self.assertTrue(why.startswith(bs.SETUP_HINT), kept)
            self.assertIn("part of the node setup", why, kept)
        for left_out in keys:
            setup, why = self.load(self.record(**{left_out: "<drop>"}))
            self.assertIsNone(setup, left_out)
            self.assertIn("part of the node setup", why)
        # even a record with only a node version note is half a node setup
        setup, why = self.load(self.record(node_keys="none", node_version="v22.12.0"))
        self.assertIsNone(setup)
        self.assertIn("node", why)

    def test_every_other_flaw_refuses_the_whole_record(self) -> None:
        elsewhere = Path(sys.executable)
        other = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(other.cleanup)
        outside_tools = Path(other.name) / "node-tools"
        shutil.copytree(self.inst.tools, outside_tools)
        cases = {
            "relative node path": dict(node=r"node\node.exe"),
            "relative tools path": dict(node_tools="node-tools"),
            "node outside the install": dict(node=str(elsewhere)),
            "tools outside the install": dict(node_tools=str(outside_tools)),
            "node missing": dict(node=str(self.inst.install / "node" / "nope.exe")),
            "no rule names": dict(node_firewall_rules=[]),
            "a blank rule name": dict(node_firewall_rules=[""]),
            "a rule name that is not text": dict(node_firewall_rules=[7]),
            "rules not a list": dict(node_firewall_rules=RULE),
            "an empty node path": dict(node=""),
            "an unknown rule": dict(node_firewall_rules=["No such rule"]),
        }
        for name, over in cases.items():
            setup, why = self.load(self.record(**over))
            self.assertIsNone(setup, name)
            self.assertTrue(why.startswith(bs.SETUP_HINT), name)
            self.assertIn("node", why, name)

    def test_the_tools_must_be_installed(self) -> None:
        modules = self.inst.tools / "node_modules"
        for rel, word in (("typescript", "typescript"), ("vitest", "vitest")):
            hidden = modules / (rel + ".gone")
            (modules / rel).rename(hidden)
            try:
                setup, why = self.load(self.record())
                self.assertIsNone(setup, rel)
                self.assertIn(word, why)
                self.assertIn("not installed", why)
            finally:
                hidden.rename(modules / rel)
        setup, why = self.load(self.record())
        self.assertIsNone(why)

    def test_node_must_carry_the_low_label_like_python(self) -> None:
        setup, why = self.load(self.record(), low=lambda p: "node.exe" not in str(p))
        self.assertIsNone(setup)
        self.assertIn("Low integrity label", why)
        self.assertIn("node.exe", why)
        # and python's own label is still checked first, as before
        setup, why = self.load(self.record(), low=lambda p: "python" not in str(p))
        self.assertIn("interpreter", why)

    def test_the_firewall_rule_must_really_be_there_and_loopback_only(self) -> None:
        node = self.inst.node
        flawed = {
            "missing": [],
            "disabled": [{**good_rule(node), "enabled": "False"}],
            "allows instead of blocks": [{**good_rule(node), "action": "Allow"}],
            "inbound": [{**good_rule(node), "direction": "Inbound"}],
            "for another program": [{**good_rule(node), "program": r"C:\other\node.exe"}],
            "for any program": [{**good_rule(node), "program": ""}],
            "blocks any address": [{**good_rule(node), "remote": ["Any"]}],
            "blocks no address": [{**good_rule(node), "remote": []}],
            "covers loopback": [{**good_rule(node), "remote": ["0.0.0.0-255.255.255.255"]}],
            "covers loopback as a net": [{**good_rule(node), "remote": ["127.0.0.0/8"]}],
            "is loopback itself": [{**good_rule(node), "remote": ["127.0.0.1"]}],
        }
        for name, rules in flawed.items():
            setup, why = self.load(self.record(), firewall=lambda _n, r=rules: r)
            self.assertIsNone(setup, name)
            self.assertTrue(why.startswith(bs.SETUP_HINT), name)
            self.assertIn("firewall rule", why, name)

    def test_a_firewall_that_cannot_be_read_refuses_the_record(self) -> None:
        def broken(_name):
            raise bs.SandboxError("the firewall could not be read (exit 1)")

        setup, why = self.load(self.record(), firewall=broken)
        self.assertIsNone(setup)
        self.assertIn("cannot be checked", why)

    def test_a_rule_given_as_one_dict_and_extra_rules_are_understood(self) -> None:
        node = self.inst.node
        self.assertIsNone(bs.firewall_problem(good_rule(node), RULE, node))
        extra = [{**good_rule(node), "enabled": "False"}, good_rule(node)]
        self.assertIsNone(bs.firewall_problem(extra, RULE, node))
        if WINDOWS:     # the program path matches as Windows does: ignoring case
            self.assertIsNone(bs.firewall_problem(
                [{**good_rule(node), "program": str(node).upper(), "enabled": True}], RULE, node))
        self.assertIsNotNone(bs.firewall_problem(None, RULE, node))

    def test_the_firewall_is_read_with_the_name_in_the_environment_not_the_command(self) -> None:
        calls = []

        def fake_run(argv, **kw):
            calls.append((argv, kw))
            return subprocess.CompletedProcess(argv, 0, json.dumps([good_rule("C:/n/node.exe")]), "")

        with mock.patch.object(bs.subprocess, "run", fake_run), \
                mock.patch.object(bs, "_WIN", True):
            rules = bs.read_firewall_rule(RULE)
        self.assertEqual(rules[0]["action"], "Block")
        argv, kw = calls[0]
        self.assertNotIn(RULE, " ".join(argv))
        self.assertEqual(kw["env"]["PIONIR_FW_RULE"], RULE)
        self.assertIn("Get-NetFirewallRule", argv[-1])
        # one rule is a JSON object, none is an empty list, a failure raises
        with mock.patch.object(bs.subprocess, "run", lambda a, **k: subprocess.CompletedProcess(
                a, 0, "", "")), mock.patch.object(bs, "_WIN", True):
            self.assertEqual(bs.read_firewall_rule(RULE), [])
        with mock.patch.object(bs.subprocess, "run", lambda a, **k: subprocess.CompletedProcess(
                a, 1, "", "denied")), mock.patch.object(bs, "_WIN", True):
            with self.assertRaises(bs.SandboxError):
                bs.read_firewall_rule(RULE)


class FakeSetup:
    """What load_setup answers, with a fake node: records the calls the run makes."""

    def __init__(self, root: Path, *, with_node: bool = True, preflight_error=None,
                 tools: bool = True) -> None:
        inst = make_install(root, tools=tools)
        self.user = bs.USER
        self.sid = SID
        self.sandbox_root = root / "sandbox"
        self.runs_dir = self.sandbox_root / ".runs"
        self.python = Path(sys.executable)
        self.node = inst.node if with_node else None
        self.node_tools = inst.tools if with_node else None
        self.preflight_error = preflight_error
        self.events: list = []

    def logon(self):
        return ("pionir-builds", ".", "pw-for-tests")

    def reap(self):
        self.events.append("reap")

    def preflight(self):
        self.events.append("preflight")
        if self.preflight_error:
            raise self.preflight_error
        return {"integrity": 0x1000, "readable": [], "listable": []}


class FakeProc:
    def __init__(self, code, log) -> None:
        self.code = code
        self.log = log

    def wait(self, timeout):
        self.log.append(("wait", timeout))
        return self.code

    def close(self) -> None:
        self.log.append(("close", None))


class Spawner:
    """The ``spawner=`` seam: answers tsc and vitest from a script and records every start."""

    def __init__(self, *, tsc=(0, TSC_OK), vitest=(0, VITEST_OK), tsc_hangs=False,
                 vitest_hangs=False, check=None) -> None:
        self.tsc, self.vitest = tsc, vitest
        self.hangs = {"tsc": tsc_hangs, "vitest": vitest_hangs}
        self.check = check
        self.calls: list = []
        self.log: list = []

    def __call__(self, argv, *, cwd, env, stdout, stderr, limits, logon, sid=None):
        which = "tsc" if "tsc.js" in str(argv[1]) else "vitest"
        snapshot = {"modules": sorted(p.name for p in (Path(cwd) / "node_modules").iterdir())
                    if (Path(cwd) / "node_modules").is_dir() else None,
                    "files": sorted(p.name for p in Path(cwd).iterdir())}
        self.calls.append(SimpleNamespace(which=which, argv=list(argv), cwd=Path(cwd), env=env,
                                          limits=limits, logon=logon, sid=sid, seen=snapshot))
        if self.check:
            self.check(self.calls[-1])
        code, text = self.tsc if which == "tsc" else self.vitest
        Path(stdout).write_text(text, encoding="utf-8")
        Path(stderr).write_text("", encoding="utf-8")
        return FakeProc(None if self.hangs[which] else code, self.log)


class RunTsChecksTests(_Case):
    def setUp(self) -> None:
        super().setUp()
        self.setup = FakeSetup(self.root)
        self.files = {"package.json": b"{}", "tsconfig.json": b"{}",
                      "src/index.ts": b"export const x = 1;\n",
                      "test/index.test.ts": b"import {it} from 'vitest';\n"}

    def run_it(self, spawner, *, setup=None, files=None, **kw) -> bs.TsRun:
        with mock.patch.object(bs, "_make_junction", self._link):
            return bs.run_ts_checks(self.files if files is None else files,
                                    setup=setup or self.setup, spawner=spawner, **kw)

    @staticmethod
    def _link(target: Path, link: Path) -> None:
        # a real junction where the OS can, so the read-through is real
        if WINDOWS:
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
        else:
            os.symlink(target, link, target_is_directory=True)

    def runs_left(self) -> list:
        return sorted(p.name for p in self.setup.runs_dir.glob("run-*")) \
            if self.setup.runs_dir.exists() else []

    def test_a_passing_product_passes_with_both_checks_run_contained(self) -> None:
        spawner = Spawner()
        result = self.run_it(spawner)
        self.assertTrue(result.passed, result.tail)
        self.assertEqual((result.ran, result.tsc_ok, result.timed_out), (5, True, False))
        self.assertIn("tsc --noEmit", result.command)
        self.assertIn("vitest run", result.command)
        self.assertLessEqual(len(result.tail), 1500)
        tsc, vitest = spawner.calls
        self.assertEqual((tsc.which, vitest.which), ("tsc", "vitest"))
        node, tools = str(self.setup.node), self.setup.node_tools
        for call in (tsc, vitest):
            self.assertEqual(call.argv[0], node)                      # node itself ...
            self.assertTrue(call.argv[1].endswith(".js") or call.argv[1].endswith(".mjs"))
            self.assertTrue(Path(call.argv[1]).is_relative_to(tools))  # ... the read-only tools
            joined = " ".join(call.argv).lower()
            for banned in ("npm", "npx", ".cmd", "cmd.exe", "powershell"):
                self.assertNotIn(banned, joined)
            self.assertEqual(call.logon, self.setup.logon())           # as the sandbox user
            self.assertEqual(call.sid, SID)
            self.assertTrue(call.cwd.is_relative_to(self.setup.runs_dir))
        self.assertEqual(Path(tsc.argv[1]).parts[-4:], ("node_modules", "typescript", "lib", "tsc.js"))
        self.assertIn("--noEmit", tsc.argv)
        self.assertEqual(Path(vitest.argv[1]).name, "vitest.mjs")
        self.assertEqual(vitest.argv[2], "run")                        # run mode ...
        for flag in ("--watch=false", "--coverage.enabled=false"):     # ... never watch/coverage
            self.assertIn(flag, vitest.argv)
        self.assertNotIn("--watch", vitest.argv)
        self.assertNotIn("--ui", vitest.argv)

    def test_the_product_is_there_with_a_node_modules_of_per_package_links(self) -> None:
        spawner = Spawner()
        self.run_it(spawner)
        seen = spawner.calls[0].seen
        self.assertEqual(seen["files"], ["node_modules", "package.json", "src", "test",
                                         "tsconfig.json"])
        # one entry per package, scopes one level down; dot entries (.bin ...) are not linked
        self.assertEqual(seen["modules"], ["@cloudflare", "typescript", "vite", "vitest"])

    def test_the_environment_is_minimal_and_has_no_network_or_update_checks(self) -> None:
        spawner = Spawner()
        self.run_it(spawner)
        env = spawner.calls[0].env
        system32 = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32")
        self.assertEqual(env["PATH"], os.pathsep.join([str(self.setup.node.parent), system32]))
        for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP"):
            self.assertTrue(Path(env[key]).is_relative_to(self.setup.runs_dir), key)
        self.assertEqual(env["NODE_ENV"], "test")
        self.assertEqual(env["NO_UPDATE_NOTIFIER"], "1")
        self.assertEqual(env["DO_NOT_TRACK"], "1")
        self.assertEqual(env["CI"], "1")
        self.assertNotIn("USERNAME", env)
        # nothing of the owner's leaks in
        owner = {k for k in os.environ if k.upper() in ("USERNAME", "USERDOMAIN", "COMPUTERNAME",
                                                         "ONEDRIVE", "HOMEPATH")}
        self.assertFalse(owner & set(env))

    def test_the_job_is_node_sized_and_kills_on_close(self) -> None:
        spawner = Spawner()
        self.run_it(spawner, timeout=200.0)
        for call in spawner.calls:
            self.assertEqual(call.limits.active_processes, 16)
            self.assertEqual(call.limits.job_memory_mb, 3072)
            self.assertGreaterEqual(call.limits.cpu_seconds, 30)
        # every contained process is closed (the kill-on-close job), after it was waited for
        self.assertEqual([k for k, _ in spawner.log], ["wait", "close", "wait", "close"])

    def test_the_timeout_is_shared_between_the_two_steps(self) -> None:
        spawner = Spawner()
        self.run_it(spawner, timeout=100.0)
        waits = [v for k, v in spawner.log if k == "wait"]
        self.assertAlmostEqual(waits[0], 40.0, delta=0.5)         # tsc: 40% of the timeout
        self.assertGreaterEqual(waits[1], 59.0)                   # vitest: at least the other 60%
        self.assertLessEqual(waits[1], 100.0)                     # ... and never more than all
        # a tsc that used its whole share leaves vitest the 60% that remains
        ticks = {"n": 0}
        real = bs.time.monotonic

        def clock():
            ticks["n"] += 1
            return real() + (40.0 if ticks["n"] > 2 else 0.0)

        spawner = Spawner()
        with mock.patch.object(bs.time, "monotonic", clock):
            self.run_it(spawner, timeout=100.0)
        waits = [v for k, v in spawner.log if k == "wait"]
        self.assertAlmostEqual(waits[1], 60.0, delta=1.5)

    def test_reap_before_and_after_a_preflight_and_nothing_is_left_behind(self) -> None:
        spawner = Spawner()
        self.run_it(spawner)
        self.assertEqual(self.setup.events, ["reap", "preflight", "reap"])
        self.assertEqual(self.runs_left(), [])

    def test_a_tsc_failure_fails_the_run_and_says_why(self) -> None:
        spawner = Spawner(tsc=(2, TSC_ERRORS))
        result = self.run_it(spawner)
        self.assertFalse(result.passed)
        self.assertFalse(result.tsc_ok)
        self.assertEqual(result.ran, 5)                  # vitest was still run for the feedback
        self.assertIn("error TS2322", result.tail)
        self.assertLessEqual(len(result.tail), 1500)
        self.assertFalse(result.timed_out)

    def test_a_vitest_failure_fails_the_run_with_tsc_ok(self) -> None:
        spawner = Spawner(vitest=(1, VITEST_FAIL))
        result = self.run_it(spawner)
        self.assertFalse(result.passed)
        self.assertTrue(result.tsc_ok)
        self.assertEqual(result.ran, 5)
        self.assertIn("expected 200 to be 404", result.tail)

    def test_zero_tests_is_a_failure_even_if_both_tools_exit_zero(self) -> None:
        for tail in (VITEST_NONE, " Test Files  1 passed (1)\n      Tests  no tests\n",
                     " Tests  3 skipped | 2 todo (5)\n"):
            for code in (0, 1):
                result = self.run_it(Spawner(vitest=(code, tail)))
                self.assertFalse(result.passed, (tail, code))
                self.assertEqual(result.ran, 0)
                self.assertTrue(result.tsc_ok)
        # and never a pass on exit codes alone
        self.assertFalse(self.run_it(Spawner(vitest=(0, ""))).passed)

    def test_a_timeout_kills_and_reports_without_a_pass(self) -> None:
        for hung in ("tsc", "vitest"):
            spawner = Spawner(**{f"{hung}_hangs": True})
            result = self.run_it(spawner)
            self.assertFalse(result.passed, hung)
            self.assertTrue(result.timed_out, hung)
            self.assertEqual(result.ran, 0)
            self.assertEqual(result.tsc_ok, hung == "vitest")
            self.assertIn("killed", result.tail)
            self.assertEqual(spawner.log[-1][0], "close")
            self.assertEqual(self.setup.events[-1], "reap")
            self.assertEqual(self.runs_left(), [])
            self.setup.events.clear()
        # a hung tsc is not followed by a vitest run
        self.assertEqual([c.which for c in Spawner(tsc_hangs=True).calls], [])
        hung = Spawner(tsc_hangs=True)
        self.run_it(hung)
        self.assertEqual([c.which for c in hung.calls], ["tsc"])

    def test_no_node_set_up_means_nothing_starts(self) -> None:
        setup = FakeSetup(self.root / "b", with_node=False)
        spawner = Spawner()
        result = self.run_it(spawner, setup=setup)
        self.assertEqual((result.passed, result.ran, result.tsc_ok), (False, 0, False))
        self.assertTrue(result.tail.startswith("the tests could not start contained ("))
        self.assertIn("node is not set up", result.tail)
        self.assertEqual(spawner.calls, [])
        self.assertEqual(setup.events, [])               # not even the sandbox user was touched

    def test_missing_tools_mean_nothing_starts(self) -> None:
        setup = FakeSetup(self.root / "c", tools=False)
        spawner = Spawner()
        result = self.run_it(spawner, setup=setup)
        self.assertFalse(result.passed)
        self.assertIn("could not start contained", result.tail)
        self.assertEqual(spawner.calls, [])

    def test_a_failed_preflight_means_nothing_runs_and_everything_is_cleaned(self) -> None:
        setup = FakeSetup(self.root / "d", preflight_error=bs.SandboxError(
            "the pionir-builds user is not contained - it can read 1 of your secrets"))
        spawner = Spawner()
        result = self.run_it(spawner, setup=setup)
        self.assertEqual((result.passed, result.ran, result.tsc_ok), (False, 0, False))
        self.assertIn("could not start contained", result.tail)
        self.assertIn("not contained", result.tail)
        self.assertEqual(spawner.calls, [])
        self.assertEqual(setup.events, ["reap", "preflight", "reap"])
        self.assertEqual(sorted(setup.runs_dir.glob("run-*")), [])

    def test_it_never_raises(self) -> None:
        def explode(*_a, **_k):
            raise RuntimeError("boom")

        result = self.run_it(explode)
        self.assertFalse(result.passed)
        self.assertIn("could not start contained", result.tail)
        self.assertIn("boom", result.tail)
        broken = FakeSetup(self.root / "e")
        broken.reap = explode
        self.assertFalse(self.run_it(Spawner(), setup=broken).passed)
        self.assertFalse(self.run_it(Spawner(), files={"a": "not bytes"}).passed)
        self.assertEqual(self.runs_left(), [])

    def test_a_tree_that_could_escape_or_fill_node_modules_is_refused(self) -> None:
        for rel in ("../evil.ts", "/abs.ts", "C:/x.ts", "a/../../b.ts", "a\\b.ts", "",
                    "node_modules/typescript/lib/tsc.js", "pkg/node_modules/x.js", "a//b.ts"):
            spawner = Spawner()
            result = self.run_it(spawner, files={rel: b"x", "package.json": b"{}"})
            self.assertFalse(result.passed, rel)
            self.assertIn("could not start contained", result.tail, rel)
            self.assertEqual(spawner.calls, [], rel)
        self.assertFalse((self.root / "evil.ts").exists())
        self.assertFalse((self.setup.runs_dir.parent / "evil.ts").exists())
        self.assertEqual(self.runs_left(), [])

    def test_the_tools_are_never_touched_by_a_run(self) -> None:
        before = sorted(str(p.relative_to(self.setup.node_tools))
                        for p in self.setup.node_tools.rglob("*"))
        self.run_it(Spawner())
        self.run_it(Spawner(tsc=(2, TSC_ERRORS)))
        self.run_it(Spawner(vitest_hangs=True))
        after = sorted(str(p.relative_to(self.setup.node_tools))
                       for p in self.setup.node_tools.rglob("*"))
        self.assertEqual(before, after)


class VitestRanTests(unittest.TestCase):
    def test_realistic_summaries(self) -> None:
        self.assertEqual(bs.vitest_ran(VITEST_OK), 5)
        self.assertEqual(bs.vitest_ran(VITEST_FAIL), 5)                # failed + passed
        self.assertEqual(bs.vitest_ran(VITEST_NONE), 0)
        self.assertEqual(bs.vitest_ran(""), 0)
        self.assertEqual(bs.vitest_ran(None), 0)
        self.assertEqual(bs.vitest_ran(" Test Files  1 passed (1)\n      Tests  12 passed (12)\n"),
                         12)
        # skipped and todo tests did not run
        self.assertEqual(bs.vitest_ran(" Tests  1 failed | 2 passed | 3 skipped | 1 todo (7)\n"), 3)
        self.assertEqual(bs.vitest_ran("      Tests  4 skipped (4)\n"), 0)

    def test_ansi_colours_do_not_hide_the_summary(self) -> None:
        coloured = ("\x1b[2m Test Files \x1b[22m \x1b[1m\x1b[32m1 passed\x1b[39m\x1b[22m\x1b[90m (1)\x1b[39m\n"
                    "\x1b[2m      Tests \x1b[22m \x1b[1m\x1b[31m1 failed\x1b[39m\x1b[22m | "
                    "\x1b[1m\x1b[32m4 passed\x1b[39m\x1b[22m\x1b[90m (5)\x1b[39m\n")
        self.assertEqual(bs.vitest_ran(coloured), 5)

    def test_the_last_summary_wins_and_test_names_cannot_fake_one(self) -> None:
        text = (" \u2713 test/a.test.ts (1 test)\n"
                "   \u2713 Tests 99 passed (99) is only a test title\n"
                " Test Files  1 passed (1)\n      Tests  1 passed (1)\n")
        self.assertEqual(bs.vitest_ran(text), 1)

    def test_a_tail_never_exceeds_the_limit(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as t:
            setup = FakeSetup(Path(t))
            noise = "x" * 9000
            with mock.patch.object(bs, "_make_junction", RunTsChecksTests._link):
                result = bs.run_ts_checks(
                    {"package.json": b"{}"}, setup=setup,
                    spawner=Spawner(tsc=(2, noise), vitest=(1, noise + VITEST_FAIL)))
        self.assertLessEqual(len(result.tail), 1500)
        self.assertIn("vitest run", result.tail)


class NodeReadyTests(_Case):
    def test_none_when_usable(self) -> None:
        self.assertIsNone(bs.node_ready(FakeSetup(self.root)))

    def test_every_gap_says_what_to_run(self) -> None:
        hint = r"run tools\setup-build-sandbox.ps1 as administrator"
        self.assertEqual(bs.node_ready(FakeSetup(self.root / "a", with_node=False)),
                         "node is not set up: " + hint)
        self.assertEqual(bs.node_ready(None), "node is not set up: " + hint)
        self.assertEqual(bs.node_ready(SimpleNamespace()), "node is not set up: " + hint)
        gone = FakeSetup(self.root / "b")
        gone.node.unlink()
        why = bs.node_ready(gone)
        self.assertIn("node.exe", why)
        self.assertIn("missing", why)
        self.assertTrue(why.endswith(hint))
        no_tools = FakeSetup(self.root / "c")
        (no_tools.node_tools / "node_modules" / "vitest" / "vitest.mjs").unlink()
        why = bs.node_ready(no_tools)
        self.assertIn("vitest", why)
        self.assertTrue(why.endswith(hint))

    def test_it_matches_the_real_dataclass(self) -> None:
        inst = make_install(self.root)
        setup = bs.SandboxSetup(bs.USER, SID, Path("p.exe"), self.root, self.root, self.root,
                                inst.node, inst.tools)
        self.assertIsNone(bs.node_ready(setup))
        bare = bs.SandboxSetup(bs.USER, SID, Path("p.exe"), self.root, self.root, self.root)
        self.assertIsNone(bare.node)
        self.assertIsNone(bare.node_tools)
        self.assertEqual(bs.node_ready(bare), bs.NODE_HINT)


class NodeModulesLinkTests(_Case):
    """The per-package junctions: the tools stay read-only, vite may still write its cache."""

    def setUp(self) -> None:
        super().setUp()
        self.setup = FakeSetup(self.root)
        self.dest = self.root / "product"
        self.dest.mkdir()

    @unittest.skipUnless(WINDOWS, "junctions are Windows")
    def test_each_package_is_a_junction_and_the_folder_is_real_and_writable(self) -> None:
        made = bs.link_node_modules(self.dest, self.setup)
        modules = self.dest / "node_modules"
        self.assertEqual(made, 4)
        self.assertFalse(bs._is_junction(modules))
        for rel in ("typescript", "vitest", "vite", "@cloudflare/workers-types"):
            self.assertTrue(bs._is_junction(modules / rel), rel)
        self.assertFalse(bs._is_junction(modules / "@cloudflare"))
        self.assertFalse((modules / ".bin").exists())
        # read through a link
        self.assertEqual((modules / "vitest" / "vitest.mjs").read_text(encoding="utf-8"),
                         "// vitest/vitest.mjs")
        # vite's cache lands in the real folder, not in the tools
        (modules / ".vite" / "deps").mkdir(parents=True)
        (modules / ".vite" / "deps" / "x").write_text("cache", encoding="utf-8")
        self.assertFalse((self.setup.node_tools / "node_modules" / ".vite").exists())

    @unittest.skipUnless(WINDOWS, "junctions are Windows")
    def test_remove_tree_unlinks_junctions_and_never_follows_them(self) -> None:
        bs.link_node_modules(self.dest, self.setup)
        precious = self.root / "precious"
        precious.mkdir()
        (precious / "keep.txt").write_text("keep", encoding="utf-8")
        import _winapi
        _winapi.CreateJunction(str(precious), str(self.dest / "evil-link"))   # generated code's
        tools_before = sorted(p.name for p in (self.setup.node_tools / "node_modules").rglob("*"))
        bs.remove_tree(self.dest)
        self.assertFalse(self.dest.exists())
        self.assertEqual((precious / "keep.txt").read_text(encoding="utf-8"), "keep")
        self.assertEqual(sorted(p.name for p in (self.setup.node_tools / "node_modules").rglob("*")),
                         tools_before)

    def test_a_package_that_cannot_be_linked_is_copied(self) -> None:
        def refuse(_target, _link):
            raise OSError("no junctions here")

        made = bs.link_node_modules(self.dest, self.setup, make_link=refuse)
        self.assertEqual(made, 4)
        modules = self.dest / "node_modules"
        self.assertEqual((modules / "typescript" / "lib" / "tsc.js").read_text(encoding="utf-8"),
                         "// typescript/lib/tsc.js")
        self.assertEqual((modules / "@cloudflare" / "workers-types" / "package.json").is_file(),
                         True)
        bs.remove_tree(self.dest)
        self.assertFalse(self.dest.exists())

    def test_linking_is_idempotent_and_needs_the_tools(self) -> None:
        links = []
        bs.link_node_modules(self.dest, self.setup, make_link=lambda t, l: links.append(l)
                             or l.mkdir())
        self.assertEqual(len(links), 4)
        again = bs.link_node_modules(self.dest, self.setup, make_link=lambda t, l: l.mkdir())
        self.assertEqual(again, 0)
        broken = FakeSetup(self.root / "z", tools=False)
        with self.assertRaises(bs.SandboxError):
            bs.link_node_modules(self.root / "nowhere", broken)


class SafeTreeTests(_Case):
    def test_only_plain_relative_files_are_written(self) -> None:
        bs.write_ts_tree({"a.txt": b"1", "src/b/c.ts": b"2"}, self.root / "t")
        self.assertEqual((self.root / "t" / "src" / "b" / "c.ts").read_bytes(), b"2")
        for rel in ("..\\x", "../x", "/x", "c:/x", "a/./b", ".", "a/../b", "node_modules/x",
                    "a/NODE_MODULES/x", "trailing./x", "space /x"):
            with self.assertRaises(bs.SandboxError, msg=rel):
                bs.write_ts_tree({rel: b"x"}, self.root / "u")
        with self.assertRaises(bs.SandboxError):
            bs.write_ts_tree({"ok.txt": "text"}, self.root / "u")


if __name__ == "__main__":
    unittest.main()
