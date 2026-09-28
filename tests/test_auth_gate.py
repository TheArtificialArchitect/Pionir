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
        for client in ("crew", "galatea", "atani", "desktop"):
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
        for client in ("desktop", "phone"):
            status, out = self.post("/api/task", read, client)
            self.assertEqual(status, 403, (client, out))
        for client in ("crew", "atani", "desktop", "phone"):
            status, out = self.post("/api/intent", {"request": "hm"}, client)
            self.assertEqual(status, 403, (client, out))
        for client in ("crew", "galatea", "atani", "desktop", "phone"):
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
    def test_the_dashboard_link_sets_a_strict_http_only_session(self) -> None:
        key = self.app.auth.tokens["dashboard"]
        status, _doc, response = self._raw("GET", f"/?key={key}")
        self.assertEqual(status, 303)
        self.assertEqual(response.getheader("Location"), "/")
        cookie = response.getheader("Set-Cookie")
        for part in ("HttpOnly", "SameSite=Strict", f"{auth.SESSION_COOKIE}={key}"):
            self.assertIn(part, cookie)
        # the session approves (the owner's page), and runs what the dashboard may
        aid = self._parked("atani")
        session = f"{auth.SESSION_COOKIE}={key}"
        status, out = self.post("/api/approvals/approve", {"id": aid}, Cookie=session)
        self.assertIn(status, (200, 202), out)

    def test_a_wrong_key_or_another_clients_token_is_no_session(self) -> None:
        status, _doc, response = self._raw("GET", f"/?key={WRONG}")
        self.assertEqual(status, 401)
        self.assertIsNone(response.getheader("Set-Cookie"))
        status, _doc, _ = self._raw("GET", f"/?key={self.app.auth.tokens['crew']}")
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
                     f"/?key={self.app.auth.tokens['dashboard']}"):
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
        session = f"{auth.SESSION_COOKIE}={self.app.auth.tokens['dashboard']}"
        status, _ = self.post("/api/approvals/approve", {"id": aid}, Cookie=session,
                              Origin="http://evil.example")
        self.assertEqual(status, 403)
        self.assertEqual(self.app.approvals.get(aid)["status"], "pending")

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

    def test_the_crews_sandbox_grant_unlocks_only_the_sandbox_build(self) -> None:
        # a privileged capability that needs the very same permission, under another name:
        # the crew's grant is scoped to coding.daedalus_build, so this still parks
        class Lookalike(Recorder):
            def __init__(self) -> None:
                super().__init__()
                self._manifest = AgentManifest("lookalike", "test", (
                    Capability("gate.sandbox_like", "privileged", risk=RiskLevel.PRIVILEGED,
                               required_permissions=frozenset({"daedalus.build_sandbox"})),))

        spec = Lookalike()
        self.app.runtime.register(spec)
        out = self.app.run_task("gate.sandbox_like", {}, client="crew", wait=30)
        self.assertEqual(out["status"], "pending_approval", out)
        self.assertEqual(spec.ran, [])

    def test_anonymous_holds_nothing_once_the_window_is_shut(self) -> None:
        self.app.auth.compat = True
        self.assertTrue(self.app.run_task("gate.read", {}, client=auth.ANONYMOUS)["ok"])
        self.app.auth.compat = False
        out = self.app.run_task("gate.read", {}, client=auth.ANONYMOUS)
        self.assertEqual(out["error"]["type"], "NotGranted", out)
        self.assertEqual(len(self.spec.ran), 1)

    def test_only_the_owner_surfaces_approve(self) -> None:
        aid = self.app.run_task("gate.privileged", {}, client="atani")["approval_id"]
        for client in ("crew", "galatea", "atani", "desktop", auth.ANONYMOUS):
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
        # the one exception: the crew's sandbox build, scoped to that one capability
        for client, grant in auth.DEFAULT_GRANTS.items():
            expected = frozenset({"daedalus.build_sandbox"}) if client == "crew" else frozenset()
            self.assertEqual(grant.permissions, expected, client)
            if auth.APPROVE in grant.routes:
                self.assertIn(client, auth.APPROVERS)
        self.assertEqual(auth.GRANTABLE_PERMISSIONS, frozenset({"daedalus.build_sandbox"}))
        crew = auth.DEFAULT_GRANTS["crew"]
        self.assertEqual(crew.permissions_for("coding.daedalus_build"),
                         frozenset({"daedalus.build_sandbox"}))
        for other in ("coding.daedalus_solve", "security.nyx_run", "product.gumroad_publish",
                      None, ""):
            self.assertEqual(crew.permissions_for(other), frozenset(), other)

    def test_the_sandbox_grant_cannot_be_given_to_another_client(self) -> None:
        grants = self._load({"galatea": {"permissions": ["daedalus.build_sandbox"]},
                             "atani": {"permissions": ["daedalus.build_sandbox",
                                                       "daedalus.solve"]}})
        self.assertEqual(grants["galatea"].permissions, frozenset())
        self.assertEqual(grants["atani"].permissions, frozenset())

    def test_a_file_narrows_a_client(self) -> None:
        grants = self._load({"crew": {"capabilities": ["client.*"]}})
        self.assertTrue(grants["crew"].allows_capability("client.orders"))
        self.assertFalse(grants["crew"].allows_capability("crew.digest"))

    def test_a_file_cannot_grant_a_privileged_permission_or_approval(self) -> None:
        grants = self._load({"crew": {"permissions": ["daedalus.solve"],
                                      "routes": ["task", "approve"]},
                             "stranger": {"routes": ["task"]}})
        self.assertNotIn("daedalus.solve", grants["crew"].permissions)
        self.assertEqual(grants["crew"].permissions_for("coding.daedalus_solve"), frozenset())
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
