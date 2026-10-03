"""Tutorial scenes: a command is shown only after it was really run in the build sandbox, and
every number narrated comes from what it printed. Each rule has a test that fails when the rule
is removed. A fake setup and spawner stand in for the sandbox; nothing is spawned."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from video_support import make_niche, pack_document

from pionir.video.passages import PassageError, load_pack
from pionir.video.script import ScriptRejected, check_script
from pionir.video import tutorial
from pionir.video.tutorial import (
    Measure, Step, TutorialError, measure_output, parse_steps, run_step, run_steps, write_runs,
)

STDOUT = "model loaded\ntokens per second: 41.5\npeak vram: 6,820 MB\n"


def spec(**overrides):
    base = {"id": "bench", "title": "Measure speed", "argv": ["python", "bench.py"],
            "files": {"bench.py": "print('hi')"},
            "measure": [{"name": "speed", "pattern": r"tokens per second: ([\d.]+)", "unit": "tok/s"},
                        {"name": "vram", "pattern": r"peak vram: ([\d,]+) MB", "unit": "MB"}]}
    base.update(overrides)
    return base


class ParseTests(unittest.TestCase):
    def test_a_valid_spec_parses(self) -> None:
        (step,) = parse_steps([spec()])
        self.assertEqual(step.argv, ("python", "bench.py"))
        self.assertEqual([m.name for m in step.measure], ["speed", "vram"])

    def test_bad_specs_are_refused_with_a_reason(self) -> None:
        bad = [
            ([], "non-empty"),
            ("x", "non-empty"),
            ([spec(id="../x")], "id must match"),
            ([spec(title=" ")], "title"),
            ([spec(argv=[])], "argv"),
            ([spec(argv=["python", ""])], "argv"),
            ([spec(bogus=1)], "unknown fields"),
            ([spec(files={"../evil.py": "x"})], "inside the run folder"),
            ([spec(files={"/abs.py": "x"})], "inside the run folder"),
            ([spec(files={"C:evil.py": "x"})], "inside the run folder"),
            ([spec(files={"a\\b.py": "x"})], "inside the run folder"),
            ([spec(measure=[{"name": "n", "pattern": "(unclosed"}])], "pattern"),
            ([spec(measure=[{"name": "n", "pattern": "no group"}])], "exactly one group"),
            ([spec(measure=[{"name": "n", "pattern": "(a)(b)"}])], "exactly one group"),
            ([spec(timeout=0)], "timeout"),
            ([spec(timeout=9999)], "timeout"),
            ([spec(), spec()], "duplicate"),
        ]
        for raw, why in bad:
            with self.assertRaisesRegex(TutorialError, why, msg=str(raw)[:60]):
                parse_steps(raw)


class MeasureTests(unittest.TestCase):
    def step(self, *measures):
        return parse_steps([spec(measure=list(measures))])[0]

    def test_numbers_are_read_off_the_output(self) -> None:
        got = measure_output(self.step(*spec()["measure"]), STDOUT)
        self.assertEqual([(m.name, m.value, m.unit) for m in got],
                         [("speed", 41.5, "tok/s"), ("vram", 6820.0, "MB")])

    def test_a_pattern_that_finds_nothing_is_an_error_not_a_zero(self) -> None:
        step = self.step({"name": "speed", "pattern": r"latency: ([\d.]+)"})
        with self.assertRaisesRegex(TutorialError, "not in the command's output"):
            measure_output(step, STDOUT)

    def test_a_match_that_is_not_a_number_is_an_error(self) -> None:
        step = self.step({"name": "speed", "pattern": r"(model) loaded"})
        with self.assertRaisesRegex(TutorialError, "not a number"):
            measure_output(step, STDOUT)


class FakeSetup:
    def __init__(self, root: Path) -> None:
        self.runs_dir = root / "runs"
        self.runs_dir.mkdir()
        self.python = Path(sys.executable)
        self.python_dir = self.python.parent
        self.sid = "S-1-5-FAKE"
        self.events: list[str] = []

    def logon(self):
        return ("pionir-builds", ".", "pw-for-tests")

    def reap(self) -> None:
        self.events.append("reap")

    def preflight(self) -> None:
        self.events.append("preflight")


class FakeProc:
    def __init__(self, code) -> None:
        self.code = code

    def wait(self, timeout):
        return self.code

    def close(self) -> None:
        pass


class Spawner:
    def __init__(self, code=0, out=STDOUT) -> None:
        self.code, self.out = code, out
        self.calls: list = []

    def __call__(self, argv, *, cwd, env, stdout, stderr, limits, logon, sid=None):
        self.calls.append(SimpleNamespace(argv=list(argv), cwd=Path(cwd), env=env, limits=limits,
                                          files=sorted(p.name for p in Path(cwd).iterdir())))
        Path(stdout).write_bytes(self.out.encode("utf-8"))
        Path(stderr).write_text("", encoding="utf-8")
        return FakeProc(self.code)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.setup = FakeSetup(self.root)


class RunStepTests(Base):
    def test_a_run_is_recorded_with_the_sandbox_runner_and_its_measurements(self) -> None:
        spawner = Spawner()
        (step,) = parse_steps([spec()])
        run = run_step(step, setup=self.setup, spawner=spawner)
        self.assertEqual(run.runner, "sandbox")
        self.assertTrue(run.ok)
        self.assertEqual({m.name: m.value for m in run.measured}, {"speed": 41.5, "vram": 6820.0})
        self.assertEqual(run.stdout, STDOUT.strip())
        call = spawner.calls[0]
        self.assertEqual(call.argv, [str(self.setup.python), "bench.py"])   # "python" = sandbox's own
        self.assertEqual(call.files, ["bench.py"])                          # the spec's files, laid down
        self.assertEqual(self.setup.events[0], "reap")
        self.assertIn("preflight", self.setup.events)
        self.assertEqual(list(self.setup.runs_dir.iterdir()), [])           # the run folder is gone

    def test_the_run_env_holds_nothing_of_the_owners(self) -> None:
        spawner = Spawner()
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-leak", "USERNAME": "zz-owner-name"}):
            run_step(parse_steps([spec()])[0], setup=self.setup, spawner=spawner)
        text = json.dumps(spawner.calls[0].env)
        self.assertNotIn("sk-leak", text)
        self.assertNotIn("zz-owner-name", text)

    def test_a_failing_command_is_returned_failed_with_no_measurements(self) -> None:
        run = run_step(parse_steps([spec()])[0], setup=self.setup, spawner=Spawner(code=3))
        self.assertFalse(run.ok)
        self.assertEqual(run.exit_code, 3)
        self.assertEqual(run.measured, ())

    def test_a_silent_command_is_not_ok(self) -> None:
        run = run_step(parse_steps([spec(measure=[])])[0], setup=self.setup,
                       spawner=Spawner(code=0, out="  \n"))
        self.assertFalse(run.ok)

    def test_a_timeout_is_not_ok(self) -> None:
        run = run_step(parse_steps([spec()])[0], setup=self.setup, spawner=Spawner(code=None))
        self.assertTrue(run.timed_out)
        self.assertFalse(run.ok)

    def test_a_measure_missing_from_a_successful_run_raises_rather_than_inventing(self) -> None:
        with self.assertRaises(TutorialError):
            run_step(parse_steps([spec()])[0], setup=self.setup,
                     spawner=Spawner(out="nothing useful here\n"))

    def test_a_sandbox_that_cannot_start_contained_raises(self) -> None:
        from pionir import build_sandbox as bs

        def refuse(*a, **k):
            raise bs.SandboxError("firewall rule missing")

        self.setup.preflight = refuse
        spawner = Spawner()
        with self.assertRaisesRegex(TutorialError, "could not start contained"):
            run_step(parse_steps([spec()])[0], setup=self.setup, spawner=spawner)
        self.assertEqual(spawner.calls, [])

    def test_no_sandbox_means_nothing_runs_there_is_no_host_fallback(self) -> None:
        from pionir import build_sandbox as bs

        with mock.patch.object(bs, "load_setup", return_value=(None, "not set up")), \
                mock.patch("subprocess.run", side_effect=AssertionError("ran on the host")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("ran on the host")):
            with self.assertRaisesRegex(TutorialError, "nothing was run"):
                run_steps(parse_steps([spec()]))


class WriteRunsTests(Base):
    def pack(self) -> Path:
        path = self.root / "pack.json"
        path.write_text(json.dumps(pack_document()), encoding="utf-8")
        return path

    def test_runs_are_merged_and_a_rerun_replaces_the_same_id(self) -> None:
        path = self.pack()
        first = run_step(parse_steps([spec()])[0], setup=self.setup, spawner=Spawner())
        other = run_step(parse_steps([spec(id="two")])[0], setup=self.setup, spawner=Spawner())
        self.assertEqual(write_runs(path, [first, other]), 2)
        again = run_step(parse_steps([spec()])[0], setup=self.setup,
                         spawner=Spawner(out="tokens per second: 50\npeak vram: 7,000 MB\n"))
        self.assertEqual(write_runs(path, [again]), 2)
        runs = json.loads(path.read_text(encoding="utf-8"))["runs"]
        self.assertEqual(sorted(r["id"] for r in runs), ["bench", "two"])
        bench = next(r for r in runs if r["id"] == "bench")
        self.assertEqual(next(m for m in bench["measured"] if m["name"] == "speed")["value"], 50.0)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["topic"], "The old harbor")

    def test_a_missing_or_corrupt_pack_is_not_overwritten(self) -> None:
        with self.assertRaises(TutorialError):
            write_runs(self.root / "absent.json", [])
        bad = self.root / "bad.json"
        bad.write_text("{nope", encoding="utf-8")
        with self.assertRaises(TutorialError):
            write_runs(bad, [])
        self.assertEqual(bad.read_text(encoding="utf-8"), "{nope")

    def test_a_recorded_run_loads_and_exposes_itself_as_a_citable_passage(self) -> None:
        path = self.pack()
        run = run_step(parse_steps([spec()])[0], setup=self.setup, spawner=Spawner())
        write_runs(path, [run])
        pack = load_pack(path, make_niche(kind="tutorial", scene_mix={"run": 0.5, "card": 0.5}))
        passage = pack.passage("run-bench")
        self.assertIsNotNone(passage)
        self.assertIn("41.5", passage.text)
        self.assertIn("Exit code: 0", passage.text)


def tutorial_niche(**kw):
    return make_niche(kind="tutorial", scene_mix={"run": 0.5, "card": 0.5}, **kw)


def pack_with_runs(folder: Path, niche, *runs: dict) -> object:
    doc = pack_document()
    doc["runs"] = list(runs)
    path = folder / "pack.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return load_pack(path, niche)


def run_doc(id="bench", exit_code=0, timed_out=False, stdout=STDOUT.strip(), runner="sandbox"):
    return {"id": id, "title": "Measure speed", "argv": ["python", "bench.py"],
            "exit_code": exit_code, "timed_out": timed_out, "stdout": stdout,
            "measured": [{"name": "speed", "value": 41.5, "unit": "tok/s"}],
            "runner": runner, "ran_at": "2026-10-03T10:00:00+00:00"}


def tutorial_script(run="bench"):
    return {"title": "Speed on a small card", "summary": "A measured run.", "scenes": [
        {"type": "card", "heading": "The old harbor", "lines": [
            {"text": "The harbor opened in 1851.", "sources": ["p1"]}]},
        {"type": "run", "heading": "Run it", "run": run, "lines": [
            {"text": "It printed 41.5 tokens per second.", "sources": [f"run-{run}"]}]}]}


class ScriptGateTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.niche = tutorial_niche()

    def problems(self, pack, raw) -> list[str]:
        with self.assertRaises(ScriptRejected) as caught:
            check_script(raw, self.niche, pack)
        return caught.exception.problems

    def test_a_script_standing_on_a_successful_run_passes(self) -> None:
        pack = pack_with_runs(self.root, self.niche, run_doc())
        script = check_script(tutorial_script(), self.niche, pack)
        self.assertEqual([s.type for s in script.scenes], ["card", "run"])

    def test_a_run_that_was_never_run_cannot_be_shown(self) -> None:
        pack = pack_with_runs(self.root, self.niche, run_doc())
        found = self.problems(pack, tutorial_script(run="ghost"))
        self.assertTrue(any("not a run in the pack" in p for p in found), found)

    def test_a_failed_timed_out_or_silent_run_blocks_the_script(self) -> None:
        cases = {"exited 2": run_doc(exit_code=2),
                 "timed out": run_doc(exit_code=None, timed_out=True),
                 "printed nothing": run_doc(stdout="  ")}
        for how, doc in cases.items():
            pack = pack_with_runs(self.root, self.niche, doc)
            found = self.problems(pack, tutorial_script())
            self.assertTrue(any(how in p for p in found), (how, found))
            self.assertTrue(any("did not succeed" in p for p in found), (how, found))

    def test_one_failed_run_blocks_even_a_script_that_shows_a_good_one(self) -> None:
        pack = pack_with_runs(self.root, self.niche, run_doc(), run_doc(id="bad", exit_code=1))
        found = self.problems(pack, tutorial_script())
        self.assertTrue(any("'bad'" in p and "failed run" in p for p in found), found)

    def test_a_tutorial_with_no_run_scene_is_rejected(self) -> None:
        pack = pack_with_runs(self.root, self.niche, run_doc())
        raw = tutorial_script()
        raw["scenes"] = raw["scenes"][:1]
        found = self.problems(pack, raw)
        self.assertTrue(any("no run scene" in p for p in found), found)

    def test_a_number_the_run_did_not_print_is_not_narrated(self) -> None:
        pack = pack_with_runs(self.root, self.niche, run_doc())
        raw = tutorial_script()
        raw["scenes"][1]["lines"][0]["text"] = "It printed 87.5 tokens per second."
        found = self.problems(pack, raw)
        self.assertTrue(any("87.5" in p for p in found), found)

    def test_a_run_scene_must_cite_the_run_it_shows(self) -> None:
        pack = pack_with_runs(self.root, self.niche, run_doc())
        raw = tutorial_script()
        raw["scenes"][1]["lines"][0]["sources"] = ["p1"]
        found = self.problems(pack, raw)
        self.assertTrue(any("no line cites the run" in p for p in found), found)

    def test_only_a_run_scene_may_name_a_run(self) -> None:
        pack = pack_with_runs(self.root, self.niche, run_doc())
        raw = tutorial_script()
        raw["scenes"][0]["run"] = "bench"
        found = self.problems(pack, raw)
        self.assertTrue(any("only a run scene may name a run" in p for p in found), found)

    def test_a_niche_without_run_in_its_scene_mix_refuses_run_scenes(self) -> None:
        niche = make_niche(kind="tutorial", scene_mix={"card": 0.6, "timeline": 0.4})
        pack = pack_with_runs(self.root, niche, run_doc())
        with self.assertRaises(ScriptRejected) as caught:
            check_script(tutorial_script(), niche, pack)
        self.assertTrue(any("does not use run scenes" in p for p in caught.exception.problems))


class RunnerNameTests(Base):
    def test_a_live_niche_accepts_only_sandbox_recorded_runs(self) -> None:
        niche = tutorial_niche(live=True)
        with self.assertRaisesRegex(PassageError, "sandbox"):
            pack_with_runs(self.root, niche, run_doc(runner="hand-typed"))
        pack_with_runs(self.root, niche, run_doc(runner="sandbox"))

    def test_a_non_live_niche_may_hold_a_test_runner(self) -> None:
        niche = tutorial_niche(live=False)
        pack_with_runs(self.root, niche, run_doc(runner="hand-typed"))


class ShippedNicheTests(unittest.TestCase):
    def test_the_shipped_tutorial_niche_is_a_tutorial_that_uses_run_scenes_and_is_not_live(self) -> None:
        from pionir.video.niche import load_niches

        niche = {n.id: n for n in load_niches()}["local-ai-12gb"]
        self.assertEqual(niche.kind, "tutorial")
        self.assertIn("run", niche.scene_mix)
        self.assertFalse(niche.live)


if __name__ == "__main__":
    unittest.main()
