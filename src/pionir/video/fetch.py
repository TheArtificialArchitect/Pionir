"""Fetching passages and images from the allowlisted public sources, politely, with their basis.

Two layers.

``PoliteFetcher`` is the only way this module reaches a host. For every GET it:

* refuses any URL that is not https on one of the niche source's own hosts (the same rule the
  pack loader applies), and re-checks every redirect hop against that allowlist, because the
  stock client follows redirects silently to anywhere,
* identifies itself (a real User-Agent), obeys the host's robots.txt (a missing robots.txt is
  allowed, an unreadable one is NOT: a 5xx or no answer means do not fetch), and its
  Crawl-delay,
* spaces its requests to a host (default 2 s) and backs off on 429/503, honouring Retry-After,
* refuses a body that was cut off at the size cap instead of using half a document,
* caches what it fetched on disk (``<video dir>/cache``), so asking twice costs nothing and a
  re-run of a script never re-hits the server.

The per-source readers turn one item into a ``FetchedItem`` carrying its source id, URL, the
licence / public-domain basis and the attribution line. A reader needs a POSITIVE signal for
the basis (the item says public domain, a CC licence we accept, a date old enough); no signal
and the item is refused, never guessed. ``write_pack`` merges items into a pack file that
passage loading accepts, so Ian reads the pack before any script is written from it.

Nothing here runs in a test against a real host: tests hand in a fake ``Http``.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from ..crew.net import Http, HttpResponse, HttpUnreachable
from .niche import Niche, Source
from .passages import MAX_TEXT, host_allowed

USER_AGENT = "pionir-video/1 (+https://dokazindustries.com; research for original educational video)"
MIN_INTERVAL = 2.0
MAX_CRAWL_DELAY = 30.0
MAX_BODY = 6_000_000
MAX_IMAGE = 8_000_000
MAX_REDIRECTS = 3
CACHE_TTL = 7 * 24 * 3600
ROBOTS_TTL = 24 * 3600
RETRIES = 2
MAX_BACKOFF = 60.0
PATENT_MIN_AGE_YEARS = 25
CHRONICLING_LAST_YEAR = 1928
ACCEPTED_IMAGE = ("image/jpeg", "image/png", "image/webp", "image/gif", "image/tiff")


# The machine APIs these hosts publish for programs (their robots.txt is written for crawlers
# and, on Wikimedia, would block the API itself). Only these exact endpoints skip robots.txt;
# the identifying User-Agent, the spacing and the backoff still apply, and every page, OCR text
# and image download obeys robots.txt.
_API = (("commons.wikimedia.org", "/w/api.php"), ("archive.org", "/metadata/"),
        ("www.loc.gov", "/item/"), ("digitalcollections.lib.washington.edu", "/digital/api/"))


def documented_api(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host == "www.loc.gov" and "fo=json" not in parts.query:
        return False
    return any(host == h and parts.path.startswith(prefix) for h, prefix in _API)


class FetchError(RuntimeError):
    """An item that was not fetched or may not be used, with the reason."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class NoRedirectHttp:
    """The real client: GET only, never follows a redirect (the fetcher decides per hop),
    and raises when the body is larger than the cap instead of silently cutting it."""

    def __init__(self, max_bytes: int = MAX_BODY) -> None:
        self.max_bytes = max_bytes
        self._opener = urllib.request.build_opener(_NoRedirect)

    def get(self, url: str, *, headers: dict | None = None, timeout: float = 20.0) -> HttpResponse:
        req = urllib.request.Request(url, method="GET", headers=headers or {})
        t0 = time.monotonic()
        try:
            try:
                with self._opener.open(req, timeout=timeout) as r:
                    status, head, body = r.status, dict(r.headers.items()), r.read(self.max_bytes + 1)
            except urllib.error.HTTPError as exc:
                status, head = exc.code, dict(exc.headers.items()) if exc.headers else {}
                body = exc.read(self.max_bytes + 1)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise HttpUnreachable(f"{type(exc).__name__}: {exc}") from exc
        if len(body) > self.max_bytes:
            raise HttpUnreachable(f"the body is larger than {self.max_bytes} bytes")
        return HttpResponse(status, body, (time.monotonic() - t0) * 1000, head)


def _header(response: HttpResponse, name: str) -> str:
    for key, value in response.headers.items():
        if key.lower() == name.lower():
            return str(value)
    return ""


class PoliteFetcher:
    def __init__(self, http: Http, cache_dir: Path | str, *, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic,
                 min_interval: float = MIN_INTERVAL, user_agent: str = USER_AGENT) -> None:
        self.http = http
        self.cache = Path(cache_dir)
        self._clock, self._sleep, self._mono = clock, sleep, monotonic
        self.min_interval = min_interval
        self.user_agent = user_agent
        self._last: dict[str, float] = {}
        self._robots: dict[str, tuple[float, RobotFileParser | None, float]] = {}
        self.requests = 0                    # network requests made (not cache hits)

    # ---- cache ----
    def _paths(self, url: str) -> tuple[Path, Path]:
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:40]
        return self.cache / f"{key}.json", self.cache / f"{key}.body"

    def _cached(self, url: str, ttl: float, limit: int) -> bytes | None:
        meta_path, body_path = self._paths(url)
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            body = body_path.read_bytes()
        except (OSError, ValueError):
            return None
        if (meta.get("url") != url or self._clock() - float(meta.get("at", 0)) > ttl
                or hashlib.sha256(body).hexdigest() != meta.get("sha256") or len(body) > limit):
            return None
        return body

    def _store(self, url: str, body: bytes) -> None:
        meta_path, body_path = self._paths(url)
        self.cache.mkdir(parents=True, exist_ok=True)
        body_path.write_bytes(body)
        meta_path.write_text(json.dumps({"url": url, "at": self._clock(),
                                         "sha256": hashlib.sha256(body).hexdigest()}),
                             encoding="utf-8")

    # ---- politeness ----
    def _wait(self, host: str, delay: float) -> None:
        last = self._last.get(host)
        if last is not None:
            gap = delay - (self._mono() - last)
            if gap > 0:
                self._sleep(gap)
        self._last[host] = self._mono()

    def _raw(self, url: str, hosts: tuple[str, ...], delay: float, limit: int) -> HttpResponse:
        """One politely-spaced GET, following redirects only within ``hosts``."""
        for hop in range(MAX_REDIRECTS + 1):
            if not host_allowed(url, hosts):
                raise FetchError(f"{url} is not an https URL on this source's hosts {list(hosts)}")
            host = urlsplit(url).hostname or ""
            response = None
            for attempt in range(RETRIES + 1):
                self._wait(host, delay)
                self.requests += 1
                try:
                    response = self.http.get(url, headers={"User-Agent": self.user_agent,
                                                           "Accept": "*/*"}, timeout=25.0)
                except HttpUnreachable as exc:
                    raise FetchError(f"{url} did not answer: {exc}") from exc
                if response.status in (429, 503) and attempt < RETRIES:
                    wait = _header(response, "Retry-After")
                    pause = float(wait) if wait.isdigit() else 5.0 * 2 ** attempt
                    self._sleep(min(pause, MAX_BACKOFF))
                    continue
                break
            if len(response.body) > limit:
                raise FetchError(f"{url}: the body is larger than {limit} bytes; refusing to "
                                 "use a cut-off document")
            if response.status in (301, 302, 303, 307, 308):
                target = _header(response, "Location")
                if not target:
                    raise FetchError(f"{url}: a redirect with no Location")
                url = urljoin(url, target)
                continue
            return response
        raise FetchError(f"{url}: more than {MAX_REDIRECTS} redirects")

    def _robots_for(self, url: str, hosts: tuple[str, ...]) -> tuple[RobotFileParser | None, float]:
        host = urlsplit(url).hostname or ""
        held = self._robots.get(host)
        if held and self._clock() - held[0] < ROBOTS_TTL:
            return held[1], held[2]
        robots_url = f"https://{host}/robots.txt"
        body = self._cached(robots_url, ROBOTS_TTL, MAX_BODY)
        status = 200
        if body is None:
            try:
                response = self._raw(robots_url, hosts, self.min_interval, MAX_BODY)
            except FetchError:
                self._robots[host] = (self._clock(), None, self.min_interval)
                return None, self.min_interval
            status, body = response.status, response.body
            if status == 200:
                self._store(robots_url, body)
        if status >= 500 or status in (401, 403, 429):
            parser = None          # unreadable robots.txt: the safe reading is "do not fetch"
        elif status == 200:
            parser = RobotFileParser()
            parser.parse(body.decode("utf-8", "replace").splitlines())
        else:
            parser = RobotFileParser()
            parser.parse([])       # 404 and the like: no rules, everything allowed
        delay = self.min_interval
        if parser is not None:
            declared = parser.crawl_delay(self.user_agent) or parser.crawl_delay("*")
            if declared:
                delay = max(delay, min(float(declared), MAX_CRAWL_DELAY))
        self._robots[host] = (self._clock(), parser, delay)
        return parser, delay

    def get(self, url: str, source: Source, *, limit: int = MAX_BODY) -> bytes:
        """The body of ``url`` (200 only), from cache when fresh, else fetched politely."""
        if not host_allowed(url, source.hosts):
            raise FetchError(f"{url} is not an https URL on {source.name}'s hosts "
                             f"{list(source.hosts)}")
        cached = self._cached(url, CACHE_TTL, limit)
        if cached is not None:
            return cached
        if documented_api(url):
            delay = self.min_interval      # an endpoint the host publishes for programs
        else:
            parser, delay = self._robots_for(url, source.hosts)
            if parser is None or not parser.can_fetch(self.user_agent, url):
                raise FetchError(f"{url}: robots.txt on {urlsplit(url).hostname} does not allow "
                                 "it (or could not be read)")
        response = self._raw(url, source.hosts, delay, limit)
        if response.status != 200:
            raise FetchError(f"{url}: the server answered {response.status}")
        self._store(url, response.body)
        return response.body


# ---- items ----

@dataclass(frozen=True, slots=True)
class FetchedItem:
    source_id: str
    url: str
    title: str
    text: str
    credit: str
    license_basis: str
    fetched_at: str
    image: bytes | None = None
    image_name: str = ""

    def passage_record(self, pid: str) -> dict[str, str]:
        return {"id": pid, "source_id": self.source_id, "url": self.url, "title": self.title,
                "text": self.text, "credit": self.credit, "license_basis": self.license_basis,
                "fetched_at": self.fetched_at}


_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


def clean_text(raw: Any) -> str:
    if isinstance(raw, (list, tuple)):
        raw = " ".join(str(r) for r in raw)
    text = html.unescape(_TAG.sub(" ", str(raw or "")))
    return _SPACE.sub(" ", text).strip()


def _json(body: bytes, what: str) -> Any:
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except ValueError as error:
        raise FetchError(f"{what}: not JSON ({error})") from error


def _today(clock: Callable[[], float]) -> str:
    return date.fromtimestamp(clock()).isoformat()


_PD_WORDS = re.compile(r"(?i)\b(public domain|no known (?:copyright )?restrictions?|"
                       r"no known copyright|cc0|pdm)\b")
_NOT_PD = re.compile(r"(?i)\b(not in the public domain|all rights reserved|copyright(?:ed)? by|"
                     r"permission required|used with permission)\b")


def _pd_signal(text: str) -> str | None:
    if _NOT_PD.search(text):
        return None
    hit = _PD_WORDS.search(text)
    return hit.group(0) if hit else None


def _need_text(item: str, text: str) -> str:
    text = clean_text(text)[:MAX_TEXT]
    if len(text) < 40:
        raise FetchError(f"{item}: there is no usable text to stand a script on")
    return text


def _source(niche: Niche, source_id: str) -> Source:
    source = niche.source(source_id)
    if source is None:
        raise FetchError(f"source {source_id!r} is not allowlisted for niche {niche.id!r}")
    return source


def fetch_internet_archive(fetcher: PoliteFetcher, niche: Niche, identifier: str,
                           clock: Callable[[], float] = time.time) -> FetchedItem:
    source = _source(niche, "internet_archive")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}", identifier):
        raise FetchError(f"{identifier!r} is not an Internet Archive identifier")
    doc = _json(fetcher.get(f"https://archive.org/metadata/{identifier}", source), identifier)
    meta = doc.get("metadata") if isinstance(doc, dict) else None
    if not isinstance(meta, dict) or not meta.get("title"):
        raise FetchError(f"{identifier}: no metadata (item missing or dark)")
    rights = " ".join(clean_text(meta.get(k)) for k in ("licenseurl", "rights", "possible-copyright-status"))
    basis = _pd_signal(rights) or ("public domain mark"
                                   if "creativecommons.org/publicdomain" in rights else None)
    if basis is None:
        raise FetchError(f"{identifier}: the item does not state a public-domain basis "
                         f"(licenseurl/rights: {rights[:80]!r}); refusing")
    title = clean_text(meta["title"])[:200]
    who = clean_text(meta.get("creator")) or "unknown creator"
    return FetchedItem(source.id, f"https://archive.org/details/{identifier}", title,
                       _need_text(identifier, meta.get("description") or ""),
                       f"{title}, {who}; Internet Archive item {identifier} ({basis})", basis,
                       _today(clock))


_COMMONS_OK = re.compile(r"(?i)^(public domain|pd[- ]|cc0|cc[- ]by(?:[- ]sa)?\b)")
_COMMONS_NO = re.compile(r"(?i)\b(nc|nd|fair use|non-?commercial|no derivatives)\b|\bgfdl\b")


def fetch_commons(fetcher: PoliteFetcher, niche: Niche, file_title: str,
                  clock: Callable[[], float] = time.time) -> FetchedItem:
    source = _source(niche, "commons")
    name = file_title if file_title.startswith("File:") else "File:" + file_title
    if not re.fullmatch(r"File:[^\n\r|<>{}\[\]]{1,200}", name):
        raise FetchError(f"{file_title!r} is not a Commons file title")
    api = ("https://commons.wikimedia.org/w/api.php?action=query&format=json&prop=imageinfo"
           "&iiprop=url|mime|extmetadata&titles=" + quote(name))
    doc = _json(fetcher.get(api, source), name)
    pages = ((doc.get("query") or {}).get("pages") or {}) if isinstance(doc, dict) else {}
    page = next(iter(pages.values()), None)
    info = ((page or {}).get("imageinfo") or [None])[0]
    if not info:
        raise FetchError(f"{name}: no such file on Commons")
    ext = info.get("extmetadata") or {}

    def field(key: str) -> str:
        return clean_text((ext.get(key) or {}).get("value", ""))

    licence = field("LicenseShortName") or field("UsageTerms")
    if not licence or _COMMONS_NO.search(licence) or not _COMMONS_OK.match(licence):
        raise FetchError(f"{name}: licence {licence!r} is not public domain, CC0, CC BY or "
                         "CC BY-SA; refusing")
    if info.get("mime") not in ACCEPTED_IMAGE:
        raise FetchError(f"{name}: type {info.get('mime')!r} is not an image we draw")
    artist = field("Artist") or field("Credit") or "unknown author"
    needs = field("AttributionRequired").lower() == "true" or licence.upper().startswith("CC BY")
    credit = f"{name[5:]}, {artist}, {licence}, via Wikimedia Commons" + (
        " (attribution required)" if needs else "")
    file_url = str(info.get("url") or "")
    if not host_allowed(file_url, source.hosts):
        raise FetchError(f"{name}: the file URL {file_url!r} is not on the Commons hosts")
    image = fetcher.get(file_url, source, limit=MAX_IMAGE)
    description = field("ImageDescription") or name[5:]
    return FetchedItem(source.id, str(info.get("descriptionurl") or
                                      "https://commons.wikimedia.org/wiki/" + quote(name)),
                       name[5:][:200], _need_text(name, f"{description}. {credit}"), credit,
                       licence, _today(clock), image,
                       re.sub(r"[^A-Za-z0-9._-]+", "_", name[5:])[-80:])


def fetch_loc(fetcher: PoliteFetcher, niche: Niche, item_id: str, source_id: str = "loc",
              clock: Callable[[], float] = time.time) -> FetchedItem:
    source = _source(niche, source_id)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,60}", item_id):
        raise FetchError(f"{item_id!r} is not a Library of Congress item id")
    url = f"https://www.loc.gov/item/{item_id}/"
    doc = _json(fetcher.get(url + "?fo=json", source), item_id)
    item = doc.get("item") if isinstance(doc, dict) else None
    if not isinstance(item, dict) or not item.get("title"):
        raise FetchError(f"{item_id}: no item record")
    rights = " ".join(clean_text(item.get(k)) for k in ("rights_advisory", "rights", "rights_information"))
    basis = _pd_signal(rights)
    if basis is None:
        raise FetchError(f"{item_id}: the record states no public-domain basis "
                         f"(rights: {rights[:80]!r}); refusing")
    title = clean_text(item["title"])[:200]
    body = " ".join(clean_text(item.get(k)) for k in ("description", "summary", "notes", "contents"))
    credit = f"{title}; Library of Congress, item {item_id} ({basis})"
    return FetchedItem(source.id, url, title, _need_text(item_id, body or ""), credit, basis,
                       _today(clock))


def fetch_chronicling(fetcher: PoliteFetcher, niche: Niche, page_ref: str,
                      clock: Callable[[], float] = time.time) -> FetchedItem:
    """``page_ref`` is ``<lccn>/<yyyy-mm-dd>/ed-<n>/seq-<n>``; the OCR text of that page."""
    source = _source(niche, "chronicling_america")
    m = re.fullmatch(r"([a-z]{1,3}\d{8,10})/((\d{4})-\d{2}-\d{2})/(ed-\d{1,2})/(seq-\d{1,3})", page_ref)
    if not m:
        raise FetchError(f"{page_ref!r} is not lccn/yyyy-mm-dd/ed-N/seq-N")
    year = int(m.group(3))
    if year > CHRONICLING_LAST_YEAR:
        raise FetchError(f"{page_ref}: published {year}; only pages up to {CHRONICLING_LAST_YEAR} "
                         "are used (clear public domain)")
    base = f"https://chroniclingamerica.loc.gov/lccn/{page_ref}/"
    ocr = fetcher.get(base + "ocr.txt", source).decode("utf-8", "replace")
    text = _need_text(page_ref, ocr)
    return FetchedItem(source.id, base, f"Chronicling America page {m.group(1)} {m.group(2)}",
                       "OCR text, may contain errors: " + text[: MAX_TEXT - 40],
                       f"Chronicling America (Library of Congress), {m.group(1)}, {m.group(2)}, "
                       f"{m.group(5)} (public domain, published {year})",
                       f"public domain, published {year}", _today(clock))


def fetch_uw(fetcher: PoliteFetcher, niche: Niche, ref: str,
             clock: Callable[[], float] = time.time) -> FetchedItem:
    """``ref`` is ``<collection alias>/<item id>`` in the UW CONTENTdm instance."""
    source = _source(niche, "uw_collections")
    m = re.fullmatch(r"([A-Za-z0-9_-]{1,40})/(\d{1,9})", ref)
    if not m:
        raise FetchError(f"{ref!r} is not collection/item-id")
    api = ("https://digitalcollections.lib.washington.edu/digital/api/collections/"
           f"{m.group(1)}/items/{m.group(2)}/false")
    doc = _json(fetcher.get(api, source), ref)
    fields = doc.get("fields") if isinstance(doc, dict) else None
    if not isinstance(fields, list):
        raise FetchError(f"{ref}: no item record")
    flat = {str(f.get("key") or f.get("label") or "").lower(): clean_text(f.get("value"))
            for f in fields if isinstance(f, dict)}
    rights = " ".join(v for k, v in flat.items() if "right" in k)
    basis = _pd_signal(rights)
    if basis is None:
        raise FetchError(f"{ref}: the record states no public-domain basis "
                         f"(rights: {rights[:80]!r}); refusing")
    title = flat.get("title") or flat.get("titla") or clean_text(doc.get("title"))
    if not title:
        raise FetchError(f"{ref}: no title")
    body = " ".join(flat.get(k, "") for k in ("descri", "description", "date", "creato", "creator"))
    url = f"https://digitalcollections.lib.washington.edu/digital/collection/{m.group(1)}/id/{m.group(2)}"
    return FetchedItem(source.id, url, title[:200], _need_text(ref, body),
                       f"{title[:120]}; University of Washington Libraries Digital Collections "
                       f"({basis})", basis, _today(clock))


class _Meta(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.meta: dict[str, list[str]] = {}

    def handle_starttag(self, tag, attrs):
        if tag == "meta":
            a = dict(attrs)
            if a.get("name") and a.get("content"):
                self.meta.setdefault(a["name"], []).append(a["content"])


def fetch_patent(fetcher: PoliteFetcher, niche: Niche, number: str,
                 clock: Callable[[], float] = time.time) -> FetchedItem:
    source = _source(niche, "patents")
    if not re.fullmatch(r"US\d{4,8}[A-Z]?\d?", number):
        raise FetchError(f"{number!r} is not a US patent number like US123456A")
    url = f"https://patents.google.com/patent/{number}/en"
    parser = _Meta()
    parser.feed(fetcher.get(url, source).decode("utf-8", "replace"))
    meta = parser.meta
    when = (meta.get("DC.date") or [""])[0]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", when):
        raise FetchError(f"{number}: no publication date on the page; refusing")
    today = date.fromtimestamp(clock())
    if today.year - int(when[:4]) < PATENT_MIN_AGE_YEARS:
        raise FetchError(f"{number}: dated {when}; only patents at least "
                         f"{PATENT_MIN_AGE_YEARS} years old are used (expired)")
    title = clean_text((meta.get("DC.title") or [""])[0])[:200]
    text = (meta.get("DC.description") or meta.get("description") or [""])[0]
    if not title:
        raise FetchError(f"{number}: no title on the page")
    inventors = ", ".join(meta.get("DC.contributor") or []) or "inventor not stated"
    basis = f"expired US patent, dated {when}"
    return FetchedItem(source.id, url, title, _need_text(number, text),
                       f"{title}, {inventors}; {number}, Google Patents ({basis})", basis,
                       today.isoformat())


def fetch_item(fetcher: PoliteFetcher, niche: Niche, source_id: str, ref: str,
               clock: Callable[[], float] = time.time) -> FetchedItem:
    if source_id == "internet_archive":
        return fetch_internet_archive(fetcher, niche, ref, clock)
    if source_id == "commons":
        return fetch_commons(fetcher, niche, ref, clock)
    if source_id in ("loc", "loc_haer"):
        return fetch_loc(fetcher, niche, ref, source_id, clock)
    if source_id == "chronicling_america":
        return fetch_chronicling(fetcher, niche, ref, clock)
    if source_id == "uw_collections":
        return fetch_uw(fetcher, niche, ref, clock)
    if source_id == "patents":
        return fetch_patent(fetcher, niche, ref, clock)
    raise FetchError(f"there is no reader for source {source_id!r}")


def write_pack(path: Path | str, topic: str, items: list[FetchedItem]) -> dict[str, int]:
    """Merge fetched items into a pack file (created if missing). A Commons image is saved
    beside the pack and recorded as an image with its credit. Returns counts."""
    path = Path(path)
    try:
        document = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError) as error:
        raise FetchError(f"cannot read the pack {path}: {error}") from error
    document.setdefault("topic", topic)
    passages = document.setdefault("passages", [])
    images = document.setdefault("images", [])
    taken = {p["id"] for p in passages} | {i["id"] for i in images}
    added = {"passages": 0, "images": 0}
    for item in items:
        if any(p.get("url") == item.url for p in passages):
            continue
        n = len(passages) + 1
        while f"p{n}" in taken:
            n += 1
        pid = f"p{n}"
        taken.add(pid)
        passages.append(item.passage_record(pid))
        added["passages"] += 1
        if item.image:
            name = f"{pid}-{item.image_name}"
            (path.parent / name).write_bytes(item.image)
            images.append({"id": f"i{n}", "file": name, "source_id": item.source_id,
                           "url": item.url, "credit": item.credit})
            taken.add(f"i{n}")
            added["images"] += 1
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return added
