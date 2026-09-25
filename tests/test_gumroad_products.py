"""Nothing goes on sale on Gumroad without the owner's yes - and never with a secret in it.

``product.gumroad_publish`` parks on every call, runs once after an approval and never after
a denial. Before it is parked the payload must pass the listing rules (the blog's Markdown
and link rules for the description), the zip every client-delivery check (with a product's
larger size limit; executables only when allowed) and the cover its checks (PNG/JPEG, at
least 1280x720, pinned sha). On approval it is all checked again, then: find by permalink ->
update, or create as a DRAFT -> upload the zip (presign, parts, complete) -> attach it ->
upload and attach the cover -> enable. A failure part-way leaves it unpublished.

Gumroad is faked at the HTTP opener with the real opener's signature; one test drives the
real urllib opener against a real loopback server. Nothing touches the network, and the
owner's real secrets are never read (every secrets folder is a temp one).
"""

from __future__ import annotations

import base64
import contextlib
import email.message
import hashlib
import io
import json
import logging
import math
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar, Self
from unittest import mock

from PIL import Image
from test_client_adapter import _settings
from test_discord_gate import API, CHANNEL, OWNER
from test_discord_gate import TOKEN as DISCORD_TOKEN
from test_instagram_post import FakeDiscordFiles

from pionir import cli
from pionir.adapters import deliveries, products
from pionir.adapters.deliveries import DeliveryProblem, inspect_zip, load_secrets
from pionir.adapters.products import (
    LIST,
    NOTHING_ON_SALE,
    PUBLISH,
    TOKEN_REJECTED,
    UNPUBLISH,
    ProductAdapter,
    ProductSettings,
    check_product,
    product_settings,
    render_description,
)
from pionir.bootstrap import build_runtime
from pionir.config import PionirSettings
from pionir.contracts import RiskLevel, Task
from pionir.discord_gate import DiscordGate, DiscordGateSettings, product_line, render_request
from pionir.errors import AdapterProtocolError, AdapterUnavailable
from pionir.server import PionirApp

# Built at runtime: push protection must never see a provider-shaped value in source.
GUMROAD_TOKEN = "gum" + "road-test-" + "tok" + "en-4f9a2c7e1b3d5a6c8e0f"
PLANTED = "PLANTED-" + "product-key-" + "1a2b3c4d5e6f"
GUMROAD = "https://api.gumroad.test/v2"
STORAGE = "https://storage.gumroad.test"
SLUG = "invoice-kit"
ZIP_NAME = "invoice-kit-1.2.0.zip"
COVER_NAME = "cover.png"
DESCRIPTION = (
    "## What it is\n\n"
    "A **small** kit that makes an invoice in one step. It works offline, with *no* "
    "account.\n\n"
    "### What you get\n\n"
    "- the `invoice.html` file\n"
    "- a README that says how to use it\n\n"
    "More at [our site](https://dokazindustries.com/obol).\n\n"
    "```\nopen invoice.html\n```\n"
)
GOOD_FILES: dict[str, bytes] = {
    "README.md": b"# Invoice kit\n\nOpen invoice.html in a browser.\n",
    "invoice.html": b"<!doctype html><title>Invoice</title>\n",
    "docs/": b"",
    "docs/guide.txt": b"Step one: fill in your name.\n",
}
PE = b"MZ" + b"\0" * 58 + (64).to_bytes(4, "little") + b"PE\0\0" + b"\0" * 64


def make_zip(files: dict[str, bytes], *, stored: bool = False) -> bytes:
    buffer = io.BytesIO()
    method = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(buffer, "w", method) as archive:
        for name, data in files.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 9, 25, 0, 0, 0))
            info.compress_type = method
            if name.endswith("/"):
                info.external_attr = (0o40755 << 16) | 0x10
            archive.writestr(info, data)
    return buffer.getvalue()


def image(width: int = 1600, height: int = 900, kind: str = "PNG") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (20, 60, 90)).save(buffer, kind)
    return buffer.getvalue()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def a_product(zip_data: bytes, cover: bytes, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "slug": SLUG, "name": "Invoice Kit", "version": "1.2.0", "price_cents": 1900,
        "pay_what_you_want": False,
        "summary": "An invoice in one step, offline, with no account and no subscription.",
        "description_md": DESCRIPTION, "tags": ["invoicing", "small-business"],
        "zip_name": ZIP_NAME, "zip_sha256": sha(zip_data), "cover_name": COVER_NAME,
        "cover_sha256": sha(cover), "allow_executables": False,
    }
    base.update(over)
    return base


# ---- a fake Gumroad (API + storage) at the opener ------------------------------------------
class _Response:
    def __init__(self, status: int, payload: Any, headers: dict[str, str] | None = None):
        self.status = status
        self._raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.headers = email.message.Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def read(self, _n: int = -1) -> bytes:
        return self._raw

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class FakeGumroad:
    """Gumroad's API v2 and its storage, in memory, checking the Bearer token like the real
    one. Same call signature as OpenerDirector.open."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.products: list[dict[str, Any]] = []
        self.forced: dict[str, Any] = {}    # step -> (status, body) or "down"
        self.down = False
        self.page_size = 10
        self.publish_on_create = False
        self.parts: dict[int, bytes] = {}
        self.files: dict[str, bytes] = {}    # file_url -> bytes
        self.blobs: dict[str, dict[str, Any]] = {}
        self.cover_data: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def steps(self) -> list[str]:
        return [c["step"] for c in self.calls]

    def add(self, **fields: Any) -> dict[str, Any]:
        product = {"id": f"prod-{len(self.products) + 1}", "name": "Existing",
                   "custom_permalink": None, "published": False, "price": 500,
                   "sales_count": 3, "sales_usd_cents": 1500, "deleted": False,
                   "files": [], "covers": [], **fields}
        product.setdefault("short_url",
                           f"https://dokaz.gumroad.com/l/{product['custom_permalink']}")
        self.products.append(product)
        return product

    def product(self, slug: str) -> dict[str, Any]:
        (found,) = [p for p in self.products if p["custom_permalink"] == slug]
        return found

    def __call__(self, request: Any, data: Any = None, timeout: float | None = None) -> Any:
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

    def _step(self, method: str, url: str) -> tuple[str, list[str]]:
        if url.startswith(STORAGE):
            path = urllib.parse.urlsplit(url).path.strip("/").split("/")
            return ("part" if path[0] == "part" else "blob"), path
        path = urllib.parse.urlsplit(url).path
        assert path.startswith("/v2/"), url
        parts = path[len("/v2/"):].split("/")
        if parts == ["products"]:
            return ("list" if method == "GET" else "create"), parts
        if parts[0] == "products" and len(parts) == 2:
            return {"GET": "get", "DELETE": "delete"}.get(method, "update"), parts
        if parts[0] == "products" and len(parts) == 3:
            return {"enable": "enable", "disable": "disable", "covers": "covers"}[parts[2]], parts
        return {"files/presign": "presign", "files/complete": "complete",
                "files/abort": "abort", "direct_uploads": "direct_upload",
                "user": "user"}["/".join(parts)], parts

    def _handle(self, request: Any, timeout: float | None) -> _Response:
        url = request.full_url
        method = request.get_method()
        step, parts = self._step(method, url)
        raw = request.data or b""
        storage = step in ("part", "blob")
        self.calls.append({
            "step": step, "method": method, "url": url, "raw": raw,
            "body": json.loads(raw) if raw and not storage else None, "timeout": timeout,
            "auth": request.get_header("Authorization"),
            "content_type": request.get_header("Content-type"),
            "md5": request.get_header("Content-md5"),
        })
        if self.down or self.forced.get(step) == "down":
            raise urllib.error.URLError(f"connection refused: {url} Bearer {GUMROAD_TOKEN}")
        if not storage and request.get_header("Authorization") != f"Bearer {GUMROAD_TOKEN}":
            self._error(url, 401, {"success": False,
                                   "message": "The access token is invalid"})
        if step in self.forced:
            status, payload = self.forced[step]
            if status >= 400:
                self._error(url, status, payload)
            return _Response(status, payload)
        body = self.calls[-1]["body"] or {}
        if step == "list":
            live = [p for p in self.products if not p.get("deleted")]
            key = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query)).get("page_key")
            start = int(key[1:]) if key else 0
            page = live[start:start + self.page_size]
            out: dict[str, Any] = {"success": True, "products": [dict(p) for p in page]}
            if start + self.page_size < len(live):
                out["next_page_key"] = f"p{start + self.page_size}"
            return _Response(200, out)
        if step == "create":
            fields = {k: v for k, v in body.items() if k not in ("draft", "native_type")}
            product = self.add(**fields, published=(self.publish_on_create
                                                   or not body.get("draft")))
            return _Response(200, {"success": True, "product": dict(product)})
        if step in ("update", "enable", "disable", "covers", "get", "delete"):
            matches = [p for p in self.products
                       if p["id"] == parts[1] and not p.get("deleted")]
            if not matches:
                self._error(url, 404, {"success": False, "message": "not found"})
            product = matches[0]
            if step == "get":
                return _Response(200, {"success": True, "product": dict(product)})
            if step == "delete":
                product["deleted"] = True
                return _Response(200, {"success": True,
                                       "message": "The product has been deleted successfully."})
            if step == "update":
                product.update(body)
            elif step in ("enable", "disable"):
                product["published"] = step == "enable"
            else:
                blob = self.blobs.get(body.get("signed_blob_id"))
                if blob is None or "data" not in blob:
                    return _Response(200, {"success": False,
                                           "message": "The signed_blob_id is invalid"})
                cover_id = f"cov-{len(self.cover_data) + 1}"
                product["covers"].append({"id": cover_id})
                self.cover_data[cover_id] = blob["data"]
                covers = [{"id": c["id"]} for c in product["covers"]]
                return _Response(200, {"success": True, "covers": covers,
                                       "main_cover_id": covers[0]["id"]})
            return _Response(200, {"success": True, "product": dict(product)})
        if step == "presign":
            count = max(1, math.ceil(body["file_size"] / products.PART_SIZE))
            self.parts = {}
            self.presigned = body
            return _Response(200, {
                "success": True, "upload_id": "up-1", "key": f"attachments/s/{body['filename']}",
                "file_url": f"https://files.gumroad.test/attachments/s/{body['filename']}",
                "parts": [{"part_number": n, "presigned_url": f"{STORAGE}/part/{n}?sig=abc"}
                          for n in range(1, count + 1)]})
        if step == "part":
            number = int(parts[1])
            self.parts[number] = raw
            return _Response(200, b"", {"ETag": f'"etag-{number}"'})
        if step == "complete":
            assert body["parts"] == [{"part_number": n, "etag": f'"etag-{n}"'}
                                     for n in sorted(self.parts)], body["parts"]
            file_url = f"https://files.gumroad.test/{body['key']}"
            self.files[file_url] = b"".join(self.parts[n] for n in sorted(self.parts))
            return _Response(200, {"success": True, "file_url": file_url})
        if step == "abort":
            return _Response(200, {"success": True, "status": "accepted"})
        if step == "direct_upload":
            blob = body["blob"]
            signed = f"signed-{len(self.blobs) + 1}"
            self.blobs[signed] = dict(blob)
            return _Response(200, {"id": len(self.blobs), "signed_id": signed,
                                   "direct_upload": {
                                       "url": f"{STORAGE}/blob/{signed}",
                                       "headers": {"Content-Type": blob["content_type"],
                                                   "Content-MD5": blob["checksum"]}}})
        if step == "blob":
            blob = self.blobs[parts[1]]
            if base64.b64encode(hashlib.md5(raw).digest()).decode() != blob["checksum"]:
                self._error(url, 400, b"<Error>BadDigest</Error>")
            blob["data"] = raw
            return _Response(200, b"")
        if step == "user":
            return _Response(200, {"success": True,
                                   "user": {"name": "Dokaz Industries", "user_id": "u1"}})
        raise AssertionError(step)


CREATE_SEQUENCE = ["list", "create", "presign", "part", "complete", "update",
                   "direct_upload", "blob", "covers", "enable"]


class _Case(unittest.TestCase):
    """A hermetic runtime; the product adapter talks to FakeGumroad, reads a temp products
    folder and temp secrets (never the owner's real ones)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.secrets = self.root / "secrets"
        self.secrets.mkdir()
        self.token_file = self.secrets / "gumroad-token.txt"
        self.token_file.write_text(GUMROAD_TOKEN + "\n", encoding="utf-8")
        (self.secrets / "planted.txt").write_text(PLANTED + "\n", encoding="utf-8")
        self.products = self.root / "products"
        self.folder = self.products / SLUG
        self.folder.mkdir(parents=True)
        self.world = FakeGumroad()
        self.adapter = ProductAdapter(ProductSettings(
            api_url=GUMROAD, token_file=self.token_file, products_dir=self.products,
            secrets_dir=self.secrets, ssh_dir=None), opener=self.world)
        runtime = build_runtime(_settings(self.root / "state", content_url=None,
                                          gumroad_url=None))
        runtime.register(self.adapter)
        self.app = PionirApp(runtime)
        self.zip = self.put(GOOD_FILES)
        self.cover = self.put_cover(image())

    def tearDown(self) -> None:
        self.app.runtime.cortex.close()
        self._tmp.cleanup()

    def put(self, files: dict[str, bytes], name: str = ZIP_NAME, **kw: Any) -> bytes:
        data = make_zip(files, **kw)
        (self.folder / name).write_bytes(data)
        return data

    def put_cover(self, data: bytes, name: str = COVER_NAME) -> bytes:
        (self.folder / name).write_bytes(data)
        return data

    def payload(self, **over: Any) -> dict[str, Any]:
        return a_product(self.zip, self.cover, **over)

    def park(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        out = self.app.run_task(PUBLISH, payload or self.payload(), permissions=[PUBLISH])
        self.assertEqual(out["status"], "pending_approval", out)
        return out

    def approve(self, out: dict[str, Any]) -> dict[str, Any]:
        res = self.app.approve(out["approval_id"])
        self.assertTrue(self.app.jobs.wait(res["task_id"], 30))
        return self.app.approvals.get(out["approval_id"])

    def publish(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Park, approve, wait: the only way a product goes on sale."""
        return self.approve(self.park(payload))

    def execute(self, payload: dict[str, Any] | None = None,
                capability: str = PUBLISH) -> dict[str, Any]:
        default = self.payload() if capability == PUBLISH else {}
        return dict(self.adapter.execute(Task(capability, payload if payload is not None
                                              else default)).output)


# ---- the gate ---------------------------------------------------------------------------
class GateTests(_Case):
    def test_the_declarations(self) -> None:
        caps = {c.name: c for c in self.adapter.manifest.capabilities}
        self.assertIs(caps[PUBLISH].risk, RiskLevel.PRIVILEGED)
        self.assertTrue(caps[PUBLISH].requires_approval)
        self.assertFalse(caps[PUBLISH].routable)
        self.assertEqual(caps[PUBLISH].required_permissions, frozenset({PUBLISH}))
        self.assertIs(caps[UNPUBLISH].risk, RiskLevel.PRIVILEGED)
        self.assertFalse(caps[UNPUBLISH].requires_approval)
        self.assertFalse(caps[UNPUBLISH].routable)
        self.assertIs(caps[LIST].risk, RiskLevel.READ_ONLY)

    def test_it_parks_every_time_even_with_the_permission(self) -> None:
        for _ in range(3):
            self.park()
        self.assertEqual(len(self.app.approvals.pending()), 3)
        self.assertEqual(self.world.calls, [])
        self.assertEqual(self.world.products, [])

    def test_an_approved_publish_creates_a_draft_then_files_cover_and_enables_once(self) -> None:
        row = self.publish()
        self.assertEqual(row["status"], "approved", row)
        self.assertEqual(self.world.steps(), CREATE_SEQUENCE)
        calls = {c["step"]: c for c in self.world.calls}
        create = calls["create"]["body"]
        self.assertIs(create["draft"], True)                     # draft first
        self.assertEqual(create["native_type"], "digital")
        self.assertEqual(create["custom_permalink"], SLUG)
        self.assertEqual(create["name"], "Invoice Kit")
        self.assertEqual(create["price"], 1900)
        self.assertEqual(create["price_currency_type"], "usd")
        self.assertIs(create["customizable_price"], False)
        self.assertEqual(create["tags"], ["invoicing", "small-business"])
        self.assertEqual(create["custom_summary"], self.payload()["summary"])
        self.assertEqual(create["description"], render_description(DESCRIPTION, "1.2.0"))
        self.assertIn("<h2>What it is</h2>", create["description"])
        self.assertIn('<a href="https://dokazindustries.com/obol" rel="noopener">our site</a>',
                      create["description"])
        self.assertIn("<p><em>Version 1.2.0</em></p>", create["description"])
        self.assertEqual(calls["presign"]["body"], {"filename": ZIP_NAME,
                                                    "file_size": len(self.zip)})
        (file_url, uploaded), = self.world.files.items()
        self.assertEqual(uploaded, self.zip)                    # exactly the approved bytes
        self.assertEqual(calls["update"]["body"],
                         {"files": [{"url": file_url, "display_name": ZIP_NAME}]})
        self.assertEqual(calls["direct_upload"]["body"]["blob"]["content_type"], "image/png")
        product = self.world.product(SLUG)
        self.assertEqual(self.world.cover_data[product["covers"][0]["id"]], self.cover)
        self.assertIs(product["published"], True)
        for step in ("part", "blob"):                           # storage never sees it
            self.assertIsNone(calls[step]["auth"])
        for call in self.world.calls:
            if call["step"] not in ("part", "blob"):
                self.assertEqual(call["auth"], f"Bearer {GUMROAD_TOKEN}")
        self.assertEqual(calls["part"]["timeout"], 600)
        self.assertEqual(calls["list"]["timeout"], 30)
        self.assertEqual(row["result"]["result"], {
            "ok": True, "product_id": "prod-1", "url": f"https://dokaz.gumroad.com/l/{SLUG}",
            "created": True, "published": True, "version": "1.2.0"})
        self.assertIn("product:id:prod-1", row["result"]["evidence"])
        self.assertFalse(self.app.approve(row["id"])["ok"])    # never twice
        self.assertEqual(len(self.world.calls), len(CREATE_SEQUENCE))

    def test_a_denied_publish_never_runs(self) -> None:
        aid = self.park()["approval_id"]
        self.assertTrue(self.app.deny(aid)["ok"])
        self.assertFalse(self.app.approve(aid)["ok"])
        self.assertEqual(self.world.calls, [])

    def test_a_missing_token_is_unavailable_before_parking(self) -> None:
        self.token_file.unlink()
        with self.assertRaisesRegex(AdapterUnavailable, "setup-gumroad.ps1"):
            self.adapter.validate(Task(PUBLISH, self.payload()))
        out = self.app.run_task(PUBLISH, self.payload(), permissions=[PUBLISH])
        self.assertEqual(out["error"]["type"], "AdapterUnavailable")
        self.assertEqual(self.app.approvals.pending(), [])
        self.assertEqual(self.world.calls, [])


class UpdatePathTests(_Case):
    def test_an_existing_permalink_is_updated_not_duplicated(self) -> None:
        self.world.page_size = 1                       # it is on the second page
        self.world.add(name="Other", custom_permalink="other-thing", published=True)
        self.world.add(name="Old name", custom_permalink=SLUG, published=True,
                       files=[{"id": "f-old", "url": "https://files.gumroad.test/old.zip"}])
        row = self.publish()
        self.assertEqual(row["status"], "approved", row)
        self.assertEqual(self.world.steps(), [
            "list", "list", "disable", "update", "presign", "part", "complete", "update",
            "direct_upload", "blob", "covers", "enable"])
        self.assertNotIn("create", self.world.steps())
        self.assertEqual(len(self.world.products), 2)
        product = self.world.product(SLUG)
        self.assertEqual(product["name"], "Invoice Kit")
        self.assertEqual(product["price"], 1900)
        self.assertEqual([f["display_name"] for f in product["files"]], [ZIP_NAME])
        self.assertIs(product["published"], True)
        self.assertEqual(row["result"]["result"]["product_id"], "prod-2")
        self.assertIs(row["result"]["result"]["created"], False)
        self.assertEqual(self.world.calls[3]["url"], f"{GUMROAD}/products/prod-2")

    def test_an_existing_draft_is_not_disabled_first(self) -> None:
        self.world.add(custom_permalink=SLUG, published=False)
        out = self.execute()
        self.assertIs(out["ok"], True, out)
        self.assertEqual(self.world.steps()[:2], ["list", "update"])
        self.assertNotIn("disable", self.world.steps())

    def test_a_deleted_product_with_the_permalink_is_not_reused(self) -> None:
        self.world.add(custom_permalink=SLUG, deleted=True)
        out = self.execute()
        self.assertIs(out["created"], True, out)
        self.assertIn("create", self.world.steps())


# ---- the checks before parking --------------------------------------------------------
class PayloadRuleTests(_Case):
    def refused(self, payload: dict[str, Any]) -> str:
        """Each rule: refused by validate() (so never parked), refused at run_task."""
        with self.assertRaises(AdapterProtocolError) as err:
            self.adapter.validate(Task(PUBLISH, payload))
        message = str(err.exception)
        self.assertIn("product.gumroad_publish refused by Pionir", message)
        out = self.app.run_task(PUBLISH, payload, permissions=[PUBLISH])
        self.assertEqual(out["status"], "error", out)
        self.assertEqual(out["error"]["type"], "AdapterProtocolError")
        self.assertEqual(self.app.approvals.pending(), [])
        self.assertEqual(self.world.calls, [])
        return message

    def test_the_good_product_passes(self) -> None:
        self.adapter.validate(Task(PUBLISH, self.payload()))
        self.adapter.validate(Task(PUBLISH, self.payload(pay_what_you_want=True, tags=[],
                                                          price_cents=100)))

    def test_bad_slugs(self) -> None:
        for slug in ("ab", "Invoice-Kit", "invoice_kit", "a" * 41, "kit/../x", "", None, 7):
            with self.subTest(slug):
                self.assertIn("slug:", self.refused(self.payload(slug=slug)))

    def test_html_in_the_description(self) -> None:
        for text in ("<b>bold</b>", "<script>alert(1)</script>", "<!-- hidden -->",
                     "<https://dokazindustries.com>"):
            with self.subTest(text):
                message = self.refused(self.payload(description_md=DESCRIPTION + text))
                self.assertIn("description_md:", message)

    def test_a_foreign_link(self) -> None:
        for text in ("[x](https://evil.example.com/a)", "see https://evil.example.com",
                     "[x](http://dokazindustries.com)", "[x](javascript:alert(1))",
                     "[x](mailto:someone)"):
            with self.subTest(text):
                self.assertIn("description_md:",
                              self.refused(self.payload(description_md=DESCRIPTION + text)))
        self.assertIn("summary:", self.refused(self.payload(
            summary="Get it at https://evil.example.com today, it is cheap")))

    def test_the_markdown_subset_and_contact_data(self) -> None:
        for text, words in (("# A title\n", "only ## and ###"),
                            ("#### Deep\n", "only ## and ###"),
                            ("![img](https://dokazindustries.com/a.png)", "images"),
                            ("mail ian@dokazindustries.com", "email"),
                            ("call +1 555 123 4567", "phone"),
                            ("```\nnot closed\n", "fence")):
            with self.subTest(text):
                self.assertIn(words, self.refused(self.payload(
                    description_md=DESCRIPTION + text)))
        # a # inside a code fence is not a heading
        self.adapter.validate(Task(PUBLISH, self.payload(
            description_md=DESCRIPTION + "```\n# a comment\n```\n")))

    def test_the_other_fields(self) -> None:
        cases = {
            "name": ("Kit", "name: 5-80"), "name\n": ("Invoice\nKit", "name:"),
            "version": ("1.2", "version:"), "price low": (99, "price_cents:"),
            "price high": (100_001, "price_cents:"), "price bool": (True, "price_cents:"),
            "price float": (19.0, "price_cents:"), "summary": ("Too short", "summary: 20-200"),
            "tags": (["a", "b", "c", "d", "e", "f"], "tags:"), "tag": (["X"], "tags:"),
            "pwyw": ("yes", "pay_what_you_want:"), "exec": (1, "allow_executables:"),
            "zip path": ("../x.zip", "zip_name:"), "zip ext": ("x.rar", "zip_name:"),
            "cover ext": ("cover.gif", "cover_name:"), "sha": ("ABC", "zip_sha256:"),
        }
        keys = {"name": "name", "name\n": "name", "version": "version",
                "price low": "price_cents", "price high": "price_cents",
                "price bool": "price_cents", "price float": "price_cents",
                "summary": "summary", "tags": "tags", "tag": "tags",
                "pwyw": "pay_what_you_want", "exec": "allow_executables",
                "zip path": "zip_name", "zip ext": "zip_name", "cover ext": "cover_name",
                "sha": "zip_sha256"}
        for label, (value, words) in cases.items():
            with self.subTest(label):
                self.assertIn(words, self.refused(self.payload(**{keys[label]: value})))
        extra = {**self.payload(), "price": 5}
        self.assertIn("price: not a product field", self.refused(extra))
        missing = self.payload()
        del missing["tags"]
        self.assertIn("tags: required", self.refused(missing))


class FileRuleTests(_Case):
    def refused(self, payload: dict[str, Any] | None = None) -> str:
        payload = payload or a_product(self.zip, self.cover)
        with self.assertRaises(AdapterProtocolError) as err:
            self.adapter.validate(Task(PUBLISH, payload))
        out = self.app.run_task(PUBLISH, payload, permissions=[PUBLISH])
        self.assertEqual(out["status"], "error", out)
        self.assertEqual(self.app.approvals.pending(), [])
        self.assertEqual(self.world.calls, [])
        return str(err.exception)

    def test_a_secret_in_the_zip_names_the_file_never_the_value(self) -> None:
        for value, source in ((PLANTED, "planted.txt"), (GUMROAD_TOKEN, "gumroad-token.txt")):
            with self.subTest(source):
                self.zip = self.put({**GOOD_FILES, "src/config.py":
                                     f"KEY = '{value}'\n".encode()})
                message = self.refused()
                self.assertIn("src/config.py", message)
                self.assertIn(source, message)
                self.assertNotIn(value, message)
        with self.subTest("a key format"):
            self.zip = self.put({**GOOD_FILES, "a.txt": b"aws = AKI" + b"AIOSFODNN7EXAMPLE\n"})
            self.assertIn("AWS access key", self.refused())

    def test_an_executable_needs_allow_executables_and_is_then_listed(self) -> None:
        self.zip = self.put({**GOOD_FILES, "bin/kit.exe": PE, "tool.dll": b"x"})
        self.assertIn("executable", self.refused())
        allowed = a_product(self.zip, self.cover, allow_executables=True)
        self.adapter.validate(Task(PUBLISH, allowed))
        preview = self.adapter.product_preview(allowed)
        self.assertEqual(preview["zip"]["executables"], ["bin/kit.exe", "tool.dll"])
        # allowed executables are still scanned for secrets, and archives still refused
        self.zip = self.put({**GOOD_FILES, "bin/kit.exe": PE + PLANTED.encode()})
        self.assertIn("planted.txt", self.refused(a_product(self.zip, self.cover,
                                                            allow_executables=True)))
        self.zip = self.put({**GOOD_FILES, "more.zip": make_zip(GOOD_FILES)})
        self.assertIn("archive inside", self.refused(a_product(self.zip, self.cover,
                                                               allow_executables=True)))

    def test_a_readme_is_required(self) -> None:
        self.zip = self.put({k: v for k, v in GOOD_FILES.items() if k != "README.md"})
        self.assertIn("no README", self.refused())

    def test_sha_mismatches(self) -> None:
        self.assertIn("not the pinned", self.refused(self.payload(zip_sha256="0" * 64)))
        self.assertIn("not the pinned", self.refused(self.payload(cover_sha256="0" * 64)))

    def test_missing_files(self) -> None:
        self.assertIn("no file at", self.refused(self.payload(zip_name="missing.zip")))
        self.assertIn("no cover at", self.refused(self.payload(cover_name="missing.png")))

    def test_the_cover_rules(self) -> None:
        small = self.put_cover(image(1000, 700))
        self.assertIn("at least 1280x720", self.refused(self.payload(cover_sha256=sha(small))))
        jpeg = self.put_cover(image(1280, 720, "JPEG"), "cover.jpg")
        ok = self.payload(cover_name="cover.jpg", cover_sha256=sha(jpeg))
        self.adapter.validate(Task(PUBLISH, ok))
        self.assertEqual(self.adapter.product_preview(ok)["cover"],
                         {"name": "cover.jpg", "size": len(jpeg), "sha256": sha(jpeg),
                          "content_type": "image/jpeg", "width": 1280, "height": 720})
        wrong = self.put_cover(image(1600, 900, "JPEG"), "named.png")
        self.assertIn("is a JPEG but is named", self.refused(
            self.payload(cover_name="named.png", cover_sha256=sha(wrong))))
        text = self.put_cover(b"not an image at all" * 10, "fake.png")
        self.assertIn("not a PNG or JPEG", self.refused(
            self.payload(cover_name="fake.png", cover_sha256=sha(text))))
        with mock.patch.object(products, "MAX_COVER_BYTES", 100):
            self.assertIn("at most 100", self.refused())

    def test_the_product_size_limit_is_its_own(self) -> None:
        big = make_zip({**GOOD_FILES, "data.bin": b"0123456789abcdef" * 1_700_000},
                       stored=True)
        self.assertGreater(len(big), deliveries.MAX_ZIP_BYTES)
        self.zip = big
        (self.folder / ZIP_NAME).write_bytes(big)
        self.adapter.validate(Task(PUBLISH, self.payload()))      # a product may be bigger
        with self.assertRaisesRegex(DeliveryProblem, "too big"):   # a delivery may not
            inspect_zip(self.folder / ZIP_NAME, load_secrets(None), pinned_sha256=sha(big))
        with mock.patch.object(products, "MAX_PRODUCT_ZIP_BYTES", len(big) - 1):
            self.assertIn("too big", self.refused())

    def test_a_file_changed_after_parking_is_refused_on_approval(self) -> None:
        out = self.park()
        self.put({**GOOD_FILES, "extra.txt": b"later\n"})
        row = self.approve(out)
        self.assertEqual(row["status"], "approved_failed")
        result = row["result"]["result"]
        self.assertIn("not the pinned", result["refused"])
        self.assertIn("nothing was published", result["refused"])
        self.assertEqual(self.world.calls, [])


# ---- failures part-way -------------------------------------------------------------------
class FailureTests(_Case):
    def assert_unpublished(self, out: dict[str, Any], step: str) -> None:
        self.assertIs(out["ok"], False, out)
        self.assertEqual(out["step"], step)
        self.assertIn(NOTHING_ON_SALE, out["error"])
        self.assertIn(f"(at step: {step})", out["error"])
        self.assertIs(out["published"], False)
        self.assertIs(self.world.product(SLUG)["published"], False)
        self.assertNotIn("enable", self.world.steps())

    def test_a_failure_after_create_leaves_an_unpublished_draft(self) -> None:
        cases = {
            "complete": ((500, {"success": False, "message": "boom"}), "upload the zip"),
            "part": ((403, b"<Error>expired</Error>"), "upload the zip"),
            "update": ((200, {"success": False, "message": "files: bad url"}),
                       "attach the zip"),
            "direct_upload": ((422, {"error": "bad blob"}), "upload the cover"),
            "covers": ((200, {"success": False, "message": "Could not process your cover"}),
                       "attach the cover"),
        }
        for forced, (answer, step) in cases.items():
            with self.subTest(forced):
                self.world = FakeGumroad()
                self.adapter._open = self.world
                self.world.forced = {forced: answer}
                out = self.execute()
                self.assert_unpublished(out, step)
                self.assertIs(out["created"], True)
                self.assertEqual(out["product_id"], "prod-1")
                if forced in ("complete", "part"):
                    self.assertIn("abort", self.world.steps())
        with self.subTest("through the gate"):
            self.world = FakeGumroad()
            self.adapter._open = self.world
            self.world.forced = {"covers": (200, {"success": False, "message": "no"})}
            row = self.publish()
            self.assertEqual(row["status"], "approved_failed")
            self.assertIn(NOTHING_ON_SALE, row["result"]["result"]["error"])

    def test_a_refused_publish_leaves_it_unpublished(self) -> None:
        self.world.forced = {"enable": (200, {"success": False,
                                              "message": "You must add a payout method"})}
        out = self.execute()
        self.assertIn("You must add a payout method", out["refused"])
        self.assertEqual(out["step"], "publish")
        self.assertIn(NOTHING_ON_SALE, out["refused"])
        self.assertIs(self.world.product(SLUG)["published"], False)

    def test_a_lost_publish_answer_is_made_sure_off_sale(self) -> None:
        self.world.forced = {"enable": "down"}
        out = self.execute()
        self.assertEqual(self.world.steps()[-2:], ["enable", "disable"])
        self.assertIn(NOTHING_ON_SALE, out["unavailable"])
        self.assertIs(out["published"], False)
        self.world = FakeGumroad()
        self.adapter._open = self.world
        self.world.forced = {"enable": "down", "disable": "down"}
        out = self.execute()
        self.assertIn("could not be confirmed off sale", out["unavailable"])
        self.assertIsNone(out["published"])

    def test_a_live_product_whose_update_fails_stays_off_sale(self) -> None:
        self.world.add(custom_permalink=SLUG, published=True)
        self.world.forced = {"complete": (500, {"success": False})}
        out = self.execute()
        self.assertEqual(self.world.steps()[:3], ["list", "disable", "update"])
        self.assertIn("it was on sale before this update", out["error"])
        self.assertIn(NOTHING_ON_SALE, out["error"])
        self.assertIs(self.world.product(SLUG)["published"], False)
        self.assertIs(out["created"], False)

    def test_gumroad_publishing_on_create_is_undone(self) -> None:
        self.world.publish_on_create = True
        out = self.execute()
        self.assertEqual(self.world.steps(), ["list", "create", "disable"])
        self.assertIn("although it was sent as a draft", out["refused"])
        self.assertIs(self.world.product(SLUG)["published"], False)

    def test_success_false_at_http_200_is_a_refusal(self) -> None:
        self.world.forced = {"create": (200, {"success": False,
                                              "message": "Custom permalink is taken"})}
        out = self.execute()
        self.assertIn("Custom permalink is taken", out["refused"])
        self.assertNotIn("unavailable", out)
        self.assertIn("nothing was changed on Gumroad", out["refused"])
        self.assertEqual(self.world.products, [])

    def test_the_status_mapping(self) -> None:
        cases = [
            ("list", (401, {"success": False, "message": "bad"}), "unavailable",
             TOKEN_REJECTED),
            ("list", (503, {"success": False}), "unavailable", "HTTP 503"),
            ("list", (429, {}), "unavailable", "rate limiting"),
            ("create", (400, {"success": False, "message": "name is required"}), "refused",
             "name is required"),
            ("list", (200, b"<html>not json</html>"), "unavailable", "not a result"),
            ("list", "down", "unavailable", "unreachable"),
        ]
        for step, answer, kind, words in cases:
            with self.subTest(step=step, answer=answer):
                self.world = FakeGumroad()
                self.adapter._open = self.world
                self.world.forced = {step: answer}
                out = self.execute()
                self.assertIs(out["ok"], False)
                self.assertIn(words, out[kind])
                self.assertIn(NOTHING_ON_SALE, out[kind])
        self.assertIn(r"run tools\setup-gumroad.ps1", TOKEN_REJECTED)
        self.world = FakeGumroad()
        self.adapter._open = self.world
        self.token_file.write_text("gum" + "road-a-revoked-token-000", encoding="utf-8")
        out = self.execute()
        self.assertEqual(out["unavailable"].split(" (at step")[0], TOKEN_REJECTED)


# ---- unpublish and list ----------------------------------------------------------------------
class UnpublishAndListTests(_Case):
    def test_unpublish_runs_at_once_with_the_permission(self) -> None:
        self.world.add(custom_permalink=SLUG, published=True)
        out = self.app.run_task(UNPUBLISH, {"slug": SLUG}, permissions=[UNPUBLISH])
        self.assertIs(out["ok"], True, out)
        self.assertEqual(out["result"], {"ok": True, "product_id": "prod-1", "slug": SLUG,
                                         "published": False})
        self.assertEqual(self.world.steps(), ["list", "disable"])
        self.assertEqual(self.app.approvals.pending(), [])
        self.assertIs(self.world.product(SLUG)["published"], False)

    def test_unpublish_without_the_permission_is_parked_and_unknown_is_refused(self) -> None:
        out = self.app.run_task(UNPUBLISH, {"slug": SLUG})
        self.assertEqual(out["status"], "pending_approval")
        self.assertEqual(self.world.calls, [])
        refused = self.execute({"slug": "no-such-thing"}, UNPUBLISH)
        self.assertIn("no Gumroad product has the permalink", refused["refused"])
        with self.assertRaises(AdapterProtocolError):
            self.adapter.validate(Task(UNPUBLISH, {"slug": SLUG, "force": True}))

    def test_the_list_shape(self) -> None:
        self.world.page_size = 1
        self.world.add(name="Invoice Kit", custom_permalink=SLUG, published=True, price=1900,
                       sales_count=4, sales_usd_cents=7600)
        self.world.add(name="Draft thing", custom_permalink=None, published=False,
                       short_url=None)
        out = self.app.run_task(LIST, {})
        self.assertIs(out["ok"], True, out)
        self.assertEqual(out["result"], {"ok": True, "products": [
            {"id": "prod-1", "slug": SLUG, "name": "Invoice Kit", "published": True,
             "price_cents": 1900, "sales_count": 4, "sales_usd_cents": 7600,
             "url": f"https://dokaz.gumroad.com/l/{SLUG}"},
            {"id": "prod-2", "slug": None, "name": "Draft thing", "published": False,
             "price_cents": 500, "sales_count": 3, "sales_usd_cents": 1500,
             "url": None}]})
        self.assertEqual(self.world.steps(), ["list", "list"])


# ---- the token never leaks -------------------------------------------------------------------
class SecretTests(_Case):
    def test_the_token_never_reaches_a_log_a_result_or_state(self) -> None:
        capture = io.StringIO()
        handler = logging.StreamHandler(capture)
        root = logging.getLogger()
        old = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(handler)
        try:
            ok = self.publish()
            echo = {"success": False, "message": f"bad token {GUMROAD_TOKEN} here"}
            self.world.forced = {"covers": (200, echo)}
            refused = self.publish()
            self.world.forced = {"enable": (502, echo)}
            failed = self.publish()
            listed = self.app.run_task(LIST, {})
            self.world.forced = {}
            self.world.down = True
            down = self.publish()
        finally:
            root.removeHandler(handler)
            root.setLevel(old)
        self.assertEqual(ok["status"], "approved")
        self.assertIn("<redacted>", refused["result"]["result"]["refused"])
        for row in (ok, refused, failed, listed, down):
            self.assertNotIn(GUMROAD_TOKEN, json.dumps(row))
        self.assertNotIn(GUMROAD_TOKEN, capture.getvalue())
        for path in (self.root / "state").rglob("*"):
            if path.is_file():
                self.assertNotIn(GUMROAD_TOKEN.encode(), path.read_bytes(), path)
        self.assertNotIn(GUMROAD_TOKEN, repr(self.adapter.settings))


# ---- the description renderer ---------------------------------------------------------------
class RendererTests(unittest.TestCase):
    def test_everything_is_escaped_and_only_allowed_links_are_links(self) -> None:
        out = render_description(
            "## Title & <more>\n\nText with `<b>code</b>` and **bold _both_** and "
            "[ok](https://www.dokazindustries.com/x?a=1&b=\"2\") and "
            "[no](https://evil.example.com) and [js](javascript:alert(1)).\n\n"
            "1. one\n2. two\n\n```\n<script>alert(1)</script>\n```\n", "2.0.1")
        self.assertIn("<h2>Title &amp; &lt;more&gt;</h2>", out)
        self.assertIn("<code>&lt;b&gt;code&lt;/b&gt;</code>", out)
        self.assertIn("<strong>bold <em>both</em></strong>", out)
        self.assertIn('<a href="https://www.dokazindustries.com/x?a=1&amp;b=&quot;2&quot;" '
                      'rel="noopener">ok</a>', out)
        self.assertIn("[no](https://evil.example.com)", out)
        self.assertNotIn('href="https://evil', out)
        self.assertNotIn('href="javascript', out)
        self.assertIn("<ol><li>one</li><li>two</li></ol>", out)
        self.assertIn("<pre><code>&lt;script&gt;alert(1)&lt;/script&gt;</code></pre>", out)
        self.assertNotIn("<script", out)
        self.assertTrue(out.endswith("<p><em>Version 2.0.1</em></p>"))
        self.assertEqual(out.count("<a "), 1)

    def test_check_product_returns_the_listing(self) -> None:
        payload = a_product(b"z", b"c")
        self.assertEqual(check_product(payload), payload)


# ---- the Discord card --------------------------------------------------------------------
class CardTests(_Case):
    def gate(self, fake: FakeDiscordFiles) -> DiscordGate:
        token_file = self.root / "discord-bot-token.txt"
        token_file.write_text(DISCORD_TOKEN, encoding="utf-8")
        return DiscordGate.for_app(
            self.app,
            DiscordGateSettings(state_root=self.root / "state", channel_id=CHANNEL,
                                owner_user_id=OWNER, token_file=token_file, api_base=API,
                                poll_seconds=0.01),
            opener=fake, sleep=lambda _s: None)

    def test_the_card_shows_the_whole_listing_and_carries_the_cover(self) -> None:
        long = DESCRIPTION + "\n".join(f"- point number {i} about the kit" for i in range(120))
        self.assertGreater(len(long), 3000)
        self.park(self.payload(description_md=long, pay_what_you_want=True))
        fake = FakeDiscordFiles()
        self.assertTrue(self.gate(fake).run_once())
        self.assertEqual(len(fake.uploads), 1)
        payload_part, file_part = fake.uploads[0]
        message = json.loads(payload_part["data"])
        self.assertEqual(message["attachments"], [{"id": 0, "filename": "cover.png"}])
        self.assertEqual(file_part["content_type"], "image/png")
        self.assertEqual(file_part["data"], self.cover)
        posts = [p["content"] for p in fake.posts()]
        self.assertGreater(len(posts), 2)                     # split, never cut
        head, text = posts[0], "\n".join(posts)
        self.assertEqual(head.split("\n")[0],
                         "\U0001f6d2 **PUTS A PRODUCT ON SALE** - the listing below goes "
                         "live on Gumroad at pay what you want, minimum $19.00 if you "
                         "approve.")
        self.assertEqual(head.split("\n")[0],
                         product_line(self.payload(pay_what_you_want=True)))
        self.assertIn("**Name:** Invoice Kit", text)
        self.assertIn("**Version:** `1.2.0`", text)
        self.assertIn("**Price:** pay what you want, minimum $19.00", text)
        self.assertIn("**Summary:** An invoice in one step", text)
        self.assertIn("`invoicing`, `small-business`", text)
        joined = "\n".join(line for line in text.split("\n") if not line.startswith("```"))
        for line in long.split("\n"):
            if line and not line.startswith("```"):
                self.assertIn(line.replace("```", "`\u200b`\u200b`"), joined)
        self.assertIn(f"`{ZIP_NAME}` - {len(self.zip):,} bytes", text)
        files = [n for n in GOOD_FILES if not n.endswith("/")]
        self.assertIn(f"**The files in it ({len(files)}), in full:**", text)
        for name in files:
            self.assertIn(f"{name}  ({len(GOOD_FILES[name]):,} bytes)", text)
        values = len(load_secrets(self.secrets, (self.token_file,), None))
        self.assertIn(f"secrets scan: clean ({len(files)} files and the cover, {values} "
                      "secret values checked)", text)
        self.assertIn("1600x900 PNG", text)
        self.assertNotIn("EXECUTABLE", text)
        self.assertEqual(self.world.calls, [])                # showing it sells nothing
        for secret in (GUMROAD_TOKEN, PLANTED):
            self.assertNotIn(secret, text)

    def test_executables_are_called_out_at_the_top(self) -> None:
        self.zip = self.put({**GOOD_FILES, "bin/kit.exe": PE})
        self.park(self.payload(allow_executables=True))
        fake = FakeDiscordFiles()
        self.assertTrue(self.gate(fake).run_once())
        head = fake.posts()[0]["content"].split("\n")
        self.assertTrue(head[0].startswith("\U0001f6d2 **PUTS A PRODUCT ON SALE**"))
        self.assertIn("**SHIPS EXECUTABLES (1)**", head[1])
        self.assertIn("`bin/kit.exe`", head[1])
        self.assertIn("bin/kit.exe  (", "\n".join(p["content"] for p in fake.posts()))
        self.assertIn("<- EXECUTABLE", "\n".join(p["content"] for p in fake.posts()))

    def test_changed_files_turn_the_card_into_do_not_approve(self) -> None:
        self.park()
        self.put({**GOOD_FILES, "extra.txt": b"later\n"})
        fake = FakeDiscordFiles()
        self.assertTrue(self.gate(fake).run_once())
        self.assertEqual(fake.uploads, [])                    # no cover for a bad product
        head = fake.posts()[0]["content"]
        self.assertIn("DO NOT APPROVE", head)
        self.assertNotIn("secrets scan: clean", head)

    def test_without_an_inspector_the_card_says_it_could_not_look(self) -> None:
        row = {"id": "a1", "capability": PUBLISH, "payload": self.payload(), "summary": "s"}
        text = render_request(row, OWNER)
        self.assertIn("could not be inspected", text)
        self.assertIn(DESCRIPTION.split("\n")[0], text)


# ---- python -m pionir gumroad-check, and the settings ----------------------------------------
class CommandAndSettingsTests(_Case):
    def test_gumroad_check_lists_products_and_sales_never_the_token(self) -> None:
        self.world.add(name="Invoice Kit", custom_permalink=SLUG, published=True, price=1900,
                       sales_count=4, sales_usd_cents=7600)
        settings = _settings(self.root / "state", gumroad_token_file=self.token_file)
        with mock.patch("pionir.adapters.products.product_settings",
                        lambda _c: self.adapter.settings), \
                contextlib.redirect_stdout(io.StringIO()) as printed:
            code = cli.gumroad_check(settings, opener=self.world)
        report = json.loads(printed.getvalue())
        self.assertEqual(code, 0, printed.getvalue())
        self.assertEqual(report["account"], "Dokaz Industries")
        self.assertEqual(report["sales_count"], 4)
        self.assertEqual(report["sales_usd"], "76.00")
        self.assertEqual(report["products"][0]["slug"], SLUG)
        self.assertNotIn(GUMROAD_TOKEN, printed.getvalue())
        self.assertEqual(self.world.steps(), ["user", "list"])
        self.world.forced = {"user": (401, {"success": False})}
        code, report = self.adapter.check_account()
        self.assertEqual((code, report["status"]), (2, "rejected"))
        self.token_file.unlink()
        self.assertEqual(self.adapter.check_account()[0], 1)
        with mock.patch.object(cli, "gumroad_check", return_value=0) as check:
            self.assertEqual(cli.main(["gumroad-check"]), 0)
        check.assert_called_once_with()

    def test_the_settings(self) -> None:
        settings = PionirSettings(state_root=Path("C:/x"))
        self.assertEqual(settings.gumroad_url, "https://api.gumroad.com/v2")
        self.assertEqual(settings.gumroad_token_path,
                         Path.home() / ".pionir" / "secrets" / "gumroad-token.txt")
        self.assertEqual(settings.products_path, Path.home() / ".pionir" / "products")
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ, {
                "PIONIR_STATE_ROOT": root, "PIONIR_PRODUCTS_DIR": str(Path(root) / "p"),
                "PIONIR_GUMROAD_TOKEN_FILE": str(Path(root) / "g.txt")}):
            configured = PionirSettings.from_environment()
            made = product_settings(configured)
        self.assertEqual(made.products_dir, Path(root) / "p")
        self.assertEqual(made.token_file, Path(root) / "g.txt")
        self.assertEqual(made.api_url, "https://api.gumroad.com/v2")
        self.assertEqual(made.secrets_dir, Path.home() / ".pionir" / "secrets")
        for path in (configured.ops_token_path, configured.content_token_path,
                     Path(root) / "g.txt"):
            self.assertIn(path, made.secret_files)
        with mock.patch.dict(os.environ, {"PIONIR_GUMROAD_URL": "off"}):
            self.assertIsNone(PionirSettings.from_environment().gumroad_url)
        with self.assertRaises(ValueError):
            ProductSettings(api_url="http://api.gumroad.com/v2")
        runtime = build_runtime(_settings(self.root / "wired", content_url=None))
        try:
            self.assertIn("product", runtime.adapters)
        finally:
            runtime.cortex.close()


# ---- the real opener -------------------------------------------------------------------------
class _Loopback(BaseHTTPRequestHandler):
    seen: ClassVar[list[dict[str, Any]]]
    state: ClassVar[dict[str, Any]]

    def _reply(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        path = urllib.parse.urlsplit(self.path).path
        headers = {k.lower(): v for k, v in self.headers.items()}
        type(self).seen.append({"method": self.command, "path": path, "body": body,
                                "headers": headers})
        base = f"http://127.0.0.1:{self.server.server_address[1]}"
        extra: dict[str, str] = {}
        state = type(self).state
        if path.startswith("/storage/"):
            if "authorization" in headers:
                status, out = 400, {"error": "a storage URL must not get the token"}
            else:
                state[path] = body
                status, out = 200, {}
                extra["ETag"] = '"loop-etag"'
        elif headers.get("authorization") != f"Bearer {GUMROAD_TOKEN}":
            status, out = 401, {"success": False, "message": "bad token"}
        elif path == "/v2/products" and self.command == "GET":
            status, out = 200, {"success": True, "products": []}
        elif path == "/v2/products":
            status, out = 200, {"success": True, "product": {"id": "p1", "published": False}}
        elif path == "/v2/files/presign":
            status, out = 200, {"success": True, "upload_id": "u", "key": "k",
                                "parts": [{"part_number": 1,
                                           "presigned_url": f"{base}/storage/part1"}]}
        elif path == "/v2/files/complete":
            ok = json.loads(body)["parts"] == [{"part_number": 1, "etag": '"loop-etag"'}]
            status, out = (200, {"success": True, "file_url": "https://files.test/k"}) if ok \
                else (400, {"success": False, "message": "bad etag"})
        elif path == "/v2/direct_uploads":
            status, out = 200, {"signed_id": "s1", "direct_upload": {
                "url": f"{base}/storage/cover", "headers": {"Content-Type": "image/png"}}}
        elif path == "/v2/products/p1/covers":
            status, out = 200, {"success": True, "covers": [{"id": "c1"}]}
        elif path == "/v2/products/p1/enable":
            status, out = 200, {"success": True, "product": {
                "id": "p1", "published": True, "short_url": "https://dokaz.gumroad.com/l/x"}}
        elif path == "/v2/products/p1" and self.command == "DELETE":
            state["deleted"] = True
            status, out = 200, {"success": True, "message": "deleted"}
        elif path == "/v2/products/p1" and self.command == "GET":
            status, out = (404, {"success": False, "message": "not found"}) \
                if state.get("deleted") else (200, {"success": True, "product": {
                    "id": "p1", "published": False,
                    "files": [{"id": "f1", "name": "pionir-probe.zip"}],
                    "covers": [{"id": "c1"}]}})
        elif path == "/v2/products/p1":
            status, out = 200, {"success": True, "product": {"id": "p1"}}
        else:
            status, out = 404, {"success": False, "message": "not found"}
        raw = json.dumps(out).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for key, value in extra.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = do_PUT = do_DELETE = _reply

    def log_message(self, *_a: Any) -> None:
        pass


class RealOpenerTests(unittest.TestCase):
    """The adapter's real urllib opener against a real loopback HTTP server: the Bearer
    header, JSON bodies, the ETag read from a real response, storage without the token."""

    def test_the_publish_through_the_real_opener(self) -> None:
        seen: list[dict[str, Any]] = []
        state: dict[str, Any] = {}
        handler = type("Handler", (_Loopback,), {"seen": seen, "state": state})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token = root / "secrets" / "gumroad-token.txt"
            token.parent.mkdir()
            token.write_text(GUMROAD_TOKEN, encoding="utf-8")
            zipped, cover = make_zip(GOOD_FILES), image()
            (root / "products" / SLUG).mkdir(parents=True)
            (root / "products" / SLUG / ZIP_NAME).write_bytes(zipped)
            (root / "products" / SLUG / COVER_NAME).write_bytes(cover)
            adapter = ProductAdapter(ProductSettings(
                api_url=f"http://127.0.0.1:{httpd.server_address[1]}/v2", token_file=token,
                products_dir=root / "products", secrets_dir=root / "secrets", ssh_dir=None))
            payload = a_product(zipped, cover)
            adapter.validate(Task(PUBLISH, payload))
            out = dict(adapter.execute(Task(PUBLISH, payload)).output)
        self.assertEqual(out, {"ok": True, "product_id": "p1",
                               "url": "https://dokaz.gumroad.com/l/x", "created": True,
                               "published": True, "version": "1.2.0"})
        self.assertEqual([(r["method"], r["path"]) for r in seen], [
            ("GET", "/v2/products"), ("POST", "/v2/products"), ("POST", "/v2/files/presign"),
            ("PUT", "/storage/part1"), ("POST", "/v2/files/complete"),
            ("PUT", "/v2/products/p1"), ("POST", "/v2/direct_uploads"),
            ("PUT", "/storage/cover"), ("POST", "/v2/products/p1/covers"),
            ("PUT", "/v2/products/p1/enable")])
        self.assertEqual(state["/storage/part1"], zipped)
        self.assertEqual(state["/storage/cover"], cover)
        self.assertIs(json.loads(seen[1]["body"])["draft"], True)
        self.assertEqual(seen[1]["headers"]["content-type"], "application/json")
        for record in seen:
            if record["path"].startswith("/storage/"):
                self.assertNotIn("authorization", record["headers"])


    def test_the_upload_probe_through_the_real_opener(self) -> None:
        seen: list[dict[str, Any]] = []
        state: dict[str, Any] = {}
        handler = type("Handler", (_Loopback,), {"seen": seen, "state": state})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        lines: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            token = Path(tmp) / "gumroad-token.txt"
            token.write_text(GUMROAD_TOKEN, encoding="utf-8")
            adapter = ProductAdapter(ProductSettings(
                api_url=f"http://127.0.0.1:{httpd.server_address[1]}/v2", token_file=token,
                products_dir=Path(tmp) / "products", secrets_dir=None, ssh_dir=None))
            code = adapter.probe_upload(say=lines.append)
        self.assertEqual(code, 0, lines)
        self.assertEqual([(r["method"], r["path"]) for r in seen], [
            ("POST", "/v2/products"), ("POST", "/v2/files/presign"),
            ("PUT", "/storage/part1"), ("POST", "/v2/files/complete"),
            ("PUT", "/v2/products/p1"), ("POST", "/v2/direct_uploads"),
            ("PUT", "/storage/cover"), ("POST", "/v2/products/p1/covers"),
            ("GET", "/v2/products/p1"), ("DELETE", "/v2/products/p1"),
            ("GET", "/v2/products/p1")])
        self.assertIs(json.loads(seen[0]["body"])["draft"], True)
        self.assertTrue(state["deleted"])
        self.assertIn("Gumroad shows 1 file(s) (pionir-probe.zip); 1 cover(s)", "\n".join(lines))
        self.assertEqual(lines[-1], "PASSED - the upload paths work; the probe draft was deleted")
        with Image.open(io.BytesIO(state["/storage/cover"])) as cover:
            self.assertEqual(cover.size, (1280, 720))
        for record in seen:
            if record["path"].startswith("/storage/"):
                self.assertNotIn("authorization", record["headers"])


# ---- python -m pionir gumroad-check --probe-upload -------------------------------------
PROBE_SEQUENCE = ["create", "presign", "part", "complete", "update", "direct_upload", "blob",
                  "covers", "get", "delete", "get"]


class ProbeTests(_Case):
    def probe(self) -> tuple[int, str]:
        lines: list[str] = []
        code = self.adapter.probe_upload(say=lines.append)
        text = "\n".join(lines)
        self.assertNotIn(GUMROAD_TOKEN, text)
        self.assertNotIn("enable", self.world.steps())          # never published
        return code, text

    def test_the_happy_path_proves_every_step_and_deletes_the_draft(self) -> None:
        code, text = self.probe()
        self.assertEqual(code, 0, text)
        self.assertEqual(self.world.steps(), PROBE_SEQUENCE)
        create = self.world.calls[0]["body"]
        self.assertEqual(create["name"], "pionir upload probe - safe to delete")
        self.assertRegex(create["custom_permalink"], r"^pionir-upload-probe-[0-9a-f]{8}$")
        self.assertEqual(create["price"], 100)
        self.assertIs(create["draft"], True)
        (product,) = self.world.products
        self.assertTrue(product["deleted"])
        self.assertIs(product["published"], False)
        (uploaded,) = self.world.files.values()
        with zipfile.ZipFile(io.BytesIO(uploaded)) as archive:
            self.assertEqual(archive.namelist(), ["README.md"])
        (cover,) = self.world.cover_data.values()
        self.assertEqual(products._png_size(cover), (1280, 720))
        with Image.open(io.BytesIO(cover)) as opened:
            self.assertEqual((opened.format, opened.size), ("PNG", (1280, 720)))
        self.assertIn("OK      create the draft: product prod-1", text)
        self.assertIn("published: false", text)
        self.assertIn("Gumroad shows 1 file(s) (pionir-probe.zip); 1 cover(s)", text)
        self.assertIn("OK      delete the draft: product prod-1 is gone", text)
        self.assertNotIn("NOT OK", text)
        self.assertTrue(text.endswith("PASSED - the upload paths work; the probe draft was "
                                      "deleted"))

    def test_a_failing_step_still_deletes_the_draft(self) -> None:
        for forced, words in (
                ("covers", (200, {"success": False, "message": "Could not process"})),
                ("complete", (500, {"success": False})),
                ("blob", (403, b"<Error>expired</Error>"))):
            with self.subTest(forced):
                self.world = FakeGumroad()
                self.adapter._open = self.world
                self.world.forced = {forced: words}
                code, text = self.probe()
                self.assertEqual(code, 1)
                self.assertIn("NOT OK", text)
                self.assertEqual(self.world.steps()[-2:], ["delete", "get"])
                self.assertTrue(self.world.products[0]["deleted"])
                self.assertIn("delete the draft: product prod-1 is gone", text)
                self.assertTrue(text.endswith("FAILED - see the NOT OK lines above"))

    def test_a_draft_that_cannot_be_deleted_is_named(self) -> None:
        self.world.forced = {"delete": (500, {"success": False})}
        code, text = self.probe()
        self.assertEqual(code, 1)
        self.assertIn("the probe draft was NOT removed: product prod-1 (permalink "
                      "pionir-upload-probe-", text)
        self.assertIn("delete it in the Gumroad dashboard", text)
        self.assertFalse(self.world.products[0]["deleted"])

    def test_a_create_that_comes_back_published_is_disabled_and_deleted(self) -> None:
        self.world.publish_on_create = True
        code, text = self.probe()
        self.assertEqual(code, 1)
        self.assertEqual(self.world.steps(), ["create", "disable", "delete", "get"])
        self.assertIn("reports product prod-1 as published", text)
        self.assertIn("OK      take it off sale: product prod-1 disabled", text)
        product = self.world.products[0]
        self.assertIs(product["published"], False)
        self.assertTrue(product["deleted"])

    def test_a_rejected_token_and_no_token(self) -> None:
        self.token_file.write_text("gum" + "road-revoked-token-0000", encoding="utf-8")
        code, text = self.probe()
        self.assertEqual(code, 2)
        self.assertIn(TOKEN_REJECTED, text)
        self.assertEqual(self.world.products, [])
        self.assertTrue(text.endswith("FAILED - nothing was created on Gumroad"))
        self.token_file.unlink()
        code, text = self.probe()
        self.assertEqual(code, 1)
        self.assertIn("setup-gumroad.ps1", text)

    def test_the_command(self) -> None:
        settings = _settings(self.root / "state", gumroad_token_file=self.token_file)
        with mock.patch("pionir.adapters.products.product_settings",
                        lambda _c: self.adapter.settings), \
                contextlib.redirect_stdout(io.StringIO()) as printed:
            code = cli.gumroad_check(settings, opener=self.world, probe_upload=True)
        self.assertEqual(code, 0, printed.getvalue())
        self.assertIn("PASSED", printed.getvalue())
        self.assertNotIn(GUMROAD_TOKEN, printed.getvalue())
        self.assertEqual(self.world.steps(), PROBE_SEQUENCE)
        with mock.patch.object(cli, "gumroad_check", return_value=0) as check:
            self.assertEqual(cli.main(["gumroad-check", "--probe-upload"]), 0)
        check.assert_called_once_with(probe_upload=True)
        with mock.patch.object(cli, "gumroad_check", return_value=0) as check:
            self.assertEqual(cli.main(["gumroad-check"]), 0)
        check.assert_called_once_with()


# ---- the approval summary names the product ---------------------------------------------
class SummaryTests(_Case):
    def test_a_parked_publish_names_the_product(self) -> None:
        out = self.park()
        self.assertEqual(out["summary"], "product · product.gumroad_publish — Invoice Kit 1.2.0 "
                                         "($19.00) - invoice-kit")
        self.assertEqual(self.app.approvals.pending()[0]["summary"], out["summary"])
        pwyw = self.park(self.payload(pay_what_you_want=True))
        self.assertIn("Invoice Kit 1.2.0 (pay what you want, from $19.00) - invoice-kit",
                      pwyw["summary"])

    def test_the_shape(self) -> None:
        from pionir.server import _product_gist
        self.assertEqual(_product_gist({"slug": "post-guard", "name": "Post Guard",
                                        "version": "1.0.0", "price_cents": 1900}),
                         "Post Guard 1.0.0 ($19.00) - post-guard")
        self.assertEqual(_product_gist({"slug": "x-y", "name": "Only  A\nName"}),
                         "Only A Name - x-y")
        out = self.app.run_task(UNPUBLISH, {"slug": SLUG})           # no name: as before
        self.assertEqual(out["summary"], "product · product.gumroad_unpublish")


if __name__ == "__main__":
    unittest.main()
