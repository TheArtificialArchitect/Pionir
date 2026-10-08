"""From a marketplace's public listings to scored gaps: pure functions, no I/O.

A **listing** is one product as the store shows it publicly::

    {"market", "key", "title", "category", "users", "rating", "ratings", "url", "text"}

``users`` is the store's own demand figure (Apify: users in the last 30 days; Chrome: the
user count on its page; Shopify: the review count, the only public proxy), ``rating`` 0-5,
``ratings`` how many ratings back it. A figure the store did not show is None - UNKNOWN,
never a zero.

A **niche** is a phrase (one or two words) that at least ``MIN_LISTINGS`` listings in the same
marketplace carry in their titles - the ideas come from what the stores measure, not from a
list here. A niche scores high when many people use what is there (demand), what is there
is rated poorly (quality) and there is not much of it (crowding)::

    score = log10(1 + demand) * (QUALITY_CEILING - quality) / (1 + CROWD_WEIGHT * log2(1 + n))
            * PREFERRED_BOOST (when its words are in PREFERRED)

``PREFERRED`` only WEIGHTS a niche the data already found (documents, SEO and AI visibility,
data conversion - the owner's research on what sells on these stores); it never adds one.

A niche that touches people's personal data (``PERSONAL``) is never a candidate, whatever its
score: the owner's rule is no scraping of personal data, and the Apify Store's own top sellers
are full of exactly that.
"""
from __future__ import annotations

import math
import re

MIN_LISTINGS = 2
MAX_NICHES = 40
QUALITY_CEILING = 1.15
CROWD_WEIGHT = 0.35
UNKNOWN_QUALITY = 0.8          # a niche with no rating at all is assumed decent, not poor
PREFERRED_BOOST = 1.3
TOP_FOR_QUALITY = 5

_WORD = re.compile(r"[a-z0-9]+(?:[.+#][a-z0-9]+)?")

# Words that say nothing about what a product does.
STOPWORDS = frozenset(["a", "an", "and", "the", "of", "for", "to", "in", "on", "with", "by", "from", "at", "as", "or", "your", "you", "my", "our", "it", "its", "is", "are", "be", "this", "that", "these", "those", "all", "any", "new", "free", "best", "top", "pro", "plus", "lite", "easy", "fast", "simple", "smart", "quick", "ultimate", "super", "tool", "tools", "app", "apps", "extension", "extensions", "actor", "actors", "api", "apis", "chrome", "shopify", "apify", "one", "get", "set", "use", "using", "via", "v1", "v2", "v3", "2024", "2025", "2026", "online", "web", "website", "site", "helper", "manager", "maker", "generator", "assistant", "ai-powered", "powered", "no", "code"])

# A niche whose listings mention any of these deals in people's personal data.
PERSONAL = frozenset(["email", "emails", "e-mail", "phone", "phones", "contact", "contacts", "lead", "leads", "linkedin", "instagram", "facebook", "tiktok", "twitter", "x.com", "threads", "snapchat", "pinterest", "whatsapp", "telegram", "profile", "profiles", "people", "person", "persons", "followers", "follower", "influencer", "influencers", "employee", "employees", "resume", "resumes", "cv", "dating", "reviewer", "reviewers", "reviews", "address", "addresses", "skiptrace", "username", "usernames"])

# The owner's research (2026-10): what sells on these stores without touching anyone's data.
PREFERRED = frozenset(["pdf", "pdfs", "document", "documents", "docx", "convert", "converter", "conversion", "markdown", "html", "text", "ocr", "seo", "sitemap", "schema", "metadata", "meta", "robots", "audit", "lighthouse", "accessibility", "visibility", "llm", "llms", "gpt", "chatgpt", "summarize", "summary", "embedding", "embeddings", "token", "tokens", "prompt", "json", "csv", "xml", "yaml", "excel", "xlsx", "dataset", "datasets", "data", "validate", "validator", "format", "formatter", "image", "images", "compress", "resize", "screenshot", "qr", "barcode", "color", "colors"])


def words(text: str) -> list:
    return _WORD.findall((text or "").lower())


def phrases(title: str) -> set:
    """The one- and two-word phrases a title offers as a niche."""
    ws = [w for w in words(title) if w not in STOPWORDS and len(w) > 1 and not w.isdigit()]
    out = set(ws)
    out.update(f"{a} {b}" for a, b in zip(ws, ws[1:], strict=False))
    return out


def personal(text: str) -> str | None:
    """The first personal-data word in ``text``, or None."""
    for w in words(text):
        if w in PERSONAL:
            return w
    return None


def _quality(listings: list) -> float:
    """0..1: how good the most-used listings already are (rating / 5, weighted by users)."""
    top = sorted(listings, key=lambda x: -(x.get("users") or 0))[:TOP_FOR_QUALITY]
    rated = [(x["rating"], max(1, x.get("users") or 1)) for x in top
             if isinstance(x.get("rating"), (int, float)) and x.get("ratings")]
    if not rated:
        return UNKNOWN_QUALITY
    total = sum(w for _r, w in rated)
    return max(0.0, min(1.0, sum(r * w for r, w in rated) / total / 5.0))


def niches(listings: list, *, min_listings: int = MIN_LISTINGS,
           max_niches: int = MAX_NICHES) -> list:
    """Every niche in one marketplace's listings, best first, as dicts::

        {"niche", "market", "listings": n, "demand", "quality", "score", "preferred",
         "personal": word or None, "category", "sample": [{title, users, rating, url}]}

    A niche found inside a bigger one with the same listings is dropped (``image`` beside
    ``image compress`` is the same niche twice)."""
    by_phrase: dict = {}
    for item in listings:
        for p in phrases(item.get("title") or ""):
            by_phrase.setdefault(p, []).append(item)
    found = []
    for phrase, items in by_phrase.items():
        uniq = list({x["key"]: x for x in items}.values())
        if len(uniq) < min_listings:
            continue
        demand = sum(int(x.get("users") or 0) for x in uniq)
        if demand <= 0:
            continue
        quality = _quality(uniq)
        n = len(uniq)
        score = math.log10(1 + demand) * (QUALITY_CEILING - quality) / (
            1 + CROWD_WEIGHT * math.log2(1 + n))
        preferred = bool(set(phrase.split()) & PREFERRED)
        if preferred:
            score *= PREFERRED_BOOST
        why_personal = None
        for x in uniq:
            why_personal = personal(f"{x.get('title')} {x.get('text') or ''}")
            if why_personal:
                break
        why_personal = why_personal or personal(phrase)
        cats: dict = {}
        for x in uniq:
            for c in (x.get("category") or "").split(","):
                if c.strip():
                    cats[c.strip()] = cats.get(c.strip(), 0) + 1
        top = sorted(uniq, key=lambda x: -(x.get("users") or 0))
        found.append({
            "niche": phrase, "market": uniq[0].get("market"), "listings": n,
            "demand": demand, "quality": round(quality, 3), "score": round(score, 4),
            "preferred": preferred, "personal": why_personal,
            "category": max(cats, key=lambda c: (cats[c], c)) if cats else "",
            "keys": sorted(x["key"] for x in uniq),
            "sample": [{"title": x.get("title"), "users": x.get("users"),
                        "rating": x.get("rating"), "ratings": x.get("ratings"),
                        "url": x.get("url")} for x in top[:5]]})
    # at equal scores the more specific phrase first: "sitemap audit" names the niche better
    # than "audit" does when the same listings carry both
    found.sort(key=lambda d: (-d["score"], -len(d["niche"].split()), d["niche"]))
    kept: list = []
    for d in found:
        keys = set(d["keys"])
        if any(keys <= set(k["keys"]) and (d["niche"] in k["niche"] or k["niche"] in d["niche"])
               for k in kept):
            continue
        if any(_overlap(keys, set(k["keys"])) > 0.8 for k in kept):
            continue
        kept.append(d)
        if len(kept) >= max_niches:
            break
    return kept


def _overlap(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def slugify(text: str, *, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:limit].strip("-")
