"""The Builds division's product backlog: what Daedalus builds next.

The backlog is a JSON file (``<builds_dir>/backlog.json``, by default
``~/.pionir/builds/backlog.json``), seeded on first use with ``SEED`` - concrete briefs for
small developer tools that fit what the Dokaz API already does (images, calendars, invoices,
data conversion, barcodes, email lists, databases, schedules), each sold for $9-19 on
Gumroad. Every entry is the whole product on paper: its listing (name, summary, price, tags)
and what the code must do (the brief, the features the listing claims, the acceptance tests
and the honest limits). The listing is built from the entry, word for word, so every claim
in it is one the owner's backlog made and Claude's review checks against the code.

The owner edits it by REPLYING to a Builds card (``apply_reply``, owner-only - Pionir records
no one else's reply): ``add <slug>`` with the fields below, ``remove <slug>`` or
``top <slug>``. Moss reprioritises through the division's goal: an entry whose slug the goal
names is taken first (``choose``). An entry already started is never picked again.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from pionir.adapters.products import check_product

from ..blog import _clip

BACKLOG_FILE = "backlog.json"
MIN_PRICE, MAX_PRICE = 900, 1900          # the owner's $9-19 product line
# Python only: a JavaScript product needs node.exe set up for the sandbox user (an absolute
# path in the setup record, its own firewall rule) before its tests can run contained.
LANGUAGES = ("python",)
MAX_ENTRIES = 40
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{2,39}")
_PACKAGE = re.compile(r"[a-z][a-z0-9_]{1,39}")
_COMMAND = re.compile(r"[a-z][a-z0-9-]{1,39}")
_TAG = re.compile(r"[a-z0-9-]{2,24}")
_LINE_BAD = re.compile(r"[\x00-\x1f\x7f<>]")
FIELDS = ("slug", "name", "price_cents", "summary", "tags", "language", "package",
          "command", "brief", "features", "acceptance", "limits")
# Slugs the product shelf already holds (the owner's hand-made products): never reused.
RESERVED = frozenset({"approval-gate", "card-press", "post-guard"})

WHAT_YOU_GET = {
    "python": ("Source code, tests and a README. Python 3.11 or newer, standard library only; "
               "it runs offline and sends nothing anywhere. Single-developer commercial "
               "licence."),
    "javascript": ("Source code, tests and a README. Node.js 20 or newer, no dependencies; it "
                   "runs offline and sends nothing anywhere. Single-developer commercial "
                   "licence."),
}


def _e(slug, name, price, summary, tags, package, command, brief, features, acceptance,
       limits) -> dict:
    return {"slug": slug, "name": name, "price_cents": price, "summary": summary,
            "tags": list(tags), "language": "python", "package": package, "command": command,
            "brief": brief, "features": list(features), "acceptance": list(acceptance),
            "limits": limits}


SEED = (
    _e("exif-strip", "EXIF Strip: remove photo metadata in bulk, losslessly", 1200,
       "Strip EXIF, GPS and other metadata from JPEG and PNG files in bulk without "
       "re-encoding a single pixel. A command-line tool and a Python function.",
       ["python", "privacy", "exif", "images", "cli"], "exif_strip", "exif-strip",
       "EXIF Strip removes the metadata cameras and phones hide in photos - GPS position, "
       "camera serial numbers, capture times, editing software, embedded thumbnails - before "
       "the files are shared or uploaded. It rewrites the file container only: JPEG segments "
       "and PNG chunks that carry metadata are dropped and everything else is copied byte for "
       "byte, so image quality never changes. Point it at files or folders; it reports what "
       "it removed from each file.",
       ["Removes APP1 (EXIF and XMP), APP13 (IPTC) and comment segments from JPEG files",
        "Removes tEXt, iTXt, zTXt, eXIf and tIME chunks from PNG files",
        "Pixel data is copied unchanged: no re-encoding, no quality loss",
        "Processes single files or whole folders, recursively on request",
        "Writes to a new file by default; in-place only with an explicit flag",
        "A dry-run mode that lists what would be removed, and a JSON report option"],
       ["A JPEG with an EXIF block and GPS data comes out with no APP1 segment and "
        "identical image data (compare the scan data bytes)",
        "A PNG with tEXt, iTXt and eXIf chunks comes out with only the critical chunks and "
        "IDAT data byte-identical",
        "A file that is not a JPEG or PNG is skipped with a clear message, never damaged",
        "A truncated or corrupt JPEG is reported as an error and no output is written",
        "Dry-run changes no file on disk",
        "Folder mode with recursion processes nested files and keeps relative paths",
        "The ICC colour profile (APP2) is kept by default"],
       "Only JPEG and PNG. Metadata inside other formats (HEIC, TIFF, RAW, WebP) is not "
       "handled. Visible content in the picture itself - faces, street signs - is not "
       "touched."),
    _e("csv-to-ics", "CSV to ICS: bulk calendar events from a spreadsheet", 1200,
       "Turn a CSV of events into one standards-compliant .ics calendar file that Google "
       "Calendar, Outlook and Apple Calendar import. Command line and Python API.",
       ["python", "calendar", "ics", "csv", "cli"], "csv_to_ics", "csv-to-ics",
       "CSV to ICS converts a spreadsheet of events - title, start, end or duration, "
       "location, description, all-day flag - into a single iCalendar file that every major "
       "calendar imports. Column names are mapped with simple options, dates are read in the "
       "common formats, and the output follows RFC 5545 exactly: line folding, escaping, "
       "stable event ids so re-importing an updated file updates events instead of "
       "duplicating them.",
       ["Reads CSV with configurable column names and delimiter",
        "Timed events with a fixed UTC offset, UTC or floating local time, and all-day events",
        "End time or duration, with sensible validation of both",
        "RFC 5545 output: CRLF line endings, 75-octet line folding, text escaping",
        "Stable UIDs derived from the event data, so updates replace instead of duplicate",
        "Optional reminders (VALARM) a set number of minutes before each event"],
       ["A three-row CSV produces exactly three VEVENT blocks inside one VCALENDAR",
        "Commas, semicolons, backslashes and newlines in titles are escaped per RFC 5545",
        "Lines longer than 75 octets are folded, including multi-byte characters",
        "All-day events use DATE values and an exclusive end date",
        "A row with an end before its start is rejected with the row number in the error",
        "The same input produces byte-identical output twice (stable UIDs, fixed DTSTAMP "
        "option)",
        "Reminder minutes produce a VALARM with the right TRIGGER"],
       "No recurring events (RRULE) in this version. Named time zones are written as UTC "
       "or with a fixed offset rather than as VTIMEZONE blocks."),
    _e("json-to-xlsx", "JSON to Excel: .xlsx files from JSON without dependencies", 1500,
       "Convert JSON arrays and JSON Lines into real .xlsx workbooks with typed cells, one "
       "sheet per array, and no third-party libraries. Command line and Python API.",
       ["python", "excel", "json", "xlsx", "cli"], "json_to_xlsx", "json-to-xlsx",
       "JSON to Excel writes genuine Office Open XML workbooks straight from JSON: an array "
       "of objects becomes a sheet with a header row, nested objects are flattened into "
       "dotted column names, and numbers, booleans and dates stay numbers, booleans and "
       "dates instead of text. It needs nothing beyond the Python standard library, so it "
       "drops into scripts, CI jobs and locked-down servers where installing a spreadsheet "
       "library is not an option.",
       ["Writes valid .xlsx files that Excel, LibreOffice and Google Sheets open",
        "Arrays of objects become sheets with a header row; column order is kept",
        "Nested objects are flattened to dotted column names; lists are written as JSON text",
        "Numbers, booleans and ISO dates are written as typed cells",
        "Several sheets from a JSON object of arrays, or from JSON Lines input",
        "Frozen header row and sensible column widths"],
       ["The output opens as a zip containing the required workbook, sheet and "
        "content-types parts, and its XML parses",
        "Header cells match the keys of the first objects, including keys that only appear "
        "in later rows",
        "Numbers are written as numeric cells and booleans as boolean cells",
        "Nested objects produce dotted column names",
        "Text with ampersands, angle brackets and quotes is escaped correctly",
        "A top-level object of arrays produces one sheet per key, with valid sheet names",
        "Invalid JSON input gives a clear error and writes no file"],
       "Writes values only: no formulas, charts, merged cells or conditional formatting. "
       "Very large inputs are held in memory while the workbook is written."),
    _e("csv-invoices", "CSV Invoices: printable invoices from a spreadsheet", 1500,
       "Generate clean, printable HTML invoices from a CSV of line items - one invoice per "
       "invoice number, with tax and totals computed exactly. Offline, no dependencies.",
       ["python", "invoices", "csv", "freelance", "cli"], "csv_invoices", "csv-invoices",
       "CSV Invoices turns a spreadsheet of billable line items into finished invoices. Rows "
       "are grouped by invoice number; each invoice gets its seller and client blocks, "
       "dates, line items, subtotal, tax and total, laid out in a self-contained HTML file "
       "with print styles, ready to open in a browser and print or save as PDF. Money is "
       "computed with exact decimal arithmetic and rounded the way accountants expect.",
       ["One HTML invoice per invoice number, each a single self-contained file",
        "Exact decimal arithmetic with half-up rounding for line totals, tax and totals",
        "Per-line or per-invoice tax rates, and a currency symbol and code setting",
        "Seller details from a small TOML or JSON settings file",
        "Print styles for A4 and US Letter; no scripts, fonts or images loaded from anywhere",
        "A summary CSV of every invoice generated, with its totals"],
       ["Rows with two invoice numbers produce two invoice files",
        "Line totals, subtotal, tax and total are exact for amounts that binary floats "
        "get wrong",
        "Half-up rounding is applied at two decimal places",
        "Text from the CSV is HTML-escaped in the output",
        "A row with a missing quantity or price is rejected with its row number",
        "The generated HTML contains no script tags and no external references",
        "The summary CSV lists every invoice with its total"],
       "HTML output only; PDF comes from the browser's print dialog. One currency per "
       "invoice. It does not send invoices or track payments."),
    _e("barcode-svg", "Barcode SVG: Code 128 and EAN-13 barcodes as SVG", 1200,
       "Generate crisp Code 128 and EAN-13 barcodes as SVG files, one at a time or in bulk "
       "from a CSV, with check digits computed for you. Pure Python, no dependencies.",
       ["python", "barcode", "svg", "labels", "cli"], "barcode_svg", "barcode-svg",
       "Barcode SVG draws barcodes as small, scalable SVG files that print sharply at any "
       "size. It implements Code 128 (with automatic switching between code sets for the "
       "shortest symbol) and EAN-13 (computing or verifying the check digit), adds the human "
       "readable text underneath, and can produce a whole folder of labels from a CSV in one "
       "command.",
       ["Code 128 with automatic code set selection and the modulo-103 check symbol",
        "EAN-13 with check digit calculation and verification",
        "SVG output with configurable module width, bar height, quiet zone and text",
        "Bulk mode: one SVG per CSV row, with file names from a chosen column",
        "Deterministic output: the same input always gives the same file",
        "A Python API that returns the bar pattern or the SVG text"],
       ["Code 128 encodes a known sample to the published bar pattern and check symbol",
        "Digit-only data switches to code set C and gives a shorter symbol",
        "EAN-13 computes the correct check digit for known product numbers",
        "An EAN-13 with a wrong check digit is rejected",
        "Characters Code 128 cannot encode are rejected with a clear message",
        "The SVG parses as XML and its bar widths add up to the expected module count",
        "Bulk mode writes one file per row"],
       "Linear barcodes only: no QR codes, DataMatrix or PDF417. It does not rasterise to "
       "PNG; use any SVG tool for that."),
    _e("email-list-clean", "Email List Clean: dedupe and check addresses offline", 900,
       "Clean a CSV of email addresses offline: fix formatting, remove duplicates, and flag "
       "invalid syntax, role accounts, disposable domains and common domain typos.",
       ["python", "email", "csv", "data-cleaning", "cli"], "email_list_clean",
       "email-list-clean",
       "Email List Clean tidies a mailing list before it is imported anywhere. It trims and "
       "lower-cases addresses, removes duplicates, and checks each one against practical "
       "syntax rules - all without sending a single network request, so nothing about the "
       "list leaves the machine. Addresses are flagged rather than silently dropped, with a "
       "reason per row, and the original columns are kept.",
       ["Normalises whitespace and case, and removes exact duplicates",
        "Practical syntax validation of the local part and domain",
        "Flags role accounts such as info and admin addresses",
        "Flags disposable email domains from a bundled, editable list",
        "Suggests fixes for common domain typos",
        "Keeps every original column and adds status and reason columns"],
       ["Leading and trailing spaces and upper case are normalised before de-duplication",
        "Duplicates differing only in case are reduced to one row",
        "Addresses without an at sign or with two at signs are flagged invalid",
        "A domain without a dot or with an empty label is flagged invalid",
        "A role account is flagged but kept",
        "A disposable domain from the bundled list is flagged",
        "A misspelt common domain gets a suggested correction",
        "No network module is imported anywhere in the package"],
       "It checks syntax only: it cannot tell whether a mailbox exists, because that would "
       "mean contacting mail servers. The disposable domain list is a starting point, not "
       "complete."),
    _e("csv-to-sqlite", "CSV to SQLite: load spreadsheets into a database", 1200,
       "Load one or many CSV files into a SQLite database with inferred column types, "
       "clean column names and optional indexes, ready to query with SQL.",
       ["python", "sqlite", "csv", "data", "cli"], "csv_to_sqlite", "csv-to-sqlite",
       "CSV to SQLite imports CSV files into a single SQLite database so they can be "
       "explored with plain SQL. It samples each file to infer integer, real and text "
       "columns, turns messy headers into safe column names, creates one table per file, "
       "and loads the rows in a single transaction. Re-running with the replace option "
       "rebuilds a table from the current file.",
       ["One table per CSV file, named after the file",
        "Column type inference: integer, real or text, from a configurable sample",
        "Header cleanup into safe, unique column names",
        "Delimiter and encoding detection with overrides",
        "Replace or append modes, and optional indexes on chosen columns",
        "A summary of tables, row counts and column types after each import"],
       ["A CSV with integers, decimals and text creates matching column types",
        "Headers with spaces, punctuation and duplicates become unique safe names",
        "All rows are loaded and the row count matches the file",
        "Quoted fields containing commas and newlines are loaded intact",
        "Replace mode rebuilds the table; append mode adds rows",
        "A requested index is created on the named column",
        "Values that break the inferred type fall back to text instead of failing"],
       "Loads into SQLite only. Very wide or very large files are loaded row by row but "
       "type inference reads only the sample."),
    _e("cron-explain", "Cron Explain: cron expressions in plain English", 900,
       "Explain cron expressions in plain English and list their next run times, with "
       "clear errors for invalid fields. A command-line tool and a small Python library.",
       ["python", "cron", "devops", "scheduling", "cli"], "cron_explain", "cron-explain",
       "Cron Explain reads a standard five-field cron expression and says what it means in "
       "a plain sentence, then lists the next times it will run from any starting point. "
       "It understands ranges, steps, lists, month and weekday names and the common "
       "shortcuts, and it points at the exact field that is wrong when an expression is "
       "invalid - handy in code review and before a deploy.",
       ["Plain-English descriptions of five-field cron expressions",
        "Ranges, steps, lists, names and shortcuts such as hourly and daily",
        "The next N run times from a given start time",
        "Precise error messages naming the invalid field and value",
        "The classic cron rule for day-of-month and day-of-week together",
        "Usable as a command-line tool or imported as a library"],
       ["An every-minute expression is described as every minute",
        "A weekday business-hours expression is described correctly",
        "Step values in the minute field produce the right next run times",
        "Month and weekday names are accepted in any case",
        "An hour of 24 is rejected with an error naming the hour field",
        "Day-of-month and day-of-week together follow the cron OR rule",
        "Next run times across a month end and a year end are correct"],
       "Five-field cron only: no seconds field and no Quartz extensions. Times are "
       "computed in one time zone; daylight saving changes are not modelled."),
)


# ---- validation -----------------------------------------------------------------------------
def _line(entry, key, lo, hi, reasons) -> None:
    v = entry.get(key)
    if not isinstance(v, str) or not lo <= len(v) <= hi or _LINE_BAD.search(v) \
            or v != v.strip():
        reasons.append(f"{key} must be one line of {lo} to {hi} characters, with no < or >")


def _lines(entry, key, lo, hi, reasons) -> None:
    v = entry.get(key)
    if not isinstance(v, list) or not lo <= len(v) <= hi or not all(
            isinstance(x, str) and 5 <= len(x) <= 240 and not _LINE_BAD.search(x) for x in v):
        reasons.append(f"{key} must be a list of {lo} to {hi} lines of 5 to 240 characters")


def entry_problems(entry) -> list:
    """Every reason this backlog entry cannot be built and listed; empty is the only pass."""
    if not isinstance(entry, dict):
        return ["the entry is not an object"]
    reasons: list = []
    missing = [k for k in FIELDS if k not in entry]
    extra = sorted(set(entry) - set(FIELDS))
    if missing:
        reasons.append(f"missing: {', '.join(missing)}")
    if extra:
        reasons.append(f"unknown: {', '.join(_clip(k, 30) for k in extra[:5])}")
    slug = entry.get("slug")
    if not isinstance(slug, str) or not SLUG.fullmatch(slug):
        reasons.append("slug must be 3 to 40 of a-z, 0-9 and -")
    elif slug in RESERVED:
        reasons.append(f"slug {slug!r} belongs to a product already on the shelf")
    _line(entry, "name", 5, 80, reasons)
    _line(entry, "summary", 20, 200, reasons)
    price = entry.get("price_cents")
    if isinstance(price, bool) or not isinstance(price, int) \
            or not MIN_PRICE <= price <= MAX_PRICE:
        reasons.append(f"price_cents must be whole cents from {MIN_PRICE} to {MAX_PRICE} "
                       "($9-19)")
    tags = entry.get("tags")
    if not isinstance(tags, list) or not 1 <= len(tags) <= 5 or not all(
            isinstance(t, str) and _TAG.fullmatch(t) for t in tags):
        reasons.append("tags must be 1 to 5 of a-z, 0-9 and -")
    if entry.get("language") not in LANGUAGES:
        reasons.append(f"language must be one of {', '.join(LANGUAGES)}")
    if not isinstance(entry.get("package"), str) or not _PACKAGE.fullmatch(entry["package"]):
        reasons.append("package must be a lower-case identifier")
    if not isinstance(entry.get("command"), str) or not _COMMAND.fullmatch(entry["command"]):
        reasons.append("command must be 2 to 40 of a-z, 0-9 and -")
    brief = entry.get("brief")
    if not isinstance(brief, str) or not 60 <= len(brief) <= 1500 or "<" in brief \
            or ">" in brief:
        reasons.append("brief must be one paragraph of 60 to 1500 characters, with no < or >")
    _lines(entry, "features", 2, 8, reasons)
    _lines(entry, "acceptance", 3, 12, reasons)
    limits = entry.get("limits")
    if not isinstance(limits, str) or not 10 <= len(limits) <= 600 or "<" in limits \
            or ">" in limits:
        reasons.append("limits must be 10 to 600 characters, with no < or >")
    if not reasons:
        try:
            check_product(listing_payload(entry, zip_sha="0" * 64, cover_sha="0" * 64))
        except ValueError as exc:
            reasons.append(f"its listing would be refused: {_clip(exc, 160)}")
    return reasons


# ---- the listing, built from the entry word for word -------------------------------------------
def description_md(entry: dict) -> str:
    features = "\n".join(f"- {f}" for f in entry["features"])
    return (f"## What it does\n\n{entry['brief']}\n\n## Features\n\n{features}\n\n"
            f"### Honest limits\n\n{entry['limits']}\n\n### What you get\n\n"
            f"{WHAT_YOU_GET[entry['language']]}\n")


def zip_name(entry: dict, version: str = "1.0.0") -> str:
    return f"{entry['slug']}-{version}.zip"


def listing(entry: dict, version: str = "1.0.0") -> dict:
    """The shelf's ``listing.json`` (crew/products.py LISTING_KEYS, exactly)."""
    return {"slug": entry["slug"], "name": entry["name"], "version": version,
            "price_cents": entry["price_cents"], "pay_what_you_want": False,
            "summary": entry["summary"], "description_md": description_md(entry),
            "tags": list(entry["tags"]), "zip_name": zip_name(entry, version),
            "cover_name": "cover.png", "allow_executables": False}


def listing_payload(entry: dict, *, zip_sha: str, cover_sha: str) -> dict:
    """What the shelf will submit as ``product.gumroad_publish`` for this entry."""
    return {**listing(entry), "zip_sha256": zip_sha, "cover_sha256": cover_sha}


# ---- the file --------------------------------------------------------------------------------
class BacklogUnreadable(ValueError):
    pass


def backlog_path(builds_dir) -> Path:
    return Path(builds_dir) / BACKLOG_FILE


def view(raw: dict) -> dict:
    """The backlog as the worker uses it, from the file's document ``raw`` (kept as it is):
    ``{"raw", "products": [usable entries], "malformed": [{"slug", "reasons"}]}``."""
    good, bad, seen = [], [], set()
    for entry in raw["products"]:
        reasons = entry_problems(entry)
        slug = entry.get("slug") if isinstance(entry, dict) else None
        if not reasons and slug in seen:
            reasons = ["listed twice"]
        if not reasons and len(good) >= MAX_ENTRIES:
            reasons = [f"beyond the first {MAX_ENTRIES} products"]
        if reasons:
            bad.append({"slug": _clip(slug or "?", 40), "reasons": reasons[:4]})
            continue
        seen.add(slug)
        good.append(entry)
    return {"raw": raw, "products": good, "malformed": bad}


def load(builds_dir) -> dict:
    """The backlog (``view``). Seeded with ``SEED`` when there is no file yet; an unreadable
    file raises ``BacklogUnreadable`` (never silently replaced - the owner's additions are in
    it)."""
    path = backlog_path(builds_dir)
    if not path.exists():
        raw = {"products": [dict(e) for e in SEED]}
        save(builds_dir, {"raw": raw})
    else:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BacklogUnreadable(f"{path}: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("products"), list):
        raise BacklogUnreadable(f"{path} has no product list")
    return view(raw)


def save(builds_dir, doc: dict) -> None:
    """Write back exactly the document that was read (``doc["raw"]``): every entry it held -
    ones this worker cannot use, fields it does not know, keys beside ``products`` - kept
    verbatim; only what a reply targeted has changed."""
    from pionir import atomic
    path = backlog_path(builds_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc["raw"], indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    atomic.replace(tmp, path)


def fingerprint(entries) -> str:
    canon = json.dumps([e.get("slug") for e in entries], separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()[:12]


def choose(entries: list, taken: set, goal: str | None) -> dict | None:
    """The next product: among the entries not yet taken, the one Moss's goal names first
    (by where in the goal its slug appears), else the first in the backlog's order."""
    free = [e for e in entries if e["slug"] not in taken]
    if not free:
        return None
    text = (goal or "").lower()
    named = []
    for e in free:
        m = re.search(r"(?<![a-z0-9-])" + re.escape(e["slug"]) + r"(?![a-z0-9-])", text)
        if m:
            named.append((m.start(), e))
    if named:
        return min(named, key=lambda p: p[0])[1]
    return free[0]


# ---- the owner's replies --------------------------------------------------------------------------
_FIELD_LINE = re.compile(r"(?i)^\s*(name|price|summary|tags|language|package|command|brief|"
                         r"limits|features|tests|acceptance)\s*:\s*(.*)$")
_BULLET = re.compile(r"^\s*[-*]\s+(.+)$")


def _price_cents(text: str):
    m = re.fullmatch(r"\s*\$?\s*(\d{1,3})(?:\.(\d{2}))?\s*(?:usd)?\s*", text or "", re.IGNORECASE)
    if m is None:
        return None
    return int(m.group(1)) * 100 + int(m.group(2) or 0)


def parse_add(slug: str, body: str) -> tuple:
    """An ``add <slug>`` reply's fields -> ``(entry, reasons)``. Fields are ``key: value``
    lines; ``features:`` and ``tests:`` take the ``- item`` lines that follow; ``brief:``
    takes the rest of its line and the plain lines that follow."""
    fields: dict = {}
    current = None
    for raw in body.splitlines():
        m = _FIELD_LINE.match(raw)
        if m:
            key = m.group(1).lower()
            key = "acceptance" if key == "tests" else key
            current = key
            value = m.group(2).strip()
            if key in ("features", "acceptance"):
                fields[key] = [value] if value else []
            else:
                fields[key] = value
            continue
        b = _BULLET.match(raw)
        if b and current in ("features", "acceptance"):
            fields[current].append(b.group(1).strip())
        elif raw.strip() and current in ("brief", "limits"):
            fields[current] = (fields[current] + " " + raw.strip()).strip()
    price = _price_cents(fields.get("price", ""))
    package = fields.get("package") or slug.replace("-", "_")
    entry = {"slug": slug, "name": fields.get("name", ""),
             "price_cents": price if price is not None else -1,
             "summary": fields.get("summary", ""),
             "tags": [t.strip().lower() for t in fields.get("tags", "").split(",") if t.strip()],
             "language": (fields.get("language") or "python").lower(),
             "package": package, "command": fields.get("command") or slug,
             "brief": fields.get("brief", ""), "features": fields.get("features", []),
             "acceptance": fields.get("acceptance", []),
             "limits": fields.get("limits") or "See the README for what it does not do."}
    reasons = entry_problems(entry)
    if price is None:
        reasons = [r for r in reasons if not r.startswith("price_cents")]
        reasons.insert(0, "price must be a dollar amount from 9 to 19, like price: 12")
    return entry, reasons


ADD_HELP = ("add <slug>\nname: ...\nprice: 12\nsummary: ...\ntags: python, cli\n"
            "brief: one paragraph\nfeatures:\n- ...\ntests:\n- ...\nlimits: ...")


def apply_reply(doc: dict, text: str, taken: set) -> tuple:
    """One owner reply applied to the backlog: ``(changed, what happened in words)``."""
    text = (text or "").strip()
    first, _, rest = text.partition("\n")
    words = first.split()
    verb = words[0].lower() if words else ""
    slug = words[1].lower() if len(words) > 1 else ""
    raw = doc["raw"]["products"]

    def slug_of(e):
        return e.get("slug") if isinstance(e, dict) else None

    known = {slug_of(e) for e in raw}
    if verb == "remove" and slug:
        if slug not in known:
            return False, f"remove {slug}: it is not in the backlog"
        # only entries of exactly this slug go; everything else stays as it was written
        doc["raw"]["products"] = [e for e in raw if slug_of(e) != slug]
        doc.update(view(doc["raw"]))
        return True, f"removed {slug}"
    if verb == "top" and slug:
        if slug not in {e["slug"] for e in doc["products"]}:
            return False, f"top {slug}: it is not a usable product in the backlog"
        if slug in taken:
            return False, f"top {slug}: it was already built or is being built"
        i = next(i for i, e in enumerate(raw) if slug_of(e) == slug)
        doc["raw"]["products"] = [raw[i], *raw[:i], *raw[i + 1:]]
        doc.update(view(doc["raw"]))
        return True, f"{slug} is next"
    if verb == "add" and slug:
        if slug in known or slug in taken:
            return False, f"add {slug}: that slug is already used"
        if len(doc["products"]) >= MAX_ENTRIES:
            return False, f"add {slug}: the backlog is full ({MAX_ENTRIES} products)"
        entry, reasons = parse_add(slug, rest)
        if reasons:
            return False, f"add {slug} was NOT added: " + "; ".join(reasons[:4])
        doc["raw"]["products"] = [*raw, entry]
        doc.update(view(doc["raw"]))
        return True, f"added {slug} ({entry['name']}, ${entry['price_cents'] / 100:.2f})"
    return False, ("not understood - reply with 'add <slug>' and its fields, "
                   "'remove <slug>' or 'top <slug>'")
