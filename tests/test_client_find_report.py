"""A "Find it for me" report reaches a client only after the owner's yes, and only with
links he has seen on the card.

``client.find_report`` parks on every call - even for a caller holding its permission - runs
once after an approval and never after a denial. The report necessarily links to
third-party shops (which ``client.email`` keeps refusing), so every link is declared in
``links`` and checked before anything is parked: https: only, a public DNS name (no IP, no
localhost/.local/.internal), no password, no port; every link in the body is declared and
every declared link is in the body. The card lists each link with its domain in bold, above
the whole report. Sending is ``client.email``'s Scrooge call with ``client.email``'s answers,
and the ops token never leaves.

Scrooge is faked at the HTTP opener (test_client_adapter.FakeScrooge); nothing touches the
network.
"""

from __future__ import annotations

import io
import json
import logging
import tempfile
import threading
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

from test_client_adapter import (
    CLIENT,
    OPS_TOKEN,
    ORDER_ID,
    SCROOGE,
    FakeScrooge,
    _Recorder,
    _settings,
    message,
)
from test_discord_gate import API, CHANNEL, OWNER, FakeDiscord
from test_discord_gate import TOKEN as DISCORD_TOKEN

from pionir.adapters.clients import (
    CHECK_BEFORE_RETRY,
    EMAIL,
    FIND_REPORT,
    INTERNAL_NAMES,
    MAX_FIND_LINKS,
    ClientAdapter,
    ClientSettings,
    check_email,
    check_find_report,
)
from pionir.bootstrap import build_runtime
from pionir.contracts import RiskLevel, Task
from pionir.crew import contentcheck
from pionir.discord_gate import (
    DiscordGate,
    DiscordGateSettings,
    _escape,
    find_report_line,
    render_request,
)
from pionir.errors import AdapterProtocolError, AdapterUnavailable
from pionir.server import PionirApp

# An ops token shaped like the real one, assembled at run time so no scanner mistakes the
# test for a leak. "+/=" makes its URL-quoted form differ, so both forms are looked for.
FIND_TOKEN = "fr" + "OPS" + "fake" + "0123456789" + "abcdefXYZ" + "+/="

SHOP = "https://www.example-shop.com/p/123"
LAMPS = "https://lamps.example.org/item?id=9&colour=green"
BODY = (
    "Hi Sam,\n\nHere is where to buy the brass desk lamp you asked about:\n\n"
    f"1. Example Shop has it for $42 (model WH-1000, in stock): {SHOP}\n"
    f"2. The green one is here: {LAMPS}.\n\n"
    "Both ship to the UK within a week. Best,\nIan at Dokaz\n"
)
OTHER = "https://other-shop.example.net/lamp"


def report(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "order_id": ORDER_ID,
        "to": CLIENT,
        "subject": "Your lamp: where to buy it",
        "body_text": BODY,
        "links": [SHOP, LAMPS],
    }
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def with_link(url: str) -> dict[str, Any]:
    """The report with one more link, declared AND written in the body."""
    return report(links=[SHOP, LAMPS, url], body_text=BODY + f"\n3. Also: {url}\n")


class _Case(unittest.TestCase):
    """A hermetic runtime with the client adapter talking to FakeScrooge."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.token_file = self.root / "secrets" / "scrooge-ops-token.txt"
        self.token_file.parent.mkdir(parents=True)
        self.token_file.write_text(FIND_TOKEN + "\n", encoding="utf-8")
        self.world = FakeScrooge()
        self.world.valid_tokens = {FIND_TOKEN}
        self.adapter = ClientAdapter(ClientSettings(base_url=SCROOGE,
                                                    token_file=self.token_file),
                                     opener=self.world)
        runtime = build_runtime(_settings(self.root, content_url=None))
        runtime.register(self.adapter)
        self.app = PionirApp(runtime)

    def tearDown(self) -> None:
        self.app.runtime.cortex.close()
        self._tmp.cleanup()

    def execute(self, capability: str = FIND_REPORT,
                payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if payload is None:
            payload = report() if capability == FIND_REPORT else message()
        return dict(self.adapter.execute(Task(capability, payload)).output)

    def park(self, payload: dict[str, Any] | None = None) -> str:
        out = self.app.run_task(FIND_REPORT, payload or report(), permissions=[FIND_REPORT])
        self.assertEqual(out["status"], "pending_approval", out)
        return str(out["approval_id"])

    def send(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Park, approve, wait: the only way a report goes out."""
        aid = self.park(payload)
        res = self.app.approve(aid)
        self.assertTrue(self.app.jobs.wait(res["task_id"], 30))
        return self.app.approvals.get(aid)


# ---- the gate -----------------------------------------------------------------------------
class GateTests(_Case):
    def test_the_declaration(self) -> None:
        caps = {c.name: c for c in self.adapter.manifest.capabilities}
        cap = caps[FIND_REPORT]
        self.assertEqual(FIND_REPORT, "client.find_report")
        self.assertIs(cap.risk, RiskLevel.PRIVILEGED)
        self.assertTrue(cap.requires_approval)
        self.assertFalse(cap.routable)
        self.assertEqual(cap.required_permissions, frozenset({FIND_REPORT}))

    def test_it_parks_every_time_even_with_the_permission(self) -> None:
        for _ in range(3):
            self.park()
        pending = self.app.approvals.pending()
        self.assertEqual(len(pending), 3)
        self.assertIn(f"order {ORDER_ID}", pending[0]["summary"])
        self.assertIn(report()["subject"], pending[0]["summary"])
        self.assertEqual(self.world.calls, [])

    def test_an_approved_report_is_sent_once_as_client_email_sends(self) -> None:
        row = self.send()
        self.assertEqual(row["status"], "approved", row)
        self.assertEqual(self.world.steps(), ["email"])
        (call,) = self.world.calls
        self.assertEqual((call["method"], call["url"]), ("POST", f"{SCROOGE}/dash/orders/email"))
        self.assertEqual(call["token"], FIND_TOKEN)
        self.assertEqual(call["content_type"], "application/json")
        # Scrooge's email fields exactly; the links are all in the body already
        expected = report()
        del expected["links"]
        self.assertEqual(call["body"], expected)
        self.assertEqual(row["result"]["result"], {"ok": True, "id": "msg-1"})
        self.assertIn("client:message:msg-1", row["result"]["evidence"])
        self.assertIn(f"client:order:{ORDER_ID}", row["result"]["evidence"])
        self.assertFalse(self.app.approve(row["id"])["ok"])        # never twice
        self.assertEqual(len(self.world.calls), 1)

    def test_a_denied_report_is_never_sent(self) -> None:
        aid = self.park()
        self.assertTrue(self.app.deny(aid)["ok"])
        self.assertFalse(self.app.approve(aid)["ok"])
        self.assertEqual(self.world.calls, [])

    def test_a_report_with_no_links(self) -> None:
        payload = report(links=[], body_text="Hi Sam,\n\nI could not find that lamp for "
                                             "sale anywhere this week. Sorry!\n\nIan")
        row = self.send(payload)
        self.assertEqual(row["status"], "approved", row)
        self.assertEqual(self.world.steps(), ["email"])


# ---- the check before parking ------------------------------------------------------------
class LocalValidationTests(_Case):
    # name -> (the bad report, words the refusal must carry)
    BAD: ClassVar[dict[str, tuple[dict[str, Any], str]]] = {
        "an http link": (with_link("http://lamps.example.org/x"), "must start with https://"),
        "an IPv4 host": (with_link("https://93.184.216.34/lamp"), "an IP address"),
        "an IPv6 host": (with_link("https://[2606:2800:220:1::1]/lamp"), "an IP address"),
        "a broken IPv6 host": (with_link("https://[::1/lamp"), "an IP address"),
        "a loopback IP": (with_link("https://127.0.0.1/lamp"), "an IP address"),
        "a decimal IP": (with_link("https://1572395042/lamp"), "a website's name"),
        "a hex IP": (with_link("https://0x5d.0xb8/lamp"), "not a number"),
        "localhost": (with_link("https://localhost/lamp"), "local or internal"),
        "LOCALHOST": (with_link("https://LOCALHOST/lamp"), "local or internal"),
        "a .local host": (with_link("https://printer.local/lamp"), "local or internal"),
        "an .internal host": (with_link("https://shop.internal/lamp"), "local or internal"),
        "a sub.localhost host": (with_link("https://a.localhost/lamp"), "local or internal"),
        "a user and password": (with_link("https://sam:pw@www.example-shop.com/p/1"),
                                "user name or password"),
        "a lookalike with userinfo": (with_link("https://www.example-shop.com@evil.example.net/"),
                                      "user name or password"),
        "a port": (with_link("https://www.example-shop.com:8443/p/1"), "port"),
        "an empty port": (with_link("https://www.example-shop.com:/p/1"), "port"),
        "a backslash": (with_link("https://www.example-shop.com\\@evil.example.net/"),
                        "backslash"),
        "a javascript: link": (report(links=[SHOP, LAMPS, "javascript:alert(1)"]),
                               "javascript:"),
        "a data: link": (report(links=[SHOP, LAMPS, "data:text/html,hi"]), "data:"),
        "javascript: inside a link": (with_link("https://shop.example.com/?next=javascript:x"),
                                      "javascript:"),
        "a non-ASCII link": (with_link("https://www.example-shop.com/p/café"),
                             "non-ASCII"),
        "a link ending in punctuation": (report(links=[SHOP, LAMPS, SHOP + "."]),
                                         "read the same"),
        "a link with parentheses": (report(links=[SHOP, LAMPS, SHOP + "_(x)"]),
                                    "read the same"),
        "a too-long link": (with_link("https://www.example-shop.com/" + "a" * 480),
                            "at most 500"),
        "a single-label host": (with_link("https://shop/lamp"), "a website's name"),
        "a link listed twice": (report(links=[SHOP, SHOP, LAMPS]), "listed twice"),
        "more than 15 links": (report(
            links=[f"https://shop{i}.example.com/p" for i in range(MAX_FIND_LINKS + 1)],
            body_text="Shops: " + " ".join(f"https://shop{i}.example.com/p"
                                           for i in range(MAX_FIND_LINKS + 1))),
            "at most 15"),
        "links as a string": (report(links=SHOP), "a list"),
        "a missing links field": (report(links=None), "links: required"),
        "a link in the body that is not listed": (
            report(body_text=BODY + f"\nOr try {OTHER} too.\n"), "not listed in links"),
        "a listed link case-changed in the body": (
            report(body_text=BODY.replace(SHOP, SHOP.replace("www.", "WWW."))),
            "not listed in links"),
        "an http link in the body only": (
            report(body_text=BODY + "\nOr http://lamps.example.org/x\n"), "https://"),
        "an IP link in the body only": (
            report(body_text=BODY + "\nOr https://10.0.0.5/admin\n"), "an IP address"),
        "a localhost link in the body only": (
            report(body_text=BODY + "\nOr https://localhost/admin\n"), "local or internal"),
        "a listed link not in the body": (report(links=[SHOP, LAMPS, OTHER]),
                                          "not in the report"),
        "an email address in the body": (
            report(body_text=BODY + "\nOr write to sales@lampmakers.co.uk.\n"),
            "no email addresses"),
        "an example.com address in the body": (
            report(body_text=BODY + "\nOr write to sales@example.com.\n"), "no email addresses"),
        "a phone number in the body": (
            report(body_text=BODY + "\nOr call them on +44 20 7946 0958.\n"), "no phone numbers"),
        "a US phone number in the body": (
            report(body_text=BODY + "\nOr call (555) 123-4567.\n"), "no phone numbers"),
        "tag-shaped text": (report(body_text=BODY + "Ready in <3 days>."), "tag-shaped"),
        "an HTML link": (report(body_text=BODY + f'<a href="{SHOP}">here</a>'), "tag-shaped"),
        "an autolink": (report(body_text=BODY.replace(SHOP, f"<{SHOP}>")), "tag-shaped"),
        "an HTML comment": (report(body_text=BODY + "<!-- x -->"), "tag-shaped"),
        "a mailto: link": (report(body_text=BODY + "mailto:someone@lampmakers.co.uk"),
                           "mailto:"),
        "a bare www address": (report(body_text=BODY + "Or www.lampmakers.co.uk"),
                               "write a link as https://"),
        "a bare host and path": (report(body_text=BODY + "Or lampmakers.co.uk/lamps"),
                                 "write a link as https://"),
        "an internal name in the body": (report(body_text=BODY + "Moss found these for you."),
                                         "internal system"),
        "an internal name in the subject": (report(subject="Galatea's picks for your lamp"),
                                            "internal system"),
        "a link in the subject": (report(subject=f"Your lamp: {SHOP}"), "no links here"),
        "a tab in the body": (report(body_text=BODY + "\tindented"), "control character"),
        "a zero-width space": (report(body_text=BODY + "\u200b"), "invisible"),
        "a newline in the subject": (report(subject="Your lamp\nBcc: x@evil.example"),
                                     "control character"),
        "a short body": (report(links=[], body_text="Here it is."), "20-5000"),
        # Scrooge refuses a body over 5,000 characters: refused here, before a wasted yes
        "a body over Scrooge's 5,000": (report(body_text=BODY + "x" * (5001 - len(BODY))),
                                        "20-5000"),
        "a short subject": (report(subject="Hi"), "5-120"),
        "a bad order id": (report(order_id="0123456789AB"), "order_id"),
        "a bad address": (report(to="not-an-address"), "to:"),
        "an extra field": (report(cc="x@evil.example"), "not a find report field"),
    }

    def test_every_rule_is_enforced_before_anything_is_sent_or_parked(self) -> None:
        for name, (payload, words) in self.BAD.items():
            with self.subTest(name):
                with self.assertRaises(ValueError) as checked:
                    check_find_report(payload)
                self.assertIn(words, str(checked.exception))
                with self.assertRaises(AdapterProtocolError) as err:
                    self.adapter.validate(Task(FIND_REPORT, payload))
                self.assertIn("client.find_report refused by Pionir", str(err.exception))
                self.assertIn(words, str(err.exception))
                out = self.app.run_task(FIND_REPORT, payload, permissions=[FIND_REPORT])
                self.assertEqual(out["status"], "error", out)
                self.assertEqual(out["error"]["type"], "AdapterProtocolError")
                # checked again at execute(): an approval of a bad report still sends nothing
                with self.assertRaises(AdapterProtocolError):
                    self.adapter.execute(Task(FIND_REPORT, payload))
        self.assertEqual(self.app.approvals.pending(), [])
        self.assertEqual(self.world.calls, [])

    def test_a_password_in_a_link_is_not_echoed(self) -> None:
        with self.assertRaises(AdapterProtocolError) as err:
            self.adapter.validate(Task(FIND_REPORT,
                                       with_link("https://sam:hunter2@www.example-shop.com/")))
        self.assertNotIn("hunter2", str(err.exception))

    def test_what_is_allowed(self) -> None:
        fifteen = [f"https://shop{i}.example.com/p/{i}?ref=find#top" for i in range(15)]
        long_body = BODY + "\n" + "Notes. " * ((5000 - len(BODY) - 1) // 7)
        long_body += "x" * (5000 - len(long_body))
        self.assertEqual(len(long_body), 5000)
        good = {
            "the example": report(),
            "fifteen links": report(links=fifteen,
                                    body_text="Shops:\n" + "\n".join(fifteen)),
            "exact limits": report(subject="S" * 120, body_text=long_body),
            "shortest": report(subject="Hello", links=[], body_text="b" * 20),
            "no links": report(links=[], body_text="Hi Sam, nothing found this week."),
            "sentence punctuation and parentheses": report(
                body_text=f"First ({SHOP}), then {LAMPS}! Done."),
            "a link written twice": report(body_text=BODY + f"\nAgain: {SHOP}\n"),
            "a Dokaz link": report(links=[SHOP, LAMPS, "https://api.dokaz.net/orders/help"],
                                   body_text=BODY + "Help: https://api.dokaz.net/orders/help"),
            "an internationalized domain": with_link("https://xn--bcher-kva.example.com/p"),
            "CRLF line breaks": report(body_text=BODY.replace("\n", "\r\n")),
            "a price and a model number": report(
                body_text=BODY + "It is $1,299.99, model WH-1000XM5, ISBN 978-0-13-468599-1."),
            "a plain less-than": report(body_text=BODY + "Ships in < 5 days."),
        }
        for name, payload in good.items():
            with self.subTest(name):
                self.adapter.validate(Task(FIND_REPORT, payload))
                self.assertEqual(check_find_report(payload), payload)
        self.assertEqual(self.world.calls, [])

    def test_client_email_still_refuses_links_to_shops(self) -> None:
        """The same text that passes as a find report is refused as a plain email: the
        shops' links go out only through client.find_report and its card."""
        email = {k: v for k, v in report().items() if k != "links"}
        with self.assertRaisesRegex(ValueError, "links may only go to"):
            check_email(email)
        with self.assertRaises(AdapterProtocolError):
            self.adapter.validate(Task(EMAIL, email))
        out = self.app.run_task(EMAIL, email, permissions=[EMAIL])
        self.assertEqual(out["error"]["type"], "AdapterProtocolError")
        with self.assertRaises(ValueError):       # and it takes no links field
            check_email(report())
        self.assertEqual(self.app.approvals.pending(), [])
        self.assertEqual(self.world.calls, [])

    def test_the_internal_names_are_the_content_checks(self) -> None:
        self.assertEqual(INTERNAL_NAMES, contentcheck.INTERNAL_NAMES)

    def test_a_missing_token_is_unavailable_before_parking(self) -> None:
        self.token_file.unlink()
        with self.assertRaisesRegex(AdapterUnavailable, "not configured"):
            self.adapter.validate(Task(FIND_REPORT, report()))
        parked = self.app.run_task(FIND_REPORT, report(), permissions=[FIND_REPORT])
        self.assertEqual(parked["error"]["type"], "AdapterUnavailable")
        self.assertEqual(self.app.approvals.pending(), [])
        out = self.execute()
        self.assertTrue(out["not_configured"])
        self.assertEqual(self.world.calls, [])

    def test_status_is_local(self) -> None:
        self.assertEqual(self.adapter.status(), {"url": SCROOGE, "token": "configured"})
        self.assertEqual(self.world.calls, [])


# ---- what Scrooge says ---------------------------------------------------------------------
class ErrorTests(_Case):
    def test_the_mapping_is_client_emails(self) -> None:
        cases = [
            (200, {"ok": True, "id": "m9"}),
            (400, {"ok": False, "error": "body_text: must be 20-5000 characters"}),
            (409, {"ok": False, "error": "to: does not match the order"}),
            (429, {"ok": False, "error": "rate"}),
            (401, {"ok": False, "error": "no"}),
            (403, {"ok": False, "error": "no"}),
            (500, {"ok": False, "error": "boom"}),
            (502, b"<html>bad gateway</html>"),
            (503, {"ok": False, "error": "RESEND_API_KEY is not set; nothing was sent"}),
            (502, {"ok": False, "error": "the mail provider refused it"}),
            (504, {"ok": False, "error": "timeout"}),
            (200, {"sent": True}),
        ]
        for status, body in cases:
            with self.subTest(status=status, body=body):
                self.world.forced = {"email": (status, body)}
                found = self.execute(FIND_REPORT, report())
                emailed = self.execute(EMAIL, message())
                self.assertEqual(found, emailed)
        # the words that matter to the owner, to be sure the comparison compared something
        self.world.forced = {"email": (500, {"ok": False, "error": "boom"})}
        self.assertIn(CHECK_BEFORE_RETRY, self.execute()["unavailable"])
        self.world.forced = {"email": (503, {"ok": False, "error": "not set"})}
        self.assertIn("nothing was sent", self.execute()["unavailable"])
        self.world.forced = {}
        self.world.down = True
        out = self.execute()
        self.assertEqual(out, self.execute(EMAIL, message()))
        self.assertIn(CHECK_BEFORE_RETRY, out["unavailable"])

    def test_a_mismatched_address_through_the_gate(self) -> None:
        row = self.send(report(to="someone.else@example.com"))
        self.assertEqual(row["status"], "approved_failed")
        self.assertEqual(row["result"]["result"]["refused"], "to: does not match the order")


# ---- the token never leaves ------------------------------------------------------------------
class SecrecyTests(_Case):
    def test_the_token_never_appears_in_results_errors_logs_or_records(self) -> None:
        capture = io.StringIO()
        handler = logging.StreamHandler(capture)
        root = logging.getLogger()
        old = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(handler)
        echo = f"bad token {FIND_TOKEN} / {urllib.parse.quote_plus(FIND_TOKEN)}"
        try:
            outputs: list[Any] = []
            for status in (400, 409, 429, 500, 502, 401):
                self.world.forced = {"email": (status, {"ok": False, "error": echo})}
                outputs.append(self.execute())
            self.world.forced = {"email": (200, {"ok": True, "id": echo})}
            outputs.append(self.execute())
            outputs.append(self.send())          # through the gate, audited and stored
            self.world.forced = {}
            self.world.down = True
            outputs.append(self.execute())
            self.world.down = False
            self.world.valid_tokens = set()
            outputs.append(self.send())          # a rejected token, through the gate
        finally:
            root.removeHandler(handler)
            root.setLevel(old)
        self.assertIn(FIND_TOKEN, {c["token"] for c in self.world.calls})
        dumped = json.dumps(outputs, default=str)
        self.assertIn("<redacted>", dumped)
        self.assertNotIn(FIND_TOKEN, dumped)
        self.assertNotIn(urllib.parse.quote_plus(FIND_TOKEN), dumped)
        self.assertNotIn(FIND_TOKEN, capture.getvalue())
        self.assertIn("client: find report for order", capture.getvalue())
        for path in self.root.rglob("*"):
            if path.is_file() and path != self.token_file:
                self.assertNotIn(FIND_TOKEN.encode("utf-8"), path.read_bytes(), path)


# ---- the Discord card ------------------------------------------------------------------------
class DiscordCardTests(_Case):
    def test_the_card_lists_every_website_and_shows_the_whole_report(self) -> None:
        links = [f"https://www.shop{i}-lamps.example.com/p/{i}?colour=brass" for i in range(14)]
        links.append(SHOP)
        paragraphs = "\n\n".join(
            f"Option {i + 1}: a brass desk lamp, in stock, shipping in 3-5 days - "
            f"{link} - the closest match to your photo." for i, link in enumerate(links))
        body = f"Hi Sam,\n\n{paragraphs}\n\n"
        body += "More notes on each shop, one per line.\n" * ((4980 - len(body)) // 39)
        body += "\n\nBest,\nIan"
        self.assertGreater(len(body), 4900)
        self.assertLessEqual(len(body), 5000)
        payload = report(body_text=body, links=links, subject="Your *lamp*: where to buy it")
        self.park(payload)

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
        self.assertGreaterEqual(len(posts), 5)               # split, not cut
        head = posts[0]
        first = head.split("\n")[0]
        self.assertEqual(first, find_report_line(CLIENT))
        self.assertEqual(first, "\U0001f50e **SENDS A FIND REPORT** - the report below goes "
                                f"to `{CLIENT}` if you approve.")
        self.assertIn(f"**Order:** `{ORDER_ID}`", head)
        self.assertIn(f"**To:** `{CLIENT}`", head)
        self.assertIn(f"**Subject:** {_escape(payload['subject'])}", head)
        self.assertIn("`client.find_report`", head)
        whole = "\n".join(posts)
        for link in links:                                   # every link, domain first
            domain = urllib.parse.urlsplit(link).hostname
            self.assertIn(f"• **{_escape(domain)}** — <{link}>", whole)
        self.assertIn("**The websites it sends the client to (15):** ", whole)
        text = "\n".join(line for chunk in posts for line in chunk.split("\n")
                         if not line.startswith("```"))
        self.assertIn(body, text)                            # the whole report, verbatim
        self.assertIn("```text", whole)
        self.assertEqual(whole.count(body[:200]), 1)         # once, not again in the JSON
        self.assertEqual(self.world.calls, [])               # showing it sends nothing
        self.assertEqual(len(self.app.approvals.pending()), 1)

    def test_the_link_list_comes_before_the_report(self) -> None:
        row = {"id": "a1", "capability": FIND_REPORT, "payload": report(),
               "summary": "order x", "requester": "moss"}
        card = render_request(row, OWNER)
        self.assertLess(card.index("**www.example-shop.com** — <" + SHOP + ">"),
                        card.index("**The report, in full"))
        self.assertIn("**lamps.example.org** — <" + LAMPS + ">", card)
        self.assertIn("**The websites it sends the client to (2):** "
                      "**www.example-shop.com**, **lamps.example.org**", card)
        none = render_request({**row, "payload": report(links=[], body_text="b" * 30)}, OWNER)
        self.assertIn("**Links:** none", none)


# ---- the real opener --------------------------------------------------------------------------
class RealOpenerTests(unittest.TestCase):
    def test_a_report_through_the_real_opener(self) -> None:
        seen: list[dict[str, Any]] = []
        handler = type("Handler", (_Recorder,), {"seen": seen})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        with tempfile.TemporaryDirectory() as tmp:
            token = Path(tmp) / "scrooge-ops-token.txt"
            token.write_text(OPS_TOKEN, encoding="utf-8")       # the recorder's token
            adapter = ClientAdapter(ClientSettings(base_url=base, token_file=token))
            sent = dict(adapter.execute(Task(FIND_REPORT, report())).output)
        self.assertEqual(sent, {"ok": True, "id": "m1"})
        (post,) = seen
        self.assertEqual((post["method"], post["path"]), ("POST", "/dash/orders/email"))
        expected = report()
        del expected["links"]
        self.assertEqual(json.loads(post["body"]), expected)


if __name__ == "__main__":
    unittest.main()
