"""The sponsor media-kit page: what a brand may read before it writes to us.

Part of the staging site (``build_site`` writes it to ``site/sponsor/index.html``, never
deploys it). It claims only what can be proven:

* **Audience numbers come from one place**: ``<video dir>/analytics.json``, a record Ian makes
  from the channel's real YouTube Studio figures (``as_of``, ``source`` and the figures). With
  no such file, a malformed one, a future or stale ``as_of`` (older than ``MAX_AGE_DAYS``), or
  no video recorded as published on the channel, the page says the channel is too new and shows
  NO number at all. It never estimates, rounds up or projects.
* **Packages are plain text.** The defaults describe what a sponsor gets without any price;
  a price appears only when Ian puts one in ``sponsor.json``.
* **No logos, testimonials, ratings or "as seen on" claims** - nothing here can show one, and
  the JSON-LD (Organization and Service) carries no rating, review or offer.
* **Enquiries go to Ian** by a ``mailto:`` link to the address in ``sponsor.json`` or a form
  whose ``action`` is the https URL in ``sponsor.json``. With neither set the page says
  enquiries are not open. Nothing is ever replied to automatically.
* Every string from either file is HTML-escaped, and JSON-LD is serialised so that no string can
  close its ``<script>`` element.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from .disclosure import DISCLOSURE

SPONSOR_FILE = "sponsor.json"
ANALYTICS_FILE = "analytics.json"
MAX_AGE_DAYS = 45
NOT_ENOUGH = "The channel is too new: there is no audience data yet, so none is shown."
_EMAIL = re.compile(r"^[^@\s<>\"',;()\[\]\\]{1,64}@[A-Za-z0-9.-]{1,80}\.[A-Za-z]{2,24}$")
_FIGURES = (("views_28d", "Views, last 28 days", 0), ("subscribers", "Subscribers", 0),
            ("watch_hours", "Watch hours, last 28 days", 1), ("videos_measured", "Videos measured", 0))

DEFAULT_PACKAGES = (
    {"name": "A mention in one video",
     "description": "A short spoken and written credit in a single video, labelled as a "
                    "sponsorship, with a link in the description."},
    {"name": "A series sponsor",
     "description": "Your name credited in each video of one series for an agreed run, "
                    "labelled as a sponsorship every time."},
    {"name": "A page on this site",
     "description": "A labelled sponsor line on the series page and the video pages it covers."},
)


@dataclass(frozen=True, slots=True)
class SponsorConfig:
    organization: str
    url: str | None
    email: str | None
    form_action: str | None
    packages: tuple[dict[str, str], ...]
    problems: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Analytics:
    as_of: str
    source: str
    figures: tuple[tuple[str, str], ...]     # (label, formatted number)
    channel_url: str | None


def _https(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parts = urlsplit(value)
    ok = parts.scheme == "https" and parts.hostname and not parts.username and len(value) <= 300
    return value if ok else None


def _text(value: Any, limit: int) -> str | None:
    if isinstance(value, str) and value.strip() and len(value) <= limit:
        return " ".join(value.split())
    return None


def load_config(video_dir: Path) -> SponsorConfig:
    """Read ``sponsor.json``; a bad field is dropped and named in ``problems``, never guessed."""
    problems: list[str] = []
    try:
        raw = json.loads((Path(video_dir) / SPONSOR_FILE).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("not an object")
    except FileNotFoundError:
        raw = {}
    except (OSError, ValueError) as error:
        problems.append(f"{SPONSOR_FILE} is unreadable ({error}); using the defaults")
        raw = {}
    organization = _text(raw.get("organization"), 80) or "Dokaz Industries"
    url = _https(raw.get("url"))
    if raw.get("url") and url is None:
        problems.append("url must be a plain https URL; ignored")
    email = raw.get("contact_email")
    if email is not None and not (isinstance(email, str) and _EMAIL.match(email)):
        problems.append("contact_email is not an email address; ignored")
        email = None
    form = _https(raw.get("form_action"))
    if raw.get("form_action") and form is None:
        problems.append("form_action must be an https URL; ignored")
    packages: list[dict[str, str]] = []
    for item in raw.get("packages") or []:
        name = _text(item.get("name") if isinstance(item, dict) else None, 80)
        desc = _text(item.get("description") if isinstance(item, dict) else None, 400)
        if not name or not desc:
            problems.append("a package needs a name and a description; skipped")
            continue
        entry = {"name": name, "description": desc}
        if item.get("price") is not None:
            price = _text(item.get("price"), 60)
            if price is None:
                problems.append(f"package {name!r}: price must be short text; ignored")
            else:
                entry["price"] = price
        packages.append(entry)
    return SponsorConfig(organization, url, email, form, tuple(packages or DEFAULT_PACKAGES),
                         tuple(problems))


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value >= 0 and value == value and value != float("inf") else None


def load_analytics(video_dir: Path, *, today: date, published: int) -> tuple[Analytics | None, str]:
    """``(analytics, "")`` only for real, recent, well-formed figures on a channel with at least
    one published video; otherwise ``(None, why)``."""
    if published < 1:
        return None, "no video is recorded as published on the channel yet"
    try:
        raw = json.loads((Path(video_dir) / ANALYTICS_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"there is no {ANALYTICS_FILE}"
    except (OSError, ValueError):
        return None, f"{ANALYTICS_FILE} is unreadable"
    if not isinstance(raw, dict):
        return None, f"{ANALYTICS_FILE} is not an object"
    source = _text(raw.get("source"), 120)
    if source is None:
        return None, "the figures do not say where they come from (source)"
    try:
        as_of = date.fromisoformat(str(raw.get("as_of")))
    except ValueError:
        return None, "as_of is not a date"
    if as_of > today:
        return None, "as_of is in the future"
    if (today - as_of).days > MAX_AGE_DAYS:
        return None, f"the figures are older than {MAX_AGE_DAYS} days"
    figures: list[tuple[str, str]] = []
    for key, label, decimals in _FIGURES:
        if key not in raw:
            continue
        value = _number(raw[key])
        if value is None:
            return None, f"{key} is not a non-negative number"
        figures.append((label, f"{value:,.{decimals}f}"))
    if not figures:
        return None, "the file holds no figures"
    return Analytics(as_of.isoformat(), source, tuple(figures), _https(raw.get("channel_url"))), ""


def sponsor_page(config: SponsorConfig, analytics: Analytics | None,
                 groups: dict[str, tuple[str, list[dict[str, Any]]]], base: str) -> str:
    from .pages import _e, _shell, json_ld

    url = f"{base}/sponsor/"
    audience = ""
    if analytics is None:
        audience = f"<p>{_e(NOT_ENOUGH)}</p>"
    else:
        rows = "".join(f"<tr><th scope=\"row\">{_e(label)}</th><td>{_e(number)}</td></tr>"
                       for label, number in analytics.figures)
        link = (f' <a href="{_e(analytics.channel_url)}" rel="noopener">The channel</a>.'
                if analytics.channel_url else "")
        audience = (f"<table>{rows}</table><p class=\"meta\">As of {_e(analytics.as_of)}. "
                    f"Source: {_e(analytics.source)}. These are the channel's own figures, "
                    f"unrounded and unprojected.{link}</p>")

    packages = []
    for p in config.packages:
        price = (f"<p><strong>Price:</strong> {_e(p['price'])}</p>" if p.get("price") else "")
        packages.append(f"<li><h3>{_e(p['name'])}</h3><p>{_e(p['description'])}</p>{price}</li>")
    pricing = ("" if any(p.get("price") for p in config.packages)
               else "<p>We have not set prices. They are agreed with each sponsor by email.</p>")

    if config.email:
        subject = quote("Sponsorship enquiry")
        contact = (f'<p>Write to <a href="mailto:{_e(config.email)}?subject={subject}">'
                   f"{_e(config.email)}</a>. A person reads every enquiry; nothing is answered "
                   "automatically.</p>")
    else:
        contact = "<p>Sponsorship enquiries are not open yet.</p>"
    if config.form_action:
        contact += (f'<form method="post" action="{_e(config.form_action)}">'
                    '<p><label>Your name <input name="name" required maxlength="120"></label></p>'
                    '<p><label>Company <input name="company" maxlength="120"></label></p>'
                    '<p><label>Email <input name="email" type="email" required maxlength="120">'
                    "</label></p>"
                    '<p><label>What you have in mind <textarea name="message" rows="5" '
                    'maxlength="2000"></textarea></label></p>'
                    '<p><button type="submit">Send</button></p></form>')

    hubs = "".join(f'<li><a href="../{_e(slug)}/">{_e(series)}</a></li>'
                   for slug, (series, _items) in groups.items())
    recent = [(slug, m) for slug, (_series, items) in groups.items() for m in items]
    recent.sort(key=lambda t: (t[1]["created_at"], t[1]["id"]), reverse=True)
    videos = "".join(f'<li><a href="../{_e(slug)}/{_e(m["id"])}/">{_e(m["title"])}</a></li>'
                     for slug, m in recent[:6])

    body = (
        '<nav><a href="../">All series</a></nav>'
        f"<h1>Sponsor these videos</h1>"
        f"<p>{_e(config.organization)} makes sourced, narrated videos. Every sponsorship is "
        "labelled as one, on screen and in the description.</p>"
        f"<h2>Audience</h2>{audience}"
        f"<h2>Ways to sponsor</h2><ul>{''.join(packages)}</ul>{pricing}"
        f"<h2>Get in touch</h2>{contact}"
        + (f"<h2>The series</h2><ul>{hubs}</ul>" if hubs else "")
        + (f"<h2>Recent videos</h2><ul>{videos}</ul>" if videos else "")
        + f'<p class="disclosure">{_e(DISCLOSURE)}</p>')

    organization: dict[str, Any] = {"@type": "Organization", "name": config.organization}
    if config.url:
        organization["url"] = config.url
    if config.email:
        organization["contactPoint"] = {"@type": "ContactPoint", "contactType": "sponsorships",
                                        "email": config.email}
    ld = {"@context": "https://schema.org",
          "@graph": [organization,
                     {"@type": "Service", "name": "Video sponsorship", "url": url,
                      "serviceType": "Sponsorship",
                      "description": "Labelled sponsor credits in sourced, narrated videos.",
                      "provider": {"@type": "Organization", "name": config.organization}}]}
    head = (f'<link rel="canonical" href="{_e(url)}">'
            f'<script type="application/ld+json">{json_ld(ld)}</script>')
    return _shell("Sponsor these videos", body, head,
                  "How to sponsor these videos, and what is known about the audience.")
