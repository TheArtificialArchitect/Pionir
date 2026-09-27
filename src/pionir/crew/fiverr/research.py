"""The research worker for Fiverr "Find it for me" orders: the finder's own research, as a
report file for the owner to upload.

It is the finder (crew/finder.py) with a different envelope: the same prompt (the buyer's
brief between marker lines, as data), the same Claude call with the web tools only
(``ctx.research``, the daily Claude cap), the same fail-closed validation
(``finder.validate_research``: https product pages on public hosts, no email address or phone
number, prices as numbers or absent) and the same screens (``orders.screen``: no finding
people, no weapons, drugs, counterfeits ...). Only the output differs: a Markdown report
(``research-report.md``) instead of an email, since the owner delivers it on Fiverr himself.

**Every listing is verified before it is reported** (``verify_options``): its page is fetched
read-only through the SSRF-safe client (safehttp.py - public addresses only, pinned, every
redirect checked), and the option is kept only when the page answers 2xx, is still on the
listing's own host (``www.`` aside), names the item (at least two of its distinctive words, or
its only one) and - when a price is given - shows that price. Anything else is dropped and
the owner's card says which and why; when no option survives, Claude is asked once more with
the reasons, and a second failure is the owner's. So the gig's words "opened and checked
before you get it" are true of every link in a report.
"""
from __future__ import annotations

import html
import http.client
import re
import ssl

from ..finder import _price, build_prompt, quoted_item
from . import safehttp

_TAG = re.compile(r"<[^>]*>")
_WORD = re.compile(r"[a-z][a-z0-9-]{3,}")
STOPWORDS = frozenset({
    "find", "found", "looking", "look", "want", "need", "needs", "please", "thanks",
    "thank", "would", "like", "with", "from", "that", "this", "these", "those", "have",
    "where", "which", "buy", "buying", "used", "new", "size", "colour", "color", "item",
    "items", "some", "also", "they", "them", "their", "there", "here", "into", "only",
    "just", "very", "must", "should", "could", "want", "wanted", "sell", "sells", "shop",
    "store", "stores", "online", "uk", "usa", "europe", "country", "region", "price",
    "cheap", "cheapest", "best", "good", "condition", "original", "genuine", "exact",
    "exactly", "specific", "model", "brand", "version", "request", "buyer", "asked",
    "revision", "notes", "seller", "original", "about", "under", "over", "than", "more",
    "less", "each", "every", "other", "another", "same", "different", "within", "without"})

REPORT = """# Your research report

You asked for:

> {item}

{summary}

## Where to buy it

{options}
{caveats}
Prices and stock change quickly - please check the listing before you buy.
"""
NOT_FOUND = """# Your research report

You asked for:

> {item}

**We couldn't find it.**

{summary}
{caveats}"""


def prompt(brief: str, reasons=()) -> str:
    """The finder's prompt, with this brief as its data."""
    return build_prompt({"brief": brief}, reasons)


def build_report(brief: str, data: dict) -> str:
    """The Markdown report around checked research (``finder.validate_research``)."""
    item = quoted_item(brief)
    caveats = f"\n**Please note:** {data['caveats']}\n" if data.get("caveats") else ""
    if not data["found"]:
        return NOT_FOUND.format(item=item, summary=data["summary"], caveats=caveats)
    blocks = []
    for n, o in enumerate(data["options"], 1):
        details = ", ".join(x for x in (o["condition"], o["availability"]) if x)
        line = (f"{n}. **{o['seller']}** - {_price(o['price'], o['currency'])}"
                + (f" ({details})" if details else "") + f"  \n   {o['url']}")
        if o["notes"]:
            line += f"  \n   {o['notes']}"
        blocks.append(line)
    return REPORT.format(item=item, summary=data["summary"], options="\n".join(blocks),
                         caveats=caveats)


# ---- verifying the listings -------------------------------------------------------------------
def item_words(item: str) -> list:
    """The item's distinctive words (lower case, 4+ letters, not a common word)."""
    words = []
    for w in _WORD.findall((item or "").lower()):
        if w not in STOPWORDS and w not in words:
            words.append(w)
    return words


def price_forms(price) -> list:
    """How a page may write this price: 1299.99, 1,299.99, 1299,99, 1.299,99 (and the whole
    number, when it is one)."""
    if price is None:
        return []
    value = float(price)
    forms = {f"{value:.2f}", f"{value:,.2f}", f"{value:.2f}".replace(".", ","),
             f"{value:,.2f}".replace(",", " ").replace(".", ",").replace(" ", ".")}
    if value.is_integer():
        forms |= {f"{int(value)}", f"{int(value):,}", f"{int(value):,}".replace(",", ".")}
    return sorted(forms)


def page_text(body: bytes) -> str:
    text = body.decode("utf-8", "replace")
    text = re.sub(r"(?is)<(script|style)\b.*?</\1\s*>", " ", text)
    return " ".join(html.unescape(_TAG.sub(" ", text)).split()).lower()


def listing_problem(option: dict, words: list, fetch: safehttp.SafeHttp) -> str | None:
    """Why this listing is not verified, or None when it is."""
    url = option["url"]
    try:
        host, _target = fetch.check(url)
        answer = fetch.get(url, timeout=20.0)
    except safehttp.Refused as exc:
        return f"not fetched: {exc}"
    except (OSError, ssl.SSLError, ValueError, http.client.HTTPException) as exc:
        return f"did not answer ({type(exc).__name__})"
    if not 200 <= answer.status < 300:
        return f"answered HTTP {answer.status}"
    final = (answer.url.split("://", 1)[-1].split("/", 1)[0]).lower()
    if final.removeprefix("www.") != host.removeprefix("www."):
        return f"moved to another site ({final[:60]})"
    text = page_text(answer.body)
    need = min(2, len(words))
    seen = [w for w in words if re.search(r"(?<![a-z0-9])" + re.escape(w), text)]
    if len(seen) < need:
        return "the page does not name the item"
    forms = price_forms(option.get("price"))
    if forms and not any(re.search(r"(?<![0-9.,])" + re.escape(f) + r"(?![0-9])", text)
                         for f in forms):
        return f"the page does not show the price {_price(option['price'], option['currency'])}"
    return None


def verify_options(item: str, data: dict, fetch: safehttp.SafeHttp) -> tuple:
    """``(data with only the verified options, [notes])``. A not-found answer has nothing to
    verify. When every option fails, the data comes back with ``options == []`` and
    ``found`` still true - the caller treats that as research that did not hold up."""
    if not data.get("found"):
        return data, []
    words = item_words(item)
    kept, notes = [], []
    for option in data["options"]:
        why = listing_problem(option, words, fetch)
        if why is None:
            kept.append(option)
        else:
            notes.append(f"{option['seller']}: {why}")
    return {**data, "options": kept}, notes
