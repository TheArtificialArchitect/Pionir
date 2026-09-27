"""The checks every Fiverr deliverable passes before the owner's READY card is posted.

A failed check is **no card**: the owner gets a short "blocked" card naming the reasons and
none of the files. Fail closed - a secrets file that cannot be read is a failed check, never
a skipped one.

What is checked, reusing the estate's own checks rather than a third copy:

- **secrets** - ``adapters.deliveries``' scan: the value of every file under the owner's
  secrets folder (``~/.pionir/secrets``), any SSH private key, and every common key format
  (``KEY_PATTERNS``). A zip is checked with ``inspect_zip`` itself (a README, no executables,
  no path tricks, every entry scanned);
- **the owner's personal data** - his home folder, his user name and any marker he lists
  (``PIONIR_FIVERR_OWNER_MARKERS``: his real name, his personal email ...);
- **local paths** - a drive path (``C:\\...``), a UNC path, ``/Users/``, ``/home/``,
  ``AppData``, ``.pionir``;
- **internal system names** - ``contentcheck.INTERNAL_NAMES`` (Pionir, Moss, Scrooge ...);
- **contact details and links** (text the buyer reads that is ours: replies, reports) -
  Fiverr forbids moving a buyer off the platform, so no email address, phone number or link;
- **money** in a reply - a price is never the crew's to state.

The buyer's own words are theirs: something a check flags that the buyer's brief itself
contains (their business name, their phone number on their own website) is not a reason.
The buyer's own DATA (a cleaned spreadsheet) is checked for the owner's SECRETS only: its
names, emails, phone numbers and paths are the buyer's (the owner's markers are looked for in
what we wrote, never in a buyer's data - a customer list may well have an "Ian" in it).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from pionir.adapters.deliveries import (
    DeliveryProblem,
    SecretValues,
    inspect_zip,
    load_secrets,
    scan_bytes,
)

from ..contentcheck import INTERNAL_NAMES
from ..finder import personal_data

MAX_REPLY = 2500                 # Fiverr's message box
_INTERNAL = re.compile(r"(?i)(?<![a-z0-9])(" + "|".join(INTERNAL_NAMES) + r")(?![a-z0-9])")
_LOCAL_PATH = re.compile(
    r"(?i)(?<![a-z0-9])[a-z]:[\\/](?:users|windows|program|src|temp|tmp)?"
    r"|\\\\[a-z0-9._-]+\\[a-z0-9$._-]+"
    r"|/(?:users|home|root|tmp|var|etc)/[^\s]"
    r"|\bappdata\b|\.pionir\b")
_LINK = re.compile(r"(?i)\b(?:https?://|www\.)\S+|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\."
                   r"(?:com|net|org|io|co|uk|de|dev|app|ai|me|us|info|biz|xyz|site|online)\b")
_WEB_URL = re.compile(r"(?i)\bhttps?://\S+")
_MONEY = re.compile(r"(?i)[$€£]\s?\d|\b\d[\d,.]*\s?(?:usd|dollars?|eur|euros?|gbp|pounds?)\b")
_HANDMADE = re.compile(r"(?i)\b(?:hand[- ]?(?:made|written|crafted|coded|built)|by hand|"
                       r"manually (?:written|coded|built|made|crafted|researched)|"
                       r"100\s?% human|no ai\b|without (?:any )?ai\b|ai[- ]free|"
                       r"human[- ]written|not (?:made|written|generated) by ai)")


@dataclass(frozen=True)
class Guard:
    """What the owner must never leak, loaded fresh for each check run."""
    secrets: SecretValues = field(default_factory=SecretValues)
    markers: tuple = ()          # lower-case owner markers (name, email, home folder ...)


def owner_markers(extra=()) -> tuple:
    """The owner's home folder (both slash forms), his user name, and every marker he
    configured - lower case, each at least 3 characters (shorter would flag ordinary text)."""
    found: list = []
    try:
        home = Path.home()
        found += [str(home), str(home).replace("\\", "/"), home.name]
    except RuntimeError:
        pass
    env = os.environ.get("PIONIR_FIVERR_OWNER_MARKERS", "")
    found += [m for m in env.split(",")]
    found += list(extra)
    out = []
    for m in found:
        m = " ".join(str(m).split()).lower()
        if len(m) >= 3 and m not in out:
            out.append(m)
    return tuple(out)


def load_guard(secrets_dir, ssh_dir=None, markers=()) -> Guard:
    """The owner's secret values and markers. Raises DeliveryProblem when a secrets file
    exists but cannot be read (fail closed: a value that cannot be read cannot be checked)."""
    secrets = load_secrets(Path(secrets_dir) if secrets_dir else None, (),
                           Path(ssh_dir).expanduser() if ssh_dir else None)
    return Guard(secrets=secrets, markers=tuple(markers))


def _given(term: str, brief: str) -> bool:
    """The buyer's brief contains this term as a whole word or phrase (never as a piece of a
    longer word: "ian" is not given by "Christian")."""
    term = (term or "").strip().lower()
    if not term or not brief:
        return False
    return re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])",
                     brief.lower()) is not None


_BRIEF_URL = re.compile(r"(?i)\b(?:https?://)?((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
                        r"[a-z]{2,63})\b(/[^\s\"'<>)]*)?")
_BRIEF_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,63})")


def bare_host(host: str) -> str:
    host = (host or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def brief_sites(brief: str) -> list:
    """Every site the brief names, as ``(host without www., path)``; the domain part of an
    email address is not a site."""
    out = []
    for m in _BRIEF_URL.finditer(brief or ""):
        if "@" in (brief or "")[max(0, m.start() - 1):m.start()]:
            continue
        path = (m.group(2) or "").rstrip(".,;:!?")
        out.append((bare_host(m.group(1)), path.rstrip("/")))
    return out


def brief_emails(brief: str) -> set:
    """Every email address the brief gives, exactly (lower case)."""
    return {m.group(0).lower().rstrip(".") for m in _BRIEF_EMAIL.finditer(brief or "")}


def site_given(host: str, path: str, brief: str) -> bool:
    """A link to ``host``/``path`` is one the brief gives: its host EXACTLY a host the brief
    names (``www.`` aside) or a proper subdomain of one, and - when the brief named that host
    with a path - the same path. Never a substring: "evilrosabakes.co.uk" is not given by
    "rosabakes.co.uk", nor "instagram.com/someone-else" by "instagram.com/rosabakes"."""
    host = bare_host(host)
    path = (path or "").rstrip("/")
    for given_host, given_path in brief_sites(brief):
        if host != given_host and not host.endswith("." + given_host):
            continue
        if not given_path or path == given_path:
            return True
    return False


def _host_given(link: str, brief: str) -> bool:
    """The buyer's own site, named in a text we wrote: see ``site_given``."""
    m = re.match(r"(?i)^(?:https?://)?([^/?#\s]+)([^?#\s]*)", link or "")
    if not m:
        return False
    host = m.group(1).lower().rstrip(".,;:!?)")
    return "." in host and site_given(host, m.group(2).rstrip(".,;:!?)"), brief)


def check_text(name: str, text: str, guard: Guard, *, brief: str = "", ours: bool = True,
               contact_ok: bool = False, links_ok: bool = False) -> list:
    """Every reason this text may not reach the buyer. ``ours`` is text we wrote (a report,
    a reply, a README, a website): it also may not name an internal system or carry contact
    details (unless ``contact_ok``: the website's buyer-given contacts, checked there) or
    links (unless ``links_ok``: a research report's checked shop links, a website's checked
    links). Anything flagged that the buyer's own brief contains is theirs, not a reason."""
    if not isinstance(text, str):
        return [f"{name} is not text"]
    reasons: list = []
    secret = scan_bytes(name, text.encode("utf-8"), guard.secrets)
    if secret:
        reasons.append(secret)
    if ours:
        # the owner's markers are looked for only in what WE wrote: a buyer's own data (a
        # customer list with an "Ian" in it) is theirs, and is never stopped for it
        low = text.lower()
        for marker in guard.markers:
            pattern = r"(?<![a-z0-9])" + re.escape(marker) + r"(?![a-z0-9])"
            if re.search(pattern, low) and not _given(marker, brief):
                reasons.append(f"{name} contains the owner's personal data (a marker from his "
                               "home folder, user name or PIONIR_FIVERR_OWNER_MARKERS)")
                break
        for m in _LOCAL_PATH.finditer(_WEB_URL.sub(" ", text)):
            if not _given(m.group(0), brief):
                reasons.append(f"{name} contains a local path ({m.group(0)[:30]!r})")
                break
        for m in _INTERNAL.finditer(text):
            if not _given(m.group(1), brief):
                reasons.append(f"{name} names an internal system ({m.group(1)!r})")
                break
        if not contact_ok:
            for what in personal_data(text):
                reasons.append(f"{name} has {what} (Fiverr forbids contact details)")
        if not links_ok:
            for m in _LINK.finditer(text):
                if not _host_given(m.group(0), brief):
                    reasons.append(f"{name} has a link or domain ({m.group(0)[:60]!r}); Fiverr "
                                   "forbids sending a buyer off the platform")
                    break
    return reasons


def check_reply(text: str, guard: Guard) -> list:
    """A drafted reply the owner will paste to the buyer on Fiverr."""
    reasons = check_text("the drafted reply", text, guard)
    if not isinstance(text, str):
        return reasons
    if not 20 <= len(text) <= MAX_REPLY:
        reasons.append(f"the drafted reply must be 20 to {MAX_REPLY} characters")
    if _MONEY.search(text):
        reasons.append("the drafted reply states an amount of money (a price is never ours to "
                       "state)")
    if _HANDMADE.search(text):
        reasons.append("the drafted reply claims the work is hand-made")
    return reasons


def check_file(path: Path, guard: Guard, *, brief: str = "", ours: bool = True,
               contact_ok: bool = False, links_ok: bool = False) -> list:
    """One deliverable file on disk. A zip goes through ``inspect_zip`` (every entry
    scanned); a text file through ``check_text``; anything else (an image, a workbook we
    wrote from checked rows) through the secrets scan of its raw bytes."""
    path = Path(path)
    try:
        if not path.is_file() or path.is_symlink():
            return [f"{path.name} is not a regular file"]
        if path.suffix.lower() == ".zip":
            inspect_zip(path, guard.secrets, folder="the order's folder")
            return []
        data = path.read_bytes()
    except DeliveryProblem as problem:
        return [f"{path.name}: {problem}"]
    except OSError as exc:
        return [f"{path.name} could not be read ({type(exc).__name__})"]
    if path.suffix.lower() in (".md", ".txt", ".csv", ".json", ".html", ".css", ".tsv"):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return [f"{path.name} is not UTF-8 text"]
        return check_text(path.name, text, guard, brief=brief, ours=ours,
                          contact_ok=contact_ok, links_ok=links_ok)
    if path.suffix.lower() == ".xlsx":
        return _check_workbook(path.name, data, guard)
    secret = scan_bytes(path.name, data, guard.secrets)
    return [secret] if secret else []


def _check_workbook(name: str, data: bytes, guard: Guard) -> list:
    """A workbook is a zip of XML: every entry is scanned unpacked (a secret would not show
    in the compressed bytes)."""
    import io
    import zipfile
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            reasons: list = []
            for info in z.infolist():
                if info.file_size > 200_000_000:
                    return [f"{name}: an entry unpacks too large to scan"]
                raw = z.read(info)
                secret = scan_bytes(f"{name}/{info.filename}", raw, guard.secrets)
                if secret:
                    reasons.append(secret)
                reasons += [r for r in check_text(f"{name}/{info.filename}",
                                                  raw.decode("utf-8", "replace"), guard,
                                                  ours=False)
                            if r != secret]
            return reasons
    except (zipfile.BadZipFile, OSError) as exc:
        return [f"{name} is not a readable workbook ({type(exc).__name__})"]
