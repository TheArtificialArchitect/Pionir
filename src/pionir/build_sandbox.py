"""The build sandbox: generated code and the build Daedalus run as ``pionir-builds``, contained.

The owner's containment decision for the Builds division (crew/builds): nothing Daedalus
writes, and nothing it runs, runs as the owner. ``tools\\setup-build-sandbox.ps1`` (run ONCE
by the owner, as administrator) makes a standard local user ``pionir-builds`` whose processes:

- run at LOW integrity (the dedicated interpreter carries a Low label, so everything started
  from it is Low): they can write only what is labelled Low - the sandbox workspace
  (``C:\\src\\daedalus-work``, which also grants the user Modify) - and nothing else;
- are DENIED read and write on the rest of ``C:\\src`` and on every other top-level data
  folder any signed-in user could write, and cannot read the owner's profile (so none of
  ``~/.pionir``'s secrets, nor another repo's ``.env``);
- use a DEDICATED interpreter and a copy of Daedalus's code under ``%ProgramData%\\PionirBuilds``
  (read-only to them), with firewall rules that block their outbound traffic except loopback,
  and loopback except the Ollama gate below.

Its password is random, known to nobody, and stored only DPAPI-encrypted for the owner's own
account (``~/.pionir/secrets/pionir-builds.cred``), so Pionir - running as the owner - can
start a process as that user (``CreateProcessWithLogonW``) and nothing else can. The script
records what it set up in ``%ProgramData%\\PionirBuilds\\setup.json``; ``load_setup`` checks
that record (the account, the interpreter and its Low label, the Daedalus copy, the Low
sandbox, the credential) and says ``SETUP_HINT`` when anything is missing - then nothing is
built and nothing is run.

Every process here starts SUSPENDED on a private window station and desktop, is put in a job
object (kill-on-close: if Pionir dies, the whole tree dies; no breakaway; every UI
restriction; a cap on processes, memory and CPU time) and only then resumed. Its output goes
to FILES, never pipes (a child that fills a pipe nobody reads hangs for ever), and a timeout
or ``kill`` terminates the whole job - every descendant with it. Before anything starts and
after anything ends, every process the sandbox user still has is killed (``reap``) - one that
got out of its job some other way included - and nothing starts while any survives.

The build Daedalus reaches Ollama only through ``OllamaGate``: chat, generate and embed for
its one model, and the read-only model listing - never pull, delete, create, copy or push.

LOOPBACK IS OPEN (owner decision, 2026-09-28: "I'm okay with it reaching programs"). Windows
Firewall does not filter loopback for ordinary programs, so a process of the sandbox user can
connect to any 127.0.0.1 port - Ollama itself included, not only the gate. What holds instead:
every service there that could do harm must demand a token the sandbox user cannot read, and
``preflight`` proves, AS that user, before any generated code runs, that it is at Low
integrity and can read none of the owner's secrets. Night builds also refuse to run while
``PIONIR_AUTH_COMPAT`` is on (a tokenless caller is still served by Pionir then).
"""
from __future__ import annotations

import ctypes
import http.client
import json
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from ctypes import wintypes
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

USER = "pionir-builds"
SETUP_HINT = r"not configured: run tools\setup-build-sandbox.ps1 once, as administrator"
RECORD_NAME = "setup.json"
CREDENTIAL_NAME = "pionir-builds.cred"
BUILD_PORT = 8772
GATE_PORT = 8773
RECORD_VERSION = 3
LOW_RID = 0x1000                    # SECURITY_MANDATORY_LOW_RID
STATE_DIR_NAME = ".daedalus-state"
RUNS_DIR_NAME = ".runs"
_LOW_LABEL = re.compile(r"(?im)^.*Mandatory Label\\Low Mandatory Level:.*$")


def default_record_path() -> Path:
    base = os.environ.get("ProgramData") or os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
    return Path(base) / "PionirBuilds" / RECORD_NAME


def default_sandbox_root() -> str:
    from pionir.adapters.daedalus import DEFAULT_SANDBOX_ROOT
    return DEFAULT_SANDBOX_ROOT


def default_credential_path() -> Path:
    return default_secrets_dir() / CREDENTIAL_NAME


def default_secrets_dir() -> Path:
    return Path.home() / ".pionir" / "secrets"


# ---- Win32 ------------------------------------------------------------------------------------
_WIN = os.name == "nt"
_WINSTA_SWAP = threading.Lock()


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", wintypes.BOOL)]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]


class _STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", _STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


class _BASIC(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class _IO(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount",
                                               "OtherOperationCount", "ReadTransferCount",
                                               "WriteTransferCount", "OtherTransferCount")]


class _EXTENDED(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BASIC), ("IoInfo", _IO),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


if _WIN:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _adv = ctypes.WinDLL("advapi32", use_last_error=True)
    _u32 = ctypes.WinDLL("user32", use_last_error=True)
    _crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    _H, _D, _B, _P = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, ctypes.c_void_p
    for _fn, _res, _args in (
            (_k32.CreateJobObjectW, _H, [_P, wintypes.LPCWSTR]),
            (_k32.SetInformationJobObject, _B, [_H, ctypes.c_int, _P, _D]),
            (_k32.QueryInformationJobObject, _B, [_H, ctypes.c_int, _P, _D, _P]),
            (_k32.AssignProcessToJobObject, _B, [_H, _H]),
            (_k32.TerminateJobObject, _B, [_H, wintypes.UINT]),
            (_k32.TerminateProcess, _B, [_H, wintypes.UINT]),
            (_k32.CloseHandle, _B, [_H]),
            (_k32.WaitForSingleObject, _D, [_H, _D]),
            (_k32.GetExitCodeProcess, _B, [_H, ctypes.POINTER(_D)]),
            (_k32.ResumeThread, _D, [_H]),
            (_k32.GetCurrentProcess, _H, []),
            (_k32.LocalFree, _P, [_P]),
            (_k32.InitializeProcThreadAttributeList, _B, [_P, _D, _D, ctypes.POINTER(ctypes.c_size_t)]),
            (_k32.UpdateProcThreadAttribute, _B, [_P, _D, ctypes.c_size_t, _P, ctypes.c_size_t, _P, _P]),
            (_k32.DeleteProcThreadAttributeList, None, [_P]),
            (_k32.CreateProcessW, _B, [wintypes.LPCWSTR, wintypes.LPWSTR, _P, _P, _B, _D, _P,
                                       wintypes.LPCWSTR, _P, _P]),
            (_adv.CreateProcessWithLogonW, _B,
             [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, _D, wintypes.LPCWSTR,
              wintypes.LPWSTR, _D, _P, wintypes.LPCWSTR, _P, _P]),
            (_adv.LookupAccountNameW, _B,
             [wintypes.LPCWSTR, wintypes.LPCWSTR, _P, ctypes.POINTER(_D),
              wintypes.LPWSTR, ctypes.POINTER(_D), ctypes.POINTER(_D)]),
            (_adv.ConvertSidToStringSidW, _B, [_P, ctypes.POINTER(wintypes.LPWSTR)]),
            (_adv.ConvertStringSecurityDescriptorToSecurityDescriptorW, _B,
             [wintypes.LPCWSTR, _D, ctypes.POINTER(_P), _P]),
            (_adv.OpenProcessToken, _B, [_H, _D, ctypes.POINTER(_H)]),
            (_adv.GetTokenInformation, _B, [_H, ctypes.c_int, _P, _D, ctypes.POINTER(_D)]),
            (_u32.CreateWindowStationW, _H, [wintypes.LPCWSTR, _D, _D, _P]),
            (_u32.OpenWindowStationW, _H, [wintypes.LPCWSTR, _B, _D]),
            (_adv.ConvertStringSidToSidW, _B, [wintypes.LPCWSTR, ctypes.POINTER(_P)]),
            (_adv.GetSecurityInfo, _D, [_H, ctypes.c_int, _D, _P, _P, ctypes.POINTER(_P), _P,
                                        ctypes.POINTER(_P)]),
            (_adv.SetEntriesInAclW, _D, [_D, _P, _P, ctypes.POINTER(_P)]),
            (_adv.SetSecurityInfo, _D, [_H, ctypes.c_int, _D, _P, _P, _P, _P]),
            (_u32.CloseWindowStation, _B, [_H]),
            (_u32.GetProcessWindowStation, _H, []),
            (_u32.SetProcessWindowStation, _B, [_H]),
            (_u32.CreateDesktopW, _H, [wintypes.LPCWSTR, wintypes.LPCWSTR, _P, _D, _D, _P]),
            (_u32.CloseDesktop, _B, [_H]),
            (_crypt.CryptProtectData, _B, [_P, wintypes.LPCWSTR, _P, _P, _P, _D, _P]),
            (_crypt.CryptUnprotectData, _B, [_P, _P, _P, _P, _P, _D, _P])):
        _fn.restype, _fn.argtypes = _res, _args

JobObjectBasicUIRestrictions = 4
JobObjectExtendedLimitInformation = 9
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_JOB_TIME = 0x00000004
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_UILIMIT_ALL = 0x000000FF
CREATE_SUSPENDED = 0x00000004
CREATE_NEW_CONSOLE = 0x00000010
CREATE_UNICODE_ENVIRONMENT = 0x00000400
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
LOGON_WITH_PROFILE = 0x00000001
STARTF_USESHOWWINDOW = 0x00000001
STARTF_USESTDHANDLES = 0x00000100
TOKEN_QUERY = 0x0008
WINSTA_ALL_ACCESS = 0x0000037F
READ_CONTROL = 0x00020000
WRITE_DAC = 0x00040000
SE_WINDOW_OBJECT = 7
DACL_SECURITY_INFORMATION = 0x4
GRANT_ACCESS = 1
TRUSTEE_IS_SID = 0
TRUSTEE_IS_USER = 1
GENERIC_ALL = 0x10000000
WAIT_TIMEOUT = 0x102
INFINITE = 0xFFFFFFFF


class SandboxError(RuntimeError):
    pass


def _check(ok, what: str):
    if not ok:
        err = ctypes.get_last_error()
        raise SandboxError(f"{what} failed (Windows error {err}: {ctypes.FormatError(err)})")
    return ok


# ---- DPAPI ------------------------------------------------------------------------------------
def _blob(data: bytes) -> _BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def dpapi_protect(data: bytes) -> bytes:
    """CryptProtectData for the current user (what PowerShell's ConvertFrom-SecureString
    does with no key). Used by tests to make a credential like the setup script's."""
    if not _WIN:
        raise SandboxError("DPAPI needs Windows")
    src, out = _blob(data), _BLOB()
    _check(_crypt.CryptProtectData(ctypes.byref(src), None, None, None, None, 0,
                                   ctypes.byref(out)), "CryptProtectData")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _k32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def dpapi_unprotect(data: bytes) -> bytes:
    if not _WIN:
        raise SandboxError("DPAPI needs Windows")
    src, out = _blob(data), _BLOB()
    _check(_crypt.CryptUnprotectData(ctypes.byref(src), None, None, None, None, 0,
                                     ctypes.byref(out)), "CryptUnprotectData")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _k32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def read_password(path: Path) -> str:
    """The sandbox user's password from the setup script's credential file: the hex of a
    DPAPI blob of the UTF-16 password (``ConvertFrom-SecureString``, no key), decryptable
    only by the owner's own account on this machine."""
    text = Path(path).read_text(encoding="utf-8-sig").strip()
    try:
        raw = bytes.fromhex(text)
    except ValueError as exc:
        raise SandboxError(f"{path} is not a DPAPI credential") from exc
    password = dpapi_unprotect(raw).decode("utf-16-le")
    if len(password) < 16:
        raise SandboxError(f"{path} holds no usable password")
    return password


def lookup_sid(name: str) -> str | None:
    """The account's SID string, or None when there is no such account."""
    if not _WIN:
        return None
    sid = ctypes.create_string_buffer(256)
    sid_size = wintypes.DWORD(256)
    domain = ctypes.create_unicode_buffer(256)
    domain_size = wintypes.DWORD(256)
    use = wintypes.DWORD()
    if not _adv.LookupAccountNameW(None, name, sid, ctypes.byref(sid_size), domain,
                                   ctypes.byref(domain_size), ctypes.byref(use)):
        return None
    text = wintypes.LPWSTR()
    if not _adv.ConvertSidToStringSidW(sid, ctypes.byref(text)):
        return None
    try:
        return text.value
    finally:
        _k32.LocalFree(ctypes.cast(text, ctypes.c_void_p))


def is_low_labelled(path) -> bool:
    """True when ``path`` carries a Low mandatory integrity label (read with icacls; a read,
    it changes nothing)."""
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    try:
        out = subprocess.run([str(system32 / "icacls.exe"), str(path)], capture_output=True,
                             text=True, errors="replace", timeout=30, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(_LOW_LABEL.search(out or ""))


# ---- the setup record ------------------------------------------------------------------------
@dataclass(frozen=True)
class SandboxSetup:
    """What the setup script made, as checked now."""

    user: str
    sid: str
    python: Path
    sandbox_root: Path
    daedalus_src: Path
    credential: Path

    @property
    def python_dir(self) -> Path:
        return self.python.parent

    @property
    def state_dir(self) -> Path:
        return self.sandbox_root / STATE_DIR_NAME

    @property
    def runs_dir(self) -> Path:
        return self.sandbox_root / RUNS_DIR_NAME

    def logon(self) -> tuple[str, str, str]:
        return (self.user, ".", read_password(self.credential))

    def reap(self) -> None:
        """Kill every process this user has; raise if any survives."""
        ensure_no_strays(self)

    def preflight(self) -> dict:
        """Prove, as this user, that it runs Low and reads none of the owner's secrets;
        raise SandboxError otherwise. Run before any generated code runs."""
        return require_preflight(self.python,
                                 work=self.runs_dir / f"preflight-{secrets.token_hex(4)}",
                                 logon=self.logon(), sid=self.sid,
                                 secrets_dir=default_secrets_dir())


def _inside(path: Path, folder: Path) -> bool:
    try:
        return os.path.normcase(os.path.abspath(path)).startswith(
            os.path.normcase(os.path.abspath(folder)) + os.sep)
    except (OSError, ValueError):
        return False


def load_setup(sandbox_root, *, record: Path | None = None, credential: Path | None = None,
               lookup=lookup_sid, read=read_password,
               low=is_low_labelled) -> tuple[SandboxSetup | None, str | None]:
    """``(setup, None)`` when the sandbox user is set up and usable, else ``(None, why)``
    (always starting with SETUP_HINT). Reads only; never creates anything."""
    record = Path(record) if record is not None else default_record_path()
    try:
        doc = json.loads(record.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None, f"{SETUP_HINT} (no {record})"
    except (OSError, ValueError) as exc:
        return None, f"{SETUP_HINT} ({record} is unreadable: {type(exc).__name__})"
    if not isinstance(doc, dict) or doc.get("user") != USER:
        return None, f"{SETUP_HINT} ({record} does not describe the {USER} user)"
    if doc.get("version") != RECORD_VERSION:
        return None, f"{SETUP_HINT} ({record} is from an older setup; run it again)"
    if doc.get("low_integrity") is not True or doc.get("secrets_readable") != 0:
        # written only by a setup whose own preflight, AS the user, proved both
        return None, (f"{SETUP_HINT} ({record} does not show that the {USER} user was proven "
                      "to run at Low integrity and to read none of your secrets)")
    sid = lookup(USER)
    if not sid or sid != doc.get("sid"):
        return None, f"{SETUP_HINT} (the {USER} account is missing or not the one set up)"
    install = record.parent
    python = Path(str(doc.get("python") or ""))
    if not python.is_file() or not _inside(python, install):
        return None, f"{SETUP_HINT} (the dedicated interpreter {python} is missing)"
    if not low(python):
        return None, (f"{SETUP_HINT} (the dedicated interpreter {python} does not carry a "
                      "Low integrity label)")
    root = Path(str(doc.get("sandbox_root") or ""))
    if os.path.normcase(os.path.abspath(root)) != os.path.normcase(
            os.path.abspath(str(sandbox_root))):
        return None, (f"{SETUP_HINT} (it was set up for {root}, but the sandbox is "
                      f"{sandbox_root})")
    if not root.is_dir():
        return None, f"{SETUP_HINT} (the sandbox {root} is missing)"
    if not low(root):
        return None, f"{SETUP_HINT} (the sandbox {root} does not carry a Low integrity label)"
    src = Path(str(doc.get("daedalus_src") or ""))
    if not (src / "daedalus" / "server.py").is_file() or not _inside(src, install):
        return None, (f"{SETUP_HINT} (Daedalus's code is not copied into {install} - the "
                      f"sandbox user may not read C:\\src)")
    cred = Path(credential) if credential is not None else Path(
        str(doc.get("credential") or default_credential_path()))
    try:
        read(cred)
    except (OSError, SandboxError, UnicodeDecodeError) as exc:
        return None, f"{SETUP_HINT} (the {USER} credential cannot be read: {exc})"
    return SandboxSetup(USER, sid, python, root, src, cred), None

# ---- a process in a job object, on a desktop of its own ----------------------------------------
@dataclass(frozen=True)
class JobLimits:
    active_processes: int = 8
    job_memory_mb: int = 2048
    cpu_seconds: float = 600.0          # user-mode CPU time for the whole job; 0 = none


def minimal_env(*, work: Path, path_dirs, extra: dict | None = None) -> dict:
    """An environment with nothing of the owner's in it: the few system variables Windows
    needs, a PATH of exactly ``path_dirs``, and HOME/USERPROFILE/APPDATA/LOCALAPPDATA/TEMP
    all inside ``work``."""
    system = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT") or r"C:\Windows"
    work = Path(work)
    env = {"SystemRoot": system, "SYSTEMROOT": system, "WINDIR": system,
           "SystemDrive": os.environ.get("SystemDrive", "C:"),
           "COMSPEC": str(Path(system) / "System32" / "cmd.exe"),
           "PATHEXT": ".COM;.EXE;.BAT;.CMD",
           "PATH": os.pathsep.join(str(p) for p in path_dirs),
           "HOME": str(work / "home"), "USERPROFILE": str(work / "home"),
           "APPDATA": str(work / "home" / "AppData" / "Roaming"),
           "LOCALAPPDATA": str(work / "home" / "AppData" / "Local"),
           "TEMP": str(work / "tmp"), "TMP": str(work / "tmp"),
           "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONNOUSERSITE": "1", "NO_COLOR": "1", "CI": "1"}
    for key in ("APPDATA", "LOCALAPPDATA", "TEMP"):
        Path(env[key]).mkdir(parents=True, exist_ok=True)
    env.update(extra or {})
    return env


def _env_block(env: dict) -> ctypes.Array:
    items = sorted(env.items(), key=lambda kv: kv[0].upper())
    text = "".join(f"{k}={v}\0" for k, v in items) + "\0"
    return ctypes.create_unicode_buffer(text, len(text))


def current_sid() -> str:
    """This process's user SID (the owner's, in production)."""
    token = wintypes.HANDLE()
    _check(_adv.OpenProcessToken(_k32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)),
           "OpenProcessToken")
    try:
        size = wintypes.DWORD(0)
        _adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))       # TokenUser
        buf = ctypes.create_string_buffer(size.value)
        _check(_adv.GetTokenInformation(token, 1, buf, size, ctypes.byref(size)),
               "GetTokenInformation")
        psid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        text = wintypes.LPWSTR()
        _check(_adv.ConvertSidToStringSidW(psid, ctypes.byref(text)), "ConvertSidToStringSid")
        try:
            return text.value
        finally:
            _k32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        _k32.CloseHandle(token)


class _TRUSTEE(ctypes.Structure):
    _fields_ = [("pMultipleTrustee", ctypes.c_void_p), ("MultipleTrusteeOperation", ctypes.c_int),
                ("TrusteeForm", ctypes.c_int), ("TrusteeType", ctypes.c_int),
                ("ptstrName", ctypes.c_void_p)]


class _EXPLICIT_ACCESS(ctypes.Structure):
    _fields_ = [("grfAccessPermissions", wintypes.DWORD), ("grfAccessMode", ctypes.c_int),
                ("grfInheritance", wintypes.DWORD), ("Trustee", _TRUSTEE)]


def _grant_window_station(winsta, sid: str) -> None:
    """Let ``sid`` use this window station - what the Secondary Logon service does itself
    when it is handed no desktop; here the desktop stays the private one."""
    psid = ctypes.c_void_p()
    _check(_adv.ConvertStringSidToSidW(sid, ctypes.byref(psid)), "ConvertStringSidToSid")
    old_dacl, sd, new_dacl = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    try:
        err = _adv.GetSecurityInfo(winsta, SE_WINDOW_OBJECT, DACL_SECURITY_INFORMATION, None,
                                   None, ctypes.byref(old_dacl), None, ctypes.byref(sd))
        if err:
            raise SandboxError(f"GetSecurityInfo failed (Windows error {err})")
        ea = _EXPLICIT_ACCESS(WINSTA_ALL_ACCESS, GRANT_ACCESS, 0,
                              _TRUSTEE(None, 0, TRUSTEE_IS_SID, TRUSTEE_IS_USER, psid))
        err = _adv.SetEntriesInAclW(1, ctypes.byref(ea), old_dacl, ctypes.byref(new_dacl))
        if err:
            raise SandboxError(f"SetEntriesInAcl failed (Windows error {err})")
        err = _adv.SetSecurityInfo(winsta, SE_WINDOW_OBJECT, DACL_SECURITY_INFORMATION, None,
                                   None, new_dacl, None)
        if err:
            raise SandboxError(f"SetSecurityInfo failed (Windows error {err})")
    finally:
        for ptr in (new_dacl, sd, psid):
            if ptr:
                _k32.LocalFree(ptr)


class PrivateDesktop:
    """A desktop of its own for one contained process tree - on a window station of its own
    when Windows allows that, else on the interactive one (the Secondary Logon service would
    grant the sandbox user that window station anyway) - so nothing in it can see, message,
    hook or screenshot the owner's windows; with the job's UI restrictions, no clipboard or
    global atoms either. Only SYSTEM, the owner and ``sid`` may use the desktop, and it
    carries a LOW integrity label."""

    def __init__(self, sid: str) -> None:
        self.name = f"PionirBuilds-{secrets.token_hex(6)}"
        me = current_sid()
        sddl = (f"D:P(A;;GA;;;SY)(A;;GA;;;{me})(A;;GA;;;{sid})S:(ML;;NW;;;LW)")
        sd = ctypes.c_void_p()
        _check(_adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(sd), None), "ConvertStringSecurityDescriptor")
        sa = _SECURITY_ATTRIBUTES(ctypes.sizeof(_SECURITY_ATTRIBUTES), sd, False)
        self._winsta = self._desk = None
        self._own_winsta = False
        try:
            winsta = _u32.CreateWindowStationW(self.name, 0, WINSTA_ALL_ACCESS, ctypes.byref(sa))
            if winsta:
                self._winsta, self._own_winsta, self.station = winsta, True, self.name
            else:
                self._winsta = _check(_u32.OpenWindowStationW(
                    "WinSta0", False, WINSTA_ALL_ACCESS | READ_CONTROL | WRITE_DAC),
                    "OpenWindowStation(WinSta0)")
                self.station = "WinSta0"
                if sid != me:
                    _grant_window_station(self._winsta, sid)
            with _WINSTA_SWAP:
                old = _u32.GetProcessWindowStation()
                _check(_u32.SetProcessWindowStation(self._winsta), "SetProcessWindowStation")
                try:
                    self._desk = _check(_u32.CreateDesktopW(self.name, None, None, 0,
                                                            GENERIC_ALL, ctypes.byref(sa)),
                                        "CreateDesktop")
                finally:
                    _u32.SetProcessWindowStation(old)
        except BaseException:
            self.close()
            raise
        finally:
            _k32.LocalFree(sd)

    @property
    def path(self) -> str:
        return f"{self.station}\\{self.name}"

    def close(self) -> None:
        if self._desk:
            _u32.CloseDesktop(self._desk)
            self._desk = None
        if self._winsta:
            _u32.CloseWindowStation(self._winsta)
            self._winsta = None


class Contained:
    """One process tree in a kill-on-close job object, on its own desktop."""

    def __init__(self, job, process, pid: int, desktop: PrivateDesktop | None = None) -> None:
        self._job = job
        self._process = process
        self.pid = pid
        self._desktop = desktop
        self._closed = False
        self._lock = threading.Lock()

    def alive(self) -> bool:
        return _k32.WaitForSingleObject(self._process, 0) == WAIT_TIMEOUT

    def wait(self, timeout: float | None) -> int | None:
        """The root's exit code, or None if it is still running after ``timeout``."""
        ms = INFINITE if timeout is None else max(0, int(timeout * 1000))
        if _k32.WaitForSingleObject(self._process, ms) == WAIT_TIMEOUT:
            return None
        code = wintypes.DWORD()
        _k32.GetExitCodeProcess(self._process, ctypes.byref(code))
        return code.value

    def kill(self) -> None:
        """Terminate the whole job: the root and every descendant."""
        with self._lock:
            if not self._closed:
                _k32.TerminateJobObject(self._job, 1)

    def close(self) -> None:
        """Kill everything that is left and let go of the job (kill-on-close)."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            _k32.TerminateJobObject(self._job, 1)
            _k32.WaitForSingleObject(self._process, 5000)
            _k32.CloseHandle(self._process)
            _k32.CloseHandle(self._job)
            if self._desktop is not None:
                self._desktop.close()


def _make_job(limits: JobLimits):
    job = _check(_k32.CreateJobObjectW(None, None), "CreateJobObject")
    info = _EXTENDED()
    flags = (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
             | JOB_OBJECT_LIMIT_ACTIVE_PROCESS | JOB_OBJECT_LIMIT_JOB_MEMORY)
    info.BasicLimitInformation.ActiveProcessLimit = max(1, int(limits.active_processes))
    info.JobMemoryLimit = int(limits.job_memory_mb) * 1024 * 1024
    if limits.cpu_seconds and limits.cpu_seconds > 0:
        flags |= JOB_OBJECT_LIMIT_JOB_TIME
        info.BasicLimitInformation.PerJobUserTimeLimit = int(limits.cpu_seconds * 10_000_000)
    # never BREAKAWAY_OK / SILENT_BREAKAWAY_OK: nothing leaves the job
    info.BasicLimitInformation.LimitFlags = flags
    ui = wintypes.DWORD(JOB_OBJECT_UILIMIT_ALL)
    try:
        _check(_k32.SetInformationJobObject(job, JobObjectExtendedLimitInformation,
                                            ctypes.byref(info), ctypes.sizeof(info)),
               "SetInformationJobObject")
        # no clipboard, no global atoms, no other process's windows, no display or system
        # settings, no desktop switching, no ExitWindows
        _check(_k32.SetInformationJobObject(job, JobObjectBasicUIRestrictions,
                                            ctypes.byref(ui), ctypes.sizeof(ui)),
               "SetInformationJobObject(UI)")
    except SandboxError:
        _k32.CloseHandle(job)
        raise
    return job


def job_ui_restrictions(proc: Contained) -> int:
    ui = wintypes.DWORD()
    _check(_k32.QueryInformationJobObject(proc._job, JobObjectBasicUIRestrictions,     # noqa: SLF001
                                          ctypes.byref(ui), ctypes.sizeof(ui), None),
           "QueryInformationJobObject")
    return ui.value


def spawn(argv: list, *, cwd, env: dict, stdout: Path, stderr: Path, limits: JobLimits,
          logon: tuple[str, str, str] | None, sid: str | None = None) -> Contained:
    """Start ``argv`` SUSPENDED on a private desktop, put it in a new kill-on-close job
    (with every UI restriction), then resume it. With ``logon`` (user, domain, password) it
    runs as that user (CreateProcessWithLogonW); without, as this user (tests only -
    production always passes the sandbox user). ``argv[0]`` must be an absolute path.
    Output goes to the two files; stdin is NUL. If it cannot be put in the job it is killed
    and this raises: nothing ever runs uncontained."""
    if not _WIN:
        raise SandboxError("contained runs need Windows")
    if not os.path.isabs(str(argv[0])):
        raise SandboxError(f"{argv[0]!r} is not an absolute path")
    import msvcrt
    job = _make_job(limits)
    desktop = None
    handles = []
    try:
        desktop = PrivateDesktop(sid or current_sid())
        out = open(stdout, "wb")                         # noqa: SIM115 - closed below
        err = open(stderr, "wb")                         # noqa: SIM115
        nul = open(os.devnull, "rb")                     # noqa: SIM115
        handles = [out, err, nul]
        std = [msvcrt.get_osfhandle(f.fileno()) for f in (nul, out, err)]
        for h in std:
            os.set_handle_inheritable(h, True)
        cmdline = ctypes.create_unicode_buffer(
            subprocess.list2cmdline([str(a) for a in argv]))
        desk = ctypes.create_unicode_buffer(desktop.path)
        pi = _PROCESS_INFORMATION()
        if logon is None:
            six = _STARTUPINFOEXW()
            six.StartupInfo.cb = ctypes.sizeof(six)
            si = six.StartupInfo
        else:
            si = _STARTUPINFOW()
            si.cb = ctypes.sizeof(si)
        si.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        si.lpDesktop = ctypes.cast(desk, wintypes.LPWSTR)
        si.hStdInput, si.hStdOutput, si.hStdError = std
        if logon is None:
            # only the three standard handles are inherited, whatever else is open
            size = ctypes.c_size_t(0)
            _k32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
            attrs = ctypes.create_string_buffer(size.value)
            _check(_k32.InitializeProcThreadAttributeList(attrs, 1, 0, ctypes.byref(size)),
                   "InitializeProcThreadAttributeList")
            handle_list = (wintypes.HANDLE * 3)(*std)
            try:
                _check(_k32.UpdateProcThreadAttribute(
                    attrs, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handle_list,
                    ctypes.sizeof(handle_list), None, None), "UpdateProcThreadAttribute")
                six.lpAttributeList = ctypes.cast(attrs, ctypes.c_void_p)
                _check(_k32.CreateProcessW(
                    str(argv[0]), cmdline, None, None, True,
                    CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | CREATE_NEW_CONSOLE
                    | EXTENDED_STARTUPINFO_PRESENT,
                    _env_block(env), str(cwd), ctypes.byref(six), ctypes.byref(pi)),
                    "CreateProcess")
            finally:
                _k32.DeleteProcThreadAttributeList(attrs)
        else:
            user, domain, password = logon
            _check(_adv.CreateProcessWithLogonW(
                user, domain, password, LOGON_WITH_PROFILE, str(argv[0]), cmdline,
                CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | CREATE_NEW_CONSOLE,
                _env_block(env), str(cwd), ctypes.byref(si), ctypes.byref(pi)),
                "CreateProcessWithLogonW")
        if not _k32.AssignProcessToJobObject(job, pi.hProcess):
            error = ctypes.get_last_error()
            _k32.TerminateProcess(pi.hProcess, 1)
            _k32.CloseHandle(pi.hThread)
            _k32.CloseHandle(pi.hProcess)
            raise SandboxError(f"the process could not be contained (AssignProcessToJobObject "
                               f"error {error}); it was killed before it ran")
        _k32.ResumeThread(pi.hThread)
        _k32.CloseHandle(pi.hThread)
        return Contained(job, pi.hProcess, pi.dwProcessId, desktop)
    except BaseException:
        _k32.TerminateJobObject(job, 1)
        _k32.CloseHandle(job)
        if desktop is not None:
            desktop.close()
        raise
    finally:
        for f in handles:
            f.close()


@dataclass
class RunResult:
    returncode: int | None
    timed_out: bool
    stdout: str
    stderr: str


def run(argv: list, *, cwd, env: dict, out_dir: Path, timeout: float, limits: JobLimits,
        logon: tuple[str, str, str] | None, spawner=None, sid: str | None = None) -> RunResult:
    """Run to completion (or ``timeout``) contained; the whole tree is killed either way,
    so nothing it started outlives the call. Output is read back from its files."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stdout, stderr = out_dir / "stdout.txt", out_dir / "stderr.txt"
    kwargs = {"sid": sid} if sid is not None else {}
    proc = (spawner or spawn)(argv, cwd=cwd, env=env, stdout=stdout, stderr=stderr,
                              limits=limits, logon=logon, **kwargs)
    try:
        code = proc.wait(timeout)
        timed_out = code is None
    finally:
        proc.close()                    # the tree dies here, finished or not

    def tail(path: Path) -> str:
        try:
            data = path.read_bytes()
        except OSError:
            return ""
        return data[-20_000:].decode("utf-8", "replace")
    return RunResult(None if timed_out else code, timed_out, tail(stdout), tail(stderr))


# ---- the sandbox user's stray processes ----------------------------------------------------------
def reap(setup: SandboxSetup, *, runner=None) -> list:
    """Kill EVERY process the sandbox user has - including one that left its job some other
    way (WMI, COM, a scheduled task) - then list what is left. Run AS that user (a user may
    always end its own processes), with Windows' own taskkill and tasklist. Returns the image
    names still running; empty is the only clean answer."""
    run_ = runner or run
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    work = setup.runs_dir / f"reap-{secrets.token_hex(4)}"
    try:
        work.mkdir(parents=True, exist_ok=True)
        env = minimal_env(work=work / "env", path_dirs=[system32])
        logon = setup.logon()
        limits = JobLimits(active_processes=4, job_memory_mb=256, cpu_seconds=60)
        run_([str(system32 / "taskkill.exe"), "/F", "/FI", f"USERNAME eq {setup.user}",
              "/FI", "IMAGENAME ne taskkill.exe"],
             cwd=work, env=env, out_dir=work / "kill", timeout=60, limits=limits,
             logon=logon, sid=setup.sid)
        listed = run_([str(system32 / "tasklist.exe"), "/FI", f"USERNAME eq {setup.user}",
                       "/FO", "CSV", "/NH"], cwd=work, env=env, out_dir=work / "list",
                      timeout=60, limits=limits, logon=logon, sid=setup.sid)
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)
    if listed.timed_out or listed.returncode not in (0,):
        return ["(the check itself failed)"]
    names = []
    for line in listed.stdout.splitlines():
        cell = line.split('","')[0].strip().strip('"')
        if cell and not cell.startswith("INFO:"):
            names.append(cell)
    # the check's own tasklist.exe and its console host are expected; anything else - and
    # a second console host - survived the kill
    left = [n for n in names if n.lower() != "tasklist.exe"]
    hosts = [n for n in left if n.lower() == "conhost.exe"]
    return [n for n in left if n.lower() != "conhost.exe"] + hosts[1:]


def ensure_no_strays(setup: SandboxSetup, *, runner=None) -> None:
    """Nothing starts while anything of the sandbox user's is still running."""
    left = reap(setup, runner=runner)
    if left:
        raise SandboxError(f"processes of {setup.user} survived being killed "
                           f"({', '.join(left[:5])}); nothing is started until they are gone")


# ---- the preflight: what a process of the sandbox user really is ---------------------------------
# Run with the dedicated interpreter, AS the sandbox user, through ``spawn`` - the very path a
# build and our test runs take. The child reports its OWN token's integrity level (nothing on
# our side has to open another user's token) and tries to read each of the owner's secrets.
PREFLIGHT_CODE = r"""
import ctypes, json, os, sys
from ctypes import wintypes
out = {"integrity": None, "readable": [], "listable": []}
try:
    k = ctypes.WinDLL("kernel32")
    a = ctypes.WinDLL("advapi32")
    k.GetCurrentProcess.restype = wintypes.HANDLE
    a.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                   ctypes.POINTER(wintypes.HANDLE)]
    a.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                      wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    a.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    a.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    a.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    a.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
    h = wintypes.HANDLE()
    if a.OpenProcessToken(k.GetCurrentProcess(), 8, ctypes.byref(h)):
        n = wintypes.DWORD(0)
        a.GetTokenInformation(h, 25, None, 0, ctypes.byref(n))
        buf = ctypes.create_string_buffer(max(n.value, 1))
        if a.GetTokenInformation(h, 25, buf, n, ctypes.byref(n)):
            sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
            count = a.GetSidSubAuthorityCount(sid)[0]
            out["integrity"] = int(a.GetSidSubAuthority(sid, count - 1)[0])
except Exception as exc:
    out["error"] = type(exc).__name__
ask = json.loads(sys.argv[1])
for p in ask.get("files", []):
    try:
        with open(p, "rb") as f:
            f.read(1)
        out["readable"].append(p)
    except OSError:
        pass
for p in ask.get("dirs", []):
    try:
        os.listdir(p)
        out["listable"].append(p)
    except OSError:
        pass
print("PREFLIGHT " + json.dumps(out))
"""
_PREFLIGHT_LINE = re.compile(r"(?m)^PREFLIGHT (\{.*\})\s*$")


def _secret_targets(secrets_dir: Path) -> dict:
    """Every file in the owner's secrets folder (as the owner sees it), and the folders a
    contained process must not be able to list."""
    secrets_dir = Path(secrets_dir)
    files = sorted(str(p) for p in secrets_dir.rglob("*") if p.is_file()) \
        if secrets_dir.is_dir() else []
    return {"files": files,
            "dirs": [str(secrets_dir), str(secrets_dir.parent), str(Path.home())]}


def preflight(python, *, work: Path, logon, sid, secrets_dir: Path, runner=None) -> dict:
    """Run the preflight as ``logon``'s user with ``python`` (contained, like everything
    else). ``{"integrity": rid|None, "readable": [...], "listable": [...], "files": n}``,
    or ``{"failed": why}`` when it did not run or said nothing we could read."""
    import shutil
    work = Path(work)
    targets = _secret_targets(secrets_dir)
    try:
        work.mkdir(parents=True, exist_ok=True)
        system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
        env = minimal_env(work=work / "env", path_dirs=[Path(python).parent, system32])
        done = (runner or run)([str(python), "-I", "-S", "-c", PREFLIGHT_CODE,
                                json.dumps(targets)],
                               cwd=work, env=env, out_dir=work / "out", timeout=60,
                               limits=JobLimits(active_processes=2, job_memory_mb=256,
                                                cpu_seconds=30),
                               logon=logon, sid=sid)
    except (SandboxError, OSError) as exc:
        return {"failed": f"it could not start ({exc})"}
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if done.timed_out:
        return {"failed": "it did not finish within 60 s"}
    found = _PREFLIGHT_LINE.search(done.stdout or "")
    if done.returncode != 0 or not found:
        return {"failed": f"it exited {done.returncode} without a report "
                          f"({(done.stderr or '').strip()[-300:]})"}
    try:
        doc = json.loads(found.group(1))
    except ValueError:
        return {"failed": "its report is not JSON"}
    if not isinstance(doc, dict):
        return {"failed": "its report is not an object"}
    doc["files"] = len(targets["files"])
    return doc


def preflight_problems(doc: dict) -> list:
    """Why a preflight report does not prove the containment - empty only when it does."""
    if doc.get("failed"):
        return [f"the preflight did not run: {doc['failed']}"]
    problems = []
    rid = doc.get("integrity")
    if rid is None:
        problems.append("it could not read its own integrity level, so it is not proven "
                        "to run at Low integrity")
    elif rid != LOW_RID:
        problems.append(f"it runs at integrity 0x{int(rid):04x}, not Low (0x{LOW_RID:04x}): "
                        "the Low label on the dedicated python.exe did not take effect")
    readable = list(doc.get("readable") or [])
    if readable:
        problems.append(f"it can read {len(readable)} of your secrets: "
                        + ", ".join(Path(p).name for p in readable[:6]))
    listable = list(doc.get("listable") or [])
    if listable:
        problems.append("it can list " + ", ".join(listable[:3]))
    return problems


def require_preflight(python, *, work: Path, logon, sid, secrets_dir: Path,
                      runner=None) -> dict:
    """``preflight``, and raise SandboxError unless it proves the containment."""
    doc = preflight(python, work=work, logon=logon, sid=sid, secrets_dir=secrets_dir,
                    runner=runner)
    problems = preflight_problems(doc)
    if problems:
        raise SandboxError(f"the {USER} user is not contained - " + "; ".join(problems)
                           + ". Nothing of a build runs until it is.")
    return doc


# ---- the Ollama gate -----------------------------------------------------------------------------
GATE_READS = frozenset({"/api/tags", "/api/ps", "/api/version"})
GATE_MODEL_CALLS = frozenset({"/api/chat", "/api/generate", "/api/embed", "/api/embeddings",
                              "/api/show"})
GATE_MAX_BODY = 8_000_000


class OllamaGate:
    """The build Daedalus's only way to Ollama: a loopback proxy that passes the model
    listing and chat / generate / embed / show for ITS model - and refuses everything else
    (``/api/pull``, ``/api/delete``, ``/api/create``, ``/api/copy``, ``/api/push``, blobs,
    another model). Runs in Pionir, as the owner, only while a build runs."""

    def __init__(self, model: str, *, upstream: str = "http://127.0.0.1:11434",
                 port: int = GATE_PORT) -> None:
        self.model = model
        parsed = urllib.parse.urlparse(upstream)
        if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise SandboxError("the Ollama gate forwards to loopback only")
        self.upstream = (parsed.hostname, parsed.port or 11434)
        self.port = port
        self.refused: list = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def allowed(self, method: str, path: str, body: bytes) -> str | None:
        """Why this request may not pass, or None."""
        path = path.split("?", 1)[0]
        if method == "GET":
            return None if path in GATE_READS else f"GET {path} is not allowed"
        if method != "POST" or path not in GATE_MODEL_CALLS:
            return f"{method} {path} is not allowed"
        try:
            doc = json.loads(body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return "the body is not JSON"
        model = doc.get("model") or doc.get("name") if isinstance(doc, dict) else None
        if model != self.model:
            return f"only {self.model} may be used, not {str(model)[:60]!r}"
        return None

    def start(self) -> None:
        gate = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args) -> None:
                pass

            def _refuse(self, why: str) -> None:
                gate.refused.append(why)
                data = json.dumps({"error": f"refused by the build gate: {why}"}).encode()
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                if length > GATE_MAX_BODY:
                    return self._refuse("the request is too large")
                body = self.rfile.read(length) if length else b""
                why = gate.allowed(method, self.path, body)
                if why:
                    return self._refuse(why)
                conn = http.client.HTTPConnection(*gate.upstream, timeout=900)
                try:
                    conn.request(method, self.path.split("?", 1)[0], body=body or None,
                                 headers={"Content-Type": "application/json"})
                    resp = conn.getresponse()
                    self.send_response(resp.status)
                    self.send_header("Content-Type",
                                     resp.getheader("Content-Type") or "application/json")
                    self.end_headers()
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except OSError as exc:
                    gate.refused.append(f"upstream: {exc}")
                finally:
                    conn.close()
                return None

            def do_GET(self) -> None:          # noqa: N802 - the http.server name
                self._handle("GET")

            def do_POST(self) -> None:         # noqa: N802
                self._handle("POST")

            def do_DELETE(self) -> None:       # noqa: N802
                self._refuse(f"DELETE {self.path} is not allowed")

            def do_PUT(self) -> None:          # noqa: N802
                self._refuse(f"PUT {self.path} is not allowed")

            def do_HEAD(self) -> None:         # noqa: N802
                self._refuse(f"HEAD {self.path} is not allowed")

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="pionir-ollama-gate", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()


# ---- the build Daedalus --------------------------------------------------------------------------
class BuildDaedalus:
    """A SECOND Daedalus, only for the Builds division, started per build as ``pionir-builds``
    on :8772 with a fresh bearer token, its roots restricted to the sandbox, its state and
    its temporary folder (where Daedalus makes its worktrees) inside the sandbox, Ollama
    reached only through the gate, in a kill-on-close job on a private desktop - and stopped
    (the whole tree killed, and every stray of the user's) when the build ends, or at its
    ``not_after`` plus a grace whatever happens. Daedalus's own code is only read (a copy
    under %ProgramData%), never changed: everything is set through its environment."""

    def __init__(self, setup: SandboxSetup, *, port: int = BUILD_PORT,
                 model: str = "qwen3-coder:30b", ollama: str = "http://127.0.0.1:11434",
                 spawner=None, opener=None, clock=time.time, sleep=time.sleep,
                 start_timeout: float = 90.0, kill_grace: float = 180.0,
                 git_dir: Path | None = None, gate=None) -> None:
        self.setup = setup
        self.port = port
        self.model = model
        self.ollama = ollama
        self._spawn = spawner or spawn
        self._open = opener or urllib.request.build_opener(urllib.request.ProxyHandler({})).open
        self._clock = clock
        self._sleep = sleep
        self.start_timeout = start_timeout
        self.kill_grace = kill_grace
        self.git_dir = git_dir
        self.gate = gate if gate is not None else OllamaGate(model, upstream=ollama)
        self.proc: Contained | None = None
        self.token: str | None = None
        self._timer: threading.Timer | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _get(self, path: str, timeout: float = 5.0):
        req = urllib.request.Request(self.base_url + path, method="GET")
        with self._open(req, timeout=timeout) as resp:
            return json.loads(resp.read(1_000_000).decode("utf-8"))

    def _answers(self) -> bool:
        try:
            self._get("/health", timeout=2.0)
            return True
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def _git_path(self) -> list:
        if self.git_dir is not None:
            return [self.git_dir]
        import shutil
        found = shutil.which("git")
        return [Path(found).parent] if found else []

    def environment(self, token: str) -> dict:
        state = self.setup.state_dir
        gitconfig = state / "gitconfig"
        state.mkdir(parents=True, exist_ok=True)
        root = str(self.setup.sandbox_root).replace("\\", "/")
        gitconfig.write_text(
            "[safe]\n\tdirectory = " + root + "/*\n"
            "[core]\n\thooksPath = " + os.devnull + "\n\tfsmonitor = false\n"
            "[user]\n\tname = builds\n\temail = builds@localhost.invalid\n",
            encoding="utf-8")
        system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
        path = [self.setup.python_dir, self.setup.python_dir / "Scripts", *self._git_path(),
                system32, system32 / "WindowsPowerShell" / "v1.0"]
        return minimal_env(work=state, path_dirs=path, extra={
            "DAEDALUS_PORT": str(self.port), "DAEDALUS_HOST": "127.0.0.1",
            "DAEDALUS_TOKEN": token, "DAEDALUS_POLICY": "full",
            "DAEDALUS_WORKSPACE_ROOTS": str(self.setup.sandbox_root),
            "DAEDALUS_STATE_DIR": str(state), "DAEDALUS_MODEL": self.model,
            "DAEDALUS_NUM_CTX": "32768", "DAEDALUS_MAX_STEPS": "32", "DAEDALUS_REPAIRS": "4",
            "DAEDALUS_TEMPERATURE": "0.35", "DAEDALUS_THINK": "1",
            "DAEDALUS_OFFLINE_CACHE": str(state / "offline"),
            "DAEDALUS_SKILLS_DIR": str(Path(self.setup.daedalus_src) / "skills"),
            "OLLAMA_HOST": self.gate.url,
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(gitconfig),
            "GIT_CEILING_DIRECTORIES": str(self.setup.sandbox_root),
            "GIT_TERMINAL_PROMPT": "0"})

    def start(self, *, not_after: float) -> str:
        """Start it and wait until it answers as ITS configuration (roots = the sandbox,
        state inside it) and accepts ITS token. Returns the token. Raises SandboxError."""
        if self.proc is not None:
            raise SandboxError("the build Daedalus is already running")
        # nothing of the sandbox user's may be running before a new build starts
        self.setup.reap()
        # ... and it is proven, as that user, to run Low and to read none of the secrets
        self.setup.preflight()
        if self._answers():
            raise SandboxError(f"something already answers on 127.0.0.1:{self.port}; the "
                               "build Daedalus is started only on a free port")
        token = secrets.token_urlsafe(32)
        state = self.setup.state_dir
        logs = state / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        env = self.environment(token)
        self.gate.start()
        try:
            self.proc = self._spawn(
                [str(self.setup.python), "-m", "daedalus.server"],
                cwd=self.setup.daedalus_src, env=env, stdout=logs / "daedalus.out.txt",
                stderr=logs / "daedalus.err.txt",
                limits=JobLimits(active_processes=32, job_memory_mb=8192, cpu_seconds=0),
                logon=self.setup.logon(), sid=self.setup.sid)
        except BaseException:
            self.gate.stop()
            raise
        delay = max(1.0, not_after - self._clock() + self.kill_grace)
        self._timer = threading.Timer(delay, self.stop)
        self._timer.daemon = True
        self._timer.start()
        try:
            self._wait_ready(token)
        except BaseException:
            self.stop()
            raise
        self.token = token
        return token

    def _wait_ready(self, token: str) -> None:
        deadline = self._clock() + self.start_timeout
        health = None
        while self._clock() < deadline:
            if self.proc is None or not self.proc.alive():
                raise SandboxError("the build Daedalus exited while starting (see "
                                   f"{self.setup.state_dir / 'logs'})")
            try:
                health = self._get("/health")
                break
            except (urllib.error.URLError, OSError, ValueError):
                self._sleep(1.0)
        if not isinstance(health, dict) or health.get("ok") is not True:
            raise SandboxError("the build Daedalus did not come up")
        roots = [os.path.normcase(str(r)) for r in health.get("roots") or []]
        if roots != [os.path.normcase(str(self.setup.sandbox_root.resolve()))]:
            raise SandboxError(f"the build Daedalus reaches {health.get('roots')}, not only "
                               "the sandbox; stopped")
        state = os.path.normcase(str(health.get("state_dir") or ""))
        if not state.startswith(os.path.normcase(str(self.setup.sandbox_root)) + os.sep):
            raise SandboxError("the build Daedalus keeps its state outside the sandbox")
        if str(health.get("policy")) != "full":
            raise SandboxError(f"the build Daedalus runs policy {health.get('policy')!r}")
        # an authenticated no-op: a server that does not hold OUR token answers 401
        req = urllib.request.Request(self.base_url + "/jobs/pionir-probe/cancel", data=b"{}",
                                     method="POST",
                                     headers={"Authorization": f"Bearer {token}",
                                              "Content-Type": "application/json"})
        try:
            with self._open(req, timeout=5.0):
                pass
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise SandboxError("the server on the build port does not hold our token") \
                    from exc
        except (urllib.error.URLError, OSError) as exc:
            raise SandboxError(f"the build Daedalus did not answer: {exc}") from exc
        wrong = urllib.request.Request(self.base_url + "/jobs/pionir-probe/cancel",
                                       data=b"{}", method="POST",
                                       headers={"Authorization": "Bearer not-the-token",
                                                "Content-Type": "application/json"})
        try:
            with self._open(wrong, timeout=5.0):
                raise SandboxError("the build Daedalus accepts a wrong token; stopped")
        except urllib.error.HTTPError as exc:
            if exc.code != 401:
                raise SandboxError("the build Daedalus does not require its token; stopped") \
                    from exc

    def alive(self) -> bool:
        return self.proc is not None and self.proc.alive()

    def stop(self) -> None:
        """Kill the whole tree, close the gate, and kill any stray of the user's
        (idempotent). A stray that survives makes the NEXT start refuse."""
        timer, self._timer = self._timer, None
        if timer is not None and timer is not threading.current_thread():
            timer.cancel()
        proc, self.proc = self.proc, None
        self.token = None
        if proc is not None:
            proc.close()
            self.gate.stop()
            try:
                self.setup.reap()
            except (SandboxError, OSError):
                pass            # reported, and refused, by the next start


def info(setup: SandboxSetup | None) -> dict[str, Any]:
    if setup is None:
        return {"configured": False}
    return {"configured": True, "user": setup.user, "python": str(setup.python),
            "sandbox_root": str(setup.sandbox_root)}


def _main(argv=None) -> int:
    """``python -m pionir.build_sandbox preflight ...``: the setup script's last proof, run
    through ``spawn`` exactly as a build is (CreateProcessWithLogonW, the job, the desktop).
    Prints the report as JSON; exit 0 only when it proves the containment. It can be run
    again later WITHOUT elevation, which is how Pionir itself runs it before every build."""
    import argparse
    parser = argparse.ArgumentParser(prog="python -m pionir.build_sandbox")
    sub = parser.add_subparsers(dest="command", required=True)
    pre = sub.add_parser("preflight")
    pre.add_argument("--python", required=True)
    pre.add_argument("--sandbox", required=True)
    pre.add_argument("--credential", default=str(default_credential_path()))
    pre.add_argument("--secrets", default=str(default_secrets_dir()))
    args = parser.parse_args(argv)
    sid = lookup_sid(USER)
    if not sid:
        print(json.dumps({"ok": False, "problems": [f"there is no {USER} account"]}))
        return 1
    try:
        logon = (USER, ".", read_password(Path(args.credential)))
    except (OSError, SandboxError, UnicodeDecodeError) as exc:
        print(json.dumps({"ok": False, "problems": [f"the credential: {exc}"]}))
        return 1
    work = Path(args.sandbox) / RUNS_DIR_NAME / f"preflight-{secrets.token_hex(4)}"
    doc = preflight(args.python, work=work, logon=logon, sid=sid,
                    secrets_dir=Path(args.secrets))
    problems = preflight_problems(doc)
    print(json.dumps({"ok": not problems, "problems": problems, "report": doc}))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(_main())
