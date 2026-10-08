"""What every marketplace listing must be before anyone is asked to approve it.

Shared by the crew's packager (crew/marketplaces/packager.py), which checks a draft before it
is submitted, and by Pionir's publish adapters (apify.py, chrome_webstore.py), which check the
same payload again before it is parked and once more before anything is sent. Fail closed:
a reason is a refusal.

- **Honest claims** (``claim_problems``): no superlative or promise nobody measured ("best",
  "#1", "fastest", "guaranteed", "100%", "unlimited", "trusted by", "official" ...), no
  compliance badge nobody audited ("GDPR compliant", "HIPAA"), nothing about collecting
  people's data, no link but the stores' own.
- **The package** (``read_package``): ``<root>/listings/<slug>/<name>``, a plain file (no link),
  its SHA-256 exactly the one the draft was made with, every byte scanned for every secret the
  owner keeps (``deliveries.scan_bytes``) and every zip entry with it.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import zipfile
from pathlib import Path
from typing import Any

from pionir.adapters.deliveries import SecretValues, scan_bytes

MAX_PACKAGE_BYTES = 20 * 1024 * 1024
MAX_PACKAGE_ENTRIES = 400
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{2,39}")
_SHA = re.compile(r"[0-9a-f]{64}")
_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}")

# Claims nobody measured. Matched as whole words, case-insensitively.
UNPROVEN = (
    r"best", r"#\s?1", r"no\.?\s?1", r"number\s+one", r"fastest", r"cheapest", r"guarantee[sd]?",
    r"100\s?%", r"unlimited", r"trusted\s+by", r"millions\s+of", r"official", r"certified",
    r"award[- ]winning", r"world[- ]class", r"most\s+(?:accurate|popular|powerful|advanced)",
    r"(?:gdpr|hipaa|soc\s?2|ccpa)[- ]?(?:compliant|certified|ready)", r"risk[- ]free",
    r"never\s+fails?", r"perfect(?:ly)?", r"instant(?:ly)?\s+rank", r"rank\s+#?1",
)
_UNPROVEN = re.compile(r"(?i)(?<![a-z0-9])(?:" + "|".join(UNPROVEN) + r")(?![a-z0-9])")
# Words that would sell a scraper of people's data, which the owner does not make.
_PERSONAL = re.compile(r"(?i)(?<![a-z0-9])(?:emails?\s+(?:addresses|scraper|extractor|finder)|"
                       r"phone\s+numbers?|leads?\s+(?:list|scraper|generation)|"
                       r"(?:linkedin|instagram|facebook|tiktok)\s+(?:profiles?|followers|scraper))"
                       r"(?![a-z0-9])")
_LINK = re.compile(r"(?i)\bhttps?://([^/\s)\"'>]+)")
LINK_HOSTS = frozenset({"apify.com", "docs.apify.com", "console.apify.com",
                        "chromewebstore.google.com", "extensionpay.com", "apps.shopify.com",
                        "example.com"})


class ListingProblem(ValueError):
    """A listing or its package may not be submitted, with the reason."""


def claim_problems(fields: dict) -> list:
    """Every reason the words in ``fields`` ({name: text or list of text}) are not honest."""
    reasons = []
    for key, value in fields.items():
        texts = value if isinstance(value, list) else [value]
        for text in texts:
            if not isinstance(text, str):
                continue
            m = _UNPROVEN.search(text)
            if m:
                reasons.append(f"{key}: claims {m.group(0)!r}, which nobody measured")
            p = _PERSONAL.search(text)
            if p:
                reasons.append(f"{key}: mentions {p.group(0)!r} - no product here handles "
                               "people's personal data")
            for host in _LINK.findall(text):
                host = host.lower().split(":")[0]
                if host not in LINK_HOSTS and not host.endswith(".example.com"):
                    reasons.append(f"{key}: links to {host}, not the store's own pages")
    return reasons


def check_slug(slug: Any) -> str:
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
        raise ListingProblem("slug: 3-40 of a-z, 0-9 and -")
    return slug


def check_sha(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ListingProblem(f"{what}: a 64-character lower-case SHA-256")
    return value


def package_path(root: Path, slug: str, name: str) -> Path:
    check_slug(slug)
    if not isinstance(name, str) or not _FILE.fullmatch(name) or name.startswith("."):
        raise ListingProblem("the package name must be a bare file name")
    return Path(root) / "listings" / slug / name


def read_package(root: Path, slug: str, name: str, sha256: str,
                 secrets: SecretValues | None) -> bytes:
    """The package's bytes, exactly as drafted, or ListingProblem with the reason."""
    check_sha(sha256, "package_sha256")
    path = package_path(root, slug, name)
    try:
        if path.is_symlink() or not path.is_file():
            raise ListingProblem(f"{path} is not a file (was the draft removed?)")
        if os.path.getsize(path) > MAX_PACKAGE_BYTES:
            raise ListingProblem(f"{path} is larger than {MAX_PACKAGE_BYTES} bytes")
        data = path.read_bytes()
    except OSError as exc:
        raise ListingProblem(f"{path} cannot be read ({type(exc).__name__})") from exc
    if hashlib.sha256(data).hexdigest() != sha256:
        raise ListingProblem("package_sha256: the package changed since the listing was "
                             "drafted; nothing was sent")
    if secrets is not None:
        why = scan_bytes(name, data, secrets)
        if why:
            raise ListingProblem(f"secrets found - {why}; nothing was sent")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_PACKAGE_ENTRIES:
                raise ListingProblem(f"the package holds more than {MAX_PACKAGE_ENTRIES} files")
            for info in infos:
                n = info.filename
                if n.startswith(("/", "\\")) or ".." in n.replace("\\", "/").split("/") \
                        or ":" in n:
                    raise ListingProblem(f"the package entry {n!r} is not a plain relative path")
                if secrets is not None and not info.is_dir():
                    why = scan_bytes(n, zf.read(info), secrets)
                    if why:
                        raise ListingProblem(f"secrets found - {why}; nothing was sent")
    except zipfile.BadZipFile as exc:
        raise ListingProblem(f"the package is not a zip ({exc})") from exc
    return data
