"""The checks a client delivery passes before the owner is asked to approve it.

The owner (or a Claude Code pane he runs) builds a client's work and drops the zip at
``<deliveries_dir>/<order_id>/<name>.zip``. ``client.deliver`` (in ``clients.py``) ships
it; this module decides whether that zip may leave the machine at all. Everything here
is local and read-only: no network, nothing written.

A zip passes only if (fail closed - any doubt is a refusal naming the reason):

- the file is a regular file inside the order's folder, at most 25,000,000 bytes, and
  its SHA-256 is the one the payload pinned (the owner approves exactly those bytes);
- it opens as a zip, declares at most 200 MB uncompressed and no entry compresses
  absurdly (a zip bomb), and no entry is encrypted (it could not be scanned);
- every entry name is relative and plain: no absolute path, drive letter or colon,
  no ``..``, no backslash, no control character, no duplicate; no symlink;
- a README or HOWTO (``.md`` or ``.txt``, any case) sits at the top level;
- no entry is an executable binary (by extension, or a Windows/ELF header), nor an
  archive inside the archive (its contents could not be scanned);
- no entry is a file that commonly holds secrets (``.env``, keys, ``credentials*``,
  ``.npmrc``, ``.pypirc``, anything under ``.git/``);
- no entry's bytes contain one of the owner's real secret values (every file under
  ``~/.pionir/secrets``, Pionir's configured token files and any ``~/.ssh`` private key)
  or anything shaped like a common key format.

A refusal names the entry and WHICH secret file or key format matched - never the value.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_ZIP_BYTES = 25_000_000
MAX_UNCOMPRESSED = 200_000_000
MAX_ENTRIES = 10_000
# Deflate tops out near 1032:1; real text and code compress 3-20x. An entry over 1 MB
# that claims more than this is a bomb, not a deliverable.
MAX_RATIO = 200
RATIO_FLOOR_BYTES = 1_000_000
MIN_SECRET_LENGTH = 12
MAX_SECRET_FILE_BYTES = 1_000_000

README_NAMES = frozenset({"readme.md", "readme.txt", "howto.md", "howto.txt"})
EXECUTABLE_SUFFIXES = frozenset({".exe", ".dll", ".msi", ".scr", ".com", ".bat", ".cmd",
                                 ".vbs", ".jar"})
# An archive inside the archive hides its contents from the scan below.
ARCHIVE_SUFFIXES = frozenset({".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".xz",
                              ".zst", ".cab", ".iso"})

# Common key formats. Each is (what it looks like, pattern over the raw bytes). A
# provider prefix needs a token-shaped tail, so a README saying "your sk_live_ key"
# passes and a real key does not.
KEY_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("a private key block (-----BEGIN ... PRIVATE KEY-----)",
     re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("an AWS access key id (AKIA...)", re.compile(rb"AKIA[0-9A-Z]{16}")),
    ("a Stripe live secret key (sk_live_...)", re.compile(rb"sk_live_[0-9A-Za-z]{8,}")),
    ("a Stripe live restricted key (rk_live_...)", re.compile(rb"rk_live_[0-9A-Za-z]{8,}")),
    ("an Anthropic API key (sk-ant-...)", re.compile(rb"sk-ant-[0-9A-Za-z_-]{8,}")),
    ("a GitHub token (ghp_...)", re.compile(rb"ghp_[0-9A-Za-z]{20,}")),
    ("a GitHub token (github_pat_...)", re.compile(rb"github_pat_[0-9A-Za-z_]{20,}")),
    ("a Slack token (xox?-...)", re.compile(rb"xox[baprs]-[0-9A-Za-z-]{10,}")),
    ("a Google API key (AIza...)", re.compile(rb"AIza[0-9A-Za-z_-]{35}")),
    ("a Discord bot token",
     re.compile(rb"(?<![\w.-])[MNO][\w-]{23,27}\.[\w-]{6}\.[\w-]{27,38}(?![\w-])")),
    ("a webhook signing secret (whsec_...)", re.compile(rb"whsec_[0-9A-Za-z+/=]{8,}")),
    ("a JWT (eyJ...eyJ...)", re.compile(rb"eyJ[\w-]+\.eyJ[\w-]+\.")),
)

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_DRIVE = re.compile(r"^[A-Za-z]:")
_PEM_ARMOR = re.compile(rb"^-----(BEGIN|END) ")


class DeliveryProblem(ValueError):
    """Why this zip may not be delivered. Never carries a secret value."""


# ---- the owner's secrets --------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SecretValues:
    """The owner's real secret values, each with the file it came from (for the report).
    ``repr`` never shows a value."""

    values: tuple[tuple[bytes, str], ...] = field(repr=False, default=())

    def __len__(self) -> int:
        return len(self.values)

    def __repr__(self) -> str:
        return f"SecretValues({len(self.values)} values)"


def _values_in(raw: bytes) -> set[bytes]:
    """Every secret-looking value in one secrets file: the whole of it, each line, a
    ``KEY=VALUE`` value, and every string inside a JSON document."""
    text = raw.lstrip(b"\xef\xbb\xbf")
    found: set[bytes] = set()
    try:
        document = json.loads(text.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        document = None
    if isinstance(document, (dict, list)):
        stack: list[Any] = [document]
        while stack:
            item = stack.pop()
            if isinstance(item, Mapping):
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, str):
                found.add(item.strip().encode("utf-8"))
        return {v for v in found if len(v) >= MIN_SECRET_LENGTH}
    found.add(text.strip())
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(b"#") or _PEM_ARMOR.match(line):
            continue
        found.add(line)
        if b"=" in line:
            found.add(line.split(b"=", 1)[1].strip().strip(b"\"'"))
    return {v for v in found if len(v) >= MIN_SECRET_LENGTH and not _PEM_ARMOR.match(v)}


def _read_secret(path: Path) -> bytes:
    with path.open("rb") as handle:
        return handle.read(MAX_SECRET_FILE_BYTES)


def load_secrets(secrets_dir: Path | None, secret_files: Iterable[Path] = (),
                 ssh_dir: Path | None = None,
                 extra: Iterable[tuple[str, str]] = ()) -> SecretValues:
    """Read the owner's secrets fresh (a new token is covered without a restart).
    Fail closed: a secrets file that exists but cannot be read raises DeliveryProblem -
    a value that cannot be read cannot be checked for."""
    sources: list[Path] = []
    if secrets_dir is not None and secrets_dir.is_dir():
        sources += sorted(p for p in secrets_dir.rglob("*") if p.is_file())
    sources += [Path(p) for p in secret_files]
    ssh_keys: list[Path] = []
    if ssh_dir is not None and ssh_dir.is_dir():
        ssh_keys = sorted(p for p in ssh_dir.iterdir()
                          if p.is_file() and not p.name.endswith(".pub")
                          and p.name not in {"known_hosts", "known_hosts.old", "config",
                                             "authorized_keys"})
    pairs: dict[bytes, str] = {}
    seen: set[Path] = set()
    for path, is_ssh in [(p, False) for p in sources] + [(p, True) for p in ssh_keys]:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            raw = _read_secret(path)
        except FileNotFoundError:
            continue
        except OSError as error:
            raise DeliveryProblem(f"the secrets scan could not read {path} "
                                  f"({type(error).__name__}) - fix that before "
                                  "delivering") from error
        if is_ssh and b"PRIVATE KEY" not in raw:
            continue   # only private keys; a public key or a note is not a secret
        for value in sorted(_values_in(raw)):
            pairs.setdefault(value, str(path))
    # Secrets that live in settings rather than files (the Daedalus and Melete service
    # tokens): (label, value) pairs; the label is what a refusal names, never the value.
    for label, value in extra:
        value = (value or "").strip()
        if len(value) >= 12:
            pairs.setdefault(value.encode("utf-8"), label)
    return SecretValues(tuple(sorted(pairs.items())))


def _is_top_readme(name: str, entries) -> bool:
    """A README/HOWTO at the top level, or inside the zip's single top-level folder - the
    shape a zipped project folder has ("project/README.md")."""
    parts = name.split("/")
    if len(parts) == 1:
        return name.lower() in README_NAMES
    if len(parts) != 2 or parts[1].lower() not in README_NAMES:
        return False
    tops = {e.filename.split("/")[0] for e in entries}
    return tops == {parts[0]}


# ---- the zip ------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Inspection:
    """What was checked, for the card and the CLI. No secret values in here."""

    path: Path
    size: int
    sha256: str
    files: tuple[tuple[str, int], ...]      # (entry name, uncompressed size), files only
    secret_values: int
    data: bytes = field(repr=False, compare=False)
    # Executable entries let through because the caller allowed them (a product may ship
    # a built program; a client delivery never does). Empty unless allowed and present.
    executables: tuple[str, ...] = ()


def _entry_name_problem(name: str) -> str | None:
    shown = repr(name[:120])
    if not name:
        return "an entry has an empty name"
    if _CONTROL.search(name):
        return f"entry {shown} has a control character in its name"
    if "\\" in name:
        return f"entry {shown} has a backslash in its name (a path trick on Windows)"
    if name.startswith("/"):
        return f"entry {shown} is an absolute path"
    if _DRIVE.match(name) or ":" in name:
        return f"entry {shown} has a drive letter or colon in its name"
    if ".." in name.split("/"):
        return f"entry {shown} climbs out of the folder with '..'"
    return None


def _secret_file_problem(name: str) -> str | None:
    parts = [p.lower() for p in name.rstrip("/").split("/")]
    base = parts[-1]
    shown = repr(name[:120])
    if ".git" in parts:
        return f"entry {shown} is inside a .git directory (history can hold secrets)"
    if name.endswith("/"):
        return None
    if base == ".env" or base.startswith(".env."):
        return f"entry {shown} is an .env file (it commonly holds secrets)"
    if base.startswith(("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")):
        return f"entry {shown} looks like an SSH private key file"
    if base.endswith((".pem", ".key")):
        return f"entry {shown} is a key file ({Path(base).suffix})"
    if base in {".npmrc", ".pypirc"}:
        return f"entry {shown} is a package-registry config ({base}; it commonly holds a token)"
    if base.startswith("credentials"):
        return f"entry {shown} is a credentials file"
    return None


def _archive_problem(name: str) -> str | None:
    suffix = Path(name.lower()).suffix
    if suffix in ARCHIVE_SUFFIXES:
        return (f"entry {name[:120]!r} is an archive inside the archive ({suffix}) - its "
                "contents cannot be scanned; put the files in the zip itself")
    return None


def _executable_problem(name: str, head: bytes) -> str | None:
    """Why this entry is an executable (by extension, or a Windows/ELF header), or None."""
    suffix = Path(name.lower()).suffix
    shown = repr(name[:120])
    if suffix in EXECUTABLE_SUFFIXES:
        return f"entry {shown} is an executable ({suffix}) - deliver source, not binaries"
    if head.startswith(b"\x7fELF"):
        return f"entry {shown} is an executable binary (ELF)"
    if head.startswith(b"MZ") and len(head) >= 0x40:
        offset = int.from_bytes(head[0x3C:0x40], "little")
        if head[offset:offset + 4] == b"PE\x00\x00":
            return f"entry {shown} is a Windows executable (whatever its name says)"
    return None


def scan_bytes(name: str, data: bytes, secrets: SecretValues) -> str | None:
    """Why this entry may not leave the machine, naming the secret FILE or key format -
    never the value. None if it is clean."""
    shown = repr(name[:120])
    for value, source in secrets.values:
        if value in data:
            return f"entry {shown} contains the value of the secret in {source}"
    for label, pattern in KEY_PATTERNS:
        if pattern.search(data):
            return f"entry {shown} contains something shaped like {label}"
    return None


def inspect_zip(path: Path, secrets: SecretValues, *, pinned_sha256: str | None = None,
                root: Path | None = None, max_bytes: int | None = None,
                max_uncompressed: int | None = None, allow_executables: bool = False,
                folder: str = "the deliveries folder") -> Inspection:
    """Run every check on the zip at ``path`` (optionally confined to ``root``), or raise
    DeliveryProblem with the first reason. The bytes returned are the bytes checked -
    the caller uploads exactly those.

    The defaults (None) are a client delivery's limits. A product for sale passes a larger
    ``max_bytes`` / ``max_uncompressed``, and may pass ``allow_executables``: then an
    executable entry is let through (and listed in ``Inspection.executables``) but is
    still scanned for secrets like every other entry. An archive inside the archive is
    refused either way (its contents could not be scanned)."""
    max_bytes = MAX_ZIP_BYTES if max_bytes is None else max_bytes
    max_uncompressed = MAX_UNCOMPRESSED if max_uncompressed is None else max_uncompressed
    if root is not None:
        try:
            inside = path.resolve().is_relative_to(root.resolve())
        except OSError:
            inside = False
        if not inside:
            raise DeliveryProblem(f"{path} is outside {folder}")
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise DeliveryProblem(f"no file at {path}") from None
    except OSError as error:
        raise DeliveryProblem(f"cannot read {path} ({type(error).__name__})") from error
    if not stat.S_ISREG(info.st_mode):
        raise DeliveryProblem(f"{path} is not a regular file (a link or a folder)")
    if info.st_size > max_bytes:
        raise DeliveryProblem(f"the zip is {info.st_size:,} bytes - at most "
                              f"{max_bytes:,} (too big)")
    try:
        with path.open("rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError as error:
        raise DeliveryProblem(f"cannot read {path} ({type(error).__name__})") from error
    if len(data) > max_bytes:
        raise DeliveryProblem(f"the zip is over {max_bytes:,} bytes (too big)")
    sha = hashlib.sha256(data).hexdigest()
    if pinned_sha256 is not None and sha != pinned_sha256:
        raise DeliveryProblem(f"the zip's sha256 is {sha[:16]}..., not the pinned "
                              f"{pinned_sha256[:16]}... - it is not the file you approved "
                              "(it changed, or the wrong sha was given)")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except Exception:  # noqa: BLE001 - fail closed: whatever it is, it is not a usable zip
        raise DeliveryProblem("not a zip (it does not open as one)") from None
    with archive:
        entries = archive.infolist()
        if not entries:
            raise DeliveryProblem("the zip is empty")
        if len(entries) > MAX_ENTRIES:
            raise DeliveryProblem(f"the zip has {len(entries):,} entries - at most "
                                  f"{MAX_ENTRIES:,}")
        total = sum(e.file_size for e in entries)
        if total > max_uncompressed:
            raise DeliveryProblem(f"the zip unpacks to {total:,} bytes - at most "
                                  f"{max_uncompressed:,} (a zip bomb?)")
        names: set[str] = set()
        for entry in entries:
            name = entry.filename
            # orig_filename is the name as stored: zipfile turns a backslash into "/"
            # on Windows and cuts at a NUL, which would hide both tricks from a check
            for raw_name in (entry.orig_filename, name):
                problem = _entry_name_problem(raw_name)
                if problem:
                    raise DeliveryProblem(problem)
            if name in names:
                raise DeliveryProblem(f"entry {name[:120]!r} appears twice")
            names.add(name)
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise DeliveryProblem(f"entry {name[:120]!r} is a symlink")
            if entry.flag_bits & 0x1:
                raise DeliveryProblem(f"entry {name[:120]!r} is encrypted - it cannot be "
                                      "scanned")
            if entry.file_size > RATIO_FLOOR_BYTES and (
                    entry.compress_size == 0
                    or entry.file_size / entry.compress_size > MAX_RATIO):
                raise DeliveryProblem(f"entry {name[:120]!r} compresses more than "
                                      f"{MAX_RATIO}:1 - a zip bomb?")
            problem = _secret_file_problem(name)
            if problem:
                raise DeliveryProblem(problem)
        if not any(_is_top_readme(e.filename, entries) for e in entries):
            raise DeliveryProblem("no README or HOWTO (.md or .txt) at the top level of the "
                                  "zip (or of its one top-level folder) - the client needs to "
                                  "know what they have")
        files: list[tuple[str, int]] = []
        executables: list[str] = []
        for entry in entries:
            if entry.is_dir():
                continue
            try:
                with archive.open(entry) as member:
                    content = member.read(entry.file_size + 1)
            except Exception:  # noqa: BLE001 - fail closed (a bad CRC, zlib.error, ...)
                raise DeliveryProblem(f"entry {entry.filename[:120]!r} cannot be read "
                                      "(damaged or an unsupported compression)") from None
            if len(content) > entry.file_size:
                raise DeliveryProblem(f"entry {entry.filename[:120]!r} unpacks larger than "
                                      "it declares")
            executable = _executable_problem(entry.filename, content[:4096])
            problem = (_archive_problem(entry.filename)
                       or (None if allow_executables else executable)
                       or scan_bytes(entry.filename, content, secrets)
                       or scan_bytes(entry.filename, entry.filename.encode("utf-8"),
                                     secrets))
            if problem:
                raise DeliveryProblem(problem)
            if executable:
                executables.append(entry.filename)
            files.append((entry.filename, entry.file_size))
    return Inspection(path=path, size=len(data), sha256=sha, files=tuple(files),
                      secret_values=len(secrets), data=data, executables=tuple(executables))
