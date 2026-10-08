"""What the Etsy and Printify adapters share: the transport, the failure shapes, the staged
file checks and the small JSON stores (a credentials file, a ledger).

Nothing here decides what may run - that is each adapter's job, behind Pionir's gate.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import secrets
import struct
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pionir import atomic
from pionir.adapters.deliveries import DeliveryProblem, load_secrets, scan_bytes

_log = logging.getLogger(__name__)

Opener = Callable[..., Any]
MAX_RESPONSE_BYTES = 4_000_000
_SHA256 = re.compile(r"[0-9a-f]{64}")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class Failure(Exception):
    """One step's plain answer (already scrubbed), carried out of the step to execute()."""

    def __init__(self, output: dict[str, Any]) -> None:
        super().__init__(output.get("error"))
        self.output = output


def refused(why: str, **extra: Any) -> Failure:
    return Failure({"ok": False, "refused": why, "error": why, **extra})


def unavailable(why: str, **extra: Any) -> Failure:
    return Failure({"ok": False, "unavailable": why, "error": why, **extra})


def check_url(url: str, what: str) -> None:
    parsed = urllib.parse.urlparse(url)
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    # Credentials only ever travel over TLS, or to this machine (a test's fake).
    if not (parsed.scheme == "https" or (parsed.scheme == "http" and loopback)):
        raise ValueError(f"the {what} URL must be https: (or http: on loopback)")
    if not parsed.hostname:
        raise ValueError(f"the {what} URL needs a host")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"the {what} URL cannot carry credentials or query data")


def http(opener: Opener, method: str, url: str, data: bytes | None,
         headers: Mapping[str, str], timeout: float, agent: str) -> tuple[int, Any]:
    """(HTTP status, parsed JSON or None). Status 0 means nothing answered."""
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={"User-Agent": agent, **headers})
    try:
        # timeout by keyword: OpenerDirector.open(url, data=None, timeout=...) takes a
        # positional second argument as the POST body.
        with opener(request, timeout=timeout) as response:
            return int(getattr(response, "status", 200)), read_json(response)
    except urllib.error.HTTPError as error:
        return error.code, read_json(error)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        # the exception text is not carried: it could echo the request
        _log.warning("%s: %s %s did not answer (%s)", agent, method,
                     urllib.parse.urlsplit(url).path, type(error).__name__)
        return 0, None


def read_json(response: Any) -> Any:
    try:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    except (OSError, ValueError):
        return None
    if not raw or len(raw) > MAX_RESPONSE_BYTES:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def multipart(fields: Mapping[str, str], files: list[tuple[str, str, str, bytes]]
              ) -> tuple[bytes, str]:
    """A multipart/form-data body: ``fields`` as text parts, ``files`` as
    (field, file name, content type, bytes). Returns (body, Content-Type header)."""
    boundary = "pionir-" + secrets.token_hex(16)
    out = io.BytesIO()
    for key, value in fields.items():
        out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n"
                  .encode())
        out.write(str(value).encode("utf-8") + b"\r\n")
    for field, name, ctype, data in files:
        out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; "
                  f"filename=\"{name}\"\r\nContent-Type: {ctype}\r\n\r\n".encode())
        out.write(data + b"\r\n")
    out.write(f"--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


def scrub(text: str, values: list[str]) -> str:
    for v in values:
        if v:
            text = text.replace(v, "<redacted>")
            q = urllib.parse.quote_plus(v)
            if q != v:
                text = text.replace(q, "<redacted>")
    return text


# ---- small JSON stores ----------------------------------------------------------------------
def read_json_file(path: Path) -> dict | None:
    """The JSON object in ``path``; None when there is no file; Failure (unavailable) when it
    cannot be read or is not an object - a ledger that cannot be read could hide a twin."""
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise unavailable(f"{path} cannot be read ({type(error).__name__})") from None
    try:
        doc = json.loads(raw)
    except ValueError:
        doc = None
    if not isinstance(doc, dict):
        raise unavailable(f"{path} is not a JSON object; fix it before going on")
    return doc


def write_json_file(path: Path, doc: Mapping[str, Any]) -> None:
    """Replace the file atomically: a crash leaves the old one or the new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(doc, handle, indent=2, sort_keys=True)
        atomic.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def restrict_to_owner(path: Path) -> bool:
    """Make a credentials file readable by this user only again, as the setup script left
    it (a replaced file takes its folder's permissions). Windows: ``icacls /inheritance:r
    /grant:r <user>:(R,W)``; elsewhere ``chmod 600``. False (and a warning) if it could not:
    the file is still written - losing a rotated refresh token is the worse outcome."""
    try:
        if os.name == "nt":
            import subprocess
            user = os.environ.get("USERNAME") or ""
            domain = os.environ.get("USERDOMAIN") or ""
            who = f"{domain}\\{user}" if domain else user
            if not user:
                raise OSError("no USERNAME in the environment")
            done = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r",
                                   f"{who}:(R,W)"], capture_output=True, timeout=30,
                                  check=False)
            if done.returncode != 0:
                raise OSError(f"icacls exited {done.returncode}")
        else:
            os.chmod(path, 0o600)
    except (OSError, ValueError) as error:
        _log.warning("could not restrict %s to its owner (%s)", path, error)
        return False
    return True


_LOCKS: dict[Path, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def lock_for(path: Path) -> threading.Lock:
    """One action at a time per ledger: two approvals of one item must not both run."""
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(Path(path).resolve(), threading.Lock())


# ---- staged files --------------------------------------------------------------------------
@dataclass(frozen=True)
class StagedFile:
    name: str
    data: bytes
    sha256: str
    content_type: str
    width: int = 0
    height: int = 0

    @property
    def size(self) -> int:
        return len(self.data)


def png_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or not data.startswith(_PNG_SIGNATURE) or data[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", data[16:24])


def secrets_for(secrets_dir: Path | None, extra_files: tuple = ()) -> Any:
    return load_secrets(secrets_dir, extra_files, None, ())


def stage_file(folder: Path, root: Path, name: str, pinned: str, *, max_bytes: int,
               secrets: Any) -> StagedFile:
    """Read one staged file - inside ``folder``, inside ``root``, its SHA-256 the pinned
    one - and check it by its type. DeliveryProblem with the reason if anything is off.
    The bytes returned are the bytes checked (and the bytes uploaded)."""
    if not isinstance(pinned, str) or not _SHA256.fullmatch(pinned):
        raise DeliveryProblem(f"{name}: its sha256 must be 64 lower-case hex characters")
    path = folder / name
    try:
        real = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise DeliveryProblem(f"{name}: not found in {folder}") from None
    if root.resolve() not in real.parents or real.parent != folder.resolve():
        raise DeliveryProblem(f"{name}: is not inside the stage folder")
    if not real.is_file():
        raise DeliveryProblem(f"{name}: is not a file")
    size = real.stat().st_size
    if size == 0 or size > max_bytes:
        raise DeliveryProblem(f"{name}: {size:,} bytes (1 to {max_bytes:,})")
    data = real.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    if sha != pinned:
        raise DeliveryProblem(f"{name}: changed since it was checked (sha256 differs) - "
                              "do not approve a file you have not seen")
    lower = name.lower()
    if lower.endswith(".png"):
        dims = png_size(data)
        if dims is None:
            raise DeliveryProblem(f"{name}: is not a PNG")
        problem = scan_bytes(name, data, secrets)
        if problem:
            raise DeliveryProblem(problem)
        return StagedFile(name, data, sha, "image/png", *dims)
    if lower.endswith(".pdf"):
        if not data.startswith(b"%PDF-"):
            raise DeliveryProblem(f"{name}: is not a PDF")
        if re.search(rb"/(?:JavaScript|JS|Launch|EmbeddedFile|OpenAction)\b", data):
            raise DeliveryProblem(f"{name}: a PDF with scripts, launch actions or embedded "
                                  "files is refused")
        problem = scan_bytes(name, data, secrets)
        if problem:
            raise DeliveryProblem(problem)
        return StagedFile(name, data, sha, "application/pdf")
    if lower.endswith(".xlsx"):
        _check_xlsx(name, data, secrets)
        return StagedFile(name, data, sha, "application/vnd.openxmlformats-officedocument."
                                           "spreadsheetml.sheet")
    raise DeliveryProblem(f"{name}: only .xlsx, .pdf and .png files are staged")


def _check_xlsx(name: str, data: bytes, secrets: Any) -> None:
    """A real workbook, with no macros, no external links, no embedded objects, nothing
    oversized, and none of the owner's secrets in any part."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise DeliveryProblem(f"{name}: is not an .xlsx (not a zip)") from None
    names = archive.namelist()
    if "xl/workbook.xml" not in names or "[Content_Types].xml" not in names:
        raise DeliveryProblem(f"{name}: is not an .xlsx workbook")
    total = 0
    for info in archive.infolist():
        low = info.filename.lower()
        if any(bad in low for bad in ("vbaproject", "externallink", "embeddings/",
                                      "activex", ".bin")):
            raise DeliveryProblem(f"{name}: carries macros, external links or embedded "
                                  f"objects ({info.filename[:80]})")
        total += info.file_size
        if total > 50_000_000:
            raise DeliveryProblem(f"{name}: unpacks to more than 50 MB")
        problem = scan_bytes(f"{name}:{info.filename}", archive.read(info), secrets)
        if problem:
            raise DeliveryProblem(problem)
