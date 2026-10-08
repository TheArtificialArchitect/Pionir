"""A Chrome Web Store package and listing pack from an approved extension build.

The package (``extension.zip``) is the build's own files minus what does not ship to Chrome
(tests, package.json, bin/), with the generated icons added and ``manifest.json``'s ``icons``
pointed at them - nothing else in the manifest changes. Every rule below is checked fail
closed before a draft exists:

- Manifest V3; the permissions are EXACTLY the spec's (plus ``storage`` for ExtensionPay),
  never a host permission, never ``<all_urls>``; a content script only on extensionpay.com
  (ExtensionPay's own page); no ``unsafe-eval`` in a content security policy;
- no remote code: no ``<script src="http...">``, no ``import("http...")``, no ``eval(`` or
  ``new Function(``; a freemium extension must ship ``ExtPay.js`` (ExtensionPay's library);
- one extension per purpose: the Store's spam policy forbids publishing two extensions with
  the same functionality - the packager refuses a second draft for a niche already drafted.

The listing pack holds what the owner pastes into the developer dashboard the ONE time he
creates the item by hand (the API cannot create an item or edit listing text): the name,
summary (132 characters at most), the description with the AI-built disclosure, the single
purpose, a justification per permission, the remote-code answer, the data-usage answers
(honest: nothing is collected by the extension; with ExtensionPay, a paying user's email and
payment are handled by ExtensionPay and Stripe), the icon, a screenshot and the small promo
tile, all drawn from the spec.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile

from pionir.adapters.marketplace_listing import claim_problems

from ..builds.package import STAMP
from . import images

PACKAGE = "extension.zip"
DISCLOSURE = ("This extension was written with the help of an AI coding system; its tests "
              "were run and its code reviewed before release.")
SKIP = ("test/", "tests/", "bin/", "node_modules/", "docs/")
SKIP_FILES = frozenset({"package.json", "package-lock.json", "CHANGELOG.md", "BRIEF.md"})
DATA_CATEGORIES = ("Personally identifiable information", "Health information",
                   "Financial and payment information", "Authentication information",
                   "Personal communications", "Location", "Web history", "User activity",
                   "Website content")
_REMOTE = re.compile(r"""(?ix)<script[^>]+src\s*=\s*["']\s*https?: | import\(\s*["']https?: |
                         \beval\s*\( | \bnew\s+Function\s*\( | importScripts\(\s*["']https?:""")
EXTPAY_MATCH = "https://extensionpay.com/*"


def _zip(files: dict) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for rel in sorted(files):
            info = zipfile.ZipInfo(rel, date_time=STAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            zf.writestr(info, files[rel], compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return out.getvalue()


def manifest_problems(manifest, spec: dict, files: dict) -> list:
    if not isinstance(manifest, dict):
        return ["manifest.json is not a JSON object"]
    out = []
    lst = spec["listing"]
    freemium = bool(lst.get("freemium"))
    if manifest.get("manifest_version") != 3:
        out.append("manifest_version must be 3")
    if not isinstance(manifest.get("version"), str) or not re.fullmatch(
            r"\d+(\.\d+){0,3}", manifest["version"]):
        out.append("version must be 1 to 4 dot-separated numbers")
    want = set(lst.get("permissions") or []) | ({"storage"} if freemium else set())
    have = set(manifest.get("permissions") or [])
    if have - want:
        out.append(f"permissions the spec does not name: {', '.join(sorted(have - want))}")
    for key in ("host_permissions", "optional_host_permissions", "optional_permissions"):
        if manifest.get(key):
            out.append(f"{key} must be empty (no access beyond what the spec names)")
    for cs in manifest.get("content_scripts") or []:
        matches = cs.get("matches") if isinstance(cs, dict) else None
        if not freemium or matches != [EXTPAY_MATCH]:
            out.append("a content script may only run on ExtensionPay's own page")
    csp = json.dumps(manifest.get("content_security_policy") or "")
    if "unsafe-eval" in csp or "http" in csp:
        out.append("the content security policy may not allow eval or remote sources")
    for rel, data in files.items():
        if rel.endswith((".js", ".mjs", ".html")) and not rel.endswith("ExtPay.js"):
            m = _REMOTE.search(data.decode("utf-8", "replace"))
            if m:
                out.append(f"{rel} loads or evaluates code at run time ({m.group(0)[:40]!r})")
    if freemium and not any(rel.endswith("ExtPay.js") for rel in files):
        out.append("a freemium extension must ship ExtPay.js (ExtensionPay's library)")
    return out


def data_usage(spec: dict) -> dict:
    freemium = bool(spec["listing"].get("freemium"))
    collected = {c: False for c in DATA_CATEGORIES}
    notes = {}
    if freemium:
        collected["Personally identifiable information"] = True
        collected["Financial and payment information"] = True
        notes = {"Personally identifiable information": "only a paying user's email address, "
                 "entered on ExtensionPay's page and kept by ExtensionPay",
                 "Financial and payment information": "card payments are taken by Stripe "
                 "through ExtensionPay; the extension never sees them"}
    return {"collected": collected, "notes": notes,
            "certify": {"not_sold_to_third_parties": True,
                        "not_used_for_unrelated_purposes": True,
                        "not_used_for_creditworthiness": True}}


def description(spec: dict) -> str:
    fm = spec["listing"].get("freemium") or {}
    free = "\n".join(f"- {f}" for f in fm.get("free") or [])
    pro = "\n".join(f"- {f}" for f in fm.get("pro") or [])
    price = fm.get("price_usd_month")
    paid = (f"\n\nPro (${float(price):.2f} a month, paid through ExtensionPay):\n{pro}"
            if pro and price else "")
    features = "\n".join(f"- {f}" for f in spec["features"])
    return (f"{spec['brief']}\n\nFeatures:\n{features}\n\nFree:\n{free}{paid}\n\n"
            f"Limits: {spec['limits']}\n\n{DISCLOSURE}")


def draft(spec: dict, build: dict) -> tuple:
    """``(listing, {file name: bytes}, problems)`` for one extension build."""
    problems: list = []
    try:
        manifest = json.loads(build.get("manifest.json", b"").decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        manifest = None
        problems.append(f"manifest.json is missing or unreadable ({exc})")
    shipped = {rel: data for rel, data in build.items()
               if not rel.startswith(SKIP) and rel not in SKIP_FILES}
    if manifest is not None:
        problems += manifest_problems(manifest, spec, shipped)
    icons = {size: images.icon(spec["name"], size) for size in (16, 48, 128)}
    for size, data in icons.items():
        shipped[f"icons/icon{size}.png"] = data
    if isinstance(manifest, dict):
        manifest = {**manifest, "icons": {str(s): f"icons/icon{s}.png" for s in icons}}
        shipped["manifest.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
    package = _zip(shipped)
    shot = images.feature_card(spec["name"], spec["summary"], spec["features"], (1280, 800),
                               label="CHROME EXTENSION")
    promo = images.feature_card(spec["name"], spec["summary"], [], (440, 280),
                                label="CHROME EXTENSION")
    extra = {PACKAGE: package, "icon128.png": icons[128], "screenshot-1.png": shot,
             "promo-small.png": promo}
    lst = spec["listing"]
    summary = spec["summary"] if len(spec["summary"]) <= 132 else \
        spec["summary"][:129].rsplit(" ", 1)[0] + "..."
    listing = {
        "slug": spec["slug"], "name": (manifest or {}).get("name") or spec["name"],
        "summary": summary, "description": description(spec), "category": lst.get("category"),
        "language": "en", "version": (manifest or {}).get("version"),
        "single_purpose": lst.get("single_purpose"),
        "permissions": {p: (lst.get("justifications") or {}).get(p) for p in
                        sorted(set(lst.get("permissions") or []))},
        "remote_code": "No, I am not using remote code",
        "data_usage": data_usage(spec),
        "pricing": {"model": "freemium via ExtensionPay (Stripe)", **(lst.get("freemium") or {})},
        "package_name": PACKAGE, "package_sha256": hashlib.sha256(package).hexdigest(),
        "images": {name: hashlib.sha256(data).hexdigest() for name, data in extra.items()
                   if name != PACKAGE},
        "screenshots_note": "screenshot-1.png is a feature card, not a capture of the running "
                            "extension; replace it with a real capture when you have one",
        "disclosure": DISCLOSURE}
    problems += claim_problems({"name": listing["name"], "summary": summary,
                                "description": listing["description"]})
    return listing, extra, problems
