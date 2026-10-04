"""No newsletter goes out without the owner's yes, and none goes out twice.

``content.newsletter_send`` parks on every call - its own card, never the digest - runs once
after an approval and never after a denial. These tests pin the adapter (the check before
parking, the ledger that keeps a newsletter from being queued twice, Scrooge's 409 recorded,
the error mapping and the publish token never leaking) and the Discord card that shows the
whole email with the footer Scrooge adds to every copy.

Scrooge is faked at the HTTP opener with the real opener's signature
``(request, data=None, timeout=None)``; nothing touches the network.
"""

from __future__ import annotations

import io
import json
import logging
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from typing import Any, Self
from unittest import mock

from test_discord_gate import API, CHANNEL, OWNER, FakeDiscord
from test_discord_gate import TOKEN as DISCORD_TOKEN

from pionir.adapters import newsletter
from pionir.adapters.content import ContentSettings
from pionir.adapters.newsletter import (
    FOOTER_LINES,
    ROUTE,
    SEND,
    NewsletterAdapter,
    NewsletterSettings,
    check_newsletter,
    utm_campaign,
    utm_query,
)
from pionir.batching import approval_level
from pionir.bootstrap import build_runtime
from pionir.config import PionirSettings
from pionir.contracts import RiskLevel, Task
from pionir.discord_gate import NEWSLETTER_LINE, DiscordGate, DiscordGateSettings, _escape
from pionir.errors import AdapterProtocolError, AdapterUnavailable
from pionir.server import PionirApp

TOKEN = "publishSECRETtoken0123456789abcdefXYZ"
SCROOGE = "https://api.dokaz.test"
NID = "weekly-2026-w40"
Q = utm_query(NID)


def make_body(nid: str = NID, extra: str = "") -> str:
    q = utm_query(nid)
    lines = [
        "This week from Dokaz Industries: what we published, and the tools on sale.",
        "",
        "## New on the blog",
        "",
        (f"- [Check an email address first](https://api.dokaz.net/blog/verify-email?{q}) - "
         "why a quick check of syntax and mail server keeps messages out of the void."),
        "",
        "## Built for you",
        "",
        ("We also build small custom tools and automations to a written brief. Tell us what "
         f"you need on the [hire page](https://api.dokaz.net/hire?{q})."),
    ]
    if extra:
        lines += ["", extra]
    return "\n".join(lines) + "\n"


def letter(**over: Any) -> dict[str, Any]:
    base = {"newsletter_id": NID, "subject": "Dokaz weekly: a new post",
            "body_md": make_body()}
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


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
    """POST /dash/content/newsletter as Scrooge answers it: the token checked, a newsletter
    id taken once (409 after), the contract's 200 shape."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.taken: dict[str, str] = {}
        self.forced: tuple[int, Any] | None = None
        self.down = False
        self._lock = threading.Lock()

    def __call__(self, request: Any, timeout: float | None = None) -> _Response:
        with self._lock:
            return self._handle(request, timeout)

    @staticmethod
    def _error(url: str, status: int, payload: Any) -> None:
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        raise urllib.error.HTTPError(url, status, "error", {},  # type: ignore[arg-type]
                                     io.BytesIO(raw))

    def _handle(self, request: Any, timeout: float | None) -> _Response:
        url = request.full_url
        body = json.loads(request.data) if request.data else None
        self.calls.append({"method": request.get_method(), "url": url, "body": body,
                           "token": request.get_header("X-dash-token"), "timeout": timeout})
        if self.down:
            raise urllib.error.URLError(f"connection refused: {url} token={TOKEN}")
        if self.forced is not None:
            status, payload = self.forced
            if status >= 400:
                self._error(url, status, payload)
            return _Response(status, payload)
        if request.get_header("X-dash-token") != TOKEN:
            self._error(url, 401, {"ok": False, "error": "token required in the x-dash-token "
                                                         "header"})
        nid = body["newsletter_id"]
        if nid in self.taken:
            self._error(url, 409, {"ok": False, "error": f"newsletter_id: already queued at "
                                   f"2026-10-04T12:00:00.000Z (send {self.taken[nid]})",
                                   "send_id": self.taken[nid], "status": "queued"})
        send_id = f"nl_{len(self.taken) + 1:024x}"
        self.taken[nid] = send_id
        return _Response(200, {"ok": True, "newsletter_id": nid, "send_id": send_id,
                               "status": "queued", "recipients": 2,
                               "queued_at": "2026-10-04T12:00:00.000Z"})

    def posts(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["method"] == "POST"]


def _settings(root: Path, **kw: Any) -> PionirSettings:
    return PionirSettings(
        state_root=root,
        atani_command=("pionir-test-no-such-binary",),
        daedalus_url=None, melete_url=None, galatea_url=None, crew_url=None,
        bryo_status_command=None, nyx_status_command=None, voodoo_status_command=None,
        embed_model=None, evict_to_fit=False, **kw,
    )


class _Case(unittest.TestCase):
    """A hermetic runtime with the newsletter adapter talking to FakeScrooge."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.token_file = self.root / "secrets" / "scrooge-publish-token.txt"
        self.token_file.parent.mkdir(parents=True)
        self.token_file.write_text(TOKEN + "\n", encoding="utf-8")
        self.ledger = self.root / "newsletter" / "sends.json"
        self.scrooge = FakeScrooge()
        self.adapter = self.make_adapter()
        # content_url=None: no default content adapters; the faked one is used
        runtime = build_runtime(_settings(self.root, content_url=None))
        runtime.register(self.adapter)
        self.app = PionirApp(runtime)
        newsletter._SENT.clear()

    def make_adapter(self) -> NewsletterAdapter:
        return NewsletterAdapter(NewsletterSettings(
            content=ContentSettings(base_url=SCROOGE, token_file=self.token_file),
            ledger_file=self.ledger), opener=self.scrooge, clock=lambda: "2026-10-04T12:00:00Z")

    def tearDown(self) -> None:
        self.app.runtime.cortex.close()
        newsletter._SENT.clear()
        self._tmp.cleanup()

    def execute(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return dict(self.adapter.execute(Task(SEND, payload or letter())).output)

    def run_send(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Park, approve, wait: the only way a newsletter goes out."""
        out = self.app.run_task(SEND, payload or letter(), permissions=[SEND])
        self.assertEqual(out["status"], "pending_approval", out)
        res = self.app.approve(out["approval_id"])
        self.assertTrue(self.app.jobs.wait(res["task_id"], 30))
        return self.app.approvals.get(out["approval_id"])


# ---- the gate ---------------------------------------------------------------------
class GateTests(_Case):
    def test_it_is_privileged_always_approved_never_routed_and_never_in_the_digest(self) -> None:
        (cap,) = self.adapter.manifest.capabilities
        self.assertEqual(cap.name, "content.newsletter_send")
        self.assertTrue(cap.requires_approval)
        self.assertIs(cap.risk, RiskLevel.PRIVILEGED)
        self.assertFalse(cap.routable)
        self.assertFalse(cap.batchable)
        self.assertEqual(approval_level(cap), "card")

    def test_it_parks_every_time_even_with_the_permission(self) -> None:
        for _ in range(3):
            out = self.app.run_task(SEND, letter(), permissions=[SEND])
            self.assertEqual(out["status"], "pending_approval")
        self.assertEqual(len(self.app.approvals.pending()), 3)
        self.assertEqual(self.scrooge.calls, [])

    def test_an_approved_newsletter_is_queued_once_exactly_as_checked(self) -> None:
        row = self.run_send()
        self.assertEqual(row["status"], "approved", row)
        (post,) = self.scrooge.posts()
        self.assertEqual(post["url"], SCROOGE + ROUTE)
        self.assertEqual(post["url"], "https://api.dokaz.test/dash/content/newsletter")
        self.assertEqual(post["token"], TOKEN)
        self.assertEqual(post["body"], letter())          # exactly the three fields
        result = row["result"]["result"]
        self.assertEqual(result, {"ok": True, "newsletter_id": NID,
                                  "send_id": "nl_" + "0" * 23 + "1", "status": "queued",
                                  "recipients": 2, "queued_at": "2026-10-04T12:00:00.000Z"})
        ledger = json.loads(self.ledger.read_text(encoding="utf-8"))
        self.assertEqual(ledger, {NID: {"send_id": result["send_id"], "status": "queued",
                                        "recipients": 2,
                                        "queued_at": "2026-10-04T12:00:00.000Z",
                                        "noted_at": "2026-10-04T12:00:00Z",
                                        "from": "scrooge_200"}})
        self.assertFalse(self.app.approve(row["id"])["ok"])       # never twice
        self.assertEqual(len(self.scrooge.posts()), 1)

    def test_a_denied_newsletter_never_runs(self) -> None:
        aid = self.app.run_task(SEND, letter(), permissions=[SEND])["approval_id"]
        self.assertTrue(self.app.deny(aid)["ok"])
        self.assertFalse(self.app.approve(aid)["ok"])
        self.assertEqual(self.scrooge.calls, [])
        self.assertFalse(self.ledger.exists())


# ---- the check before parking ------------------------------------------------------
class CheckTests(_Case):
    def test_every_rule_is_enforced_before_anything_is_sent_or_parked(self) -> None:
        other = "utm_source=newsletter&utm_medium=email&utm_campaign=weekly-2026-w41"
        cases = {
            "an extra field": letter(to="someone@example.com"),
            "no subject": letter(subject=None),
            "a short subject": letter(subject="Weekly"),
            "a two-line subject": letter(subject="Dokaz weekly:\na new post"),
            "a short body": letter(body_md="Too short."),
            "a bad id": letter(newsletter_id="Weekly 40"),
            "an all-dash id": letter(newsletter_id="---"),
            "raw HTML": letter(body_md=make_body(extra="<b>bold</b>")),
            "a foreign link": letter(body_md=make_body(
                extra=f"[elsewhere](https://example.com/?{Q})")),
            "an untagged link": letter(body_md=make_body(
                extra="[docs](https://api.dokaz.net/docs)")),
            "the blog's tags": letter(body_md=make_body(
                extra="[docs](https://api.dokaz.net/docs?utm_source=blog&utm_medium=referral"
                      f"&utm_campaign={NID})")),
            "another week's campaign": letter(body_md=make_body(
                extra=f"[docs](https://api.dokaz.net/docs?{other})")),
            "a tag twice": letter(body_md=make_body(
                extra=f"[docs](https://api.dokaz.net/docs?{Q}&utm_source=newsletter)")),
            "an email address": letter(body_md=make_body(extra="Write to ian@dokaz.net.")),
            "a phone number": letter(body_md=make_body(extra="Call +1 206 555 0100 now.")),
            "an image": letter(body_md=make_body(
                extra=f"![x](https://api.dokaz.net/x.png?{Q})")),
            "an open fence": letter(body_md=make_body(extra="```\ncode")),
        }
        for what, payload in cases.items():
            with self.subTest(what):
                with self.assertRaises(ValueError):
                    check_newsletter(payload)
                with self.assertRaises(AdapterProtocolError):
                    self.adapter.validate(Task(SEND, payload))
                out = self.app.run_task(SEND, payload, permissions=[SEND])
                self.assertEqual(out["status"], "error", out)
        self.assertEqual(self.app.approvals.pending(), [])
        self.assertEqual(self.scrooge.calls, [])

    def test_what_is_allowed(self) -> None:
        ok = letter(body_md=make_body(extra=(
            f"The [kit](https://dokaz.gumroad.com/l/kit?{Q}), an API call "
            "(https://api.dokaz.net/v1/qr?data=hi needs no tags), and a reply to "
            "someone@example.com reaches nobody.")))
        self.assertEqual(check_newsletter(ok), ok)
        self.assertEqual(utm_campaign("-" + "a" * 50), "a" * 39)
        self.assertEqual(utm_campaign(NID), NID)

    def test_a_missing_token_is_unavailable_before_parking(self) -> None:
        self.token_file.unlink()
        with self.assertRaisesRegex(AdapterUnavailable, "publish token"):
            self.adapter.validate(Task(SEND, letter()))
        out = self.execute()
        self.assertIs(out["not_configured"], True)
        self.assertEqual(self.scrooge.calls, [])
        with self.assertRaises(AdapterUnavailable):
            self.adapter.status()

    def test_status_is_local(self) -> None:
        self.assertEqual(self.adapter.status(), {"url": SCROOGE, "token": "configured",
                                                 "sent": 0})
        self.assertEqual(self.scrooge.calls, [])


# ---- never twice --------------------------------------------------------------------
class LedgerTests(_Case):
    def test_a_second_send_of_the_same_newsletter_is_refused_before_scrooge(self) -> None:
        first = self.execute()
        self.assertIs(first["ok"], True)
        second = self.execute()
        self.assertIs(second["ok"], False)
        self.assertIn("already sent", second["refused"])
        self.assertEqual(second["send_id"], first["send_id"])
        self.assertEqual(len(self.scrooge.posts()), 1)
        nxt = letter(newsletter_id="weekly-2026-w41", body_md=make_body("weekly-2026-w41"))
        self.assertIs(self.execute(nxt)["ok"], True)
        self.assertEqual(len(self.scrooge.posts()), 2)

    def test_a_sent_newsletter_is_refused_before_parking(self) -> None:
        self.execute()
        out = self.app.run_task(SEND, letter(), permissions=[SEND])
        self.assertEqual(out["status"], "error", out)
        self.assertIn("already sent", out["error"]["message"])
        self.assertEqual(self.app.approvals.pending(), [])

    def test_two_approvals_parked_before_either_ran_send_once(self) -> None:
        aids = [self.app.run_task(SEND, letter(), permissions=[SEND])["approval_id"]
                for _ in range(2)]
        for aid in aids:
            res = self.app.approve(aid)
            self.assertTrue(self.app.jobs.wait(res["task_id"], 30))
        rows = [self.app.approvals.get(aid) for aid in aids]
        self.assertEqual(sorted(r["status"] for r in rows), ["approved", "approved_failed"])
        self.assertEqual(len(self.scrooge.posts()), 1)

    def test_scrooge_already_having_it_is_recorded_and_refused(self) -> None:
        self.scrooge.taken[NID] = "nl_" + "a" * 24          # queued there, unknown here
        out = self.execute()
        self.assertIs(out["ok"], False)
        self.assertEqual(out["status"], 409)
        self.assertIn("already queued", out["refused"])
        self.assertEqual(out["send_id"], "nl_" + "a" * 24)
        ledger = json.loads(self.ledger.read_text(encoding="utf-8"))
        self.assertEqual(ledger[NID]["send_id"], "nl_" + "a" * 24)
        self.assertEqual(ledger[NID]["from"], "scrooge_409")
        with self.assertRaisesRegex(AdapterProtocolError, "already sent"):
            self.adapter.validate(Task(SEND, letter()))
        self.assertEqual(len(self.scrooge.posts()), 1)

    def test_an_unreadable_ledger_stops_it(self) -> None:
        self.ledger.parent.mkdir(parents=True)
        self.ledger.write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(AdapterUnavailable, "ledger"):
            self.adapter.validate(Task(SEND, letter()))
        self.assertIn("ledger", self.execute()["unavailable"])
        self.assertEqual(self.scrooge.calls, [])

    def test_a_refused_newsletter_is_not_recorded(self) -> None:
        self.scrooge.forced = (400, {"ok": False, "error": "body_md: raw HTML is not allowed"})
        out = self.execute()
        self.assertEqual(out["refused"], "body_md: raw HTML is not allowed")
        self.assertFalse(self.ledger.exists())
        self.scrooge.forced = None
        self.assertIs(self.execute()["ok"], True)

    def test_a_queued_newsletter_whose_ledger_write_fails_is_still_reported_queued(
            self) -> None:
        with mock.patch("pionir.adapters.newsletter.atomic.replace",
                        side_effect=OSError("disk")):
            out = self.execute()
        self.assertIs(out["ok"], True)
        self.assertIn("do not send this newsletter again", out["ledger_error"])
        self.assertEqual(list(self.ledger.parent.glob("*.tmp")), [])
        self.assertIn("already sent", self.execute()["refused"])     # this process knows


# ---- what Scrooge says ---------------------------------------------------------------
class ErrorTests(_Case):
    def test_not_configured_rejected_and_down_are_unavailable_never_recorded(self) -> None:
        cases = [((503, {"ok": False, "error": "newsletter sending is not configured: "
                                                "POSTAL_ADDRESS is not set"}), "POSTAL_ADDRESS"),
                 ((401, {"ok": False, "error": "token required"}), "token was rejected"),
                 ((403, {"ok": False, "error": "publish token: not allowed here"}),
                  "token was rejected"),
                 ((500, {"ok": False, "error": "internal error"}), "HTTP 500")]
        for forced, said in cases:
            with self.subTest(forced[0]):
                self.scrooge.forced = forced
                out = self.execute()
                self.assertIs(out["ok"], False)
                self.assertIn(said, out["unavailable"])
                # only Scrooge's 503 is the typed not-configured state
                self.assertIs(out.get("not_configured", False), forced[0] == 503)
        self.scrooge.forced = None
        self.scrooge.down = True
        self.assertIn("unreachable", self.execute()["unavailable"])
        self.assertFalse(self.ledger.exists())

    def test_a_200_without_its_send_is_kept_from_going_again(self) -> None:
        self.scrooge.forced = (200, {"ok": True})
        out = self.execute()
        self.assertIs(out["ok"], False)
        self.assertIn("may be going out", out["unavailable"])
        self.assertIn(NID, json.loads(self.ledger.read_text(encoding="utf-8")))


class SecrecyTests(_Case):
    def test_the_token_never_appears_in_results_errors_logs_or_records(self) -> None:
        with self.assertLogs("pionir", level=logging.DEBUG) as logs:
            logging.getLogger("pionir").info("start")
            outs = [self.execute()]
            self.scrooge.forced = (409, {"ok": False, "error": f"already queued {TOKEN}",
                                         "send_id": "nl_" + "b" * 24, "status": "queued"})
            outs.append(self.execute(letter(newsletter_id="weekly-2026-w41",
                                            body_md=make_body("weekly-2026-w41"))))
            self.scrooge.forced = None
            self.scrooge.down = True
            outs.append(self.execute(letter(newsletter_id="weekly-2026-w42",
                                            body_md=make_body("weekly-2026-w42"))))
        everything = json.dumps(outs) + "\n".join(logs.output) + self.ledger.read_text(
            encoding="utf-8")
        self.assertNotIn(TOKEN, everything)
        self.assertIn("<redacted>", json.dumps(outs[1]))


class BootstrapTests(unittest.TestCase):
    def test_it_is_registered_with_the_blog_on_the_blogs_token_without_the_network(
            self) -> None:
        for content, present in (("https://api.dokaz.net", True), (None, False)):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as root:
                runtime = build_runtime(_settings(
                    Path(root), content_url=content,
                    content_token_file=Path(root) / "no-token.txt"))
                try:
                    self.assertEqual("newsletter" in runtime.adapters, present)
                    if present:   # doctor's health check is local: no token, no call
                        adapter = runtime.adapters["newsletter"]
                        with self.assertRaisesRegex(AdapterUnavailable, "not configured"):
                            adapter.status()
                        self.assertEqual(adapter.settings.ledger_file,
                                         Path(root) / "newsletter" / "sends.json")
                        self.assertEqual(adapter.settings.content.base_url, content)
                        self.assertEqual(adapter.settings.content.token_file,
                                         Path(root) / "no-token.txt")
                finally:
                    runtime.cortex.close()
        self.assertEqual(PionirSettings(state_root=Path("C:/x")).newsletter_ledger_path,
                         Path("C:/x") / "newsletter" / "sends.json")


# ---- the card ----------------------------------------------------------------------------
class DiscordCardTests(_Case):
    def test_the_card_shows_the_subject_the_whole_body_and_the_footer(self) -> None:
        paragraphs = "\n\n".join(
            f"Paragraph {i}: a short note on what the week brought and why it helps a "
            "small business with its paperwork." for i in range(150))
        body = make_body(extra=paragraphs)
        self.assertGreater(len(body), 15_000)
        payload = letter(body_md=body)
        out = self.app.run_task(SEND, payload, permissions=[SEND])
        self.assertEqual(out["status"], "pending_approval")

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
        self.assertGreater(len(posts), 3)                   # split, not cut
        head = posts[0]
        self.assertEqual(head.split("\n")[0], NEWSLETTER_LINE)
        self.assertTrue(head.startswith(
            "\U0001f4e7 **EMAILS EVERY CONFIRMED SUBSCRIBER** - the email below goes to the "
            "whole newsletter list if you approve."))
        self.assertIn(f"**Subject:** {_escape(payload['subject'])}", head)
        self.assertIn("`content.newsletter_send`", head)
        self.assertNotIn("daily digest", head)              # its own card, answered here
        text = "\n".join(line for chunk in posts for line in chunk.split("\n")
                         if not line.startswith("```"))
        self.assertIn(body, text)                           # the whole body, verbatim
        self.assertIn("\n".join(FOOTER_LINES), text)        # and what Scrooge adds to it
        self.assertIn("Unsubscribe with one click", text)
        self.assertIn("List-Unsubscribe", text)
        self.assertEqual(self.scrooge.calls, [])            # showing it sends nothing
        self.assertEqual(len(self.app.approvals.pending()), 1)


if __name__ == "__main__":
    unittest.main()
