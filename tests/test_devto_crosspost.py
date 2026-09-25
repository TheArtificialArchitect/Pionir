"""Nothing goes up on dev.to without the owner's yes, and only a post already live on the blog.

The same rule as the blog and Instagram: ``content.crosspost_devto`` parks on every call,
runs once after an approval and never after a denial. These tests pin the adapter (the check
before parking, the live check on the blog, the ledger that keeps a draft from going up
twice, the canonical URL built from the slug, the error mapping and the key never leaking),
the Discord card that shows the whole article, the ``devto-check`` command, and - the lesson
``content.publish`` paid for - the real urllib opener against real loopback HTTP servers for
the blog and dev.to.

The blog and dev.to are faked at the HTTP opener with the real opener's signature
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
from pionir.adapters import devto
from pionir.adapters.devto import (
    CROSSPOST,
    FOREM_ACCEPT,
    KEY_REJECTED,
    NOT_LIVE,
    DevtoAdapter,
    DevtoSettings,
    attribution_line,
    check_crosspost,
    utm_campaign,
)
from pionir.bootstrap import build_runtime
from pionir.config import PionirSettings
from pionir.contracts import RiskLevel, Task
from pionir.crew import contentcheck
from pionir.discord_gate import DEVTO_LINE, DiscordGate, DiscordGateSettings, _escape
from pionir.errors import AdapterProtocolError, AdapterUnavailable
from pionir.server import PionirApp

KEY = "devtoSECRETapiKEY0123456789abcdefXYZ"
BLOG = "https://api.dokaz.test"
DEVTO = "https://devto.test/api"
DRAFT_ID = "2026-09-25-verify-email"
SLUG = "verify-email"
UTM = f"utm_source=devto&utm_medium=referral&utm_campaign={DRAFT_ID}"


def make_body(*, slug: str = SLUG, draft_id: str = DRAFT_ID, extra: str = "",
              ending: str | None = None) -> str:
    lines = [
        "# Check an email address before you hit send",
        "",
        ("Try the [email check](https://api.dokaz.net/email?" + UTM + ") on a real "
         "address first; the kit is on [Gumroad](https://dokaz.gumroad.com/l/kit?" + UTM
         + ")."),
        "",
        *[f"Paragraph {i}: a bounced message chips away at how inboxes treat your mail, "
          "so checking the address first is cheap and keeps the sender reputation clean."
          for i in range(4)],
    ]
    if extra:
        lines += ["", extra]
    lines += ["", ending if ending is not None else attribution_line(slug, draft_id)]
    return "\n".join(lines) + "\n"


def article(**over: Any) -> dict[str, Any]:
    base = {
        "draft_id": DRAFT_ID,
        "slug": SLUG,
        "title": "Check an email address before you hit send",
        "description": "Why a quick check of syntax, domain and mail server before sending "
                       "keeps your sender reputation clean.",
        "body_md": make_body(),
        "tags": ["email", "webdev", "api"],
    }
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


class FakeWorld:
    """The blog's public pages and dev.to's articles API, in memory. dev.to checks the key
    like the real one. Same call signature as OpenerDirector.open."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.live: set[str] = {SLUG}
        self.valid_keys = {KEY}
        self.forced: dict[str, tuple[int, Any]] = {}   # step -> (status, body)
        self.down: set[str] = set()                    # hosts that do not answer
        self.next_id = 1000
        self._lock = threading.Lock()

    def steps(self) -> list[str]:
        return [c["step"] for c in self.calls]

    def articles(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["step"] == "article"]

    def __call__(self, request: Any, data: Any = None, timeout: float | None = None) -> _Response:
        # The real opener is OpenerDirector.open(url, data=None, timeout=...): a timeout
        # passed positionally lands in `data`. This fake fails the same way.
        if data is not None:
            raise TypeError(f"opener got a positional data argument: {data!r}")
        with self._lock:
            return self._handle(request, timeout)

    def _error(self, url: str, status: int, payload: Any) -> None:
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        raise urllib.error.HTTPError(url, status, "error", {},  # type: ignore[arg-type]
                                     io.BytesIO(raw))

    def _handle(self, request: Any, timeout: float | None) -> _Response:
        url = request.full_url
        parts = urllib.parse.urlsplit(url)
        method = request.get_method()
        if url.startswith(BLOG + "/blog/"):
            step = "live"
        elif url == f"{DEVTO}/articles":
            step = "article"
        elif url == f"{DEVTO}/users/me":
            step = "me"
        else:
            step = "unknown"
        body = json.loads(request.data) if request.data else None
        self.calls.append({
            "step": step, "method": method, "url": url, "body": body, "timeout": timeout,
            "key": request.get_header("Api-key"), "accept": request.get_header("Accept"),
            "content_type": request.get_header("Content-type"),
            "agent": request.get_header("User-agent"),
        })
        if parts.netloc in self.down:
            raise urllib.error.URLError(f"connection refused: {url} key={KEY}")
        if step in self.forced:
            status, payload = self.forced[step]
            if status >= 400:
                self._error(url, status, payload)
            return _Response(status, payload)
        if step == "live":
            if parts.path.removeprefix("/blog/") in self.live:
                return _Response(200, b"<!doctype html><title>post</title>")
            self._error(url, 404, b"<!doctype html><title>not found</title>")
        if step in ("article", "me") and request.get_header("Api-key") not in self.valid_keys:
            self._error(url, 401, {"error": "unauthorized", "status": 401})
        if step == "article":
            self.next_id += 1
            slug = f"check-an-email-{self.next_id}"
            return _Response(201, {"id": self.next_id, "slug": slug,
                                   "url": f"https://dev.to/dokaz/{slug}",
                                   "canonical_url": body["article"]["canonical_url"]})
        if step == "me":
            return _Response(200, {"type_of": "user", "id": 77, "username": "dokaz",
                                   "name": "Dokaz Industries"})
        self._error(url, 404, {"error": "not found", "status": 404})
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
    """A hermetic runtime with the dev.to adapter talking to FakeWorld."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.key_file = self.root / "secrets" / "devto-api-key.txt"
        self.key_file.parent.mkdir(parents=True)
        self.key_file.write_text(KEY + "\n", encoding="utf-8")
        self.ledger = self.root / "devto" / "posts.json"
        self.world = FakeWorld()
        self.adapter = self.make_adapter()
        # content_url=None: no default content/devto adapters; the faked one is used
        runtime = build_runtime(_settings(self.root, content_url=None))
        runtime.register(self.adapter)
        self.app = PionirApp(runtime)

    def make_adapter(self) -> DevtoAdapter:
        return DevtoAdapter(DevtoSettings(api_url=DEVTO, key_file=self.key_file,
                                          ledger_file=self.ledger, blog_url=BLOG),
                            opener=self.world, clock=lambda: "2026-09-25T12:00:00Z")

    def tearDown(self) -> None:
        self.app.runtime.cortex.close()
        self._tmp.cleanup()

    def forget(self) -> None:
        """Start the ledger over: the file, and what this process remembers."""
        self.ledger.unlink(missing_ok=True)
        devto._POSTED.clear()

    def execute(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return dict(self.adapter.execute(Task(CROSSPOST, payload or article())).output)

    def run_crosspost(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Park, approve, wait: the only way an article goes up."""
        out = self.app.run_task(CROSSPOST, payload or article(), permissions=[CROSSPOST])
        self.assertEqual(out["status"], "pending_approval", out)
        res = self.app.approve(out["approval_id"])
        self.assertTrue(self.app.jobs.wait(res["task_id"], 30))
        return self.app.approvals.get(out["approval_id"])


# ---- the gate ---------------------------------------------------------------------
class GateTests(_Case):
    def test_it_is_declared_as_always_approved_and_never_routed(self) -> None:
        (cap,) = self.adapter.manifest.capabilities
        self.assertEqual(cap.name, "content.crosspost_devto")
        self.assertTrue(cap.requires_approval)
        self.assertIs(cap.risk, RiskLevel.PRIVILEGED)
        self.assertFalse(cap.routable)

    def test_it_parks_every_time_even_with_the_permission(self) -> None:
        for _ in range(3):
            out = self.app.run_task(CROSSPOST, article(), permissions=[CROSSPOST])
            self.assertEqual(out["status"], "pending_approval")
        pending = self.app.approvals.pending()
        self.assertEqual(len(pending), 3)
        self.assertIn(article()["title"], pending[0]["summary"])
        self.assertEqual(self.world.calls, [])

    def test_an_approved_crosspost_runs_once_exactly_as_checked(self) -> None:
        row = self.run_crosspost()
        self.assertEqual(row["status"], "approved", row)
        self.assertEqual(self.world.steps(), ["live", "article"])
        live, post = self.world.calls
        self.assertEqual((live["method"], live["url"]), ("GET", f"{BLOG}/blog/{SLUG}"))
        self.assertIsNone(live["key"])                          # the key goes only to dev.to
        self.assertEqual((post["method"], post["url"]), ("POST", f"{DEVTO}/articles"))
        self.assertEqual(post["key"], KEY)
        self.assertEqual(post["accept"], FOREM_ACCEPT)
        self.assertEqual(post["accept"], "application/vnd.forem.api-v1+json")
        self.assertEqual(post["content_type"], "application/json")
        self.assertEqual(post["body"], {"article": {
            "title": article()["title"],
            "body_markdown": article()["body_md"],
            "published": True,
            "canonical_url": f"https://api.dokaz.net/blog/{SLUG}",
            "description": article()["description"],
            "tags": "email,webdev,api",
        }})
        self.assertTrue(all(c["timeout"] == 30 for c in self.world.calls))
        result = row["result"]["result"]
        self.assertEqual(result, {"ok": True, "id": 1001,
                                  "url": "https://dev.to/dokaz/check-an-email-1001",
                                  "canonical_url": f"https://api.dokaz.net/blog/{SLUG}"})
        self.assertEqual(json.loads(self.ledger.read_text(encoding="utf-8")), {
            DRAFT_ID: {"id": 1001, "url": result["url"], "posted_at": "2026-09-25T12:00:00Z"}})
        self.assertFalse(self.app.approve(row["id"])["ok"])       # never twice
        self.assertEqual(len(self.world.articles()), 1)

    def test_a_denied_crosspost_never_runs(self) -> None:
        aid = self.app.run_task(CROSSPOST, article(), permissions=[CROSSPOST])["approval_id"]
        self.assertTrue(self.app.deny(aid)["ok"])
        self.assertFalse(self.app.approve(aid)["ok"])
        self.assertEqual(self.world.calls, [])
        self.assertFalse(self.ledger.exists())


# ---- the check before parking ------------------------------------------------------
class LocalValidationTests(_Case):
    OTHER = attribution_line("some-other-post", DRAFT_ID)
    BAD: ClassVar[dict[str, dict[str, Any]]] = {
        "no attribution line": {"body_md": make_body(ending="Thanks for reading.")},
        "foreign attribution line": {"body_md": make_body(ending=OTHER)},
        "attribution for another campaign": {"body_md": make_body(
            ending=attribution_line(SLUG, "2026-01-01-something-else"))},
        "attribution with blog utm": {"body_md": make_body(
            ending=attribution_line(SLUG, DRAFT_ID).replace("devto", "blog"))},
        "attribution not last": {"body_md": make_body() + "\nOne more line.\n"},
        "utm_source=blog link": {"body_md": make_body(
            extra=f"See [the blog](https://api.dokaz.net/blog/other?utm_source=blog"
                  f"&utm_medium=referral&utm_campaign={DRAFT_ID}).")},
        "untagged Dokaz link": {"body_md": make_body(
            extra="See [the site](https://www.dokazindustries.com/about).")},
        "utm_source=blog in description": {"description": (
            "Why checking an address first keeps your reputation clean: "
            "[see](https://api.dokaz.net/email?utm_source=blog).")},
        "5 tags": {"tags": ["email", "webdev", "api", "saas", "tools"]},
        "tag with a hyphen": {"tags": ["email", "web-dev"]},
        "no tags": {"tags": []},
        "missing tags": {"tags": None},
        "uppercase tag": {"tags": ["Email"]},
        "31-character tag": {"tags": ["a" * 31]},
        "repeated tag": {"tags": ["email", "email"]},
        "front matter": {"body_md": "---\ntitle: x\npublished: true\n---\n" + make_body()},
        "liquid embed": {"body_md": make_body(extra="{% youtube dQw4w9WgXcQ %}")},
        "liquid output": {"body_md": make_body(extra="Hello {{ user.name }} there.")},
        "at-handle": {"body_md": make_body(extra="Thanks to @ben for the idea.")},
        "canonical_url field": {"canonical_url": "https://evil.example.com/x"},
        "blog rule: foreign host": {"body_md": make_body(
            extra="[x](https://evil.example.com/a)")},
        "blog rule: email": {"body_md": make_body(extra="Write to ian@fastmail.net.")},
        "blog rule: short title": {"title": "Too short"},
        "blog rule: bad slug": {"slug": "-bad"},
    }
    WHY: ClassVar[dict[str, str]] = {
        "no attribution line": "body_md: must end with the line *Originally published at",
        "foreign attribution line": f"body_md: must end with the line "
                                    f"{attribution_line(SLUG, DRAFT_ID)}",
        "attribution for another campaign": "body_md: must end with the line",
        "attribution with blog utm": "body_md: every link to a Dokaz page must carry "
                                     "utm_source=devto",
        "attribution not last": "body_md: must end with the line",
        "utm_source=blog link": "has utm_source=blog",
        "untagged Dokaz link": "has no single utm_source",
        "utm_source=blog in description": "description: every link to a Dokaz page",
        "5 tags": "tags: a list of 1-4", "tag with a hyphen": "tags: each 1-30 of a-z and 0-9",
        "no tags": "tags: a list of 1-4", "missing tags": "tags: required",
        "uppercase tag": "tags: each 1-30", "31-character tag": "tags: each 1-30",
        "repeated tag": "tags: each tag once", "front matter": "body_md: cannot open with ---",
        "liquid embed": "body_md: cannot carry {% %}", "liquid output": "Liquid",
        "at-handle": "body_md: cannot carry an @-handle ('@ben')",
        "canonical_url field": "canonical_url: not a cross-post field",
        "blog rule: foreign host": "body_md: links may only go to",
        "blog rule: email": "body_md: no email", "blog rule: short title": "title: 10-120",
        "blog rule: bad slug": "slug: 3-80",
    }

    def test_every_rule_is_enforced_before_anything_is_sent_or_parked(self) -> None:
        self.assertEqual(check_crosspost(article()), article())      # the good one passes
        self.assertEqual(set(self.BAD), set(self.WHY))
        for label, change in self.BAD.items():
            with self.subTest(label):
                payload = article(**change)
                with self.assertRaises(AdapterProtocolError) as refused:
                    self.adapter.validate(Task(CROSSPOST, payload))
                self.assertIn(self.WHY[label], str(refused.exception))
                with self.assertRaises(AdapterProtocolError):
                    self.adapter.execute(Task(CROSSPOST, payload))
                out = self.app.run_task(CROSSPOST, payload, permissions=[CROSSPOST])
                self.assertEqual(out["status"], "error", out)
                self.assertEqual(out["error"]["type"], "AdapterProtocolError")
        self.assertEqual(self.app.approvals.pending(), [])
        self.assertEqual(self.world.calls, [])

    def test_what_is_allowed(self) -> None:
        # an API endpoint is not a page: it needs no UTM tags, like the blog's own rule
        check_crosspost(article(body_md=make_body(
            extra="Call `GET https://api.dokaz.net/v1/email/check` from anywhere.")))
        check_crosspost(article(tags=["a" * 30]))
        check_crosspost(article(tags=["email"]))
        check_crosspost(article(body_md=make_body().rstrip("\n")))     # no final newline
        check_crosspost(article(body_md=make_body(extra="Test with user@example.com first.")))

    def test_the_campaign_agrees_with_the_crews(self) -> None:
        long_id = "2026-09-25-why-a-bounded-digest-beats-a-firehose"
        self.assertEqual(long_id[:40][-1], "-")       # the case where a plain cut differs
        for draft_id in (DRAFT_ID, long_id, "x" * 64):
            with self.subTest(draft_id):
                self.assertEqual(utm_campaign(draft_id), contentcheck.utm_campaign(draft_id))
        self.assertEqual(attribution_line(SLUG, DRAFT_ID),
                         "*Originally published at [api.dokaz.net](https://api.dokaz.net/blog/"
                         "verify-email?utm_source=devto&utm_medium=referral&utm_campaign="
                         "2026-09-25-verify-email)*")

    def test_a_missing_key_is_unavailable_before_parking(self) -> None:
        self.key_file.unlink()
        with self.assertRaisesRegex(AdapterUnavailable, "not configured") as err:
            self.adapter.validate(Task(CROSSPOST, article()))
        self.assertIn(str(self.key_file), str(err.exception))
        self.assertIn(r"tools\setup-devto.ps1", str(err.exception))
        with self.assertRaisesRegex(AdapterUnavailable, "not configured"):
            self.adapter.status()
        out = self.execute()
        self.assertIs(out["ok"], False)
        self.assertTrue(out["not_configured"])
        parked = self.app.run_task(CROSSPOST, article(), permissions=[CROSSPOST])
        self.assertEqual(parked["status"], "error")
        self.assertEqual(parked["error"]["type"], "AdapterUnavailable")
        self.assertEqual(self.app.approvals.pending(), [])
        self.assertEqual(self.world.calls, [])

    def test_status_is_local(self) -> None:
        self.assertEqual(self.adapter.status(), {"api": DEVTO, "key": "configured",
                                                 "cross_posted": 0})
        self.execute()
        self.assertEqual(self.adapter.status()["cross_posted"], 1)
        self.assertEqual(self.world.steps(), ["live", "article"])      # status added none


# ---- what is sent -------------------------------------------------------------------------
class CanonicalTests(_Case):
    def test_the_canonical_url_is_built_from_the_slug_whatever_the_body_says(self) -> None:
        body = make_body(extra=(
            "canonical_url: https://api.dokaz.net/blog/some-other-post?utm_source=devto\n\n"
            "The canonical copy is [elsewhere](https://api.dokaz.net/blog/elsewhere?"
            "utm_source=devto)."))
        out = self.execute(article(body_md=body))
        self.assertIs(out["ok"], True, out)
        (post,) = self.world.articles()
        self.assertEqual(post["body"]["article"]["canonical_url"],
                         "https://api.dokaz.net/blog/verify-email")
        self.assertEqual(out["canonical_url"], "https://api.dokaz.net/blog/verify-email")
        self.assertEqual(post["body"]["article"]["body_markdown"], body)

    def test_the_canonical_url_is_the_public_blog_not_the_configured_base(self) -> None:
        self.execute()
        live, post = self.world.calls
        self.assertTrue(live["url"].startswith(BLOG))
        self.assertTrue(post["body"]["article"]["canonical_url"].startswith(
            "https://api.dokaz.net/blog/"))


class LiveCheckTests(_Case):
    def test_the_liveness_check_is_not_counted_as_a_visitor(self) -> None:
        # Scrooge's traffic counter skips any agent matching its CRAWLER pattern; the
        # end-to-end run showed the check counted as a view without it (8 for 7 visits)
        import re
        self.execute()
        (live,) = [c for c in self.world.calls if c["step"] == "live"]
        self.assertRegex(live["agent"], re.compile(r"bot|crawl|spider|slurp|preview", re.IGNORECASE))

    def test_an_original_that_is_not_live_is_refused_and_dev_to_never_called(self) -> None:
        self.world.live = set()
        out = self.execute()
        self.assertIs(out["ok"], False)
        self.assertIn(NOT_LIVE, out["refused"])
        self.assertIn("HTTP 404", out["refused"])
        self.assertEqual(self.world.steps(), ["live"])
        self.assertFalse(self.ledger.exists())

    def test_a_blog_that_does_not_answer_is_not_live_either(self) -> None:
        for label, change in (("down", "down"), ("500", "500")):
            with self.subTest(label):
                self.world.calls.clear()
                if change == "down":
                    self.world.down = {"api.dokaz.test"}
                else:
                    self.world.down = set()
                    self.world.forced = {"live": (500, b"oops")}
                out = self.execute()
                self.assertIn(NOT_LIVE, out["refused"])
                self.assertEqual(self.world.steps(), ["live"])

    def test_through_the_gate_it_settles_failed(self) -> None:
        self.world.live = set()
        row = self.run_crosspost()
        self.assertEqual(row["status"], "approved_failed")
        self.assertIn(NOT_LIVE, row["result"]["result"]["refused"])
        self.assertEqual(self.world.articles(), [])


class LedgerTests(_Case):
    def test_a_second_crosspost_of_the_same_draft_is_refused_with_the_first_url(self) -> None:
        first = self.execute()
        self.assertIs(first["ok"], True)
        second = self.execute()
        self.assertIs(second["ok"], False)
        self.assertIn(first["url"], second["refused"])
        self.assertEqual(second["url"], first["url"])
        self.assertEqual(len(self.world.articles()), 1)
        # a different draft still goes
        other = article(draft_id="2026-09-26-verify-email",
                        body_md=make_body(draft_id="2026-09-26-verify-email").replace(
                            DRAFT_ID, "2026-09-26-verify-email"))
        self.assertIs(self.execute(other)["ok"], True)
        self.assertEqual(len(self.world.articles()), 2)
        self.assertEqual(set(json.loads(self.ledger.read_text(encoding="utf-8"))),
                         {DRAFT_ID, "2026-09-26-verify-email"})
        self.assertEqual(list(self.ledger.parent.glob("*.tmp")), [])

    def test_a_crossposted_draft_is_refused_before_parking(self) -> None:
        first = self.execute()
        out = self.app.run_task(CROSSPOST, article(), permissions=[CROSSPOST])
        self.assertEqual(out["status"], "error", out)
        self.assertIn(first["url"], out["error"]["message"])
        self.assertEqual(self.app.approvals.pending(), [])

    def test_two_approvals_parked_before_either_ran_post_once(self) -> None:
        aids = [self.app.run_task(CROSSPOST, article(), permissions=[CROSSPOST])["approval_id"]
                for _ in range(2)]
        for aid in aids:
            res = self.app.approve(aid)
            self.assertTrue(self.app.jobs.wait(res["task_id"], 30))
        rows = [self.app.approvals.get(aid) for aid in aids]
        self.assertEqual(sorted(r["status"] for r in rows), ["approved", "approved_failed"])
        self.assertEqual(len(self.world.articles()), 1)
        failed = next(r for r in rows if r["status"] == "approved_failed")
        self.assertIn("already cross-posted", failed["result"]["result"]["refused"])

    def test_concurrent_crossposts_of_one_draft_post_once(self) -> None:
        results: list[dict[str, Any]] = []
        threads = [threading.Thread(target=lambda: results.append(self.execute()))
                   for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(sorted(r["ok"] for r in results), [False, False, False, True])
        self.assertEqual(len(self.world.articles()), 1)

    def test_an_unreadable_ledger_stops_it(self) -> None:
        self.ledger.parent.mkdir(parents=True)
        self.ledger.write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(AdapterUnavailable, "ledger"):
            self.adapter.validate(Task(CROSSPOST, article()))
        out = self.execute()
        self.assertIn("ledger", out["unavailable"])
        self.assertEqual(self.world.calls, [])

    def test_any_ledger_entry_counts_even_a_bare_one(self) -> None:
        self.ledger.parent.mkdir(parents=True)
        self.ledger.write_text(json.dumps({DRAFT_ID: {}}), encoding="utf-8")
        out = self.execute()
        self.assertIn("already cross-posted", out["refused"])
        self.assertEqual(self.world.calls, [])

    def test_a_failed_article_is_not_recorded(self) -> None:
        self.world.forced["article"] = (422, {"error": "Title is too short", "status": 422})
        self.execute()
        self.assertFalse(self.ledger.exists())
        self.world.forced.clear()
        self.assertIs(self.execute()["ok"], True)

    def test_a_live_article_whose_ledger_write_fails_is_still_reported_live(self) -> None:
        with mock.patch("pionir.adapters.devto.os.replace", side_effect=OSError("disk")):
            out = self.execute()
        self.assertIs(out["ok"], True)
        self.assertIn("do not cross-post this draft again", out["ledger_error"])
        self.assertEqual(list(self.ledger.parent.glob("*.tmp")), [])
        # while this process runs it still knows, even with nothing on disk
        again = self.execute()
        self.assertIn(out["url"], again["refused"])
        self.assertEqual(len(self.world.articles()), 1)


class ErrorTests(_Case):
    def test_a_rejected_key_is_unavailable_with_the_setup_hint(self) -> None:
        for status in (401, 403):
            with self.subTest(status):
                self.world.forced = {"article": (status, {"error": "unauthorized"})}
                out = self.execute()
                self.assertEqual(out["unavailable"], KEY_REJECTED)
                self.assertEqual(out["unavailable"],
                                 r"the dev.to API key was rejected - run tools\setup-devto.ps1")
                self.assertTrue(out["key_rejected"])
                self.assertNotIn("refused", out)
        self.world.forced = {}
        self.world.valid_keys = set()                      # the fake's own key check
        self.assertEqual(self.execute()["unavailable"], KEY_REJECTED)
        self.assertFalse(self.ledger.exists())

    def test_dev_to_saying_no_is_refused_with_its_words(self) -> None:
        for status in (400, 422):
            with self.subTest(status):
                self.world.forced = {"article": (status, {
                    "error": "Canonical url has already been taken", "status": status})}
                out = self.execute()
                self.assertEqual(out["refused"], "Canonical url has already been taken")
                self.assertEqual(out["status"], status)

    def test_rate_limits_5xx_and_network_are_unavailable(self) -> None:
        cases = {"429": ((429, {"error": "Rate limit reached"}), "rate limiting"),
                 "500": ((500, {"error": "boom"}), "HTTP 500"),
                 "503": ((503, b"<html>down</html>"), "HTTP 503")}
        for label, (forced, text) in cases.items():
            with self.subTest(label):
                self.world.forced = {"article": forced}
                out = self.execute()
                self.assertIn(text, out["unavailable"])
                self.assertNotIn("refused", out)
        self.world.forced = {}
        self.world.down = {"devto.test"}
        out = self.execute()
        self.assertIn("unreachable", out["unavailable"])
        self.assertIn("check dev.to before trying again", out["unavailable"])
        self.assertFalse(self.ledger.exists())

    def test_through_the_gate_a_rejected_key_settles_failed_with_the_hint(self) -> None:
        self.world.valid_keys = set()
        row = self.run_crosspost()
        self.assertEqual(row["status"], "approved_failed")
        self.assertEqual(row["result"]["result"]["unavailable"], KEY_REJECTED)


class SecrecyTests(_Case):
    def test_the_key_never_appears_in_results_errors_logs_or_records(self) -> None:
        capture = io.StringIO()
        handler = logging.StreamHandler(capture)
        root = logging.getLogger()
        old = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(handler)
        echo = f"bad key {KEY}"
        try:
            outputs: list[Any] = []
            self.world.forced = {"article": (422, {"error": echo})}
            outputs.append(self.execute())
            self.world.forced = {"article": (500, {"error": echo})}
            outputs.append(self.execute())
            self.world.forced = {"article": (429, {"error": echo})}
            outputs.append(self.execute())
            self.world.forced = {"article": (201, {"id": 5, "url": f"https://dev.to/x?{KEY}"})}
            outputs.append(self.execute())      # dev.to echoing it in a success, too
            self.assertNotIn(KEY, self.ledger.read_text(encoding="utf-8"))
            self.forget()
            self.world.forced = {}
            self.world.down = {"devto.test"}    # the transport error names it
            outputs.append(self.execute())
            self.world.down = set()
            outputs.append(self.adapter.status())
            outputs.append(repr(self.adapter.settings))
            outputs.append(self.run_crosspost())       # through the gate, audited
            self.forget()
            self.world.valid_keys = set()
            outputs.append(self.run_crosspost())       # a rejected key, through the gate
            self.world.valid_keys = {KEY}
            with contextlib.redirect_stdout(io.StringIO()) as printed:
                cli.devto_check(_settings(self.root, devto_key_file=self.key_file,
                                          devto_url=DEVTO), opener=self.world)
            outputs.append(printed.getvalue())
        finally:
            root.removeHandler(handler)
            root.setLevel(old)
        # the key really was used and echoed, so its absence below means something
        self.assertIn(KEY, {c["key"] for c in self.world.calls})
        dumped = json.dumps(outputs, default=str)
        self.assertIn("<redacted>", dumped)
        self.assertNotIn(KEY, dumped)
        self.assertNotIn(KEY, capture.getvalue())
        self.assertIn("devto:", capture.getvalue())      # the adapter did log
        for path in self.root.rglob("*"):   # the audit ledger, jobs, approvals, memory, posts
            if path.is_file() and path != self.key_file:
                self.assertNotIn(KEY.encode("utf-8"), path.read_bytes(), path)


# ---- settings and wiring -----------------------------------------------------------------
class SettingsTests(unittest.TestCase):
    def test_defaults_env_and_off(self) -> None:
        settings = PionirSettings(state_root=Path("C:/x"))
        self.assertEqual(settings.devto_url, "https://dev.to/api")
        self.assertEqual(settings.devto_key_path,
                         Path.home() / ".pionir" / "secrets" / "devto-api-key.txt")
        self.assertEqual(settings.devto_ledger_path, Path("C:/x") / "devto" / "posts.json")
        self.assertEqual(DevtoSettings().api_url, "https://dev.to/api")
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            os.environ, {"PIONIR_STATE_ROOT": root}
        ):
            os.environ.pop("PIONIR_DEVTO_URL", None)
            os.environ.pop("PIONIR_DEVTO_KEY_FILE", None)
            self.assertEqual(PionirSettings.from_environment().devto_url, "https://dev.to/api")
            os.environ["PIONIR_DEVTO_URL"] = "off"
            self.assertIsNone(PionirSettings.from_environment().devto_url)
            os.environ["PIONIR_DEVTO_KEY_FILE"] = str(Path(root) / "k.txt")
            self.assertEqual(PionirSettings.from_environment().devto_key_path,
                             Path(root) / "k.txt")

    def test_bootstrap_registers_it_only_with_the_blog_and_without_the_network(self) -> None:
        cases = (("https://api.dokaz.net", DEVTO, True), (None, DEVTO, False),
                 ("https://api.dokaz.net", None, False))
        for content, api, present in cases:
            with self.subTest(content=content, api=api), \
                    tempfile.TemporaryDirectory() as root:
                runtime = build_runtime(_settings(
                    Path(root), content_url=content, devto_url=api,
                    content_token_file=Path(root) / "no-token.txt",
                    devto_key_file=Path(root) / "no-key.txt"))
                try:
                    self.assertEqual("devto" in runtime.adapters, present)
                    if present:   # doctor's health check is local: no key, no call
                        adapter = runtime.adapters["devto"]
                        with self.assertRaisesRegex(AdapterUnavailable, "not configured"):
                            adapter.status()
                        self.assertEqual(adapter.settings.ledger_file,
                                         Path(root) / "devto" / "posts.json")
                        self.assertEqual(adapter.settings.blog_url, content)
                finally:
                    runtime.cortex.close()

    def test_the_key_never_leaves_over_plain_http_to_a_remote_host(self) -> None:
        with self.assertRaises(ValueError):
            DevtoSettings(api_url="http://dev.to/api")
        with self.assertRaises(ValueError):
            DevtoSettings(blog_url="http://api.dokaz.net")
        DevtoSettings(api_url="http://127.0.0.1:9/api")


# ---- devto-check ---------------------------------------------------------------------------
class DevtoCheckTests(_Case):
    def check(self) -> tuple[int, str]:
        settings = _settings(self.root, devto_key_file=self.key_file, devto_url=DEVTO)
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            code = cli.devto_check(settings, opener=self.world)
        return code, printed.getvalue()

    def test_ok_prints_the_username_and_never_posts(self) -> None:
        code, printed = self.check()
        self.assertEqual(code, 0, printed)
        report = json.loads(printed)
        self.assertEqual(report["username"], "dokaz")
        self.assertEqual(self.world.steps(), ["me"])
        (me,) = self.world.calls
        self.assertEqual((me["method"], me["key"], me["accept"]), ("GET", KEY, FOREM_ACCEPT))
        self.assertNotIn(KEY, printed)

    def test_not_configured_is_1_rejected_is_2_and_down_is_1(self) -> None:
        self.world.valid_keys = set()
        code, printed = self.check()
        self.assertEqual(code, 2)
        self.assertIn("setup-devto.ps1", printed)
        self.world.valid_keys = {KEY}
        self.world.down = {"devto.test"}
        code, printed = self.check()
        self.assertEqual(code, 1)
        self.assertIn("not_checked", printed)
        self.key_file.unlink()
        code, printed = self.check()
        self.assertEqual(code, 1)
        self.assertIn("not_configured", printed)
        self.assertNotIn("article", self.world.steps())

    def test_the_command_is_wired(self) -> None:
        with mock.patch.object(cli, "devto_check", return_value=0) as check:
            self.assertEqual(cli.main(["devto-check"]), 0)
        check.assert_called_once_with()


# ---- the Discord card -----------------------------------------------------------------------
class DiscordCardTests(_Case):
    def test_the_card_shows_the_canonical_url_title_tags_and_the_whole_body(self) -> None:
        paragraphs = "\n\n".join(
            f"Paragraph {i}: a bounced message chips away at how inboxes treat your mail, "
            "so checking first keeps the sender reputation clean." for i in range(200))
        body = make_body(extra=paragraphs)
        self.assertGreater(len(body), 20_000)
        payload = article(body_md=body)
        out = self.app.run_task(CROSSPOST, payload, permissions=[CROSSPOST])
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
        self.assertEqual(head.split("\n")[0], DEVTO_LINE)
        self.assertTrue(head.startswith(
            "\U0001f4f0 **CROSS-POSTS TO DEV.TO** - the blog post below, already live on "
            "api.dokaz.net, goes up on dev.to under your account if you approve."))
        self.assertIn("<https://api.dokaz.net/blog/verify-email>", head)
        self.assertIn(_escape(payload["title"]), head)
        self.assertIn("`email`, `webdev`, `api`", head)
        self.assertIn("`content.crosspost_devto`", head)
        text = "\n".join(line for chunk in posts for line in chunk.split("\n")
                         if not line.startswith("```"))
        self.assertIn(body, text)                           # the whole body, verbatim
        self.assertIn(attribution_line(SLUG, DRAFT_ID), text)
        self.assertEqual(self.world.calls, [])              # showing it sends nothing
        self.assertEqual(len(self.app.approvals.pending()), 1)


# ---- the real opener ----------------------------------------------------------------------
class _Recorder(BaseHTTPRequestHandler):
    seen: ClassVar[list[dict[str, Any]]]
    answer: ClassVar[Any]

    def _reply(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        record = {"method": self.command, "path": self.path, "body": body,
                  "headers": {k.lower(): v for k, v in self.headers.items()}}
        type(self).seen.append(record)
        status, content_type, out = type(self).answer(record)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = _reply

    def log_message(self, *_a: Any) -> None:
        pass


def serve(test: unittest.TestCase, answer: Any) -> tuple[str, list[dict[str, Any]]]:
    seen: list[dict[str, Any]] = []
    handler = type("Handler", (_Recorder,), {"seen": seen, "answer": staticmethod(answer)})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    test.addCleanup(httpd.server_close)
    test.addCleanup(httpd.shutdown)
    return f"http://127.0.0.1:{httpd.server_address[1]}", seen


class RealOpenerTests(unittest.TestCase):
    """The adapter's real urllib opener against real loopback HTTP servers for the blog and
    dev.to: the path the owner's approval takes. content.publish's first real run crashed
    here with every fake green."""

    def test_a_crosspost_through_the_real_opener(self) -> None:
        def blog(record: dict[str, Any]) -> tuple[int, str, bytes]:
            if record["path"] == f"/blog/{SLUG}":
                return 200, "text/html", b"<!doctype html><title>post</title>"
            return 404, "text/html", b"<!doctype html><title>no</title>"

        def devto(record: dict[str, Any]) -> tuple[int, str, bytes]:
            if record["headers"].get("api-key") != KEY:
                return 401, "application/json", b'{"error":"unauthorized","status":401}'
            if record["path"] == "/api/articles":
                return 201, "application/json", json.dumps(
                    {"id": 42, "slug": "check-an-email-4x2",
                     "url": "https://dev.to/dokaz/check-an-email-4x2"}).encode()
            return 200, "application/json", b'{"username":"dokaz","id":7}'

        blog_url, blog_seen = serve(self, blog)
        devto_url, devto_seen = serve(self, devto)
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "devto-api-key.txt"
            key.write_text(KEY, encoding="utf-8")
            ledger = Path(tmp) / "devto" / "posts.json"
            adapter = DevtoAdapter(DevtoSettings(api_url=f"{devto_url}/api", key_file=key,
                                                 ledger_file=ledger, blog_url=blog_url))
            out = dict(adapter.execute(Task(CROSSPOST, article())).output)   # real opener
            self.assertEqual(json.loads(ledger.read_text(encoding="utf-8"))[DRAFT_ID]["id"], 42)
            code, report = adapter.check_account()
            not_live = dict(adapter.execute(Task(CROSSPOST, article(
                draft_id="2026-09-26-other", slug="other-post",
                body_md=make_body(slug="other-post", draft_id="2026-09-26-other").replace(
                    DRAFT_ID, "2026-09-26-other")))).output)
        self.assertEqual(out, {"ok": True, "id": 42,
                               "url": "https://dev.to/dokaz/check-an-email-4x2",
                               "canonical_url": f"https://api.dokaz.net/blog/{SLUG}"})
        self.assertEqual((code, report["username"]), (0, "dokaz"))
        self.assertIn(NOT_LIVE, not_live["refused"])
        self.assertEqual([(r["method"], r["path"]) for r in blog_seen],
                         [("GET", f"/blog/{SLUG}"), ("GET", "/blog/other-post")])
        self.assertNotIn("api-key", blog_seen[0]["headers"])
        self.assertEqual([(r["method"], r["path"]) for r in devto_seen],
                         [("POST", "/api/articles"), ("GET", "/api/users/me")])
        post = devto_seen[0]
        self.assertEqual(post["headers"]["api-key"], KEY)
        self.assertEqual(post["headers"]["accept"], "application/vnd.forem.api-v1+json")
        self.assertEqual(post["headers"]["content-type"], "application/json")
        self.assertEqual(json.loads(post["body"]), {"article": {
            "title": article()["title"], "body_markdown": article()["body_md"],
            "published": True, "canonical_url": f"https://api.dokaz.net/blog/{SLUG}",
            "description": article()["description"], "tags": "email,webdev,api"}})


if __name__ == "__main__":
    unittest.main()
