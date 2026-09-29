"""The approval gate cannot be bypassed over HTTP.

The hole (a security review): the loopback API trusted the ``permissions`` a caller put in
its body, so any local process could POST ``coding.daedalus_solve`` with
``["daedalus.solve"]`` and it ran without the owner being asked. These tests speak real
HTTP to the handler, as that process would, and read the queue and the executive - not a
log line - to show what ran.
"""

import http.client
import json
import logging
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from standins import down_url

from pionir import auth
from pionir.auth import ClientAuth, ClientGrant, ensure_tokens, load_grants, token_path
from pionir.bootstrap import build_runtime
from pionir.config import PionirSettings
from pionir.contracts import AgentManifest, Capability, RiskLevel, Task, TaskResult
from pionir.server import PionirApp, _make_handler

ATTACK = {"capability": "coding.daedalus_solve", "permissions": ["daedalus.solve"],
          "payload": {"intent": "rewrite the gate", "repo": "pionir-sandbox", "dry_run": False}}
# Shaped like a token, belongs to no client. Built from pieces, never a real key's shape.
WRONG = "not-a-client-token" + "_" * 30


class Recorder:
    """A specialist with a read, a write and a privileged action; records what it ran."""

    def __init__(self) -> None:
        self.ran: list[Task] = []
        self._manifest = AgentManifest("gatecheck", "test", (
            Capability("gate.read", "a read"),
            Capability("gate.write", "a reversible write", risk=RiskLevel.REVERSIBLE_WRITE),
            Capability("gate.privileged", "a privileged act", risk=RiskLevel.PRIVILEGED,
                       required_permissions=frozenset({"gate.run"})),
        ))

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def execute(self, task: Task) -> TaskResult:
        self.ran.append(task)
        return TaskResult(task.task_id, "gatecheck", {"ok": True, "cap": task.capability}, ())


def _runtime(tmp: str):
    runtime = build_runtime(PionirSettings(
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
    return runtime


class GateOverHttp(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        runtime = _runtime(self._tmp.name)
        self.spec = Recorder()
        runtime.register(self.spec)
        self.app = PionirApp(runtime)
        self.app.auth.compat = True
        self.executed: list[Task] = []
        real = self.app.runtime.executive.execute

        def execute(task, *args, **kwargs):
            self.executed.append(task)
            return real(task, *args, **kwargs)

        patcher = mock.patch.object(self.app.runtime.executive, "execute", side_effect=execute)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self.app))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.app.runtime.cortex.close()
        self._tmp.cleanup()

    # ---- wire helpers ------------------------------------------------------------------
    def _raw(self, method: str, path: str, body: dict | None = None,
             headers: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        headers = dict(headers or {})
        conn.putrequest(method, path, skip_host="Host" in headers)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        if data is not None:
            headers.setdefault("Content-Type", "application/json")
            headers["Content-Length"] = str(len(data))
        for key, value in headers.items():
            conn.putheader(key, value)
        conn.endheaders(data)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        try:
            document = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            document = {"raw": raw[:200]}
        return response.status, document, response

    def post(self, path: str, body: dict, client: str | None = None, **headers: str):
        if client is not None:
            headers["Authorization"] = f"Bearer {self.app.auth.tokens[client]}"
        status, document, _ = self._raw("POST", path, body, headers)
        return status, document

    def ran(self, capability: str) -> list[Task]:
        return [t for t in self.executed if t.capability == capability]

    # ---- 1. the reviewer's exact attack ------------------------------------------------
    def test_the_reported_attack_without_a_token_is_refused_and_nothing_runs(self) -> None:
        status, out = self.post("/api/task", ATTACK)
        self.assertEqual(status, 401, out)
        self.assertEqual(self.ran("coding.daedalus_solve"), [])
        self.assertEqual(self.app.approvals.pending(), [])      # not even parked

    def test_the_reported_attack_with_any_client_token_parks_and_never_runs(self) -> None:
        for client in ("crew", "galatea", "atani", "dashboard"):
            status, out = self.post("/api/task", ATTACK, client)
            self.assertEqual(status, 200, (client, out))
            self.assertEqual(out["status"], "pending_approval", (client, out))
        self.assertEqual(self.ran("coding.daedalus_solve"), [])
        rows = self.app.approvals.pending()
        self.assertEqual(len(rows), 4)
        # the queue records the permission the ACTION needs, never the caller's list
        self.assertTrue(all(r["permissions"] == ["daedalus.solve"] for r in rows))
        self.assertEqual({r["requester"] for r in rows},
                         {"crew", "galatea", "atani", "dashboard"})

    def test_asserted_permissions_are_ignored_for_every_privileged_capability(self) -> None:
        body = {"capability": "gate.privileged", "permissions": ["gate.run"], "payload": {}}
        status, out = self.post("/api/task", body, "atani")
        self.assertEqual((status, out["status"]), (200, "pending_approval"), out)
        self.assertEqual(self.spec.ran, [])
        self.post("/api/route", {"request": "anything", "execute": True,
                                 "permissions": ["gate.run"]}, "dashboard")
        self.assertEqual(self.spec.ran, [])

    # ---- 2. approve / deny need the owner's credential -------------------------------
    def _parked(self, client: str = "atani") -> str:
        status, out = self.post("/api/task", {"capability": "gate.privileged", "payload": {}},
                                client)
        self.assertEqual((status, out["status"]), (200, "pending_approval"), out)
        return out["approval_id"]

    def test_approve_and_deny_without_or_with_a_wrong_token_are_401(self) -> None:
        aid = self._parked()
        for path in ("/api/approvals/approve", "/api/approvals/deny", "/api/approvals/digest"):
            status, out = self.post(path, {"id": aid})
            self.assertEqual(status, 401, (path, out))
            status, out = self.post(path, {"id": aid}, Authorization=f"Bearer {WRONG}")
            self.assertEqual(status, 401, (path, out))
            status, out = self.post(path, {"id": aid}, Authorization="Basic xyz")
            self.assertEqual(status, 401, (path, out))
        self.assertEqual(self.app.approvals.get(aid)["status"], "pending")
        self.assertEqual(self.spec.ran, [])

    def test_approve_stays_closed_even_in_the_compatibility_window(self) -> None:
        self.assertTrue(self.app.auth.compat)
        aid = self._parked()
        status, _ = self.post("/api/approvals/approve", {"id": aid})
        self.assertEqual(status, 401)
        self.assertEqual(self.spec.ran, [])

    def test_no_tasking_client_can_approve_or_deny(self) -> None:
        aid = self._parked()
        for client in ("crew", "galatea", "atani"):
            for path in ("/api/approvals/approve", "/api/approvals/deny"):
                status, out = self.post(path, {"id": aid}, client)
                self.assertEqual(status, 403, (client, path, out))
        self.assertEqual(self.app.approvals.get(aid)["status"], "pending")
        self.assertEqual(self.spec.ran, [])

    def test_a_client_cannot_approve_what_it_parked(self) -> None:
        aid = self._parked("dashboard")
        status, out = self.post("/api/approvals/approve", {"id": aid}, "dashboard")
        self.assertEqual(status, 403, out)
        self.assertEqual(out["error"]["type"], "SelfApproval")
        self.assertEqual(self.app.approvals.get(aid)["status"], "pending")
        self.assertEqual(self.spec.ran, [])
        # the owner's other surface can
        status, out = self.post("/api/approvals/approve", {"id": aid}, "phone")
        self.assertIn(status, (200, 202), out)
        self.assertTrue(self.app.jobs.wait(out["task_id"], 30))
        self.assertEqual([t.capability for t in self.spec.ran], ["gate.privileged"])

    def test_the_phone_approves_and_the_run_has_exactly_the_recorded_permission(self) -> None:
        aid = self._parked("galatea")
        status, out = self.post("/api/approvals/approve", {"id": aid}, "phone")
        self.assertIn(status, (200, 202), out)
        self.assertTrue(self.app.jobs.wait(out["task_id"], 30))
        self.assertEqual(len(self.spec.ran), 1)
        self.assertEqual(self.spec.ran[0].granted_permissions, frozenset({"gate.run"}))

    def test_the_phone_denies(self) -> None:
        aid = self._parked()
        status, out = self.post("/api/approvals/deny", {"id": aid}, "phone")
        self.assertEqual((status, out["status"]), (200, "denied"), out)
        self.assertEqual(self.spec.ran, [])

    # ---- 2b. Pionir Desktop: the owner's console, signed, never a bearer --------------
    def signed(self, path: str, body: dict | None, client: str = "desktop", *,
               token: str | None = None, ts: float | None = None, nonce: str | None = None,
               sign_path: str | None = None, sign_body: bytes | None = None,
               **extra: str):
        """POST `body` to `path`, signed as `client` (with its own token unless `token`)."""
        import secrets as _secrets
        import time as _time
        data = json.dumps(body).encode("utf-8") if body is not None else b""
        stamp = str(int(ts if ts is not None else _time.time()))
        once = nonce or _secrets.token_hex(16)
        key = token if token is not None else self.app.auth.tokens[client]
        sig = auth.request_sig(key, "POST", sign_path or path, stamp, once,
                               data if sign_body is None else sign_body)
        headers = {"X-Pionir-Client": client, "X-Pionir-Ts": stamp, "X-Pionir-Nonce": once,
                   "X-Pionir-Sig": sig, **extra}
        status, document, _ = self._raw("POST", path, body, headers)
        return status, document

    def test_the_desktop_approves_signed_and_the_run_has_exactly_the_recorded_permission(self) -> None:
        aid = self._parked("galatea")
        status, out = self.signed("/api/approvals/approve", {"id": aid})
        self.assertIn(status, (200, 202), out)
        self.assertTrue(self.app.jobs.wait(out["task_id"], 30))
        self.assertEqual(len(self.spec.ran), 1)
        self.assertEqual(self.spec.ran[0].granted_permissions, frozenset({"gate.run"}))
        self.assertEqual(self.app.approvals.get(aid)["status"], "approved")

    def test_the_desktop_denies_signed(self) -> None:
        aid = self._parked()
        status, out = self.signed("/api/approvals/deny", {"id": aid})
        self.assertEqual((status, out["status"]), (200, "denied"), out)
        self.assertEqual(self.spec.ran, [])

    def test_the_desktop_token_as_a_bearer_is_refused_everywhere(self) -> None:
        aid = self._parked()
        for path, body in (("/api/approvals/approve", {"id": aid}), ("/api/approvals/deny", {"id": aid}),
                           ("/api/task", {"capability": "gate.read", "payload": {}}),
                           ("/api/intent", {"request": "hm"})):
            status, out = self.post(path, body, "desktop")
            self.assertEqual(status, 401, (path, out))
        self.assertEqual(self.app.approvals.get(aid)["status"], "pending")
        self.assertEqual(self.spec.ran, [])

    def test_a_bad_signature_never_approves(self) -> None:
        import time as _time
        aid = self._parked()
        body = {"id": aid}
        tries = {
            "another client's token": dict(token=self.app.auth.tokens["phone"]),
            "a made-up token": dict(token=WRONG),
            "stale": dict(ts=_time.time() - 120),
            "from the future": dict(ts=_time.time() + 120),
            "from before this server started": dict(ts=self.app.auth.booted - 5),
            "another path signed": dict(sign_path="/api/approvals/deny"),
            "another body signed": dict(sign_body=json.dumps({"id": "other"}).encode()),
            "short nonce": dict(nonce="abc"),
        }
        for why, kw in tries.items():
            status, out = self.signed("/api/approvals/approve", body, **kw)
            self.assertEqual(status, 401, (why, out))
        # signed as a client that may not approve: authenticated, then refused
        status, out = self.signed("/api/approvals/approve", body, "crew")
        self.assertEqual(status, 403, out)
        # an unknown client name
        status, out = self.signed("/api/approvals/approve", body, "desktop", **{"X-Pionir-Client": "ghost"})
        self.assertEqual(status, 401, out)
        self.assertEqual(self.app.approvals.get(aid)["status"], "pending")
        self.assertEqual(self.spec.ran, [])

    def test_a_signed_request_is_taken_once(self) -> None:
        aid = self._parked()
        status, out = self.signed("/api/approvals/deny", {"id": aid}, nonce="n" * 32)
        self.assertEqual(status, 200, out)
        aid2 = self._parked()
        status, out = self.signed("/api/approvals/deny", {"id": aid2}, nonce="n" * 32)
        self.assertEqual(status, 401, out)
        self.assertEqual(out["reason"], "replayed signature")
        self.assertEqual(self.app.approvals.get(aid2)["status"], "pending")

    def test_the_desktop_cannot_approve_what_it_parked(self) -> None:
        status, out = self.signed("/api/task", {"capability": "gate.privileged", "payload": {}})
        self.assertEqual((status, out["status"]), (200, "pending_approval"), out)
        aid = out["approval_id"]
        self.assertEqual(self.app.approvals.get(aid)["requester"], "desktop")
        status, out = self.signed("/api/approvals/approve", {"id": aid})
        self.assertEqual(status, 403, out)
        self.assertEqual(out["error"]["type"], "SelfApproval")
        self.assertEqual(self.spec.ran, [])

    def test_the_desktop_asks_pionir_like_the_dashboard(self) -> None:
        status, out = self.signed("/api/task", {"capability": "gate.read", "payload": {}})
        self.assertEqual(status, 200, out)
        self.assertTrue(out["ok"], out)
        # asserted permissions are ignored for it too: a privileged ask parks
        status, out = self.signed("/api/task", {**ATTACK})
        self.assertEqual(out["status"], "pending_approval", out)
        self.assertEqual(self.ran("coding.daedalus_solve"), [])

    # ---- 3. in-process approval (the Discord gate) -----------------------------------
    def test_in_process_approval_runs_with_the_recorded_permission(self) -> None:
        aid = self._parked("atani")
        out = self.app.approve(aid, wait=30)          # what the Discord gate calls
        self.assertEqual(out["status"], "approved", out)
        self.assertEqual(len(self.spec.ran), 1)
        self.assertEqual(self.spec.ran[0].granted_permissions, frozenset({"gate.run"}))

    # ---- 4. each token, its grant only -----------------------------------------------
    def test_each_client_reaches_only_its_granted_routes(self) -> None:
        read = {"capability": "gate.read", "payload": {}}
        for client in ("crew", "galatea", "atani", "dashboard"):
            status, out = self.post("/api/task", read, client)
            self.assertEqual(status, 200, (client, out))
            self.assertTrue(out["ok"], (client, out))
        for client in ("phone",):
            status, out = self.post("/api/task", read, client)
            self.assertEqual(status, 403, (client, out))
        for client in ("crew", "atani", "phone"):
            status, out = self.post("/api/intent", {"request": "hm"}, client)
            self.assertEqual(status, 403, (client, out))
        for client in ("crew", "galatea", "atani", "phone"):
            status, out = self.post("/api/route", {"request": "hm", "execute": False}, client)
            self.assertEqual(status, 403, (client, out))
        self.assertEqual(len(self.spec.ran), 4)

    def test_a_narrowed_grant_refuses_capabilities_outside_it(self) -> None:
        self.app.auth.grants = {**self.app.auth.grants,
                                "crew": ClientGrant(frozenset({auth.TASK}), ("gate.read",))}
        status, out = self.post("/api/task", {"capability": "gate.read", "payload": {}}, "crew")
        self.assertEqual(status, 200, out)
        status, out = self.post("/api/task", {"capability": "gate.write", "payload": {}}, "crew")
        self.assertEqual(status, 403, out)
        self.assertEqual(out["error"]["type"], "NotGranted")
        self.assertEqual([t.capability for t in self.spec.ran], ["gate.read"])
        # the galatea client is untouched by the crew's narrowing
        status, out = self.post("/api/task", {"capability": "gate.write", "payload": {}},
                                "galatea")
        self.assertEqual(status, 200, out)

    # ---- 5. the compatibility window -------------------------------------------------
    def test_no_token_non_privileged_is_served_with_a_warning_while_the_window_is_open(self):
        with self.assertLogs("pionir.auth", logging.WARNING) as logs:
            status, out = self.post("/api/task", {"capability": "gate.read", "payload": {}})
        self.assertEqual(status, 200, out)
        self.assertTrue(out["ok"])
        self.assertIn("UNAUTHENTICATED", "\n".join(logs.output))

    def test_no_token_privileged_is_refused_even_while_the_window_is_open(self) -> None:
        status, out = self.post("/api/task", {"capability": "gate.privileged", "payload": {}})
        self.assertEqual(status, 401, out)
        self.assertEqual(self.app.approvals.pending(), [])
        self.assertEqual(self.spec.ran, [])

    def test_the_window_closed_refuses_every_tokenless_post(self) -> None:
        self.app.auth.compat = False
        status, _ = self.post("/api/task", {"capability": "gate.read", "payload": {}})
        self.assertEqual(status, 401)
        status, _ = self.post("/api/route", {"request": "hm", "execute": False})
        self.assertEqual(status, 401)
        status, _ = self.post("/api/intent", {"request": "hm"})
        self.assertEqual(status, 401)
        self.assertEqual(self.spec.ran, [])
        status, _ = self.post("/api/task", {"capability": "gate.read", "payload": {}}, "crew")
        self.assertEqual(status, 200)

    def test_a_wrong_token_is_refused_even_while_the_window_is_open(self) -> None:
        status, _ = self.post("/api/task", {"capability": "gate.read", "payload": {}},
                              Authorization=f"Bearer {WRONG}")
        self.assertEqual(status, 401)
        self.assertEqual(self.spec.ran, [])

    def test_a_tokenless_intent_never_runs_the_privileged_voice_seam(self) -> None:
        # a request that names a doer goes to manager.atani_manage, privileged
        status, out = self.post("/api/intent", {"request": "ask nyx to scan the box"})
        self.assertEqual(status, 401, out)
        self.assertEqual(self.ran("manager.atani_manage"), [])

    # ---- 6. the owner's dashboard session --------------------------------------------
    def _session(self) -> dict:
        """Sign in the way the dashboard page does: /?code=, keep the cookie, take the
        proof from the redirect's #fragment into X-Session-Proof."""
        status, _doc, response = self._raw("GET", f"/?code={self.app.signin_codes.mint()}")
        self.assertEqual(status, 303)
        return {"Cookie": response.getheader("Set-Cookie").split(";", 1)[0],
                "X-Session-Proof": response.getheader("Location").split("#proof=", 1)[1]}

    def test_the_dashboard_link_sets_a_strict_http_only_session(self) -> None:
        key = self.app.auth.tokens["dashboard"]
        code = self.app.signin_codes.mint()
        self.assertNotIn(key, code)
        status, _doc, response = self._raw("GET", f"/?code={code}")
        self.assertEqual(status, 303)
        location = response.getheader("Location")
        self.assertTrue(location.startswith("/#proof="), location[:12])   # the proof in the fragment
        proof = location.split("#proof=", 1)[1]
        cookie = response.getheader("Set-Cookie")
        for part in ("HttpOnly", "SameSite=Strict", f"{auth.SESSION_COOKIE}="):
            self.assertIn(part, cookie)
        for secret in list(self.app.auth.tokens.values()) + [proof]:
            self.assertNotIn(secret, cookie)                  # a random id, never the token
        # the session approves (the owner's page), and runs what the dashboard may
        aid = self._parked("atani")
        session = {"Cookie": cookie.split(";", 1)[0], "X-Session-Proof": proof}
        status, out = self.post("/api/approvals/approve", {"id": aid}, **session)
        self.assertIn(status, (200, 202), out)
        # used once
        status, _doc, response = self._raw("GET", f"/?code={code}")
        self.assertEqual(status, 401)
        self.assertIsNone(response.getheader("Set-Cookie"))

    def test_pionir_opens_its_browser_with_a_one_time_code(self) -> None:
        url = self.app.signin_url(f"http://127.0.0.1:{self.port}/")
        self.assertTrue(url.startswith(f"http://127.0.0.1:{self.port}/?code="), url[:30])
        for token in self.app.auth.tokens.values():
            self.assertNotIn(token, url)
        status, _doc, response = self._raw("GET", url.split(str(self.port), 1)[1])
        self.assertEqual(status, 303)
        self.assertIn("HttpOnly", response.getheader("Set-Cookie"))

    def test_the_cookie_alone_is_no_session(self) -> None:
        # cookies ignore the port: a server another local user runs on 127.0.0.1:5555 is
        # sent this one - it must be worth nothing there, even with a forged Origin
        s = self._session()
        other = self._session()
        aid = self._parked()
        origin = {"Origin": f"http://127.0.0.1:{self.port}"}
        for headers in ({"Cookie": s["Cookie"]},
                        {"Cookie": s["Cookie"], "X-Session-Proof": "p" * 43},
                        {"Cookie": s["Cookie"], "X-Session-Proof": other["X-Session-Proof"]}):
            status, _ = self.post("/api/approvals/approve", {"id": aid}, **headers, **origin)
            self.assertEqual(status, 401, headers.keys())
            status, _out, _ = self._raw("GET", "/api/voice_link", headers=headers)
            self.assertEqual(status, 401)
        # and the cookie carries no token to lift: the dashboard token as a cookie is refused
        status, _ = self.post("/api/approvals/approve", {"id": aid}, **origin,
                              Cookie=f"{auth.SESSION_COOKIE}={self.app.auth.tokens['dashboard']}",
                              **{"X-Session-Proof": s["X-Session-Proof"]})
        self.assertEqual(status, 401)
        self.assertEqual(self.app.approvals.get(aid)["status"], "pending")

    def test_signing_out_and_expiry_end_the_session(self) -> None:
        s = self._session()
        aid = self._parked()
        status, out = self.post("/api/logout", {}, **s)
        self.assertEqual(status, 200, out)
        status, _ = self.post("/api/approvals/approve", {"id": aid}, **s)
        self.assertEqual(status, 401)
        s = self._session()
        clock = self.app.auth._clock
        self.app.auth._clock = lambda: clock() + auth.SESSION_TTL + 1
        try:
            status, _ = self.post("/api/approvals/approve", {"id": aid}, **s)
            self.assertEqual(status, 401)
        finally:
            self.app.auth._clock = clock

    def test_a_stale_code_is_no_session(self) -> None:
        from pionir.signin import CODE_TTL, SigninCodes
        codes = SigninCodes(clock=lambda: 1000.0)
        code = codes.mint()
        codes._clock = lambda: 1000.0 + CODE_TTL + 1
        self.assertFalse(codes.redeem(code))
        self.assertLessEqual(CODE_TTL, 60)

    def test_no_long_lived_key_signs_in_from_a_url(self) -> None:
        # the dashboard token itself in the address bar is no sign-in any more
        for q in (f"key={self.app.auth.tokens['dashboard']}", f"code={self.app.auth.tokens['dashboard']}"):
            _status, _doc, response = self._raw("GET", f"/?{q}")
            self.assertIsNone(response.getheader("Set-Cookie"), q[:4])

    def _ask_code(self, key: str | None = None, *, nonce: str | None = None, ts: float | None = None,
                  port: int | None = None, **headers: str):
        import secrets as _s
        import time as _t
        from pionir.signin import sign
        nonce = nonce or _s.token_hex(16)
        ts = int(_t.time() if ts is None else ts)
        sig = sign(key or self.app.auth.tokens["dashboard"], "signin", nonce, ts, port or self.port)
        status, out = self.post("/api/signin_code", {"nonce": nonce, "ts": ts},
                                **{"X-Pionir-Sign": sig, **headers})
        return status, out, nonce

    def test_a_launcher_signed_with_the_dashboard_token_gets_a_code_and_the_proof(self) -> None:
        from pionir.signin import same, sign
        status, out, nonce = self._ask_code()
        self.assertEqual(status, 200, out)
        self.assertTrue(same(out["proof"], sign(self.app.auth.tokens["dashboard"], "signin-reply",
                                                nonce, out["code"], self.port)))
        self.assertNotIn(self.app.auth.tokens["dashboard"], json.dumps(out))
        status, _doc, response = self._raw("GET", f"/?code={out['code']}")
        self.assertEqual(status, 303)
        self.assertIn("HttpOnly", response.getheader("Set-Cookie"))

    def test_every_other_ask_for_a_code_is_refused(self) -> None:
        import time as _t
        status, _out, nonce = self._ask_code()
        self.assertEqual(status, 200)
        self.assertEqual(self._ask_code(nonce=nonce)[0], 403)                     # replay
        for c in ("crew", "galatea", "phone", "desktop"):
            self.assertEqual(self._ask_code(self.app.auth.tokens[c])[0], 403, c)  # another client's
        self.assertEqual(self._ask_code(ts=_t.time() - 120)[0], 403)              # stale
        self.assertEqual(self._ask_code(port=self.port + 1)[0], 403)              # another port
        self.assertEqual(self._ask_code(Origin=f"http://127.0.0.1:{self.port}")[0], 403)  # a page
        status, _ = self.post("/api/signin_code", {"nonce": "n" * 32, "ts": int(_t.time())})
        self.assertEqual(status, 403)

    def test_a_wrong_key_or_another_clients_token_is_no_session(self) -> None:
        status, _doc, response = self._raw("GET", f"/?code={WRONG}")
        self.assertEqual(status, 401)
        self.assertIsNone(response.getheader("Set-Cookie"))
        status, _doc, _ = self._raw("GET", f"/?code={self.app.auth.tokens['crew']}")
        self.assertEqual(status, 401)
        aid = self._parked()
        for value in (WRONG, self.app.auth.tokens["crew"], self.app.auth.tokens["phone"]):
            status, _ = self.post("/api/approvals/approve", {"id": aid},
                                  Cookie=f"{auth.SESSION_COOKIE}={value}")
            self.assertEqual(status, 401, value[:4])
        self.assertEqual(self.spec.ran, [])

    # ---- 7. DNS rebinding and cross-site pages ---------------------------------------
    def test_a_rebound_host_can_neither_read_nor_write(self) -> None:
        for path in ("/", "/api/state", "/api/approvals", "/api/audit",
                     f"/?code={self.app.signin_codes.mint()}"):
            status, out, response = self._raw("GET", path, headers={"Host": "evil.example"})
            self.assertEqual(status, 403, path)
            self.assertIsNone(response.getheader("Set-Cookie"))
        aid = self._parked()
        status, out = self.post("/api/approvals/approve", {"id": aid}, "phone",
                                Host=f"evil.example:{self.port}")
        self.assertEqual(status, 403, out)
        self.assertEqual(self.app.approvals.get(aid)["status"], "pending")

    def test_a_foreign_origin_is_refused_even_with_the_owner_session(self) -> None:
        aid = self._parked()
        status, _ = self.post("/api/approvals/approve", {"id": aid}, **self._session(),
                              Origin="http://evil.example")
        self.assertEqual(status, 403)
        self.assertEqual(self.app.approvals.get(aid)["status"], "pending")

    # ---- 8. her glass: the Voice view's key goes to the owner's session only ----------
    def _glass(self) -> str:
        key = "g" * 43
        folder = self.app.runtime.settings.client_token_path
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "galatea-glass-token.txt").write_text(key, encoding="utf-8")
        return key

    def _her(self, key: str, *, honest: bool = True) -> str:
        """A stand-in for her /api/ticket on a free port: signs as she does, or - a
        squatter - answers without knowing her key. Points the runtime at it."""
        import dataclasses
        from http.server import BaseHTTPRequestHandler
        from pionir.signin import same, sign
        seen = self.her_asks = []

        class Her(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append({"headers": dict(self.headers), "body": body})
                port = self.server.server_address[1]
                ok = same(self.headers.get("X-Galatea-Sign"),
                          sign(key, "ticket", body["nonce"], body["ts"], port))
                ticket = "t" * 32
                proof = sign(key if honest else "not-her-key" * 4, "ticket-reply", body["nonce"], ticket, port)
                data = json.dumps({"ticket": ticket, "proof": proof} if ok or not honest else {"error": "no"}).encode()
                self.send_response(200 if ok or not honest else 403)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Her)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        saved = self.app.runtime.settings
        self.app.runtime.settings = dataclasses.replace(saved, galatea_url=url)
        self.addCleanup(setattr, self.app.runtime, "settings", saved)
        return url

    def test_the_owners_session_gets_her_glass_with_a_one_time_ticket(self) -> None:
        key = self._glass()
        url = self._her(key)
        status, out, response = self._raw("GET", "/api/voice_link", headers=self._session())
        self.assertEqual(status, 200, out)
        self.assertEqual(out["url"], f"{url}/?ticket={'t' * 32}")
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        # her key never left: not in the ask, not in the answer
        self.assertNotIn(key, json.dumps(self.her_asks))
        self.assertNotIn(key, json.dumps(out))

    def test_a_squatter_on_her_port_is_handed_nothing(self) -> None:
        key = self._glass()
        self._her(key, honest=False)
        status, out, _ = self._raw("GET", "/api/voice_link", headers=self._session())
        self.assertEqual(status, 502, out)
        self.assertNotIn("url", out)
        self.assertNotIn(key, json.dumps(self.her_asks))

    def test_no_one_else_gets_her_glass(self) -> None:
        key = self._glass()
        self._her(key)
        tries = [{}, {"Cookie": f"{auth.SESSION_COOKIE}={WRONG}"},
                 {"Origin": f"http://127.0.0.1:{self.port}", "X-Forwarded-For": "100.101.102.103",
                  "Tailscale-User-Login": "ian@example.com"}]
        tries += [{"Authorization": f"Bearer {self.app.auth.tokens[c]}"}
                  for c in ("crew", "galatea", "atani", "desktop", "phone")]
        tries += [{"Cookie": f"{auth.SESSION_COOKIE}={self.app.auth.tokens[c]}"} for c in ("phone", "dashboard")]
        for headers in tries:
            status, out, _ = self._raw("GET", "/api/voice_link", headers=headers)
            self.assertEqual(status, 401, headers)
            self.assertNotIn("ticket", json.dumps(out))
        # nor a rebound name, even carrying the session
        status, out, _ = self._raw("GET", "/api/voice_link",
                                   headers={**self._session(), "Host": "evil.example"})
        self.assertEqual(status, 403)
        self.assertNotIn(key, json.dumps(out))
        # and the open state names her address, never her key
        status, out, _ = self._raw("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertNotIn(key, json.dumps(out))

    def test_no_glass_key_yet_is_said_plainly(self) -> None:
        status, out, _ = self._raw("GET", "/api/voice_link", headers=self._session())
        self.assertEqual(status, 503, out)
        self.assertIn("galatea-glass-token.txt", out["reason"])

    # ---- 9. the launcher's sign-in link (pionir.ps1, PowerShell 5.1) ------------------
    def _dashboard_url(self, port: int) -> str:
        import subprocess
        text = (Path(__file__).resolve().parent.parent / "pionir.ps1").read_text(encoding="utf-8-sig")
        start, end = text.index("function Hex-Hmac"), text.index("function Test-Port")
        env = {**os.environ,
               "PIONIR_CLIENT_TOKEN_DIR": str(self.app.runtime.settings.client_token_path)}
        env.pop("PIONIR_STATE_ROOT", None)
        done = subprocess.run(["powershell", "-NoProfile", "-Command",
                               text[start:end] + f"\nDashboard-Url {port}"],
                              env=env, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    @unittest.skipUnless(os.name == "nt", "runs the launcher's PowerShell")
    def test_the_launcher_opens_a_one_time_code_never_the_token(self) -> None:
        url = self._dashboard_url(self.port)
        self.assertTrue(url.startswith(f"http://127.0.0.1:{self.port}/?code="), url[:40])
        self.assertNotIn(self.app.auth.tokens["dashboard"], url)
        status, _doc, response = self._raw("GET", url.split(str(self.port), 1)[1])
        self.assertEqual(status, 303)
        self.assertIn("HttpOnly", response.getheader("Set-Cookie"))

    @unittest.skipUnless(os.name == "nt", "runs the launcher's PowerShell")
    def test_the_launcher_opens_nothing_signed_in_on_a_squatter(self) -> None:
        from http.server import BaseHTTPRequestHandler

        class Squatter(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                data = json.dumps({"code": "c" * 32, "proof": "0" * 64}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Squatter)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        port = httpd.server_address[1]
        self.assertEqual(self._dashboard_url(port), f"http://127.0.0.1:{port}/")

    def test_reads_stay_open_on_loopback(self) -> None:
        for path in ("/api/state", "/api/approvals", "/api/capabilities"):
            status, _doc, _ = self._raw("GET", path)
            self.assertEqual(status, 200, path)


class InProcessRules(unittest.TestCase):
    """The same rules on PionirApp itself, so no future HTTP shell can undo them."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        runtime = _runtime(self._tmp.name)
        self.spec = Recorder()
        runtime.register(self.spec)
        self.app = PionirApp(runtime)

    def tearDown(self) -> None:
        self.app.runtime.cortex.close()
        self._tmp.cleanup()

    def test_a_client_never_combines_with_caller_permissions(self) -> None:
        with self.assertRaises(ValueError):
            self.app.run_task("gate.privileged", {}, permissions=["gate.run"], client="crew")
        with self.assertRaises(ValueError):
            self.app.route("x", permissions=["gate.run"], client="crew")
        self.assertEqual(self.spec.ran, [])

    def test_anonymous_holds_nothing_once_the_window_is_shut(self) -> None:
        self.app.auth.compat = True
        self.assertTrue(self.app.run_task("gate.read", {}, client=auth.ANONYMOUS)["ok"])
        self.app.auth.compat = False
        out = self.app.run_task("gate.read", {}, client=auth.ANONYMOUS)
        self.assertEqual(out["error"]["type"], "NotGranted", out)
        self.assertEqual(len(self.spec.ran), 1)

    def test_only_the_owner_surfaces_approve(self) -> None:
        aid = self.app.run_task("gate.privileged", {}, client="atani")["approval_id"]
        for client in ("crew", "galatea", "atani", auth.ANONYMOUS):
            self.assertEqual(self.app.approve(aid, approver=client)["error"]["type"],
                             "Forbidden", client)
            self.assertEqual(self.app.deny(aid, approver=client)["error"]["type"],
                             "Forbidden", client)
        self.assertEqual(self.app.approvals.get(aid)["status"], "pending")
        self.assertEqual(self.spec.ran, [])


class TokenFiles(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "secrets"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_first_start_makes_one_distinct_32_byte_token_per_client(self) -> None:
        tokens = ensure_tokens(self.dir)
        self.assertEqual(set(tokens), set(auth.CLIENTS))
        self.assertEqual(len(set(tokens.values())), len(auth.CLIENTS))
        for client, token in tokens.items():
            self.assertEqual(len(token), 43)          # 32 bytes, url-safe base64
            self.assertEqual(auth.read_token(token_path(self.dir, client)), token)

    def test_a_restart_keeps_the_tokens_and_a_malformed_one_is_replaced(self) -> None:
        first = ensure_tokens(self.dir)
        token_path(self.dir, "crew").write_text("short\n", encoding="utf-8")
        second = ensure_tokens(self.dir)
        self.assertEqual({k: v for k, v in second.items() if k != "crew"},
                         {k: v for k, v in first.items() if k != "crew"})
        self.assertNotEqual(second["crew"], first["crew"])
        self.assertEqual(auth.read_token(token_path(self.dir, "crew")), second["crew"])

    def test_identify_names_the_one_client_and_nothing_else(self) -> None:
        tokens = ensure_tokens(self.dir)
        gate = ClientAuth(tokens)
        for client, token in tokens.items():
            self.assertEqual(gate.identify(token), client)
        for bad in (None, "", WRONG, tokens["crew"][:-1], tokens["crew"] + "x"):
            self.assertIsNone(gate.identify(bad))

    def test_the_server_makes_the_tokens_under_its_state_root(self) -> None:
        runtime = _runtime(self._tmp.name)
        app = PionirApp(runtime)
        try:
            for client in auth.CLIENTS:
                self.assertTrue(token_path(Path(self._tmp.name) / "secrets", client).is_file())
            self.assertEqual(app.auth.identify(app.auth.tokens["atani"]), "atani")
        finally:
            runtime.cortex.close()

    def test_the_compat_flag_reads_the_environment(self) -> None:
        with mock.patch.dict(os.environ, {auth.COMPAT_ENV: "off"}):
            self.assertFalse(auth.compat_from_environment())
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(auth.COMPAT_ENV, None)
            self.assertTrue(auth.compat_from_environment())      # on for this release


class GrantsFile(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "grants.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _load(self, document) -> dict:
        self.path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertLogs("pionir.auth", logging.WARNING) if self._warns(document) \
                else _quiet():
            return load_grants(self.path)

    @staticmethod
    def _warns(document) -> bool:
        return any(k not in auth.CLIENTS or "permissions" in v or "approve" in v.get("routes", [])
                   for k, v in document.items())

    def test_no_file_is_the_code_default(self) -> None:
        self.assertEqual(load_grants(self.path), dict(auth.DEFAULT_GRANTS))

    def test_no_default_grant_holds_a_permission_or_lets_a_tasking_client_approve(self) -> None:
        for client, grant in auth.DEFAULT_GRANTS.items():
            self.assertEqual(grant.permissions, frozenset(), client)
            if auth.APPROVE in grant.routes:
                self.assertIn(client, auth.APPROVERS)
        self.assertEqual(auth.GRANTABLE_PERMISSIONS, frozenset())

    def test_a_file_narrows_a_client(self) -> None:
        grants = self._load({"crew": {"capabilities": ["client.*"]}})
        self.assertTrue(grants["crew"].allows_capability("client.orders"))
        self.assertFalse(grants["crew"].allows_capability("crew.digest"))

    def test_a_file_cannot_grant_a_privileged_permission_or_approval(self) -> None:
        grants = self._load({"crew": {"permissions": ["daedalus.solve"],
                                      "routes": ["task", "approve"]},
                             "stranger": {"routes": ["task"]}})
        self.assertEqual(grants["crew"].permissions, frozenset())
        self.assertNotIn(auth.APPROVE, grants["crew"].routes)
        self.assertNotIn("stranger", grants)

    def test_the_server_reads_the_grants_file_under_its_state_root(self) -> None:
        folder = Path(self._tmp.name) / "auth"
        folder.mkdir()
        (folder / "grants.json").write_text(json.dumps({"galatea": {"routes": ["task"]}}),
                                            encoding="utf-8")
        runtime = _runtime(self._tmp.name)
        try:
            app = PionirApp(runtime)
            self.assertEqual(app.auth.grant("galatea").routes, frozenset({auth.TASK}))
        finally:
            runtime.cortex.close()


class _quiet:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


if __name__ == "__main__":
    unittest.main()
