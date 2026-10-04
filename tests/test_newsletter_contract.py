"""The newsletter's JSON contract with Scrooge, held on Pionir's side.

``fixtures/scrooge_newsletter_contract.json`` is the same file as Scrooge's
``worker/test/fixtures/newsletter-contract.json``: Scrooge's test holds its real
``POST /dash/content/newsletter`` and ``/dash/api.json`` answers to it, and this one drives
Pionir's real code - the ``content.newsletter_send`` adapter and the ``posting.newsletter``
worker - with every document in it. A shape changed on one side alone fails here or there.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from typing import Any, Self

from test_crew_fakes import FakeHttp

from pionir.adapters import newsletter
from pionir.adapters.content import ContentSettings
from pionir.adapters.newsletter import (
    ROUTE,
    SEND,
    NewsletterAdapter,
    NewsletterSettings,
    check_newsletter,
)
from pionir.contracts import Task
from pionir.crew import contentcheck
from pionir.crew.newsletter import assemble, read_stats, sender_off
from pionir.crew.registry import default_registry
from pionir.crew.worker import WorkContext

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "scrooge_newsletter_contract.json"
CONTRACT = json.loads(FIXTURE.read_text(encoding="utf-8"))
TOKEN = "publishTOKENcontract0123456789"
DASH = "https://api.dokaz.net/dash/api.json"


class _Answer:
    def __init__(self, doc: Any) -> None:
        self.status = 200
        self._raw = json.dumps(doc).encode()

    def read(self, _n: int = -1) -> bytes:
        return self._raw

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class Scripted:
    """Scrooge answering with one of the contract's documents."""

    def __init__(self, status: int, doc: Any) -> None:
        self.status, self.doc = status, doc
        self.seen: list[dict[str, Any]] = []

    def __call__(self, request: Any, timeout: float | None = None) -> _Answer:
        self.seen.append({"url": request.full_url, "body": json.loads(request.data)})
        if self.status >= 400:
            raise urllib.error.HTTPError(request.full_url, self.status, "x", {},  # type: ignore[arg-type]
                                         io.BytesIO(json.dumps(self.doc).encode()))
        return _Answer(self.doc)


class AdapterContractTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.token_file = self.root / "token.txt"
        self.token_file.write_text(TOKEN, encoding="utf-8")
        newsletter._SENT.clear()
        self.addCleanup(newsletter._SENT.clear)

    def send(self, status: int, doc: Any) -> tuple[dict[str, Any], Scripted]:
        opener = Scripted(status, doc)
        adapter = NewsletterAdapter(NewsletterSettings(
            content=ContentSettings(base_url="https://api.dokaz.net", token_file=self.token_file),
            ledger_file=self.root / "sends.json"), opener=opener)
        out = adapter.execute(Task(SEND, CONTRACT["queue_request"])).output
        return dict(out), opener

    def ledger(self) -> dict[str, Any]:
        return json.loads((self.root / "sends.json").read_text(encoding="utf-8"))

    def test_the_request_is_one_pionir_sends_unchanged(self) -> None:
        request = CONTRACT["queue_request"]
        self.assertEqual(set(request), {"newsletter_id", "subject", "body_md"})
        self.assertEqual(check_newsletter(request), request)
        self.assertEqual(contentcheck.check_newsletter(request), [])
        _out, opener = self.send(200, CONTRACT["queue_response"])
        (seen,) = opener.seen
        self.assertEqual(seen, {"url": "https://api.dokaz.net" + ROUTE, "body": request})

    def test_the_200_answer_is_the_result_and_the_ledger_entry(self) -> None:
        answer = CONTRACT["queue_response"]
        out, _ = self.send(200, answer)
        self.assertEqual(out, answer)                        # every field, nothing added
        entry = self.ledger()[answer["newsletter_id"]]
        self.assertEqual((entry["send_id"], entry["recipients"], entry["queued_at"]),
                         (answer["send_id"], answer["recipients"], answer["queued_at"]))

    def test_the_409_answer_is_refused_and_recorded(self) -> None:
        answer = CONTRACT["duplicate_response"]
        out, _ = self.send(409, answer)
        self.assertEqual((out["ok"], out["refused"], out["send_id"]),
                         (False, answer["error"], answer["send_id"]))
        nid = CONTRACT["queue_request"]["newsletter_id"]
        self.assertEqual(self.ledger()[nid]["send_id"], answer["send_id"])

    def test_the_400_answer_is_refused_and_not_recorded(self) -> None:
        answer = CONTRACT["refused_response"]
        out, _ = self.send(400, answer)
        self.assertEqual((out["ok"], out["refused"]), (False, answer["error"]))
        self.assertFalse((self.root / "sends.json").exists())

    def test_the_503_answer_is_unavailable_with_scrooges_words(self) -> None:
        answer = CONTRACT["not_configured_response"]
        out, _ = self.send(503, answer)
        self.assertIs(out["ok"], False)
        self.assertIn(answer["error"], out["unavailable"])
        # typed: the worker settles it NOT CONFIGURED, never a generic failure to retry
        self.assertEqual((out["status"], out["not_configured"]), (503, True))
        self.assertFalse((self.root / "sends.json").exists())


class StatsContractTests(unittest.TestCase):
    def test_the_stats_documents_parse_to_the_counts_they_hold(self) -> None:
        for name in ("stats", "stats_empty"):
            with self.subTest(name):
                doc = CONTRACT[name]
                self.assertEqual(set(doc), {"subscribers", "sends", "last_send", "sender",
                                            "sent_today", "daily_cap"})
                stats, why = read_stats({"newsletter": doc})
                self.assertIsNone(why)
                self.assertEqual(stats["subscribers"], doc["subscribers"])
                self.assertEqual((stats["sends"], stats["sent_today"], stats["daily_cap"]),
                                 (doc["sends"], doc["sent_today"], doc["daily_cap"]))
                self.assertEqual(stats["sender"], doc["sender"])
        last = CONTRACT["stats"]["last_send"]
        self.assertEqual(set(last), {"newsletter_id", "send_id", "subject", "status",
                                     "recipients", "sent", "failed", "skipped", "queued_at",
                                     "finished_at"})
        parsed, _ = read_stats({"newsletter": CONTRACT["stats"]})
        for key in ("newsletter_id", "status", "recipients", "sent", "failed", "skipped",
                    "queued_at", "finished_at"):
            self.assertEqual(parsed["last_send"][key], last[key], key)
        self.assertIsNone(read_stats({"newsletter": CONTRACT["stats_empty"]})[0]["last_send"])

    def test_a_sender_scrooge_says_is_off_is_not_configured(self) -> None:
        stats, _why = read_stats({"newsletter": CONTRACT["stats_empty"]})
        self.assertIn(CONTRACT["stats_empty"]["sender"]["why"], sender_off(stats))
        stats, _why = read_stats({"newsletter": CONTRACT["stats"]})
        self.assertIsNone(sender_off(stats))

    def test_the_unavailable_document_is_said_never_zero(self) -> None:
        stats, why = read_stats({"newsletter": CONTRACT["stats_unavailable"]})
        self.assertIsNone(stats)
        self.assertIn(CONTRACT["stats_unavailable"]["error"], why)

    def test_the_worker_reads_each_document_from_the_dash(self) -> None:
        worker = default_registry().require("posting.newsletter")
        for name, kind in (("stats", "newsletter.audience"),
                           ("stats_empty", "newsletter.audience"),
                           ("stats_unavailable", "newsletter.audience_unavailable")):
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                state = Path(tmp)
                (state / "scrooge-read-token.txt").write_text("read", encoding="utf-8")
                http = FakeHttp({DASH: (200, {"summary": {}, "newsletter": CONTRACT[name]})})
                result = worker.run(WorkContext(now=1_790_000_000.0, http=http,
                                                secrets_dir=state, state_dir=state))
                # stats_empty's sender is off: the run is NOT CONFIGURED, its rows kept
                rows = result.error.partial if name == "stats_empty" else result.value
                if name == "stats_empty":
                    self.assertEqual(result.error.kind, "not_configured")
                (out,) = [o for o in rows if o.kind.startswith("newsletter.")]
                self.assertEqual(out.kind, kind)
                if kind == "newsletter.audience":
                    figs = {f.measures: f.value for f in out.figures}
                    subs = CONTRACT[name]["subscribers"]
                    self.assertEqual(figs["newsletter subscribers confirmed"], subs["confirmed"])
                    self.assertEqual(figs["newsletter subscribers unsubscribed"],
                                     subs["unsubscribed"])
                    self.assertEqual(figs["newsletters sent"], CONTRACT[name]["sends"])

    def test_what_the_worker_builds_has_the_requests_shape(self) -> None:
        built = assemble("weekly-2026-w40", [
            {"draft_id": "2026-10-01-qr", "slug": "qr-code-api-howto",
             "title": "How to make a QR code API call",
             "description": "A two-minute walk through the QR endpoint, with curl."}], [])
        self.assertEqual(set(built), set(CONTRACT["queue_request"]))
        self.assertEqual(built["newsletter_id"], CONTRACT["queue_request"]["newsletter_id"])
        self.assertEqual(check_newsletter(built), built)


if __name__ == "__main__":
    unittest.main()
