"""The posting workers' Claude fallback: one draft a day, after the local model keeps failing.

The blog worker inside a real crew (its real escalator, a fake Claude runner, a fake brain, a
fake Pionir). Each test fails if its rule is reverted: Claude asked before the local model has
failed ``claude_after_blocked`` times today, more than ``claude_drafts_per_day`` a day, a
Claude draft that skips the check or the approval, the crew-wide Claude cap ignored, a model
other than the pinned Sonnet-class one, or Claude reached from a context with no Claude.
"""

import json
import tempfile
import unittest
from pathlib import Path

from crew_support import temp_dir
from test_crew_blog import FakeBrain, FakeHands, bad, good
from test_crew_fakes import FakeHttp, make_crew

from pionir.crew import blog as blog_module
from pionir.crew import contentcheck, escalation
from pionir.crew.registry import build_registry, default_registry, load_catalogue
from pionir.crew.result import Ok
from pionir.crew.worker import WorkContext

T0 = 1_790_000_000.0


class ModelRunner:
    """A review runner that takes ``model=`` like the real one, and answers ``answer``."""

    def __init__(self, answer) -> None:
        self.answer = answer
        self.calls: list = []

    def __call__(self, prompt, timeout, *, model=None):
        self.calls.append({"prompt": prompt, "timeout": timeout, "model": model})
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = temp_dir()
        self.addCleanup(tmp.cleanup)
        cat = load_catalogue()
        cat["divisions"] = [d for d in cat["divisions"] if d["id"] == "posting"]
        self.crew = make_crew(tmp.name, registry=build_registry(cat), http=FakeHttp())
        self.addCleanup(self.crew.stop)
        self.worker = self.crew.registry.require("posting.blog")
        self.runner = ModelRunner(json.dumps(good(slug="claude-wrote-this",
                                                  title="Check an address before you send")))
        self.crew.escalator.review_runner = self.runner
        self.hands = FakeHands()
        self.state = Path(tmp.name) / "state"

    def run_at(self, now, brain):
        ctx = self.crew.context_for(self.worker)
        ctx = WorkContext(**{**ctx.__dict__, "now": now, "words": brain, "job": self.hands.job,
                             "approval": self.hands.approval, "state_dir": self.state})
        return self.worker.run(ctx)

    def record(self) -> dict:
        return json.loads(self.worker.record_path(self.state).read_text(encoding="utf-8"))


class FallbackTests(Case):
    def test_after_three_blocked_local_drafts_claude_drafts_once_and_it_is_parked(self) -> None:
        self.run_at(T0, FakeBrain(bad(), bad(), bad()))
        (call,) = self.runner.calls
        self.assertEqual(call["model"], "claude-sonnet-5")
        self.assertIn("Hard rules", call["prompt"])                 # the same rules
        self.assertIn("Jane Doe", call["prompt"])                   # and why the last failed
        (job,) = self.hands.jobs
        self.assertEqual(job.capability, "content.publish")         # the same approval gate
        self.assertIn("drafted by Claude", job.what)
        self.assertEqual(contentcheck.check(job.payload), [])       # the same check
        rec = self.record()
        (post,) = rec["posts"]
        self.assertEqual((post["status"], post["drafted_by"]), ("pending_approval", "claude"))
        (entry,) = rec["claude_drafts"]
        self.assertEqual((entry["outcome"], entry["after_blocked"]), ("submitted", 3))
        self.assertEqual(rec["counts"]["claude_drafts"], 1)
        # the crew-wide Claude ledger counted it too
        day = escalation.local_day(self.crew.escalator._clock())
        self.assertEqual(self.crew.store.escalations_on(day), 1)

    def test_never_before_the_local_model_has_failed_enough_today(self) -> None:
        self.run_at(T0, FakeBrain(bad(), bad(), good()))
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(len(self.hands.jobs), 1)

    def test_at_most_one_claude_draft_a_day_and_a_new_day_is_the_way_back(self) -> None:
        self.runner.answer = json.dumps(bad())                      # Claude's draft fails too
        brain = FakeBrain(*[bad()] * 30)
        for hours in (0, 6, 12, 18):
            self.run_at(T0 + hours * 3600, brain)
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.hands.jobs, [])                       # blocked: never submitted
        rec = self.record()
        self.assertEqual(rec["claude_drafts"][0]["outcome"], "blocked")
        self.assertTrue(any(b.get("drafted_by") == "claude" for b in rec["blocked"]))
        self.run_at(T0 + 2 * 86400, brain)          # the next day's draft is due
        self.assertEqual(len(self.runner.calls), 2)

    def test_the_crew_wide_claude_cap_stops_it(self) -> None:
        self.crew.escalator.daily_cap = 0
        self.run_at(T0, FakeBrain(bad(), bad(), bad()))
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.record()["claude_drafts"][0]["outcome"], "failed")
        self.assertIn("off", self.record()["claude_drafts"][0]["why"])

    def test_a_failing_claude_call_is_recorded_not_raised(self) -> None:
        self.runner.answer = RuntimeError("claude -p exited 1")
        result = self.run_at(T0, FakeBrain(bad(), bad(), bad()))
        self.assertIsInstance(result, Ok)
        self.assertEqual(self.hands.jobs, [])
        self.assertIn("claude -p exited 1", self.record()["claude_drafts"][0]["why"])

    def test_zero_a_day_is_off(self) -> None:
        self.worker.claude_drafts_per_day = 0
        self.run_at(T0, FakeBrain(bad(), bad(), bad()))
        self.assertEqual(self.runner.calls, [])


class WiringTests(unittest.TestCase):
    def test_a_context_without_claude_never_reaches_it(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            w = default_registry().require("posting.blog")
            hands = FakeHands()
            w.run(WorkContext(now=T0, http=None, secrets_dir=Path(d),
                              words=FakeBrain(bad(), bad(), bad()), job=hands.job,
                              approval=hands.approval, state_dir=Path(d)))
            rec = json.loads(w.record_path(Path(d)).read_text(encoding="utf-8"))
            self.assertNotIn("claude_drafts", rec)
            self.assertEqual(hands.jobs, [])

    def test_the_catalogue_turns_it_on_for_the_blog_only_with_the_pinned_model(self) -> None:
        reg = default_registry()
        blog = reg.require("posting.blog")
        self.assertEqual((blog.claude_drafts_per_day, blog.claude_after_blocked,
                          blog.claude_model), (1, 3, escalation.DEFAULT_CLAUDE_MODEL))
        self.assertEqual(blog_module.CLAUDE_MODEL, escalation.DEFAULT_CLAUDE_MODEL)
        self.assertEqual(reg.require("posting.instagram").claude_drafts_per_day, 0)

    def test_claude_is_reached_only_from_the_one_function_and_never_with_the_api_key(self) -> None:
        import ast
        tree = ast.parse(Path(blog_module.__file__).read_text(encoding="utf-8"))
        where = []
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef):
                for node in ast.walk(fn):
                    if isinstance(node, ast.ImportFrom) and any(
                            a.name == "claudedraft" for a in node.names):
                        where.append(fn.name)
        self.assertEqual(where, ["_claude_draft"])
        self.assertIn("ANTHROPIC_API_KEY", escalation.STRIPPED_ENV)
        self.assertNotIn("--tools", [a for a in escalation.review_argv("x")
                                     if a.startswith("--tools=")])
        argv = escalation.review_argv("claude-sonnet-5")
        self.assertEqual(argv[argv.index("--tools") + 1], "")       # no tools at all
        self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-5")

    def test_a_bad_model_id_is_refused_at_build(self) -> None:
        from types import SimpleNamespace
        spec = SimpleNamespace(worker_id="posting.blog", impl="blog_writer", name="blog",
                               division="posting", kind="post", cadence_seconds=21600,
                               provider="none", stage=0, entities=(), note="")
        blog_module.BlogWorker(spec)                                # the defaults build
        with self.assertRaises(ValueError):
            blog_module.BlogWorker(spec, claude_model="--dangerously-skip-permissions")
        with self.assertRaises(ValueError):
            blog_module.BlogWorker(spec, claude_drafts_per_day=-1)


if __name__ == "__main__":
    unittest.main()
