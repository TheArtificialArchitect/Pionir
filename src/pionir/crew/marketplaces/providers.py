"""Reading the three stores' PUBLIC catalogues, politely. GET only (``ctx.http``).

- **Apify Store**: the documented public API, ``GET https://api.apify.com/v2/store`` (no
  token): ``data.items[]`` with ``title, name, username, description, categories,
  stats.totalUsers30Days, actorReviewRating, actorReviewCount, currentPricingInfo``.
- **Chrome Web Store**: the public pages robots.txt allows - the home page and category pages
  (each lists items with their average rating) and an item's own page (its user count, rating
  and number of ratings). ``/search`` and ``/detail/*/reviews`` are disallowed by the store's
  robots.txt and are never read; every URL is checked against robots.txt before it is.
- **Shopify App Store**: its home and category pages (each app card carries the app's name,
  rating and review count). Same robots rule.

Politeness: at most ``pages``/``details`` page reads per run, ``pause`` seconds between two
reads of the same host, and nothing is read twice within its ``fresh`` time (the cache is the
scout's own record). No account, no cookie, no personal data: a listing is a product, its
title, its public figures and its address.
"""
from __future__ import annotations

import html
import json
import re
import time
from urllib.parse import quote, urlsplit

from ..net import HttpUnreachable

APIFY_STORE = "https://api.apify.com/v2/store"
CWS = "https://chromewebstore.google.com"
SHOPIFY = "https://apps.shopify.com"
# Only when the store's own pages name no category at all (a fallback, never the source).
CWS_SEED = ("extensions/productivity/tools", "extensions/productivity/developer",
            "extensions/productivity/workflow", "extensions/make_chrome_yours/accessibility")


class ProviderError(RuntimeError):
    """The store could not be read this run (said, never swallowed)."""


# ---- robots.txt ---------------------------------------------------------------------------
def robots_rules(text: str) -> list:
    """The ``Disallow``/``Allow`` rules of the ``User-agent: *`` group, in order."""
    rules, group, star = [], [], False
    seen_rule = False
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, value = (s.strip() for s in line.split(":", 1))
        field = field.lower()
        if field == "user-agent":
            if seen_rule:
                group, star, seen_rule = [], False, False
            group.append(value)
            star = star or value == "*"
        elif field in ("disallow", "allow"):
            seen_rule = True
            if star and value:
                rules.append((field, value))
    return rules


def _pattern(rule: str) -> re.Pattern:
    end = rule.endswith("$")
    body = re.escape(rule[:-1] if end else rule).replace(r"\*", ".*")
    return re.compile(body + ("$" if end else ""))


def robots_allows(rules: list, url: str) -> bool:
    """Longest matching rule wins; an Allow beats a Disallow of the same length."""
    parts = urlsplit(url)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    best = None
    for kind, rule in rules:
        if _pattern(rule).match(path):
            key = (len(rule), kind == "allow")
            if best is None or key > best[0]:
                best = (key, kind)
    return best is None or best[1] == "allow"


class Reader:
    """GETs for one run: robots.txt per host (fail closed: an unreadable robots.txt means
    nothing on that host is read this run), a pause between reads of one host, a count."""

    def __init__(self, http, *, pause: float = 3.0, sleep=time.sleep) -> None:
        self.http = http
        self.pause = float(pause)
        self.sleep = sleep
        self.reads = 0
        self._rules: dict = {}
        self._last: dict = {}

    def _robots(self, host_url: str) -> list:
        if host_url not in self._rules:
            resp = self._get(host_url + "/robots.txt", check=False)
            if resp.status == 404:
                self._rules[host_url] = []
            elif resp.status != 200:
                raise ProviderError(f"{host_url}/robots.txt answered HTTP {resp.status}; "
                                    "nothing on that host is read without it")
            else:
                self._rules[host_url] = robots_rules(resp.body.decode("utf-8", "replace"))
        return self._rules[host_url]

    def _get(self, url: str, *, check: bool = True, headers: dict | None = None):
        parts = urlsplit(url)
        host = f"{parts.scheme}://{parts.netloc}"
        if check and not robots_allows(self._robots(host), url):
            raise ProviderError(f"robots.txt disallows {url}; not read")
        last = self._last.get(host)
        if last is not None and self.pause > 0:
            self.sleep(self.pause)
        try:
            resp = self.http.get(url, headers={"Accept-Language": "en", **(headers or {})},
                                 timeout=30.0)
        except HttpUnreachable as exc:
            raise ProviderError(f"{host} did not answer: {exc}") from exc
        finally:
            self._last[host] = time.monotonic()
            self.reads += 1
        return resp

    def get_text(self, url: str) -> str:
        resp = self._get(url)
        if resp.status != 200:
            raise ProviderError(f"{url} answered HTTP {resp.status}")
        return resp.body.decode("utf-8", "replace")

    def get_json(self, url: str):
        resp = self._get(url, check=False, headers={"Accept": "application/json"})
        if resp.status != 200:
            raise ProviderError(f"{url} answered HTTP {resp.status}")
        try:
            return json.loads(resp.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProviderError(f"{url} answered something that is not JSON ({exc})") from exc


# ---- Apify Store ------------------------------------------------------------------------------
def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def apify_item(item: dict) -> dict | None:
    """One Store item -> a listing, or None when it lacks a name."""
    if not isinstance(item, dict):
        return None
    user, name = item.get("username"), item.get("name")
    if not isinstance(user, str) or not isinstance(name, str) or not user or not name:
        return None
    stats = item.get("stats") if isinstance(item.get("stats"), dict) else {}
    cats = item.get("categories") if isinstance(item.get("categories"), list) else []
    pricing = item.get("currentPricingInfo") if isinstance(item.get("currentPricingInfo"),
                                                           dict) else {}
    return {"market": "apify", "key": f"{user}/{name}",
            "title": str(item.get("title") or name)[:120],
            "text": str(item.get("description") or "")[:400],
            "category": ",".join(str(c) for c in cats if isinstance(c, str)),
            "users": _int(stats.get("totalUsers30Days")),
            "rating": _num(item.get("actorReviewRating")),
            "ratings": _int(item.get("actorReviewCount")),
            "pricing": str(pricing.get("pricingModel") or ""),
            "url": f"https://apify.com/{user}/{name}"}


def read_apify(reader: Reader, *, pages: int = 2, page_size: int = 500) -> list:
    """The Store's most popular Actors, ``pages`` x ``page_size`` of them."""
    out = []
    for i in range(max(1, pages)):
        doc = reader.get_json(f"{APIFY_STORE}?limit={page_size}&offset={i * page_size}"
                              "&sortBy=popularity")
        data = doc.get("data") if isinstance(doc, dict) else None
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise ProviderError("the Apify Store answered without data.items")
        out += [x for x in (apify_item(it) for it in items) if x is not None]
        if len(items) < page_size:
            break
    return out


# ---- Chrome Web Store ----------------------------------------------------------------------------
_CWS_ITEM = re.compile(r'href="\./detail/([a-z0-9-]+)/([a-p]{32})"')
_CWS_CATEGORY = re.compile(r'category/(extensions/[a-z_]+/[a-z_]+)')
_STARS = re.compile(r"(\d(?:\.\d)?) out of 5")
_TAG = re.compile(r"<[^>]+>")


def _count(text: str):
    """``21,000,000`` / ``3.6K`` / ``1M`` -> an int; None when it is not a count."""
    m = re.fullmatch(r"\s*([\d,]*\.?\d+)\s*([KM]?)\+?\s*", text or "")
    if not m:
        return None
    try:
        n = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return int(n * {"": 1, "K": 1000, "M": 1_000_000}[m.group(2)])


def cws_categories(page: str) -> list:
    return sorted(set(_CWS_CATEGORY.findall(page)))


def cws_list(page: str, category: str) -> list:
    """The items a Chrome Web Store page lists, with the rating its card shows."""
    out, starts = [], list(_CWS_ITEM.finditer(page))
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else m.end() + 6000
        seg = re.sub(r"\s+", " ", html.unescape(_TAG.sub(" | ", page[m.start():end])))
        bits = [b.strip() for b in seg.split("|") if b.strip()]
        title = next((b for b in bits[1:] if len(b) > 2 and not _STARS.search(b)
                      and not re.fullmatch(r"[\d.]+", b)), m.group(1))
        stars = _STARS.search(seg)
        out.append({"market": "chrome", "key": m.group(2), "title": title[:120], "text": "",
                    "category": category, "users": None,
                    "rating": float(stars.group(1)) if stars else None, "ratings": None,
                    "url": f"{CWS}/detail/{m.group(1)}/{m.group(2)}"})
    return list({x["key"]: x for x in out}.values())


def cws_detail(page: str) -> dict:
    """An item page's public figures: users, rating, ratings, title, description."""
    text = re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", page)))
    users = re.search(r"([\d,.]+[KM]?)\+? users\b", text)
    ratings = re.search(r"([\d,.]+[KM]?) ratings?\b", text)
    stars = _STARS.search(text)
    title = re.search(r'og:title" content="([^"]*)"', page)
    desc = re.search(r'og:description" content="([^"]*)"', page)
    return {"users": _count(users.group(1)) if users else None,
            "ratings": _count(ratings.group(1)) if ratings else None,
            "rating": float(stars.group(1)) if stars else None,
            "title": html.unescape(title.group(1)).replace(" - Chrome Web Store", "")[:120]
            if title else None,
            "text": html.unescape(desc.group(1))[:400] if desc else ""}


# ---- Shopify App Store ------------------------------------------------------------------------------
_SHOP_CARD = re.compile(r'data-app-card-handle-value="([a-z0-9-]+)"')
_SHOP_NAME = re.compile(r'data-app-card-name-value="([^"]*)"')
_SHOP_CATEGORY = re.compile(r'href="https://apps\.shopify\.com/categories/([a-z0-9-]+)"')


def shopify_categories(page: str) -> list:
    return sorted(set(_SHOP_CATEGORY.findall(page)))


def _tagline(text: str) -> str:
    for stop in ("popover", "<", "Built for Shopify", "data-"):
        text = text.split(stop, 1)[0]
    return text.strip()


def shopify_list(page: str, category: str) -> list:
    """The apps a Shopify App Store page lists. ``users`` is the review count - the only
    public demand figure the store shows (labelled so in every report)."""
    out, starts = [], list(_SHOP_CARD.finditer(page))
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else m.end() + 6000
        raw = page[m.start():end]
        name = _SHOP_NAME.search(raw)
        seg = re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", raw)))
        stars = re.search(r"([\d.]+) out of 5 stars", seg)
        reviews = re.search(r"([\d,]+) total reviews", seg)
        tagline = re.search(r"total reviews\s*•\s*([^•]{0,160})", seg)
        n = _count(reviews.group(1)) if reviews else None
        out.append({"market": "shopify", "key": m.group(1),
                    "title": html.unescape(name.group(1))[:120] if name else m.group(1),
                    "text": _tagline(tagline.group(1)) if tagline else "",
                    "category": category, "users": n,
                    "rating": float(stars.group(1)) if stars else None, "ratings": n,
                    "url": f"{SHOPIFY}/{quote(m.group(1))}"})
    return list({x["key"]: x for x in out}.values())
