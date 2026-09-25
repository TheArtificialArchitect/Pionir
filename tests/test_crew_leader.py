"""Division leaders: a bounded brief, an honest abstention, a grounded report, capped
escalation to Claude.

The model is a fake (``FakeAsk`` or ``FakeOllama`` behind the real brain) and Claude is
``FakeClaude``. Each test fails if the behaviour it names is reverted: one kind of output
crowding another out of the brief, a blind division reporting (or calling the model)
instead of abstaining, a report with an invented figure reaching Moss, or Claude asked
past the daily cap or past a division's share.
"""

import json
import time
import unittest

from crew_support import FakeOllama, temp_dir
from test_crew_fakes import FakeClaude, ScriptedWorker, catalogue, make_crew

from pionir.crew.brief import DEFAULT_QUOTA, TOTAL_LIMIT, build_brief
from pionir.crew.leader import REPORT_SCHEMA, Abstention, Leader, Report
from pionir.crew.registry import WorkerSpec
from pionir.crew.result import Err, Ok
from pionir.crew.worker import make_output

DIVISIONS = {
    "alpha": [{"name": "ledger", "entities": ["Scrooge"]}],
    "posting": [{"name": "blog", "impl": "placeholder", "provider": "none", "kind": "post"},
                {"name": "insta", "impl": "placeholder", "provider": "none", "kind": "post"}],
    "watch": [{"name": "health", "kind": "uptime", "params": {"mode": "err"}}],
}


def reply(summary="Revenue is $12.00 so far this month, recorded by the ledger.",
          figures=({"value": 1200, "unit": "usd_cents", "measures": "revenue"},),
          escalate=False, question="", attention="none") -> str:
    return json.dumps({"headline": "Revenue steady", "summary": summary,
                       "attention": attention, "figures": list(figures), "routine": [],
                       "escalate": escalate, "question": question})


class FakeAsk:
    def __init__(self, text: str | None = None, err: str | None = None) -> None:
        self.text = text if text is not None else reply()
        self.err = err
        self.calls: list = []

    def __call__(self, agent_id, purpose, messages, options, *, fmt=None, division=None):
        self.calls.append({"agent": agent_id, "messages": messages, "options": options,
                           "fmt": fmt, "division": division})
        return (None if self.err else self.text), {"prompt_eval_count": 9}, self.err


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = temp_dir()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name

    def crew(self, divisions=DIVISIONS, **kw):
        crew = make_crew(self.root, cat=catalogue(divisions, known=("Moss",)), **kw)
        self.addCleanup(crew.stop)
        return crew

    def leader(self, crew, division="alpha", ask=None) -> Leader:
        return Leader(division, crew.registry, crew.store, ask=ask or FakeAsk(),
                      escalator=crew.escalator, model="gemma3:12b")


class BriefTests(_Case):
    def test_one_prolific_kind_cannot_crowd_another_out(self) -> None:
        crew = self.crew({"alpha": [{"name": "ledger"},
                                    {"name": "chatty", "kind": "metric"}]})
        crew.dispatcher.dispatch(only=["alpha.ledger"], wait=True)     # one revenue row, oldest
        chatty = crew.registry.require("alpha.chatty")
        now = time.time()
        for i in range(60):
            out = make_output(chatty, valid_at=now + i, observed_at=now + i,
                              payload={"i": i}, figures=[{"value": i, "unit": "count",
                                                          "measures": "views"}],
                              provenance={"source": "real"})
            crew.store.record_attempt(worker_id=chatty.worker_id, division="alpha",
                                      started_at=now + i, finished_at=now + i, error=None,
                                      outputs=[out])
        brief = build_brief(crew.store, crew.registry, "alpha", now=now + 61)
        kinds = [o.kind for o in brief.outputs]
        self.assertIn("revenue", kinds)                      # the one that matters survives
        self.assertLessEqual(kinds.count("metric"), DEFAULT_QUOTA)
        self.assertLessEqual(len(brief.outputs), TOTAL_LIMIT)

    def test_the_brief_is_stamped_with_its_stalest_input(self) -> None:
        crew = self.crew({"alpha": [{"name": "old"}, {"name": "new"}]})
        t0 = time.time()
        for wid, t in (("alpha.old", t0 - 500), ("alpha.new", t0 - 10)):
            w = crew.registry.require(wid)
            crew.store.record_attempt(
                worker_id=wid, division="alpha", started_at=t, finished_at=t, error=None,
                outputs=[make_output(w, valid_at=t, observed_at=t, payload={},
                                     provenance={"source": "real"})])
        brief = build_brief(crew.store, crew.registry, "alpha", now=t0)
        self.assertEqual(brief.stamp, t0 - 500)

    def test_a_model_written_output_backs_no_figure(self) -> None:
        crew = self.crew({"alpha": [{"name": "drafter"}]})
        w = crew.registry.require("alpha.drafter")
        t = time.time()
        crew.store.record_attempt(worker_id=w.worker_id, division="alpha", started_at=t,
                                  finished_at=t, error=None, outputs=[make_output(
                                      w, valid_at=t, observed_at=t, payload={},
                                      figures=[{"value": 99900, "unit": "usd_cents",
                                                "measures": "revenue"}],
                                      provenance={"derived": True, "model": "x"})])
        brief = build_brief(crew.store, crew.registry, "alpha", now=t)
        self.assertNotIn(99900, [f.value for f in brief.recorded_figures()])


class AbstentionTests(_Case):
    def test_a_division_of_placeholders_abstains_blocking_without_a_model_call(self) -> None:
        crew = self.crew()
        crew.dispatcher.dispatch(wait=True)
        ask = FakeAsk()
        result = self.leader(crew, "posting", ask).run()
        self.assertIsInstance(result, Ok)
        self.assertIsInstance(result.value, Abstention)
        self.assertTrue(result.value.blocking)
        self.assertIn("not wired", result.value.reason)
        self.assertEqual(ask.calls, [])
        row = crew.store.reports(division="posting")[0]
        self.assertEqual((row["status"], row["blocking"]), ("abstained", True))

    def test_a_blind_division_abstains_blocking_and_says_why(self) -> None:
        crew = self.crew()
        crew.dispatcher.dispatch(wait=True)
        ask = FakeAsk()
        result = self.leader(crew, "watch", ask).run()
        self.assertTrue(result.value.blocking)
        self.assertIn("could not see", result.value.reason)
        self.assertIn("watch.health", result.value.reason)
        self.assertEqual(ask.calls, [])

    def test_nothing_new_is_a_non_blocking_abstention(self) -> None:
        crew = self.crew()
        crew.dispatcher.dispatch(only=["alpha.ledger"], wait=True)
        ask = FakeAsk()
        lead = self.leader(crew, "alpha", ask)
        self.assertIsInstance(lead.run().value, Report)
        second = lead.run().value
        self.assertIsInstance(second, Abstention)
        self.assertFalse(second.blocking)
        self.assertIn("nothing new", second.reason)
        self.assertEqual(len(ask.calls), 1)                  # no second model call
        # and Moss still sees the standing report, marked as nothing new since
        entry = next(e for e in crew.direction.digest()["divisions"] if e["division"] == "alpha")
        self.assertEqual(entry["headline"], "Revenue steady")
        self.assertIn("nothing_new_since_s", entry)


class ReportTests(_Case):
    def test_a_report_is_distilled_by_the_local_brain_in_distiller_style(self) -> None:
        post = FakeOllama(reply=reply())
        crew = self.crew(post=post)
        crew.dispatcher.dispatch(only=["alpha.ledger"], wait=True)
        crew.brain.start()
        result = crew.leaders["alpha"].run()
        self.assertIsInstance(result, Ok)
        self.assertIsInstance(result.value, Report)
        _url, body = post.calls[0]
        self.assertEqual(body["options"]["temperature"], 0)
        self.assertEqual(body["format"], REPORT_SCHEMA)
        row = crew.store.reports(division="alpha")[0]
        self.assertEqual(row["status"], "report")
        self.assertEqual(row["figures"], [{"value": 1200, "unit": "usd_cents",
                                           "measures": "revenue"}])
        self.assertEqual(row["provenance"]["model"], crew.cfg.model)
        self.assertEqual(len(row["provenance"]["prompt_digest"]), 16)
        self.assertEqual(crew.store.calls_last_hour("alpha"), 1)   # charged to the division

    def test_a_report_stating_an_unbacked_figure_is_rejected(self) -> None:
        crew = self.crew()
        crew.dispatcher.dispatch(only=["alpha.ledger"], wait=True)
        ask = FakeAsk(reply(summary="Revenue reached $470 this month."))
        result = self.leader(crew, "alpha", ask).run()
        self.assertIsInstance(result, Err)
        self.assertEqual(result.error.kind, "ungrounded")
        row = crew.store.reports(division="alpha")[0]
        self.assertEqual(row["status"], "rejected")
        self.assertIn("$470", row["reason"])                 # the record says exactly why
        digest = json.dumps(crew.direction.digest())
        self.assertNotIn("470", digest)                      # Moss never sees the number
        self.assertIn("rejected (unbacked figure)", digest)

    def test_a_structured_figure_in_the_wrong_unit_is_rejected(self) -> None:
        crew = self.crew()
        crew.dispatcher.dispatch(only=["alpha.ledger"], wait=True)
        ask = FakeAsk(reply(figures=({"value": 1200, "unit": "count", "measures": "revenue"},)))
        self.assertIsInstance(self.leader(crew, "alpha", ask).run(), Err)

    def test_a_name_nothing_recorded_is_rejected(self) -> None:
        crew = self.crew()
        crew.dispatcher.dispatch(only=["alpha.ledger"], wait=True)
        ask = FakeAsk(reply(summary="Revenue is $12 so far, all of it from Etsy."))
        result = self.leader(crew, "alpha", ask).run()
        self.assertIsInstance(result, Err)
        self.assertIn("Etsy", crew.store.reports(division="alpha")[0]["reason"])

    def test_malformed_model_output_is_recorded_not_reported(self) -> None:
        crew = self.crew()
        crew.dispatcher.dispatch(only=["alpha.ledger"], wait=True)
        result = self.leader(crew, "alpha", FakeAsk('{"headline": 3}')).run()
        self.assertEqual(result.error.kind, "malformed")
        self.assertEqual(crew.store.reports(division="alpha")[0]["status"], "rejected")

    def test_the_leader_run_is_recorded_like_a_workers(self) -> None:
        crew = self.crew()
        crew.dispatcher.dispatch(only=["alpha.ledger"], wait=True)
        self.leader(crew, "alpha", FakeAsk(err="refused: share used up")).run()
        run = crew.store.runs("leader.alpha")[0]
        self.assertEqual((run["outcome"], run["error_kind"]), ("err", "no_words"))


class EscalationTests(_Case):
    def test_the_daily_cap_holds_across_all_leaders(self) -> None:
        claude = FakeClaude()
        crew = self.crew(claude=claude, claude_daily_cap=2)
        got = [crew.escalator.escalate(d, "hard?", "facts") for d in ("alpha", "watch", "posting")]
        self.assertEqual([type(g).__name__ for g in got], ["Ok", "Ok", "Err"])
        self.assertIn("daily Claude cap of 2", got[2].error)
        self.assertEqual(len(claude.prompts), 2)             # the third never reached Claude

    def test_a_failed_call_still_spends_its_slot(self) -> None:
        claude = FakeClaude(error=RuntimeError("usage limit"))
        crew = self.crew(claude=claude, claude_daily_cap=1)
        self.assertIsInstance(crew.escalator.escalate("alpha", "q", "c"), Err)
        self.assertIn("daily Claude cap", crew.escalator.escalate("watch", "q", "c").error)

    def test_a_division_cannot_exceed_its_share(self) -> None:
        claude = FakeClaude()
        crew = self.crew(claude=claude, claude_daily_cap=4)
        crew.direction.allocate("claude_escalations", {"alpha": 0.25})
        self.assertIsInstance(crew.escalator.escalate("alpha", "q", "c"), Ok)
        second = crew.escalator.escalate("alpha", "q", "c")
        self.assertIsInstance(second, Err)
        self.assertIn("alpha's share", second.error)
        self.assertIsInstance(crew.escalator.escalate("watch", "q", "c"), Ok)
        self.assertEqual(len(claude.prompts), 2)

    def test_a_leader_attaches_claudes_answer_or_why_not(self) -> None:
        claude = FakeClaude(answer="Keep the price; nothing recorded argues otherwise.")
        crew = self.crew(claude=claude, claude_daily_cap=1)
        crew.dispatcher.dispatch(only=["alpha.ledger"], wait=True)
        ask = FakeAsk(reply(escalate=True, question="Should we change the price?"))
        report = self.leader(crew, "alpha", ask).run().value
        self.assertEqual(report.escalation["answer"], claude.answer)
        self.assertIn("Should we change the price?", claude.prompts[0])

    def test_claudes_invented_figure_is_withheld(self) -> None:
        crew = self.crew(claude=FakeClaude(answer="Aim for $5,000 next month."),
                         claude_daily_cap=1)
        crew.dispatcher.dispatch(only=["alpha.ledger"], wait=True)
        report = self.leader(crew, "alpha", FakeAsk(reply(escalate=True, question="q?"))).run()
        self.assertIsNone(report.value.escalation["answer"])
        self.assertIn("withheld", report.value.escalation)

    def test_escalation_is_off_with_a_zero_cap(self) -> None:
        claude = FakeClaude()
        crew = self.crew(claude=claude, claude_daily_cap=0)
        self.assertIsInstance(crew.escalator.escalate("alpha", "q", "c"), Err)
        self.assertEqual(claude.prompts, [])


class WordsTests(_Case):
    def test_a_worker_gets_words_only_through_the_shared_brain(self) -> None:
        post = FakeOllama(reply='{"title": "Hello"}')
        crew = self.crew(post=post)
        crew.brain.start()
        w = ScriptedWorker(WorkerSpec("alpha.x", "x", "alpha", "scripted", "post", 60, "p"))
        ctx = crew.context_for(w)
        got = ctx.words("draft", "system", "user", {"type": "object"})
        self.assertEqual(got.value, {"title": "Hello"})
        _url, body = post.calls[0]
        self.assertEqual((body["options"]["temperature"], body["format"]), (0, {"type": "object"}))
        self.assertEqual(crew.store.calls_last_hour("alpha"), 1)


if __name__ == "__main__":
    unittest.main()
