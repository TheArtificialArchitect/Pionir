"""Nothing goes public without the owner's yes.

Ian's rule, the same shape as the money rule: every post goes live only after he
approves it, and holding a permission must not be a way around that. These tests pin
the general mechanism (``Capability.requires_approval``), the content adapter that
publishes to Scrooge through it, and the Discord card that shows him the whole post.
Scrooge is faked at the HTTP opener, so the adapter's real request building, status
mapping and token handling are what is under test; nothing touches the network.
"""

from __future__ import annotations

import io
import json
import logging
import tempfile
import unittest
import urllib.error
from pathlib import Path
from typing import Any, ClassVar, Self

from test_discord_gate import API, CHANNEL, OWNER, FakeDiscord
from test_discord_gate import TOKEN as DISCORD_TOKEN

from pionir.adapters.content import (
    PUBLIC_BLOG_BASE,
    ContentAdapter,
    ContentSettings,
    check_draft,
)
from pionir.bootstrap import build_runtime
from pionir.config import PionirSettings
from pionir.contracts import AgentManifest, Capability, RiskLevel, Task, TaskResult
from pionir.discord_gate import DiscordGate, DiscordGateSettings, _escape
from pionir.errors import AdapterProtocolError, AdapterUnavailable
from pionir.router import IntentRouter
from pionir.server import PionirApp

SECRET = "scrooge-SECRET-publish-token-7f3a9c"
BODY = "\n".join(
    [
        "# Why a bounded digest beats a firehose",
        "",
        ("Read the [whole design note](https://api.dokaz.net/blog/crew-divisions) first; "
         "the kit is on [Gumroad](https://dokaz.gumroad.com/l/pionir) and the company is at "
         "[Dokaz Industries](https://www.dokazindustries.com)."),
    ]
    + [f"Paragraph {i}: the crew keeps one bounded digest per division, newest word "
       f"first, so the voice reads what matters and nothing else." for i in range(6)]
)


def draft(**overrides: Any) -> dict[str, Any]:
    base = {
        "draft_id": "d-2026-09-25-digest",
        "slug": "bounded-digest",
        "title": "Why a bounded digest beats a firehose",
        "description": "How the crew's per-division digest keeps the voice focused on "
                       "what matters, and why it is bounded.",
        "body_md": BODY,
        "tags": ["crew", "design"],
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


class _Response:
    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self, _n: int = -1) -> bytes:
        return self._raw

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class FakeScrooge:
    """Scrooge's two content routes, in memory. Checks the token like the real one."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.answer: tuple[int, Any] | None = None   # force one status/body
        self.down = False

    def __call__(self, request: Any, timeout: float) -> _Response:
        body = json.loads(request.data)
        self.calls.append({"url": request.full_url, "method": request.get_method(),
                           "token": request.get_header("X-dash-token"), "body": body,
                           "timeout": timeout})
        if self.down:
            raise urllib.error.URLError(f"connection refused ({SECRET})")
        status, payload = self.answer or (200, None)
        if request.get_header("X-dash-token") != SECRET:
            status, payload = 401, {"ok": False, "error": "unauthorized"}
        if status != 200:
            raise urllib.error.HTTPError(request.full_url, status, "error", {},  # type: ignore[arg-type]
                                         io.BytesIO(json.dumps(payload).encode("utf-8")))
        if payload is None:
            if request.full_url.endswith("/publish"):
                payload = {"ok": True, "slug": body["slug"],
                           "url": PUBLIC_BLOG_BASE + body["slug"],
                           "published_at": "2026-09-25T12:00:00Z", "created": True}
            else:
                payload = {"ok": True, "slug": body["slug"], "removed": True}
        return _Response(200, payload)


def _settings(root: Path, **kw: Any) -> PionirSettings:
    return PionirSettings(
        state_root=root,
        atani_command=("pionir-test-no-such-binary",),
        daedalus_url=None, melete_url=None, galatea_url=None, crew_url=None,
        bryo_status_command=None, nyx_status_command=None, voodoo_status_command=None,
        embed_model=None, evict_to_fit=False, **kw,
    )


class _Case(unittest.TestCase):
    """A hermetic runtime with the content adapter talking to FakeScrooge."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.token_file = self.root / "secrets" / "scrooge-publish-token.txt"
        self.token_file.parent.mkdir(parents=True)
        self.token_file.write_text(SECRET + "\n", encoding="utf-8")
        self.scrooge = FakeScrooge()
        self.adapter = ContentAdapter(
            ContentSettings(base_url="https://api.dokaz.test", token_file=self.token_file),
            opener=self.scrooge,
        )
        # content_url=None: the one registered here is the faked one
        runtime = build_runtime(_settings(self.root, content_url=None))
        runtime.register(self.adapter)
        self.app = PionirApp(runtime)

    def tearDown(self) -> None:
        self.app.runtime.cortex.close()
        self._tmp.cleanup()

    def run_publish(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Park, approve, wait: the only way a post goes out."""
        out = self.app.run_task("content.publish", payload or draft(),
                                permissions=["content.publish"])
        self.assertEqual(out["status"], "pending_approval")
        res = self.app.approve(out["approval_id"])
        self.assertTrue(self.app.jobs.wait(res["task_id"], 30))
        return self.app.approvals.get(out["approval_id"])


# ---- the general rule ------------------------------------------------------------
class _Poster:
    """Any capability that needs the owner's yes every time. Counts what actually ran."""

    def __init__(self, *, routable: bool = False) -> None:
        self.ran = 0
        self._manifest = AgentManifest(
            agent_id="poster", version="test",
            capabilities=(Capability(
                name="poster.announce", description="announce launch publicly to everyone",
                risk=RiskLevel.PRIVILEGED, required_permissions=frozenset({"poster.announce"}),
                requires_approval=True, routable=routable,
                routing_hints=frozenset({"announce", "launch", "publicly"}),
            ),),
        )

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def execute(self, task: Task) -> TaskResult:
        self.ran += 1
        return TaskResult(task_id=task.task_id, agent_id="poster", output={"ok": True})


class RequiresApprovalDefinitionTests(unittest.TestCase):
    def test_it_must_be_privileged(self) -> None:
        for risk in (RiskLevel.READ_ONLY, RiskLevel.REVERSIBLE_WRITE):
            with self.assertRaises(ValueError, msg=risk):
                Capability(name="x.post", description="d", risk=risk, requires_approval=True)
        Capability(name="x.post", description="d", risk=RiskLevel.PRIVILEGED,
                   requires_approval=True)

    def test_spending_money_implies_it(self) -> None:
        cap = Capability(name="x.buy", description="d", risk=RiskLevel.PRIVILEGED,
                         spends_money=True)
        self.assertTrue(cap.requires_approval)
        self.assertFalse(Capability(name="x.read", description="d").requires_approval)

    def test_the_router_never_classifies_to_it_even_when_routable(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            runtime = build_runtime(_settings(Path(root), content_url=None))
            try:
                runtime.register(_Poster(routable=True))
                decision = IntentRouter(runtime.executive).classify(
                    "announce the launch publicly")
                self.assertNotEqual(decision.capability, "poster.announce")
            finally:
                runtime.cortex.close()


class RequiresApprovalGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        runtime = build_runtime(_settings(Path(self._tmp.name), content_url=None))
        self.poster = _Poster()
        runtime.register(self.poster)
        self.app = PionirApp(runtime)

    def tearDown(self) -> None:
        self.app.runtime.cortex.close()
        self._tmp.cleanup()

    def test_it_parks_even_when_the_permission_is_held(self) -> None:
        out = self.app.run_task("poster.announce", {"content": "hi"},
                                permissions=["poster.announce"])
        self.assertEqual(out["status"], "pending_approval")
        self.assertEqual(self.poster.ran, 0)

    def test_it_runs_exactly_once_after_approval(self) -> None:
        aid = self.app.run_task("poster.announce", {"content": "hi"},
                                permissions=["poster.announce"])["approval_id"]
        res = self.app.approve(aid)
        self.assertTrue(self.app.jobs.wait(res["task_id"], 30))
        self.assertEqual(self.poster.ran, 1)
        self.assertFalse(self.app.approve(aid)["ok"])
        self.assertEqual(self.poster.ran, 1)

    def test_a_denial_never_runs_it(self) -> None:
        aid = self.app.run_task("poster.announce", {"content": "hi"},
                                permissions=["poster.announce"])["approval_id"]
        self.assertTrue(self.app.deny(aid)["ok"])
        self.assertEqual(self.poster.ran, 0)


# ---- the content adapter ---------------------------------------------------------
class PublishGateTests(_Case):
    def test_publish_is_declared_as_always_approved_and_unpublish_is_not(self) -> None:
        caps = {c.name: c for c in self.adapter.manifest.capabilities}
        self.assertTrue(caps["content.publish"].requires_approval)
        self.assertIs(caps["content.publish"].risk, RiskLevel.PRIVILEGED)
        self.assertFalse(caps["content.publish"].routable)
        self.assertFalse(caps["content.unpublish"].requires_approval)
        self.assertIs(caps["content.unpublish"].risk, RiskLevel.PRIVILEGED)
        self.assertFalse(caps["content.unpublish"].routable)

    def test_publish_parks_every_time_even_with_the_permission(self) -> None:
        for _ in range(3):
            out = self.app.run_task("content.publish", draft(),
                                    permissions=["content.publish"])
            self.assertEqual(out["status"], "pending_approval")
        self.assertEqual(len(self.app.approvals.pending()), 3)
        self.assertEqual(self.scrooge.calls, [])

    def test_an_approved_publish_is_sent_once_exactly_as_drafted(self) -> None:
        row = self.run_publish()
        self.assertEqual(row["status"], "approved")
        self.assertEqual(len(self.scrooge.calls), 1)
        call = self.scrooge.calls[0]
        self.assertEqual(call["url"], "https://api.dokaz.test/dash/content/publish")
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["token"], SECRET)
        self.assertEqual(call["body"], draft())
        result = row["result"]["result"]
        self.assertEqual(result["url"], PUBLIC_BLOG_BASE + "bounded-digest")
        self.assertIs(result["created"], True)

    def test_a_denied_publish_is_never_sent(self) -> None:
        aid = self.app.run_task("content.publish", draft(),
                                permissions=["content.publish"])["approval_id"]
        self.app.deny(aid)
        self.assertEqual(self.scrooge.calls, [])

    def test_unpublish_runs_at_once_with_its_permission(self) -> None:
        out = self.app.run_task("content.unpublish", {"slug": "bounded-digest"},
                                permissions=["content.unpublish"], wait=30)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["result"], {"ok": True, "slug": "bounded-digest",
                                         "removed": True})
        self.assertEqual(self.scrooge.calls[0]["url"],
                         "https://api.dokaz.test/dash/content/unpublish")
        self.assertEqual(self.scrooge.calls[0]["body"], {"slug": "bounded-digest"})


class LocalValidationTests(_Case):
    BAD: ClassVar[dict[str, dict[str, Any]]] = {
        "raw html tag": {"body_md": BODY + "\n<script>alert(1)</script>"},
        "inline html": {"body_md": BODY + "\nSome <b>bold</b> words."},
        "html comment": {"body_md": BODY + "\n<!-- hidden -->"},
        "autolink": {"body_md": BODY + "\nSee <https://api.dokaz.net/blog>."},
        "javascript": {"body_md": BODY + "\n[click](javascript:alert(1))"},
        "data uri": {"body_md": BODY + "\n![x](data:image/png;base64,AAAA)"},
        "vbscript": {"body_md": BODY + "\n[x](VBScript:msgbox)"},
        "http link": {"body_md": BODY + "\n[x](http://api.dokaz.net/blog)"},
        "foreign host": {"body_md": BODY + "\n[x](https://evil.example.com/a)"},
        "lookalike host": {"body_md": BODY + "\n[x](https://api.dokaz.net.evil.com/a)"},
        "userinfo trick": {"body_md": BODY + "\n[x](https://api.dokaz.net@evil.com/a)"},
        "bare url": {"body_md": BODY + "\nGo to https://evil.example.com now."},
        "relative link": {"body_md": BODY + "\n[x](/blog/other)"},
        "reference link": {"body_md": BODY + "\n\n[ref]: https://evil.example.com"},
        "bare www": {"body_md": BODY + "\nor www.example.com today"},
        "mailto": {"body_md": BODY + "\n[mail](mailto:someone@example.com)"},
        "email": {"body_md": BODY + "\nWrite to ian@example.com with questions."},
        "phone": {"body_md": BODY + "\nCall (555) 123-4567 any time."},
        "intl phone": {"body_md": BODY + "\nCall +44 20 7946 0958 any time."},
        "ipv4": {"body_md": BODY + "\nThe box is at 192.168.1.20 on the LAN."},
        "ipv6": {"body_md": BODY + "\nThe box is at fe80::1ff:fe23:4567:890a here."},
        "email in title": {"title": "Mail ian@example.com today"},
        "phone in description": {"description": "Call 555-123-4567 to hear about "
                                                "the bounded digest design and more."},
        "html in title": {"title": "A <i>bounded</i> digest"},
        "short title": {"title": "Too short"},
        "long title": {"title": "x" * 121},
        "short description": {"description": "Too short a description."},
        "long description": {"description": "d" * 301},
        "short body": {"body_md": "Just a line."},
        "long body": {"body_md": "a" * 30_001},
        "bad slug": {"slug": "-bounded"},
        "uppercase slug": {"slug": "Bounded-Digest"},
        "short slug": {"slug": "ab"},
        "bad draft id": {"draft_id": "Draft_1"},
        "missing title": {"title": None},
        "too many tags": {"tags": [f"t{i}" for i in range(9)]},
        "bad tag": {"tags": ["Not Ok"]},
        "long tag": {"tags": ["t" * 25]},
        "unknown field": {"author": "moss"},
    }
    # Each case must be refused for ITS rule, not caught by some other one by accident.
    WHY: ClassVar[dict[str, str]] = {
        "raw html tag": "body_md: raw HTML", "inline html": "body_md: raw HTML",
        "html comment": "HTML comments", "autolink": "raw HTML",
        "javascript": "javascript:", "data uri": "javascript:, data:", "vbscript": "vbscript:",
        "http link": "not http:", "foreign host": "not 'evil.example.com'",
        "lookalike host": "not 'api.dokaz.net.evil.com'", "userinfo trick": "credentials",
        "bare url": "not 'evil.example.com'", "relative link": "not a relative link",
        "reference link": "not 'evil.example.com'", "bare www": "bare address",
        "mailto": "not mailto:", "email": "body_md: no email", "phone": "body_md: no phone",
        "intl phone": "body_md: no phone", "ipv4": "body_md: no IP",
        "ipv6": "body_md: no IP", "email in title": "title: no email",
        "phone in description": "description: no phone", "html in title": "title: raw HTML",
        "short title": "title: 10-120", "long title": "title: 10-120",
        "short description": "description: 50-300", "long description": "description: 50-300",
        "short body": "body_md: 300-30000", "long body": "body_md: 300-30000",
        "bad slug": "slug: 3-80", "uppercase slug": "slug: 3-80", "short slug": "slug: 3-80",
        "bad draft id": "draft_id: 1-64", "missing title": "title: required",
        "too many tags": "tags: a list of at most 8", "bad tag": "tags: each",
        "long tag": "tags: each", "unknown field": "author: not a publish field",
    }

    def test_every_rule_is_enforced_before_anything_is_sent_or_parked(self) -> None:
        self.assertEqual(check_draft(draft()), draft())       # the good draft passes
        self.assertEqual(set(self.WHY), set(self.BAD))
        for label, change in self.BAD.items():
            with self.subTest(label):
                payload = draft(**change)
                with self.assertRaises(AdapterProtocolError) as refused:
                    self.adapter.validate(Task("content.publish", payload))
                self.assertIn(self.WHY[label], str(refused.exception))
                with self.assertRaises(AdapterProtocolError):
                    self.adapter.execute(Task("content.publish", payload))
                out = self.app.run_task("content.publish", payload,
                                        permissions=["content.publish"])
                self.assertEqual(out["status"], "error", out)
                self.assertEqual(out["error"]["type"], "AdapterProtocolError")
        self.assertEqual(self.app.approvals.pending(), [])
        self.assertEqual(self.scrooge.calls, [])

    def test_the_refusal_names_the_field(self) -> None:
        with self.assertRaisesRegex(AdapterProtocolError, r"body_md: no email addresses"):
            self.adapter.validate(Task("content.publish",
                                       draft(body_md=BODY + "\nian@example.com")))

    def test_ordinary_prose_is_not_mistaken_for_contact_details(self) -> None:
        body = BODY + ("\nOn 2026-09-25 at 12:30:45 we shipped v1.2.3; it took 2019-2021 "
                       "to get here, `std::vector` and all, for 30000 users.")
        check_draft(draft(body_md=body))

    def test_unpublish_checks_its_slug(self) -> None:
        for payload in ({}, {"slug": "-x-"}, {"slug": "ok-slug", "extra": 1}):
            with self.subTest(payload), self.assertRaises(AdapterProtocolError):
                self.adapter.validate(Task("content.unpublish", payload))
        self.assertEqual(self.scrooge.calls, [])


class ResponseMappingTests(_Case):
    def test_409_is_a_refusal_with_scrooges_reason(self) -> None:
        self.scrooge.answer = (409, {"ok": False, "error": "slug taken"})
        out = self.adapter.execute(Task("content.publish", draft()))
        self.assertEqual(out.output["refused"], "slug taken")
        self.assertIs(out.output["ok"], False)

    def test_409_through_the_gate_settles_as_a_refusal(self) -> None:
        self.scrooge.answer = (409, {"ok": False, "error": "slug taken"})
        row = self.run_publish()
        self.assertEqual(row["status"], "approved_failed")
        self.assertEqual(row["result"]["result"]["refused"], "slug taken")

    def test_400_is_a_refusal_with_the_field_and_why(self) -> None:
        self.scrooge.answer = (400, {"ok": False, "error": "title: too long"})
        out = self.adapter.execute(Task("content.publish", draft()))
        self.assertEqual(out.output["refused"], "title: too long")

    def test_a_rejected_token_is_unavailable(self) -> None:
        self.token_file.write_text("wrong-token", encoding="utf-8")
        out = self.adapter.execute(Task("content.publish", draft()))
        self.assertEqual(out.output["unavailable"], "the publish token was rejected")
        self.assertNotIn("refused", out.output)

    def test_scrooge_down_is_unavailable(self) -> None:
        self.scrooge.down = True
        out = self.adapter.execute(Task("content.publish", draft()))
        self.assertIn("unreachable", out.output["unavailable"])


class TokenTests(_Case):
    def test_a_missing_token_is_a_clear_unavailable_never_a_crash(self) -> None:
        self.token_file.unlink()
        out = self.adapter.execute(Task("content.publish", draft()))
        self.assertIs(out.output["ok"], False)
        self.assertTrue(out.output["not_configured"])
        self.assertIn("not configured", out.output["unavailable"])
        self.assertIn(str(self.token_file), out.output["unavailable"])
        with self.assertRaisesRegex(AdapterUnavailable, "not configured"):
            self.adapter.status()
        # and it is not parked: approving a post that cannot be sent wastes the yes
        parked = self.app.run_task("content.publish", draft(),
                                   permissions=["content.publish"])
        self.assertEqual(parked["status"], "error")
        self.assertEqual(parked["error"]["type"], "AdapterUnavailable")
        self.assertIn("not configured", parked["error"]["message"])
        self.assertEqual(self.scrooge.calls, [])

    def test_the_token_never_appears_in_results_errors_logs_or_records(self) -> None:
        capture = io.StringIO()
        handler = logging.StreamHandler(capture)
        root = logging.getLogger()
        old = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(handler)
        try:
            outputs: list[Any] = []
            # Scrooge echoing the token back in its error text
            self.scrooge.answer = (400, {"ok": False, "error": f"bad token {SECRET}"})
            outputs.append(self.adapter.execute(Task("content.publish", draft())).output)
            self.scrooge.answer = None
            self.scrooge.down = True                       # the transport error names it too
            outputs.append(self.adapter.execute(Task("content.publish", draft())).output)
            self.scrooge.down = False
            outputs.append(self.adapter.execute(Task("content.publish", draft())).output)
            outputs.append(self.adapter.status())
            outputs.append(repr(self.adapter.settings))
            row = self.run_publish()                       # through the gate, audited
            outputs.append(row)
            self.token_file.write_text("nope", encoding="utf-8")
            outputs.append(self.run_publish())             # a rejected token
        finally:
            root.removeHandler(handler)
            root.setLevel(old)
        # the token really was used, so its absence below means something
        self.assertTrue(all(call["token"] in (SECRET, "nope") for call in self.scrooge.calls))
        self.assertIn(SECRET, [call["token"] for call in self.scrooge.calls])
        self.assertIn("<redacted>", outputs[0]["refused"])
        dumped = json.dumps(outputs, default=str)
        self.assertNotIn(SECRET, dumped)
        self.assertNotIn(SECRET, capture.getvalue())
        for path in self.root.rglob("*"):   # the audit ledger, jobs, approvals, memory
            if path.is_file() and path != self.token_file:
                self.assertNotIn(SECRET.encode("utf-8"), path.read_bytes(), path)


class SettingsTests(unittest.TestCase):
    def test_defaults_env_and_off(self) -> None:
        import os
        from unittest import mock

        settings = PionirSettings(state_root=Path("C:/x"))
        self.assertEqual(settings.content_url, "https://api.dokaz.net")
        self.assertEqual(settings.content_token_path,
                         Path.home() / ".pionir" / "secrets" / "scrooge-publish-token.txt")
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            os.environ, {"PIONIR_STATE_ROOT": root}
        ):
            os.environ.pop("PIONIR_CONTENT_URL", None)
            self.assertEqual(PionirSettings.from_environment().content_url,
                             "https://api.dokaz.net")
            os.environ["PIONIR_CONTENT_URL"] = "off"
            self.assertIsNone(PionirSettings.from_environment().content_url)
            os.environ["PIONIR_CONTENT_TOKEN_FILE"] = str(Path(root) / "t.txt")
            self.assertEqual(PionirSettings.from_environment().content_token_path,
                             Path(root) / "t.txt")

    def test_bootstrap_registers_it_without_touching_the_network(self) -> None:
        for url, present in (("https://api.dokaz.net", True), (None, False)):
            with tempfile.TemporaryDirectory() as root:
                runtime = build_runtime(_settings(
                    Path(root), content_url=url,
                    content_token_file=Path(root) / "no-token.txt"))
                try:
                    self.assertEqual("content" in runtime.adapters, present)
                    if present:   # doctor's health check is local: no token, no call
                        with self.assertRaisesRegex(AdapterUnavailable, "not configured"):
                            runtime.adapters["content"].status()
                finally:
                    runtime.cortex.close()

    def test_the_token_never_leaves_over_plain_http_to_a_remote_host(self) -> None:
        with self.assertRaises(ValueError):
            ContentSettings(base_url="http://api.dokaz.net")
        ContentSettings(base_url="http://127.0.0.1:8787")


# ---- the Discord card ------------------------------------------------------------
class DiscordCardTests(_Case):
    def test_the_card_shows_the_title_the_url_and_the_whole_post(self) -> None:
        paragraphs = [f"Paragraph {i}: the crew keeps one bounded digest per division, "
                      f"newest word first, so the voice reads what matters." for i in range(260)]
        body = "\n".join(["# A long post", "",
                          "See [the note](https://api.dokaz.net/blog/crew-divisions).",
                          *paragraphs])
        self.assertGreater(len(body), 20_000)
        payload = draft(body_md=body)
        out = self.app.run_task("content.publish", payload, permissions=["content.publish"])
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
        self.assertGreater(len(posts), 5)                   # split, not cut
        head = posts[0]
        self.assertTrue(head.split("\n")[0].startswith("\U0001f4dd **PUBLISHES PUBLICLY**"))
        self.assertIn(_escape(payload["title"]), head)
        self.assertIn(_escape(payload["description"]), head)
        self.assertIn("https://api.dokaz.net/blog/bounded-digest", head)
        self.assertIn("`content.publish`", head)
        # the whole body, verbatim, across the messages
        text = "\n".join(line for chunk in posts for line in chunk.split("\n")
                         if not line.startswith("```"))
        self.assertIn(body, text)
        self.assertEqual(self.scrooge.calls, [])            # showing it sends nothing


if __name__ == "__main__":
    unittest.main()
