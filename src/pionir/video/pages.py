"""Static pages for staged videos: transcript, sources, disclosure, schema markup, interlinks.

``build_site(video_dir)`` reads every package under ``<video_dir>/queue`` and writes a small
static site into ``<video_dir>/site`` (the staging folder, nothing else). It never deploys, never
touches the network, and never writes outside that folder.

What a page claims is limited to what the package proves:

* every piece of text from a manifest or script is HTML-escaped; JSON-LD is serialised with
  ``<``, ``>``, ``&`` and the line separators escaped so a hostile title cannot leave its
  ``<script>`` element,
* only ``https`` source links become links; anything else is printed as inert text,
* ``uploadDate`` and ``embedUrl`` appear only when the manifest records a real PUBLIC upload
  (an upload is private until Ian makes it public in Studio and records that with
  ``pionir video published``), so a page never says a video is on YouTube before anyone can
  watch it,
* there are no view counts, ratings, dates of publication or any other figure the pipeline did
  not measure itself (the duration comes from the rendered file),
* an example niche's video gets no page at all, and a package that fails ``verify_package`` is
  skipped and reported rather than published.
"""
from __future__ import annotations

import html
import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .disclosure import DISCLOSURE
from .package import ID_RE, PackageError, load_package, queue_dir, slugify, verify_package

SITE = "site"
DEFAULT_BASE_URL = "https://dokazindustries.com/video"
_YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{6,20}$")

_CSS = (
    "body{font:17px/1.6 system-ui,sans-serif;max-width:46rem;margin:2rem auto;padding:0 1rem;"
    "color:#1d1d1f;background:#fff}a{color:#0b57d0}nav{font-size:.9rem;margin-bottom:1.5rem}"
    "img{max-width:100%;height:auto}h1{line-height:1.2}.disclosure{border-left:4px solid #888;"
    "padding:.25rem 1rem;background:#f4f4f4}sup{font-size:.75em}.meta{color:#555;font-size:.9rem}"
)


class SiteError(ValueError):
    """The base URL or output location is not one a page may be built for."""


@dataclass(slots=True)
class Site:
    root: Path
    pages: list[Path] = field(default_factory=list)
    skipped: dict[str, list[str]] = field(default_factory=dict)


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def iso_duration(seconds: float) -> str:
    """ISO 8601 duration, whole seconds: 125.4 -> PT2M5S."""
    total = max(0, round(float(seconds)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    out = "PT" + (f"{hours}H" if hours else "") + (f"{minutes}M" if minutes else "")
    return out + (f"{secs}S" if secs or out == "PT" else "")


def clock(seconds: float) -> str:
    total = max(0, round(float(seconds)))
    return f"{total // 60}:{total % 60:02d}"


def json_ld(document: dict[str, Any]) -> str:
    """JSON safe to put inside a ``<script>`` element, whatever the strings contain."""
    text = json.dumps(document, ensure_ascii=True, indent=2)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _https(url: Any) -> str | None:
    if not isinstance(url, str):
        return None
    parts = urlsplit(url)
    return url if parts.scheme == "https" and parts.hostname and not parts.username else None


def _upload(manifest: dict[str, Any]) -> tuple[str, str] | None:
    """(uploadDate, embedUrl) only from a well-formed recorded upload, else None."""
    uploaded = manifest.get("uploaded")
    if not isinstance(uploaded, dict):
        return None
    stamp, youtube_id = uploaded.get("upload_date"), uploaded.get("youtube_id")
    if uploaded.get("privacy") != "public":
        return None
    try:
        datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if not isinstance(youtube_id, str) or not _YOUTUBE_ID.match(youtube_id):
        return None
    return str(stamp), f"https://www.youtube.com/embed/{youtube_id}"


def _base(base_url: str) -> str:
    parts = urlsplit(base_url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.query \
            or parts.fragment:
        raise SiteError(f"base_url must be a plain https URL, not {base_url!r}")
    return base_url.rstrip("/")


def _shell(title: str, body: str, head_extra: str = "", description: str = "") -> str:
    meta = f'<meta name="description" content="{_e(description)}">' if description else ""
    return ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{_e(title)}</title>{meta}<style>{_CSS}</style>{head_extra}</head>"
            f"<body>{body}</body></html>\n")


def _video_page(m: dict[str, Any], script: dict[str, Any], base: str, series_slug: str,
                prev: dict[str, Any] | None, nxt: dict[str, Any] | None,
                siblings: list[dict[str, Any]]) -> str:
    url = f"{base}/{series_slug}/{m['id']}/"
    hub = f"{base}/{series_slug}/"
    sources = m["sources"]
    number = {s["id"]: i + 1 for i, s in enumerate(sources)}
    upload = _upload(m)

    transcript = []
    for scene in script.get("scenes", []):
        transcript.append(f"<h3>{_e(scene.get('heading', ''))}</h3>")
        for line in scene.get("lines", []):
            marks = "".join(f"<sup>[{number[s]}]</sup>" for s in line.get("sources", [])
                            if s in number)
            transcript.append(f"<p>{_e(line.get('text', ''))}{marks}</p>")

    source_items = []
    for i, s in enumerate(sources, 1):
        link = _https(s.get("url"))
        where = f'<a href="{_e(link)}" rel="noopener">{_e(link)}</a>' if link \
            else _e(s.get("url", ""))
        source_items.append(f"<li id=\"s{i}\">{_e(s.get('title', ''))}. {_e(s.get('credit', ''))}. "
                            f"{where}</li>")
    for image in m.get("images", []):
        link = _https(image.get("url"))
        where = f'<a href="{_e(link)}" rel="noopener">{_e(link)}</a>' if link \
            else _e(image.get("url", ""))
        source_items.append(f"<li>Image: {_e(image.get('credit', ''))}. {where}</li>")

    related = "".join(
        f'<li><a href="../{_e(o["id"])}/">{_e(o["title"])}</a></li>'
        for o in siblings if o["id"] != m["id"])
    nav = ['<a href="../../">All series</a>', '<a href="../">' + _e(m["series"]) + "</a>",
           '<a href="../../sponsor/">Sponsor</a>']
    if prev:
        nav.append(f'<a rel="prev" href="../{_e(prev["id"])}/">Previous: {_e(prev["title"])}</a>')
    if nxt:
        nav.append(f'<a rel="next" href="../{_e(nxt["id"])}/">Next: {_e(nxt["title"])}</a>')

    if upload:
        player = (f'<p><iframe src="{_e(upload[1])}" title="{_e(m["title"])}" width="560" '
                  'height="315" allowfullscreen loading="lazy"></iframe></p>')
    else:
        player = "<p class=\"meta\">This video is not published on YouTube yet.</p>"

    ld: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "VideoObject",
        "name": m["title"],
        "description": m["summary"],
        "thumbnailUrl": f"{url}thumbnail.png",
        "duration": iso_duration(m["duration_seconds"]),
        "inLanguage": "en",
        "url": url,
        "isPartOf": {"@type": "CreativeWorkSeries", "name": m["series"], "url": hub},
        "isBasedOn": [{"@type": "CreativeWork", "name": s["title"], "url": link}
                      for s in sources if (link := _https(s.get("url")))],
        "transcript": " ".join(line.get("text", "") for scene in script.get("scenes", [])
                               for line in scene.get("lines", [])),
    }
    if upload:
        ld["uploadDate"], ld["embedUrl"] = upload

    body = (
        f"<nav>{' &middot; '.join(nav)}</nav>"
        f"<h1>{_e(m['title'])}</h1>"
        f"<p class=\"meta\">{_e(m['series'])} &middot; {_e(clock(m['duration_seconds']))}</p>"
        f"<p>{_e(m['summary'])}</p>{player}"
        f"<p class=\"disclosure\">{_e(DISCLOSURE)}</p>"
        f"<h2>Transcript</h2>{''.join(transcript)}"
        f"<h2>Sources and credits</h2><ol>{''.join(source_items)}</ol>"
        + (f"<h2>More in this series</h2><ul>{related}</ul>" if related else "")
        + '<p><a href="captions.srt">Captions (SRT)</a></p>'
    )
    head = (f'<link rel="canonical" href="{_e(url)}">'
            f'<script type="application/ld+json">{json_ld(ld)}</script>')
    return _shell(m["title"], body, head, m["summary"])


def _hub_page(series: str, series_slug: str, items: list[dict[str, Any]], base: str) -> str:
    rows = "".join(f'<li><a href="{_e(i["id"])}/">{_e(i["title"])}</a> '
                   f'<span class="meta">({_e(clock(i["duration_seconds"]))})</span></li>'
                   for i in items)
    ld = {"@context": "https://schema.org", "@type": "CollectionPage", "name": series,
          "url": f"{base}/{series_slug}/",
          "hasPart": [{"@type": "VideoObject", "name": i["title"],
                       "url": f"{base}/{series_slug}/{i['id']}/"} for i in items]}
    body = (f'<nav><a href="../">All series</a> &middot; <a href="../sponsor/">Sponsor</a></nav>'
            f'<h1>{_e(series)}</h1><ul>{rows}</ul>'
            f'<p class="disclosure">{_e(DISCLOSURE)}</p>')
    return _shell(series, body,
                  f'<script type="application/ld+json">{json_ld(ld)}</script>')


def _index_page(groups: dict[str, tuple[str, list[dict[str, Any]]]], base: str) -> str:
    rows = "".join(f'<li><a href="{_e(slug)}/">{_e(series)}</a> '
                   f'<span class="meta">({len(items)} video{"s" if len(items) != 1 else ""})'
                   "</span></li>" for slug, (series, items) in groups.items())
    ld = {"@context": "https://schema.org", "@type": "CollectionPage", "name": "Videos",
          "url": f"{base}/"}
    body = (f'<h1>Videos</h1><ul>{rows}</ul><p><a href="sponsor/">Sponsor these videos</a></p>'
            f'<p class="disclosure">{_e(DISCLOSURE)}</p>')
    return _shell("Videos", body, f'<script type="application/ld+json">{json_ld(ld)}</script>')


def build_site(video_dir: Path, *, base_url: str = DEFAULT_BASE_URL,
               today: date | None = None) -> Site:
    """Write the staging site for every staged, verified, non-example package, and the sponsor
    media-kit page (see sponsor.py)."""
    from . import sponsor
    base = _base(base_url)
    video_dir = Path(video_dir)
    out = video_dir / SITE
    site = Site(out)
    queue = queue_dir(video_dir)
    ids = sorted(p.name for p in queue.iterdir() if p.is_dir()) if queue.is_dir() else []

    staged: list[tuple[dict[str, Any], dict[str, Any], Path]] = []
    for video_id in ids:
        if not ID_RE.match(video_id):
            site.skipped[video_id] = ["the folder name is not a video id"]
            continue
        try:
            package = load_package(video_dir, video_id)
            script = json.loads((package.dir / "script.json").read_text(encoding="utf-8"))
        except (PackageError, OSError, ValueError) as error:
            site.skipped[video_id] = [str(error)]
            continue
        problems = verify_package(package, allow_uploaded=True)
        if problems:
            site.skipped[video_id] = problems
            continue
        staged.append((dict(package.manifest), script, package.dir))

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    groups: dict[str, tuple[str, list[dict[str, Any]]]] = {}
    for manifest, _script, _dir in sorted(staged, key=lambda t: (t[0]["created_at"], t[0]["id"])):
        slug = slugify(manifest["series"])
        groups.setdefault(slug, (manifest["series"], []))[1].append(manifest)

    for slug, (series, items) in groups.items():
        hub = out / slug
        hub.mkdir()
        (hub / "index.html").write_text(_hub_page(series, slug, items, base), encoding="utf-8")
        site.pages.append(hub / "index.html")
    for manifest, script, source_dir in staged:
        slug = slugify(manifest["series"])
        items = groups[slug][1]
        at = next(i for i, m in enumerate(items) if m["id"] == manifest["id"])
        folder = out / slug / manifest["id"]
        folder.mkdir()
        page = _video_page(manifest, script, base, slug, items[at - 1] if at else None,
                           items[at + 1] if at + 1 < len(items) else None, items)
        (folder / "index.html").write_text(page, encoding="utf-8")
        for name in ("thumbnail.png", "captions.srt"):
            shutil.copyfile(source_dir / name, folder / name)
        site.pages.append(folder / "index.html")
    (out / "index.html").write_text(_index_page(groups, base), encoding="utf-8")
    site.pages.append(out / "index.html")
    published = sum(1 for manifest, _s, _d in staged if _upload(manifest))
    config = sponsor.load_config(video_dir)
    analytics, _why = sponsor.load_analytics(video_dir, today=today or date.today(),
                                             published=published)
    (out / "sponsor").mkdir()
    (out / "sponsor" / "index.html").write_text(
        sponsor.sponsor_page(config, analytics, groups, base), encoding="utf-8")
    site.pages.append(out / "sponsor" / "index.html")
    return site
