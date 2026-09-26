"""Affiliate links in "Find it for me" reports: the owner's commission tag on the shop links
the finder found, deterministically, disclosed to the client, shown on the owner's card.

The owner's decision: a find report may earn an affiliate commission, clearly disclosed. The
rules, each pinned by a test:

- **Off unless configured.** No program set up (or one whose value is malformed) rewrites
  nothing: the report is byte-for-byte what it was without this module, with no disclosure.
- **Only a supported retailer's own product page.** Each program lists its stores; a link is
  rewritten only when its host is EXACTLY one of them (``amazon.com`` or ``www.amazon.com``,
  never ``amazon.com.evil.example`` or ``smile.amazon.com``) and its path is that retailer's
  product page (``/dp/<ASIN>``, ``/itm/<id>``). A shortener (``amzn.to``, ``a.co``,
  ``ebay.us``), a redirect, a search or a store page is never rewritten: it cannot be
  resolved here without fetching it, and nothing here fetches anything.
- **Someone else's tag never passes.** The rewritten link is rebuilt from the product id
  alone - every parameter the found link carried (another affiliate's ``tag``, ``campid``,
  ``ascsubtag``, ``utm_*``, ``ref``) is dropped - and then the owner's own is added.
- **The recommendation is untouched.** Rewriting maps each option's link to a link to the
  same product on the same host; it never adds a link the finder did not find, never drops
  one, and never reorders them. Which shop is recommended is Claude's research, checked,
  and is decided before this module is consulted.
- **Amazon's own statement.** A report with an Amazon Associates link also carries, word for
  word, the sentence Amazon's Operating Agreement requires (``AMAZON_STATEMENT``).
- **No model.** Pure string work on the checked research: the same input and settings give
  the same links, every time.

Commissions themselves are NOT observed: they appear only in each program's own reporting
(Amazon Associates Central, the eBay Partner Network portal). The finder counts reports and
links that carried the owner's tag - never revenue.

Settings (the crew's environment, read by ``programs_from_environment``):

- ``PIONIR_AFFILIATE_AMAZON_TAG``: the Amazon Associates tracking id (like ``dokaz-20``).
- ``PIONIR_AFFILIATE_AMAZON_STORES``: the Amazon stores it is for, comma-separated (default
  ``amazon.com``). An Associates tag belongs to ONE store's program; another store's own
  tag is given as ``amazon.co.uk=dokaz-21``.
- ``PIONIR_AFFILIATE_EBAY_CAMPID``: the eBay Partner Network campaign id (10 digits).
- ``PIONIR_AFFILIATE_EBAY_SITES``: the eBay sites it is for (default ``ebay.com``).

A new program is one entry in ``RULES`` (its product-page rule), one in ``_READERS`` (its
settings) and its stores' fixed parameters.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit

from .log import log

# What the client reads, above the links, whenever any link in the report is an affiliate
# link - and never otherwise. Pionir's client.find_report holds the same words
# (adapters/clients.py AFFILIATE_DISCLOSURE; a test pins the two together).
DISCLOSURE = ("Some links below are affiliate links: we may earn a commission if you buy "
              "through them, at no extra cost to you. It doesn't change what we recommend.")
# Amazon's own required statement, word for word, whenever a report carries an Amazon
# Associates link (Associates Program Operating Agreement, section 5: "As an Amazon Associate
# I earn from qualifying purchases." shown clearly and prominently where the links are;
# https://affiliate-program.amazon.com/help/operating/agreement). Pionir's client.find_report
# holds the same words (adapters/clients.py AMAZON_ASSOCIATE_STATEMENT; a test pins them).
AMAZON_STATEMENT = "As an Amazon Associate I earn from qualifying purchases."

# What the finder's tally says about the money, so nobody reads a count as revenue.
COMMISSION_UNSEEN = ("not observed: a commission shows only in the program's own reporting "
                     "(Amazon Associates Central, the eBay Partner Network portal); the counts "
                     "here are reports and links that carried the owner's tag, not revenue")


@dataclass(frozen=True, slots=True)
class Store:
    """One store of a program: its domain, and the query its affiliate link carries."""

    domain: str                                   # e.g. "amazon.com"
    params: tuple[tuple[str, str], ...]           # added, in this order, after the kept ones

    @property
    def hosts(self) -> frozenset:
        return frozenset({self.domain, "www." + self.domain})


@dataclass(frozen=True, slots=True)
class Program:
    key: str                                      # "amazon", "ebay" - names its rule
    name: str                                     # for the owner: "Amazon Associates"
    stores: tuple[Store, ...]
    statement: str = ""                           # a sentence the program requires, if any

    def store_for(self, host: str) -> Store | None:
        return next((s for s in self.stores if host in s.hosts), None)

    def public(self) -> dict:
        return {"program": self.name, "stores": [s.domain for s in self.stores]}


# ---- the product-page rules: the path a found link must have, and the path it gets ---------
# Each takes the found link's path and query and gives (canonical path, kept query pairs),
# or None when the link is not unmistakably one product's page.
_ASIN = r"[A-Z0-9]{10}"
_AMAZON_PRODUCT = re.compile(
    r"/(?:[A-Za-z0-9._~%!$&'()*+,;=:@-]+/)?(?:dp|gp/product|gp/aw/d)/(" + _ASIN + r")"
    r"(?:/(?:ref=[A-Za-z0-9._~%-]*)?)?")
_EBAY_ITEM = re.compile(r"/itm/(?:[A-Za-z0-9._~%-]+/)?(\d{9,15})/?")
_DIGITS = re.compile(r"\d{1,20}")


def _amazon(path: str, query: str) -> tuple[str, list] | None:
    m = _AMAZON_PRODUCT.fullmatch(path)
    return (f"/dp/{m.group(1)}", []) if m else None


def _ebay(path: str, query: str) -> tuple[str, list] | None:
    m = _EBAY_ITEM.fullmatch(path)
    if not m:
        return None
    # ``var`` picks the variation of a multi-variation listing: the product, not tracking
    kept = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True)
            if k == "var" and _DIGITS.fullmatch(v)][:1]
    return f"/itm/{m.group(1)}", kept


RULES: dict[str, Callable[[str, str], tuple[str, list] | None]] = {
    "amazon": _amazon, "ebay": _ebay}


def rewrite(url: str, programs: Iterable[Program]) -> str | None:
    """The affiliate link for ``url`` under the first program whose store it is on and
    whose product page it is, or None: leave the link exactly as found."""
    if not isinstance(url, str) or not url.startswith("https://"):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or port is not None or "@" in parts.netloc \
            or "\\" in url or parts.netloc != parts.netloc.lower():
        return None
    host = parts.netloc
    for program in programs:
        store = program.store_for(host)
        rule = RULES.get(program.key)
        if store is None or rule is None:
            continue
        got = rule(parts.path, parts.query)
        if got is None:
            return None
        path, kept = got
        return f"https://{host}{path}?{urlencode([*kept, *store.params])}"
    return None


def is_amazon(url) -> bool:
    """True for a link on one of Amazon's own stores (www. or not)."""
    try:
        host = urlsplit(url).netloc if isinstance(url, str) else ""
    except ValueError:
        return False
    return host.removeprefix("www.") in AMAZON_STORES


def statements(links: Iterable[str], programs: Iterable[Program]) -> list[str]:
    """The statements the programs require for these affiliate links, once each, in the
    programs' order: Amazon's only when an Amazon link is among them."""
    hosts = set()
    for link in links:
        try:
            hosts.add(urlsplit(link).netloc)
        except ValueError:
            continue
    return list(dict.fromkeys(p.statement for p in programs if p.statement
                              and any(p.store_for(h) for h in hosts)))


def apply(options: list, programs: Iterable[Program]) -> tuple[list, list]:
    """``(options, affiliate_urls)``: the same options in the same order, each link either
    exactly as found or its affiliate link. A link whose affiliate form another option
    already has (two links to one product) is left as found: a report lists a link once."""
    programs = tuple(programs)
    if not programs:
        return list(options), []
    found = {o["url"] for o in options}
    out, affiliate, used = [], [], set()
    for o in options:
        new = rewrite(o["url"], programs)
        if new is None or new in used or (new != o["url"] and new in found):
            out.append(o)
            used.add(o["url"])
            continue
        used.add(new)
        affiliate.append(new)
        out.append({**o, "url": new})
    return out, affiliate


# ---- the settings -------------------------------------------------------------------------
AMAZON_STORES = frozenset({
    "amazon.com", "amazon.ca", "amazon.com.mx", "amazon.com.br", "amazon.co.uk", "amazon.de",
    "amazon.fr", "amazon.it", "amazon.es", "amazon.nl", "amazon.se", "amazon.pl",
    "amazon.com.be", "amazon.ie", "amazon.co.jp", "amazon.in", "amazon.com.au", "amazon.sg",
    "amazon.ae", "amazon.sa", "amazon.com.tr", "amazon.eg"})
# eBay Partner Network: each marketplace's rotation id (mkrid), copied from eBay's own table
# in "Creating an EPN Tracking Link" (https://developer.ebay.com/api-docs/buy/static/
# ref-epn-link.html, as archived 2026-01-25). The link is the target URL
# (https://www.ebay.<tld>/itm/<listing id>?var=<variation id>) followed by, in the page's own
# order, mkevt=1 (click), mkcid=1 (EPN), mkrid, campid and toolid=10001 (the documented
# default). A site not in that table cannot be configured.
EBAY_SITES = {
    "ebay.at": "5221-53469-19255-0",
    "ebay.com.au": "705-53470-19255-0",
    "ebay.be": "1553-53471-19255-0",
    "ebay.ca": "706-53473-19255-0",
    "ebay.ch": "5222-53480-19255-0",
    "ebay.de": "707-53477-19255-0",
    "ebay.es": "1185-53479-19255-0",
    "ebay.fr": "709-53476-19255-0",
    "ebay.ie": "5282-53468-19255-0",
    "ebay.co.uk": "710-53481-19255-0",
    "ebay.it": "724-53478-19255-0",
    "ebay.nl": "1346-53482-19255-0",
    "ebay.pl": "4908-226936-19255-0",
    "ebay.com": "711-53200-19255-0",
}
_AMAZON_TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}")
_EBAY_CAMPID = re.compile(r"\d{10}")


def _entries(raw: str | None, default: str) -> list[tuple[str, str | None]]:
    """``[(domain, own value or None)]`` from ``"a.com, b.co.uk=x"``."""
    out = []
    for item in (raw or default).split(","):
        item = item.strip()
        if not item:
            continue
        domain, _, value = item.partition("=")
        out.append((domain.strip().lower().removeprefix("www."), value.strip() or None))
    return out


def _amazon_program(env: Mapping[str, str]) -> Program | None:
    tag = (env.get("PIONIR_AFFILIATE_AMAZON_TAG") or "").strip()
    stores = []
    for domain, own in _entries(env.get("PIONIR_AFFILIATE_AMAZON_STORES"), "amazon.com"):
        value = own or tag
        if not value:
            continue
        if domain not in AMAZON_STORES:
            log.error("affiliate: %r is not an Amazon store; ignored", domain)
            continue
        if not _AMAZON_TAG.fullmatch(value):
            log.error("affiliate: the Amazon tag for %s is not a tracking id; ignored", domain)
            continue
        stores.append(Store(domain, (("tag", value),)))
    return (Program("amazon", "Amazon Associates", tuple(stores), AMAZON_STATEMENT)
            if stores else None)


def _ebay_program(env: Mapping[str, str]) -> Program | None:
    campid = (env.get("PIONIR_AFFILIATE_EBAY_CAMPID") or "").strip()
    stores = []
    for domain, own in _entries(env.get("PIONIR_AFFILIATE_EBAY_SITES"), "ebay.com"):
        value = own or campid
        if not value:
            continue
        if domain not in EBAY_SITES:
            log.error("affiliate: %r is not an eBay site this knows; ignored", domain)
            continue
        if not _EBAY_CAMPID.fullmatch(value):
            log.error("affiliate: the eBay campid for %s is not 10 digits; ignored", domain)
            continue
        stores.append(Store(domain, (("mkevt", "1"), ("mkcid", "1"),
                                     ("mkrid", EBAY_SITES[domain]), ("campid", value),
                                     ("toolid", "10001"))))
    return Program("ebay", "eBay Partner Network", tuple(stores)) if stores else None


_READERS = (_amazon_program, _ebay_program)


def programs_from_environment(env: Mapping[str, str]) -> tuple[Program, ...]:
    """Every program the owner has set up; none (rewrite nothing) when none is."""
    return tuple(p for p in (read(env) for read in _READERS) if p is not None)
