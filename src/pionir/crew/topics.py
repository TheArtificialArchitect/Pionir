"""Where the posting workers' topics come from: what we sell, ordered by measured demand.

The blog (blog.py) and Instagram (instagram.py) once drew from a static list of 14 seed topics
(``blog.SEEDS``); the blog had used 3 of them and Instagram 12, so Instagram would have stopped
within two days and the blog not long after. Now a topic is GENERATED:

- **What a post may be about** is the inventory of paid API guides that exist on api.dokaz.net
  (``GUIDES``: one entry per guide in Scrooge ``worker/src/docs.ts``, the same paths
  ``demand.GUIDES`` scores). Each guide is a product we sell, with a page that sells it, so every
  post points at something a reader can pay for. These are facts about the shelf, not ideas: a
  guide that is not live is not listed, and a guide added to Scrooge must be added here.
- **Several angles per guide** (``ANGLES``): a how-to, the common mistakes, doing it in bulk,
  doing it without code, a checklist, automating it, doing it from Python. A guide's how-to is
  the angle a seed topic about that guide already covers. 22 guides x 7 angles, minus the
  seeds, plus the seeds.
- **Which comes first is measured demand only** (products.demand's ``demand.json``, read fresh or
  not at all): a guide's score is its product's demand score (API calls, caller-days, saturated
  days, guide views), plus that guide's own page views, plus five per free-tool click-through
  to it (when Scrooge reports ``utm_content``), divided by one plus the posts this worker
  already wrote on that guide - so the best-selling guide leads, and the next post goes
  elsewhere once it has had its turn. A guide with no measured score ranks after every scored
  one. UNKNOWN is never zero.
- **With no fresh ranking** (none written yet, stale, unreadable) the order is the fallback:
  the seeds in their order, then the generated topics guide by guide.

**De-duplication.** A topic used by THIS worker (``used_topics``) is never offered again - each
worker keeps its own ledger, so a topic the blog used is still free for Instagram. A topic
retired after ``blog.MAX_TOPIC_BLOCKS`` blocked days is in ``used_topics`` too. Only when every
topic has been used does one come back: the one used longest ago, and not before
``REUSE_AFTER`` (it is then a fresh post, under a fresh slug). So the list cannot run dry while
the posts keep going out; it can only be exhausted by drafting faster than ``REUSE_AFTER``
allows, which ``topics_left`` then says.

Nothing here reads a model or the network.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

REUSE_AFTER = 120 * 86400.0          # a used topic may come back after this long, oldest first
TOOL_CLICK_WEIGHT = 5                # demand.INPUTS weighs a click like five views


@dataclass(frozen=True)
class Guide:
    path: str               # the paid API guide on api.dokaz.net
    product: str            # the product a post names (an allowlisted name)
    does: str               # what it really does (Scrooge docs.ts, in plain words)
    task: str               # the reader's task, as a gerund phrase
    label: str              # the footer link's text
    words: tuple            # for matching a goal


# Scrooge worker/src/docs.ts, one per guide (demand.GUIDES holds the same paths).
GUIDES = (
    Guide("/docs/email-verification-api", "Email Verify",
          "checks an address's syntax, MX records, disposable domains, role accounts and typos",
          "checking email addresses before you send to them", "Email Verify API guide",
          ("email", "verify", "verification", "mx", "bounce", "typo", "deliverability")),
    Guide("/docs/disposable-email-detection-api", "Email Verify",
          "flags disposable and throwaway email domains and role accounts",
          "keeping throwaway email addresses out of a sign-up form",
          "Email Verify guide to disposable addresses",
          ("email", "disposable", "throwaway", "signup", "spam")),
    Guide("/docs/invoice-pdf-api", "Invoice PDF",
          "turns a JSON description of an invoice, estimate or receipt into a clean PDF",
          "making invoice, estimate and receipt PDFs", "Invoice PDF API guide",
          ("invoice", "invoices", "pdf", "receipt", "estimate", "billing")),
    Guide("/docs/qr-code-api", "QR Code API",
          "generates QR codes as SVG or PNG with custom colours and error-correction levels",
          "making QR codes for print and screens", "QR Code API guide",
          ("qr", "code", "codes", "svg", "png")),
    Guide("/docs/wifi-qr-code-api", "QR Code API",
          "encodes a network's name and password as a QR code a phone camera can join",
          "letting guests join a Wi-Fi network by scanning a QR code",
          "QR Code API guide to Wi-Fi codes", ("qr", "wifi", "wi-fi", "network", "guest")),
    Guide("/docs/vcard-qr-code-api", "QR Code API",
          "encodes contact details as a vCard QR code that adds a contact when scanned",
          "sharing contact details with a vCard QR code", "QR Code API guide to vCard codes",
          ("qr", "vcard", "contact", "card")),
    Guide("/docs/website-technology-detection-api", "Site Intel",
          "reads which platform, framework and analytics tags a web page uses",
          "finding out what technology a website is built with",
          "Site Intel guide to technology detection",
          ("site", "website", "tech", "technology", "stack", "seo")),
    Guide("/docs/extract-contact-info-from-website-api", "Site Intel",
          "pulls the public contact details and social links a web page lists",
          "collecting the public contact details a business lists on its own website",
          "Site Intel guide to contact details", ("site", "website", "contact")),
    Guide("/docs/text-summarization-api", "Text AI",
          "summarises text in a set number of sentences, as a paragraph or bullets",
          "summarising long text into a few sentences or bullets",
          "Text AI guide to summaries", ("text", "summary", "summarize", "summarise")),
    Guide("/docs/sentiment-analysis-api", "Text AI",
          "scores text as positive, neutral or negative",
          "scoring the sentiment of reviews and messages", "Text AI guide to sentiment",
          ("text", "sentiment", "review", "reviews", "feedback")),
    Guide("/docs/keyword-extraction-api", "Text AI", "extracts the weighted keywords from a text",
          "pulling the keywords out of a piece of text", "Text AI guide to keywords",
          ("text", "keyword", "keywords", "tags")),
    Guide("/docs/zero-shot-text-classification-api", "Text AI",
          "sorts text into labels you choose, with no training",
          "sorting text into your own categories without training a model",
          "Text AI guide to classification",
          ("text", "classify", "classification", "labels", "categories")),
    Guide("/docs/rewrite-text-tone-api", "Text AI",
          "rewrites text as professional, casual, concise, friendly or formal",
          "rewriting a message in a different tone", "Text AI guide to rewriting",
          ("text", "rewrite", "tone")),
    Guide("/docs/barcode-generator-api", "Barcode API",
          "draws Code 128, EAN-13 and UPC-A barcodes as sharp SVG, check digits included",
          "making barcodes for products, labels and stock", "Barcode API guide",
          ("barcode", "barcodes", "label", "labels", "stock", "inventory")),
    Guide("/docs/ean-13-upc-a-barcode-api", "Barcode API",
          "renders EAN-13 and UPC-A retail barcodes and computes or verifies the check digit",
          "printing retail barcodes with the right check digit",
          "Barcode API guide to EAN-13 and UPC-A", ("barcode", "ean", "upc", "retail")),
    Guide("/docs/markdown-to-pdf-api", "Markdown to PDF",
          "turns Markdown into a clean, paginated PDF in letter or A4",
          "turning Markdown notes and reports into PDF documents",
          "Markdown to PDF API guide", ("markdown", "pdf")),
    Guide("/docs/csv-to-json-api", "Convert API",
          "converts CSV to JSON with quoting, line breaks and headers handled correctly",
          "converting a CSV export into JSON", "Convert API guide to CSV and JSON",
          ("csv", "json", "convert", "spreadsheet", "export")),
    Guide("/docs/json-to-csv-api", "Convert API",
          "turns a JSON list of records into a spreadsheet-ready CSV",
          "turning JSON data into a CSV file a spreadsheet can open",
          "Convert API guide to JSON and CSV", ("json", "csv", "convert", "spreadsheet")),
    Guide("/docs/json-to-excel-api", "Convert API",
          "turns a JSON list into a real Excel workbook with a bold, frozen header",
          "turning JSON data into an Excel workbook", "Convert API guide to Excel",
          ("json", "excel", "xlsx", "spreadsheet", "convert")),
    Guide("/docs/ics-calendar-file-api", "Calendar API",
          "builds a standards-correct calendar file from JSON, time zones and reminders included",
          "sending bookings as calendar files people can add in one click",
          "Calendar API guide to ICS files", ("calendar", "ics", "booking", "event")),
    Guide("/docs/add-to-calendar-link-api", "Calendar API",
          "makes one link that downloads a calendar event, for an add to calendar button",
          "putting an add to calendar button in an email or on a page",
          "Calendar API guide to calendar links", ("calendar", "ics", "event")),
    Guide("/docs/remove-exif-metadata-api", "Image API",
          "strips location, camera and time metadata from JPEG and PNG photos without "
          "re-encoding them",
          "removing location and camera data from photos before you share them",
          "Image API guide to removing EXIF data",
          ("exif", "gps", "photo", "photos", "image", "privacy")),
)
GUIDE_BY_PATH = {g.path: g for g in GUIDES}


@dataclass(frozen=True)
class Angle:
    key: str
    subject: str            # with {task}


# How one guide becomes several posts. Each is a framing of the reader's task, not a subject.
# "how-to" is the angle a seed topic about the same guide already covers.
ANGLES = (
    Angle("how-to", "{task}: a practical how-to"),
    Angle("mistakes", "the common mistakes people make when {task}, and how to avoid them"),
    Angle("in-bulk", "{task} in bulk, from a spreadsheet or a list"),
    Angle("no-code", "{task} for a small business owner who does not write code"),
    Angle("checklist", "a short checklist for {task}"),
    Angle("automate", "automating {task} inside a workflow you already have"),
    Angle("python", "{task} from a short Python script"),
)
BASE_ANGLE = "how-to"
_KEY = re.compile(r"^(?P<guide>[a-z0-9-]+)\.(?P<angle>[a-z0-9-]+)$")


def guide_slug(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def generated_key(guide: Guide, angle: Angle) -> str:
    return f"{guide_slug(guide.path)}.{angle.key}"


def make_topic(guide: Guide, angle: Angle):
    from .blog import Topic
    return Topic(generated_key(guide, angle), angle.subject.format(task=guide.task),
                 guide.product, guide.does, guide.words, guide.path, guide.label)


def generated():
    """Every generated topic, guide by guide (``GUIDES`` order), angle by angle."""
    return tuple(make_topic(g, a) for g in GUIDES for a in ANGLES)


def all_topics():
    """Every topic a posting worker may draft: the seeds, then the generated ones a seed does
    not already cover (a seed is its guide's how-to)."""
    from .blog import SEEDS
    seeded = {t.path for t in SEEDS}
    return tuple(SEEDS) + tuple(t for t in generated()
                                if not (t.key.endswith("." + BASE_ANGLE)
                                        and t.path in seeded))


def topic_by_key(key: str):
    """The topic with this key, seed or generated; None for a key nobody knows (a topic from
    an older release stays in a record and is simply never offered)."""
    for t in all_topics():
        if t.key == key:
            return t
    return None


def angle_of(topic) -> str:
    m = _KEY.match(topic.key)
    return m.group("angle") if m else BASE_ANGLE


# ---- measured demand -> a score per guide ------------------------------------------------------
def _value(inp) -> int | None:
    if not isinstance(inp, dict):
        return None
    v = inp.get("value")
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def guide_scores(doc: dict | None) -> dict:
    """``{guide path: score}`` for every guide with at least one measured input in a demand
    document (``demand.read_demand``). The score is the product's demand score plus the guide's
    own page views plus ``TOOL_CLICK_WEIGHT`` per free-tool click-through to it. A guide with
    nothing measured is absent - unknown, not zero."""
    if not isinstance(doc, dict):
        return {}
    product: dict = {}
    for c in doc.get("ranking") or []:
        if isinstance(c, dict) and c.get("kind") == "api" and isinstance(c.get("ref"), str):
            s = c.get("score")
            if isinstance(s, int) and not isinstance(s, bool):
                product[c["ref"]] = s
    guides = doc.get("guides") if isinstance(doc.get("guides"), dict) else {}
    from .demand import GUIDES as GUIDE_PRODUCT
    out: dict = {}
    for path, pid in GUIDE_PRODUCT.items():
        g = guides.get(path) if isinstance(guides.get(path), dict) else {}
        parts = [product.get(pid), _value(g.get("views"))]
        clicks = _value(g.get("tool_clicks"))
        parts.append(None if clicks is None else TOOL_CLICK_WEIGHT * clicks)
        known = [p for p in parts if p is not None]
        if known:
            out[path] = sum(known)
    return out


# ---- the choice ------------------------------------------------------------------------------------
def free_topics(rec: dict, now: float | None = None) -> list:
    """The topics this worker may still draft, in the fallback order. When none is left, the
    one used longest ago - if that was at least ``REUSE_AFTER`` ago - comes back."""
    used = set(rec.get("used_topics") or [])
    every = all_topics()
    free = [t for t in every if t.key not in used]
    if free or now is None:
        return free
    used_at = rec.get("topic_used_at") if isinstance(rec.get("topic_used_at"), dict) else {}
    old = sorted((float(used_at.get(t.key) or 0), i, t) for i, t in enumerate(every))
    if old and now - old[0][0] >= REUSE_AFTER:
        return [old[0][2]]
    return []


def rank(free: list, rec: dict, doc: dict | None) -> list:
    """``free`` (in fallback order) ordered by measured demand: a guide's score divided by one
    plus the posts this worker has already drafted on it, best first; ties keep the fallback
    order. Topics on a guide with no measured score follow, in the fallback order. With no
    demand document, the fallback order itself."""
    scores = guide_scores(doc)
    if not scores:
        return list(free)
    covered: dict = {}
    for key in rec.get("used_topics") or []:
        t = topic_by_key(key)
        if t is not None:
            covered[t.path] = covered.get(t.path, 0) + 1
    scored = [(scores[t.path] / (1 + covered.get(t.path, 0)), i, t)
              for i, t in enumerate(free) if t.path in scores]
    scored.sort(key=lambda x: (-x[0], x[1]))
    rest = [t for t in free if t.path not in scores]
    return [t for _s, _i, t in scored] + rest


def choose(rec: dict, goal: str | None, doc: dict | None, now: float | None = None):
    """The next topic: the free one the goal's words match best; else the best by measured
    demand (``rank``); else the fallback order. None when nothing is left."""
    free = free_topics(rec, now)
    if goal:
        words = set(re.findall(r"[a-z0-9-]+", goal.lower()))
        scored = sorted(((len(words & set(t.words)), i, t) for i, t in enumerate(free)),
                        key=lambda x: (-x[0], x[1]))
        if scored and scored[0][0] > 0:
            return scored[0][2]
    ranked = rank(free, rec, doc)
    return ranked[0] if ranked else None


def topics_left(rec: dict) -> int:
    used = set(rec.get("used_topics") or [])
    return sum(1 for t in all_topics() if t.key not in used)
