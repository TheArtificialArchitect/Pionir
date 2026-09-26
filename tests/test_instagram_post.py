"""Nothing goes on Instagram without the owner's yes.

The same rule as the blog: ``social.instagram_post`` parks on every call, runs once after an
approval and never after a denial. These tests pin the adapter (the check before parking, the
Scrooge media upload, the Graph container / poll / publish / permalink dance, the error
mapping, the token refresh and the token never leaking), the Discord card that carries the
rendered image, the ``instagram-check`` command, and - the lesson ``content.publish`` paid for
- the real urllib opener against real loopback HTTP servers for Scrooge and the Graph API.

Scrooge and Instagram are faked at the HTTP opener with the real opener's signature
``(request, data=None, timeout=None)``; nothing touches the network.
"""

from __future__ import annotations

import contextlib
import email.parser
import email.policy
import hashlib
import io
import json
import logging
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar, Self
from unittest import mock

from test_discord_gate import API, CHANNEL, OWNER, FakeDiscord
from test_discord_gate import TOKEN as DISCORD_TOKEN

from pionir import cli, discord_gate
from pionir.adapters.content import ContentSettings
from pionir.adapters.instagram import (
    POST,
    TOKEN_REJECTED,
    InstagramAdapter,
    InstagramSettings,
    read_instagram_token,
)
from pionir.bootstrap import build_runtime
from pionir.config import PionirSettings
from pionir.contracts import RiskLevel, Task
from pionir.discord_gate import (
    INSTAGRAM_LINE,
    DiscordGate,
    DiscordGateSettings,
    _escape,
    multipart_body,
)
from pionir.errors import AdapterProtocolError, AdapterUnavailable
from pionir.server import PionirApp
from pionir.social.card import card_sha, render_card
from pionir.social.post import full_caption

PUBLISH_SECRET = "scrooge-SECRET-publish-token-7f3a9c"
IG_SECRET = "IGAAsecretINSTAGRAMtoken0123456789abcdefghijklmnopqrstuv"
IG_FRESH = "IGAAfreshREFRESHEDtoken9876543210zyxwvutsrqponmlkjihgfedc"
USER_ID = "17841400000000001"
SCROOGE = "https://api.dokaz.test"
GRAPH = "https://graph.instagram.test/v25.0"
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)

GOOD = {
    "draft_id": "2026-09-25-verify-email",
    "headline": "Check an email address before you hit send",
    "points": ["A typo in the domain bounces, and bounces hurt everything you send after.",
               "Syntax, domain and mail server: three checks, one call.",
               "Catch throwaway addresses at sign-up, not after."],
    "caption": "Every bounced message chips away at how inboxes treat your mail. Checking the "
               "address first is cheap, fast, and keeps your sender reputation clean.",
    "hashtags": ["emaildeliverability", "webdev"],
}


def post(**over: Any) -> dict[str, Any]:
    out = {**GOOD, **over}
    return {k: v for k, v in out.items() if v is not None}


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


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


class FakeWorld:
    """Scrooge's media route and the Instagram Graph API, in memory. Each checks its
    token like the real one. Same call signature as OpenerDirector.open."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.uploads: list[bytes] = []
        self.valid_ig = {IG_SECRET, IG_FRESH}
        self.statuses: list[str] = ["IN_PROGRESS", "FINISHED"]
        self.forced: dict[str, tuple[int, Any]] = {}   # step -> (status, body)
        self.sha_override: str | None = None
        self.down: set[str] = set()                    # hosts that do not answer
        self._lock = threading.Lock()

    def steps(self) -> list[str]:
        return [c["step"] for c in self.calls]

    def __call__(self, request: Any, data: Any = None, timeout: float | None = None) -> _Response:
        # The real opener is OpenerDirector.open(url, data=None, timeout=...): a timeout
        # passed positionally lands in `data`. This fake has the same signature, so it
        # fails the same way.
        if data is not None:
            raise TypeError(f"opener got a positional data argument: {data!r}")
        with self._lock:
            return self._handle(request, timeout)

    def _error(self, url: str, status: int, payload: Any) -> None:
        raise urllib.error.HTTPError(url, status, "error", {},  # type: ignore[arg-type]
                                     io.BytesIO(json.dumps(payload).encode("utf-8")))

    def _handle(self, request: Any, timeout: float | None) -> _Response:
        url = request.full_url
        parsed = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        method = request.get_method()
        if parsed.netloc in self.down:
            raise urllib.error.URLError(f"connection refused: {url}")
        if url.startswith(SCROOGE):
            return self._scrooge(request, url, method, timeout)
        form = dict(urllib.parse.parse_qsl(request.data.decode())) if request.data else {}
        token = form.get("access_token") or query.get("access_token")
        path = parsed.path
        if path == "/refresh_access_token":
            step = "refresh"
        elif path.endswith("/media_publish"):
            step = "publish"
        elif path.endswith("/media"):
            step = "container"
        elif path.endswith("/content_publishing_limit"):
            step = "quota"
        elif path.endswith("/me"):
            step = "me"
        elif query.get("fields") == "status_code":
            step = "status"
        elif query.get("fields") == "permalink":
            step = "permalink"
        else:
            step = "unknown"
        self.calls.append({"step": step, "method": method, "url": url, "path": path,
                           "query": query, "form": form, "token": token, "timeout": timeout})
        if step in self.forced:
            status, payload = self.forced[step]
            if status != 200:
                self._error(url, status, payload)
            return _Response(200, payload)
        if token not in self.valid_ig:
            self._error(url, 400, {"error": {"message": "Invalid OAuth access token - "
                                             "Cannot parse access token", "type": "OAuthException",
                                             "code": 190, "fbtrace_id": "x"}})
        if step == "refresh":
            return _Response(200, {"access_token": IG_FRESH, "token_type": "bearer",
                                   "expires_in": 5_184_000})
        if step == "container":
            return _Response(200, {"id": "C-900"})
        if step == "status":
            code = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return _Response(200, {"status_code": code, "id": "C-900"})
        if step == "publish":
            return _Response(200, {"id": "M-17900"})
        if step == "permalink":
            return _Response(200, {"permalink": "https://www.instagram.com/p/DOKAZ1/",
                                   "id": "M-17900"})
        if step == "me":
            return _Response(200, {"user_id": USER_ID, "username": "dokaz.industries",
                                   "id": "999"})
        if step == "quota":
            return _Response(200, {"data": [{"quota_usage": 3, "config": {
                "quota_total": 100, "quota_duration": 86400}}]})
        self._error(url, 404, {"error": {"message": "unknown path", "code": 100}})
        raise AssertionError  # unreachable

    def _scrooge(self, request: Any, url: str, method: str, timeout: float | None) -> _Response:
        self.calls.append({"step": "upload", "method": method, "url": url,
                           "token": request.get_header("X-dash-token"),
                           "content_type": request.get_header("Content-type"),
                           "timeout": timeout})
        if "upload" in self.forced:
            status, payload = self.forced["upload"]
            if status != 200:
                self._error(url, status, payload)
            return _Response(200, payload)
        if request.get_header("X-dash-token") != PUBLISH_SECRET:
            self._error(url, 401, {"ok": False, "error": "unauthorized"})
        body = request.data
        self.uploads.append(body)
        sha = self.sha_override or hashlib.sha256(body).hexdigest()
        return _Response(200, {"ok": True, "sha": sha, "url": f"{SCROOGE}/media/{sha}.jpg",
                               "width": 1080, "height": 1350, "created": True})


def _settings(root: Path, **kw: Any) -> PionirSettings:
    return PionirSettings(
        state_root=root,
        atani_command=("pionir-test-no-such-binary",),
        daedalus_url=None, melete_url=None, galatea_url=None, crew_url=None,
        bryo_status_command=None, nyx_status_command=None, voodoo_status_command=None,
        embed_model=None, evict_to_fit=False, **kw,
    )


def write_ig_token(path: Path, *, token: str = IG_SECRET, age: timedelta = timedelta(days=1),
                   refreshed_at: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"access_token": token, "user_id": USER_ID,
                                "username": "dokaz.industries",
                                "refreshed_at": refreshed_at or iso(NOW - age)}),
                    encoding="utf-8")


class _Case(unittest.TestCase):
    """A hermetic runtime with the Instagram adapter talking to FakeWorld."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.publish_file = self.root / "secrets" / "scrooge-publish-token.txt"
        self.publish_file.parent.mkdir(parents=True)
        self.publish_file.write_text(PUBLISH_SECRET + "\n", encoding="utf-8")
        self.ig_file = self.root / "secrets" / "instagram.json"
        write_ig_token(self.ig_file)
        self.world = FakeWorld()
        self.sleeps: list[float] = []
        self.adapter = self.make_adapter()
        # content_url=None: no default content/instagram adapters; the faked one is used
        runtime = build_runtime(_settings(self.root, content_url=None))
        runtime.register(self.adapter)
        self.app = PionirApp(runtime)

    def make_adapter(self, **kw: Any) -> InstagramAdapter:
        return InstagramAdapter(
            InstagramSettings(graph_url=GRAPH, token_file=self.ig_file,
                              content=ContentSettings(base_url=SCROOGE,
                                                      token_file=self.publish_file), **kw),
            opener=self.world, sleep=self.sleeps.append, clock=lambda: NOW)

    def tearDown(self) -> None:
        self.app.runtime.cortex.close()
        self._tmp.cleanup()

    def execute(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return dict(self.adapter.execute(Task(POST, payload or post())).output)

    def run_post(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Park, approve, wait: the only way a post goes out."""
        out = self.app.run_task(POST, payload or post(), permissions=[POST])
        self.assertEqual(out["status"], "pending_approval")
        res = self.app.approve(out["approval_id"])
        self.assertTrue(self.app.jobs.wait(res["task_id"], 30))
        return self.app.approvals.get(out["approval_id"])


# ---- the gate ---------------------------------------------------------------------
class GateTests(_Case):
    def test_it_is_declared_as_always_approved_and_never_routed(self) -> None:
        caps = {c.name: c for c in self.adapter.manifest.capabilities}
        self.assertEqual(set(caps), {"social.instagram_post", "social.instagram_insights"})
        cap = caps["social.instagram_post"]
        self.assertTrue(cap.requires_approval)
        self.assertIs(cap.risk, RiskLevel.PRIVILEGED)
        self.assertFalse(cap.routable)

    def test_it_parks_every_time_even_with_the_permission(self) -> None:
        for _ in range(3):
            out = self.app.run_task(POST, post(), permissions=[POST])
            self.assertEqual(out["status"], "pending_approval")
        pending = self.app.approvals.pending()
        self.assertEqual(len(pending), 3)
        self.assertIn(GOOD["headline"], pending[0]["summary"])
        self.assertEqual(self.world.calls, [])

    def test_an_approved_post_runs_once_and_goes_out_exactly_as_checked(self) -> None:
        payload = post(card_sha=card_sha(GOOD["headline"], GOOD["points"]))
        out = self.app.run_task(POST, payload, permissions=[POST])
        res = self.app.approve(out["approval_id"])
        self.assertTrue(self.app.jobs.wait(res["task_id"], 30))
        row = self.app.approvals.get(out["approval_id"])
        self.assertEqual(row["status"], "approved", row)
        self.assertFalse(self.app.approve(out["approval_id"])["ok"])     # never twice
        self.assertEqual(self.world.steps(),
                         ["upload", "container", "status", "status", "publish", "permalink"])
        image = render_card(GOOD["headline"], GOOD["points"])
        self.assertEqual(self.world.uploads, [image])                   # the approved card
        upload = self.world.calls[0]
        self.assertEqual(upload["url"], f"{SCROOGE}/dash/content/media")
        self.assertEqual(upload["method"], "POST")
        self.assertEqual(upload["token"], PUBLISH_SECRET)
        self.assertEqual(upload["content_type"], "image/jpeg")
        sha = hashlib.sha256(image).hexdigest()
        container = self.world.calls[1]
        self.assertEqual(container["path"], f"/v25.0/{USER_ID}/media")
        self.assertEqual(container["method"], "POST")
        self.assertEqual(container["form"], {
            "image_url": f"{SCROOGE}/media/{sha}.jpg",
            "caption": full_caption(GOOD["caption"], GOOD["hashtags"]),
            "access_token": IG_SECRET})
        self.assertEqual(self.world.calls[2]["query"],
                         {"fields": "status_code", "access_token": IG_SECRET})
        self.assertEqual(self.world.calls[2]["path"], "/v25.0/C-900")
        self.assertEqual(self.world.calls[4]["form"],
                         {"creation_id": "C-900", "access_token": IG_SECRET})
        self.assertEqual(self.world.calls[4]["path"], f"/v25.0/{USER_ID}/media_publish")
        self.assertEqual(self.world.calls[5]["path"], "/v25.0/M-17900")
        self.assertEqual(self.sleeps, [2.0])                             # polled every 2 s
        self.assertTrue(all(c["timeout"] == 30 for c in self.world.calls))
        self.assertEqual(row["result"]["result"], {
            "ok": True, "media_id": "M-17900",
            "permalink": "https://www.instagram.com/p/DOKAZ1/",
            "image_url": f"{SCROOGE}/media/{sha}.jpg", "card_sha": sha})

    def test_a_denied_post_never_runs(self) -> None:
        aid = self.app.run_task(POST, post(), permissions=[POST])["approval_id"]
        self.assertTrue(self.app.deny(aid)["ok"])
        self.assertFalse(self.app.approve(aid)["ok"])
        self.assertEqual(self.world.calls, [])


class LocalValidationTests(_Case):
    BAD: ClassVar[dict[str, dict[str, Any]]] = {
        "link in caption": {"caption": GOOD["caption"] + " See https://api.dokaz.net/blog."},
        "mention": {"caption": GOOD["caption"] + " Thanks to @someone."},
        "long headline": {"headline": "word " * 30},
        "unknown field": {"image_url": "https://evil.example/x.jpg"},
        "bad card_sha": {"card_sha": "0" * 64},
        "hashtag with #": {"hashtags": ["#webdev"]},
        "no points": {"points": []},
    }
    WHY: ClassVar[dict[str, str]] = {
        "link in caption": "caption: has a link", "mention": "caption: has an @",
        "long headline": "headline: 10-90", "unknown field": "image_url: not a post field",
        "bad card_sha": "card_sha: the card would render differently",
        "hashtag with #": "hashtags: each", "no points": "points: a list",
    }

    def test_check_post_refusals_are_refused_before_parking(self) -> None:
        self.assertEqual(set(self.BAD), set(self.WHY))
        for label, change in self.BAD.items():
            with self.subTest(label):
                payload = post(**change)
                with self.assertRaises(AdapterProtocolError) as refused:
                    self.adapter.validate(Task(POST, payload))
                self.assertIn(self.WHY[label], str(refused.exception))
                with self.assertRaises(AdapterProtocolError):
                    self.adapter.execute(Task(POST, payload))
                out = self.app.run_task(POST, payload, permissions=[POST])
                self.assertEqual(out["status"], "error", out)
                self.assertEqual(out["error"]["type"], "AdapterProtocolError")
        self.assertEqual(self.app.approvals.pending(), [])
        self.assertEqual(self.world.calls, [])

    def test_missing_tokens_are_unavailable_before_parking(self) -> None:
        for missing in (self.ig_file, self.publish_file):
            with self.subTest(missing.name):
                saved = missing.read_bytes()
                missing.unlink()
                try:
                    with self.assertRaisesRegex(AdapterUnavailable, "not configured") as err:
                        self.adapter.validate(Task(POST, post()))
                    self.assertIn(str(missing), str(err.exception))
                    with self.assertRaisesRegex(AdapterUnavailable, "not configured"):
                        self.adapter.status()
                    out = self.execute()
                    self.assertIs(out["ok"], False)
                    self.assertTrue(out["not_configured"])
                    self.assertIn("not configured", out["unavailable"])
                    parked = self.app.run_task(POST, post(), permissions=[POST])
                    self.assertEqual(parked["status"], "error")
                    self.assertEqual(parked["error"]["type"], "AdapterUnavailable")
                finally:
                    missing.write_bytes(saved)
        self.assertIn("setup-instagram.ps1",
                      str(self._unavailable_message(self.ig_file)))
        self.assertEqual(self.app.approvals.pending(), [])
        self.assertEqual(self.world.calls, [])

    def _unavailable_message(self, path: Path) -> str:
        saved = path.read_bytes()
        path.write_text("{not json", encoding="utf-8")        # unusable counts as missing
        try:
            with self.assertRaises(AdapterUnavailable) as err:
                self.adapter.validate(Task(POST, post()))
            return str(err.exception)
        finally:
            path.write_bytes(saved)

    def test_status_is_local_and_names_the_account(self) -> None:
        status = self.adapter.status()
        self.assertEqual(status["account"], "dokaz.industries")
        self.assertEqual(status["token"], "configured")
        self.assertEqual(self.world.calls, [])


# ---- the steps and their answers ---------------------------------------------------
class ScroogeTests(_Case):
    def test_a_scrooge_sha_mismatch_refuses_and_nothing_reaches_instagram(self) -> None:
        self.world.sha_override = "f" * 64
        out = self.execute()
        self.assertIs(out["ok"], False)
        self.assertIn("sha mismatch", out["refused"])
        self.assertEqual(self.world.steps(), ["upload"])

    def test_an_image_url_that_does_not_name_the_card_refuses(self) -> None:
        self.world.forced["upload"] = (200, {"ok": True, "sha": card_sha(GOOD["headline"],
                                                                         GOOD["points"]),
                                             "url": f"{SCROOGE}/media/other.jpg"})
        out = self.execute()
        self.assertIn("does not name the uploaded card", out["refused"])
        self.assertEqual(self.world.steps(), ["upload"])

    def test_scrooge_answers_map_to_refused_or_unavailable(self) -> None:
        cases = {
            400: ("refused", "media: not a jpeg"),
            413: ("refused", "too big"),
            401: ("unavailable", "publish token was rejected"),
            403: ("unavailable", "publish token was rejected"),
            502: ("unavailable", "HTTP 502"),
        }
        for status, (key, text) in cases.items():
            with self.subTest(status):
                self.world.forced["upload"] = (status, {"ok": False,
                                                        "error": "media: not a jpeg"})
                out = self.execute()
                self.assertIs(out["ok"], False)
                self.assertIn(key, out)
                self.assertIn(text, out[key])
        self.world.forced.clear()
        self.world.down.add("api.dokaz.test")
        self.assertIn("unreachable", self.execute()["unavailable"])


class ContainerTests(_Case):
    def test_a_container_that_errors_is_failed_and_never_published(self) -> None:
        for code in ("ERROR", "EXPIRED"):
            with self.subTest(code):
                self.world.calls.clear()
                self.world.statuses = ["IN_PROGRESS", code]
                out = self.execute()
                self.assertIs(out["ok"], False)
                self.assertEqual(out["container_status"], code)
                self.assertIn(code, out["error"])
                self.assertIn("nothing was published", out["error"])
                self.assertNotIn("publish", self.world.steps())

    def test_a_container_still_processing_after_60_s_gives_up_unpublished(self) -> None:
        self.world.statuses = ["IN_PROGRESS"]
        out = self.execute()
        self.assertIn("still processing", out["unavailable"])
        self.assertEqual(sum(self.sleeps), 60.0)
        self.assertTrue(all(s == 2.0 for s in self.sleeps))
        self.assertNotIn("publish", self.world.steps())

    def test_a_failed_permalink_read_is_still_a_published_post(self) -> None:
        self.world.forced["permalink"] = (500, {"error": {"message": "oops", "code": 1}})
        out = self.execute()
        self.assertIs(out["ok"], True)
        self.assertEqual(out["media_id"], "M-17900")
        self.assertIsNone(out["permalink"])
        self.assertIn("permalink_error", out)


class GraphErrorTests(_Case):
    def test_a_rejected_token_is_unavailable_with_the_setup_hint(self) -> None:
        self.world.valid_ig = set()
        out = self.execute()
        self.assertEqual(out["unavailable"], TOKEN_REJECTED)
        self.assertIn(r"tools\setup-instagram.ps1", out["unavailable"])
        self.assertTrue(out["token_rejected"])
        self.assertNotIn("refused", out)
        self.assertEqual(self.world.steps(), ["upload", "container"])

    def test_through_the_gate_a_rejected_token_settles_failed_with_the_hint(self) -> None:
        self.world.valid_ig = set()
        row = self.run_post()
        self.assertEqual(row["status"], "approved_failed")
        self.assertEqual(row["result"]["result"]["unavailable"], TOKEN_REJECTED)

    def test_other_4xx_is_refused_with_graphs_message_scrubbed(self) -> None:
        self.world.forced["container"] = (400, {"error": {
            "message": f"Invalid parameter image_url (token {IG_SECRET})",
            "type": "OAuthException", "code": 100}})
        out = self.execute()
        self.assertEqual(out["refused"], "Invalid parameter image_url (token <redacted>)")
        self.assertEqual(out["graph_code"], 100)
        self.assertEqual(out["step"], "container")

    def test_5xx_network_and_rate_limits_are_unavailable(self) -> None:
        cases = {
            "5xx": ((503, {"error": {"message": "down", "code": 2}}), "HTTP 503"),
            "rate": ((400, {"error": {"message": "limit", "code": 4}}), "asked to wait"),
            "transient": ((400, {"error": {"message": "t", "code": 1, "is_transient": True}}),
                          "asked to wait"),
        }
        for label, (forced, text) in cases.items():
            with self.subTest(label):
                self.world.forced = {"publish": forced}
                out = self.execute()
                self.assertIn(text, out["unavailable"])
                self.assertNotIn("refused", out)
        self.world.forced = {}
        self.world.down.add("graph.instagram.test")
        out = self.execute()
        self.assertIn("unreachable", out["unavailable"])


# ---- the token -------------------------------------------------------------------------
class RefreshTests(_Case):
    def test_a_token_over_7_days_old_is_refreshed_and_the_file_rewritten(self) -> None:
        write_ig_token(self.ig_file, age=timedelta(days=8))
        out = self.execute()
        self.assertIs(out["ok"], True)
        refresh = self.world.calls[0]
        self.assertEqual(refresh["step"], "refresh")
        self.assertEqual(refresh["method"], "GET")
        self.assertEqual(refresh["url"].split("?")[0], "https://graph.instagram.test/"
                                                       "refresh_access_token")
        self.assertEqual(refresh["query"], {"grant_type": "ig_refresh_token",
                                            "access_token": IG_SECRET})
        saved = json.loads(self.ig_file.read_text(encoding="utf-8"))
        self.assertEqual(saved, {"access_token": IG_FRESH, "user_id": USER_ID,
                                 "username": "dokaz.industries", "refreshed_at": iso(NOW)})
        # the post went out with the new token
        self.assertEqual({c["token"] for c in self.world.calls[2:]}, {IG_FRESH})
        self.assertEqual(list(self.ig_file.parent.glob("*.tmp")), [])

    def test_a_fresh_token_is_not_refreshed(self) -> None:
        for age in (timedelta(hours=2), timedelta(days=1), timedelta(days=6, hours=23)):
            with self.subTest(age):
                write_ig_token(self.ig_file, age=age)
                before = self.ig_file.read_bytes()
                self.world.calls.clear()
                self.assertIs(self.execute()["ok"], True)
                self.assertNotIn("refresh", self.world.steps())
                self.assertEqual(self.ig_file.read_bytes(), before)

    def test_a_failed_refresh_does_not_block_the_post(self) -> None:
        write_ig_token(self.ig_file, age=timedelta(days=30))
        before = self.ig_file.read_bytes()
        self.world.forced["refresh"] = (400, {"error": {"message": "cannot refresh",
                                                        "code": 10}})
        with self.assertLogs("pionir.adapters.instagram", logging.WARNING) as logs:
            out = self.execute()
        self.assertIs(out["ok"], True)
        self.assertIn("token refresh failed", "\n".join(logs.output))
        self.assertEqual(self.ig_file.read_bytes(), before)
        self.assertEqual({c["token"] for c in self.world.calls if c["step"] != "upload"},
                         {IG_SECRET})


class SecrecyTests(_Case):
    def test_the_tokens_never_appear_in_results_errors_logs_or_records(self) -> None:
        capture = io.StringIO()
        handler = logging.StreamHandler(capture)
        root = logging.getLogger()
        old = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(handler)
        echo = f"bad token {IG_SECRET} / {PUBLISH_SECRET}"
        try:
            outputs: list[Any] = []
            # every side echoing the secrets back in its error text
            self.world.forced["upload"] = (400, {"ok": False, "error": echo})
            outputs.append(self.execute())
            self.world.forced = {"container": (400, {"error": {"message": echo, "code": 100}})}
            outputs.append(self.execute())
            self.world.forced = {"publish": (503, {"error": {"message": echo, "code": 2}})}
            outputs.append(self.execute())
            self.world.forced = {"refresh": (400, {"error": {"message": echo, "code": 10}})}
            write_ig_token(self.ig_file, age=timedelta(days=9))
            outputs.append(self.execute())                   # refresh fails, post goes out
            fresh_echo = f"bad token {IG_FRESH} / {IG_SECRET} / {PUBLISH_SECRET}"
            self.world.forced = {"container": (400, {"error": {"message": fresh_echo,
                                                               "code": 100}})}
            outputs.append(self.execute())          # refresh works; the next step echoes it
            self.world.forced = {"container": (400, {"error": {
                "message": f"bad token {IG_FRESH} / {PUBLISH_SECRET}", "code": 100}})}
            outputs.append(self.execute())          # the refreshed token, read from the file
            self.world.forced = {}
            self.world.down = {"graph.instagram.test"}       # the URL carries the token
            outputs.append(self.execute())
            self.world.down = set()
            self.world.valid_ig = set()
            outputs.append(self.execute())
            self.world.valid_ig = {IG_SECRET, IG_FRESH}
            outputs.append(self.adapter.status())
            outputs.append(repr(self.adapter.settings))
            outputs.append(repr(read_instagram_token(self.ig_file)))
            outputs.append(self.run_post())                  # through the gate, audited
            with contextlib.redirect_stdout(io.StringIO()) as printed:
                cli.instagram_check(_settings(self.root, instagram_token_file=self.ig_file,
                                              instagram_graph_url=GRAPH),
                                    opener=self.world)
            outputs.append(printed.getvalue())
        finally:
            root.removeHandler(handler)
            root.setLevel(old)
        # the secrets really were used and echoed, so their absence below means something
        tokens = {c["token"] for c in self.world.calls}
        self.assertTrue({IG_SECRET, IG_FRESH, PUBLISH_SECRET} <= tokens, tokens)
        dumped = json.dumps(outputs, default=str)
        self.assertIn("<redacted>", dumped)
        for secret in (IG_SECRET, IG_FRESH, PUBLISH_SECRET):
            self.assertNotIn(secret, dumped)
            self.assertNotIn(secret, capture.getvalue())
            for path in self.root.rglob("*"):   # the audit ledger, jobs, approvals, memory
                if path.is_file() and path not in (self.ig_file, self.publish_file):
                    self.assertNotIn(secret.encode("utf-8"), path.read_bytes(), path)
        self.assertIn("token refresh failed", capture.getvalue())


# ---- settings and wiring -----------------------------------------------------------------
class SettingsTests(unittest.TestCase):
    def test_defaults_env_and_off(self) -> None:
        settings = PionirSettings(state_root=Path("C:/x"))
        self.assertEqual(settings.instagram_graph_url, "https://graph.instagram.com/v25.0")
        self.assertEqual(settings.instagram_token_path,
                         Path.home() / ".pionir" / "secrets" / "instagram.json")
        self.assertEqual(InstagramSettings().graph_url, "https://graph.instagram.com/v25.0")
        self.assertEqual(InstagramSettings().refresh_url,
                         "https://graph.instagram.com/refresh_access_token")
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            os.environ, {"PIONIR_STATE_ROOT": root}
        ):
            os.environ.pop("PIONIR_INSTAGRAM_GRAPH_URL", None)
            os.environ.pop("PIONIR_INSTAGRAM_TOKEN_FILE", None)
            self.assertEqual(PionirSettings.from_environment().instagram_graph_url,
                             "https://graph.instagram.com/v25.0")
            os.environ["PIONIR_INSTAGRAM_GRAPH_URL"] = "https://graph.instagram.com/v26.0"
            self.assertEqual(PionirSettings.from_environment().instagram_graph_url,
                             "https://graph.instagram.com/v26.0")
            os.environ["PIONIR_INSTAGRAM_GRAPH_URL"] = "off"
            self.assertIsNone(PionirSettings.from_environment().instagram_graph_url)
            os.environ["PIONIR_INSTAGRAM_TOKEN_FILE"] = str(Path(root) / "ig.json")
            self.assertEqual(PionirSettings.from_environment().instagram_token_path,
                             Path(root) / "ig.json")

    def test_bootstrap_registers_it_without_touching_the_network(self) -> None:
        cases = (("https://api.dokaz.net", GRAPH, True), (None, GRAPH, False),
                 ("https://api.dokaz.net", None, False))
        for content, graph, present in cases:
            with self.subTest(content=content, graph=graph), \
                    tempfile.TemporaryDirectory() as root:
                runtime = build_runtime(_settings(
                    Path(root), content_url=content, instagram_graph_url=graph,
                    content_token_file=Path(root) / "no-token.txt",
                    instagram_token_file=Path(root) / "no-ig.json"))
                try:
                    self.assertEqual("instagram" in runtime.adapters, present)
                    if present:   # doctor's health check is local: no token, no call
                        with self.assertRaisesRegex(AdapterUnavailable, "not configured"):
                            runtime.adapters["instagram"].status()
                finally:
                    runtime.cortex.close()

    def test_the_token_never_leaves_over_plain_http_to_a_remote_host(self) -> None:
        with self.assertRaises(ValueError):
            InstagramSettings(graph_url="http://graph.instagram.com/v25.0")
        InstagramSettings(graph_url="http://127.0.0.1:9/v25.0")


# ---- instagram-check ------------------------------------------------------------------------
class InstagramCheckTests(_Case):
    def check(self) -> tuple[int, str]:
        settings = _settings(self.root, instagram_token_file=self.ig_file,
                             instagram_graph_url=GRAPH)
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            code = cli.instagram_check(settings, opener=self.world)
        return code, printed.getvalue()

    def test_ok_prints_the_account_and_quota_and_never_posts(self) -> None:
        code, printed = self.check()
        self.assertEqual(code, 0, printed)
        report = json.loads(printed)
        self.assertEqual(report["username"], "dokaz.industries")
        self.assertEqual(report["quota"], {"used": 3, "total": 100, "window_seconds": 86400})
        self.assertEqual(self.world.steps(), ["me", "quota"])
        self.assertEqual(self.world.calls[0]["query"],
                         {"fields": "user_id,username", "access_token": IG_SECRET})
        self.assertEqual(self.world.calls[1]["path"],
                         f"/v25.0/{USER_ID}/content_publishing_limit")
        self.assertNotIn(IG_SECRET, printed)

    def test_not_configured_is_1_and_rejected_is_2(self) -> None:
        self.world.valid_ig = set()
        code, printed = self.check()
        self.assertEqual(code, 2)
        self.assertIn("setup-instagram.ps1", printed)
        self.ig_file.unlink()
        code, printed = self.check()
        self.assertEqual(code, 1)
        self.assertIn("not_configured", printed)

    def test_the_command_is_wired(self) -> None:
        with mock.patch.object(cli, "instagram_check", return_value=0) as check:
            self.assertEqual(cli.main(["instagram-check"]), 0)
        check.assert_called_once_with()


# ---- the Discord card -----------------------------------------------------------------------
def parse_multipart(body: bytes, content_type: str) -> list[dict[str, Any]]:
    message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body)
    assert message.is_multipart(), content_type
    parts = []
    for part in message.iter_parts():
        parts.append({"name": part.get_param("name", header="content-disposition"),
                      "filename": part.get_filename(),
                      "content_type": part.get_content_type(),
                      "data": part.get_payload(decode=True)})
    return parts


class FakeDiscordFiles(FakeDiscord):
    """FakeDiscord that also takes Discord's multipart message-with-files form."""

    def __init__(self) -> None:
        super().__init__()
        self.uploads: list[list[dict[str, Any]]] = []
        self.refuse_files: tuple[int, Any] | None = None

    def __call__(self, request: Any, timeout: float | None = None) -> Any:
        content_type = request.get_header("Content-type") or ""
        if content_type.startswith("multipart/form-data"):
            parts = parse_multipart(request.data, content_type)
            self.uploads.append(parts)
            if self.refuse_files is not None:
                self._error(request.full_url, *self.refuse_files)
            payload = json.loads(parts[0]["data"])
            request.data = json.dumps(payload).encode("utf-8")
            request.remove_header("Content-type")
            request.add_header("Content-type", "application/json")
        return super().__call__(request, timeout)


class DiscordCardTests(_Case):
    def gate(self, fake: FakeDiscord) -> DiscordGate:
        token_file = self.root / "secrets" / "discord-bot-token.txt"
        token_file.write_text(DISCORD_TOKEN, encoding="utf-8")
        return DiscordGate.for_app(
            self.app,
            DiscordGateSettings(state_root=self.root, channel_id=CHANNEL, owner_user_id=OWNER,
                                token_file=token_file, api_base=API, poll_seconds=0.01),
            opener=fake, sleep=lambda _s: None)

    def test_the_card_carries_the_image_and_the_full_caption(self) -> None:
        out = self.app.run_task(POST, post(), permissions=[POST])
        self.assertEqual(out["status"], "pending_approval")
        fake = FakeDiscordFiles()
        self.assertTrue(self.gate(fake).run_once())
        self.assertEqual(len(fake.uploads), 1)
        payload_part, file_part = fake.uploads[0]
        self.assertEqual(payload_part["name"], "payload_json")
        self.assertEqual(payload_part["content_type"], "application/json")
        message = json.loads(payload_part["data"])
        self.assertEqual(message["attachments"], [{"id": 0, "filename": "card.jpg"}])
        self.assertEqual(file_part["name"], "files[0]")
        self.assertEqual(file_part["filename"], "card.jpg")
        self.assertEqual(file_part["content_type"], "image/jpeg")
        self.assertEqual(file_part["data"], render_card(GOOD["headline"], GOOD["points"]))
        head = message["content"]
        self.assertEqual(head.split("\n")[0], INSTAGRAM_LINE)
        self.assertTrue(head.startswith("\U0001f4f8 **POSTS PUBLICLY TO INSTAGRAM** - the image "
                                        "and caption below go live on the Dokaz Instagram if "
                                        "you approve."))
        text = "\n".join(p["content"] for p in fake.posts())
        self.assertIn(_escape(GOOD["headline"]), text)
        for point in GOOD["points"]:
            self.assertIn(_escape(point), text)
        self.assertIn(full_caption(GOOD["caption"], GOOD["hashtags"]), text)
        self.assertIn("`social.instagram_post`", text)
        self.assertEqual(self.world.calls, [])                # showing it posts nothing
        self.assertEqual(len(self.app.approvals.pending()), 1)

    def test_a_card_that_cannot_render_says_so_and_stays_parked(self) -> None:
        aid = self.app.run_task(POST, post(), permissions=[POST])["approval_id"]
        discord_gate._render_cached.cache_clear()
        self.addCleanup(discord_gate._render_cached.cache_clear)
        fake = FakeDiscordFiles()
        with mock.patch.object(discord_gate, "render_card", side_effect=OSError("no font")):
            self.assertTrue(self.gate(fake).run_once())
        self.assertEqual(fake.uploads, [])
        text = "\n".join(p["content"] for p in fake.posts())
        self.assertIn("The card image could not be rendered", text)
        self.assertIn("no font", text)
        self.assertEqual(self.app.approvals.get(aid)["status"], "pending")

    def test_a_refused_attachment_still_shows_the_approval_with_a_warning(self) -> None:
        aid = self.app.run_task(POST, post(), permissions=[POST])["approval_id"]
        fake = FakeDiscordFiles()
        fake.refuse_files = (403, {"message": "Missing Permissions", "code": 50013})
        self.assertTrue(self.gate(fake).run_once())
        posts = [p["content"] for p in fake.posts()]
        self.assertTrue(posts[0].startswith(INSTAGRAM_LINE))
        self.assertIn("could not be attached", posts[1])
        self.assertIn("Attach Files", posts[1])
        self.assertEqual(self.app.approvals.get(aid)["status"], "pending")

    def test_the_multipart_body_is_well_formed(self) -> None:
        image = b"\xff\xd8binary\r\n--not-a-boundary\xff\xd9"
        body, content_type = multipart_body({"content": "hi \u00e9", "allowed_mentions": {}},
                                            [("card.jpg", "image/jpeg", image)])
        self.assertTrue(content_type.startswith("multipart/form-data; boundary=pionir-"))
        boundary = content_type.split("boundary=")[1]
        self.assertTrue(body.startswith(f"--{boundary}\r\n".encode()))
        self.assertTrue(body.endswith(f"--{boundary}--\r\n".encode()))
        parts = parse_multipart(body, content_type)
        self.assertEqual([p["name"] for p in parts], ["payload_json", "files[0]"])
        self.assertEqual(json.loads(parts[0]["data"])["content"], "hi \u00e9")
        self.assertEqual(parts[1]["data"], image)


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
        status, payload = type(self).answer(record)
        out = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
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
    """The adapter's real urllib opener against real loopback HTTP servers for Scrooge and
    the Graph API: the path the owner's approval takes. content.publish's first real run
    crashed here with every fake green."""

    def test_a_post_through_the_real_opener(self) -> None:
        def scrooge(record: dict[str, Any]) -> tuple[int, Any]:
            sha = hashlib.sha256(record["body"]).hexdigest()
            return 200, {"ok": True, "sha": sha, "url": f"https://api.dokaz.net/media/{sha}.jpg",
                         "width": 1080, "height": 1350, "created": True}

        statuses = ["IN_PROGRESS", "FINISHED"]

        def graph(record: dict[str, Any]) -> tuple[int, Any]:
            path = urllib.parse.urlsplit(record["path"]).path
            if path == "/refresh_access_token":
                return 200, {"access_token": IG_FRESH, "token_type": "bearer",
                             "expires_in": 5_184_000}
            if path.endswith("/media_publish"):
                return 200, {"id": "M-1"}
            if path.endswith("/media"):
                return 200, {"id": "C-1"}
            if "status_code" in record["path"]:
                return 200, {"status_code": statuses.pop(0)}
            return 200, {"permalink": "https://www.instagram.com/p/REAL/"}

        scrooge_url, scrooge_seen = serve(self, scrooge)
        graph_url, graph_seen = serve(self, graph)
        with tempfile.TemporaryDirectory() as tmp:
            publish = Path(tmp) / "publish.txt"
            publish.write_text(PUBLISH_SECRET, encoding="utf-8")
            ig = Path(tmp) / "instagram.json"
            write_ig_token(ig, age=timedelta(days=10))
            adapter = InstagramAdapter(InstagramSettings(
                graph_url=f"{graph_url}/v25.0", token_file=ig,
                content=ContentSettings(base_url=scrooge_url, token_file=publish)),
                sleep=lambda _s: None, clock=lambda: NOW)       # the default, real opener
            out = dict(adapter.execute(Task(POST, post())).output)
            self.assertEqual(json.loads(ig.read_text(encoding="utf-8"))["access_token"],
                             IG_FRESH)
        self.assertIs(out["ok"], True, out)
        self.assertEqual(out["permalink"], "https://www.instagram.com/p/REAL/")
        (upload,) = scrooge_seen
        self.assertEqual((upload["method"], upload["path"]), ("POST", "/dash/content/media"))
        self.assertEqual(upload["headers"]["x-dash-token"], PUBLISH_SECRET)
        self.assertEqual(upload["headers"]["content-type"], "image/jpeg")
        self.assertEqual(upload["body"], render_card(GOOD["headline"], GOOD["points"]))
        self.assertEqual([(r["method"], urllib.parse.urlsplit(r["path"]).path)
                          for r in graph_seen],
                         [("GET", "/refresh_access_token"), ("POST", f"/v25.0/{USER_ID}/media"),
                          ("GET", "/v25.0/C-1"), ("GET", "/v25.0/C-1"),
                          ("POST", f"/v25.0/{USER_ID}/media_publish"), ("GET", "/v25.0/M-1")])
        form = dict(urllib.parse.parse_qsl(graph_seen[1]["body"].decode()))
        self.assertEqual(form["caption"], full_caption(GOOD["caption"], GOOD["hashtags"]))
        self.assertEqual(form["access_token"], IG_FRESH)
        self.assertEqual(graph_seen[1]["headers"]["content-type"],
                         "application/x-www-form-urlencoded")

    def test_the_discord_image_through_the_real_opener(self) -> None:
        def discord(record: dict[str, Any]) -> tuple[int, Any]:
            return 200, {"id": "1300000000000000001", "channel_id": CHANNEL}

        base, seen = serve(self, discord)
        rest = discord_gate.DiscordRest(DISCORD_TOKEN, api_base=base)    # real urlopen
        image = render_card(GOOD["headline"], GOOD["points"])
        rest.call("POST", f"/channels/{CHANNEL}/messages", {"content": "look"},
                  files=[("card.jpg", "image/jpeg", image)])
        (request,) = seen
        parts = parse_multipart(request["body"], request["headers"]["content-type"])
        self.assertEqual(json.loads(parts[0]["data"])["content"], "look")
        self.assertEqual(parts[1]["data"], image)
        self.assertEqual(request["headers"]["authorization"], f"Bot {DISCORD_TOKEN}")


if __name__ == "__main__":
    unittest.main()
