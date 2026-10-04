"""Bearer tokens for the Daedalus and Melete bridges, and a check that they are required.

Daedalus (:8771 - it reaches every repo under C:\\src and runs policy "full") and Melete
(:8770, a tool executor) take a job from ANY local process unless they were started with a
token: Daedalus checks ``DAEDALUS_TOKEN`` (Tech-Support/daedalus/daedalus/config.py, falling
back to ``MELETE_TOKEN``), Melete checks ``MELETE_TOKEN`` (melete/config.py); each then wants
``Authorization: Bearer <token>`` on every job call.

- ``ensure_tokens`` makes ``daedalus-token.txt`` and ``melete-token.txt`` in the owner's
  secrets folder when they are missing: 32 random bytes, url-safe, the file readable by the
  owner only (every other entry on it removed). ``pionir.ps1`` runs it
  (``python -m pionir bridge-tokens``) before any pane starts, then starts each bridge with
  its token and hands Pionir both (``PIONIR_DAEDALUS_TOKEN`` / ``PIONIR_MELETE_TOKEN``);
  Pionir's config also reads the files when those variables are unset.
- ``daedalus_open`` asks the running Daedalus a job-changing call WITHOUT a token (a cancel
  of a job that does not exist - it changes nothing): only 401 is a locked Daedalus. The
  same call WITH Pionir's token must be answered 404 (past the token check); 401 there is a
  Daedalus started with a different token. ``pionir doctor`` fails on either.
- Melete cannot be asked the same way: its only job route validates the request body
  before it checks the token, so the only unauthenticated probe is a real job. Instead the
  start time of the process listening on its port is compared with the token file's: a
  Melete that has run since before its token was made was started without it. Doctor
  fails on that, and on a Melete it cannot tell about.
- ``pionir bridge-tokens`` reports the same for both bridges (``open_bridges``) so the
  launcher can warn loudly about a bridge that is already up without its token.
"""
from __future__ import annotations

import ctypes
import os
import secrets
import urllib.error
import urllib.parse
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
    """Leave the owner alone on the file: its whole access list replaced by one entry, the
    owner with full control, protected from inheritance.

    The list is SET, not edited: ``icacls /inheritance:r /grant:r`` only removes entries
    marked inherited, and a file can carry SYSTEM and Administrators as EXPLICIT entries -
    the process's default list when its folder passes nothing down, or copies Windows Server
    makes from a parent created without auto-inheritance (Python's ``mkdir(mode=0o700)``)."""
    if os.name != "nt":
        os.chmod(path, 0o600)
        return
    sid = _owner_sid()
    if not sid:
        raise OSError("cannot tell who the owner is")
    from ctypes import wintypes
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD)]
    adv.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    k32.LocalFree.argtypes = [ctypes.c_void_p]
    sd = ctypes.c_void_p()
    # D:P = a protected DACL (nothing inherited); FA = full access, for the owner's SID only
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            f"D:P(A;;FA;;;{sid})", 1, ctypes.byref(sd), None):
        raise OSError(f"cannot build an owner-only access list (error {ctypes.get_last_error()})")
    try:
        dacl_info = 0x00000004 | 0x80000000     # DACL_ | PROTECTED_DACL_SECURITY_INFORMATION
        if not adv.SetFileSecurityW(str(path), dacl_info, sd):
            raise OSError(f"could not restrict {path} to its owner "
                          f"(error {ctypes.get_last_error()})")
    finally:
        k32.LocalFree(sd)


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


RESTART = "Stop the stack (pionir.ps1 -Stop) and start it again with pionir.ps1."
# The launcher's bridges: token file -> (name, port). pionir.ps1 starts them on these.
BRIDGES = {"daedalus-token.txt": ("Daedalus", 8771), "melete-token.txt": ("Melete", 8770)}
PROBE = "/jobs/pionir-auth-probe/cancel"


def _cancel_probe(base_url: str, token: str | None, *, opener=None) -> int | None:
    """POST a cancel of a job that does not exist (it changes nothing) and return the HTTP
    status; None when nothing answers there."""
    open_ = opener or urllib.request.build_opener(urllib.request.ProxyHandler({})).open
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base_url.rstrip("/") + PROBE, data=b"{}", method="POST",
                                 headers=headers)
    try:
        with open_(req, timeout=5.0) as answer:
            return int(getattr(answer, "status", 200) or 200)
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError):
        return None


def daedalus_open(base_url: str, *, opener=None) -> bool | None:
    """True when the Daedalus at ``base_url`` takes a job-changing call without a token;
    False when it refuses (401); None when nothing answers there."""
    code = _cancel_probe(base_url, None, opener=opener)
    return None if code is None else code != 401


def daedalus_token_accepted(base_url: str, token: str, *, opener=None) -> bool | None:
    """Whether Daedalus takes ``token``: the same harmless cancel WITH it is answered 404 (no
    such job - it got past the token check). 401 is a token it was not started with; None
    is anything else (not running, or an answer that says neither)."""
    code = _cancel_probe(base_url, token, opener=opener)
    if code == 404:
        return True
    if code == 401:
        return False
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


def listener_started(port: int) -> float | None:
    """When the process listening on local TCP ``port`` started (epoch seconds), or None
    when nothing listens there or it cannot be told. A read of the TCP table; it sends
    nothing."""
    if os.name != "nt":
        return None
    from ctypes import wintypes
    iphlp = ctypes.WinDLL("iphlpapi")
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    size = wintypes.DWORD(0)
    iphlp.GetExtendedTcpTable(None, ctypes.byref(size), False, 2, 3, 0)  # v4, OWNER_PID_LISTENER
    buf = ctypes.create_string_buffer(size.value + 4096)
    size = wintypes.DWORD(len(buf))
    if iphlp.GetExtendedTcpTable(buf, ctypes.byref(size), False, 2, 3, 0) != 0:
        return None
    count = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0]
    rows = ctypes.cast(ctypes.addressof(buf) + 4, ctypes.POINTER(wintypes.DWORD * 6))
    pid = None
    for i in range(count):
        row = rows[i]
        local_port = ((row[2] & 0xFF) << 8) | ((row[2] >> 8) & 0xFF)
        if local_port == port:
            pid = row[5]
            break
    if not pid:
        return None
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not k32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            return None
        ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        return ticks / 10_000_000 - 11_644_473_600
    finally:
        k32.CloseHandle(handle)


def started_before_token(port: int, name: str, directory: Path | None = None, *,
                         started=listener_started) -> bool | None:
    """True when the bridge listening on ``port`` has run since before its token file was
    made - so it was started without it. None when that cannot be told (nothing listening,
    no token file, or the listener's start time unreadable)."""
    began = started(port)
    if began is None:
        return None
    try:
        made = ((directory or secrets_dir()) / name).stat().st_mtime
    except OSError:
        return None
    return began < made


def _port(url: str) -> int | None:
    try:
        return urllib.parse.urlsplit(url).port
    except ValueError:
        return None


def open_bridges(made: dict[str, str], directory: Path | None = None, *,
                 started=listener_started, listening=None) -> list[dict[str, Any]]:
    """The launcher's bridges already listening WITHOUT the token just made or kept: one
    whose token was made now while its port listens, or whose listener is older than its
    token file. pionir.ps1 prints each loudly - it never leaves one silently."""
    listening = listening or _port_listens
    out = []
    for name, (bridge, port) in BRIDGES.items():
        if made.get(name) == "made" and listening(port):
            why = "its token was just made, after it started"
        elif started_before_token(port, name, directory, started=started):
            why = "it has run since before its token was made"
        else:
            continue
        out.append({"bridge": bridge, "port": port, "why": why})
    return out


def _port_listens(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def bridge_report(settings: Any, *, opener=None, started=listener_started,
                  directory: Path | None = None) -> dict[str, Any]:
    """What doctor says about the bridges' tokens: an entry per configured bridge, with a
    ``warning`` on each one that takes jobs from anyone or refuses Pionir's token."""
    report: dict[str, Any] = {}
    if settings.daedalus_url:
        report["daedalus"] = _daedalus_entry(settings, opener)
    if settings.melete_url:
        report["melete"] = _melete_entry(settings, opener, started, directory)
    return report


def _daedalus_entry(settings: Any, opener) -> dict[str, Any]:
    entry: dict[str, Any] = {"url": settings.daedalus_url,
                             "token_held": bool(settings.daedalus_token)}
    state = daedalus_open(settings.daedalus_url, opener=opener)
    if state is None:
        entry["status"] = "not_running"
    elif state:
        entry["status"] = "open"
        entry["warning"] = ("Daedalus answered a job call WITHOUT a token: any local "
                            "process can hand it work in any of the owner's repos. "
                            + RESTART)
    elif not settings.daedalus_token:
        entry["status"] = "no_token_held"
        entry["warning"] = ("Daedalus wants a token and Pionir holds none, so it refuses "
                            "every job Pionir sends. " + RESTART)
    else:
        accepted = daedalus_token_accepted(settings.daedalus_url, settings.daedalus_token,
                                           opener=opener)
        if accepted:
            entry["status"] = "token_required"
        elif accepted is False:
            entry["status"] = "token_mismatch"
            entry["warning"] = ("Daedalus refuses Pionir's token (401): it was started with "
                                "a different one, so every job Pionir sends fails. " + RESTART)
        else:
            entry["status"] = "token_unverified"
            entry["note"] = "Daedalus locks out callers without a token; Pionir's was not confirmed"
    return entry


def _melete_entry(settings: Any, opener, started, directory: Path | None) -> dict[str, Any]:
    entry: dict[str, Any] = {"url": settings.melete_url,
                             "token_held": bool(settings.melete_token)}
    if not _answers(settings.melete_url, opener=opener):
        entry["status"] = "not_running"
        return entry
    # Melete reads and validates a whole job before it checks the token, so an
    # unauthenticated probe would have to be a real job. Instead: a Melete that has run since
    # before its token file was made cannot have been started with it.
    port = _port(settings.melete_url)
    older = (started_before_token(port, "melete-token.txt", directory, started=started)
             if port else None)
    if older:
        entry["status"] = "open"
        entry["warning"] = ("Melete has run since before its token was made, so it was "
                            "started without one and takes jobs from any local process. "
                            + RESTART)
    elif not settings.melete_token:
        entry["status"] = "no_token_held"
        entry["warning"] = ("Melete is up and Pionir holds no Melete token, so it was not "
                            "started with one by the launcher. " + RESTART)
    elif older is False:
        entry["status"] = "started_after_token"
        entry["note"] = ("Melete cannot be probed without running a job; it started after "
                         "its token was made")
    else:
        entry["status"] = "unverified"
        entry["warning"] = ("Melete is up but whether it holds its token cannot be told "
                            "(its start time or token file is unreadable). " + RESTART)
    return entry
