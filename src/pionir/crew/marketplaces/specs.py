"""A marketplace product on paper: the spec, its check, and its hand-off to the night builds.

A spec is what the scout writes for one gap (scout.py): the product's words - asked of the
crew's shared brain, checked here fail closed - and its marketplace fields, plus the
``evidence`` the scout MEASURED (never the model's)::

    {"slug", "product_type", "market", "name", "summary", "brief", "features", "acceptance",
     "limits", "tags", "package", "command", "listing": {...per market...},
     "evidence": {"niche", "listings", "demand", "quality", "score", "sample", "read_at"}}

``listing`` per market:

- ``apify_actor``: ``io`` ("fetch_urls": the Actor fetches each URL the user gives and hands
  the page to the core; "items": the user gives JSON items), ``categories`` (the Store's own
  category names, taken from the competing Actors), ``event`` {name, title, description,
  price_usd} - one pay-per-event charge per successful result.
- ``chrome_extension``: ``category``, ``single_purpose``, ``permissions`` (a small allowed
  set; never a host permission), ``justifications`` {permission: why}, ``freemium``
  {free: [...], pro: [...], price_usd_month}.
- ``shopify_app``: ``category``, ``scopes`` (read scopes only) with ``justifications``,
  ``plans`` [{name, price_usd_month, features}].

``queue_for_build`` is the one hook into the Builds division: a buildable spec becomes a
backlog entry (``product_type`` set, word for word from the spec, the core's contract added)
appended to ``backlog.json``, unless one marketplace entry is already waiting there.
"""
from __future__ import annotations

import re

from pionir.adapters.marketplace_listing import claim_problems

from ..builds import backlog as bl
from . import paths
from .terms import personal

FIELDS = ("slug", "product_type", "market", "name", "summary", "brief", "features",
          "acceptance", "limits", "tags", "package", "command", "listing", "evidence")
LANGUAGE = {"apify_actor": "python", "chrome_extension": "javascript"}
APIFY_IO = ("fetch_urls", "items")
CHROME_PERMISSIONS = frozenset({"activeTab", "storage", "contextMenus", "scripting",
                                "downloads", "clipboardWrite", "sidePanel"})
SHOPIFY_SCOPES = frozenset({"read_products", "read_content", "read_themes",
                            "read_translations", "read_files", "read_inventory"})
MIN_EVENT_USD, MAX_EVENT_USD = 0.0005, 0.05
MAX_PRO_USD = 20.0
_UPPER = re.compile(r"[A-Z][A-Z0-9_]{1,30}")
_LINE_BAD = re.compile(r"[\x00-\x1f\x7f<>]")
_EVENT = re.compile(r"[a-z][a-z0-9-]{1,39}")

CORE_CONTRACT = {
    "fetch_urls": ("The package exposes process(record), returning a dict, used by the Apify Actor: record "
                   "is {url, status, content_type, text} for one fetched page"),
    "items": ("The package exposes process(record), returning a dict, used by the Apify Actor: record is "
              "one JSON object the user supplied"),
}
CORE_TEST = ("process() returns a dict with an error key, never raises, for an empty or "
             "malformed record")
CHROME_CONTRACT = ("Manifest V3 extension: manifest.json at the root, its logic in src/ as ES "
                   "modules tested with node:test, no remote code and no network access")


# ---- the words ------------------------------------------------------------------------------
def words_schema(market: str) -> dict:
    """The JSON schema the brain answers a spec in (its marketplace part per market)."""
    line = {"type": "string"}
    lines = {"type": "array", "items": {"type": "string"}}
    listing: dict = {"apify": {
        "type": "object", "properties": {
            "io": {"type": "string", "enum": list(APIFY_IO)},
            "event_title": line, "event_description": line,
            "price_usd_per_result": {"type": "number"}},
        "required": ["io", "event_title", "event_description", "price_usd_per_result"]},
        "chrome": {"type": "object", "properties": {
            "single_purpose": line, "permissions": lines,
            "justifications": {"type": "object"}, "free_features": lines,
            "pro_features": lines, "pro_price_usd_month": {"type": "number"}},
            "required": ["single_purpose", "permissions", "justifications", "free_features",
                         "pro_features", "pro_price_usd_month"]},
        "shopify": {"type": "object", "properties": {
            "scopes": lines, "justifications": {"type": "object"},
            "plans": {"type": "array", "items": {"type": "object"}}},
            "required": ["scopes", "justifications", "plans"]}}[market]
    return {"type": "object", "properties": {
        "feasible": {"type": "boolean"}, "why_not": line,
        "slug": line, "name": line, "summary": line, "brief": line, "features": lines,
        "acceptance": lines, "limits": line, "tags": lines, "listing": listing},
        "required": ["feasible", "slug", "name", "summary", "brief", "features", "acceptance",
                     "limits", "tags", "listing"]}


def assemble(market: str, words: dict, niche: dict, now: float) -> dict:
    """A spec from the brain's words and the scout's measured niche. Pure; checked after."""
    w = words if isinstance(words, dict) else {}
    lst = w.get("listing") if isinstance(w.get("listing"), dict) else {}
    slug = str(w.get("slug") or "").strip().lower()
    spec = {
        "slug": slug, "product_type": paths.PRODUCT_TYPES[market], "market": market,
        "name": str(w.get("name") or "").strip(), "summary": str(w.get("summary") or "").strip(),
        "brief": " ".join(str(w.get("brief") or "").split()),
        "features": [str(x).strip() for x in w.get("features") or [] if isinstance(x, str)],
        "acceptance": [str(x).strip() for x in w.get("acceptance") or []
                       if isinstance(x, str)],
        "limits": " ".join(str(w.get("limits") or "").split()),
        "tags": [str(x).strip().lower() for x in w.get("tags") or [] if isinstance(x, str)],
        "package": slug.replace("-", "_"), "command": slug,
        "evidence": {k: niche.get(k) for k in ("niche", "listings", "demand", "quality",
                                               "score", "category", "sample")}
                    | {"read_at": now, "market": market},
    }
    if market == "apify":
        cats = [c for c in str(niche.get("category") or "").split(",") if c]
        spec["listing"] = {
            "io": lst.get("io"),
            # the Store's own categories, as the competing Actors carry them - never the model's
            "categories": [c for c in cats if _UPPER.fullmatch(c)][:3] or ["DEVELOPER_TOOLS"],
            "event": {"name": "result", "title": str(lst.get("event_title") or "").strip(),
                      "description": str(lst.get("event_description") or "").strip(),
                      "price_usd": lst.get("price_usd_per_result")}}
    elif market == "chrome":
        spec["listing"] = {
            "category": niche.get("category") or "",
            "single_purpose": str(lst.get("single_purpose") or "").strip(),
            "permissions": [p for p in lst.get("permissions") or [] if isinstance(p, str)],
            "justifications": {str(k): str(v) for k, v in
                               (lst.get("justifications") or {}).items()}
            if isinstance(lst.get("justifications"), dict) else {},
            "freemium": {"free": [str(x) for x in lst.get("free_features") or []],
                         "pro": [str(x) for x in lst.get("pro_features") or []],
                         "price_usd_month": lst.get("pro_price_usd_month")}}
    else:
        spec["listing"] = {
            "category": niche.get("category") or "",
            "scopes": [s for s in lst.get("scopes") or [] if isinstance(s, str)],
            "justifications": {str(k): str(v) for k, v in
                               (lst.get("justifications") or {}).items()}
            if isinstance(lst.get("justifications"), dict) else {},
            "plans": [p for p in lst.get("plans") or [] if isinstance(p, dict)][:4]}
    return spec


# ---- the check ---------------------------------------------------------------------------------
def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _text(spec, key, lo, hi, reasons, *, one_line=True) -> None:
    v = spec.get(key)
    if not isinstance(v, str) or not lo <= len(v) <= hi or (one_line and _LINE_BAD.search(v)):
        reasons.append(f"{key} must be {lo} to {hi} characters" +
                       (" on one line, with no < or >" if one_line else ""))


def spec_problems(spec) -> list:
    """Every reason this spec cannot go further; empty is the only pass."""
    if not isinstance(spec, dict):
        return ["the spec is not an object"]
    reasons: list = []
    missing = [k for k in FIELDS if k not in spec]
    if missing:
        reasons.append(f"missing: {', '.join(missing)}")
    market = spec.get("market")
    if market not in paths.MARKETS or spec.get("product_type") != paths.PRODUCT_TYPES.get(market):
        return reasons + ["market and product_type must be one of the three stores"]
    slug = spec.get("slug")
    if not isinstance(slug, str) or not bl.SLUG.fullmatch(slug) or slug in bl.RESERVED:
        reasons.append("slug must be 3 to 40 of a-z, 0-9 and -, and not a shelf product's")
    _text(spec, "name", 5, 80, reasons)
    _text(spec, "summary", 20, 200, reasons)
    _text(spec, "brief", 60, 1400, reasons)
    _text(spec, "limits", 10, 600, reasons)
    for key, lo, hi in (("features", 2, 6), ("acceptance", 3, 10)):
        v = spec.get(key)
        if not isinstance(v, list) or not lo <= len(v) <= hi or not all(
                isinstance(x, str) and 5 <= len(x) <= 240 and not _LINE_BAD.search(x)
                for x in v):
            reasons.append(f"{key} must be {lo} to {hi} lines of 5 to 240 characters")
    tags = spec.get("tags")
    if not isinstance(tags, list) or not 1 <= len(tags) <= 5 or not all(
            isinstance(t, str) and re.fullmatch(r"[a-z0-9-]{2,24}", t) for t in tags):
        reasons.append("tags must be 1 to 5 of a-z, 0-9 and -")
    words = " ".join(str(spec.get(k) or "") for k in ("name", "summary", "brief"))
    words += " " + " ".join(spec.get("features") or [] if isinstance(spec.get("features"),
                                                                     list) else [])
    hit = personal(words)
    if hit:
        reasons.append(f"it touches people's personal data ({hit!r}); never built")
    reasons += claim_problems({k: spec.get(k) for k in ("name", "summary", "brief",
                                                       "features", "limits")})
    lst = spec.get("listing")
    if not isinstance(lst, dict):
        return reasons + ["listing must be an object"]
    if market == "apify":
        reasons += _apify_problems(lst)
    elif market == "chrome":
        reasons += _chrome_problems(lst)
    else:
        reasons += _shopify_problems(lst)
    ev = spec.get("evidence")
    if not isinstance(ev, dict) or not isinstance(ev.get("demand"), int) \
            or not isinstance(ev.get("niche"), str):
        reasons.append("evidence must carry the measured niche and demand")
    return reasons


def _apify_problems(lst: dict) -> list:
    out = []
    if lst.get("io") not in APIFY_IO:
        out.append(f"listing.io must be one of {', '.join(APIFY_IO)}")
    cats = lst.get("categories")
    if not isinstance(cats, list) or not 1 <= len(cats) <= 3 or not all(
            isinstance(c, str) and _UPPER.fullmatch(c) for c in cats):
        out.append("listing.categories must be 1 to 3 of the Store's category names")
    elif {"LEAD_GENERATION", "SOCIAL_MEDIA"} & set(cats):
        out.append("listing.categories may not be lead generation or social media")
    ev = lst.get("event")
    if not isinstance(ev, dict):
        return out + ["listing.event must be an object"]
    if not isinstance(ev.get("name"), str) or not _EVENT.fullmatch(ev["name"]):
        out.append("listing.event.name must be 2-40 of a-z, 0-9 and -")
    for key, lo, hi in (("title", 3, 40), ("description", 10, 200)):
        v = ev.get(key)
        if not isinstance(v, str) or not lo <= len(v) <= hi or _LINE_BAD.search(v):
            out.append(f"listing.event.{key} must be {lo} to {hi} characters on one line")
    price = ev.get("price_usd")
    if not _num(price) or not MIN_EVENT_USD <= float(price) <= MAX_EVENT_USD:
        out.append(f"listing.event.price_usd must be {MIN_EVENT_USD} to {MAX_EVENT_USD} "
                   "dollars per result")
    out += claim_problems({"listing.event": [ev.get("title"), ev.get("description")]})
    return out


def _chrome_problems(lst: dict) -> list:
    out = []
    sp = lst.get("single_purpose")
    if not isinstance(sp, str) or not 20 <= len(sp) <= 300:
        out.append("listing.single_purpose must be 20 to 300 characters")
    perms = lst.get("permissions")
    if not isinstance(perms, list) or len(perms) > 4 or not all(
            p in CHROME_PERMISSIONS for p in perms) or len(set(perms)) != len(perms):
        out.append("listing.permissions must be at most 4 of "
                   + ", ".join(sorted(CHROME_PERMISSIONS)) + " (never a host permission)")
    else:
        just = lst.get("justifications") if isinstance(lst.get("justifications"), dict) else {}
        for p in perms:
            if not isinstance(just.get(p), str) or not 15 <= len(just[p]) <= 300:
                out.append(f"listing.justifications.{p} must say in 15 to 300 characters why "
                           "it is needed")
    fm = lst.get("freemium")
    if not isinstance(fm, dict):
        return out + ["listing.freemium must be an object"]
    for key in ("free", "pro"):
        v = fm.get(key)
        if not isinstance(v, list) or not 1 <= len(v) <= 5 or not all(
                isinstance(x, str) and 5 <= len(x) <= 120 for x in v):
            out.append(f"listing.freemium.{key} must be 1 to 5 features of 5 to 120 characters")
    price = fm.get("price_usd_month")
    if not _num(price) or not 0.99 <= float(price) <= MAX_PRO_USD:
        out.append(f"listing.freemium.price_usd_month must be 0.99 to {MAX_PRO_USD}")
    out += claim_problems({"listing": [sp, *(fm.get("free") or []), *(fm.get("pro") or [])]})
    return out


def _shopify_problems(lst: dict) -> list:
    out = []
    scopes = lst.get("scopes")
    if not isinstance(scopes, list) or not 1 <= len(scopes) <= 4 or not all(
            s in SHOPIFY_SCOPES for s in scopes):
        out.append("listing.scopes must be 1 to 4 READ scopes of "
                   + ", ".join(sorted(SHOPIFY_SCOPES)) + " (no customer or order data)")
    else:
        just = lst.get("justifications") if isinstance(lst.get("justifications"), dict) else {}
        for s in scopes:
            if not isinstance(just.get(s), str) or not 15 <= len(just[s]) <= 300:
                out.append(f"listing.justifications.{s} must say why it is needed")
    plans = lst.get("plans")
    if not isinstance(plans, list) or not 1 <= len(plans) <= 4:
        out.append("listing.plans must be 1 to 4 plans")
    else:
        for i, p in enumerate(plans):
            if not isinstance(p.get("name"), str) or not 2 <= len(p["name"]) <= 30 \
                    or not _num(p.get("price_usd_month")) \
                    or not 0 <= float(p["price_usd_month"]) <= 200:
                out.append(f"listing.plans[{i}] needs a name and a monthly price of 0 to 200")
    return out


# ---- the hand-off to the night builds ------------------------------------------------------------
def buildable(spec: dict) -> str | None:
    """Why the night builds cannot build this spec now, or None."""
    lang = LANGUAGE.get(spec.get("product_type"))
    if lang is None:
        return ("a Shopify app is not built by the night builds yet: the packager prepares its "
                "submission pack from the spec")
    if lang not in bl.LANGUAGES:
        return (f"the night builds have no contained {lang} test runner yet "
                f"(builds/backlog.LANGUAGES is {', '.join(bl.LANGUAGES)}); the spec waits")
    return None


def to_backlog_entry(spec: dict) -> dict:
    """The Builds backlog entry, word for word from the spec, with the core's contract the
    Actor wrapper (or the extension's shell) relies on added as a feature and a test."""
    io = (spec.get("listing") or {}).get("io")
    if spec["product_type"] == "apify_actor":
        extra_feature, extra_test = CORE_CONTRACT.get(io, CORE_CONTRACT["items"]), CORE_TEST
    else:
        extra_feature, extra_test = CHROME_CONTRACT, "manifest.json parses and declares only " \
            "the permissions the brief names"
    return {"slug": spec["slug"], "name": spec["name"], "price_cents": 0,
            "summary": spec["summary"], "tags": list(spec["tags"]),
            "language": LANGUAGE[spec["product_type"]], "package": spec["package"],
            "command": spec["command"], "brief": spec["brief"],
            "features": [*spec["features"], extra_feature],
            "acceptance": [*spec["acceptance"], extra_test],
            "limits": spec["limits"], "product_type": spec["product_type"]}


def waiting_in_backlog(doc: dict, taken: set) -> list:
    """The marketplace entries in the backlog the night builds have not started yet."""
    return [e["slug"] for e in doc["products"]
            if bl.product_type(e) != bl.GUMROAD and e["slug"] not in taken]


def queue_for_build(builds_dir, spec: dict, taken: set) -> tuple:
    """Append this spec to the Builds backlog: ``(True, note)`` or ``(False, why not)``.
    One marketplace entry waits there at a time, so the owner's own products keep their turn
    and a run of scouts never floods it."""
    why = buildable(spec)
    if why:
        return False, why
    entry = to_backlog_entry(spec)
    problems = bl.entry_problems(entry)
    if problems:
        return False, "the backlog would refuse it: " + "; ".join(problems[:3])
    try:
        doc = bl.load(builds_dir)
    except bl.BacklogUnreadable as exc:
        return False, f"the Builds backlog is unreadable ({exc})"
    known = {e.get("slug") for e in doc["raw"]["products"] if isinstance(e, dict)}
    if entry["slug"] in known or entry["slug"] in taken:
        return False, f"{entry['slug']} is already in the backlog or was built"
    waiting = waiting_in_backlog(doc, taken)
    if waiting:
        return False, f"a marketplace product is already waiting for the night builds " \
                      f"({waiting[0]})"
    if len(doc["products"]) >= bl.MAX_ENTRIES:
        return False, "the Builds backlog is full"
    doc["raw"]["products"] = [*doc["raw"]["products"], entry]
    bl.save(builds_dir, doc)
    return True, f"{entry['slug']} added to the Builds backlog"
