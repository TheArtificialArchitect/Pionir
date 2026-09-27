"""The four Fiverr gigs: their listings, the honesty check, and the drafter that hands each
one to the owner on Discord.

**Pionir never posts a gig.** Fiverr has no seller API and automating the account breaks its
terms, so the drafter only PREPARES: one complete listing per service - title, description,
three packages (what is delivered, days, revisions, price), FAQ, search tags, the questions
to ask the buyer, and a gig image - as a Discord card and a Markdown file the owner pastes
from. He creates or edits the gig on Fiverr himself.

**One per service, redrafted only on request**: the owner replies ``redraft`` to the gig's
card (or ``price <basic> <standard> <premium>``, which sets the prices and redrafts).

**The words are fixed text** (``SERVICES`` below - the owner can read and edit them here); no
model writes a listing. Every listing is checked (``check_gig``, fail closed) before a card is
posted: Fiverr's lengths; no claim that the work is hand-made; no invented reviews, counts,
years or credentials; an honest line saying how the work is made (``made_by``: AI-assisted
for research and websites, automated for data and health reports); no link, email address or
phone number (Fiverr forbids them); no internal system name; no money except the package
prices (prices.py - the site's, grossed up for Fiverr's fee, or the owner's own).
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import Path

from ..blog import _clip, _Unreadable, read_record, record_path, save_record
from ..figures import Figure
from ..hands import Job
from ..log import log
from ..result import Err, Ok, Result
from ..worker import ErrorKind, WorkContext, make_output, never_raises
from ..workers import _Base
from . import checks
from .prices import (
    FIVERR_FEE_PERCENT,
    PACKAGES,
    Mirror,
    Proposal,
    net_of,
    parse_owner_prices,
    price_packages,
    unchecked_sources,
    usd,
    verify_site_prices,
)

CARD = "fiverr.card"
INBOX = "fiverr.inbox"
REFUSED_BY_PIONIR = "refused by Pionir's checks"
TITLE_MAX = 80
DESCRIPTION_MAX = 1200
PACKAGE_NAME_MAX = 35
PACKAGE_DESC_MAX = 100
TAGS_MAX = 5
TAG_MAX = 20
FAQ_Q_MAX = 90
FAQ_A_MAX = 300
REQ_MAX = 450

AI_MADE = ("AI-assisted: AI tools do the work, automated checks test every delivery, and I "
           "send each one to you myself.")
AUTO_MADE = ("Automated: my own tools do the work, automated checks test every delivery, and "
             "I send each one to you myself.")


@dataclass(frozen=True)
class Package:
    name: str
    description: str
    deliverables: tuple
    days: int
    revisions: int


@dataclass(frozen=True)
class Service:
    key: str
    title: str
    description: str
    made_by: str
    uses_ai: bool
    packages: dict              # basic/standard/premium -> Package
    prices: dict                # basic/standard/premium -> Mirror | Proposal
    faq: tuple                  # (question, answer)
    tags: tuple
    requirements: tuple
    image_headline: str
    image_points: tuple
    limits: dict                # per package: how many items / files / sites / sections


RESEARCH = Service(
    key="research",
    title="I will find where to buy a hard to find item and send you a report",
    description=(
        "Looking for something specific - a discontinued part, an exact model, a gift in one "
        "particular colour? Tell me exactly what you need and I'll search the web for where you "
        "can buy it, then send you a short report: each option with the seller, the price as "
        "the listing shows it, the condition, availability and a direct link to the listing.\n\n"
        "What you get:\n"
        "- Up to 8 buying options per item, best first\n"
        "- Every listing opened before you get it: live, on the seller's own site, naming "
        "your item and showing the price I list\n"
        "- Notes on anything to watch out for\n"
        "- If I can't find it, the report says where I looked\n\n"
        "What I won't search for: people or anyone's contact details, weapons, drugs or "
        "prescription medicines, counterfeit or stolen goods, or anything illegal.\n\n"
        + AI_MADE),
    made_by=AI_MADE, uses_ai=True,
    packages={
        "basic": Package("One item", "Where to buy one item: up to 8 options with prices and "
                         "links", ("A report for 1 item", "Up to 8 buying options"), 2, 1),
        "standard": Package("Three items", "Where to buy up to 3 items, each with up to 8 "
                            "options", ("A report for up to 3 items", "Up to 8 options each"),
                            3, 1),
        "premium": Package("Five items", "Where to buy up to 5 items, each with up to 8 "
                           "options", ("A report for up to 5 items", "Up to 8 options each"),
                           4, 2),
    },
    prices={
        "basic": Mirror("find"),
        "standard": Proposal(5700, "Three finds at the site's price for one find ($19 net "
                                   "each), so the net per item matches the site.",
                             ("find",)),
        "premium": Proposal(9500, "Five finds at the site's price for one find ($19 net "
                                  "each), so the net per item matches the site.", ("find",)),
    },
    faq=(
        ("What should I tell you about the item?",
         "The exact item: brand, model or part number, size, colour, new or used, and the "
         "country you want to buy in. A photo helps."),
        ("Do you buy it for me?",
         "No. I find where you can buy it and send you the listings; you buy it yourself from "
         "the seller you choose."),
        ("Will the prices stay the same?",
         "No. Prices and stock belong to the sellers and change quickly; the report shows them "
         "as listed when I checked."),
        ("How is the research done?",
         "AI tools search the web and draft it. An automated check then opens every "
         "listing and keeps only pages that are live, on the seller's own site, and show "
         "your item and the listed price."),
    ),
    tags=("product research", "find products", "online shopping", "product sourcing",
          "hard to find"),
    requirements=(
        "What exactly should I find? Brand, model or part number, size, colour, and anything "
        "else that matters to you.",
        "New, used, or either?",
        "Which country or region should I search in?",
        "Any budget limit, or sellers to avoid? (optional)",
    ),
    image_headline="Find It For Me",
    image_points=("Where to buy hard to find items", "Prices and links, as listed",
                  "A short report, fast"),
    limits={"basic": 1, "standard": 3, "premium": 5},
)

DATA = Service(
    key="data",
    title="I will clean up and convert your CSV, JSON or Excel data files",
    description=(
        "Messy spreadsheet or data export? I'll clean it up and convert it between CSV, JSON "
        "and Excel (.xlsx).\n\n"
        "The cleanup:\n"
        "- Trims stray spaces from the ends of every cell\n"
        "- Fixes blank and repeated column names\n"
        "- Removes empty rows and exact duplicate rows\n"
        "- Pads short rows so every row lines up\n"
        "- Keeps your values exactly as they are: leading zeros stay, nothing is re-typed\n\n"
        "You also get a cleanup report that lists exactly what changed, in counts, so you can "
        "check it.\n\n"
        "Attach your file to the order requirements and tell me which format you want back.\n\n"
        + AUTO_MADE),
    made_by=AUTO_MADE, uses_ai=False,
    packages={
        "basic": Package("One file", "One file cleaned and converted to one format, with a "
                         "cleanup report", ("1 file", "1 output format", "Cleanup report"),
                         1, 1),
        "standard": Package("Up to 3 files", "Up to 3 files cleaned, in CSV, JSON and Excel, "
                            "with a cleanup report", ("Up to 3 files", "All 3 formats",
                                                      "Cleanup report"), 2, 2),
        "premium": Package("Up to 10 files", "Up to 10 files cleaned, in CSV, JSON and Excel, "
                           "with a cleanup report", ("Up to 10 files", "All 3 formats",
                                                     "Cleanup report"), 2, 3),
    },
    prices={
        "basic": Proposal(2000, "No site price for a one-off cleanup (the site sells the "
                                "convert API by subscription). The work is automated, so one "
                                "file is priced near one find ($19 net) and far under the "
                                "smallest custom build ($149 net).", ("find", "small")),
        "standard": Proposal(4000, "Twice the one-file price for up to three files in all "
                                   "three formats; still automated.", ("find",)),
        "premium": Proposal(8000, "Twice the standard price for up to ten files; still well "
                                  "under the smallest custom build ($149 net).", ("small",)),
    },
    faq=(
        ("Which files can you read?",
         "CSV (any common separator), TSV, JSON (a list of rows) and Excel .xlsx (the first "
         "sheet)."),
        ("Will you change my data?",
         "Only as the cleanup list says. Values are never re-typed or reformatted, and the "
         "report counts every change."),
        ("Can you merge files, split columns or fix dates?",
         "Not in these packages: they are the standard cleanup. Message me before ordering if "
         "you need something custom."),
        ("Is my data kept private?",
         "Your file is processed on my own computer, is not uploaded to any other service, and "
         "comes back to you only through Fiverr."),
    ),
    tags=("data cleaning", "csv to excel", "json to csv", "excel cleanup", "data conversion"),
    requirements=(
        "Attach your file or files (CSV, TSV, JSON or Excel .xlsx).",
        "Which format or formats do you want back: CSV, JSON, Excel, or all three?",
        "Anything I should know about the file? (optional)",
    ),
    image_headline="Clean Data, Any Format",
    image_points=("CSV, JSON and Excel", "Spaces, blanks and duplicates fixed",
                  "A report of every change"),
    limits={"basic": 1, "standard": 3, "premium": 10},
)

WEBSITE = Service(
    key="website",
    title="I will build a clean one page website for your small business",
    description=(
        "Need a simple, fast website for your business? I'll build a one-page site with your "
        "business name, what you do, your services and how to reach you, written only from the "
        "details you give me.\n\n"
        "What you get:\n"
        "- A responsive one-page site (index.html and styles.css) that works well on phones\n"
        "- Plain HTML and CSS: no scripts, no trackers, nothing loaded from other websites\n"
        "- Works on any web host: just upload the files\n"
        "- A preview image and simple steps to put it online\n\n"
        "I never make up testimonials, reviews, prices or contact details. If you don't give "
        "it to me, it isn't on the page.\n\n"
        + AI_MADE),
    made_by=AI_MADE, uses_ai=True,
    packages={
        "basic": Package("Starter page", "A one-page site with up to 4 sections",
                         ("1 page", "Up to 4 sections", "Preview image", "Setup steps"), 3, 1),
        "standard": Package("Business page", "A one-page site with up to 6 sections",
                            ("1 page", "Up to 6 sections", "Preview image", "Setup steps"),
                            3, 2),
        "premium": Package("Business page plus", "A one-page site with up to 8 sections",
                           ("1 page", "Up to 8 sections", "Preview image", "Setup steps"),
                           5, 3),
    },
    prices={
        "basic": Proposal(9900, "No site price for a one-page site handed over as files. The "
                                "site's one-time build price is $149 (the Techne Starter setup "
                                "fee, for a hosted site of up to 5 pages); one page without "
                                "hosting proposes about two thirds of it.",
                          ("techne_setup",)),
        "standard": Proposal(14900, "Mirrors the site's one-time build price: the Techne "
                                    "Starter setup fee and the Dokaz Small build are both "
                                    "$149.", ("techne_setup", "small")),
        "premium": Proposal(24900, "Mirrors the Techne Care setup fee ($249), the site's "
                                   "larger one-time build.", ("techne_care_setup",)),
    },
    faq=(
        ("Is hosting included?",
         "No. You get the files, and they work on any web host. I don't need or ask for your "
         "hosting login."),
        ("Can I edit it later?",
         "Yes. It's plain HTML and CSS: open index.html in any text editor, change the words "
         "and upload it again."),
        ("Can it have a contact form?",
         "Not in these packages: a form needs a server behind it. The page uses email and "
         "phone links instead."),
        ("Can you add my photos?",
         "These packages are text and design only, with no images, which keeps the page fast "
         "and simple."),
    ),
    tags=("website", "landing page", "small business", "html css", "one page website"),
    requirements=(
        "Your business name and what you do, in one or two sentences.",
        "Your services or products, one per line.",
        "The contact details to show on the page (email, phone, area you serve). Only what you "
        "want public.",
        "Colours or a style you like, and your existing website or social pages to link to. "
        "(optional)",
    ),
    image_headline="Your Business, One Clean Page",
    image_points=("Fast, simple, works on phones", "Plain HTML and CSS, no scripts",
                  "Works on any web host"),
    limits={"basic": 4, "standard": 6, "premium": 8},
)

UPTIME = Service(
    key="uptime",
    title="I will check your website uptime, SSL and domain setup and send a report",
    description=(
        "Is your website quietly broken? I'll check it and send you a clear health report with "
        "a step-by-step fix list.\n\n"
        "What I check:\n"
        "- Uptime and response time of your home page\n"
        "- Your SSL certificate: trusted, covering your domain, days until it expires\n"
        "- Domain and DNS, for your domain and its www version\n"
        "- The redirect from http to https\n"
        "- Security headers\n"
        "- Page title, meta description and structured data\n\n"
        "Every result is measured, never estimated. For anything that needs attention you get "
        "the exact step to fix it at your host or registrar.\n\n"
        + AUTO_MADE),
    made_by=AUTO_MADE, uses_ai=False,
    packages={
        "basic": Package("One site", "A health report with fix steps for 1 website",
                         ("1 website", "Health report", "Step-by-step fixes"), 1, 1),
        "standard": Package("Up to 3 sites", "A health report with fix steps for up to 3 "
                            "websites", ("Up to 3 websites", "Health reports",
                                         "Step-by-step fixes"), 2, 1),
        "premium": Package("Up to 5 sites", "A health report with fix steps for up to 5 "
                           "websites", ("Up to 5 websites", "Health reports",
                                        "Step-by-step fixes"), 2, 2),
    },
    prices={
        "basic": Proposal(2000, "No site price for a one-off report: the site sells monitoring "
                                "by the month ($5 per site). An automated one-off report "
                                "proposes four months of monitoring.", ("uptime1",)),
        "standard": Proposal(4000, "Up to three sites at about two thirds of the one-site "
                                   "price each.", ("uptime1",)),
        "premium": Proposal(6000, "Up to five sites at under two thirds of the one-site price "
                                  "each.", ("uptime1",)),
    },
    faq=(
        ("Do you need my passwords?",
         "No. Every check is made from outside, the way a visitor sees your site. You get the "
         "steps to fix anything yourself."),
        ("Will you fix the problems for me?",
         "The report gives you the exact steps for your host or registrar. I don't log in to "
         "your accounts."),
        ("What is checked?",
         "Uptime, response time, the SSL certificate, DNS, the http to https redirect, security "
         "headers, the page title, the meta description and structured data."),
    ),
    tags=("website audit", "ssl certificate", "uptime", "dns", "website health"),
    requirements=(
        "Your website address, or addresses for a multi-site package.",
        "Your web host and domain registrar, if you know them. (optional)",
    ),
    image_headline="Is Your Website Healthy?",
    image_points=("Uptime and speed", "SSL and domain checked", "A clear fix list"),
    limits={"basic": 1, "standard": 3, "premium": 5},
)

SERVICES = {s.key: s for s in (RESEARCH, DATA, WEBSITE, UPTIME)}


# ---- the honesty check ----------------------------------------------------------------------
_INVENTED = re.compile(
    r"(?i)(?<!up to )\b\d[\d,.]*\+?\s*(?:happy |satisfied )?(?:clients|customers|buyers|orders|reviews|"
    r"projects|websites|sites built)\b"
    r"|\b(?:years?|yrs) of experience\b|\b\d+\+?\s*years?\b"
    r"|\b(?:5|five)[- ]stars?\b|\btop[- ]rated\b|\bbest[- ]selling\b|\bcertified\b"
    r"|\baward(?:s|-winning)?\b|\bexperts?\b|\btrusted by\b|\bguarantee[ds]?\b"
    r"|\b#\s?1\b|\bnumber one\b|\bmoney[- ]back\b")


def listing_texts(service: Service) -> list:
    texts = [("title", service.title), ("description", service.description)]
    for pkg in PACKAGES:
        p = service.packages[pkg]
        texts += [(f"{pkg} name", p.name), (f"{pkg} description", p.description)]
        texts += [(f"{pkg} deliverable", d) for d in p.deliverables]
    for q, a in service.faq:
        texts += [("FAQ question", q), ("FAQ answer", a)]
    texts += [("tag", t) for t in service.tags]
    texts += [("requirement", r) for r in service.requirements]
    texts += [("image headline", service.image_headline)]
    texts += [("image point", p) for p in service.image_points]
    return texts


def check_gig(service: Service, guard: checks.Guard) -> list:
    """Every reason this listing may not be handed to the owner; empty is the only pass."""
    reasons: list = []
    if len(service.title) > TITLE_MAX:
        reasons.append(f"the title is {len(service.title)} characters; Fiverr allows "
                       f"{TITLE_MAX}")
    if not service.title.startswith("I will "):
        reasons.append("a Fiverr title starts with 'I will'")
    if len(service.description) > DESCRIPTION_MAX:
        reasons.append(f"the description is {len(service.description)} characters; Fiverr "
                       f"allows {DESCRIPTION_MAX}")
    if service.made_by not in service.description:
        reasons.append("the description does not say how the work is made")
    if service.uses_ai and "AI" not in service.made_by:
        reasons.append("an AI-made service must say it is AI-assisted")
    if set(service.packages) != set(PACKAGES) or set(service.prices) != set(PACKAGES):
        reasons.append("a gig has exactly three packages: basic, standard and premium")
        return reasons
    for pkg in PACKAGES:
        p = service.packages[pkg]
        if len(p.name) > PACKAGE_NAME_MAX:
            reasons.append(f"the {pkg} package name is over {PACKAGE_NAME_MAX} characters")
        if len(p.description) > PACKAGE_DESC_MAX:
            reasons.append(f"the {pkg} package description is over {PACKAGE_DESC_MAX} "
                           "characters")
        if not 1 <= p.days <= 30 or not 0 <= p.revisions <= 10:
            reasons.append(f"the {pkg} package's days or revisions are out of range")
    if not 1 <= len(service.tags) <= TAGS_MAX:
        reasons.append(f"a gig has 1 to {TAGS_MAX} search tags")
    for t in service.tags:
        if len(t) > TAG_MAX or not re.fullmatch(r"[a-z0-9 ]+", t):
            reasons.append(f"the tag {t!r} is not up to {TAG_MAX} lower-case letters and "
                           "digits")
    for q, a in service.faq:
        if len(q) > FAQ_Q_MAX or len(a) > FAQ_A_MAX:
            reasons.append(f"the FAQ {q[:30]!r} is too long for Fiverr")
    for r in service.requirements:
        if len(r) > REQ_MAX:
            reasons.append(f"a requirement question is over {REQ_MAX} characters")
    for field, text in listing_texts(service):
        if checks._HANDMADE.search(text):
            reasons.append(f"the {field} claims the work is hand-made")
        m = _INVENTED.search(text)
        if m:
            reasons.append(f"the {field} claims something nothing backs ({m.group(0)!r}): no "
                           "invented reviews, counts, years or credentials")
        if checks._MONEY.search(text):
            reasons.append(f"the {field} states money; a price goes only in a package's "
                           "price")
        reasons += checks.check_text(f"the {field}", text, guard)
    return reasons


# ---- the listing, as the owner pastes it ------------------------------------------------------
def _price_line(priced) -> str:
    if priced.status == "proposed":
        return "price NOT SET - reply to the card with `price <basic> <standard> <premium>`"
    who = "set by you" if priced.status == "owner" else "mirrors the site price"
    return (f"{usd(priced.gross_cents)} on Fiverr (you keep about {usd(net_of(priced.gross_cents))}"
            f" after Fiverr's {FIVERR_FEE_PERCENT}% fee; {who})")


def render_markdown(service: Service, priced: dict) -> str:
    """The whole listing, in the order Fiverr's gig editor asks for it."""
    lines = [f"# Fiverr gig: {service.key}", "", "## Title", "", service.title, "",
             "## Search tags", "", ", ".join(service.tags), "", "## Packages", ""]
    for pkg in PACKAGES:
        p = service.packages[pkg]
        lines += [f"### {pkg.title()}: {p.name}", "", p.description, "",
                  f"- Delivery: {p.days} day{'s' if p.days != 1 else ''}",
                  f"- Revisions: {p.revisions}",
                  *[f"- {d}" for d in p.deliverables],
                  f"- Price: {_price_line(priced[pkg])}", ""]
    lines += ["## Description", "", service.description, "", "## FAQ", ""]
    for q, a in service.faq:
        lines += [f"**{q}**", "", a, ""]
    lines += ["## Requirements (questions for the buyer)", ""]
    lines += [f"{i}. {r}" for i, r in enumerate(service.requirements, 1)]
    lines += ["", "## Gig image", "", "gig.png (1280 x 769), in this folder.", ""]
    return "\n".join(lines)


def render_card(service: Service, priced: dict, version: int, unchecked: list) -> str:
    """What the owner reads on Discord, above the attached listing and image."""
    lines = [f"**{service.title}**  (draft {version})", "",
             "**Packages** (price, then where it came from):"]
    for pkg in PACKAGES:
        p, pr = service.packages[pkg], priced[pkg]
        lines.append(f"- **{pkg.title()} - {p.name}**: {p.days} day{'s' if p.days != 1 else ''}"
                     f", {p.revisions} revision{'s' if p.revisions != 1 else ''} - "
                     + (f"{usd(pr.gross_cents)} ({pr.source})" if pr.set else
                        f"NOT SET. Proposed {usd(pr.proposed_gross_cents)} on Fiverr "
                        f"(about {usd(net_of(pr.proposed_gross_cents))} to you): "
                        f"{pr.reasoning}"))
    if any(not pr.set for pr in priced.values()):
        lines += ["", "No price is set until you set it: reply `price <basic> <standard> "
                      "<premium>` in whole dollars as Fiverr shows them (e.g. "
                      f"`price {' '.join(str((pr.gross_cents or pr.proposed_gross_cents) // 100) for pr in priced.values())}`)."]
    if unchecked:
        lines += ["", "Site prices not re-checked (source not on this machine): "
                  + "; ".join(unchecked)]
    lines += ["", f"**Tags:** {', '.join(service.tags)}",
              "", "The full listing (description, FAQ, the buyer's requirement questions) is "
                  "in the attached gig.md; the image is gig.png.",
              "Reply `redraft` to get a fresh draft of this gig."]
    return "\n".join(lines)


# ---- the gig image ----------------------------------------------------------------------------
IMAGE_W, IMAGE_H = 1280, 769


def render_image(headline: str, points) -> bytes | None:
    """A 1280x769 PNG in the Instagram card's style (social/card.py: its font and colours),
    or None when Pillow is not installed. No contact details: Fiverr refuses them."""
    try:
        from PIL import Image, ImageDraw

        from pionir.social import card as style
    except ImportError:
        return None
    img = Image.new("RGB", (IMAGE_W, IMAGE_H), style.BACKGROUND)
    draw = ImageDraw.Draw(img)
    margin = 90
    width = IMAGE_W - 2 * margin
    head = style._font(76, style.BOLD)
    lines = style._wrap(headline, head, width)[:2]
    block = 50 + 92 * len(lines) + 40 + 64 * len(points[:3])
    top = max(60, (IMAGE_H - block) // 2)
    draw.rectangle((margin, top, margin + 120, top + 8), fill=style.ACCENT)
    y = top + 50
    for line in lines:
        draw.text((margin, y), line, font=head, fill=style.INK)
        y += 92
    y += 40
    body = style._font(40, style.REGULAR)
    for point in points[:3]:
        draw.ellipse((margin, y + 16, margin + 16, y + 32), fill=style.ACCENT)
        draw.text((margin + 40, y), _clip(point, 60), font=body, fill=style.MUTED)
        y += 64
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


# ---- the owner's replies to gig cards ------------------------------------------------------------
def read_inbox(ctx: WorkContext, kind: str) -> Result:
    """The owner's replies to this kind of Fiverr card, as Pionir recorded them: ``Ok(list)``
    of {reply_id, key, ref, text, at}, or the Err. Only the owner's replies are there
    (Pionir's gate records no one else's, and fiverr.inbox returns only his)."""
    out = ctx.job(Job(INBOX, {"kind": kind}, what=f"read the owner's replies to the Fiverr "
                                                   f"{kind} cards"))
    if out.status != "done":
        return Err(out.error or f"Pionir said {out.status}")
    result = out.result if isinstance(out.result, dict) else {}
    replies = result.get("replies")
    if result.get("ok") is False or not isinstance(replies, list):
        return Err(str(result.get("error") or "fiverr.inbox answered without replies"))
    return Ok([r for r in replies if isinstance(r, dict) and isinstance(r.get("reply_id"), str)])


def post_card(ctx: WorkContext, payload: dict, what: str) -> tuple:
    """``(posted, why)``: Pionir's fiverr.card, which posts to the owner's channel only. A
    card Pionir REFUSED (its own checks - a secret in a file) says so: ``why`` then starts
    with ``REFUSED_BY_PIONIR``, and posting it again would be refused again."""
    out = ctx.job(Job(CARD, payload, what=what))
    ok = out.status == "done" and isinstance(out.result, dict) \
        and out.result.get("ok") is not False
    if ok:
        return True, None
    why = out.error
    if not why and isinstance(out.result, dict):
        why = out.result.get("error")
    why = _clip(why or f"Pionir said {out.status}", 300)
    if out.status == "failed" and out.error_type == "AdapterProtocolError":
        why = f"{REFUSED_BY_PIONIR}: {why}"
    return False, why


class GigDrafter(_Base):
    """``fiverr.gigs``: one checked listing per service, handed to the owner on Discord."""

    record_what = "the gig drafter's own record of every listing it handed to the owner"

    def __init__(self, spec, *, source_root: str | None = None, ssh_dir: str | None = None,
                 services: tuple = tuple(SERVICES)) -> None:
        super().__init__(spec)
        self.source_root = Path(source_root) if source_root else None
        self.ssh_dir = ssh_dir
        unknown = [s for s in services if s not in SERVICES]
        if unknown:
            raise ValueError(f"unknown Fiverr services {unknown}; known: {', '.join(SERVICES)}")
        self.services = tuple(services)

    def load(self, state_dir) -> dict:
        doc = read_record(state_dir, self.worker_id)
        blank = {"gigs": {}, "replies_seen": []}
        return blank if doc is None else {**blank, **doc}

    @never_raises()
    def run(self, ctx: WorkContext) -> Result:
        if ctx.state_dir is None or ctx.fiverr_dir is None:
            return self._err(ErrorKind.NOT_CONFIGURED, "no state or Fiverr folder: the drafter "
                             "cannot keep its record or write the listings", retryable=False)
        if ctx.job is None:
            return self._err(ErrorKind.NOT_CONFIGURED, "no hands: gig cards are posted only "
                             "through Pionir", retryable=False)
        try:
            rec = self.load(ctx.state_dir)
        except _Unreadable as exc:
            return self._err(ErrorKind.MALFORMED, f"its record is unreadable ({exc})",
                             retryable=False)
        events: list = []
        inbox = read_inbox(ctx, "gig")
        if isinstance(inbox, Err):
            why = str(inbox.error)
            if re.search(r"(?i)unknown capability|not configured|no such capability|"
                         r"CapabilityNotFound|fiverr\.inbox", why):
                return self._err(ErrorKind.NOT_CONFIGURED, f"Pionir's Fiverr desk is off "
                                 f"({_clip(why, 160)}); set PIONIR_FIVERR_DESK=1",
                                 retryable=False)
            return self._err(ErrorKind.UNAVAILABLE, f"could not read the owner's replies "
                             f"({_clip(why, 160)})")
        self._replies(ctx, rec, inbox.value, events)
        try:
            guard = checks.load_guard(ctx.secrets_dir, self.ssh_dir, checks.owner_markers())
        except Exception as exc:  # noqa: BLE001 - fail closed: no guard, no card
            return self._err(ErrorKind.UNAVAILABLE, f"the secrets scan could not load "
                             f"({_clip(exc, 160)}); no gig is handed over unchecked")
        for key in self.services:
            gig = rec["gigs"].setdefault(key, {"version": 0, "owner_prices": None,
                                               "redraft": True, "cards": []})
            if gig.get("redraft"):
                self._draft(ctx, SERVICES[key], gig, guard, events)
        save_record(record_path(ctx.state_dir, self.worker_id), rec)
        return Ok((*events, self._tally(ctx, rec)))

    def _replies(self, ctx: WorkContext, rec: dict, replies: list, events: list) -> None:
        seen = set(rec["replies_seen"])
        for r in sorted(replies, key=lambda r: str(r.get("reply_id"))):
            rid = r["reply_id"]
            service = r.get("ref")
            if rid in seen or service not in SERVICES:
                continue
            seen.add(rid)
            rec["replies_seen"].append(rid)
            gig = rec["gigs"].setdefault(service, {"version": 0, "owner_prices": None,
                                                   "redraft": True, "cards": []})
            text = " ".join(str(r.get("text") or "").split())
            if text.lower() in ("redraft", "redraft please", "redo"):
                gig["redraft"] = True
                events.append(self._event(ctx, "fiverr.gig_redraft_asked", {"service": service}))
                continue
            try:
                prices = parse_owner_prices(text)
            except ValueError as exc:
                gig.setdefault("unread_replies", []).append({"reply_id": rid,
                                                             "why": _clip(exc, 200)})
                post_card(ctx, {"key": f"gig-note:{service}:{rid}", "kind": "note",
                                "ref": service, "title": f"Gig {service}: reply not read",
                                "body": f"Your reply wasn't `redraft` or a price: {exc}. "
                                        "Nothing changed."},
                          f"tell the owner his reply to the {service} gig was not read")
                continue
            gig["owner_prices"] = prices
            gig["redraft"] = True
            events.append(self._event(ctx, "fiverr.gig_priced_by_owner", {
                "service": service},
                [Figure(c, "usd_cents", f"{service} {pkg} Fiverr price set by the owner",
                        window="now") for pkg, c in prices.items()]))

    def _draft(self, ctx: WorkContext, service: Service, gig: dict, guard, events: list) -> None:
        version = int(gig.get("version") or 0) + 1
        site_keys = sorted({r.site_key for r in service.prices.values() if isinstance(r, Mirror)}
                           | {k for r in service.prices.values() if isinstance(r, Proposal)
                              for k in r.cites})
        reasons = verify_site_prices(self.source_root, site_keys)
        reasons += check_gig(service, guard)
        priced = price_packages(service.prices, gig.get("owner_prices"))
        folder = Path(ctx.fiverr_dir) / "gigs" / service.key
        files: list = []
        if not reasons:
            folder.mkdir(parents=True, exist_ok=True)
            md = folder / "gig.md"
            md.write_text(render_markdown(service, priced), encoding="utf-8")
            files.append(md)
            png = render_image(service.image_headline, service.image_points)
            if png is not None:
                (folder / "gig.png").write_bytes(png)
                files.append(folder / "gig.png")
            for f in files:
                reasons += checks.check_file(f, guard)
        if reasons:
            key = f"gig-blocked:{service.key}:v{version}"
            # posted once; a card that did not go up is tried again on the next run
            if not any(c.get("key") == key and c.get("status") == "posted"
                       for c in gig["cards"]):
                posted, why = post_card(ctx, {
                    "key": key, "kind": "problem", "ref": service.key,
                    "title": f"Gig {service.key} not drafted",
                    "body": "The listing failed its checks, so no draft card was posted:\n"
                            + "\n".join(f"- {_clip(r, 180)}" for r in reasons[:10])},
                    f"tell the owner the {service.key} gig failed its checks")
                gig["cards"].append({"key": key, "status": "posted" if posted else
                                     "not_posted", "why": why, "at": ctx.now})
            gig.update(blocked=[_clip(r, 200) for r in reasons[:10]], blocked_at=ctx.now)
            log.warning("%s: the %s gig is BLOCKED: %s", self.worker_id, service.key,
                        "; ".join(reasons[:4]))
            events.append(self._event(ctx, "fiverr.gig_blocked", {
                "service": service.key, "reasons": [_clip(r, 120) for r in reasons[:3]]}))
            return
        key = f"gig:{service.key}:v{version}"
        unchecked = unchecked_sources(self.source_root, site_keys)
        posted, why = post_card(ctx, {
            "key": key, "kind": "gig", "ref": service.key,
            "title": f"Fiverr gig draft: {service.key}",
            "body": render_card(service, priced, version, unchecked),
            "files": [f.relative_to(Path(ctx.fiverr_dir)).as_posix() for f in files],
            "replies": True}, f"hand the owner the {service.key} gig draft")
        gig["cards"].append({"key": key, "status": "posted" if posted else "not_posted",
                             "why": why, "at": ctx.now})
        if not posted:
            log.warning("%s: the %s gig card was not posted: %s", self.worker_id, service.key,
                        why)
            events.append(self._event(ctx, "fiverr.gig_not_posted", {
                "service": service.key, "why": _clip(why, 160)}))
            return
        gig.update(version=version, redraft=False, drafted_at=ctx.now, blocked=None,
                   priced={p: {"status": pr.status, "gross_cents": pr.gross_cents,
                               "proposed_gross_cents": pr.proposed_gross_cents}
                           for p, pr in priced.items()})
        figs = [Figure(pr.gross_cents, "usd_cents", f"{service.key} {p} Fiverr price",
                       window="now") for p, pr in priced.items() if pr.set]
        events.append(self._event(ctx, "fiverr.gig_drafted", {
            "service": service.key, "version": version,
            "unpriced_packages": [p for p, pr in priced.items() if not pr.set]}, figs))

    def _event(self, ctx: WorkContext, kind: str, payload: dict, figures=()):
        return make_output(self, kind=kind, valid_at=ctx.now, observed_at=ctx.now,
                           payload=payload, figures=figures, entities=self.entities,
                           provenance={"source": "real", "provider": self.provider,
                                       "record": self.record_what})

    def _tally(self, ctx: WorkContext, rec: dict):
        gigs = [rec["gigs"].get(k) or {} for k in self.services]
        drafted = sum(1 for g in gigs if int(g.get("version") or 0) > 0)
        unpriced = sum(1 for g in gigs for p in (g.get("priced") or {}).values()
                       if p.get("status") == "proposed")
        figures = [
            Figure(drafted, "count", "Fiverr gigs drafted for the owner", window="now"),
            Figure(sum(1 for g in gigs if g.get("blocked")), "count",
                   "Fiverr gigs blocked by the checks", window="now"),
            Figure(unpriced, "count", "Fiverr packages waiting for the owner's price",
                   window="now"),
        ]
        return make_output(self, kind="fiverr.gig_tally", valid_at=ctx.now, observed_at=ctx.now,
                           payload={"services": list(self.services),
                                    "blocked": [k for k, g in zip(self.services, gigs,
                                                                  strict=True)
                                                if g.get("blocked")]},
                           figures=figures, entities=self.entities,
                           provenance={"source": "real", "provider": self.provider,
                                       "record": self.record_what})
