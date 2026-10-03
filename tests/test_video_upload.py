"""The YouTube uploader against a FAKE transport only: nothing here can reach Google.

Every rule in ``pionir.video.upload`` has a test that fails when the rule is reverted:
private only, approval, live niche, secrets, resumable sessions, backoff, one token refresh,
quota stop, no secret in any text, no transport built before a gate passes.
"""
from __future__ import annotations

import json
import socket
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from pionir.adapters import video as video_adapter
from pionir.adapters.video import UPLOAD, VideoAdapter, VideoSettings
from pionir.batching import OWNER_APPROVED_GRANT
from pionir.contracts import Task
from pionir.video import upload as up
from pionir.video.disclosure import DISCLOSURE
from pionir.video.package import load_package, sha256_file
from video_support import make_niche, niche_entry

CLIENT_SECRET = "zz-client-secret-value"
REFRESH_TOKEN = "zz-refresh-token-value"
CLIENT_ID = "zz-client-id.apps.googleusercontent.com"
ACCESS = "zz-access-token-value"
SESSION = "https://upload.example.test/session/abc123"
VIDEO_ID = "dQw4w9WgXcQ"
SECRETS = (CLIENT_SECRET, REFRESH_TOKEN, ACCESS)


def ok_token(access: str = ACCESS) -> up.TransportResponse:
    return up.TransportResponse(200, {}, json.dumps({"access_token": access}).encode())


def started(url: str = SESSION) -> up.TransportResponse:
    return up.TransportResponse(200, {"Location": url}, b"")


def done(video_id: str = VIDEO_ID, privacy: str = "private") -> up.TransportResponse:
    return up.TransportResponse(
        200, {}, json.dumps({"id": video_id, "status": {"privacyStatus": privacy}}).encode())


def resume(last_byte: int | None) -> up.TransportResponse:
    headers = {} if last_byte is None else {"Range": f"bytes=0-{last_byte}"}
    return up.TransportResponse(308, headers, b"")


def error(status: int, reason: str = "", body_extra: str = "") -> up.TransportResponse:
    doc = {"error": {"errors": [{"reason": reason}] if reason else [], "message": body_extra}}
    return up.TransportResponse(status, {}, json.dumps(doc).encode())


class FakeTransport:
    """Answers in the scripted order; a callable entry sees the request and decides."""

    def __init__(self, *responses) -> None:
        self.script = list(responses)
        self.log: list[dict] = []

    def request(self, method, url, *, headers, body, timeout=60.0):
        self.log.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        if not self.script:
            raise AssertionError(f"unscripted request: {method} {url}")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item(method, url, headers, body) if callable(item) else item

    def urls(self) -> list[str]:
        return [entry["url"] for entry in self.log]


class Sleeper:
    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


class UploadCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.video_dir = self.root / "video"
        self.secrets = self.root / "secrets"
        self.secrets.mkdir()
        self.sleep = Sleeper()
        self.niche = make_niche()
        self.package = self.make_package("harbor-story")
        self.write_secrets()
        # Whatever a test does, nothing may reach a real network.
        for target in (mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network")),
                       mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("network")),
                       mock.patch.object(up, "UrllibTransport",
                                         side_effect=AssertionError("real transport built"))):
            target.start()
            self.addCleanup(target.stop)

    def write_secrets(self, *, client: bool = True, token: bool = True) -> None:
        if client:
            (self.secrets / up.CLIENT_FILE).write_bytes(json.dumps(
                {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}).encode())
        if token:
            (self.secrets / up.TOKEN_FILE).write_bytes(json.dumps(
                {"refresh_token": REFRESH_TOKEN}).encode())

    def make_package(self, video_id: str, *, size: int = 1000, **manifest_overrides):
        folder = self.video_dir / "queue" / video_id
        folder.mkdir(parents=True)
        (folder / "video.mp4").write_bytes(bytes(i % 251 for i in range(size)))
        (folder / "captions.srt").write_bytes(b"1\n00:00:00,000 --> 00:00:02,000\nHello.\n")
        (folder / "thumbnail.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
        (folder / "script.json").write_bytes(b"{}")
        manifest = {
            "id": video_id, "status": "staged", "niche": "test-harbor", "title": "The old harbor",
            "series": "Harbor stories", "description": "A short history.\n\n" + DISCLOSURE,
            "duration_seconds": 600.0, "video_sha256": sha256_file(folder / "video.mp4"),
            "sources": [{"id": "p1", "title": "Harbor", "credit": "Wikipedia, CC BY-SA 4.0",
                         "url": "https://en.wikipedia.org/wiki/Harbor"}],
            "uploaded": None, "created_at": "2026-10-01T00:00:00+00:00", "example": False}
        manifest.update(manifest_overrides)
        (folder / "manifest.json").write_bytes(json.dumps(manifest).encode())
        return load_package(self.video_dir, video_id)

    def reload(self, video_id: str = "harbor-story"):
        return load_package(self.video_dir, video_id)

    def run_upload(self, transport, *, approved=True, niche=None, package=None):
        return up.upload_package(package or self.package, niche or self.niche, approved=approved,
                                 secrets_dir=self.secrets, transport=transport, sleep=self.sleep,
                                 clock=lambda: "2026-10-03T12:00:00+00:00")

    def happy(self) -> FakeTransport:
        return FakeTransport(ok_token(), started(), done(), up.TransportResponse(200))

    def assert_no_secret(self, text: str) -> None:
        for secret in SECRETS:
            self.assertNotIn(secret, text)


class GateTests(UploadCase):
    def refused(self, transport=None, **kwargs):
        transport = transport or FakeTransport()
        with self.assertRaises(up.UploadRefused) as caught:
            self.run_upload(transport, **kwargs)
        self.assertEqual(transport.log, [], "a refused upload must not touch the transport")
        return str(caught.exception)

    def test_unapproved_is_refused_before_any_request(self) -> None:
        self.assertIn("not approved", self.refused(approved=False))
        self.assertIsNone(self.reload().manifest["uploaded"])

    def test_niche_not_live_is_refused(self) -> None:
        self.assertIn("not live", self.refused(niche=make_niche(live=False)))

    def test_example_niche_is_refused(self) -> None:
        self.assertIn("not live", self.refused(niche=make_niche(example=True, live=False)))

    def test_wrong_niche_is_refused(self) -> None:
        self.assertIn("different niche", self.refused(niche=make_niche(id="other-niche")))

    def test_package_that_does_not_verify_is_refused(self) -> None:
        for override, fragment in (({"status": "draft"}, "status"),
                                   ({"description": "no disclosure here"}, "disclosure"),
                                   ({"sources": []}, "sources"),
                                   ({"example": True}, "example")):
            with self.subTest(override=override):
                package = self.make_package("bad-" + next(iter(override)), **override)
                self.assertIn(fragment, self.refused(package=package))

    def test_changed_video_is_refused(self) -> None:
        (self.package.dir / "video.mp4").write_bytes(b"tampered after the card was shown")
        self.assertIn("changed", self.refused())

    def test_already_uploaded_is_refused(self) -> None:
        package = self.make_package("done-already", uploaded={"youtube_id": VIDEO_ID})
        self.assertIn("already uploaded", self.refused(package=package))

    def test_missing_secrets_are_refused_naming_the_setup_doc(self) -> None:
        for client, token in ((False, True), (True, False), (False, False)):
            with self.subTest(client=client, token=token):
                for f in self.secrets.iterdir():
                    f.unlink()
                self.write_secrets(client=client, token=token)
                self.assertIn("VIDEO_SETUP_FOR_IAN", self.refused())

    def test_no_real_transport_is_built_when_a_gate_refuses(self) -> None:
        # UrllibTransport is patched to raise in setUp; a refusal must come first.
        with self.assertRaises(up.UploadRefused):
            up.upload_package(self.package, self.niche, approved=False, secrets_dir=self.secrets)

    def test_credentials_files_without_keys_are_refused_without_values(self) -> None:
        (self.secrets / up.CLIENT_FILE).write_bytes(json.dumps({"client_id": CLIENT_ID}).encode())
        message = self.refused()
        self.assertIn("client_secret", message)
        self.assert_no_secret(message)
        self.assertNotIn(CLIENT_ID, message)

    def test_unreadable_credentials_file_is_refused_without_its_content(self) -> None:
        (self.secrets / up.TOKEN_FILE).write_bytes(b"{not json " + REFRESH_TOKEN.encode())
        message = self.refused()
        self.assertIn("unreadable", message)
        self.assert_no_secret(message)

    def test_credentials_repr_hides_the_values(self) -> None:
        creds = up.load_credentials(self.secrets)
        self.assert_no_secret(repr(creds))


class HappyPathTests(UploadCase):
    def test_uploads_private_and_records_it(self) -> None:
        transport = self.happy()
        record = self.run_upload(transport)
        self.assertEqual(record["youtube_id"], VIDEO_ID)
        self.assertEqual(record["privacy"], "private")
        self.assertTrue(record["thumbnail_set"])
        manifest = self.reload().manifest
        self.assertEqual(manifest["uploaded"]["privacy"], "private")
        self.assertEqual(manifest["uploaded"]["youtube_id"], VIDEO_ID)
        self.assertEqual(manifest["uploaded"]["sha256"], manifest["video_sha256"])
        self.assertFalse((self.package.dir / up.STATE_FILE).exists())

    def test_start_request_declares_private_synthetic_and_not_for_kids(self) -> None:
        transport = self.happy()
        self.run_upload(transport)
        start = transport.log[1]
        self.assertEqual(start["method"], "POST")
        self.assertEqual(start["url"], up.INSERT_URL)
        body = json.loads(start["body"])
        self.assertEqual(body["status"]["privacyStatus"], "private")
        self.assertIs(body["status"]["containsSyntheticMedia"], True)
        self.assertIs(body["status"]["selfDeclaredMadeForKids"], False)
        self.assertTrue(body["snippet"]["description"].endswith(DISCLOSURE))
        self.assertEqual(start["headers"]["X-Upload-Content-Length"], "1000")

    def test_no_request_ever_asks_for_a_public_video(self) -> None:
        transport = self.happy()
        self.run_upload(transport)
        for entry in transport.log:
            self.assertNotIn(b"public", entry["body"] or b"")

    def test_token_comes_from_the_refresh_token_and_is_sent_as_bearer(self) -> None:
        transport = self.happy()
        self.run_upload(transport)
        refresh = transport.log[0]
        self.assertEqual(refresh["url"], up.TOKEN_URL)
        self.assertIn(b"grant_type=refresh_token", refresh["body"])
        self.assertIn(REFRESH_TOKEN.encode(), refresh["body"])
        for entry in transport.log[1:]:
            self.assertEqual(entry["headers"]["Authorization"], f"Bearer {ACCESS}")

    def test_single_chunk_for_a_small_video(self) -> None:
        transport = self.happy()
        self.run_upload(transport)
        put = transport.log[2]
        self.assertEqual(put["method"], "PUT")
        self.assertEqual(put["url"], SESSION)
        self.assertEqual(put["headers"]["Content-Range"], "bytes 0-999/1000")
        self.assertEqual(len(put["body"]), 1000)

    def test_thumbnail_is_posted_for_the_returned_id(self) -> None:
        transport = self.happy()
        self.run_upload(transport)
        self.assertEqual(transport.log[3]["url"], up.THUMB_URL + VIDEO_ID)

    def test_thumbnail_failure_does_not_fail_the_upload(self) -> None:
        transport = FakeTransport(ok_token(), started(), done(), error(400, "invalidImage"))
        record = self.run_upload(transport)
        self.assertFalse(record["thumbnail_set"])
        self.assertEqual(self.reload().manifest["uploaded"]["youtube_id"], VIDEO_ID)

    def test_thumbnail_transport_error_does_not_fail_the_upload(self) -> None:
        transport = FakeTransport(ok_token(), started(), done(), up.TransportError("x"))
        self.assertFalse(self.run_upload(transport)["thumbnail_set"])

    def test_oversize_thumbnail_is_skipped(self) -> None:
        (self.package.dir / "thumbnail.png").write_bytes(b"x" * (up.MAX_THUMBNAIL + 1))
        transport = FakeTransport(ok_token(), started(), done())
        self.assertFalse(self.run_upload(transport)["thumbnail_set"])
        self.assertEqual(len(transport.log), 3)

    def test_second_upload_of_the_same_package_is_refused(self) -> None:
        self.run_upload(self.happy())
        with self.assertRaises(up.UploadRefused):
            self.run_upload(FakeTransport(), package=self.reload())

    def test_answer_without_a_video_id_is_an_error_and_records_nothing(self) -> None:
        transport = FakeTransport(ok_token(), started(),
                                  up.TransportResponse(200, {}, b'{"id": "../../etc"}'))
        with self.assertRaises(up.UploadError):
            self.run_upload(transport)
        self.assertIsNone(self.reload().manifest["uploaded"])

    def test_title_with_angle_brackets_is_stripped_before_sending(self) -> None:
        package = self.make_package("angle", title="<b>Harbor</b> story")
        transport = self.happy()
        self.run_upload(transport, package=package)
        self.assertEqual(json.loads(transport.log[1]["body"])["snippet"]["title"], "bHarbor/b story")

    def test_too_long_title_is_refused_before_any_request(self) -> None:
        package = self.make_package("long-title", title="x" * 101)
        transport = FakeTransport()
        with self.assertRaises(up.UploadRefused):
            self.run_upload(transport, package=package)
        self.assertEqual(transport.urls(), [])
        self.assertFalse((package.dir / up.STATE_FILE).exists())


class ChunkTests(UploadCase):
    def test_chunks_follow_content_range_and_resume_offsets(self) -> None:
        package = self.make_package("big-one", size=2500)
        transport = FakeTransport(ok_token(), started(), resume(999), resume(1999), done(),
                                  up.TransportResponse(200))
        with mock.patch.object(up, "CHUNK", 1000):
            self.run_upload(transport, package=package)
        ranges = [e["headers"]["Content-Range"] for e in transport.log if e["url"] == SESSION]
        self.assertEqual(ranges, ["bytes 0-999/2500", "bytes 1000-1999/2500", "bytes 2000-2499/2500"])
        self.assertEqual([len(e["body"]) for e in transport.log if e["url"] == SESSION],
                         [1000, 1000, 500])

    def test_server_acknowledging_less_than_sent_is_honoured(self) -> None:
        package = self.make_package("partial", size=2000)
        transport = FakeTransport(ok_token(), started(), resume(499), done(), up.TransportResponse(200))
        with mock.patch.object(up, "CHUNK", 1000):
            self.run_upload(transport, package=package)
        ranges = [e["headers"]["Content-Range"] for e in transport.log if e["url"] == SESSION]
        self.assertEqual(ranges, ["bytes 0-999/2000", "bytes 500-1499/2000"])

    def test_chunk_size_is_a_multiple_of_256k(self) -> None:
        self.assertEqual(up.CHUNK % (256 * 1024), 0)


class RetryTests(UploadCase):
    def test_5xx_on_a_chunk_backs_off_probes_and_resumes(self) -> None:
        transport = FakeTransport(ok_token(), started(), error(503), resume(None), done(),
                                  up.TransportResponse(200))
        self.run_upload(transport)
        self.assertEqual(self.sleep.delays, [2.0])
        probe = transport.log[3]
        self.assertEqual(probe["headers"]["Content-Range"], "bytes */1000")
        self.assertEqual(probe["body"], b"")

    def test_probe_that_finds_the_upload_complete_ends_it(self) -> None:
        transport = FakeTransport(ok_token(), started(), up.TransportError("drop"), done(),
                                  up.TransportResponse(200))
        record = self.run_upload(transport)
        self.assertEqual(record["youtube_id"], VIDEO_ID)

    def test_backoff_doubles_and_is_capped(self) -> None:
        responses = [ok_token(), started()] + [error(500), error(500)] * 6
        transport = FakeTransport(*responses)
        with self.assertRaises(up.UploadError) as caught:
            self.run_upload(transport)
        self.assertIn("session is kept", str(caught.exception))
        self.assertEqual(self.sleep.delays, [2.0, 4.0, 8.0, 16.0, 32.0])
        self.assertTrue((self.package.dir / up.STATE_FILE).exists())
        self.assertIsNone(self.reload().manifest["uploaded"])

    def test_backoff_never_exceeds_the_cap(self) -> None:
        session = up._Session(FakeTransport(), up.load_credentials(self.secrets), self.sleep)
        session.backoff(20)
        self.assertEqual(self.sleep.delays, [up.MAX_BACKOFF])

    def test_5xx_on_start_is_retried(self) -> None:
        transport = FakeTransport(ok_token(), error(500), up.TransportError("drop"), started(),
                                  done(), up.TransportResponse(200))
        self.run_upload(transport)
        self.assertEqual(self.sleep.delays, [1.0, 2.0])

    def test_start_that_never_answers_gives_up(self) -> None:
        transport = FakeTransport(ok_token(), *[error(500)] * up.MAX_ATTEMPTS)
        with self.assertRaises(up.UploadError):
            self.run_upload(transport)

    def test_4xx_on_start_is_not_retried(self) -> None:
        transport = FakeTransport(ok_token(), error(400, "badRequest"))
        with self.assertRaises(up.UploadError) as caught:
            self.run_upload(transport)
        self.assertIn("400", str(caught.exception))
        self.assertEqual(self.sleep.delays, [])

    def test_4xx_on_a_chunk_is_not_retried(self) -> None:
        transport = FakeTransport(ok_token(), started(), error(400, "invalidVideo"))
        with self.assertRaises(up.UploadError):
            self.run_upload(transport)
        self.assertEqual(self.sleep.delays, [])


class TokenTests(UploadCase):
    def test_401_refreshes_the_token_once_and_retries(self) -> None:
        transport = FakeTransport(ok_token("first"), up.TransportResponse(401, {}, b"{}"),
                                  ok_token("second"), started(), done(), up.TransportResponse(200))
        self.run_upload(transport)
        auth = [e["headers"].get("Authorization") for e in transport.log
                if e["url"] != up.TOKEN_URL]
        self.assertEqual(auth[0], "Bearer first")
        self.assertEqual(auth[1], "Bearer second")
        self.assertEqual(transport.urls().count(up.TOKEN_URL), 2)

    def test_a_second_401_is_returned_not_looped(self) -> None:
        transport = FakeTransport(ok_token("a"), up.TransportResponse(401), ok_token("b"),
                                  up.TransportResponse(401))
        with self.assertRaises(up.UploadError):
            self.run_upload(transport)
        self.assertEqual(transport.urls().count(up.TOKEN_URL), 2)

    def test_refresh_failure_names_the_code_and_leaks_nothing(self) -> None:
        body = json.dumps({"error": "invalid_grant", "error_description":
                           f"Bad client {CLIENT_SECRET} {REFRESH_TOKEN}"}).encode()
        transport = FakeTransport(up.TransportResponse(400, {}, body))
        with self.assertRaises(up.UploadError) as caught:
            self.run_upload(transport)
        message = str(caught.exception)
        self.assertIn("invalid_grant", message)
        self.assertIn("video-youtube-consent", message)
        self.assert_no_secret(message)
        self.assertNotIn("Bad client", message)

    def test_refresh_answer_without_access_token_is_an_error(self) -> None:
        transport = FakeTransport(up.TransportResponse(200, {}, b'{"nope": 1}'))
        with self.assertRaises(up.UploadError):
            self.run_upload(transport)

    def test_refresh_with_no_answer_is_an_error(self) -> None:
        transport = FakeTransport(up.TransportError("TimeoutError"))
        with self.assertRaises(up.UploadError) as caught:
            self.run_upload(transport)
        self.assert_no_secret(str(caught.exception))

    def test_secrets_never_appear_in_the_result_or_the_manifest(self) -> None:
        record = self.run_upload(self.happy())
        self.assert_no_secret(json.dumps(record))
        self.assert_no_secret((self.package.dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertNotIn(CLIENT_ID, json.dumps(record))

    def test_state_file_holds_no_token_or_secret(self) -> None:
        transport = FakeTransport(ok_token(), started(), error(400, "invalidVideo"))
        with self.assertRaises(up.UploadError):
            self.run_upload(transport)
        self.assert_no_secret((self.package.dir / up.STATE_FILE).read_text(encoding="utf-8"))

    def test_error_text_from_google_bodies_is_not_echoed(self) -> None:
        transport = FakeTransport(ok_token(), started(),
                                  error(400, "invalidVideo", body_extra=f"leak {ACCESS} {CLIENT_SECRET}"))
        with self.assertRaises(up.UploadError) as caught:
            self.run_upload(transport)
        self.assert_no_secret(str(caught.exception))
        self.assertNotIn("leak", str(caught.exception))


class QuotaTests(UploadCase):
    def test_quota_on_start_raises_quota_exhausted(self) -> None:
        transport = FakeTransport(ok_token(), error(403, "quotaExceeded"))
        with self.assertRaises(up.QuotaExhausted):
            self.run_upload(transport)
        self.assertEqual(self.sleep.delays, [])
        self.assertIsNone(self.reload().manifest["uploaded"])

    def test_quota_mid_upload_keeps_the_session_for_tomorrow(self) -> None:
        transport = FakeTransport(ok_token(), started(), error(403, "quotaExceeded"))
        with self.assertRaises(up.QuotaExhausted) as caught:
            self.run_upload(transport)
        self.assertIn("midnight Pacific", str(caught.exception))
        state = json.loads((self.package.dir / up.STATE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(state["session_url"], SESSION)

    def test_quota_is_not_retried_with_backoff(self) -> None:
        transport = FakeTransport(ok_token(), started(), error(429, "rateLimitExceeded"))
        with self.assertRaises(up.QuotaExhausted):
            self.run_upload(transport)
        self.assertEqual(self.sleep.delays, [])

    def test_other_403_is_a_plain_error_not_quota(self) -> None:
        transport = FakeTransport(ok_token(), started(), error(403, "forbidden"))
        with self.assertRaises(up.UploadError) as caught:
            self.run_upload(transport)
        self.assertNotIsInstance(caught.exception, up.QuotaExhausted)
        self.assertIn("forbidden", str(caught.exception))


class ResumeTests(UploadCase):
    def seed_state(self, package=None, url: str = SESSION) -> None:
        package = package or self.package
        (package.dir / up.STATE_FILE).write_bytes(json.dumps(
            {"session_url": url, "total": 1000,
             "sha256": package.manifest["video_sha256"]}).encode())

    def test_persisted_session_is_resumed_with_no_second_start(self) -> None:
        self.seed_state()
        transport = FakeTransport(ok_token(), resume(499), done(), up.TransportResponse(200))
        self.run_upload(transport)
        self.assertNotIn(up.INSERT_URL, transport.urls())
        ranges = [e["headers"]["Content-Range"] for e in transport.log if e["url"] == SESSION]
        self.assertEqual(ranges, ["bytes */1000", "bytes 500-999/1000"])

    def test_resumed_session_already_complete_is_not_resent(self) -> None:
        self.seed_state()
        transport = FakeTransport(ok_token(), done(), up.TransportResponse(200))
        record = self.run_upload(transport)
        self.assertEqual(record["youtube_id"], VIDEO_ID)
        self.assertEqual([e["method"] for e in transport.log if e["url"] == SESSION], ["PUT"])
        self.assertEqual(len([e for e in transport.log if e["body"] and e["url"] == SESSION]), 0)

    def test_crash_then_rerun_makes_one_session_not_two(self) -> None:
        crash = FakeTransport(ok_token(), started(), up.TransportError("a"), up.TransportError("b"),
                              up.TransportError("c"), up.TransportError("d"), up.TransportError("e"),
                              up.TransportError("f"), up.TransportError("g"), up.TransportError("h"),
                              up.TransportError("i"), up.TransportError("j"), up.TransportError("k"))
        with self.assertRaises(up.UploadError):
            self.run_upload(crash)
        rerun = FakeTransport(ok_token(), resume(None), done(), up.TransportResponse(200))
        self.run_upload(rerun)
        self.assertEqual(crash.urls().count(up.INSERT_URL), 1)
        self.assertEqual(rerun.urls().count(up.INSERT_URL), 0)

    def test_expired_session_restarts_once(self) -> None:
        self.seed_state()
        fresh = "https://upload.example.test/session/fresh"
        transport = FakeTransport(ok_token(), up.TransportResponse(404), started(fresh), done(),
                                  up.TransportResponse(200))
        self.run_upload(transport)
        self.assertEqual(transport.urls().count(up.INSERT_URL), 1)
        self.assertIn(fresh, transport.urls())

    def test_gone_session_mid_upload_restarts_once(self) -> None:
        self.seed_state()
        fresh = "https://upload.example.test/session/fresh"
        transport = FakeTransport(ok_token(), resume(None), up.TransportResponse(410), started(fresh),
                                  done(), up.TransportResponse(200))
        self.run_upload(transport)
        self.assertEqual(transport.urls().count(up.INSERT_URL), 1)

    def test_a_session_that_expires_twice_stops(self) -> None:
        self.seed_state()
        transport = FakeTransport(ok_token(), up.TransportResponse(404), started(),
                                  up.TransportResponse(404))
        with self.assertRaises(up.UploadError):
            self.run_upload(transport)

    def test_state_for_a_different_video_is_ignored(self) -> None:
        (self.package.dir / up.STATE_FILE).write_bytes(json.dumps(
            {"session_url": SESSION, "total": 1000, "sha256": "0" * 64}).encode())
        transport = self.happy()
        self.run_upload(transport)
        self.assertIn(up.INSERT_URL, transport.urls())

    def test_state_with_a_non_https_url_is_ignored(self) -> None:
        self.seed_state(url="http://evil.example.test/session")
        transport = self.happy()
        self.run_upload(transport)
        self.assertNotIn("http://evil.example.test/session", transport.urls())

    def test_garbled_state_is_ignored(self) -> None:
        (self.package.dir / up.STATE_FILE).write_bytes(b"{nope")
        self.run_upload(self.happy())


class MarkPublicTests(UploadCase):
    def test_mark_public_flips_the_record_and_sends_nothing(self) -> None:
        self.run_upload(self.happy())
        record = up.mark_public(self.video_dir, "harbor-story", clock=lambda: "2026-10-04T00:00:00+00:00")
        self.assertEqual(record["privacy"], "public")
        self.assertEqual(record["published_at"], "2026-10-04T00:00:00+00:00")
        self.assertEqual(self.reload().manifest["uploaded"]["privacy"], "public")
        self.assertEqual(self.reload().manifest["uploaded"]["youtube_id"], VIDEO_ID)

    def test_mark_public_twice_is_refused(self) -> None:
        self.run_upload(self.happy())
        up.mark_public(self.video_dir, "harbor-story")
        with self.assertRaises(up.UploadRefused):
            up.mark_public(self.video_dir, "harbor-story")

    def test_mark_public_without_an_upload_is_refused(self) -> None:
        with self.assertRaises(up.UploadRefused):
            up.mark_public(self.video_dir, "harbor-story")

    def test_mark_public_with_a_bad_recorded_id_is_refused(self) -> None:
        self.make_package("bad-id", uploaded={"youtube_id": "../x", "privacy": "private"})
        with self.assertRaises(up.UploadRefused):
            up.mark_public(self.video_dir, "bad-id")


class TransportTests(UploadCase):
    def test_real_transport_does_not_follow_a_308(self) -> None:
        handler = up._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 308, "", {}, "http://x"))

    def test_no_google_request_is_possible_in_this_file(self) -> None:
        # setUp patches socket.connect and urlopen to raise; a fake-only run must succeed.
        self.run_upload(self.happy())


class AdapterTests(UploadCase):
    def setUp(self) -> None:
        super().setUp()
        self.niches = self.root / "niches.json"
        self.niches.write_bytes(json.dumps({"niches": [niche_entry()]}).encode())

    def adapter(self, transport, niches: Path | None = None) -> VideoAdapter:
        settings = VideoSettings(video_dir=self.video_dir, niches_file=niches or self.niches,
                                 secrets_dir=self.secrets)
        return VideoAdapter(settings, transport=transport, sleep=self.sleep)

    def payload(self, package=None) -> dict:
        m = (package or self.package).manifest
        return {"video_id": m["id"], "title": m["title"], "series": m["series"],
                "duration_seconds": m["duration_seconds"], "sha256": m["video_sha256"]}

    def task(self, *, approved: bool, payload=None) -> Task:
        grants = frozenset({OWNER_APPROVED_GRANT}) if approved else frozenset()
        return Task(UPLOAD, payload or self.payload(), granted_permissions=grants)

    def test_capability_is_privileged_gated_and_not_routable(self) -> None:
        (cap,) = self.adapter(FakeTransport()).manifest.capabilities
        self.assertEqual(cap.name, UPLOAD)
        self.assertTrue(cap.requires_approval)
        self.assertFalse(cap.routable)

    def test_approved_task_uploads_private(self) -> None:
        transport = self.happy()
        result = self.adapter(transport).execute(self.task(approved=True))
        self.assertTrue(result.output["ok"])
        self.assertTrue(result.output["uploaded"])
        self.assertEqual(result.output["privacy"], "private")
        self.assertEqual(result.output["youtube_id"], VIDEO_ID)
        self.assertEqual(self.reload().manifest["uploaded"]["privacy"], "private")

    def test_holding_the_permission_is_not_approval(self) -> None:
        transport = FakeTransport()
        task = Task(UPLOAD, self.payload(), granted_permissions=frozenset({UPLOAD}))
        result = self.adapter(transport).execute(task)
        self.assertFalse(result.output["ok"])
        self.assertFalse(result.output["uploaded"])
        self.assertEqual(transport.log, [])
        self.assertIsNone(self.reload().manifest["uploaded"])

    def test_unapproved_task_refuses_and_sends_nothing(self) -> None:
        transport = FakeTransport()
        result = self.adapter(transport).execute(self.task(approved=False))
        self.assertFalse(result.output["uploaded"])
        self.assertIn("not approved", result.output["error"])
        self.assertEqual(transport.log, [])

    def test_not_live_niche_stays_disabled_even_when_approved(self) -> None:
        self.niches.write_bytes(json.dumps({"niches": [niche_entry(live=False)]}).encode())
        transport = FakeTransport()
        result = self.adapter(transport).execute(self.task(approved=True))
        self.assertFalse(result.output["uploaded"])
        self.assertIn("not live", result.output["error"])
        self.assertEqual(transport.log, [])

    def test_missing_secrets_stay_disabled_even_when_approved(self) -> None:
        for f in self.secrets.iterdir():
            f.unlink()
        transport = FakeTransport()
        result = self.adapter(transport).execute(self.task(approved=True))
        self.assertFalse(result.output["uploaded"])
        self.assertEqual(transport.log, [])

    def test_unknown_niche_is_refused(self) -> None:
        self.niches.write_bytes(json.dumps({"niches": [niche_entry(id="someone-else")]}).encode())
        result = self.adapter(FakeTransport()).execute(self.task(approved=True))
        self.assertFalse(result.output["uploaded"])
        self.assertIn("niche", result.output["error"])

    def test_quota_is_reported_not_raised(self) -> None:
        transport = FakeTransport(ok_token(), error(403, "quotaExceeded"))
        result = self.adapter(transport).execute(self.task(approved=True))
        self.assertTrue(result.output["quota_exhausted"])
        self.assertFalse(result.output["uploaded"])

    def test_card_that_disagrees_with_the_package_is_refused(self) -> None:
        for key, value in (("title", "Another title"), ("sha256", "0" * 64),
                           ("duration_seconds", 1.0), ("series", "Other")):
            with self.subTest(key=key):
                payload = {**self.payload(), key: value}
                with self.assertRaises(video_adapter.AdapterProtocolError):
                    self.adapter(FakeTransport()).execute(self.task(approved=True, payload=payload))

    def test_payload_with_extra_or_missing_keys_is_refused(self) -> None:
        for payload in ({**self.payload(), "privacy": "public"},
                        {k: v for k, v in self.payload().items() if k != "sha256"}):
            with self.subTest(keys=sorted(payload)):
                with self.assertRaises(video_adapter.AdapterProtocolError):
                    self.adapter(FakeTransport()).execute(self.task(approved=True, payload=payload))

    def test_path_traversal_video_id_is_refused(self) -> None:
        with self.assertRaises(video_adapter.AdapterProtocolError):
            self.adapter(FakeTransport()).execute(
                self.task(approved=True, payload={**self.payload(), "video_id": "../secrets"}))

    def test_status_reports_the_uploader_state_without_network(self) -> None:
        self.assertEqual(self.adapter(FakeTransport()).status()["uploader"], "ready")
        self.assertEqual(self.adapter(FakeTransport()).status()["live"], ["test-harbor"])
        for f in self.secrets.iterdir():
            f.unlink()
        self.assertEqual(self.adapter(FakeTransport()).status()["uploader"], "secrets not set up")

    def test_shipped_niches_are_all_disabled_for_upload(self) -> None:
        from pionir.video.niche import load_niches
        for niche in load_niches():
            self.assertFalse(niche.live, niche.id)


if __name__ == "__main__":
    unittest.main()
