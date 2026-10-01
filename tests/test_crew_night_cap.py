"""The overnight Claude budget: the per-night cap inside the daily cap, and the model setting.

Nothing here reaches Claude: runners are fakes, ``subprocess.run`` is injected, and the crew's
store is a throw-away file.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from pionir.crew import escalation
from pionir.crew.config import CrewSettings
from pionir.crew.escalation import (
    DEFAULT_CLAUDE_MODEL,
    DEFAULT_NIGHT_CAP,
    Escalator,
    claude_review_runner,
    claude_site_runner,
    clean_model,
    night_key,
    review_argv,
    site_argv,
)
from pionir.crew.result import Ok
from pionir.crew.store import CrewStore


def at(day: int, hour: int, minute: int = 0) -> float:
    return datetime(2026, 10, day, hour, minute).timestamp()


class Shares:
    """Moss's allocation, stood in for: everyone may have the whole cap."""

    def cap(self, resource: str, division: str) -> int:
        return 100


class FakeClock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class PlainRunner:
    """A runner with the plain two-argument signature (as most test fakes have)."""

    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, prompt: str, timeout: float) -> str:
        self.calls.append(prompt)
        return "answer"


class ModelRunner:
    """A runner that takes ``model=`` (as the real ones do), recording what it was given."""

    def __init__(self) -> None:
        self.models: list = []

    def __call__(self, prompt: str, timeout: float, *, model=None) -> str:
        self.models.append(model)
        return "answer"


class Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = CrewStore(Path(tmp.name) / "crew.db")
        self.addCleanup(self.store.close)
        self.clock = FakeClock(at(1, 2))               # 02:00 on the 1st: in the night

    def escalator(self, **kw) -> Escalator:
        kw.setdefault("runner", PlainRunner())
        kw.setdefault("daily_cap", 10)
        kw.setdefault("review_runner", PlainRunner())
        kw.setdefault("site_runner", PlainRunner())
        return Escalator(self.store, Shares(), clock=self.clock, **kw)


class NightKeyTests(unittest.TestCase):
    def test_a_night_is_noon_to_noon(self) -> None:
        self.assertEqual(night_key(at(1, 23)), night_key(at(2, 5)))        # one night
        self.assertEqual(night_key(at(2, 11, 59)), night_key(at(1, 20)))   # still that night
        self.assertNotEqual(night_key(at(2, 12, 1)), night_key(at(2, 11)))  # a new one
        self.assertNotEqual(night_key(at(2, 3)), night_key(at(3, 3)))


class NightCapTests(Base):
    def test_three_calls_pass_and_the_fourth_waits(self) -> None:
        review = PlainRunner()
        site = PlainRunner()
        esc = self.escalator(night_cap=3, review_runner=review, site_runner=site)
        results = [esc.review("builds", "r1", night=True),
                   esc.build_site("products", "e1", night=True),
                   esc.review("products", "r2", night=True)]
        self.assertTrue(all(isinstance(r, Ok) for r in results), results)
        fourth = esc.review("builds", "r3", night=True)
        self.assertNotIsInstance(fourth, Ok)
        self.assertEqual(fourth.error.kind, "budget")
        self.assertTrue(fourth.error.waits)
        self.assertIn("night's Claude cap of 3", fourth.error.message)
        self.assertEqual(review.calls, ["r1", "r2"])         # Claude was not asked a 4th time
        self.assertEqual(site.calls, ["e1"])
        self.assertEqual(self.store.night_escalations(night_key(self.clock())), 3)

    def test_the_cap_is_per_night(self) -> None:
        esc = self.escalator(night_cap=1)
        self.assertIsInstance(esc.review("builds", "a", night=True), Ok)
        self.assertNotIsInstance(esc.review("builds", "b", night=True), Ok)
        self.clock.t = at(2, 3)                               # the next night
        self.assertIsInstance(esc.review("builds", "c", night=True), Ok)

    def test_daytime_calls_do_not_spend_the_night_cap_but_do_spend_the_daily_one(self) -> None:
        esc = self.escalator(night_cap=1, daily_cap=3)
        self.assertIsInstance(esc.review("fiverr", "client site review"), Ok)   # not night=True
        self.assertIsInstance(esc.build_site("fiverr", "client site"), Ok)
        self.assertIsInstance(esc.review("builds", "night one", night=True), Ok)
        self.assertNotIsInstance(esc.review("builds", "night two", night=True), Ok)  # night cap
        # the daily cap (3) is now spent too, whichever kind asks
        self.assertEqual(esc.review("fiverr", "another").error.kind, "budget")

    def test_the_night_cap_is_inside_the_daily_cap(self) -> None:
        esc = self.escalator(night_cap=9, daily_cap=2)
        self.assertIsInstance(esc.review("builds", "a", night=True), Ok)
        self.assertIsInstance(esc.review("builds", "b", night=True), Ok)
        got = esc.review("builds", "c", night=True)
        self.assertEqual(got.error.kind, "budget")
        self.assertIn("daily Claude cap of 2", got.error.message)

    def test_a_night_cap_of_zero_is_off_overnight_and_asks_nothing(self) -> None:
        review = PlainRunner()
        esc = self.escalator(night_cap=0, review_runner=review)
        got = esc.review("builds", "a", night=True)
        self.assertEqual((got.error.kind, got.error.waits), ("off", True))
        self.assertEqual(review.calls, [])
        self.assertIsInstance(esc.review("builds", "a"), Ok)         # daytime use is untouched

    def test_a_failed_overnight_call_still_spends_its_night_slot(self) -> None:
        def boom(prompt: str, timeout: float) -> str:
            raise RuntimeError("claude -p exited 1: boom")
        esc = self.escalator(night_cap=1, review_runner=boom)
        self.assertEqual(esc.review("builds", "a", night=True).error.kind, "failed")
        self.assertEqual(esc.review("builds", "b", night=True).error.kind, "budget")

    def test_negative_caps_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.escalator(night_cap=-1)

    def test_the_snapshot_reports_the_night(self) -> None:
        esc = self.escalator(night_cap=3)
        esc.review("builds", "a", night=True)
        snap = esc.snapshot()
        self.assertEqual((snap["night_cap"], snap["used_tonight"], snap["night_model"]),
                         (3, 1, DEFAULT_CLAUDE_MODEL))


class ModelSettingTests(Base):
    def test_the_default_is_a_pinned_sonnet_class_id(self) -> None:
        self.assertEqual(DEFAULT_CLAUDE_MODEL, "claude-sonnet-5")
        self.assertEqual(DEFAULT_NIGHT_CAP, 3)

    def test_overnight_calls_pass_the_model_and_daytime_calls_do_not(self) -> None:
        runner = ModelRunner()
        esc = self.escalator(review_runner=runner, night_model="claude-sonnet-5")
        esc.review("builds", "night", night=True)
        esc.review("fiverr", "day")
        self.assertEqual(runner.models, ["claude-sonnet-5", None])

    def test_a_plain_runner_is_still_called_without_a_model(self) -> None:
        runner = PlainRunner()
        esc = self.escalator(review_runner=runner)
        self.assertIsInstance(esc.review("builds", "night", night=True), Ok)
        self.assertEqual(runner.calls, ["night"])

    def test_the_argv_carries_the_model_only_when_one_is_given(self) -> None:
        self.assertNotIn("--model", review_argv())
        self.assertNotIn("--model", site_argv())
        for argv in (review_argv("claude-sonnet-5"), site_argv("claude-sonnet-5")):
            at_ = argv.index("--model")
            self.assertEqual(argv[at_ + 1], "claude-sonnet-5")
            self.assertEqual(argv[:2], ["claude", "-p"])
        # the locks do not move with the model
        self.assertEqual([a for a in review_argv("x") if a not in ("--model", "x")],
                         review_argv())

    def test_the_real_runners_put_the_model_on_the_command_line(self) -> None:
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"] = argv
            return mock.Mock(returncode=0, stderr="",
                             stdout=json.dumps({"result": "ok", "subtype": "success"}))

        self.assertEqual(claude_review_runner("p", 5.0, run=fake_run, model="claude-sonnet-5"),
                         "ok")
        self.assertEqual(seen["argv"], review_argv("claude-sonnet-5"))
        claude_site_runner("p", 5.0, run=fake_run, model="claude-sonnet-5")
        self.assertEqual(seen["argv"], site_argv("claude-sonnet-5"))

    def test_the_api_key_is_still_stripped(self) -> None:
        seen = {}

        def fake_run(argv, **kw):
            seen["env"] = kw["env"]
            return mock.Mock(returncode=0, stderr="",
                             stdout=json.dumps({"result": "ok", "subtype": "success"}))

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            claude_review_runner("p", 5.0, run=fake_run, model="claude-sonnet-5")
        self.assertNotIn("ANTHROPIC_API_KEY", seen["env"])

    def test_a_model_that_could_be_another_option_is_refused(self) -> None:
        for bad in ("--dangerously-skip-permissions", "a b", "x;y", "-m"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                clean_model(bad)
        self.assertIsNone(clean_model(""))
        self.assertIsNone(clean_model("default"))
        self.assertEqual(clean_model(" sonnet "), "sonnet")
        self.assertEqual(clean_model("claude-opus-4-1"), "claude-opus-4-1")


def settings(**kw) -> CrewSettings:
    return CrewSettings(state_dir=Path("state"), gpu_lock_path=Path("gpu.lock"), **kw)


class ConfigTests(unittest.TestCase):
    def test_defaults(self) -> None:
        cfg = settings()
        self.assertEqual((cfg.claude_night_cap, cfg.claude_model, cfg.claude_daily_cap),
                         (3, "claude-sonnet-5", 10))

    def test_environment_overrides(self) -> None:
        with mock.patch.dict(os.environ, {"PIONIR_CREW_CLAUDE_MODEL": "haiku",
                                          "PIONIR_CREW_CLAUDE_NIGHT_CAP": "2"}):
            cfg = CrewSettings.from_environment()
        self.assertEqual((cfg.claude_model, cfg.claude_night_cap), ("haiku", 2))

    def test_a_bad_value_is_refused_at_load(self) -> None:
        with self.assertRaises(ValueError):
            settings(claude_night_cap=-1)
        with self.assertRaises(ValueError):
            settings(claude_model="--evil")


class WiringTests(unittest.TestCase):
    def test_only_the_overnight_workers_are_night_workers(self) -> None:
        from pionir.crew.runtime import NIGHT_IMPLS
        self.assertEqual(NIGHT_IMPLS, {"daedalus_builds", "api_builder"})

    def test_the_real_workers_carry_their_impl_so_the_night_flag_reaches_them(self) -> None:
        from pionir.crew.registry import default_registry
        from pionir.crew.runtime import NIGHT_IMPLS
        night = {w.worker_id for w in default_registry().all()
                 if getattr(w, "impl", None) in NIGHT_IMPLS}
        self.assertEqual(night, {"products.api_builder", "builds.daedalus"})


if __name__ == "__main__":
    unittest.main()
