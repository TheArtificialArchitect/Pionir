"""Marketplaces: Pionir's Apify and Chrome Web Store adapters, faked at the HTTP boundary.

The fakes answer like the official APIs (Apify REST v2: ``/v2/users/me``, ``/v2/acts``,
``/v2/acts/<id>/versions``, ``/v2/acts/<id>/builds``, ``/v2/actor-builds/<id>``; Chrome Web
Store API v2: ``oauth2.googleapis.com/token``, ``/upload/v2/publishers/<p>/items/<i>:upload``,
``:publish``, ``:fetchStatus``), and every request is recorded and checked for its shape. Each
test fails if its rule is reverted: a publish that does not park, a package that changed or
carries a secret sent anyway, an Actor made public after a failed build, a price change with
no notice, an upload to an item the owner did not create, ``skipReview`` sent, a submission
called live, or a token in a result.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from marketplace_support import Recorder, apify_spec, chrome_build, chrome_spec, core_build

from pionir.adapters.apify import (
    PUBLISH,
    STATS,
    ApifyAdapter,
    ApifySettings,
    check_publish,
)
from pionir.adapters.chrome_webstore import (
    PUBLISH as CHROME_PUBLISH,
)
from pionir.adapters.chrome_webstore import (
    STATUS as CHROME_STATUS,
)
from pionir.adapters.chrome_webstore import (
    ChromeSettings,
    ChromeWebStoreAdapter,
)
from pionir.contracts import RiskLevel, Task
from pionir.crew.marketplaces import apify_pack, chrome_pack
from pionir.discord_gate import render_request
from pionir.errors import AdapterProtocolError, AdapterUnavailable

TOKEN = "apify" + "_api_" + "TESTTOKENVALUE0123456789abcdef"
API = "https://api.apify.test"
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
ITEM = "abcdefghijklmnopabcdefghijklmnop"
REFRESH = "1//refresh-" + "TOKENVALUE0123456789"
SECRET = "client-" + "SECRETVALUE0123456789"


def write_draft(root: Path, slug: str, listing: dict, files: dict) -> None:
    folder = root / "listings" / slug
    folder.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (folder / name).write_bytes(data)
    (folder / "listing.json").write_text(json.dumps(listing), encoding="utf-8")


class FakeApify:
    """The Apify API: one account ``dokaz``; actors by ``user~name``; builds that end as
    ``build_result``."""

    def __init__(self) -> None:
        self.actors: dict = {}
        self.build_result = "SUCCEEDED"
        self.build_polls = 1
        self._polls = 0

    def __call__(self, call):
        url, method = call["url"], call["method"]
        assert url.startswith(API), url
        assert call["headers"].get("authorization") == f"Bearer {TOKEN}", call["headers"]
        path = url[len(API):]
        if path == "/v2/users/me":
            return 200, {"data": {"id": "u1", "username": "dokaz"}}
        if path.startswith("/v2/acts/dokaz~") and method == "GET":
            name = path.split("~", 1)[1]
            act = self.actors.get(name)
            return (200, {"data": act}) if act else (404, {"error": {"type": "record-not-found",
                                                                     "message": "Actor not found"}})
        if path == "/v2/acts" and method == "POST":
            body = json.loads(call["body"])
            act = {"id": f"act-{body['name']}", "name": body["name"], "isPublic": False,
                   "pricingInfos": [], "stats": {"totalUsers": 0}}
            self.actors[body["name"]] = act
            return 201, {"data": act}
        if "/builds?" in path and method == "POST":
            self._polls = 0
            return 201, {"data": {"id": "build-1", "status": "RUNNING"}}
        if path.startswith("/v2/actor-builds/build-1"):
            self._polls += 1
            done = self._polls >= self.build_polls
            return 200, {"data": {"id": "build-1",
                                  "status": self.build_result if done else "RUNNING"}}
        if "/versions" in path and method in ("PUT", "POST"):
            return 200, {"data": json.loads(call["body"])}
        if path.startswith("/v2/acts/act-") and method == "PUT":
            return 200, {"data": json.loads(call["body"])}
        if path.startswith("/v2/store?"):
            return 200, {"data": {"items": [{"name": "sitemap-audit", "actorReviewRating": 4.5,
                                             "actorReviewCount": 3}]}}
        if path.startswith("/v2/acts/act-") and "/runs?" in path:
            return 200, {"data": {"items": [
                {"status": "SUCCEEDED", "startedAt": "2026-10-06T10:00:00.000Z"},
                {"status": "FAILED", "startedAt": "2026-10-05T10:00:00.000Z"},
                {"status": "SUCCEEDED", "startedAt": "2026-09-01T10:00:00.000Z"}]}}
        raise AssertionError(f"unexpected Apify call {method} {path}")


class _ApifyCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.root = self.dir / "marketplaces"
        self.secrets = self.dir / "secrets"
        self.secrets.mkdir()
        self.token_file = self.secrets / "apify-token.txt"
        self.token_file.write_text(TOKEN, encoding="utf-8")
        self.listing, files, problems = apify_pack.draft(apify_spec(), core_build())
        self.assertEqual(problems, [])
        write_draft(self.root, "sitemap-audit", self.listing, files)
        self.api = FakeApify()
        self.http = Recorder(self.api)
        self.adapter = ApifyAdapter(ApifySettings(api_url=API, token_file=self.token_file,
                                                  root=self.root, secrets_dir=self.secrets),
                                    opener=self.http, clock=lambda: NOW, sleep=lambda s: None)

    def publish(self, payload=None) -> dict:
        return dict(self.adapter.execute(Task(PUBLISH, payload or self.listing)).output)

    def calls(self) -> list:
        return [(c["method"], c["url"][len(API):].split("?")[0]) for c in self.http.calls]


class ApifyGateTests(_ApifyCase):
    def test_publish_always_parks_and_stats_only_reads(self) -> None:
        caps = {c.name: c for c in self.adapter.manifest.capabilities}
        self.assertTrue(caps[PUBLISH].requires_approval)
        self.assertIs(caps[PUBLISH].risk, RiskLevel.PRIVILEGED)
        self.assertFalse(caps[PUBLISH].routable)
        self.assertIs(caps[STATS].risk, RiskLevel.READ_ONLY)

    def test_the_card_shows_the_store_page_and_the_price(self) -> None:
        text = render_request({"id": "a1", "capability": PUBLISH, "payload": self.listing,
                               "summary": "s"}, "123")
        self.assertIn("PUBLISHES PUBLICLY ON THE APIFY STORE", text)
        self.assertIn("$0.0030 per successful", text)
        self.assertIn("## Pricing", text)             # the README in full

    def test_validate_refuses_a_bad_listing_or_a_missing_token_before_parking(self) -> None:
        with self.assertRaises(AdapterProtocolError):
            self.adapter.validate(Task(PUBLISH, {**self.listing, "title": "The best Actor"}))
        with self.assertRaises(AdapterProtocolError):
            self.adapter.validate(Task(PUBLISH, {**self.listing, "extra": 1}))
        self.token_file.unlink()
        with self.assertRaises(AdapterUnavailable):
            self.adapter.validate(Task(PUBLISH, self.listing))
        self.assertEqual(self.http.calls, [])

    def test_a_changed_package_is_never_sent(self) -> None:
        (self.root / "listings" / "sitemap-audit" / "actor-source.zip").write_bytes(b"PK other")
        out = self.publish()
        self.assertFalse(out["ok"])
        self.assertIn("changed since", out["refused"])
        self.assertEqual(self.http.calls, [])

    def test_a_package_carrying_a_secret_is_never_sent(self) -> None:
        secret = "sk_" + "live_" + "SECRETVALUE987654321"
        (self.secrets / "stripe.txt").write_text(secret, encoding="utf-8")
        core = core_build()
        core["src/sitemap_audit/cli.py"] = f"KEY = '{secret}'\n".encode()
        listing, files, _ = apify_pack.draft(apify_spec(), core)
        spec_root = self.root / "listings" / "sitemap-audit"
        for name, data in files.items():
            (spec_root / name).write_bytes(data)
        out = self.publish(listing)
        self.assertFalse(out["ok"])
        self.assertIn("secrets found", out["refused"])
        self.assertEqual(self.http.calls, [])


class ApifyPublishTests(_ApifyCase):
    def test_a_new_actor_is_created_private_built_then_made_public_with_its_price(self) -> None:
        out = self.publish()
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["published"])
        self.assertEqual(out["url"], "https://apify.com/dokaz/sitemap-audit")
        self.assertEqual(self.calls(), [
            ("GET", "/v2/users/me"), ("GET", "/v2/acts/dokaz~sitemap-audit"),
            ("POST", "/v2/acts"), ("POST", "/v2/acts/act-sitemap-audit/builds"),
            ("GET", "/v2/actor-builds/build-1"), ("PUT", "/v2/acts/act-sitemap-audit")])
        create = self.http.json_body(2)
        self.assertEqual(create["name"], "sitemap-audit")
        self.assertIs(create["isPublic"], False)                 # private until it builds
        version = create["versions"][0]
        self.assertEqual((version["versionNumber"], version["sourceType"], version["buildTag"]),
                         ("0.1", "SOURCE_FILES", "latest"))
        names = sorted(f["name"] for f in version["sourceFiles"])
        self.assertEqual(names, self.listing["files"])
        self.assertTrue(all(f["format"] in ("TEXT", "BASE64") for f in version["sourceFiles"]))
        self.assertIn("version=0.1", self.http.calls[3]["url"])
        self.assertIn("tag=latest", self.http.calls[3]["url"])
        final = self.http.json_body(5)
        self.assertIs(final["isPublic"], True)
        self.assertEqual(final["categories"], ["SEO_TOOLS", "DEVELOPER_TOOLS"])
        (ppe,) = final["pricingInfos"]
        self.assertEqual(ppe["pricingModel"], "PAY_PER_EVENT")
        self.assertEqual(ppe["pricingPerEvent"]["actorChargeEvents"]["result"],
                         {"eventTitle": "Sitemap audited",
                          "eventDescription": "One sitemap URL fetched and audited",
                          "eventPriceUsd": 0.003})
        self.assertEqual(ppe["startedAt"], ppe["createdAt"])     # new: priced at once
        self.assertNotIn(TOKEN, json.dumps(out))

    def test_a_failed_build_leaves_the_actor_private_and_unpriced(self) -> None:
        self.api.build_result = "FAILED"
        out = self.publish()
        self.assertFalse(out["ok"])
        self.assertIn("NOT made public", out["refused"])
        self.assertNotIn(("PUT", "/v2/acts/act-sitemap-audit"), self.calls())

    def test_an_update_uploads_the_version_and_gives_notice_of_a_new_price(self) -> None:
        self.api.actors["sitemap-audit"] = {
            "id": "act-sitemap-audit", "isPublic": True, "pricingInfos": [
                {"pricingModel": "PAY_PER_EVENT", "pricingPerEvent": {"actorChargeEvents": {
                    "result": {"eventTitle": "Sitemap audited", "eventPriceUsd": 0.002}}}}]}
        out = self.publish()
        self.assertTrue(out["ok"], out)
        self.assertIn(("PUT", "/v2/acts/act-sitemap-audit/versions/0.1"), self.calls())
        self.assertNotIn(("POST", "/v2/acts"), self.calls())
        infos = self.http.json_body(len(self.http.calls) - 1)["pricingInfos"]
        self.assertEqual(len(infos), 2)                          # append-only
        self.assertEqual(infos[-1]["startedAt"], "2026-10-21T12:00:00.000Z")   # 14 days

    def test_the_same_price_is_not_appended_again(self) -> None:
        self.api.actors["sitemap-audit"] = {
            "id": "act-sitemap-audit", "isPublic": True, "pricingInfos": [
                {"pricingModel": "PAY_PER_EVENT", "pricingPerEvent": {"actorChargeEvents": {
                    "result": {"eventTitle": "Sitemap audited", "eventPriceUsd": 0.003}}}}]}
        out = self.publish()
        self.assertEqual(out["pricing"], "unchanged")
        self.assertNotIn("pricingInfos", self.http.json_body(len(self.http.calls) - 1))

    def test_a_rejected_token_is_unavailable_and_never_echoed(self) -> None:
        self.http.route = lambda call: (401, {"error": {"message": f"bad token {TOKEN}"}})
        out = self.publish()
        self.assertFalse(out["ok"])
        self.assertIn("setup-apify.ps1", out["unavailable"])
        self.assertNotIn(TOKEN, json.dumps(out))

    def test_stats_reads_users_runs_and_rating(self) -> None:
        self.api.actors["sitemap-audit"] = {"id": "act-sitemap-audit", "isPublic": True,
                                            "stats": {"totalUsers": 9, "totalUsers30Days": 4,
                                                      "totalRuns": 40}}
        out = dict(self.adapter.execute(Task(STATS, {"actors": ["sitemap-audit"]})).output)
        self.assertTrue(out["ok"], out)
        (a,) = out["actors"]
        self.assertEqual((a["users_30d"], a["runs_total"], a["rating"], a["reviews"]),
                         (4, 40, 4.5, 3))
        self.assertEqual(a["runs_7d"], {"SUCCEEDED": 1, "FAILED": 1})
        self.assertTrue(all(c["method"] == "GET" for c in self.http.calls))

    def test_check_publish_is_exact(self) -> None:
        self.assertEqual(check_publish(self.listing)["slug"], "sitemap-audit")
        with self.assertRaises(ValueError):
            check_publish({**self.listing, "categories": ["SOCIAL_MEDIA"]})
        with self.assertRaises(ValueError):
            check_publish({**self.listing, "pricing": {**self.listing["pricing"],
                                                       "price_usd": 1.0}})


class FakeChrome:
    def __init__(self) -> None:
        self.upload_state = "SUCCEEDED"
        self.async_states = ["SUCCEEDED"]

    def __call__(self, call):
        url, method = call["url"], call["method"]
        if url == "https://oauth2.test/token":
            return 200, {"access_token": "ya29.ACCESS", "expires_in": 3599}
        base = "https://cws.test"
        name = f"publishers/pub-1/items/{ITEM}"
        assert call["headers"].get("authorization") == "Bearer ya29.ACCESS"
        if url == f"{base}/upload/v2/{name}:upload" and method == "POST":
            return 200, {"name": name, "itemId": ITEM, "crxVersion": "1.0.0",
                         "uploadState": self.upload_state}
        if url == f"{base}/v2/{name}:fetchStatus" and method == "GET":
            return 200, {"name": name, "itemId": ITEM,
                         "lastAsyncUploadState": self.async_states.pop(0) if self.async_states
                         else "SUCCEEDED",
                         "publishedItemRevisionStatus": {"state": "PUBLISHED",
                                                         "distributionChannels": [
                                                             {"crxVersion": "0.9.0",
                                                              "deployPercentage": 100}]},
                         "takenDown": False, "warned": False}
        if url == f"{base}/v2/{name}:publish" and method == "POST":
            return 200, {"name": name, "itemId": ITEM, "state": "PENDING_REVIEW"}
        raise AssertionError(f"unexpected Chrome call {method} {url}")


class ChromeTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        self.root = d / "marketplaces"
        self.secrets = d / "secrets"
        self.secrets.mkdir()
        self.creds = self.secrets / "chrome-webstore.json"
        self.creds.write_text(json.dumps({"client_id": "cid.apps.googleusercontent.com",
                                          "client_secret": SECRET, "refresh_token": REFRESH,
                                          "publisher_id": "pub-1"}), encoding="utf-8")
        listing, files, problems = chrome_pack.draft(chrome_spec(), chrome_build())
        self.assertEqual(problems, [])
        write_draft(self.root, "tab-word-count", listing, files)
        (self.root / "chrome-items.json").write_text(json.dumps({"tab-word-count": ITEM}),
                                                     encoding="utf-8")
        self.payload = {"slug": "tab-word-count", "item_id": ITEM, "name": listing["name"],
                        "summary": listing["summary"], "version": "1.0.0",
                        "package_name": "extension.zip",
                        "package_sha256": listing["package_sha256"],
                        "publish_type": "DEFAULT_PUBLISH"}
        self.api = FakeChrome()
        self.http = Recorder(self.api)
        self.adapter = ChromeWebStoreAdapter(
            ChromeSettings(api_url="https://cws.test", token_url="https://oauth2.test/token",
                           credentials_file=self.creds, root=self.root,
                           secrets_dir=self.secrets),
            opener=self.http, sleep=lambda s: None)

    def run_task(self, cap, payload) -> dict:
        return dict(self.adapter.execute(Task(cap, payload)).output)

    def test_it_parks_and_reads(self) -> None:
        caps = {c.name: c for c in self.adapter.manifest.capabilities}
        self.assertTrue(caps[CHROME_PUBLISH].requires_approval)
        self.assertIs(caps[CHROME_PUBLISH].risk, RiskLevel.PRIVILEGED)
        self.assertIs(caps[CHROME_STATUS].risk, RiskLevel.READ_ONLY)

    def test_upload_then_submit_for_review_with_the_v2_api(self) -> None:
        out = self.run_task(CHROME_PUBLISH, self.payload)
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["submitted"])
        self.assertFalse(out["published"])          # in review: never called live
        token, upload, publish = self.http.calls
        self.assertEqual(token["method"], "POST")
        form = token["body"].decode()
        self.assertIn("grant_type=refresh_token", form)
        self.assertEqual(upload["url"], f"https://cws.test/upload/v2/publishers/pub-1/items/"
                                        f"{ITEM}:upload")
        self.assertEqual(upload["headers"]["content-type"], "application/zip")
        self.assertTrue(upload["body"].startswith(b"PK"))
        self.assertEqual(publish["url"], f"https://cws.test/v2/publishers/pub-1/items/"
                                         f"{ITEM}:publish")
        body = json.loads(publish["body"])
        self.assertEqual(body, {"publishType": "DEFAULT_PUBLISH"})     # never skipReview
        text = json.dumps(out)
        self.assertNotIn(REFRESH, text)
        self.assertNotIn(SECRET, text)

    def test_an_upload_in_progress_is_followed_until_it_ends(self) -> None:
        self.api.upload_state = "IN_PROGRESS"
        self.api.async_states = ["IN_PROGRESS", "SUCCEEDED"]
        out = self.run_task(CHROME_PUBLISH, self.payload)
        self.assertTrue(out["ok"], out)
        self.assertEqual([c["url"].rsplit(":", 1)[-1] for c in self.http.calls[1:]],
                         ["upload", "fetchStatus", "fetchStatus", "publish"])

    def test_a_failed_upload_is_never_submitted(self) -> None:
        self.api.upload_state = "FAILED"
        out = self.run_task(CHROME_PUBLISH, self.payload)
        self.assertFalse(out["ok"])
        self.assertFalse(any(c["url"].endswith(":publish") for c in self.http.calls))

    def test_only_the_item_the_owner_created_for_the_slug(self) -> None:
        other = "ponmlkjihgfedcbaponmlkjihgfedcba"
        with self.assertRaises(AdapterProtocolError):
            self.adapter.validate(Task(CHROME_PUBLISH, {**self.payload, "item_id": other}))
        out = self.run_task(CHROME_PUBLISH, {**self.payload, "item_id": other})
        self.assertFalse(out["ok"])
        self.assertEqual(self.http.calls, [])

    def test_the_approved_version_must_be_the_packages(self) -> None:
        out = self.run_task(CHROME_PUBLISH, {**self.payload, "version": "2.0.0"})
        self.assertFalse(out["ok"])
        self.assertIn("version", out["refused"])

    def test_missing_credentials_are_not_configured(self) -> None:
        self.creds.unlink()
        with self.assertRaises(AdapterUnavailable):
            self.adapter.validate(Task(CHROME_PUBLISH, self.payload))
        out = self.run_task(CHROME_STATUS, {"items": [ITEM]})
        self.assertTrue(out["not_configured"])

    def test_status_reads_the_revisions(self) -> None:
        out = self.run_task(CHROME_STATUS, {"items": [ITEM]})
        self.assertTrue(out["ok"], out)
        (item,) = out["items"]
        self.assertEqual(item["published"], {"state": "PUBLISHED", "crx_version": "0.9.0",
                                             "deploy_percentage": 100})


if __name__ == "__main__":
    unittest.main()
