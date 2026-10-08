"""Marketplaces: the listing drafts (apify_pack, chrome_pack, shopify_pack, images).

Each test fails if its rule is reverted: a wrapper that does not compile, charges for a failed
record or ignores the spending limit; a listing that is not deterministic; a privacy answer
that hides what ExtensionPay collects; remote code or a host permission let through; a
Shopify field over the store's limit; or the AI-built disclosure missing.
"""
from __future__ import annotations

import ast
import io
import json
import unittest
import zipfile

from marketplace_support import (
    apify_spec,
    chrome_build,
    chrome_spec,
    core_build,
    shopify_spec,
)

from pionir.crew.marketplaces import apify_pack, chrome_pack, shopify_pack


def unzip(data: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {i.filename: zf.read(i) for i in zf.infolist()}


class ApifyPackTests(unittest.TestCase):
    def test_the_actor_compiles_and_charges_only_good_results(self) -> None:
        for io_mode in ("fetch_urls", "items"):
            spec = apify_spec()
            spec["listing"]["io"] = io_mode
            listing, files, problems = apify_pack.draft(spec, core_build())
            self.assertEqual(problems, [])
            src = unzip(files[apify_pack.PACKAGE])
            main = src["src/main.py"].decode()
            ast.parse(main)
            ast.parse(src["src/__main__.py"].decode())
            emit = main[main.index("async def _emit"):main.index("async def main")]
            self.assertLess(emit.index('if "error" in result'), emit.index("Actor.charge"))
            self.assertIn("event_charge_limit_reached", emit)
            actor = json.loads(src[".actor/actor.json"])
            self.assertEqual((actor["actorSpecification"], actor["name"], actor["input"]),
                             (1, "sitemap-audit", "./input_schema.json"))
            schema = json.loads(src[".actor/input_schema.json"])
            key = "startUrls" if io_mode == "fetch_urls" else "items"
            self.assertEqual(schema["required"], [key])
            self.assertIn("apify/actor-python", src[".actor/Dockerfile"].decode())
            self.assertEqual("httpx" in src["requirements.txt"].decode(),
                             io_mode == "fetch_urls")

    def test_the_store_page_is_the_specs_words_with_the_exact_price(self) -> None:
        listing, _files, _ = apify_pack.draft(apify_spec(), core_build())
        readme = listing["readme_md"]
        for line in apify_spec()["features"]:
            self.assertIn(line, readme)
        self.assertIn("$0.0030 per successful result", readme)
        self.assertIn("$3.00 per 1,000 results", readme)
        self.assertIn(apify_pack.DISCLOSURE, readme)
        self.assertLessEqual(len(listing["seo_description"]), 156)

    def test_the_same_build_gives_the_same_bytes(self) -> None:
        a = apify_pack.draft(apify_spec(), core_build())
        b = apify_pack.draft(apify_spec(), core_build())
        self.assertEqual(a[0], b[0])
        self.assertEqual(a[1], b[1])

    def test_no_core_no_actor(self) -> None:
        core = {k: v for k, v in core_build().items() if "__init__" not in k}
        with self.assertRaises(ValueError):
            apify_pack.draft(apify_spec(), core)


class ChromePackTests(unittest.TestCase):
    def test_the_package_ships_the_extension_and_its_icons_only(self) -> None:
        listing, files, problems = chrome_pack.draft(chrome_spec(), chrome_build())
        self.assertEqual(problems, [])
        shipped = unzip(files[chrome_pack.PACKAGE])
        self.assertIn("manifest.json", shipped)
        self.assertIn("icons/icon128.png", shipped)
        self.assertFalse(any(k.startswith("test/") or k == "package.json" for k in shipped))
        manifest = json.loads(shipped["manifest.json"])
        self.assertEqual(manifest["icons"]["128"], "icons/icon128.png")
        self.assertEqual(manifest["permissions"], ["activeTab", "scripting", "storage"])

    def test_the_privacy_answers_say_what_extensionpay_collects(self) -> None:
        listing, _f, _p = chrome_pack.draft(chrome_spec(), chrome_build())
        usage = listing["data_usage"]
        self.assertTrue(usage["collected"]["Personally identifiable information"])
        self.assertTrue(usage["collected"]["Financial and payment information"])
        self.assertFalse(usage["collected"]["Web history"])
        self.assertTrue(all(usage["certify"].values()))
        self.assertEqual(set(listing["permissions"]), {"activeTab", "scripting"})
        self.assertTrue(all(listing["permissions"].values()))
        self.assertIn(chrome_pack.DISCLOSURE, listing["description"])
        self.assertLessEqual(len(listing["summary"]), 132)

    def test_remote_code_host_access_and_a_missing_extpay_are_refused(self) -> None:
        cases = {
            "remote script": chrome_build(extra={
                "popup.html": b'<script src="https://cdn.example.com/x.js"></script>'}),
            "eval": chrome_build(extra={"src/x.js": b"eval(code)"}),
            "no ExtPay.js": chrome_build(extpay=False),
        }
        hosts = json.loads(chrome_build()["manifest.json"])
        hosts["host_permissions"] = ["<all_urls>"]
        cases["host permission"] = {**chrome_build(),
                                    "manifest.json": json.dumps(hosts).encode()}
        for name, build in cases.items():
            _l, _f, problems = chrome_pack.draft(chrome_spec(), build)
            self.assertTrue(problems, name)


class ShopifyPackTests(unittest.TestCase):
    def test_the_pack_fits_the_stores_limits_and_says_nothing_was_sent(self) -> None:
        spec = shopify_spec()
        spec["summary"] = "Lists the product images in a store that have no alt text, product " \
                          "by product, with a link to each product to fix it quickly."
        listing, files, problems = shopify_pack.draft(spec)
        self.assertEqual(problems, [])
        self.assertLessEqual(len(listing["app_name"]), 30)
        self.assertLessEqual(len(listing["introduction"]), 100)
        self.assertLessEqual(len(listing["details"]), 500)
        self.assertTrue(all(len(f) <= 80 for f in listing["features"]))
        text = files[shopify_pack.PACK].decode()
        self.assertIn("NOT submitted", text)
        self.assertIn("$19", text)
        self.assertIn(shopify_pack.DISCLOSURE, text)
        self.assertEqual(len(files["app-icon.png"]) > 0, True)


if __name__ == "__main__":
    unittest.main()
