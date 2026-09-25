"""Workers: one job each, no model, a Result on every path, every run recorded.

Every network is a fake. Each test fails if the behaviour it names is reverted: an
exception escaping a worker (or escaping unrecorded), a worker that has never succeeded
reading as healthy, a success that produced nothing passing unremarked, the ledger
inventing a zero when it has no token, or a figure recorded without its unit.
"""

import ast
import json
import time
import unittest
from pathlib import Path

from crew_support import temp_dir
from test_crew_fakes import FakeHttp, ScriptedWorker, catalogue, make_crew

from pionir.crew import workers as workers_module
from pionir.crew.registry import WorkerSpec
from pionir.crew.result import Err, Ok
from pionir.crew.worker import ErrorKind, WorkContext
from pionir.crew.workers import HealthWorker, LedgerWorker, Placeholder

LEDGER_URL = "https://api.dokaz.net/dash/api.json"
HEALTH_URL = "https://api.dokaz.net/health"

SUMMARY = {"all_time_net": 123456, "mtd_net": 1200, "last30_net": 4700, "last30_gross": 5000,
           "by_stream_30": [{"stream": "api", "net": 4700, "sales": 12}],
           "by_stream_all": [], "recent": [], "run_rate_month": 4700, "target_month": 400000}


def spec(name="w", division="treasury", **kw) -> WorkerSpec:
    base = {"worker_id": f"{division}.{name}", "name": name, "division": division,
            "impl": "x", "kind": "revenue", "cadence_seconds": 60, "provider": "dokaz"}
    return WorkerSpec(**{**base, **kw})


def ctx(http=None, secrets: Path | None = None) -> WorkContext:
    return WorkContext(now=time.time(), http=http or FakeHttp(),
                       secrets_dir=secrets or Path("does-not-exist"))


class _CrewCase(unittest.TestCase):
    def crew(self, divisions, **kw):
        tmp = temp_dir()
        self.addCleanup(tmp.cleanup)
        crew = make_crew(tmp.name, cat=catalogue(divisions), **kw)
        self.addCleanup(crew.stop)
        return crew


class NeverRaisesTests(_CrewCase):
    def test_an_exception_inside_a_worker_becomes_a_typed_err(self) -> None:
        w = ScriptedWorker(spec(), mode="raise")
        result = w.run(ctx())
        self.assertIsInstance(result, Err)
        self.assertEqual(result.error.kind, ErrorKind.UNAVAILABLE)
        self.assertIn("KeyError", result.error.message)

    def test_the_err_is_recorded_as_a_failed_run(self) -> None:
        crew = self.crew({"alpha": [{"name": "boom", "params": {"mode": "raise"}}]})
        report = crew.dispatcher.dispatch(only=["alpha.boom"], wait=True)
        self.assertEqual((report.attempted, report.failed), (1, 1))
        run = crew.store.runs("alpha.boom")[0]
        self.assertEqual(run["outcome"], "err")
        self.assertEqual(run["error_kind"], "unavailable")

    def test_even_an_undecorated_worker_cannot_escape_unrecorded(self) -> None:
        crew = self.crew({"alpha": [{"name": "raw", "params": {"mode": "raw_raise"}},
                                    {"name": "fine"}]})
        report = crew.dispatcher.dispatch(wait=True)
        self.assertEqual((report.attempted, report.succeeded, report.failed), (2, 1, 1))
        run = crew.store.runs("alpha.raw")[0]
        self.assertEqual(run["outcome"], "err")
        self.assertIn("escaped the contract", run["error_message"])

    def test_a_placeholder_says_not_wired_and_never_fakes_output(self) -> None:
        result = Placeholder(spec(note="posting to Instagram")).run(ctx())
        self.assertIsInstance(result, Err)
        self.assertEqual(result.error.kind, ErrorKind.NOT_WIRED)
        self.assertIn("not wired yet", result.error.message)


class HealthTests(_CrewCase):
    def test_a_worker_that_has_never_succeeded_is_reported_as_such(self) -> None:
        crew = self.crew({"alpha": [{"name": "dead", "params": {"mode": "err"}},
                                    {"name": "unrun"}]})
        for _ in range(3):
            crew.dispatcher.dispatch(only=["alpha.dead"], wait=True)
        health = {h.worker_id: h for h in crew.store.health(crew.registry.cadences(),
                                                            time.time())}
        self.assertTrue(health["alpha.dead"].has_never_succeeded)
        self.assertEqual(health["alpha.dead"].attempts, 3)
        self.assertEqual(health["alpha.dead"].consecutive_failures, 3)
        # a worker that has never even run is present - never succeeded - not absent
        self.assertTrue(health["alpha.unrun"].has_never_succeeded)
        self.assertEqual(health["alpha.unrun"].attempts, 0)
        open_checks = {(v["who"], v["check"]) for v in crew.vitals.check(force=True)}
        self.assertIn(("alpha.dead", "never"), open_checks)

    def test_a_success_that_produced_nothing_is_flagged(self) -> None:
        crew = self.crew({"alpha": [{"name": "hush", "params": {"mode": "silent"}}]})
        report = crew.dispatcher.dispatch(only=["alpha.hush"], wait=True)
        self.assertEqual(report.succeeded, 1)
        self.assertTrue(report.silent_success)
        h = crew.store.health(crew.registry.cadences(), time.time())[0]
        self.assertFalse(h.has_never_succeeded)
        self.assertTrue(h.silent_success)
        for _ in range(2):
            crew.dispatcher.dispatch(only=["alpha.hush"], wait=True)
        h = crew.store.health(crew.registry.cadences(), time.time())[0]
        self.assertEqual(h.silent_streak, 3)
        self.assertIn(("alpha.hush", "silent"),
                      {(v["who"], v["check"]) for v in crew.vitals.check(force=True)})

    def test_a_success_that_wrote_something_is_not_silent_and_clears_the_warning(self) -> None:
        crew = self.crew({"alpha": [{"name": "w"}]})
        w = crew.registry.require("alpha.w")
        w.mode = "silent"
        for _ in range(3):
            crew.dispatcher.dispatch(only=["alpha.w"], wait=True)
        self.assertTrue(crew.vitals.check(force=True))
        w.mode = "ok"
        report = crew.dispatcher.dispatch(only=["alpha.w"], wait=True)
        self.assertFalse(report.silent_success)
        self.assertEqual(report.written, 1)
        h = crew.store.health(crew.registry.cadences(), time.time())[0]
        self.assertEqual(h.silent_streak, 0)
        self.assertEqual(crew.vitals.check(force=True), [])
        self.assertEqual(crew.vitals.cleared, 1)


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = temp_dir()
        self.addCleanup(tmp.cleanup)
        self.secrets = Path(tmp.name) / "secrets"
        self.worker = LedgerWorker(spec("ledger", entities=("Scrooge",)), url=LEDGER_URL,
                                   token_file="scrooge-read-token.txt")

    def token(self, value="abc123") -> None:
        self.secrets.mkdir(parents=True, exist_ok=True)
        (self.secrets / "scrooge-read-token.txt").write_text(value, encoding="utf-8")

    def test_no_token_file_is_not_configured_never_zero(self) -> None:
        http = FakeHttp({LEDGER_URL: (200, {"summary": SUMMARY})})
        result = self.worker.run(ctx(http, self.secrets))
        self.assertIsInstance(result, Err)
        self.assertEqual(result.error.kind, ErrorKind.NOT_CONFIGURED)
        self.assertIn("UNKNOWN, not zero", result.error.message)
        self.assertEqual(http.calls, [])                       # did not even try
        self.assertIn("NOT CONFIGURED", self.worker.readiness(self.secrets))

    def test_no_token_file_records_no_row_and_no_zero(self) -> None:
        with temp_dir() as root:
            from pionir.crew.registry import default_registry
            crew = make_crew(root, registry=default_registry())
            try:
                crew.dispatcher.dispatch(only=["treasury.ledger"], wait=True)
                self.assertEqual(crew.store.count_outputs("treasury"), 0)
                h = crew.store.health(crew.registry.cadences("treasury"), time.time())[0]
                self.assertTrue(h.not_configured)
                self.assertTrue(h.has_never_succeeded)
            finally:
                crew.stop()

    def test_an_empty_token_file_is_not_configured_either(self) -> None:
        self.token("  \n")
        result = self.worker.run(ctx(FakeHttp(), self.secrets))
        self.assertEqual(result.error.kind, ErrorKind.NOT_CONFIGURED)

    def test_revenue_is_recorded_as_typed_cents_with_the_token_sent(self) -> None:
        self.token()
        http = FakeHttp({LEDGER_URL: (200, {"summary": SUMMARY})})
        result = self.worker.run(ctx(http, self.secrets))
        self.assertIsInstance(result, Ok)
        self.assertEqual(http.calls[0][1], {"x-dash-token": "abc123"})
        (out,) = result.value
        figs = {(f.measures, f.stream, f.window): f for f in out.figures}
        mtd = figs[("revenue", "all", "mtd")]
        self.assertEqual((mtd.value, mtd.unit), (1200, "usd_cents"))
        self.assertEqual(figs[("revenue", "api", "last30")].value, 4700)
        sales = figs[("sales", "api", "last30")]
        self.assertEqual((sales.value, sales.unit), (12, "count"))
        self.assertEqual(out.provenance["source"], "real")
        self.assertFalse(out.derived)

    def test_a_missing_field_is_malformed_not_a_default_zero(self) -> None:
        self.token()
        broken = {k: v for k, v in SUMMARY.items() if k != "mtd_net"}
        result = self.worker.run(ctx(FakeHttp({LEDGER_URL: (200, {"summary": broken})}),
                                     self.secrets))
        self.assertIsInstance(result, Err)
        self.assertEqual(result.error.kind, ErrorKind.MALFORMED)

    def test_a_refused_token_is_an_auth_error(self) -> None:
        self.token()
        result = self.worker.run(ctx(FakeHttp({LEDGER_URL: (403, b"read-only token")}),
                                     self.secrets))
        self.assertEqual(result.error.kind, ErrorKind.AUTH)


class HealthWorkerTests(unittest.TestCase):
    def worker(self) -> HealthWorker:
        return HealthWorker(spec("health", division="watch", kind="uptime"), url=HEALTH_URL)

    def test_up_with_latency(self) -> None:
        body = {"ok": True, "products": {"a": "ok", "b": "ok"}, "at": "x"}
        (out,) = self.worker().run(ctx(FakeHttp({HEALTH_URL: (200, body)}))).value
        figs = {f.measures: f for f in out.figures}
        self.assertEqual((figs["up"].value, figs["up"].unit), (1, "boolean"))
        self.assertEqual((figs["latency"].value, figs["latency"].unit), (42, "ms"))
        self.assertEqual(figs["failing_products"].value, 0)

    def test_a_failing_product_is_down(self) -> None:
        body = {"ok": False, "products": {"a": "ok", "b": "FAIL: timeout"}}
        (out,) = self.worker().run(ctx(FakeHttp({HEALTH_URL: (503, body)}))).value
        figs = {f.measures: f for f in out.figures}
        self.assertEqual(figs["up"].value, 0)
        self.assertEqual(figs["status"].value, 503)
        self.assertEqual(out.payload["failing"], ["b"])

    def test_no_answer_at_all_is_recorded_as_down_not_skipped(self) -> None:
        (out,) = self.worker().run(ctx(FakeHttp())).value
        self.assertFalse(out.payload["reachable"])
        self.assertEqual({f.measures: f.value for f in out.figures}, {"up": 0})


class NoModelInWorkersTests(unittest.TestCase):
    def test_worker_modules_import_nothing_that_can_call_a_model(self) -> None:
        # ADR-0001 in Peter: "the 300th specialist is a config line". A worker reaches a
        # model only through ctx.words (the shared brain), never by importing one.
        for mod in (workers_module, __import__("pionir.crew.worker", fromlist=["x"])):
            tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
            names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    names.add(node.module or "")
                elif isinstance(node, ast.Import):
                    names |= {a.name for a in node.names}
            for bad in ("brain", "budget", "escalation", "leader", "urllib", "subprocess"):
                self.assertFalse(any(bad in n for n in names), (mod.__name__, bad, names))

    def test_payloads_are_json(self) -> None:
        body = {"ok": True, "products": {}}
        (out,) = HealthWorkerTests().worker().run(ctx(FakeHttp({HEALTH_URL: (200, body)}))).value
        json.dumps(out.payload)


if __name__ == "__main__":
    unittest.main()
