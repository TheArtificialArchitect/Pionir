"""Shared fakes for the Marketplaces tests (not a test module).

- ``spec_words`` / ``apify_spec`` / ``chrome_spec`` / ``shopify_spec``: what the shared brain
  would answer, and the checked specs built from it with a measured niche;
- ``core_build``: the files of an approved Apify core build (a tiny real package);
- ``chrome_build``: the files of an approved extension build;
- ``FakeHttp``: the crew's GET client, answering from a dict of URL -> (status, body);
- ``Response`` / ``Recorder``: an ``OpenerDirector.open`` stand-in for Pionir's adapters that
  records every request (method, URL, headers, body) and answers from a script.
"""
from __future__ import annotations

import json
import urllib.error
from typing import Any

from pionir.crew.marketplaces import specs
from pionir.crew.net import HttpResponse, HttpUnreachable

T0 = 1_790_000_000.0

NICHE = {"niche": "sitemap audit", "market": "apify", "listings": 4, "demand": 5200,
         "quality": 0.62, "score": 1.9, "category": "SEO_TOOLS,DEVELOPER_TOOLS",
         "keys": ["a/x", "b/y", "c/z", "d/w"],
         "sample": [{"title": "Sitemap Checker", "users": 3000, "rating": 3.1, "ratings": 9,
                     "url": "https://apify.com/a/x"}],
         "preferred": True, "personal": None}

APIFY_WORDS = {
    "feasible": True, "slug": "sitemap-audit", "name": "Sitemap Audit: check every URL a "
    "sitemap lists", "summary": "Reads an XML sitemap and reports each listed URL's status, "
    "canonical tag and robots meta in one table.",
    "brief": "Sitemap Audit takes the text of a fetched XML sitemap page and reports, for each "
             "URL it lists, whether the entry is well formed, its last-modified date and "
             "priority, and which entries are duplicates.",
    "features": ["Parses urlset and sitemapindex documents",
                 "Flags duplicate and malformed URL entries",
                 "Reports lastmod and priority for each entry"],
    "acceptance": ["A sitemap with three URLs gives three entries",
                   "A duplicate URL is flagged once",
                   "A sitemap index lists its child sitemaps"],
    "limits": "It reads one sitemap per URL and does not follow child sitemaps on its own.",
    "tags": ["seo", "sitemap", "audit"],
    "listing": {"io": "fetch_urls", "event_title": "Sitemap audited",
                "event_description": "One sitemap URL fetched and audited",
                "price_usd_per_result": 0.003}}

CHROME_WORDS = {
    "feasible": True, "slug": "tab-word-count", "name": "Tab Word Count: words on this page",
    "summary": "Counts the words, sentences and reading time of the page you are reading.",
    "brief": "Tab Word Count reads the visible text of the active tab when you click its button "
             "and shows the number of words and sentences and an estimated reading time.",
    "features": ["Counts words and sentences on the active tab", "Estimates reading time"],
    "acceptance": ["Counts ten words as ten", "An empty page counts zero",
                   "Reading time rounds up to a whole minute"],
    "limits": "It reads only the tab you click it on, and only its visible text.",
    "tags": ["writing", "productivity"],
    "listing": {"single_purpose": "Count the words on the page the user is reading.",
                "permissions": ["activeTab", "scripting"],
                "justifications": {"activeTab": "To read the visible text of the tab the user "
                                                "clicked the button on.",
                                   "scripting": "To run the counting script in that tab only."},
                "free_features": ["Word and sentence count"],
                "pro_features": ["Reading time history"], "pro_price_usd_month": 1.99}}

SHOPIFY_WORDS = {
    "feasible": True, "slug": "alt-text-check", "name": "Alt Text Check: product images",
    "summary": "Lists the product images in a store that have no alt text, product by product.",
    "brief": "Alt Text Check reads the store's products and their images and lists every image "
             "with an empty alt text, grouped by product, so the owner can fix them.",
    "features": ["Lists images without alt text", "Groups them by product"],
    "acceptance": ["An image with no alt is listed", "An image with alt is not",
                   "Products without images are skipped"],
    "limits": "It reads products only; it does not write alt text itself.",
    "tags": ["accessibility", "seo"],
    "listing": {"scopes": ["read_products"],
                "justifications": {"read_products": "To read product images and their alt "
                                                    "text."},
                "plans": [{"name": "Free", "price_usd_month": 0}]}}


def spec_for(market: str, words: dict, **niche_over) -> dict:
    niche = {**NICHE, "market": market, **niche_over}
    return specs.assemble(market, words, niche, T0)


def apify_spec() -> dict:
    return spec_for("apify", APIFY_WORDS)


def chrome_spec() -> dict:
    return spec_for("chrome", CHROME_WORDS, niche="word count", category="productivity/tools")


def shopify_spec() -> dict:
    return spec_for("shopify", SHOPIFY_WORDS, niche="alt text", category="store-design")


CORE_INIT = b'''"""Sitemap audit core."""
import xml.etree.ElementTree as ET


def process(record):
    text = record.get("text") if isinstance(record, dict) else None
    if not text:
        return {"error": "no sitemap text"}
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        return {"error": f"not XML: {exc}"}
    urls = [e.text for e in root.iter() if e.tag.endswith("loc")]
    return {"url": record.get("url"), "entries": len(urls),
            "duplicates": len(urls) - len(set(urls))}
'''


def core_build(package: str = "sitemap_audit") -> dict:
    return {f"src/{package}/__init__.py": CORE_INIT,
            f"src/{package}/cli.py": b"def main():\n    return 0\n",
            "tests/test_core.py": b"import unittest\n",
            "README.md": b"# Sitemap Audit\n\nThe core.\n",
            "LICENSE.txt": b"licence\n", "THIRD_PARTY.txt": b"None.\n"}


def chrome_build(*, permissions=("activeTab", "scripting", "storage"), extpay=True,
                 extra: dict | None = None) -> dict:
    manifest = {"manifest_version": 3, "name": "Tab Word Count", "version": "1.0.0",
                "description": "Counts words.", "permissions": list(permissions),
                "background": {"service_worker": "src/background.js"},
                "action": {"default_title": "Count words"},
                "content_scripts": [{"matches": ["https://extensionpay.com/*"],
                                     "js": ["ExtPay.js"], "run_at": "document_start"}]}
    files = {"manifest.json": json.dumps(manifest).encode(),
             "src/background.js": b"importScripts('../ExtPay.js');\nexport const n = 1;\n",
             "src/count.js": b"export function count(t) { return t.split(/\\s+/).length; }\n",
             "test/count.test.js": b"import test from 'node:test';\n",
             "package.json": b"{}", "README.md": b"# Tab Word Count\n"}
    if extpay:
        files["ExtPay.js"] = b"/* ExtensionPay library (MIT) */\n"
    files.update(extra or {})
    return files


class FakeHttp:
    """The crew's GET-only client: URL -> (status, body bytes); unknown URLs are 404."""

    def __init__(self, pages: dict | None = None, *, down: bool = False) -> None:
        self.pages = dict(pages or {})
        self.down = down
        self.calls: list = []

    def get(self, url: str, *, headers=None, timeout: float = 20.0) -> HttpResponse:
        self.calls.append(url)
        if self.down:
            raise HttpUnreachable("connection refused")
        status, body = self.pages.get(url, (404, b"not found"))
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        return HttpResponse(status, body, 5.0, {})


class Response:
    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self._raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def read(self, _n: int = -1) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


class Recorder:
    """``opener(request, timeout=...)``: records each request and answers with ``route``,
    a function (method, url, headers, body) -> (status, payload)."""

    def __init__(self, route) -> None:
        self.route = route
        self.calls: list = []

    def __call__(self, request, timeout: float = 30.0):
        headers = {k.lower(): v for k, v in request.header_items()}
        body = request.data
        call = {"method": request.get_method(), "url": request.full_url, "headers": headers,
                "body": body}
        self.calls.append(call)
        status, payload = self.route(call)
        if status >= 400:
            raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            import io
            raise urllib.error.HTTPError(request.full_url, status, "error", {},
                                         io.BytesIO(raw))
        return Response(status, payload)

    def json_body(self, i: int):
        return json.loads(self.calls[i]["body"].decode("utf-8"))


def isolate(case) -> None:
    """The Marketplaces folder follows the test's Builds folder, never the owner's
    ``PIONIR_MARKETPLACES_DIR``."""
    import os
    from unittest import mock

    patcher = mock.patch.dict(os.environ)
    patcher.start()
    os.environ.pop("PIONIR_MARKETPLACES_DIR", None)
    case.addCleanup(patcher.stop)
