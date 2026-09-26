"""No email reaches a client without the owner's yes; reading and moving orders never waits.

``client.email`` parks on every call - even for a caller holding its permission - runs once
after an approval and never after a denial. ``client.orders`` and ``client.set_status``
contact nobody and run at once. These tests pin the adapter (the check before parking, the
error mapping, the token never leaking), the Discord card that shows the recipient and the
whole message, the ``orders`` command, and the real urllib opener against a real loopback
HTTP server - the lesson ``content.publish`` paid for.

Scrooge is faked at the HTTP opener with the real opener's signature
``(request, data=None, timeout=None)``; nothing touches the network.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar, Self
from unittest import mock

from test_discord_gate import API, CHANNEL, OWNER, FakeDiscord
from test_discord_gate import TOKEN as DISCORD_TOKEN

from pionir import cli
from pionir.adapters.clients import (
    CHECK_BEFORE_RETRY,
    EMAIL,
    ORDERS,
    RATE_LIMITED,
    SET_STATUS,
    TOKEN_REJECTED,
    ClientAdapter,
    ClientSettings,
    check_email,
)
from pionir.bootstrap import build_runtime
from pionir.config import PionirSettings
from pionir.contracts import RiskLevel, Task
from pionir.discord_gate import DiscordGate, DiscordGateSettings, _escape, client_email_line
from pionir.errors import AdapterProtocolError, AdapterUnavailable
from pionir.reliability import CircuitState
from pionir.server import PionirApp

OPS_TOKEN = "opsSECRETtoken0123456789abcdefXYZ+/="
SCROOGE = "https://api.dokaz.test"
ORDER_ID = "0123456789ab"
CLIENT = "client.person+work@example.com"
BRIEF = ("Please set up email verification for my signup form: the form is on my Webflow "
         "site and I want disposable addresses turned away. " * 3)


def message(**over: Any) -> dict[str, Any]:
    base = {
        "order_id": ORDER_ID,
        "to": CLIENT,
        "subject": "Your email-check setup is underway",
        "body_text": ("Hi Sam,\n\nThanks for your order - I've started on your signup form "
                      "today and will have it delivered by Friday.\n\nThe details are on "
                      "https://api.dokaz.net/orders/help.\n\nBest,\nIan at Dokaz\n"),
    }
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def an_order(**over: Any) -> dict[str, Any]:
    base = {"id": ORDER_ID, "created_at": "2026-09-24T10:00:00Z", "name": "Sam Client",
            "email": CLIENT, "package": "setup", "brief": BRIEF, "status": "paid",
            "amount_cents": 14900, "paid_at": "2026-09-24T10:05:00Z",
            "messages": [{"at": "2026-09-24T11:00:00Z", "subject": "Thanks"}]}
    base.update(over)
    return base


class _Response:
    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self._raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def read(self, _n: int = -1) -> bytes:
        return self._raw

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class FakeScrooge:
    """Scrooge's /dash/orders endpoints, in memory, checking the ops token like the real
    one. Same call signature as OpenerDirector.open."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.valid_tokens = {OPS_TOKEN}
        self.forced: dict[str, tuple[int, Any]] = {}   # step -> (status, body)
        self.down = False
        self.orders = [an_order(), an_order(id="ba9876543210", status="delivered",
                                            brief="short")]
        self._lock = threading.Lock()

    def steps(self) -> list[str]:
        return [c["step"] for c in self.calls]

    def __call__(self, request: Any, data: Any = None, timeout: float | None = None) -> _Response:
        # The real opener is OpenerDirector.open(url, data=None, timeout=...): a timeout
        # passed positionally lands in `data`. This fake fails the same way.
        if data is not None:
            raise TypeError(f"opener got a positional data argument: {data!r}")
        with self._lock:
            return self._handle(request, timeout)

    @staticmethod
    def _error(url: str, status: int, payload: Any) -> None:
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        raise urllib.error.HTTPError(url, status, "error", {},  # type: ignore[arg-type]
                                     io.BytesIO(raw))

    def _handle(self, request: Any, timeout: float | None) -> _Response:
        url = request.full_url
        parts = urllib.parse.urlsplit(url)
        method = request.get_method()
        step = {"/dash/orders": "orders", "/dash/orders/email": "email",
                "/dash/orders/status": "status"}.get(parts.path, "unknown")
        body = json.loads(request.data) if request.data else None
        self.calls.append({
            "step": step, "method": method, "url": url, "query": parts.query, "body": body,
            "timeout": timeout, "token": request.get_header("X-dash-token"),
            "content_type": request.get_header("Content-type"),
        })
        if self.down:
            raise urllib.error.URLError(f"connection refused: {url} token={OPS_TOKEN}")
        if request.get_header("X-dash-token") not in self.valid_tokens:
            self._error(url, 401, {"ok": False, "error": "unauthorized"})
        if step in self.forced:
            status, payload = self.forced[step]
            if status >= 400:
                self._error(url, status, payload)
            return _Response(status, payload)
        if step == "orders" and method == "GET":
            wanted = urllib.parse.parse_qs(parts.query).get("status")
            rows = [o for o in self.orders if not wanted or o["status"] == wanted[0]]
            return _Response(200, {"ok": True, "orders": rows})
        if step == "email" and method == "POST":
            (order,) = [o for o in self.orders if o["id"] == body["order_id"]]
            if body["to"] != order["email"]:
                self._error(url, 409, {"ok": False, "error": "to: does not match the order"})
            return _Response(200, {"ok": True, "id": f"msg-{len(self.calls)}"})
        if step == "status" and method == "POST":
            return _Response(200, {"ok": True, "order_id": body["order_id"],
                                   "status": body["status"]})
        self._error(url, 404, {"ok": False, "error": "not found"})
        raise AssertionError  # unreachable


def _settings(root: Path, **kw: Any) -> PionirSettings:
    return PionirSettings(
        state_root=root,
        atani_command=("pionir-test-no-such-binary",),
        daedalus_url=None, melete_url=None, galatea_url=None, crew_url=None,
        bryo_status_command=None, nyx_status_command=None, voodoo_status_command=None,
        embed_model=None, evict_to_fit=False, **kw,
    )


class _Case(unittest.TestCase):
    """A hermetic runtime with the client adapter talking to FakeScrooge."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.token_file = self.root / "secrets" / "scrooge-ops-token.txt"
        self.token_file.parent.mkdir(parents=True)
        self.token_file.write_text(OPS_TOKEN + "\n", encoding="utf-8")
        self.world = FakeScrooge()
        self.adapter = ClientAdapter(ClientSettings(base_url=SCROOGE,
                                                    token_file=self.token_file),
                                     opener=self.world)
        # content_url=None: no default content/client adapters; the faked one is used
        runtime = build_runtime(_settings(self.root, content_url=None))
        runtime.register(self.adapter)
        self.app = PionirApp(runtime)

    def tearDown(self) -> None:
        self.app.runtime.cortex.close()
        self._tmp.cleanup()

    def execute(self, capability: str = EMAIL,
                payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = message() if payload is None and capability == EMAIL else (payload or {})
        return dict(self.adapter.execute(Task(capability, payload)).output)

    def send(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Park, approve, wait: the only way an email goes out."""
        out = self.app.run_task(EMAIL, payload or message(), permissions=[EMAIL])
        self.assertEqual(out["status"], "pending_approval", out)
        res = self.app.approve(out["approval_id"])
        self.assertTrue(self.app.jobs.wait(res["task_id"], 30))
        return self.app.approvals.get(out["approval_id"])


# ---- the gate ---------------------------------------------------------------------
class GateTests(_Case):
    def test_the_declarations(self) -> None:
        caps = {c.name: c for c in self.adapter.manifest.capabilities}
        # client.deliver is pinned in test_client_deliver.py, client.find_report in
        # test_client_find_report.py, the quote capabilities in test_quote_paid.py
        self.assertEqual(set(caps), {ORDERS, EMAIL, SET_STATUS, "client.deliver",
                                     "client.find_report", "client.quote",
                                     "client.quote_reminder", "client.release"})
        self.assertTrue(caps[EMAIL].requires_approval)
        self.assertIs(caps[EMAIL].risk, RiskLevel.PRIVILEGED)
        self.assertIs(caps[ORDERS].risk, RiskLevel.READ_ONLY)
        self.assertIs(caps[SET_STATUS].risk, RiskLevel.REVERSIBLE_WRITE)
        self.assertFalse(caps[ORDERS].requires_approval)
        self.assertFalse(caps[SET_STATUS].requires_approval)
        self.assertTrue(all(not c.routable for c in caps.values()))

    def test_an_email_parks_every_time_even_with_the_permission(self) -> None:
        for _ in range(3):
            out = self.app.run_task(EMAIL, message(), permissions=[EMAIL])
            self.assertEqual(out["status"], "pending_approval")
        pending = self.app.approvals.pending()
        self.assertEqual(len(pending), 3)
        # the summary names the order and the subject
        self.assertIn(f"order {ORDER_ID}", pending[0]["summary"])
        self.assertIn(message()["subject"], pending[0]["summary"])
        self.assertEqual(self.world.calls, [])

    def test_an_approved_email_is_sent_once_exactly_as_checked(self) -> None:
        row = self.send()
        self.assertEqual(row["status"], "approved", row)
        self.assertEqual(self.world.steps(), ["email"])
        (call,) = self.world.calls
        self.assertEqual((call["method"], call["url"]),
                         ("POST", f"{SCROOGE}/dash/orders/email"))
        self.assertEqual(call["token"], OPS_TOKEN)
        self.assertEqual(call["content_type"], "application/json")
        self.assertEqual(call["timeout"], 30)
        self.assertEqual(call["body"], message())
        self.assertEqual(row["result"]["result"], {"ok": True, "id": "msg-1"})
        self.assertFalse(self.app.approve(row["id"])["ok"])       # never twice
        self.assertEqual(len(self.world.calls), 1)

    def test_a_denied_email_is_never_sent(self) -> None:
        aid = self.app.run_task(EMAIL, message(), permissions=[EMAIL])["approval_id"]
        self.assertTrue(self.app.deny(aid)["ok"])
        self.assertFalse(self.app.approve(aid)["ok"])
        self.assertEqual(self.world.calls, [])

    def test_reading_and_moving_orders_do_not_park(self) -> None:
        listed = self.app.run_task(ORDERS, {}, wait=30)
        self.assertIs(listed["ok"], True, listed)
        self.assertEqual(len(listed["result"]["orders"]), 2)
        moved = self.app.run_task(SET_STATUS, {"order_id": ORDER_ID, "status": "delivered"},
                                  wait=30)
        self.assertIs(moved["ok"], True, moved)
        self.assertEqual(moved["result"], {"ok": True, "order_id": ORDER_ID,
                                           "status": "delivered"})
        self.assertEqual(self.app.approvals.pending(), [])
        self.assertEqual(self.world.steps(), ["orders", "status"])
        self.assertEqual(self.world.calls[1]["body"], {"order_id": ORDER_ID,
                                                       "status": "delivered"})

    def test_orders_can_be_filtered_by_status(self) -> None:
        out = self.execute(ORDERS, {"status": "delivered"})
        self.assertEqual([o["id"] for o in out["orders"]], ["ba9876543210"])
        self.assertEqual(self.world.calls[0]["url"],
                         f"{SCROOGE}/dash/orders?status=delivered")
        self.assertEqual(self.world.calls[0]["method"], "GET")
        self.assertIsNone(self.world.calls[0]["body"])


# ---- the check before parking ------------------------------------------------------
class LocalValidationTests(_Case):
    BAD: ClassVar[dict[str, dict[str, Any]]] = {
        "HTML": {"body_text": message()["body_text"] + "<b>Thanks!</b>"},
        "a link tag": {"body_text": message()["body_text"] + '<a href="https://api.dokaz.net">'},
        "an autolink": {"body_text": message()["body_text"] + "<https://api.dokaz.net/x>"},
        "tag-shaped text": {"body_text": message()["body_text"] + "Ready in <3 days>."},
        "an HTML comment": {"body_text": message()["body_text"] + "<!-- hidden -->"},
        "a foreign link": {"body_text": message()["body_text"] + "See https://evil.example.com/x"},
        "a lookalike host": {"body_text": "Your files: https://api.dokaz.net.evil.example/x ok"},
        "credentials in a link": {"body_text": "Your files: https://api.dokaz.net@evil.example/"},
        "an http link": {"body_text": message()["body_text"] + "http://api.dokaz.net/x"},
        "a mailto link": {"body_text": message()["body_text"] + "mailto:someone@evil.example"},
        "a javascript link": {"body_text": message()["body_text"] + "javascript:alert(1)"},
        "a bare www address": {"body_text": message()["body_text"] + "Go to www.evil.example"},
        "a bare host and path": {"body_text": message()["body_text"] + "Log in: evil.example/login"},
        "a long subject": {"subject": "S" * 121},
        "a short subject": {"subject": "Hi"},
        "a newline in the subject": {"subject": "Your order\nBcc: someone@evil.example"},
        "a carriage return in the subject": {"subject": "Your order\rX-Header: 1"},
        "a link in the subject": {"subject": "See https://evil.example.com now"},
        "a short body": {"body_text": "Thanks!"},
        "a long body": {"body_text": "x" * 5001},
        "a tab in the body": {"body_text": message()["body_text"] + "\tindented"},
        "a NUL in the body": {"body_text": message()["body_text"] + "\x00"},
        "a bidi override": {"body_text": message()["body_text"] + "\u202egnp.exe"},
        "a zero-width space": {"subject": "Your\u200b order"},
        "an uppercase order id": {"order_id": "0123456789AB"},
        "a short order id": {"order_id": "0123456789a"},
        "a non-hex order id": {"order_id": "0123456789zz"},
        "a numeric order id": {"order_id": 123456789012},
        "a bad email": {"to": "not-an-address"},
        "two addresses": {"to": f"{CLIENT},attacker@evil.example"},
        "an address with a newline": {"to": f"{CLIENT}\nBcc: x@evil.example"},
        "an address with a name": {"to": f"Sam <{CLIENT}>"},
        "a double dot address": {"to": "sam..x@example.com"},
        "a too-long address": {"to": "a" * 60 + "@" + ("b" * 60 + ".") * 2 + "b" * 14 + ".com"},
        "a 64-character domain label": {"to": "sam@" + "b" * 64 + ".com"},
        "an extra field": {"cc": "x@evil.example"},
        "a missing field": {"subject": None},
        "a non-string body": {"body_text": ["a list"] * 10},
    }

    def test_every_rule_is_enforced_before_anything_is_sent_or_parked(self) -> None:
        for name, over in self.BAD.items():
            with self.subTest(name):
                payload = message(**over)
                with self.assertRaises(AdapterProtocolError) as err:
                    self.adapter.validate(Task(EMAIL, payload))
                self.assertIn("refused by Pionir", str(err.exception))
                with self.assertRaises(ValueError):
                    check_email(payload)
                out = self.app.run_task(EMAIL, payload, permissions=[EMAIL])
                self.assertEqual(out["status"], "error", out)
                self.assertEqual(out["error"]["type"], "AdapterProtocolError")
                # checked again at execute(): an approval of a bad email still sends nothing
                with self.assertRaises(AdapterProtocolError):
                    self.adapter.execute(Task(EMAIL, payload))
        self.assertEqual(self.app.approvals.pending(), [])
        self.assertEqual(self.world.calls, [])

    def test_the_address_is_not_echoed_in_a_refusal(self) -> None:
        with self.assertRaises(AdapterProtocolError) as err:
            self.adapter.validate(Task(EMAIL, message(to="sam@@example.com")))
        self.assertNotIn("sam@@example.com", str(err.exception))

    def test_what_is_allowed(self) -> None:
        good = {
            "the example": {},
            "every allowed host, with sentence punctuation": {"body_text": (
                "Links: https://api.dokaz.net/x, https://dokazindustries.com. "
                "https://www.dokazindustries.com/about and (https://dokaz.gumroad.com/l/kit).")},
            "CRLF line breaks": {"body_text": "Hi Sam,\r\n\r\nYour setup is delivered.\r\n"},
            "a plain less-than": {"body_text": "It checks addresses in < 200 ms, every time."},
            "exact limits": {"subject": "S" * 120, "body_text": "b" * 5000},
            "shortest": {"subject": "Hello", "body_text": "b" * 20},
            "a 200-character address": {
                "to": "a" * 60 + "@" + ("b" * 60 + ".") * 2 + "b" * 13 + ".com"},
        }
        for name, over in good.items():
            with self.subTest(name):
                payload = message(**over)
                self.adapter.validate(Task(EMAIL, payload))
                self.assertEqual(check_email(payload), payload)

    def test_orders_and_status_payloads_are_checked(self) -> None:
        bad = [
            (ORDERS, {"status": "shipped"}),
            (ORDERS, {"other": 1}),
            (SET_STATUS, {"order_id": ORDER_ID, "status": "paid"}),
            (SET_STATUS, {"order_id": ORDER_ID, "status": "awaiting_payment"}),
            (SET_STATUS, {"order_id": ORDER_ID, "status": "quote_requested"}),
            (SET_STATUS, {"order_id": ORDER_ID}),
            (SET_STATUS, {"order_id": "XYZ", "status": "delivered"}),
            (SET_STATUS, {"order_id": ORDER_ID, "status": "delivered", "note": "x"}),
        ]
        for capability, payload in bad:
            with self.subTest(capability=capability, payload=payload):
                with self.assertRaises(AdapterProtocolError):
                    self.adapter.validate(Task(capability, payload))
                out = self.app.run_task(capability, payload)
                self.assertEqual(out["error"]["type"], "AdapterProtocolError")
        for status in ("in_progress", "delivered", "declined", "refunded", "quoted"):
            self.adapter.validate(Task(SET_STATUS, {"order_id": ORDER_ID, "status": status}))
        for status in ("awaiting_payment", "paid", "in_progress", "delivered", "declined",
                       "refunded", "quote_requested", "quoted"):
            self.adapter.validate(Task(ORDERS, {"status": status}))
        self.assertEqual(self.world.calls, [])

    def test_a_missing_token_is_unavailable_before_parking(self) -> None:
        self.token_file.unlink()
        with self.assertRaisesRegex(AdapterUnavailable, "not configured") as err:
            self.adapter.validate(Task(EMAIL, message()))
        self.assertIn(str(self.token_file), str(err.exception))
        self.assertIn(r"tools\setup-ops-token.ps1", str(err.exception))
        with self.assertRaisesRegex(AdapterUnavailable, "not configured"):
            self.adapter.status()
        parked = self.app.run_task(EMAIL, message(), permissions=[EMAIL])
        self.assertEqual(parked["status"], "error")
        self.assertEqual(parked["error"]["type"], "AdapterUnavailable")
        self.assertEqual(self.app.approvals.pending(), [])
        for capability, payload in ((EMAIL, message()), (ORDERS, {}),
                                    (SET_STATUS, {"order_id": ORDER_ID,
                                                  "status": "delivered"})):
            out = self.execute(capability, payload)
            self.assertIs(out["ok"], False)
            self.assertTrue(out["not_configured"])
            self.assertIn("not configured", out["unavailable"])
        self.assertEqual(self.world.calls, [])

    def test_status_is_local(self) -> None:
        self.assertEqual(self.adapter.status(), {"url": SCROOGE, "token": "configured"})
        self.assertEqual(self.world.calls, [])


# ---- what Scrooge says --------------------------------------------------------------------
class ErrorTests(_Case):
    def test_a_mismatched_address_is_refused_with_scrooges_reason(self) -> None:
        out = self.execute(EMAIL, message(to="someone.else@example.com"))
        self.assertEqual(out, {"ok": False, "refused": "to: does not match the order",
                               "error": "to: does not match the order", "status": 409})
        row = self.send(message(to="someone.else@example.com"))    # through the gate
        self.assertEqual(row["status"], "approved_failed")
        self.assertEqual(row["result"]["result"]["refused"], "to: does not match the order")

    def test_a_bad_request_is_refused(self) -> None:
        self.world.forced = {"email": (400, {"ok": False, "error": "body_text: too long"})}
        out = self.execute()
        self.assertEqual((out["refused"], out["status"]), ("body_text: too long", 400))

    def test_the_email_mapping(self) -> None:
        cases = [
            (429, {"ok": False, "error": "rate"}, "unavailable", RATE_LIMITED),
            (401, {"ok": False, "error": "no"}, "unavailable", TOKEN_REJECTED),
            (403, {"ok": False, "error": "no"}, "unavailable", TOKEN_REJECTED),
            (500, {"ok": False, "error": "boom"}, "unavailable", CHECK_BEFORE_RETRY),
            (502, b"<html>bad gateway</html>", "unavailable", CHECK_BEFORE_RETRY),
            # Scrooge's own refusals, before (503) or instead of (502) a send: nothing went out
            (503, {"ok": False, "error": "RESEND_API_KEY is not set; nothing was sent"},
             "unavailable", "nothing was sent"),
            (502, {"ok": False, "error": "the mail provider refused it"},
             "unavailable", "nothing was sent"),
            (200, {"sent": True}, "unavailable", CHECK_BEFORE_RETRY),
        ]
        for status, body, kind, words in cases:
            with self.subTest(status=status):
                self.world.forced = {"email": (status, body)}
                out = self.execute()
                self.assertIs(out["ok"], False)
                self.assertIn(words, out[kind])
                self.assertNotIn("refused", out)
                if words == "nothing was sent":        # and no "may have been sent" warning
                    self.assertNotIn(CHECK_BEFORE_RETRY, out[kind])
        self.assertIn(r"Scrooge's tools\setup-ops-token.ps1", TOKEN_REJECTED)
        self.assertEqual(RATE_LIMITED, "too many emails to this order today")
        self.world.forced = {}
        self.world.down = True
        out = self.execute()
        self.assertIn("unreachable", out["unavailable"])
        self.assertIn(CHECK_BEFORE_RETRY, out["unavailable"])
        self.assertEqual(out["status"], 0)

    def test_a_rejected_token_through_the_gate(self) -> None:
        self.world.valid_tokens = set()
        row = self.send()
        self.assertEqual(row["status"], "approved_failed")
        self.assertEqual(row["result"]["result"]["unavailable"], TOKEN_REJECTED)

    def test_status_and_orders_mapping(self) -> None:
        self.world.forced = {"status": (409, {"ok": False,
                                              "error": "status: cannot go from refunded"})}
        out = self.execute(SET_STATUS, {"order_id": ORDER_ID, "status": "delivered"})
        self.assertEqual(out["refused"], "status: cannot go from refunded")
        self.world.forced = {}
        self.world.valid_tokens = set()
        out = self.execute(ORDERS, {})
        self.assertEqual(out["unavailable"], TOKEN_REJECTED)
        self.world.valid_tokens = {OPS_TOKEN}
        self.world.down = True
        out = self.execute(ORDERS, {})
        self.assertIn("unreachable", out["unavailable"])
        self.assertNotIn(CHECK_BEFORE_RETRY, out["unavailable"])   # nothing could be sent

    def test_answers_do_not_trip_the_circuit_breaker(self) -> None:
        self.world.forced = {"email": (500, {"ok": False, "error": "boom"})}
        for _ in range(8):
            self.send()
        self.world.forced = {"email": (409, {"ok": False, "error": "to: no"})}
        for _ in range(8):
            self.send()
        self.assertIs(self.app.runtime.executive.circuit("client").snapshot().state,
                      CircuitState.CLOSED)


# ---- the token never leaves -------------------------------------------------------------
class SecrecyTests(_Case):
    def test_the_token_never_appears_in_results_errors_logs_or_records(self) -> None:
        capture = io.StringIO()
        handler = logging.StreamHandler(capture)
        root = logging.getLogger()
        old = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(handler)
        echo = f"bad token {OPS_TOKEN} / {urllib.parse.quote_plus(OPS_TOKEN)}"
        try:
            outputs: list[Any] = []
            for status in (400, 409, 429, 500, 401):
                self.world.forced = {"email": (status, {"ok": False, "error": echo})}
                outputs.append(self.execute())
            self.world.forced = {"email": (200, {"ok": True, "id": echo})}
            outputs.append(self.execute())       # Scrooge echoing it in a success, too
            outputs.append(self.send())          # through the gate, audited and stored
            self.world.forced = {"orders": (200, {"ok": True, "orders": [
                an_order(brief=echo, name=OPS_TOKEN)]})}
            outputs.append(self.execute(ORDERS, {}))
            outputs.append(self.app.run_task(ORDERS, {}, wait=30))
            with contextlib.redirect_stdout(io.StringIO()) as printed:
                cli.orders(_settings(self.root, content_url=SCROOGE,
                                     ops_token_file=self.token_file), opener=self.world)
            outputs.append(printed.getvalue())
            self.world.forced = {"status": (409, {"ok": False, "error": echo})}
            outputs.append(self.execute(SET_STATUS, {"order_id": ORDER_ID,
                                                     "status": "delivered"}))
            self.world.forced = {}
            self.world.down = True               # the transport error names it
            outputs.append(self.execute())
            self.world.down = False
            self.world.valid_tokens = set()
            outputs.append(self.send())          # a rejected token, through the gate
            outputs.append(self.adapter.status())
            outputs.append(repr(self.adapter.settings))
        finally:
            root.removeHandler(handler)
            root.setLevel(old)
        # the token really was used and echoed, so its absence below means something
        self.assertIn(OPS_TOKEN, {c["token"] for c in self.world.calls})
        dumped = json.dumps(outputs, default=str)
        self.assertIn("<redacted>", dumped)
        self.assertNotIn(OPS_TOKEN, dumped)
        self.assertNotIn(urllib.parse.quote_plus(OPS_TOKEN), dumped)
        self.assertNotIn(OPS_TOKEN, capture.getvalue())
        self.assertIn("client: email for order", capture.getvalue())   # the adapter did log
        for path in self.root.rglob("*"):   # the audit ledger, jobs, approvals, memory
            if path.is_file() and path != self.token_file:
                self.assertNotIn(OPS_TOKEN.encode("utf-8"), path.read_bytes(), path)


# ---- settings and wiring -----------------------------------------------------------------
class SettingsTests(unittest.TestCase):
    def test_defaults_env_and_off(self) -> None:
        settings = PionirSettings(state_root=Path("C:/x"))
        self.assertEqual(settings.ops_token_path,
                         Path.home() / ".pionir" / "secrets" / "scrooge-ops-token.txt")
        self.assertEqual(ClientSettings().token_file,
                         Path.home() / ".pionir" / "secrets" / "scrooge-ops-token.txt")
        self.assertEqual(ClientSettings().base_url, "https://api.dokaz.net")
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            os.environ, {"PIONIR_STATE_ROOT": root}
        ):
            os.environ.pop("PIONIR_OPS_TOKEN_FILE", None)
            self.assertIsNone(PionirSettings.from_environment().ops_token_file)
            os.environ["PIONIR_OPS_TOKEN_FILE"] = str(Path(root) / "t.txt")
            self.assertEqual(PionirSettings.from_environment().ops_token_path,
                             Path(root) / "t.txt")

    def test_bootstrap_registers_it_with_scrooge_and_without_the_network(self) -> None:
        for content, present in (("https://api.dokaz.net", True), (None, False)):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as root:
                runtime = build_runtime(_settings(
                    Path(root), content_url=content, devto_url=None,
                    instagram_graph_url=None,
                    content_token_file=Path(root) / "no-token.txt",
                    ops_token_file=Path(root) / "no-ops-token.txt"))
                try:
                    self.assertEqual("client" in runtime.adapters, present)
                    if present:   # doctor's health check is local: no token, no call
                        adapter = runtime.adapters["client"]
                        with self.assertRaisesRegex(AdapterUnavailable, "not configured"):
                            adapter.status()
                        self.assertEqual(adapter.settings.token_file,
                                         Path(root) / "no-ops-token.txt")
                        self.assertEqual(adapter.settings.base_url, content)
                finally:
                    runtime.cortex.close()

    def test_the_token_never_leaves_over_plain_http_to_a_remote_host(self) -> None:
        with self.assertRaises(ValueError):
            ClientSettings(base_url="http://api.dokaz.net")
        with self.assertRaises(ValueError):
            ClientSettings(base_url="https://api.dokaz.net/?x=1")
        ClientSettings(base_url="http://127.0.0.1:9")


# ---- python -m pionir orders -------------------------------------------------------------
class OrdersCommandTests(_Case):
    def run_cli(self, status: str | None = None) -> tuple[int, str]:
        settings = _settings(self.root, content_url=SCROOGE, ops_token_file=self.token_file)
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            code = cli.orders(settings, status=status, opener=self.world)
        return code, printed.getvalue()

    def test_it_prints_the_orders_and_never_the_token(self) -> None:
        code, printed = self.run_cli()
        self.assertEqual(code, 0, printed)
        report = json.loads(printed)
        self.assertEqual(report["count"], 2)
        first = report["orders"][0]
        self.assertEqual(first, {
            "id": ORDER_ID, "created": "2026-09-24T10:00:00Z", "package": "setup",
            "status": "paid", "amount": "149.00", "name": "Sam Client", "email": CLIENT,
            "brief": BRIEF[:117] + "...",
        })
        self.assertEqual(len(first["brief"]), 120)
        self.assertEqual(report["orders"][1]["brief"], "short")
        self.assertNotIn(OPS_TOKEN, printed)
        self.assertEqual(self.world.steps(), ["orders"])
        self.assertEqual(self.world.calls[0]["method"], "GET")

    def test_status_filter_and_exit_codes(self) -> None:
        code, printed = self.run_cli("delivered")
        self.assertEqual(code, 0)
        self.assertEqual([o["id"] for o in json.loads(printed)["orders"]], ["ba9876543210"])
        self.assertEqual(self.world.calls[-1]["query"], "status=delivered")
        code, printed = self.run_cli("shipped")
        self.assertEqual(code, 1)
        self.assertIn("status: one of", printed)
        self.world.valid_tokens = set()
        code, printed = self.run_cli()
        self.assertEqual(code, 2)
        self.assertIn("setup-ops-token.ps1", printed)
        self.world.valid_tokens = {OPS_TOKEN}
        self.world.down = True
        code, printed = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("not_listed", printed)
        self.token_file.unlink()
        code, printed = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("not_configured", printed)
        self.assertNotIn(OPS_TOKEN, printed)
        with contextlib.redirect_stdout(io.StringIO()) as off:
            self.assertEqual(cli.orders(_settings(self.root, content_url=None)), 1)
        self.assertIn("not_configured", off.getvalue())
        self.assertNotIn("email", self.world.steps())

    def test_the_command_is_wired(self) -> None:
        with mock.patch.object(cli, "orders", return_value=0) as command:
            self.assertEqual(cli.main(["orders"]), 0)
            self.assertEqual(cli.main(["orders", "--status", "paid"]), 0)
        self.assertEqual(command.call_args_list,
                         [mock.call(status=None), mock.call(status="paid")])


# ---- the Discord card -----------------------------------------------------------------------
class DiscordCardTests(_Case):
    def test_the_card_names_the_recipient_and_shows_the_whole_message(self) -> None:
        paragraphs = "\n\n".join(
            f"Paragraph {i}: your signup form now turns away disposable addresses, "
            "and every check is logged for you." for i in range(60))
        body = f"Hi Sam,\n\n{paragraphs}\n\nBest,\nIan"
        body = body[:4990] + "\nThe end."
        self.assertGreater(len(body), 4900)
        payload = message(body_text=body, subject="Your *setup* is delivered")
        out = self.app.run_task(EMAIL, payload, permissions=[EMAIL])
        self.assertEqual(out["status"], "pending_approval", out)

        token_file = self.root / "secrets" / "discord-bot-token.txt"
        token_file.write_text(DISCORD_TOKEN, encoding="utf-8")
        fake = FakeDiscord()
        gate = DiscordGate.for_app(
            self.app,
            DiscordGateSettings(state_root=self.root, channel_id=CHANNEL, owner_user_id=OWNER,
                                token_file=token_file, api_base=API, poll_seconds=0.01),
            opener=fake, sleep=lambda _s: None,
        )
        self.assertTrue(gate.run_once())
        posts = [p["content"] for p in fake.posts()]
        self.assertGreaterEqual(len(posts), 3)              # split, not cut
        head = posts[0]
        first = head.split("\n")[0]
        self.assertEqual(first, client_email_line(CLIENT))
        self.assertEqual(first, "\u2709\ufe0f **EMAILS A CLIENT** - the message below is "
                                f"sent to `{CLIENT}` if you approve.")
        self.assertIn(f"**Order:** `{ORDER_ID}`", head)
        self.assertIn(f"**To:** `{CLIENT}`", head)
        self.assertIn(f"**Subject:** {_escape(payload['subject'])}", head)
        self.assertIn("`client.email`", head)
        self.assertIn(f"order {ORDER_ID}", head)            # the summary names the order
        text = "\n".join(line for chunk in posts for line in chunk.split("\n")
                         if not line.startswith("```"))
        self.assertIn(body, text)                           # the whole message, verbatim
        self.assertIn("```text", "\n".join(posts))          # in a plain-text block
        self.assertEqual(self.world.calls, [])              # showing it sends nothing
        self.assertEqual(len(self.app.approvals.pending()), 1)


# ---- the real opener ----------------------------------------------------------------------
class _Recorder(BaseHTTPRequestHandler):
    seen: ClassVar[list[dict[str, Any]]]

    def _reply(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        record = {"method": self.command, "path": self.path, "body": body,
                  "headers": {k.lower(): v for k, v in self.headers.items()}}
        type(self).seen.append(record)
        if record["headers"].get("x-dash-token") != OPS_TOKEN:
            status, out = 401, {"ok": False, "error": "unauthorized"}
        elif self.path.startswith("/dash/orders?") or self.path == "/dash/orders":
            status, out = 200, {"ok": True, "orders": [an_order()]}
        elif self.path == "/dash/orders/email":
            sent = json.loads(body)
            status, out = ((200, {"ok": True, "id": "m1"}) if sent["to"] == CLIENT
                           else (409, {"ok": False, "error": "to: does not match the order"}))
        elif self.path == "/dash/orders/status":
            sent = json.loads(body)
            status, out = 200, {"ok": True, **sent}
        else:
            status, out = 404, {"ok": False, "error": "not found"}
        raw = json.dumps(out).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = _reply

    def log_message(self, *_a: Any) -> None:
        pass


class RealOpenerTests(unittest.TestCase):
    """The adapter's real urllib opener against a real loopback HTTP server: the path the
    owner's approval takes. content.publish's first real run crashed here with every fake
    green."""

    def test_the_three_calls_through_the_real_opener(self) -> None:
        seen: list[dict[str, Any]] = []
        handler = type("Handler", (_Recorder,), {"seen": seen})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        with tempfile.TemporaryDirectory() as tmp:
            token = Path(tmp) / "scrooge-ops-token.txt"
            token.write_text(OPS_TOKEN, encoding="utf-8")
            adapter = ClientAdapter(ClientSettings(base_url=base, token_file=token))
            sent = dict(adapter.execute(Task(EMAIL, message())).output)     # real opener
            wrong = dict(adapter.execute(Task(EMAIL, message(to="x@example.com"))).output)
            listed = dict(adapter.execute(Task(ORDERS, {"status": "paid"})).output)
            moved = dict(adapter.execute(Task(SET_STATUS, {"order_id": ORDER_ID,
                                                           "status": "in_progress"})).output)
            code, report = adapter.list_orders()
        self.assertEqual(sent, {"ok": True, "id": "m1"})
        self.assertEqual(wrong["refused"], "to: does not match the order")
        self.assertEqual(listed["orders"][0]["id"], ORDER_ID)
        self.assertEqual(moved, {"ok": True, "order_id": ORDER_ID, "status": "in_progress"})
        self.assertEqual((code, report["count"]), (0, 1))
        self.assertEqual([(r["method"], r["path"]) for r in seen], [
            ("POST", "/dash/orders/email"), ("POST", "/dash/orders/email"),
            ("GET", "/dash/orders?status=paid"), ("POST", "/dash/orders/status"),
            ("GET", "/dash/orders")])
        post = seen[0]
        self.assertEqual(post["headers"]["x-dash-token"], OPS_TOKEN)
        self.assertEqual(post["headers"]["content-type"], "application/json")
        self.assertEqual(json.loads(post["body"]), message())
        self.assertEqual(seen[2]["body"], b"")


if __name__ == "__main__":
    unittest.main()
