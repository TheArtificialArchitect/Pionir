"""Adapter for Gumroad: a product goes on sale only after the owner's yes.

The same rule as the blog, Instagram and money: nothing is put on sale without Ian's
approval of that product, and holding a permission is not a way around it. So:

- ``product.gumroad_publish`` is PRIVILEGED with ``requires_approval=True``: PionirApp parks
  it on EVERY call, and the Discord card shows the whole listing (name, version, price, the
  summary, the tags, the FULL description, the zip's full file list, the secrets scan, any
  executables, and the cover itself) before he answers. ``routable=False``: by name only.
- ``product.gumroad_unpublish`` is PRIVILEGED but does NOT require approval: taking a
  product off sale is the safe direction and must be fast.
- ``product.gumroad_list`` is READ_ONLY: the seller's products and their sales.

A product is staged by the owner at ``<products_dir>/<slug>/`` (default
``~/.pionir/products``): a zip and a cover image, each pinned in the payload by SHA-256.
Before anything is parked, the payload is checked (the blog's Markdown and link rules for
the description), the zip passes the same checks as a client delivery (``deliveries.py``:
a README, no path tricks, no zip bomb, and a scan of every entry for the owner's real
secret values and common key formats) with a product's larger size limit, and the cover
must be a PNG or JPEG of at least 1280x720. Executables are refused unless the payload
says ``allow_executables`` - and then the card says so at the top and lists them.

Publishing, once approved (everything is checked again from the bytes on disk first):

1. find the product whose ``custom_permalink`` is the slug (``GET /v2/products``, every
   page). If it exists and is on sale, take it off sale first (``PUT .../disable``) so a
   failure part-way never leaves a live listing half updated; then update it
   (``PUT /v2/products/:id``). Otherwise create it as a DRAFT (``POST /v2/products`` with
   ``draft=true``).
2. upload the zip (``POST /v2/files/presign``, ``PUT`` each part to its presigned URL -
   never with the token - then ``POST /v2/files/complete``) and attach it
   (``PUT /v2/products/:id`` with ``files``: the zip replaces whatever was there);
3. upload the cover (``POST /v2/direct_uploads``, ``PUT`` the bytes to the storage URL it
   names) and attach it (``POST /v2/products/:id/covers`` with the ``signed_blob_id``);
4. only then publish it (``PUT /v2/products/:id/enable``).

If any step after the product is touched fails, it is left as an unpublished draft and the
answer names the step and says nothing was put on sale - never a half-published product.

The token is read from a file on every call (so adding it needs no restart), sent only in
the ``Authorization: Bearer`` header to Gumroad's API, and never appears in a log, an error
or a result. Gumroad's quirk - ``success: false`` with HTTP 200 - is a refusal like a 4xx;
401 means the token was rejected; 5xx or no answer means Gumroad is unavailable.
"""

from __future__ import annotations

import base64
import hashlib
import html
import io
import json
import logging
import math
import re
import secrets
import stat
import struct
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pionir.adapters.content import (
    _contact_problem,
    _link_problem,
    _scrooge_problem,
    _text_problem,
    read_token,
)
from pionir.adapters.deliveries import (
    DeliveryProblem,
    Inspection,
    inspect_zip,
    load_secrets,
    scan_bytes,
)
from pionir.contracts import AgentManifest, Capability, RiskLevel, Task, TaskResult
from pionir.errors import AdapterProtocolError, AdapterUnavailable

_log = logging.getLogger(__name__)

PUBLISH = "product.gumroad_publish"
UNPUBLISH = "product.gumroad_unpublish"
LIST = "product.gumroad_list"

DEFAULT_GUMROAD_URL = "https://api.gumroad.com/v2"
SETUP_HINT = r"run tools\setup-gumroad.ps1"
TOKEN_REJECTED = f"the Gumroad token was rejected - {SETUP_HINT}"
NOTHING_ON_SALE = "nothing was put on sale"
NOTHING_PUBLISHED = "nothing was published"

MAX_PRODUCT_ZIP_BYTES = 250_000_000
MAX_PRODUCT_UNCOMPRESSED = 1_000_000_000
MAX_COVER_BYTES = 5_000_000
MIN_COVER_SIZE = (1280, 720)
MAX_RESPONSE_BYTES = 4_000_000
MAX_PAGES = 100
# Gumroad's multipart part size (files_controller PART_SIZE = 100.megabytes).
PART_SIZE = 100 * 1024 * 1024

PUBLISH_FIELDS = ("slug", "name", "version", "price_cents", "pay_what_you_want", "summary",
                  "description_md", "tags", "zip_name", "zip_sha256", "cover_name",
                  "cover_sha256", "allow_executables")

# ---- the payload rules ------------------------------------------------------------------
_SLUG = re.compile(r"[a-z0-9-]{3,40}")
_VERSION = re.compile(r"\d{1,6}\.\d{1,6}\.\d{1,6}")
_TAG = re.compile(r"[a-z0-9-]{2,24}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_ZIP_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}\.(?i:zip)")
_COVER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}\.(?i:png|jpe?g)")
LENGTHS = {"name": (5, 80), "summary": (20, 200), "description_md": (100, 8000)}
PRICE_CENTS = (100, 100_000)
MAX_TAGS = 5
_HEADING_LINE = re.compile(r"^\s{0,3}(#{1,6})(?:\s|$)")
_FENCE_LINE = re.compile(r"^\s{0,3}```")


def check_slug(value: Any) -> str:
    if not isinstance(value, str) or not _SLUG.fullmatch(value):
        raise ValueError("slug: 3-40 of a-z, 0-9 and '-' (it is also the Gumroad permalink)")
    problem = _contact_problem(value)
    if problem:
        raise ValueError(f"slug: {problem}")
    return value


def _file_name(key: str, value: Any, pattern: re.Pattern[str], what: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key}: required, the {what}'s file name")
    if "/" in value or "\\" in value or ".." in value or ":" in value:
        raise ValueError(f"{key}: a bare file name in the product's folder (no path "
                         "separators, no '..')")
    if not pattern.fullmatch(value):
        ends = ".zip" if what == "zip" else ".png, .jpg or .jpeg"
        raise ValueError(f"{key}: letters, digits, '.', '_' and '-' only, ending in {ends} "
                         "(at most 100 characters)")
    return value


def _description_problem(text: str) -> str | None:
    """The blog's Markdown subset: only ## and ### headings (outside code fences)."""
    fenced = False
    for line in text.splitlines():
        if _FENCE_LINE.match(line):
            fenced = not fenced
            continue
        heading = _HEADING_LINE.match(line)
        if not fenced and heading and len(heading.group(1)) not in (2, 3):
            return "only ## and ### headings are allowed (the listing has its own title)"
    return None


def check_product(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The product exactly as it will be listed, or ValueError("<field>: <why>")."""
    unknown = sorted(set(payload) - set(PUBLISH_FIELDS))
    if unknown:
        raise ValueError(f"{unknown[0]}: not a product field (allowed: "
                         f"{', '.join(PUBLISH_FIELDS)})")
    missing = [key for key in PUBLISH_FIELDS if key not in payload]
    if missing:
        raise ValueError(f"{missing[0]}: required")
    product: dict[str, Any] = {"slug": check_slug(payload["slug"])}
    version = payload["version"]
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise ValueError("version: MAJOR.MINOR.PATCH, digits only (like 1.2.0)")
    for key, (low, high) in LENGTHS.items():
        text = payload[key]
        if not isinstance(text, str):
            # ValueError like every other rule: one "<field>: <why>" refusal type
            raise ValueError(f"{key}: required, a string")  # noqa: TRY004
        if not low <= len(text) <= high:
            raise ValueError(f"{key}: {low}-{high} characters (this is {len(text)})")
        if not text.strip():
            raise ValueError(f"{key}: cannot be blank")
        rule = "body_md" if key == "description_md" else "title"
        problem = (_text_problem(text) or _scrooge_problem(rule, text)
                   or (_description_problem(text) if key == "description_md" else None))
        if problem:
            raise ValueError(f"{key}: {problem}")
        product[key] = text
    product["version"] = version
    price = payload["price_cents"]
    if not isinstance(price, int) or isinstance(price, bool) \
            or not PRICE_CENTS[0] <= price <= PRICE_CENTS[1]:
        raise ValueError(f"price_cents: a whole number of cents, {PRICE_CENTS[0]}-"
                         f"{PRICE_CENTS[1]} (${PRICE_CENTS[0] / 100:.2f}-"
                         f"${PRICE_CENTS[1] / 100:,.2f})")
    product["price_cents"] = price
    for key in ("pay_what_you_want", "allow_executables"):
        if not isinstance(payload[key], bool):
            raise ValueError(f"{key}: true or false")  # noqa: TRY004 - one refusal type
        product[key] = payload[key]
    tags = payload["tags"]
    if not isinstance(tags, list) or len(tags) > MAX_TAGS:
        raise ValueError(f"tags: a list of at most {MAX_TAGS}")
    for tag in tags:
        if not isinstance(tag, str) or not _TAG.fullmatch(tag):
            raise ValueError("tags: each 2-24 of a-z, 0-9 and '-'")
        problem = _contact_problem(tag)
        if problem:
            raise ValueError(f"tags: {problem}")
    if len(set(tags)) != len(tags):
        raise ValueError("tags: no repeats")
    product["tags"] = list(tags)
    product["zip_name"] = _file_name("zip_name", payload["zip_name"], _ZIP_NAME, "zip")
    product["cover_name"] = _file_name("cover_name", payload["cover_name"], _COVER_NAME,
                                       "cover")
    for key in ("zip_sha256", "cover_sha256"):
        value = payload[key]
        if not isinstance(value, str) or not _SHA256.fullmatch(value):
            raise ValueError(f"{key}: the file's SHA-256, 64 lowercase hex characters")
        product[key] = value
    return product


def check_unpublish(payload: Mapping[str, Any]) -> str:
    extra = sorted(set(payload) - {"slug"})
    if extra:
        raise ValueError(f"{extra[0]}: not an unpublish field (only slug)")
    return check_slug(payload.get("slug"))


def price_text(cents: Any, pay_what_you_want: Any = False) -> str:
    """How the price is shown to the owner: "$19.00", or the minimum of a
    pay-what-you-want price."""
    if not isinstance(cents, int) or isinstance(cents, bool):
        return "(no valid price)"
    amount = f"${cents / 100:,.2f}"
    return f"pay what you want, minimum {amount}" if pay_what_you_want is True else amount


# ---- the description: a small, escaping Markdown renderer -------------------------------
_HEADING = re.compile(r"^\s{0,3}(#{2,3})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^\s{0,3}[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^\s{0,3}\d{1,9}[.)]\s+(.*)$")
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\(\s*<?([^\s()<>]+)>?\s*\)")
_STASHED = re.compile(r"\x00(\d+)\x00")
_EMPHASIS = (
    (re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*"), "strong"),
    (re.compile(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)"), "strong"),
    (re.compile(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])"), "em"),
    (re.compile(r"(?<![\w_])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w_])"), "em"),
)


def _escape(text: str) -> str:
    return html.escape(text, quote=True)


def _emphasis(escaped: str) -> str:
    for pattern, tag in _EMPHASIS:
        escaped = pattern.sub(lambda m, t=tag: f"<{t}>{m.group(1)}</{t}>", escaped)
    return escaped


def _inline(text: str) -> str:
    """One line of Markdown as safe HTML: everything is escaped; the only markup made is
    <code>, <strong>, <em> and <a> to an allowed https host (any other link stays text)."""
    stash: list[str] = []

    def keep(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    text = _CODE_SPAN.sub(lambda m: keep(f"<code>{_escape(m.group(1))}</code>"), text)

    def link(match: re.Match[str]) -> str:
        label, url = match.group(1), match.group(2)
        if _link_problem(url) is not None:
            return keep(_escape(match.group(0)))
        return keep(f'<a href="{_escape(url)}" rel="noopener">'
                    f"{_emphasis(_escape(label))}</a>")

    text = _emphasis(_escape(_LINK.sub(link, text)))
    for _ in range(len(stash) + 1):          # a stashed link may hold a stashed code span
        if not _STASHED.search(text):
            break
        text = _STASHED.sub(lambda m: stash[int(m.group(1))], text)
    return text


def render_description(markdown: str, version: str | None = None) -> str:
    """The listing's description as HTML, from the blog's Markdown subset: ## / ###
    headings, paragraphs, bullet and numbered lists, bold, italic, code, fenced code and
    links to the Dokaz hosts. Nothing from the source reaches the HTML unescaped."""
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    paragraph: list[str] = []
    items: list[str] = []
    kind: str | None = None

    def flush_paragraph() -> None:
        if paragraph:
            out.append(f"<p>{_inline(' '.join(p.strip() for p in paragraph))}</p>")
            paragraph.clear()

    def flush_list() -> None:
        nonlocal kind
        if items:
            out.append(f"<{kind}>" + "".join(f"<li>{_inline(i)}</li>" for i in items)
                       + f"</{kind}>")
            items.clear()
        kind = None

    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if _FENCE_LINE.match(line):
            flush_paragraph()
            flush_list()
            code: list[str] = []
            while index < len(lines) and not _FENCE_LINE.match(lines[index]):
                code.append(lines[index])
                index += 1
            index += 1                                  # the closing fence
            out.append(f"<pre><code>{_escape(chr(10).join(code))}</code></pre>")
            continue
        if not line.strip():
            flush_paragraph()
            flush_list()
            continue
        heading = _HEADING.match(line)
        if heading:
            flush_paragraph()
            flush_list()
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            continue
        bullet, numbered = _BULLET.match(line), _NUMBERED.match(line)
        if bullet or numbered:
            flush_paragraph()
            wanted = "ul" if bullet else "ol"
            if kind != wanted:
                flush_list()
                kind = wanted
            items.append((bullet or numbered).group(1))  # type: ignore[union-attr]
            continue
        if items and line[:1] in (" ", "\t"):
            items[-1] += " " + line.strip()             # a wrapped list item
            continue
        flush_list()
        paragraph.append(line)
    flush_paragraph()
    flush_list()
    if version:
        out.append(f"<p><em>Version {_escape(version)}</em></p>")
    return "\n".join(out)


# ---- the cover ----------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Cover:
    path: Path
    size: int
    sha256: str
    content_type: str
    width: int
    height: int
    data: bytes = field(repr=False, compare=False)


def _png_size(data: bytes) -> tuple[int, int] | None:
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR" or len(data) < 24:
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


_SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    """The frame size from a JPEG's SOF segment (the markers before the scan)."""
    if data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return None
        while i < len(data) and data[i] == 0xFF:        # fill bytes
            i += 1
        if i >= len(data):
            return None
        marker = data[i]
        i += 1
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:   # no length
            continue
        if marker in (0xD9, 0xDA):                      # end, or the scan: no frame seen
            return None
        if i + 2 > len(data):
            return None
        length = int.from_bytes(data[i:i + 2], "big")
        if length < 2:
            return None
        if marker in _SOF:
            if i + 7 > len(data):
                return None
            return (int.from_bytes(data[i + 5:i + 7], "big"),
                    int.from_bytes(data[i + 3:i + 5], "big"))
        i += length
    return None


def inspect_cover(path: Path, *, pinned_sha256: str | None, root: Path,
                  secrets: Any = None) -> Cover:
    """The cover as it is on disk, or DeliveryProblem naming why it cannot be used."""
    try:
        inside = path.resolve().is_relative_to(root.resolve())
    except OSError:
        inside = False
    if not inside:
        raise DeliveryProblem(f"{path} is outside the products folder")
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise DeliveryProblem(f"no cover at {path}") from None
    except OSError as error:
        raise DeliveryProblem(f"cannot read {path} ({type(error).__name__})") from error
    if not stat.S_ISREG(info.st_mode):
        raise DeliveryProblem(f"the cover {path} is not a regular file (a link or a folder)")
    if info.st_size > MAX_COVER_BYTES:
        raise DeliveryProblem(f"the cover is {info.st_size:,} bytes - at most "
                              f"{MAX_COVER_BYTES:,}")
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_COVER_BYTES + 1)
    except OSError as error:
        raise DeliveryProblem(f"cannot read {path} ({type(error).__name__})") from error
    if len(data) > MAX_COVER_BYTES:
        raise DeliveryProblem(f"the cover is over {MAX_COVER_BYTES:,} bytes")
    sha = hashlib.sha256(data).hexdigest()
    if pinned_sha256 is not None and sha != pinned_sha256:
        raise DeliveryProblem(f"the cover's sha256 is {sha[:16]}..., not the pinned "
                              f"{pinned_sha256[:16]}... - it is not the image you approved "
                              "(it changed, or the wrong sha was given)")
    png, jpeg = _png_size(data), _jpeg_size(data)
    suffix = path.suffix.lower()
    if png is not None:
        content_type, size = "image/png", png
        if suffix != ".png":
            raise DeliveryProblem(f"the cover is a PNG but is named {path.name!r}")
    elif jpeg is not None:
        content_type, size = "image/jpeg", jpeg
        if suffix not in (".jpg", ".jpeg"):
            raise DeliveryProblem(f"the cover is a JPEG but is named {path.name!r}")
    else:
        raise DeliveryProblem("the cover is not a PNG or JPEG image (its header says "
                              "otherwise)")
    width, height = size
    if width < MIN_COVER_SIZE[0] or height < MIN_COVER_SIZE[1]:
        raise DeliveryProblem(f"the cover is {width}x{height} - at least "
                              f"{MIN_COVER_SIZE[0]}x{MIN_COVER_SIZE[1]}")
    if secrets is not None:
        problem = scan_bytes(path.name, data, secrets)
        if problem:
            raise DeliveryProblem(f"the cover: {problem}")
    return Cover(path=path, size=len(data), sha256=sha, content_type=content_type,
                 width=width, height=height, data=data)


# ---- settings -----------------------------------------------------------------------------
Opener = Callable[..., Any]


def _is_loopback(url: str) -> bool:
    return urllib.parse.urlparse(url).hostname in {"127.0.0.1", "localhost", "::1"}


@dataclass(frozen=True, slots=True)
class ProductSettings:
    """Where Gumroad is, where the token lives (its PATH, never the token), where products
    are staged, and where the owner's secrets are (every value is looked for in a zip)."""

    api_url: str = DEFAULT_GUMROAD_URL
    token_file: Path = Path("~/.pionir/secrets/gumroad-token.txt")
    products_dir: Path = Path("~/.pionir/products")
    secrets_dir: Path | None = Path("~/.pionir/secrets")
    secret_files: tuple[Path, ...] = ()
    secret_values: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    ssh_dir: Path | None = Path("~/.ssh")
    timeout_seconds: int = 30
    upload_timeout_seconds: int = 600

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlparse(self.api_url)
        # The token is a secret: it only ever travels over TLS, or to this machine.
        if not (parsed.scheme == "https" or (parsed.scheme == "http"
                                             and _is_loopback(self.api_url))):
            raise ValueError("the Gumroad URL must be https: (or http: on loopback)")
        if not parsed.hostname:
            raise ValueError("the Gumroad URL needs a host")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("the Gumroad URL cannot carry credentials or query data")
        if self.timeout_seconds < 5:
            raise ValueError("the Gumroad timeout must be at least 5 seconds")
        if self.upload_timeout_seconds < self.timeout_seconds:
            raise ValueError("the upload timeout cannot be shorter than the Gumroad timeout")
        for name in ("token_file", "products_dir"):
            object.__setattr__(self, name, Path(getattr(self, name)).expanduser())
        for name in ("secrets_dir", "ssh_dir"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, Path(value).expanduser())
        object.__setattr__(self, "secret_files",
                           tuple(Path(p).expanduser() for p in self.secret_files))


def product_settings(configured: Any) -> ProductSettings:
    """The ProductSettings for a PionirSettings: its Gumroad URL and token, the products
    folder, and the same secret sources a client delivery is scanned for."""
    from pionir.adapters.clients import client_settings

    client = client_settings(configured)
    return ProductSettings(
        api_url=configured.gumroad_url or DEFAULT_GUMROAD_URL,
        token_file=configured.gumroad_token_path,
        products_dir=configured.products_path,
        secrets_dir=client.secrets_dir,
        secret_files=(*client.secret_files, client.token_file),
        secret_values=client.secret_values,
        ssh_dir=client.ssh_dir,
    )


# ---- failures -----------------------------------------------------------------------------
class _Failure(Exception):
    """One step's plain answer (already scrubbed), carried out of the step to execute()."""

    def __init__(self, output: dict[str, Any]) -> None:
        super().__init__(output.get("error"))
        self.output = output


def _refused(why: str, **extra: Any) -> _Failure:
    return _Failure({"ok": False, "refused": why, "error": why, **extra})


def _unavailable(why: str, **extra: Any) -> _Failure:
    return _Failure({"ok": False, "unavailable": why, "error": why, **extra})


@dataclass(frozen=True, slots=True)
class Staged:
    """A product's files as checked on disk: the zip and the cover. No secret in here."""

    zip: Inspection
    cover: Cover


def _row(product: Mapping[str, Any]) -> dict[str, Any]:
    def number(key: str) -> Any:
        value = product.get(key)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) \
            else None
    return {"id": product.get("id"), "slug": product.get("custom_permalink"),
            "name": product.get("name"), "published": product.get("published") is True,
            "price_cents": number("price"), "sales_count": number("sales_count"),
            "sales_usd_cents": number("sales_usd_cents"), "url": product.get("short_url")}


class ProductAdapter:
    """Gumroad products as gated, audited Pionir capabilities."""

    def __init__(self, settings: ProductSettings | None = None, *,
                 opener: Opener | None = None) -> None:
        self.settings = settings or ProductSettings()
        self._api = self.settings.api_url.rstrip("/")
        self._open = opener or urllib.request.build_opener().open
        self._manifest = AgentManifest(
            agent_id="product",
            version="pionir/product",
            capabilities=(
                Capability(
                    name=PUBLISH,
                    description="Put a staged product on sale on Gumroad: create or update "
                                "its listing, upload its zip and cover, and publish it "
                                "(only after the owner approves it)",
                    risk=RiskLevel.PRIVILEGED,
                    required_permissions=frozenset({PUBLISH}),
                    requires_approval=True,
                    routable=False,
                ),
                Capability(
                    name=UNPUBLISH,
                    description="Take a product off sale on Gumroad (it stays as a draft)",
                    risk=RiskLevel.PRIVILEGED,
                    required_permissions=frozenset({UNPUBLISH}),
                    routable=False,
                ),
                Capability(
                    name=LIST,
                    description="List the Gumroad products, whether each is on sale, and "
                                "their sales",
                    risk=RiskLevel.READ_ONLY,
                    routable=False,
                ),
            ),
        )

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    # ---- configuration (local files only) --------------------------------------------
    def _not_configured(self) -> str:
        return f"not configured: no Gumroad token at {self.settings.token_file} - {SETUP_HINT}"

    def status(self) -> Mapping[str, Any]:
        """Local only - no call to Gumroad, so doctor stays hermetic and fast."""
        if read_token(self.settings.token_file) is None:
            raise AdapterUnavailable(self._not_configured())
        return {"url": self._api, "token": "configured",
                "products_dir": str(self.settings.products_dir)}

    # ---- the request -----------------------------------------------------------------
    def validate(self, task: Task) -> None:
        """Refuse a bad product, missing or failing files, or a call that cannot run,
        before it is parked."""
        if task.capability == PUBLISH:
            product = self._product(task)
            try:
                self._stage(product)
            except DeliveryProblem as error:
                raise AdapterProtocolError(f"{PUBLISH} refused by Pionir - {error}") from error
        elif task.capability == UNPUBLISH:
            self._slug(task)
        elif task.capability == LIST:
            self._check_list(task)
        else:
            raise AdapterProtocolError(f"product has no capability {task.capability!r}")
        if task.capability in (PUBLISH, UNPUBLISH) \
                and read_token(self.settings.token_file) is None:
            # Asking the owner to approve a listing that cannot be sent wastes his yes.
            raise AdapterUnavailable(self._not_configured())

    @staticmethod
    def _product(task: Task) -> dict[str, Any]:
        try:
            return check_product(task.payload)
        except ValueError as error:
            raise AdapterProtocolError(f"{PUBLISH} refused by Pionir - {error}") from error

    @staticmethod
    def _slug(task: Task) -> str:
        try:
            return check_unpublish(task.payload)
        except ValueError as error:
            raise AdapterProtocolError(f"{UNPUBLISH} refused by Pionir - {error}") from error

    @staticmethod
    def _check_list(task: Task) -> None:
        if task.payload:
            raise AdapterProtocolError(f"{LIST} refused by Pionir - it takes no fields")

    def _secrets(self) -> Any:
        return load_secrets(self.settings.secrets_dir,
                            (*self.settings.secret_files, self.settings.token_file),
                            self.settings.ssh_dir, self.settings.secret_values)

    def _stage(self, product: Mapping[str, Any]) -> Staged:
        """Every file check, with the owner's secrets read fresh. DeliveryProblem if any
        fails; the bytes returned are the bytes checked (and uploaded)."""
        secrets = self._secrets()
        root = self.settings.products_dir
        folder = root / product["slug"]
        checked = inspect_zip(folder / product["zip_name"], secrets,
                              pinned_sha256=product["zip_sha256"], root=root,
                              max_bytes=MAX_PRODUCT_ZIP_BYTES,
                              max_uncompressed=MAX_PRODUCT_UNCOMPRESSED,
                              allow_executables=product["allow_executables"],
                              folder="the products folder")
        cover = inspect_cover(folder / product["cover_name"],
                              pinned_sha256=product["cover_sha256"], root=root,
                              secrets=secrets)
        return Staged(zip=checked, cover=cover)

    def product_preview(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """What the Discord card shows for a parked publish: the zip and the cover as they
        are on disk now, checked the same way execute() will check them. The cover's
        bytes are included (the card attaches it). Never a secret value."""
        try:
            staged = self._stage(check_product(payload))
        except (ValueError, DeliveryProblem) as error:
            return {"ok": False, "problem": str(error)}
        return {"ok": True,
                "zip": {"size": staged.zip.size, "sha256": staged.zip.sha256,
                        "files": [list(f) for f in staged.zip.files],
                        "executables": list(staged.zip.executables),
                        "secret_values": staged.zip.secret_values},
                "cover": {"name": staged.cover.path.name, "size": staged.cover.size,
                          "sha256": staged.cover.sha256,
                          "content_type": staged.cover.content_type,
                          "width": staged.cover.width, "height": staged.cover.height},
                "cover_bytes": staged.cover.data}

    # ---- the call --------------------------------------------------------------------
    def execute(self, task: Task) -> TaskResult:
        if task.capability == PUBLISH:
            product = self._product(task)
            try:
                # Everything again, from the bytes on disk NOW: they may have changed since
                # the owner approved. The bytes checked here are the bytes uploaded.
                staged = self._stage(product)
            except DeliveryProblem as error:
                _log.info("product: %s refused before anything was sent", product["slug"])
                return self._result(task, {"ok": False, "refused": f"{error} - "
                                           f"{NOTHING_PUBLISHED}",
                                           "error": f"{error} - {NOTHING_PUBLISHED}"})
        elif task.capability == UNPUBLISH:
            slug = self._slug(task)
        elif task.capability == LIST:
            self._check_list(task)
        else:
            raise AdapterProtocolError(f"product has no capability {task.capability!r}")
        token = read_token(self.settings.token_file)
        if token is None:
            why = self._not_configured()
            return self._result(task, {"ok": False, "unavailable": why, "error": why,
                                       "not_configured": True})
        try:
            if task.capability == PUBLISH:
                output = self._publish(product, staged, token)
            elif task.capability == UNPUBLISH:
                output = self._unpublish(slug, token)
            else:
                output = {"ok": True, "products": [_row(p) for p in self._all(token)]}
        except _Failure as failure:
            output = failure.output
        # Belt and braces: whatever Gumroad echoed, the token does not leave in the result.
        return self._result(task, json.loads(self._scrub(json.dumps(output), token)))

    def _publish(self, product: Mapping[str, Any], staged: Staged,
                 token: str) -> dict[str, Any]:
        slug = product["slug"]
        fields = {
            "name": product["name"],
            "price": product["price_cents"],
            "price_currency_type": "usd",
            "customizable_price": product["pay_what_you_want"],
            "custom_permalink": slug,
            "custom_summary": product["summary"],
            "description": render_description(product["description_md"],
                                              product["version"]),
            "tags": product["tags"],
        }
        product_id: str | None = None
        created = False
        was_live = False
        step = "find the product by its permalink"
        try:
            existing = self._find(slug, token)    # a failure here has touched nothing
            if existing is not None:
                found_id = existing.get("id")
                if not isinstance(found_id, str) or not found_id:
                    raise _unavailable("Gumroad listed the product without an id")
                product_id = found_id
                if existing.get("published") is True:
                    # Off sale BEFORE it is changed: a failure part-way must never leave a
                    # live listing with half its new content.
                    step = "take the live listing off sale for the update"
                    self._call("PUT", self._path(product_id, "/disable"), step, token)
                    was_live = True
                step = "update the listing"
                self._call("PUT", self._path(product_id), step, token, body=fields)
                _log.info("product: %s: updated product %s", slug, product_id)
            else:
                step = "create the draft"
                made = self._call("POST", "/products", step, token,
                                  body={"native_type": "digital", **fields, "draft": True})
                made_product = made.get("product")
                made_id = made_product.get("id") if isinstance(made_product, Mapping) \
                    else None
                if not isinstance(made_id, str) or not made_id:
                    raise _unavailable("Gumroad answered the create without a product id - "
                                       "check the Gumroad dashboard for a stray draft")
                product_id, created = made_id, True
                _log.info("product: %s: created draft %s", slug, product_id)
                if made_product.get("published") is True:
                    raise _refused("Gumroad published the product on create although it "
                                   "was sent as a draft", maybe_live=True)
            step = "upload the zip"
            file_url = self._upload_zip(product["zip_name"], staged.zip.data, token)
            step = "attach the zip"
            self._call("PUT", self._path(product_id), step, token,
                       body={"files": [{"url": file_url,
                                        "display_name": product["zip_name"]}]})
            step = "upload the cover"
            signed_id = self._upload_cover(staged.cover, token)
            step = "attach the cover"
            covers = self._call("POST", self._path(product_id, "/covers"), step, token,
                                body={"signed_blob_id": signed_id})
            step = "publish"
            done = self._call("PUT", self._path(product_id, "/enable"), step, token)
        except _Failure as failure:
            raise self._left_unpublished(failure, step, product_id, created, was_live,
                                         token) from None
        live = done.get("product") if isinstance(done.get("product"), Mapping) else {}
        if live.get("published") is False:
            raise self._left_unpublished(
                _unavailable("Gumroad answered the publish step but says the product is "
                             "not published"), "publish", product_id, created, was_live,
                token)
        url = live.get("short_url")
        if not isinstance(url, str) or not url.startswith("https://"):
            url = f"https://gumroad.com/l/{slug}"
        _log.info("product: %s: product %s is on sale", slug, product_id)
        result: dict[str, Any] = {"ok": True, "product_id": product_id, "url": url,
                                  "created": created, "published": True,
                                  "version": product["version"]}
        listed = covers.get("covers")
        if isinstance(listed, list) and len(listed) > 1:
            result["note"] = (f"the product now has {len(listed)} covers - the new one was "
                              "added; remove old ones in the Gumroad dashboard if unwanted")
        return result

    def _left_unpublished(self, failure: _Failure, step: str, product_id: str | None,
                          created: bool, was_live: bool, token: str) -> _Failure:
        """The answer for a publish that stopped at ``step``: what state it was left in."""
        output = dict(failure.output)
        kind = "refused" if "refused" in output else "unavailable"
        why = str(output.get("error") or "it failed")
        if product_id is None:
            message = f"{why} (at step: {step}) - nothing was changed on Gumroad; " \
                      f"{NOTHING_ON_SALE}"
        else:
            status = output.get("status")
            # The publish step without a clear answer (nothing came back, a server error,
            # or a reply that did not say it is off sale) may have gone through; so may a
            # create that Gumroad published despite draft=true. Make sure it is off sale.
            maybe_live = output.get("maybe_live") is True or (
                step == "publish" and (not isinstance(status, int) or status == 0
                                       or status >= 500))
            output.pop("maybe_live", None)
            if maybe_live and not self._confirm_off_sale(product_id, token):
                message = (f"{why} (at step: {step}) - the answer to the publish was lost "
                           "and the product could not be confirmed off sale: check product "
                           f"{product_id} in the Gumroad dashboard")
                _log.warning("product: publish stopped at %s and product %s could not be "
                             "confirmed off sale", step, product_id)
                output.update({kind: message, "error": message, "step": step,
                               "product_id": product_id, "created": created,
                               "published": None})
                return _Failure(output)
            state = "left as an unpublished draft"
            if was_live:
                state += " (it was on sale before this update and is off sale until a " \
                         "publish completes)"
            message = f"{why} (at step: {step}) - the product was {state}; {NOTHING_ON_SALE}"
        _log.warning("product: publish stopped at %s: %s", step, why)
        output.update({kind: message, "error": message, "step": step,
                       "product_id": product_id, "created": created, "published": False})
        return _Failure(output)

    def _confirm_off_sale(self, product_id: str, token: str) -> bool:
        try:
            answer = self._call("PUT", self._path(product_id, "/disable"),
                                "take it off sale", token)
        except _Failure:
            return False
        product = answer.get("product")
        return not (isinstance(product, Mapping) and product.get("published") is True)

    def _unpublish(self, slug: str, token: str) -> dict[str, Any]:
        found = self._find(slug, token)
        if found is None:
            raise _refused(f"no Gumroad product has the permalink {slug!r}")
        product_id = found.get("id")
        if not isinstance(product_id, str) or not product_id:
            raise _unavailable("Gumroad listed the product without an id")
        answer = self._call("PUT", self._path(product_id, "/disable"), "unpublish", token)
        product = answer.get("product") if isinstance(answer.get("product"), Mapping) else {}
        if product.get("published") is True:
            raise _unavailable("Gumroad answered the unpublish but says it is still on sale",
                               product_id=product_id)
        _log.info("product: %s: product %s taken off sale", slug, product_id)
        return {"ok": True, "product_id": product_id, "slug": slug, "published": False}

    # ---- Gumroad: finding, uploading ---------------------------------------------------
    @staticmethod
    def _path(product_id: str, suffix: str = "") -> str:
        return f"/products/{urllib.parse.quote(product_id, safe='')}{suffix}"

    def _all(self, token: str) -> list[Mapping[str, Any]]:
        """Every product, following Gumroad's page_key cursor."""
        products: list[Mapping[str, Any]] = []
        page_key: str | None = None
        for _ in range(MAX_PAGES):
            path = "/products" + (f"?{urllib.parse.urlencode({'page_key': page_key})}"
                                  if page_key else "")
            page = self._call("GET", path, "list the products", token)
            rows = page.get("products")
            products += [p for p in rows if isinstance(p, Mapping) and not p.get("deleted")] \
                if isinstance(rows, list) else []
            next_key = page.get("next_page_key")
            if not isinstance(next_key, str) or not next_key or next_key == page_key:
                return products
            page_key = next_key
        raise _unavailable(f"Gumroad listed more than {MAX_PAGES} pages of products")

    def _find(self, slug: str, token: str) -> Mapping[str, Any] | None:
        for product in self._all(token):
            permalink = product.get("custom_permalink")
            if isinstance(permalink, str) and permalink.lower() == slug:
                return product
        return None

    def _upload_zip(self, name: str, data: bytes, token: str) -> str:
        """Gumroad's multipart upload: presign, PUT each part, complete. The file URL."""
        started = self._call("POST", "/files/presign", "upload the zip (presign)", token,
                             body={"filename": name, "file_size": len(data)})
        upload_id, key, parts = started.get("upload_id"), started.get("key"), \
            started.get("parts")
        expected = max(1, math.ceil(len(data) / PART_SIZE))
        if not isinstance(upload_id, str) or not isinstance(key, str) \
                or not isinstance(parts, list) or len(parts) != expected:
            raise _unavailable("Gumroad's presign answer was not a usable upload "
                               f"(expected {expected} part(s))")
        try:
            done: list[dict[str, Any]] = []
            for part in parts:
                number = part.get("part_number") if isinstance(part, Mapping) else None
                url = part.get("presigned_url") if isinstance(part, Mapping) else None
                if not isinstance(number, int) or not 1 <= number <= expected \
                        or not isinstance(url, str) or not self._storage_url_ok(url):
                    raise _unavailable("Gumroad's presign answer had an unusable part")
                chunk = data[(number - 1) * PART_SIZE:number * PART_SIZE]
                status, _doc, headers = self._http("PUT", url, raw=chunk, auth=None,
                                                   content_type="application/octet-stream",
                                                   upload=True)
                etag = headers.get("ETag") if headers is not None else None
                if not 200 <= status < 300 or not etag:
                    why = (f"Gumroad's storage did not take part {number} of the zip "
                           + (f"(HTTP {status})" if status else "(no answer)"))
                    raise (_refused if 400 <= status < 500 else _unavailable)(why,
                                                                              status=status)
                done.append({"part_number": number, "etag": etag})
            finished = self._call("POST", "/files/complete", "upload the zip (complete)",
                                  token, body={"upload_id": upload_id, "key": key,
                                               "parts": done})
        except _Failure:
            self._abort(upload_id, key, token)
            raise
        file_url = finished.get("file_url")
        if not isinstance(file_url, str) or not file_url.startswith("https://"):
            raise _unavailable("Gumroad completed the upload without a usable file URL")
        return file_url

    def _abort(self, upload_id: str, key: str, token: str) -> None:
        try:
            self._call("POST", "/files/abort", "abort the upload", token,
                       body={"upload_id": upload_id, "key": key})
        except _Failure:
            _log.warning("product: a failed zip upload could not be aborted (Gumroad "
                         "expires it)")

    def _upload_cover(self, cover: Cover, token: str) -> str:
        """Gumroad's direct upload for images: the blob, then its bytes. The signed id."""
        checksum = base64.b64encode(hashlib.md5(cover.data, usedforsecurity=False)
                                    .digest()).decode("ascii")
        blob = self._call("POST", "/direct_uploads", "upload the cover (start)", token,
                          body={"blob": {"filename": cover.path.name,
                                         "byte_size": cover.size, "checksum": checksum,
                                         "content_type": cover.content_type}},
                          envelope=False)
        signed_id = blob.get("signed_id")
        direct = blob.get("direct_upload")
        url = direct.get("url") if isinstance(direct, Mapping) else None
        headers = direct.get("headers") if isinstance(direct, Mapping) else None
        if not isinstance(signed_id, str) or not signed_id or not isinstance(url, str) \
                or not self._storage_url_ok(url):
            raise _unavailable("Gumroad's direct upload answer was not a usable upload")
        extra = {str(k): str(v) for k, v in headers.items()} \
            if isinstance(headers, Mapping) else {}
        extra.pop("Authorization", None)
        status, _doc, _headers = self._http("PUT", url, raw=cover.data, auth=None,
                                            content_type=cover.content_type, upload=True,
                                            extra=extra)
        if not 200 <= status < 300:
            why = "Gumroad's storage did not take the cover " + (
                f"(HTTP {status})" if status else "(no answer)")
            raise (_refused if 400 <= status < 500 else _unavailable)(why, status=status)
        return signed_id

    def _storage_url_ok(self, url: str) -> bool:
        """A storage URL gets the bytes but never the token: https (or loopback http when
        Gumroad itself is on loopback, for tests), no credentials."""
        parsed = urllib.parse.urlparse(url)
        if parsed.username or parsed.password or not parsed.hostname:
            return False
        return parsed.scheme == "https" or (parsed.scheme == "http" and _is_loopback(url)
                                            and _is_loopback(self._api))

    # ---- Gumroad: one call ---------------------------------------------------------------
    def _call(self, method: str, path: str, step: str, token: str, *,
              body: Mapping[str, Any] | None = None, envelope: bool = True) -> dict[str, Any]:
        """One Gumroad API call, its envelope mapped to an answer: the document, or a
        _Failure (refused: Gumroad said no; unavailable: token, outage, not an answer)."""
        status, document, _headers = self._http(method, f"{self._api}{path}", body=body,
                                                auth=token)
        doc = document if isinstance(document, Mapping) else {}
        said = self._scrub(str(doc.get("message") or doc.get("error") or ""), token)[:300]
        if status == 0:
            host = urllib.parse.urlparse(self._api).netloc
            raise _unavailable(f"Gumroad is unreachable at {host} ({step})", status=0)
        if status == 401:
            raise _unavailable(TOKEN_REJECTED, status=401, token_rejected=True)
        if status == 403:
            raise _unavailable(f"the Gumroad token may not do this ({step}; it needs the "
                               f"edit_products and view_sales scopes) - {SETUP_HINT}",
                               status=403, token_rejected=True)
        if status == 429:
            raise _unavailable(f"Gumroad is rate limiting ({step}) - try again later",
                               status=429)
        if status >= 500:
            raise _unavailable(f"Gumroad answered HTTP {status} ({step})"
                               + (f": {said}" if said else ""), status=status)
        if 400 <= status < 500:
            raise _refused(f"Gumroad refused it ({step}, HTTP {status})"
                           + (f": {said}" if said else ""), status=status)
        if not 200 <= status < 300 or not isinstance(document, Mapping):
            raise _unavailable(f"Gumroad answered the {step} step with something that is "
                               "not a result", status=status)
        if doc.get("success") is False:
            # Gumroad's quirk: a refusal at HTTP 200.
            raise _refused(f"Gumroad refused it ({step}): {said or 'no reason given'}",
                           status=status)
        if envelope and doc.get("success") is not True:
            raise _unavailable(f"Gumroad answered the {step} step without success: true",
                               status=status)
        return dict(doc)

    def _http(self, method: str, url: str, *, body: Mapping[str, Any] | None = None,
              raw: bytes | None = None, auth: str | None, content_type: str | None = None,
              upload: bool = False, extra: Mapping[str, str] | None = None,
              ) -> tuple[int, Any, Any]:
        """(HTTP status, parsed JSON or None, response headers or None). Status 0 means
        nothing answered. ``auth`` is the token for Gumroad's API, None for a storage URL
        (which must never see it)."""
        headers = {"Accept": "application/json", "User-Agent": "pionir-product/0.1"}
        data = None
        if raw is not None:
            data = raw
            headers["Content-Type"] = content_type or "application/octet-stream"
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        headers.update(extra or {})
        if auth is not None:
            headers["Authorization"] = f"Bearer {auth}"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        timeout = self.settings.upload_timeout_seconds if upload \
            else self.settings.timeout_seconds
        try:
            # timeout by keyword: OpenerDirector.open(url, data=None, timeout=...) takes a
            # positional second argument as the POST body.
            with self._open(request, timeout=timeout) as response:
                return (int(getattr(response, "status", 200)), self._json(response),
                        getattr(response, "headers", None))
        except urllib.error.HTTPError as error:
            return error.code, self._json(error), getattr(error, "headers", None)
        except (urllib.error.URLError, TimeoutError, OSError):
            # the exception text is not carried: it could echo the request
            return 0, None, None

    @staticmethod
    def _json(response: Any) -> Any:
        try:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, ValueError, AttributeError):
            return None
        if not raw or len(raw) > MAX_RESPONSE_BYTES:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None

    @staticmethod
    def _scrub(text: str, token: str) -> str:
        if not token:
            return text
        text = text.replace(token, "<redacted>")
        quoted = urllib.parse.quote_plus(token)
        return text.replace(quoted, "<redacted>") if quoted != token else text

    def _result(self, task: Task, output: dict[str, Any]) -> TaskResult:
        evidence = [f"product:{task.capability.split('.', 1)[1]}"]
        slug = task.payload.get("slug")
        if isinstance(slug, str) and _SLUG.fullmatch(slug):
            evidence.append(f"product:slug:{slug}")
        if isinstance(output.get("product_id"), str):
            evidence.append(f"product:id:{output['product_id']}")
        return TaskResult(task_id=task.task_id, agent_id=self.manifest.agent_id,
                          output=output, evidence=tuple(evidence))

    # ---- the account check (python -m pionir gumroad-check) ----------------------------
    def check_account(self) -> tuple[int, dict[str, Any]]:
        """Who the token is for, the products and their sales. Reads only: never changes a
        product, never shows the token. (0 ok, 1 not configured or not checked, 2
        rejected)."""
        token = read_token(self.settings.token_file)
        if token is None:
            return 1, {"status": "not_configured", "message": self._not_configured()}
        try:
            me = self._call("GET", "/user", "account check", token)
            products = [_row(p) for p in self._all(token)]
        except _Failure as failure:
            out = failure.output
            if out.get("token_rejected"):
                return 2, {"status": "rejected", "message": self._scrub(
                    str(out.get("error")), token)}
            return 1, {"status": "not_checked",
                       "message": self._scrub(str(out.get("error")), token)}
        user = me.get("user") if isinstance(me.get("user"), Mapping) else {}
        cents = [p["sales_usd_cents"] for p in products
                 if isinstance(p["sales_usd_cents"], (int, float))]
        counts = [p["sales_count"] for p in products
                  if isinstance(p["sales_count"], (int, float))]
        report = {"status": "ok", "account": user.get("name") or user.get("user_id"),
                  "api": self._api, "products": products,
                  "on_sale": sum(1 for p in products if p["published"]),
                  "sales_count": sum(counts), "sales_usd": f"{sum(cents) / 100:,.2f}"}
        return 0, json.loads(self._scrub(json.dumps(report), token))

    # ---- the upload probe (python -m pionir gumroad-check --probe-upload) --------------
    def probe_upload(self, say: Callable[[str], Any] = print) -> int:
        """Prove the upload paths once, before the owner's first real approval: make a
        throwaway DRAFT (never enabled), upload a tiny zip and a cover to it the way a real
        publish does, look at what Gumroad shows, then delete it. Every step is said in
        plain words, never the token. 0 only if every step passed and the draft is gone;
        1 otherwise (or not configured); 2 if the token was rejected."""
        token = read_token(self.settings.token_file)
        if token is None:
            say(f"NOT OK  {self._not_configured()}")
            return 1
        slug = f"{PROBE_PERMALINK_PREFIX}{secrets.token_hex(4)}"
        state: dict[str, Any] = {"ok": True, "rejected": False, "product_id": None,
                                 "live": False}

        def ok(text: str) -> None:
            say(f"OK      {self._scrub(text, token)}")

        def fail(text: str) -> None:
            state["ok"] = False
            say(f"NOT OK  {self._scrub(text, token)}")

        def failed(step: str, failure: _Failure) -> None:
            if failure.output.get("token_rejected"):
                state["rejected"] = True
            fail(f"{step}: {failure.output.get('error')}")

        say(f"Gumroad upload probe at {self._api} - a throwaway draft, never published, "
            "deleted at the end")

        def run_steps() -> None:
            step = "create the draft"
            try:
                made = self._call("POST", "/products", step, token, body={
                    "native_type": "digital", "name": PROBE_NAME, "price": 100,
                    "price_currency_type": "usd", "custom_permalink": slug, "draft": True})
            except _Failure as failure:
                failed(step, failure)
                return
            product = made.get("product") if isinstance(made.get("product"), Mapping) else {}
            product_id = product.get("id")
            if not isinstance(product_id, str) or not product_id:
                fail(f"{step}: Gumroad answered without a product id - look for a draft "
                     f"with the permalink {slug} in the dashboard and delete it")
                return
            state["product_id"] = product_id
            if product.get("published") is not False:
                # Never continue with something that might be on sale.
                state["live"] = True
                fail(f"{step}: Gumroad reports product {product_id} as published "
                     f"(published: {product.get('published')!r}) although it was sent as a "
                     "draft - stopping; it is taken off sale and deleted below")
                return
            ok(f"{step}: product {product_id}, permalink {slug}, published: false")

            zipped = _probe_zip()
            png = _probe_png(*PROBE_COVER)
            cover = Cover(path=Path("pionir-probe-cover.png"), size=len(png),
                          sha256=hashlib.sha256(png).hexdigest(), content_type="image/png",
                          width=PROBE_COVER[0], height=PROBE_COVER[1], data=png)
            held: dict[str, Any] = {}

            def upload_zip() -> str:
                held["file_url"] = self._upload_zip("pionir-probe.zip", zipped, token)
                return (f"a {len(zipped)}-byte zip went up through presign, one part PUT "
                        "and complete")

            def attach_zip() -> str:
                answer = self._call("PUT", self._path(product_id), "attach the zip", token,
                                    body={"files": [{"url": held["file_url"],
                                                     "display_name": "pionir-probe.zip"}]})
                shown = answer.get("product")
                files = shown.get("files") if isinstance(shown, Mapping) else None
                return "Gumroad accepted it" + (
                    f" (the product now lists {len(files)} file(s))"
                    if isinstance(files, list) else "")

            def upload_cover() -> str:
                held["signed_id"] = self._upload_cover(cover, token)
                return (f"a {PROBE_COVER[0]}x{PROBE_COVER[1]} PNG ({cover.size} bytes) went "
                        "up through direct_uploads and its storage PUT")

            def attach_cover() -> str:
                answer = self._call("POST", self._path(product_id, "/covers"),
                                    "attach the cover", token,
                                    body={"signed_blob_id": held["signed_id"]})
                covers = answer.get("covers")
                return "Gumroad accepted it" + (
                    f" ({len(covers)} cover(s), main cover {answer.get('main_cover_id')})"
                    if isinstance(covers, list) else "")

            stages = [("upload the zip", upload_zip), ("attach the zip", attach_zip),
                      ("upload the cover", upload_cover), ("attach the cover", attach_cover)]
            for name, run in stages:
                try:
                    ok(f"{name}: {run()}")
                except _Failure as failure:
                    failed(name, failure)
                    return
            self._probe_verify(product_id, token, state, ok, fail, failed)

        try:
            run_steps()
        finally:
            # whatever happened above, a probe draft never outlives the probe
            if state["product_id"] is not None:
                self._probe_cleanup(state, token, slug, ok, fail, failed)
                say("PASSED - the upload paths work; the probe draft was deleted"
                    if state["ok"] else "FAILED - see the NOT OK lines above")
            elif not state["ok"]:
                say("FAILED - nothing was created on Gumroad")
        return self._probe_code(state)

    def _probe_verify(self, product_id: str, token: str, state: dict[str, Any],
                      ok: Callable[[str], None], fail: Callable[[str], None],
                      failed: Callable[[str, _Failure], None]) -> None:
        """Re-read the draft and report what Gumroad shows of the file and the cover."""
        step = "check the draft"
        try:
            answer = self._call("GET", self._path(product_id), step, token)
        except _Failure as failure:
            failed(step, failure)
            return
        product = answer.get("product") if isinstance(answer.get("product"), Mapping) else {}
        if product.get("published") is not False:
            state["live"] = True
            fail(f"{step}: Gumroad reports the draft as published "
                 f"(published: {product.get('published')!r}) - it is taken off sale and "
                 "deleted below")
            return
        files = product.get("files")
        covers = product.get("covers")
        preview = product.get("preview_url") or product.get("thumbnail_url")
        seen: list[str] = []
        if isinstance(files, list):
            if not files:
                fail(f"{step}: Gumroad shows the draft with no files - the zip did not "
                     "stick")
                return
            names = ", ".join(str(f.get("name") or f.get("display_name") or f.get("id"))
                              for f in files if isinstance(f, Mapping))
            seen.append(f"{len(files)} file(s) ({names})")
        else:
            seen.append("no file list in its product view (the attach call succeeded)")
        if isinstance(covers, list):
            if not covers:
                fail(f"{step}: Gumroad shows the draft with no covers - the cover did not "
                     "stick")
                return
            seen.append(f"{len(covers)} cover(s)")
        elif preview:
            seen.append("a cover image (preview_url)")
        else:
            seen.append("no cover list in its product view (the attach call succeeded)")
        ok(f"{step}: still a draft (published: false); Gumroad shows " + "; ".join(seen))

    def _probe_cleanup(self, state: dict[str, Any], token: str, slug: str,
                       ok: Callable[[str], None], fail: Callable[[str], None],
                       failed: Callable[[str, _Failure], None]) -> None:
        """Always: off sale if it might be on sale, then delete, then make sure it is gone."""
        product_id = state["product_id"]
        if state["live"]:
            try:
                self._call("PUT", self._path(product_id, "/disable"), "take it off sale",
                           token)
                ok(f"take it off sale: product {product_id} disabled")
            except _Failure as failure:
                failed("take it off sale", failure)
        step = "delete the draft"
        try:
            self._call("DELETE", self._path(product_id), step, token)
        except _Failure as failure:
            failed(step, failure)
        gone = self._probe_gone(product_id, token)
        if gone:
            ok(f"{step}: product {product_id} is gone")
        else:
            fail(f"the probe draft was NOT removed: product {product_id} (permalink {slug}) "
                 "- delete it in the Gumroad dashboard")

    def _probe_gone(self, product_id: str, token: str) -> bool:
        status, document, _headers = self._http("GET", f"{self._api}{self._path(product_id)}",
                                                auth=token)
        if status == 404:
            return True
        if not 200 <= status < 300 or not isinstance(document, Mapping):
            return False                     # no answer, a 5xx, a 401: not confirmed
        if document.get("success") is False:
            return True                      # Gumroad's "not found" at HTTP 200
        product = document.get("product")
        return isinstance(product, Mapping) and product.get("deleted") is True

    @staticmethod
    def _probe_code(state: Mapping[str, Any]) -> int:
        if state["ok"]:
            return 0
        return 2 if state["rejected"] else 1


PROBE_NAME = "pionir upload probe - safe to delete"
PROBE_PERMALINK_PREFIX = "pionir-upload-probe-"
PROBE_COVER = (1280, 720)


def _probe_zip() -> bytes:
    """A tiny zip that would pass a product's own checks: a README and nothing else."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.md", "# Pionir upload probe\n\nSafe to delete. Pionir made "
                                      "this to check that uploads to Gumroad work.\n")
    return buffer.getvalue()


def _probe_png(width: int, height: int) -> bytes:
    """A plain PNG of the given size, made with the standard library."""
    row = b"\x00" + bytes((24, 70, 110)) * width
    raw = zlib.compress(row * height, 9)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", raw) + chunk(b"IEND", b""))
