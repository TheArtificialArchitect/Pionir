"""The Mail tab's read-only routes (pionir/mailapi.py), over real HTTP against the real handler.

Nothing here reaches Gmail, a network, the real ``~/.pionir`` or a real secret: the mailbox
adapter is the real one wired to the fake IMAP of tests/test_mailbox.py and a temp credentials
file. Each test fails if the rule it names is reverted: a route a bot or an unsigned caller can
read, a route that can send / delete / mark (any write verb, any IMAP command but SEARCH and
PEEKed FETCH), a mail body written to the job store or any file, a failure answered as an empty
inbox, a sender's string outside ``untrusted``, a client token echoed from a message.
"""
from __future__ import annotations

import http.client
import imaplib
import json
import secrets
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from standins import down_url
from test_mailbox import ADDRESS, SPACED, FakeIMAP

from pionir import auth, mailapi
from pionir.adapters.mailbox import MailboxAdapter, MailboxSettings
from pionir.bootstrap import build_runtime
from pionir.config import PionirSettings
from pionir.server import PionirApp, _make_handler


def _runtime(tmp: str):
    return build_runtime(PionirSettings(
        state_root=Path(tmp),
        atani_command=("pionir-test-no-such-binary",),
        galatea_url=down_url(),
        galatea_model_id="stub-model",
        embed_model=None,
        daedalus_url=down_url(),
        melete_url=down_url(),
        crew_url=None,
        bryo_status_command=None,
        nyx_status_command=None,
        voodoo_status_command=None,
        evict_to_fit=False,
        mailbox=False,
    ))


class MailHttpCase(unittest.TestCase):
    credentials = True

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.secrets = root / "elsewhere" / "pantheon-gmail.txt"
        self.secrets.parent.mkdir()
        if self.credentials:
            self.secrets.write_text(f"{ADDRESS}\n{SPACED}\n", encoding="utf-8")
        self.fake = FakeIMAP()
        runtime = _runtime(str(root / "state"))
        self.runtime = runtime
        self.connected = self.fake

        def connect(host, port, timeout):
            if isinstance(self.connected, Exception):
                raise self.connected
            return self.connected

        runtime.register(MailboxAdapter(MailboxSettings(secrets_file=self.secrets),
                                        connect=connect))
        self.app = PionirApp(runtime)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self.app))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self._stop)

    def _stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        cortex = getattr(self.runtime, "cortex", None)
        if cortex is not None:
            cortex.close()

    # ---- wire -----------------------------------------------------------------------------
    def raw(self, path: str, headers: dict | None = None, method: str = "GET"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.putrequest(method, path)
        for key, value in dict(headers or {}).items():
            conn.putheader(key, value)
        conn.putheader("Content-Length", "0")
        conn.endheaders()
        response = conn.getresponse()
        body = response.read()
        conn.close()
        try:
            return response.status, json.loads(body.decode("utf-8") or "{}")
        except ValueError:
            return response.status, {"raw": body.decode("utf-8", "replace")}

    def signed(self, path: str, client: str = "desktop", *, nonce: str | None = None,
               sign_path: str | None = None, token: str | None = None):
        stamp = str(int(time.time()))
        once = nonce or secrets.token_hex(16)
        key = token if token is not None else self.app.auth.tokens[client]
        sig = auth.request_sig(key, "GET", sign_path or path, stamp, once, b"")
        return self.raw(path, {"X-Pionir-Client": client, "X-Pionir-Ts": stamp,
                               "X-Pionir-Nonce": once, "X-Pionir-Sig": sig})

    def dashboard(self, path: str):
        return self.raw(path, {"Authorization": f"Bearer {self.app.auth.tokens['dashboard']}"})


class WhoMayReadTests(MailHttpCase):
    def test_only_the_owners_surfaces_read_the_mailbox(self) -> None:
        for path in ("/api/mail/inbox", "/api/mail/message/1"):
            status, out = self.raw(path)
            self.assertEqual(status, 401, out)                   # loopback alone is not enough
            for bot in ("crew", "galatea", "atani", "phone"):
                status, out = self.raw(path, {"Authorization": f"Bearer {self.app.auth.tokens[bot]}"})
                self.assertEqual(status, 403, (bot, out))        # no bot reads the owner's mail
            status, out = self.raw(path, {"Authorization":
                                          f"Bearer {self.app.auth.tokens['desktop']}"})
            self.assertEqual(status, 401, out)                   # the desktop signs, never a bearer
            status, out = self.signed(path, token="y" * 43)
            self.assertEqual(status, 401, out)                   # signed with the wrong key
            status, out = self.signed(path, sign_path="/api/mail/message/2")
            self.assertEqual(status, 401, out)                   # a signature for another read
        self.assertEqual(self.fake.calls, [])                    # not one of those reached Gmail

    def test_a_signature_cannot_be_replayed(self) -> None:
        nonce = secrets.token_hex(16)
        self.assertEqual(self.signed("/api/mail/inbox", nonce=nonce)[0], 200)
        self.assertEqual(self.signed("/api/mail/inbox", nonce=nonce)[0], 401)

    def test_the_desktop_the_dashboard_token_and_the_dashboard_session_read(self) -> None:
        self.assertEqual(self.signed("/api/mail/inbox")[0], 200)
        self.assertEqual(self.dashboard("/api/mail/inbox")[0], 200)
        sid, proof = self.app.auth.start_session()
        status, _ = self.raw("/api/mail/inbox", {"Cookie": f"{auth.SESSION_COOKIE}={sid}",
                                                 auth.PROOF_HEADER: proof})
        self.assertEqual(status, 200)
        status, _ = self.raw("/api/mail/inbox", {"Cookie": f"{auth.SESSION_COOKIE}={sid}"})
        self.assertEqual(status, 401)                            # the cookie alone is refused

    def test_an_owner_client_the_grants_file_stripped_of_mail_is_refused(self) -> None:
        self.app.auth.grants = {**self.app.auth.grants,
                                "dashboard": auth.ClientGrant(auth.ROUTES, ("route.*",))}
        status, out = self.dashboard("/api/mail/inbox")
        self.assertEqual(status, 403, out)
        self.assertEqual(self.fake.calls, [])


class ReadTests(MailHttpCase):
    def test_the_inbox_lists_newest_first_with_the_unread_flag_and_sender_text_untrusted(self) -> None:
        status, out = self.signed("/api/mail/inbox")
        self.assertEqual(status, 200, out)
        self.assertTrue(out["ok"])
        self.assertEqual(out["state"], "ok")
        self.assertEqual([m["id"] for m in out["messages"]], ["3", "2", "1"])
        by = {m["id"]: m for m in out["messages"]}
        self.assertTrue(by["2"]["unread"])
        self.assertFalse(by["1"]["unread"])
        self.assertEqual(by["1"]["untrusted"]["subject"], "Plain one")
        self.assertIn("untrusted_notice", out)
        for message in out["messages"]:                          # nothing a sender wrote is loose
            self.assertEqual(set(message) - {"untrusted"},
                             {"id", "date", "unread", "labels"})

    def test_limit_and_unread_are_passed_on_and_checked(self) -> None:
        status, out = self.signed("/api/mail/inbox?limit=1")
        self.assertEqual((status, [m["id"] for m in out["messages"]]), (200, ["3"]))
        status, out = self.signed("/api/mail/inbox?unread=1")
        self.assertEqual((status, sorted(m["id"] for m in out["messages"])), (200, ["2", "3"]))
        for bad in ("limit=0", "limit=51", "limit=abc", "limit=-1", "limit=1.5", "unread=0",
                    "unread=yes", "folder=Trash", "limit=1&limit=2&x=1"):
            status, out = self.signed(f"/api/mail/inbox?{bad}")
            self.assertEqual(status, 400, (bad, out))
            self.assertFalse(out["ok"])

    def test_a_message_reads_as_plain_text_with_links_and_attachments_listed_by_name(self) -> None:
        status, out = self.signed("/api/mail/message/3")
        self.assertEqual(status, 200, out)
        body = out["untrusted"]
        self.assertIn("plain body", body["body"])
        self.assertEqual(out["attachment_count"], 1)
        self.assertEqual([a["name"] for a in body["attachments"]], ["invoice .pdf"])
        self.assertNotIn("‮", json.dumps(out, ensure_ascii=False))
        self.assertEqual(set(body["attachments"][0]), {"name", "type", "size"})
        status, out = self.signed("/api/mail/message/1")
        urls = out["untrusted"]["urls"]                          # listed as text; a token is scrubbed
        self.assertEqual(len(urls), 1)
        self.assertTrue(urls[0].startswith("https://example.org/reset?token="))
        self.assertNotIn("token=abc", urls[0])
        self.assertNotIn("attachments_data", json.dumps(out))

    def test_a_message_id_is_only_the_number_the_inbox_gave(self) -> None:
        for bad in ("0", "01", "abc", "1/../2", "1%2F2", "1%0D%0AUID", "-1", "9" * 13, "1?x=1",
                    "1%20", "%20"):
            status, out = self.signed(f"/api/mail/message/{bad}")
            self.assertIn(status, (400, 404), (bad, out))
            self.assertFalse(out.get("ok"), (bad, out))
        self.assertEqual(self.fake.calls, [])

    def test_a_missing_message_is_not_found_not_an_empty_message(self) -> None:
        status, out = self.signed("/api/mail/message/999")
        self.assertEqual(status, 404, out)
        self.assertEqual(out["state"], "not_found")
        self.assertFalse(out["ok"])
        self.assertNotIn("untrusted", out)

    def test_a_client_token_in_a_message_is_scrubbed_from_the_answer(self) -> None:
        token = self.app.auth.tokens["crew"]
        fake = self.fake
        fake.messages["4"] = {
            "flags": "", "labels": "\\Inbox", "date": "04-Oct-2026 09:00:00 +0000",
            "head": b"From: x@example.com\r\nSubject: leak\r\nDate: Sun, 04 Oct 2026 09:00:00 +0000\r\n\r\n",
            "structure": ('("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "7BIT" '
                          f'{len(token) + 5} 1 NIL NIL NIL NIL)'),
            "parts": {"1": f"key {token}".encode()}}
        status, out = self.signed("/api/mail/message/4")
        self.assertEqual(status, 200, out)
        self.assertNotIn(token, json.dumps(out))


class NothingWrittenTests(MailHttpCase):
    def test_gmail_is_only_searched_and_peeked_and_nothing_is_changed(self) -> None:
        self.signed("/api/mail/inbox")
        self.signed("/api/mail/message/2")
        self.signed("/api/mail/message/3")
        self.assertEqual(self.fake.mutations, [])
        commands = {c[1] for c in self.fake.calls if c[0] == "uid"}
        self.assertEqual(commands, {"SEARCH", "FETCH"})
        for call in self.fake.calls:
            if call[0] == "select":
                self.assertTrue(call[2], "the folder must be opened read-only")
            if call[0] == "uid" and call[1] == "FETCH" and "BODY[" in call[3]:
                self.fail(f"a non-PEEK body fetch: {call}")

    def test_no_write_verb_reaches_the_mailbox(self) -> None:
        sid_headers = {"Authorization": f"Bearer {self.app.auth.tokens['dashboard']}"}
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            for path in ("/api/mail/inbox", "/api/mail/message/1", "/api/mail/send",
                         "/api/mail/delete/1", "/api/mail/message/1/read"):
                status, out = self.raw(path, sid_headers, method=method)
                self.assertNotEqual(status, 200, (method, path, out))
        for path in ("/api/mail/send", "/api/mail/delete/1", "/api/mail/message/1/read",
                     "/api/mail/", "/api/mail/inbox/1", "/api/mail/mark"):
            status, out = self.signed(path)
            self.assertIn(status, (400, 404), (path, out))       # no route but the two reads
            self.assertFalse(out.get("ok"), (path, out))
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.fake.mutations, [])

    def test_the_route_module_only_ever_runs_the_two_read_only_capabilities(self) -> None:
        seen: list[str] = []

        def run(capability, payload):
            seen.append(capability)
            return {"ok": True, "messages": []}

        for path in ("/api/mail/inbox", "/api/mail/message/5"):
            mailapi.serve(path, "", client="desktop", refused=None,
                          allows=lambda who, cap: True, run=run)
        self.assertEqual(seen, ["mail.inbox", "mail.read"])
        capabilities = {c.name: c for m in self.runtime.executive.registry.manifests()
                        for c in m.capabilities}
        for name in seen:
            self.assertEqual(capabilities[name].risk.name, "READ_ONLY")

    def test_mail_is_never_written_to_the_job_store_or_any_file(self) -> None:
        self.signed("/api/mail/inbox")
        self.signed("/api/mail/message/1")
        self.signed("/api/mail/message/3")
        self.assertEqual(self.app.jobs_view(50)["tasks"], [])
        for marker in (b"Plain one", b"plain body", b"Html one", b"Alice", b"invoice.pdf",
                       b"IGNORE ALL PREVIOUS"):
            for path in Path(self._tmp.name).rglob("*"):
                if path.is_file() and path != self.secrets:
                    self.assertNotIn(marker, path.read_bytes(), f"{marker!r} reached {path.name}")

    def test_each_read_is_audited_through_the_executive_without_its_content(self) -> None:
        self.signed("/api/mail/inbox")
        self.signed("/api/mail/message/1")
        events = self.app.audit(50)["events"]
        text = json.dumps(events, default=str)
        self.assertGreaterEqual(text.count("task.completed"), 2)
        self.assertIn("mail", text)
        self.assertNotIn("Plain one", text)
        self.assertNotIn("plain body", text)


class FailureStateTests(MailHttpCase):
    def test_a_refused_signin_is_unavailable_never_an_empty_inbox(self) -> None:
        self.connected = FakeIMAP(login_error=imaplib.IMAP4.error("AUTHENTICATIONFAILED"))
        for path in ("/api/mail/inbox", "/api/mail/message/1"):
            status, out = self.signed(path)
            self.assertEqual(status, 503, out)
            self.assertFalse(out["ok"])
            self.assertEqual(out["state"], "unavailable")
            self.assertIn("app password", out["error"])
            self.assertNotIn("messages", out)
            self.assertNotIn("untrusted", out)

    def test_an_unreachable_gmail_is_unavailable(self) -> None:
        self.connected = OSError("no route")
        status, out = self.signed("/api/mail/inbox")
        self.assertEqual((status, out["state"], out["ok"]), (503, "unavailable", False), out)
        self.assertNotIn("messages", out)

    def test_an_adapter_that_raises_is_an_error_naming_only_its_type(self) -> None:
        class Boom(Exception):
            pass

        self.connected = Boom("the subject was SECRET-SUBJECT")
        status, out = self.signed("/api/mail/inbox")
        self.assertFalse(out["ok"])
        self.assertIn(out["state"], ("unavailable", "error"))
        self.assertNotIn("SECRET-SUBJECT", json.dumps(out))
        self.assertNotIn("messages", out)


class NotConfiguredTests(MailHttpCase):
    credentials = False

    def test_a_missing_credentials_file_says_how_to_fix_it_and_is_not_an_empty_inbox(self) -> None:
        for path in ("/api/mail/inbox", "/api/mail/message/1"):
            status, out = self.signed(path)
            self.assertEqual(status, 503, out)
            self.assertFalse(out["ok"])
            self.assertEqual(out["state"], "not_configured")
            self.assertIn("pantheon-gmail.txt", out["error"])
            self.assertNotIn("messages", out)
        self.assertEqual(self.fake.calls, [])


if __name__ == "__main__":
    unittest.main()
