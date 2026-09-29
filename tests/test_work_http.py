"""The work log's HTTP door: the owner's signed surfaces only, bounded, audited, and never
a stack trace.

Real HTTP against the real handler (as Pionir Desktop speaks to it) over a temporary state
root - never ~/.pionir. Every route is tried unauthenticated, as a bot, as the phone, with a
wrong key and with a replayed signature; the writes are read back; the audit ledger is
checked for metadata only.
"""

import contextlib
import http.client
import json
import secrets
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

from standins import down_url

from pionir import auth
from pionir.bootstrap import build_runtime
from pionir.config import PionirSettings
from pionir.server import PionirApp, _make_handler

CANARY = "CANARY-zebra-note-words"


def _runtime(tmp: str):
    return build_runtime(PionirSettings(
        state_root=Path(tmp),
        atani_command=("pionir-test-no-such-binary",),
        galatea_url=down_url(),
        galatea_model_id="stub-model",
        embed_model=None,  # never reach the live Ollama embedder from a test
        daedalus_url=down_url(),
        melete_url=down_url(),
        crew_url=None,
        bryo_status_command=None,
        nyx_status_command=None,
        voodoo_status_command=None,
        evict_to_fit=False,
    ))


def _z(delta: timedelta) -> str:
    return (datetime.now(UTC) + delta).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


READS = ["/api/work/summary", "/api/work/jobs", "/api/work/sessions", "/api/work/log"]
WRITES = [("POST", "/api/work/timer/start", {"job": "x"}), ("POST", "/api/work/timer/stop", {"job": "x"}),
          ("POST", "/api/work/session", {"job": "x", "start": "a", "end": "b"}),
          ("POST", "/api/work/log", {"job": "x", "kind": "note", "text": "hi"}),
          ("POST", "/api/work/job", {"name": "x"}),
          ("DELETE", "/api/work/session?id=1", None), ("DELETE", "/api/work/log?id=1", None)]


class WorkOverHttp(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.app = PionirApp(_runtime(self._tmp.name))
        self.db = self.app.runtime.settings.worklog_path
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self.app))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.app.runtime.cortex.close()
        self._tmp.cleanup()

    # ---- wire ------------------------------------------------------------------------------
    def raw(self, method: str, path: str, body: bytes = b"", headers: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        headers = dict(headers or {})
        conn.putrequest(method, path, skip_host="Host" in headers)
        for key, value in headers.items():
            conn.putheader(key, value)
        if method != "GET":
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body if method != "GET" else None)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, json.loads(data.decode("utf-8") or "{}")

    def signed(self, method: str, path: str, doc: dict | None = None, client: str = "desktop", *,
               nonce: str | None = None, sign_path: str | None = None, token: str | None = None,
               body: bytes | None = None, **extra: str):
        payload = body if body is not None else (json.dumps(doc).encode() if doc is not None else b"")
        stamp = str(int(time.time()))
        once = nonce or secrets.token_hex(16)
        key = token if token is not None else self.app.auth.tokens[client]
        sig = auth.request_sig(key, method, sign_path or path, stamp, once, payload)
        headers = {"X-Pionir-Client": client, "X-Pionir-Ts": stamp, "X-Pionir-Nonce": once,
                   "X-Pionir-Sig": sig, "Content-Type": "application/json", **extra}
        return self.raw(method, path, payload, headers)

    def ok(self, method: str, path: str, doc: dict | None = None):
        status, out = self.signed(method, path, doc)
        self.assertEqual(status, 200, out)
        return out

    def bad(self, expect: int, method: str, path: str, doc: dict | None = None, **kw):
        status, out = self.signed(method, path, doc, **kw)
        self.assertEqual(status, expect, out)
        return out

    def job(self, name: str = "Data Annotation", **kw) -> dict:
        return self.ok("POST", "/api/work/job", {"name": name, "kind": "freelance", **kw})["job"]

    # ---- who may use it ---------------------------------------------------------------------
    def test_every_route_refuses_the_unauthenticated_and_every_non_owner(self) -> None:
        calls = [("GET", p, None) for p in READS] + WRITES
        for method, path, doc in calls:
            body = json.dumps(doc).encode() if doc is not None else b""
            head = {"Content-Type": "application/json"}
            label = f"{method} {path}"
            status, out = self.raw(method, path, body, head)
            self.assertEqual(status, 401, f"{label}: {out}")                   # loopback alone is not enough
            for bot in ("crew", "galatea", "atani", "phone"):
                status, out = self.raw(method, path, body,
                                       {**head, "Authorization": f"Bearer {self.app.auth.tokens[bot]}"})
                self.assertEqual(status, 403, f"{label} as {bot}: {out}")      # a bot / the phone may not
                status, out = self.signed(method, path, doc, bot)
                self.assertEqual(status, 403, f"{label} signed as {bot}: {out}")
            status, out = self.raw(method, path, body,
                                   {**head, "Authorization": f"Bearer {self.app.auth.tokens['desktop']}"})
            self.assertEqual(status, 401, f"{label}: {out}")                   # the desktop signs, never a bearer
            status, out = self.signed(method, path, doc, token="y" * 43)
            self.assertEqual(status, 401, f"{label} wrong key: {out}")
            status, out = self.signed(method, path, doc, sign_path="/api/work/summary?x=1")
            self.assertEqual(status, 401, f"{label} other path: {out}")
        self.assertFalse(self.db.exists(), "a refused request must not even create the work log")

    def test_a_signature_is_bound_to_its_body_and_used_once(self) -> None:
        good = {"name": "Day job", "kind": "employment"}
        signed_for = json.dumps({"name": "Day job", "kind": "employment", "hourly_rate_cents": 1}).encode()
        stamp, once = str(int(time.time())), "n" * 32
        sig = auth.request_sig(self.app.auth.tokens["desktop"], "POST", "/api/work/job", stamp, once, signed_for)
        status, out = self.raw("POST", "/api/work/job", json.dumps(good).encode(),
                               {"X-Pionir-Client": "desktop", "X-Pionir-Ts": stamp, "X-Pionir-Nonce": once,
                                "X-Pionir-Sig": sig, "Content-Type": "application/json"})
        self.assertEqual(status, 401, out)                                     # body swapped after signing
        self.assertFalse(self.db.exists())
        self.assertEqual(self.signed("POST", "/api/work/job", good, nonce="m" * 32)[0], 200)
        status, out = self.signed("POST", "/api/work/job", {"name": "Second", "kind": "employment"},
                                  nonce="m" * 32)
        self.assertEqual(status, 401, out)
        self.assertIn("replayed", out["reason"])

    def test_the_dashboard_session_is_an_owner_surface_too(self) -> None:
        status, out = self.raw("GET", "/api/work/jobs",
                               headers={"Authorization": f"Bearer {self.app.auth.tokens['dashboard']}"})
        self.assertEqual(status, 200, out)

    def test_the_host_content_type_and_origin_are_checked(self) -> None:
        self.bad(403, "GET", "/api/work/jobs", Host="evil.example")
        self.bad(403, "POST", "/api/work/job", {"name": "x"}, Host="evil.example")
        self.bad(403, "DELETE", "/api/work/session?id=1", Host="evil.example")
        self.bad(403, "POST", "/api/work/job", {"name": "x"}, **{"Content-Type": "text/plain"})
        self.bad(403, "POST", "/api/work/job", {"name": "x"}, Origin="http://evil.example")
        self.bad(403, "DELETE", "/api/work/session?id=1", Origin="http://evil.example")
        self.assertFalse(self.db.exists())

    # ---- what it does -----------------------------------------------------------------------
    def test_a_working_day(self) -> None:
        job = self.job(hourly_rate_cents=2500)
        day = self.ok("POST", "/api/work/job", {"name": "Day job", "kind": "employment"})["job"]
        self.assertEqual(day["hourly_rate_cents"], None)
        started = self.ok("POST", "/api/work/timer/start", {"job": "Data Annotation", "note": "batch 1"})["session"]
        self.assertTrue(started["open"])
        self.bad(409, "POST", "/api/work/timer/start", {"job": job["id"]})                  # already running
        self.bad(409, "POST", "/api/work/timer/start", {"job": "Day job"})                  # another job's timer
        self.ok("POST", "/api/work/timer/start", {"job": "Day job", "allow_concurrent": True})
        time.sleep(1.2)                                        # a timer must run for at least a second
        self.ok("POST", "/api/work/timer/stop", {"job": "Day job"})
        summary = self.ok("GET", "/api/work/summary")["summary"]
        self.assertEqual(len(summary["timers"]), 1)
        stopped = self.ok("POST", "/api/work/timer/stop", {"job": "Data Annotation"})
        self.assertEqual(stopped["session"]["id"], started["id"])
        added = self.ok("POST", "/api/work/session", {"job": "Day job", "start": _z(timedelta(hours=-5)),
                                                      "end": _z(timedelta(hours=-3)), "note": "morning"})["session"]
        self.assertEqual(added["seconds"], 7200)
        self.bad(409, "POST", "/api/work/session", {"job": "Day job", "start": _z(timedelta(hours=-4)),
                                                    "end": _z(timedelta(hours=-3, minutes=-30))})
        edited = self.ok("POST", "/api/work/session", {"id": added["id"], "end": _z(timedelta(hours=-2, minutes=-30)),
                                                       "note": "edited"})["session"]
        self.assertEqual((edited["seconds"], edited["note"]), (9000, "edited"))
        self.bad(400, "POST", "/api/work/session", {"id": added["id"], "job": "Day job"})     # a session keeps its job
        entry = self.ok("POST", "/api/work/log", {"job": "Data Annotation", "kind": "payout",
                                                  "amount_cents": 4500, "text": "March"})["entry"]
        self.assertEqual(entry["amount_cents"], 4500)
        self.ok("POST", "/api/work/log", {"job": "Data Annotation", "kind": "rubric", "text": "explain the why"})
        listed = self.ok("GET", "/api/work/sessions?job=Day%20job")["sessions"]
        self.assertEqual(len(listed), 2)
        self.assertIn(added["id"], [s["id"] for s in listed])
        self.assertTrue(all(s["job"] == "Day job" for s in listed))
        self.assertEqual(len(self.ok("GET", "/api/work/log?kind=payout")["entries"]), 1)
        self.assertEqual(len(self.ok("GET", "/api/work/log")["entries"]), 2)
        deleted = self.ok("DELETE", f"/api/work/session?id={added['id']}")["session"]
        self.assertTrue(deleted["deleted"])
        self.assertNotIn(added["id"], [s["id"] for s in self.ok("GET", "/api/work/sessions")["sessions"]])
        self.assertIn(added["id"], [s["id"] for s in self.ok("GET", "/api/work/sessions?deleted=1")["sessions"]])
        self.ok("DELETE", f"/api/work/log?id={entry['id']}")
        self.assertEqual(len(self.ok("GET", "/api/work/log?kind=payout")["entries"]), 0)
        updated = self.ok("POST", "/api/work/job", {"id": job["id"], "hourly_rate_cents": 2600, "active": True})["job"]
        self.assertEqual(updated["hourly_rate_cents"], 2600)
        self.assertEqual(len(self.ok("GET", "/api/work/jobs")["jobs"]), 2)
        self.assertIn("NDA", self.ok("GET", "/api/work/sessions")["reminder"])

    def test_a_job_with_no_rate_says_so(self) -> None:
        self.job("Unrated")
        summary = self.ok("GET", "/api/work/summary")["summary"]
        self.assertFalse(summary["jobs"][0]["rate_set"])
        self.assertIsNone(summary["jobs"][0]["periods"]["week"]["earned_cents"])

    def test_bad_work_errors_are_structured_with_the_right_status(self) -> None:
        self.job()
        self.assertEqual(self.bad(404, "POST", "/api/work/timer/start", {"job": "nope"})["error"], "not_found")
        self.assertEqual(self.bad(409, "POST", "/api/work/timer/stop", {"job": "Data Annotation"})["error"], "not_open")
        self.assertEqual(self.bad(409, "POST", "/api/work/job", {"name": "data annotation"})["error"], "conflict")
        self.assertEqual(self.bad(404, "DELETE", "/api/work/session?id=999")["error"], "not_found")
        out = self.bad(400, "POST", "/api/work/log", {"job": "Data Annotation", "kind": "note",
                                                       "text": "Prompt: write a poem"})
        self.assertEqual(out["error"], "looks_like_task")
        self.assertIn("NDA", out["reason"])
        for body in (self.bad(400, "POST", "/api/work/log", {"job": "Data Annotation", "kind": "note", "text": "x" * 650}),):
            self.assertEqual(body["error"], "too_long")
        self.assertNotIn("Traceback", json.dumps(out))

    # ---- bounds -----------------------------------------------------------------------------
    def test_inputs_are_bounded_and_unknowns_refused(self) -> None:
        self.job()
        big = json.dumps({"name": "x" * (workapi_max() + 10)}).encode()
        self.assertEqual(self.signed("POST", "/api/work/job", body=big)[0], 413)      # refused unread
        self.bad(400, "POST", "/api/work/job", {"name": "Extra", "admin": True})
        self.bad(400, "POST", "/api/work/timer/start", {"job": "Data Annotation", "note": "n" * 701})
        self.bad(400, "POST", "/api/work/timer/start", {"job": ["Data Annotation"]})
        self.bad(400, "POST", "/api/work/timer/start", {"job": True})
        self.bad(400, "POST", "/api/work/timer/start", {"job": "Data Annotation", "allow_concurrent": "yes"})
        self.bad(400, "POST", "/api/work/job", {"name": "Bad rate", "hourly_rate_cents": 12.5})
        self.bad(400, "POST", "/api/work/job", {"name": "Bad rate", "hourly_rate_cents": "2500"})
        self.bad(400, "POST", "/api/work/job", {"name": "Bad rate", "hourly_rate_cents": 10 ** 9})
        self.bad(400, "POST", "/api/work/job", {"name": "Odd", "active": False})               # active is for updates
        self.bad(400, "POST", "/api/work/log", {"job": "Data Annotation", "kind": "payout", "amount_cents": "12"})
        self.bad(400, "POST", "/api/work/log", {"job": "Data Annotation", "kind": "payout", "amount_cents": -1})
        self.bad(400, "POST", "/api/work/log", {"job": "Data Annotation", "kind": "gift", "text": "x"})
        self.bad(400, "POST", "/api/work/session", {"job": "Data Annotation", "start": "2026-03-11T09:00",
                                                    "end": "2026-03-11T10:00"})              # no zone
        self.bad(400, "POST", "/api/work/session", {"job": "Data Annotation", "start": _z(timedelta(hours=-30)),
                                                    "end": _z(timedelta(hours=-1))})          # over 16 hours
        self.bad(400, "POST", "/api/work/session", {"job": "Data Annotation", "start": 5, "end": 6})
        self.bad(400, "POST", "/api/work/session", {"start": _z(timedelta(hours=-3)), "end": _z(timedelta(hours=-2))})
        self.bad(400, "GET", "/api/work/sessions?limit=501")
        self.bad(400, "GET", "/api/work/sessions?limit=abc")
        self.bad(400, "GET", "/api/work/sessions?limit=1&limit=2")
        self.bad(400, "GET", "/api/work/sessions?from=2020-01-01T00:00:00Z&to=2026-03-11T00:00:00Z")   # over 800 days
        self.bad(400, "GET", "/api/work/sessions?from=2026-03-11T00:00:00")                            # no zone
        self.bad(400, "GET", "/api/work/sessions?job=" + "j" * 65)
        self.bad(400, "GET", "/api/work/sessions?deleted=yes")
        self.bad(400, "GET", "/api/work/log?kind=chat")
        self.bad(400, "GET", "/api/work/summary?x=1")
        self.bad(400, "GET", "/api/work/sessions?secret=1")
        self.bad(400, "DELETE", "/api/work/session")
        self.bad(400, "DELETE", "/api/work/session?id=1%3B%20DROP")
        self.bad(400, "DELETE", "/api/work/session?id=1&job=x")
        self.bad(400, "POST", "/api/work/job", body=b"[1, 2]")
        self.bad(400, "POST", "/api/work/job", body=b"{not json")
        self.bad(404, "GET", "/api/work/nothing")
        self.bad(404, "POST", "/api/work/summary", {})
        self.bad(404, "DELETE", "/api/work/job?id=1")
        self.assertEqual(self.ok("GET", "/api/work/jobs")["jobs"][0]["name"], "Data Annotation")
        self.assertEqual(len(self.ok("GET", "/api/work/jobs")["jobs"]), 1)

    def test_a_broken_store_is_a_503_with_no_stack_trace(self) -> None:
        self.job()
        self.db.write_bytes(b"this is not a database" * 100)
        status, out = self.signed("GET", "/api/work/summary")
        self.assertEqual(status, 503, out)
        self.assertEqual(out["error"], "store unavailable")
        self.assertNotIn("Traceback", json.dumps(out))
        self.assertNotIn(str(self.db), json.dumps(out))

    # ---- audit and secrets ---------------------------------------------------------------
    def test_writes_are_audited_by_route_and_id_never_by_content(self) -> None:
        self.job(hourly_rate_cents=2500)
        self.ok("POST", "/api/work/log", {"job": "Data Annotation", "kind": "note", "text": f"{CANARY} note"})
        self.ok("POST", "/api/work/log", {"job": "Data Annotation", "kind": "payout", "amount_cents": 98765,
                                          "text": f"{CANARY} pay"})
        s = self.ok("POST", "/api/work/session", {"job": "Data Annotation", "start": _z(timedelta(hours=-3)),
                                                  "end": _z(timedelta(hours=-2)), "note": f"{CANARY} s"})["session"]
        self.ok("DELETE", f"/api/work/session?id={s['id']}")
        self.bad(400, "POST", "/api/work/log", {"job": "Data Annotation", "kind": "note", "text": "x" * 700})
        self.ok("GET", "/api/work/summary")
        ledger = self.app.runtime.settings.audit_path.read_text(encoding="utf-8")
        events = [json.loads(line) for line in ledger.splitlines() if '"work.write"' in line]
        details = [e["detail"] for e in events]
        self.assertEqual(len(events), 5, details)             # job, note, payout, session, delete - no read, no refusal
        self.assertIn(f"session.delete session={s['id']}", details)
        self.assertTrue(all(e["agent_id"] == "desktop" for e in events))
        self.assertNotIn(CANARY, ledger)
        self.assertNotIn("98765", ledger)
        self.assertEqual(self.app.runtime.executive.audit_sink.verify()[0] > 0, True)   # the chain still verifies

    def test_a_client_token_in_a_note_is_never_stored_or_echoed(self) -> None:
        self.job()
        token = self.app.auth.tokens["crew"]
        out = self.ok("POST", "/api/work/log", {"job": "Data Annotation", "kind": "note", "text": f"key {token} here"})
        self.assertNotIn(token, json.dumps(out))
        self.assertNotIn(token.encode(), self.db.read_bytes())
        self.assertNotIn(token, json.dumps(self.ok("GET", "/api/work/log")))

    def test_an_answer_is_scrubbed_even_if_the_store_holds_a_secret(self) -> None:
        job = self.job()
        token = self.app.auth.tokens["crew"]
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:      # planted behind the door's back
            con.execute("INSERT INTO log(job_id, ts, kind, text, tz, created_ts) VALUES(?,?,?,?,?,?)",
                        (job["id"], "2026-03-11T09:00:00Z", "note", f"leaked {token}", "UTC",
                         "2026-03-11T09:00:00Z"))
        for path in ("/api/work/log", "/api/work/summary"):
            self.assertNotIn(token, json.dumps(self.ok("GET", path)))

    def test_a_secret_shaped_note_is_scrubbed_on_the_way_in(self) -> None:
        self.job()
        secret = "sk-" + "Zq8Xv3Lm2Np7Rt5Wy1Bc4Dh6"
        out = self.ok("POST", "/api/work/log", {"job": "Data Annotation", "kind": "note", "text": f"see {secret}"})
        self.assertNotIn(secret, json.dumps(out))
        self.assertNotIn(secret.encode(), self.db.read_bytes())

    def test_lookalike_digits_and_extreme_times_are_a_400_never_a_500(self) -> None:
        self.job()
        added = self.ok("POST", "/api/work/session", {"job": "Data Annotation", "start": _z(timedelta(hours=-3)),
                                                     "end": _z(timedelta(hours=-2))})["session"]
        for bad_id in ("%C2%B2", "%D9%A1", "%EF%BC%91", "%E2%82%82"):      # superscript two, Arabic-Indic one, ...
            for route in ("session", "log"):
                out = self.bad(400, "DELETE", f"/api/work/{route}?id={bad_id}")
                self.assertEqual(out["error"], "bad request")
                self.assertNotIn("Traceback", json.dumps(out))
            self.bad(400, "GET", f"/api/work/sessions?limit={bad_id}")
        self.assertEqual(self.ok("DELETE", f"/api/work/session?id={added['id']}")["session"]["id"], added["id"])
        extreme = ("9999-12-31T23:59:59-23:59", "0001-01-01T00:00:00+23:59", "2999-01-01T00:00:00Z",
                   "1969-12-31T23:59:59Z")
        for ts in extreme:
            self.bad(400, "POST", "/api/work/session", {"job": "Data Annotation", "start": ts, "end": _z(timedelta())})
            self.bad(400, "POST", "/api/work/session", {"job": "Data Annotation", "start": _z(timedelta(hours=-1)),
                                                         "end": ts})
            self.bad(400, "POST", "/api/work/log", {"job": "Data Annotation", "kind": "payout", "text": "x",
                                                     "amount_cents": 100, "ts": ts})
        self.ok("POST", "/api/work/timer/start", {"job": "Data Annotation"})
        self.bad(400, "POST", "/api/work/timer/stop", {"job": "Data Annotation", "end": extreme[0]})
        self.bad(404, "POST", "/api/work/timer/stop", {"job": "²"})     # not an id: a name that is not there
        for query in ("from=9999-12-31T23%3A59%3A59-23%3A59", "to=9999-12-31T23%3A59%3A59-23%3A59"):
            status, _ = self.signed("GET", f"/api/work/sessions?{query}")
            self.assertEqual(status, 400, query)


def workapi_max() -> int:
    from pionir import workapi
    return workapi.MAX_BODY


if __name__ == "__main__":
    unittest.main()
