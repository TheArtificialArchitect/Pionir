"""What a Fiverr package costs, and who decided it.

**No AI ever sets a price.** A package's price comes from exactly one of two places:

1. **Our own site's price**, mirrored: the net the owner gets on Fiverr must match what the
   same thing costs on our site. Fiverr keeps a 20% seller fee (``FIVERR_FEE_PERCENT``), so
   the Fiverr price is the site price grossed up - ``gross_for_net``, rounded UP to whole
   dollars so the net is never below the site's. Each site price below names the file it
   was read from (``SitePrice.source``); ``verify_site_prices`` re-reads those files when they
   are on this machine and refuses a price that no longer matches (the gig is then not
   drafted, and the owner is told) - a constant here can never quietly drift from the site.
2. **The owner's own reply** on the gig's Discord card (``price 24 72 119`` - the three
   packages' Fiverr prices in whole dollars, ``parse_owner_prices``). Exactly what he typed.

A package with neither is **not priced**: the crew shows a PROPOSAL on the owner's card, with
its reasoning in words (``Proposal``), and the listing he pastes says "price not set". A
proposal is arithmetic over the site prices it cites - never a number a model produced.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

FIVERR_FEE_PERCENT = 20                 # Fiverr's seller fee on every order
MIN_FIVERR_CENTS = 500                  # Fiverr's smallest package price ($5)
MAX_FIVERR_CENTS = 1_000_000            # a typo guard for the owner's reply ($10,000)
PACKAGES = ("basic", "standard", "premium")


def gross_for_net(net_cents: int) -> int:
    """The Fiverr price (whole dollars, in cents) whose net after Fiverr's fee is at least
    ``net_cents``. $19 net -> $23.75 -> **$24** (net $19.20)."""
    if isinstance(net_cents, bool) or not isinstance(net_cents, int) or net_cents <= 0:
        raise ValueError(f"a net price is a positive whole number of cents, not {net_cents!r}")
    keep = 100 - FIVERR_FEE_PERCENT
    exact = net_cents * 100 / keep
    return max(MIN_FIVERR_CENTS, int(math.ceil(exact / 100 - 1e-9)) * 100)


def net_of(gross_cents: int) -> int:
    """What the owner keeps of a Fiverr price, in cents (rounded down)."""
    return gross_cents * (100 - FIVERR_FEE_PERCENT) // 100


def usd(cents: int) -> str:
    return f"${cents // 100:,}" if cents % 100 == 0 else f"${cents // 100:,}.{cents % 100:02d}"


# ---- our site's prices, each with where it was read -----------------------------------------
@dataclass(frozen=True)
class SitePrice:
    key: str
    cents: int
    what: str               # what the site sells at this price, in words
    source: str             # repo-relative file and the field, for the owner's card
    repo: str               # the repo's folder under C:\src (read-only)
    path: str               # the file inside it
    pattern: str            # a regex whose group 1 is the cents (or dollars, see ``dollars``)
    dollars: bool = False


SITE_PRICES = {
    "find": SitePrice(
        "find", 1900, "Find it for me: one item found, with where to buy it, in 2 business days",
        "Scrooge worker/src/orders.ts PACKAGES.find.cents", "Scrooge", "worker/src/orders.ts",
        r"find:\s*\{[^}]*?cents:\s*(\d+)"),
    "small": SitePrice(
        "small", 14900, "Dokaz build Small: one script or automation, 3 business days",
        "Scrooge worker/src/orders.ts PACKAGES.small.cents", "Scrooge", "worker/src/orders.ts",
        r"small:\s*\{[^}]*?cents:\s*(\d+)"),
    "uptime1": SitePrice(
        "uptime1", 500, "Uptime Monitor, 1 site, per month (a subscription)",
        "Scrooge worker/src/uptime.ts UPTIME_PLANS.uptime1.cents", "Scrooge",
        "worker/src/uptime.ts", r"uptime1:\s*\{[^}]*?cents:\s*(\d+)"),
    "techne_setup": SitePrice(
        "techne_setup", 14900, "Techne Starter's one-time setup fee, which pays for building "
                               "the site (up to 5 pages, hosted on a $49/month plan)",
        "doxa-techne site/src/pages/terms.astro (setup fee $149 Starter)", "doxa-techne",
        "site/src/pages/terms.astro", r"setup fee \(\$(\d+) Starter", dollars=True),
    "techne_care_setup": SitePrice(
        "techne_care_setup", 24900, "Techne Care's one-time setup fee",
        "doxa-techne site/src/pages/terms.astro (setup fee $249 Care)", "doxa-techne",
        "site/src/pages/terms.astro", r"setup fee \(\$\d+ Starter / \$(\d+) Care", dollars=True),
}


def verify_site_prices(src_root: Path | None, keys) -> list:
    """Every reason a site price used here no longer matches its source file. A source not on
    this machine is not a mismatch (``unchecked`` lists it); a source that exists but no
    longer says the price, or says another, is."""
    problems: list = []
    if src_root is None:
        return problems
    for key in keys:
        sp = SITE_PRICES[key]
        path = Path(src_root) / sp.repo / sp.path
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            problems.append(f"{sp.source}: could not be read ({type(exc).__name__})")
            continue
        m = re.search(sp.pattern, text, re.DOTALL)
        if m is None:
            problems.append(f"{sp.source}: the price is no longer where it was read from")
            continue
        found = int(m.group(1)) * (100 if sp.dollars else 1)
        if found != sp.cents:
            problems.append(f"{sp.source}: the site now says {usd(found)}, not {usd(sp.cents)}"
                            " - the gig is not drafted until this file is updated")
    return problems


def unchecked_sources(src_root: Path | None, keys) -> list:
    """The sources of these site prices that are not on this machine to be checked."""
    out = []
    for key in keys:
        sp = SITE_PRICES[key]
        if src_root is None or not (Path(src_root) / sp.repo / sp.path).is_file():
            out.append(sp.source)
    return out


# ---- a package's price: mirrored, proposed, or the owner's ---------------------------------
@dataclass(frozen=True)
class Mirror:
    """Priced from the site: ``net_cents`` is the site price (or a whole multiple of it)."""
    site_key: str
    times: int = 1

    @property
    def net_cents(self) -> int:
        return SITE_PRICES[self.site_key].cents * self.times


@dataclass(frozen=True)
class Proposal:
    """Not priced: the crew's proposal for the owner, and why. Never in the listing."""
    net_cents: int
    reasoning: str
    cites: tuple = ()       # SITE_PRICES keys the reasoning uses


@dataclass(frozen=True)
class Priced:
    """What the listing and the card say about one package's price."""
    package: str
    status: str             # "site" | "owner" | "proposed"
    gross_cents: int | None # the Fiverr price, or None when not set (proposed)
    proposed_gross_cents: int | None = None
    reasoning: str = ""
    source: str = ""

    @property
    def set(self) -> bool:
        return self.gross_cents is not None


def price_packages(rules: dict, owner: dict | None = None) -> dict:
    """``{package: Priced}`` for the three packages. ``rules`` maps each package to a Mirror
    or a Proposal; ``owner`` is the owner's reply ({package: gross cents}) and wins for every
    package it names."""
    out: dict = {}
    for pkg in PACKAGES:
        rule = rules[pkg]
        if owner and pkg in owner:
            out[pkg] = Priced(pkg, "owner", int(owner[pkg]), source="the owner's reply on "
                              "the gig card")
        elif isinstance(rule, Mirror):
            sp = SITE_PRICES[rule.site_key]
            times = f"{rule.times} x " if rule.times != 1 else ""
            out[pkg] = Priced(pkg, "site", gross_for_net(rule.net_cents),
                              source=f"{times}{usd(sp.cents)} from {sp.source}, grossed up "
                                     f"for Fiverr's {FIVERR_FEE_PERCENT}% fee")
        elif isinstance(rule, Proposal):
            out[pkg] = Priced(pkg, "proposed", None,
                              proposed_gross_cents=gross_for_net(rule.net_cents),
                              reasoning=rule.reasoning,
                              source=", ".join(SITE_PRICES[k].source for k in rule.cites))
        else:                           # pragma: no cover - a programming error
            raise TypeError(f"no price rule for {pkg}")
    return out


_OWNER_PRICE = re.compile(r"(?i)\s*price\s+\$?(\d{1,5})\s+\$?(\d{1,5})\s+\$?(\d{1,5})\s*")


def parse_owner_prices(text) -> dict:
    """The owner's ``price <basic> <standard> <premium>`` reply (Fiverr prices, whole
    dollars) -> {package: cents}, or ValueError saying what is wrong. The whole reply must be
    exactly that: nothing is guessed."""
    m = _OWNER_PRICE.fullmatch(text) if isinstance(text, str) else None
    if m is None:
        raise ValueError("a price reply is `price <basic> <standard> <premium>` in whole "
                         "dollars, e.g. `price 24 72 119`")
    cents = [int(g) * 100 for g in m.groups()]
    for pkg, c in zip(PACKAGES, cents, strict=True):
        if not MIN_FIVERR_CENTS <= c <= MAX_FIVERR_CENTS:
            raise ValueError(f"the {pkg} price must be {usd(MIN_FIVERR_CENTS)} to "
                             f"{usd(MAX_FIVERR_CENTS)}")
    if not cents[0] <= cents[1] <= cents[2]:
        raise ValueError("the prices must rise from basic to standard to premium")
    return dict(zip(PACKAGES, cents, strict=True))
