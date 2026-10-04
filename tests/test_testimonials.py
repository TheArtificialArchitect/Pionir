"""A testimonial goes public only with the client's consent AND the owner's yes; a feedback
request is one templated client.email Scrooge fills in.

``client.testimonial_publish`` parks on every call, is checked by the content rules before it is
parked, and runs with the PUBLISH token (never the ops token) against Scrooge's approve route with
exactly the stored words. ``client.testimonials`` reads with the ops token. ``client.email`` takes
``kind: "feedback_request"`` with its two placeholders kept for the owner's card.

The contract with Scrooge (worker/src/feedback.ts) is pinned here by its literal values, as
Scrooge's test/feedback.test.ts pins Pionir's template: change one side and a suite fails.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Self

from pionir.adapters.clients import (
    EMAIL,
    EMAIL_KINDS,
    FEEDBACK_KIND,
    FEEDBACK_LINK_PLACEHOLDER,
    REFERRAL_CODE_PLACEHOLDER,
    SAMPLE_FEEDBACK_LINK,
    SAMPLE_REFERRAL_CODE,
    ClientAdapter,
    ClientSettings,
    check_email,
)
from pionir.adapters.testimonials import (
    APPROVE_ROUTE,
    PENDING_ROUTE,
    PUBLISH_TESTIMONIAL,
    TESTIMONIALS,
    TestimonialAdapter,
    check_testimonial,
)
from pionir.contracts import RiskLevel, Task
from pionir.crew import feedback as desk
from pionir.discord_gate import render_request
from pionir.errors import AdapterProtocolError, AdapterUnavailable

OPS = "opsSECRETtoken0123456789abcdef"
PUB = "pubSECRETtoken0123456789abcdef"
TID = "tm_" + "a" * 24
OID = "a1a1a1a1a1a1"


def testimonial(**over: Any) -> dict[str, Any]:
    base = {"testimonial_id": TID, "order_id": OID, "rating": 5, "display_name": "Ann C.",
            "body": "Fast, careful, and it just works.\nWould hire again."}
    base.update(over)
    return base


class _Response:
    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self._raw = json.dumps(payload).encode()

    def read(self, _n: int = -1) -> bytes:
        return self._raw

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class FakeScrooge:
    """The testimonial routes, checking each token's scope like the real gate (dashboard.ts)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, request: Any, data: Any = None, timeout: float | None = None):
        token = request.get_header("X-dash-token")
        body = json.loads(request.data.decode()) if request.data else None
        path = request.full_url.split("api.dokaz.test", 1)[1]
        self.calls.append({"method": request.get_method(), "path": path, "token": token,
                           "body": body})
        if (request.get_method(), path) == ("GET", PENDING_ROUTE):
            if token != OPS:
                return _Response(403, {"ok": False, "error": "not allowed here"})
            return _Response(200, {"ok": True, "testimonials": [], "stats": {}})
        if (request.get_method(), path) == ("POST", APPROVE_ROUTE):
            if token != PUB:
                return _Response(403, {"ok": False, "error": "not allowed here"})
            return _Response(200, {"ok": True, "id": body["id"], "status": "approved"})
        return _Response(404, {"ok": False, "error": "no such route"})


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        (self.dir / "ops.txt").write_text(OPS, encoding="utf-8")
        (self.dir / "pub.txt").write_text(PUB, encoding="utf-8")
        self.scrooge = FakeScrooge()

    def adapter(self, publish: bool = True) -> TestimonialAdapter:
        return TestimonialAdapter(ClientSettings(
            base_url="https://api.dokaz.test", token_file=self.dir / "ops.txt",
            publish_token_file=(self.dir / "pub.txt") if publish else self.dir / "none.txt",
            secrets_dir=None, ssh_dir=None), opener=self.scrooge)


class ContractTests(unittest.TestCase):
    """Scrooge's vocabulary, by its literal values (worker/src/feedback.ts)."""

    def test_the_email_kind_and_its_placeholders(self) -> None:
        self.assertEqual(EMAIL_KINDS, ("feedback_request",))
        self.assertEqual((FEEDBACK_KIND, FEEDBACK_LINK_PLACEHOLDER, REFERRAL_CODE_PLACEHOLDER),
                         ("feedback_request", "{feedback_link}", "{referral_code}"))
        self.assertRegex(SAMPLE_FEEDBACK_LINK, r"^https://api\.dokaz\.net/feedback/[0-9a-f]{64}$")
        self.assertRegex(SAMPLE_REFERRAL_CODE, r"^[A-HJ-NP-Z2-9]{8}$")

    def test_the_crew_desk_and_the_adapter_speak_the_same_words(self) -> None:
        self.assertEqual((desk.FEEDBACK_KIND, desk.FEEDBACK_LINK, desk.REFERRAL_CODE),
                         (FEEDBACK_KIND, FEEDBACK_LINK_PLACEHOLDER, REFERRAL_CODE_PLACEHOLDER))
        self.assertEqual((desk.EMAIL, desk.TESTIMONIALS, desk.PUBLISH),
                         (EMAIL, TESTIMONIALS, PUBLISH_TESTIMONIAL))
        self.assertEqual(desk.REFERRAL_CREDIT_CENTS, 2500)
        self.assertEqual(desk.FEEDBACK_AFTER_SECONDS, 3 * 86400)

    def test_the_routes(self) -> None:
        self.assertEqual((PENDING_ROUTE, APPROVE_ROUTE),
                         ("/dash/testimonials/pending", "/dash/testimonials/approve"))

    def test_the_feedback_template_is_word_for_word_the_one_scrooge_accepts(self) -> None:
        # The same text is in Scrooge's test/feedback.test.ts (pionirFeedback), sent to the real
        # route there: if either copy changes, one of the two suites fails.
        email = desk.build_feedback_email({"id": OID, "name": "Ann Client",
                                           "email": "Ann@Example.com"})
        self.assertEqual(email["subject"], f"How did your Dokaz order {OID} go?")
        self.assertEqual(email["body_text"], f"""Hello Ann Client,

It's been a few days since we delivered your order {OID}. We hope it's doing its job.

Would you tell us how it went? It takes two minutes:
{{feedback_link}}

That link is private to you and works for 60 days. If you're happy for us to, you can let us show your words on our website - only if you tick the box, and only after we've read them.

Your referral code is {{referral_code}}. If someone you know needs a script, a data clean-up or a small tool, send them to https://api.dokaz.net/hire?ref={{referral_code}} - when they order, you get $25 off your next order.

Thank you,
Dokaz""")
        self.assertEqual(email["kind"], "feedback_request")


class FeedbackKindEmailTests(unittest.TestCase):
    def setUp(self) -> None:
        self.email = desk.build_feedback_email({"id": OID, "name": "Ann Client",
                                                "email": "ann@example.com"})

    def test_a_feedback_request_passes_with_its_placeholders_kept(self) -> None:
        out = check_email(self.email)
        self.assertEqual(out["kind"], "feedback_request")
        self.assertEqual(out["body_text"], self.email["body_text"])
        self.assertIn("{feedback_link}", out["body_text"])

    def test_the_placeholders_are_checked(self) -> None:
        body = self.email["body_text"]
        for bad in (body.replace("{feedback_link}", ""), body + "\n{feedback_link}",
                    body.replace("{referral_code}", "X")):
            with self.assertRaises(ValueError):
                check_email({**self.email, "body_text": bad})
        with self.assertRaisesRegex(ValueError, r"kind: absent \(a plain email\), 'recovery' or 'feedback_request'"):
            check_email({**self.email, "kind": "newsletter"})
        with self.assertRaisesRegex(ValueError, "subject"):
            check_email({**self.email, "subject": "Hi {referral_code}"})

    def test_a_plain_email_cannot_carry_the_placeholders(self) -> None:
        plain = {k: v for k, v in self.email.items() if k != "kind"}
        with self.assertRaisesRegex(ValueError, "only for kind 'feedback_request'"):
            check_email(plain)

    def test_the_kind_reaches_scrooge_and_the_email_still_parks(self) -> None:
        adapter = ClientAdapter(ClientSettings(base_url="https://api.dokaz.test",
                                               secrets_dir=None, ssh_dir=None))
        method, path, body = adapter._request(Task(EMAIL, self.email))
        self.assertEqual((method, path), ("POST", "/dash/orders/email"))
        self.assertEqual(body["kind"], "feedback_request")
        cap = next(c for c in adapter.manifest.capabilities if c.name == EMAIL)
        self.assertTrue(cap.requires_approval)

    def test_the_card_says_what_scrooge_fills_in(self) -> None:
        text = render_request({"id": "ap1", "capability": EMAIL, "payload": self.email}, "1")
        self.assertIn("ONE feedback request", text)
        self.assertIn("{feedback_link}", text)
        self.assertIn("ann@example.com", text)


class CheckTestimonialTests(unittest.TestCase):
    def test_a_plain_testimonial_passes_unchanged(self) -> None:
        self.assertEqual(check_testimonial(testimonial()), testimonial())

    def test_untrusted_text_is_refused(self) -> None:
        for field, value in (
                ("body", "Great! See https://evil.example/x for more."),
                ("body", "Great, visit shop.example.com today."),
                ("body", "Email me at ann@example.com any time."),
                ("body", "Call me on +1 555 123 4567 any time."),
                ("body", "Nice <b>bold</b> work there."),
                ("body", "Nice work ‮ reversed."),
                ("body", "short"),
                ("body", "x" * 601),
                ("display_name", "ann@example.com"),
                ("display_name", "Ann\nC."),
                ("display_name", ""),
                ("display_name", "A" * 41),
                ("display_name", "www.ann.com"),
                ("rating", 0), ("rating", 6), ("rating", True), ("rating", "5"),
                ("testimonial_id", "tm_1"), ("order_id", "nope")):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                check_testimonial(testimonial(**{field: value}))
        with self.assertRaises(ValueError):
            check_testimonial({**testimonial(), "email": "x@y.co"})


class TestimonialAdapterTests(_Case):
    def test_the_manifest(self) -> None:
        caps = {c.name: c for c in self.adapter().manifest.capabilities}
        self.assertEqual(set(caps), {TESTIMONIALS, PUBLISH_TESTIMONIAL})
        self.assertEqual(caps[TESTIMONIALS].risk, RiskLevel.READ_ONLY)
        self.assertFalse(caps[TESTIMONIALS].requires_approval)
        pub = caps[PUBLISH_TESTIMONIAL]
        self.assertEqual(pub.risk, RiskLevel.PRIVILEGED)
        self.assertTrue(pub.requires_approval)
        self.assertFalse(pub.batchable)
        self.assertFalse(pub.routable or caps[TESTIMONIALS].routable)

    def test_publish_sends_the_exact_words_with_the_publish_token_never_the_ops_token(
            self) -> None:
        out = self.adapter().execute(Task(PUBLISH_TESTIMONIAL, testimonial()))
        self.assertTrue(out.output["ok"])
        (call,) = self.scrooge.calls
        self.assertEqual((call["method"], call["path"], call["token"]),
                         ("POST", APPROVE_ROUTE, PUB))
        self.assertEqual(call["body"], {"id": TID, "rating": 5, "display_name": "Ann C.",
                                        "body": "Fast, careful, and it just works.\n"
                                                "Would hire again."})
        self.assertNotIn(PUB, json.dumps(dict(out.output)))

    def test_the_pending_list_is_read_with_the_ops_token(self) -> None:
        out = self.adapter().execute(Task(TESTIMONIALS, {}))
        self.assertTrue(out.output["ok"])
        self.assertEqual([(c["method"], c["path"], c["token"]) for c in self.scrooge.calls],
                         [("GET", PENDING_ROUTE, OPS)])

    def test_a_bad_testimonial_is_refused_before_it_is_parked(self) -> None:
        with self.assertRaises(AdapterProtocolError):
            self.adapter().validate(Task(PUBLISH_TESTIMONIAL,
                                         testimonial(body="Visit https://evil.example now")))
        self.assertEqual(self.scrooge.calls, [])

    def test_without_the_publish_token_nothing_is_parked_or_sent(self) -> None:
        adapter = self.adapter(publish=False)
        with self.assertRaises(AdapterUnavailable):
            adapter.validate(Task(PUBLISH_TESTIMONIAL, testimonial()))
        out = adapter.execute(Task(PUBLISH_TESTIMONIAL, testimonial()))
        self.assertFalse(out.output["ok"])
        self.assertEqual(self.scrooge.calls, [])

    def test_the_card_shows_the_exact_name_rating_and_words(self) -> None:
        text = render_request({"id": "ap2", "capability": PUBLISH_TESTIMONIAL,
                               "payload": testimonial()}, "1")
        self.assertIn("PUBLISHES A CLIENT TESTIMONIAL", text)
        self.assertIn("https://api.dokaz.net/hire", text)
        self.assertIn("`Ann C.`", text)
        self.assertIn("★" * 5, text)
        self.assertIn("Fast, careful, and it just works.\nWould hire again.", text)


if __name__ == "__main__":
    unittest.main()
