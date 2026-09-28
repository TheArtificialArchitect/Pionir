"""Bearer tokens for the Daedalus and Melete bridges, and a check that they are required.

Daedalus (:8771 - it reaches every repo under C:\\src and runs policy "full") and Melete
(:8770, a tool executor) take a job from ANY local process unless they were started with a
token: Daedalus checks ``DAEDALUS_TOKEN`` (Tech-Support/daedalus/daedalus/config.py, falling
back to ``MELETE_TOKEN``), Melete checks ``MELETE_TOKEN`` (melete/config.py); each then wants
``Authorization: Bearer <token>`` on every job call.

- ``ensure_tokens`` makes ``daedalus-token.txt`` and ``melete-token.txt`` in the owner's
  secrets folder when they are missing: 32 random bytes, url-safe, the file readable by the
  owner only (its inherited permissions removed). ``pionir.ps1`` runs it
  (``python -m pionir bridge-tokens``) before any pane starts, then starts each bridge with
  its token and hands Pionir both (``PIONIR_DAEDALUS_TOKEN`` / ``PIONIR_MELETE_TOKEN``);
  Pionir's config also reads the files when those variables are unset.
- ``daedalus_open`` asks the running Daedalus a job-changing call WITHOUT a token (a cancel
  of a job that does not exist - it changes nothing): only 401 is a locked Daedalus.
  ``pionir doctor`` warns when it is open. Melete cannot be asked the same way: its only
  job route validates the request body before it checks the token, so the only way to see
  an open Melete is to hand it a real job. Doctor therefore warns when Pionir holds no Melete
  token (then Melete was not started with one by the launcher) and says it cannot probe.
"""
from __future__ import annotations

import ctypes
import os
import secrets
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

TOKENS = ("daedalus-token.txt", "melete-token.txt")
MIN_LENGTH = 32


def secrets_dir() -> Path:
    return Path.home() / ".pionir" / "secrets"


def read_token(name: str, directory: Path | None = None) -> str | None:
    """A bridge token from its file, or None (missing, unreadable, or too short)."""
    try:
        text = ((directory or secrets_dir()) / name).read_text(encoding="utf-8-sig")
    except (OSError, RuntimeError):
        return None
    token = text.strip()
    return token if len(token) >= MIN_LENGTH else None


def _owner_sid() -> str | None:
    if os.name != "nt":
        return None
    from ctypes import wintypes
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                     ctypes.POINTER(wintypes.HANDLE)]
    adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    k32.LocalFree.argtypes = [ctypes.c_void_p]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    token = wintypes.HANDLE()
    if not adv.OpenProcessToken(k32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        return None
    try:
        size = wintypes.DWORD(0)
        adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buf = ctypes.create_string_buffer(size.value)
        if not adv.GetTokenInformation(token, 1, buf, size, ctypes.byref(size)):
            return None
        psid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        text = wintypes.LPWSTR()
        if not adv.ConvertSidToStringSidW(psid, ctypes.byref(text)):
            return None
        try:
            return text.value
        finally:
            k32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        k32.CloseHandle(token)


def owner_only(path: Path) -> None:
    """Remove the file's inherited permissions and leave the owner alone on it."""
    if os.name != "nt":
        os.chmod(path, 0o600)
        return
    sid = _owner_sid()
    if not sid:
        raise OSError("cannot tell who the owner is")
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    done = subprocess.run([str(system32 / "icacls.exe"), str(path), "/inheritance:r",
                           "/grant:r", f"*{sid}:F"], capture_output=True, text=True,
                          errors="replace", timeout=30, check=False)
    if done.returncode != 0:
        raise OSError(f"icacls could not restrict {path}: {done.stdout.strip()[-200:]}")


def ensure_tokens(directory: Path | None = None) -> dict[str, str]:
    """Each bridge token file, made when it is missing and never overwritten:
    ``{name: 'made' | 'kept'}``. A file that is there but holds no usable token (under 32
    characters) is not replaced - the bridges may already run with it - and raises OSError, as
    does a file that cannot be made or restricted."""
    directory = directory or secrets_dir()
    directory.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {}
    for name in TOKENS:
        path = directory / name
        if path.exists():
            if read_token(name, directory) is None:
                raise OSError(f"{path} holds no usable token (32+ characters); "
                              "fix or delete it")
            out[name] = "kept"
            continue
        tmp = path.with_name(f".{name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(secrets.token_urlsafe(32), encoding="utf-8")
            owner_only(tmp)
            # A hard link lands the finished, already-restricted file under its name and
            # fails if another launcher made it first - a token is never swapped under a
            # running bridge.
            try:
                os.link(tmp, path)
            except FileExistsError:
                out[name] = "kept"
                continue
        finally:
            tmp.unlink(missing_ok=True)
        out[name] = "made"
    return out


def daedalus_open(base_url: str, *, opener=None) -> bool | None:
    """True when the Daedalus at ``base_url`` takes a job-changing call without a token;
    False when it refuses (401); None when nothing answers there."""
    open_ = opener or urllib.request.build_opener(urllib.request.ProxyHandler({})).open
    req = urllib.request.Request(base_url.rstrip("/") + "/jobs/pionir-auth-probe/cancel",
                                 data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with open_(req, timeout=5.0):
            return True
    except urllib.error.HTTPError as exc:
        return exc.code != 401
    except (urllib.error.URLError, OSError):
        return None


def _answers(base_url: str, *, opener=None) -> bool:
    """Whether anything answers GET /health at ``base_url`` (a read; it runs nothing)."""
    open_ = opener or urllib.request.build_opener(urllib.request.ProxyHandler({})).open
    try:
        with open_(urllib.request.Request(base_url.rstrip("/") + "/health"), timeout=5.0):
            return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError):
        return False


def bridge_report(settings: Any, *, opener=None) -> dict[str, Any]:
    """What doctor says about the bridges' tokens: an entry per configured bridge, with a
    ``warning`` on each one that takes jobs from anyone."""
    report: dict[str, Any] = {}
    if settings.daedalus_url:
        state = daedalus_open(settings.daedalus_url, opener=opener)
        entry: dict[str, Any] = {"url": settings.daedalus_url,
                                 "token_held": bool(settings.daedalus_token)}
        if state is None:
            entry["status"] = "not_running"
        elif state:
            entry["status"] = "open"
            entry["warning"] = ("Daedalus answered a job call WITHOUT a token: any local "
                                "process can hand it work in any of the owner's repos. Stop "
                                "the stack (pionir.ps1 -Stop) and start it with pionir.ps1, "
                                "which starts Daedalus with DAEDALUS_TOKEN.")
        else:
            entry["status"] = "token_required"
        report["daedalus"] = entry
    if settings.melete_url:
        entry = {"url": settings.melete_url, "token_held": bool(settings.melete_token)}
        if not _answers(settings.melete_url, opener=opener):
            entry["status"] = "not_running"
        else:
            # Melete reads and validates a whole job before it checks the token, so an
            # unauthenticated probe would have to be a real job. Not probed.
            entry["status"] = "not_probed"
            entry["note"] = "Melete checks its token only after reading a whole job"
            if not settings.melete_token:
                entry["warning"] = ("Melete is up and Pionir holds no Melete token, so it "
                                    "was not started with one and takes jobs from any local "
                                    "process. Stop the stack (pionir.ps1 -Stop) and start it "
                                    "with pionir.ps1, which starts Melete with MELETE_TOKEN.")
        report["melete"] = entry
    return report
