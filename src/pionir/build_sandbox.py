"""The build sandbox: generated code and the build Daedalus run as ``pionir-builds``, contained.

The owner's containment decision for the Builds division (crew/builds): nothing Daedalus
writes, and nothing it runs, runs as the owner. ``tools\\setup-build-sandbox.ps1`` (run ONCE
by the owner, as administrator) makes a standard local user ``pionir-builds`` that:

- can write ONLY the sandbox workspace (``C:\\src\\daedalus-work``) - an explicit deny on the
  rest of ``C:\\src``, whose inherited ACL otherwise lets every signed-in user modify it;
- cannot read the owner's profile (so none of ``~/.pionir``'s secrets);
- runs a DEDICATED interpreter (a copy of Python under ``%ProgramData%\\PionirBuilds``, read-only
  to it) that an outbound firewall rule blocks from everything but loopback (Ollama on
  :11434 still answers), with a second rule scoped to the user itself.

Its password is random, known to nobody, and stored only DPAPI-encrypted for the owner's own
account (``~/.pionir/secrets/pionir-builds.cred``), so Pionir - running as the owner - can
start a process as that user (``CreateProcessWithLogonW``) and nothing else can. The script
records what it set up in ``%ProgramData%\\PionirBuilds\\setup.json``; ``load_setup`` checks
that record (and the account, the interpreter, the credential) and says ``SETUP_HINT`` when
anything is missing - then nothing is built and nothing is run.

Every process here starts SUSPENDED, is put in a job object (kill-on-close: if Pionir dies,
the whole tree dies; no breakaway; a cap on processes, memory and CPU time) and only then
resumed. Its output goes to FILES, never pipes (a child that fills a pipe nobody reads hangs
for ever), and a timeout or ``kill`` terminates the whole job - every descendant with it.
"""
from __future__ import annotations

import ctypes
import json
import os
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

USER = "pionir-builds"
SETUP_HINT = r"not configured: run tools\setup-build-sandbox.ps1 once, as administrator"
RECORD_NAME = "setup.json"
CREDENTIAL_NAME = "pionir-builds.cred"
BUILD_PORT = 8772
STATE_DIR_NAME = ".daedalus-state"
RUNS_DIR_NAME = ".runs"


def default_record_path() -> Path:
    base = os.environ.get("ProgramData") or os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
    return Path(base) / "PionirBuilds" / RECORD_NAME


def default_sandbox_root() -> str:
    from pionir.adapters.daedalus import DEFAULT_SANDBOX_ROOT
    return DEFAULT_SANDBOX_ROOT


def default_credential_path() -> Path:
    return Path.home() / ".pionir" / "secrets" / CREDENTIAL_NAME


# ---- Win32 ------------------------------------------------------------------------------------
_WIN = os.name == "nt"
if _WIN:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _adv = ctypes.WinDLL("advapi32", use_last_error=True)
    _crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")
    _H, _D, _B = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL
    for _fn, _res, _args in (
            (_k32.CreateJobObjectW, _H, [ctypes.c_void_p, wintypes.LPCWSTR]),
            (_k32.SetInformationJobObject, _B, [_H, ctypes.c_int, ctypes.c_void_p, _D]),
            (_k32.AssignProcessToJobObject, _B, [_H, _H]),
            (_k32.TerminateJobObject, _B, [_H, wintypes.UINT]),
            (_k32.TerminateProcess, _B, [_H, wintypes.UINT]),
            (_k32.CloseHandle, _B, [_H]),
            (_k32.WaitForSingleObject, _D, [_H, _D]),
            (_k32.GetExitCodeProcess, _B, [_H, ctypes.POINTER(_D)]),
            (_k32.ResumeThread, _D, [_H]),
            (_k32.GetCurrentProcess, _H, []),
            (_k32.DuplicateHandle, _B, [_H, _H, _H, ctypes.POINTER(_H), _D, _B, _D]),
            (_k32.LocalFree, ctypes.c_void_p, [ctypes.c_void_p]),
            (_ntdll.NtResumeProcess, ctypes.c_long, [_H]),
            (_adv.CreateProcessWithLogonW, _B,
             [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, _D, wintypes.LPCWSTR,
              wintypes.LPWSTR, _D, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p,
              ctypes.c_void_p]),
            (_adv.LookupAccountNameW, _B,
             [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(_D),
              wintypes.LPWSTR, ctypes.POINTER(_D), ctypes.POINTER(_D)]),
            (_adv.ConvertSidToStringSidW, _B, [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]),
            (_crypt.CryptProtectData, _B, [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p,
                                           ctypes.c_void_p, ctypes.c_void_p, _D,
                                           ctypes.c_void_p]),
            (_crypt.CryptUnprotectData, _B, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                             ctypes.c_void_p, ctypes.c_void_p, _D,
                                             ctypes.c_void_p])):
        _fn.restype, _fn.argtypes = _res, _args

JobObjectExtendedLimitInformation = 9
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_JOB_TIME = 0x00000004
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
CREATE_SUSPENDED = 0x00000004
CREATE_NEW_CONSOLE = 0x00000010
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_NO_WINDOW = 0x08000000
LOGON_WITH_PROFILE = 0x00000001
STARTF_USESHOWWINDOW = 0x00000001
STARTF_USESTDHANDLES = 0x00000100
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102
INFINITE = 0xFFFFFFFF
STILL_ACTIVE = 259


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


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


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


def load_setup(sandbox_root, *, record: Path | None = None, credential: Path | None = None,
               lookup=lookup_sid, read=read_password) -> tuple[SandboxSetup | None, str | None]:
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
    sid = lookup(USER)
    if not sid or sid != doc.get("sid"):
        return None, f"{SETUP_HINT} (the {USER} account is missing or not the one set up)"
    python = Path(str(doc.get("python") or ""))
    if not python.is_file():
        return None, f"{SETUP_HINT} (the dedicated interpreter {python} is missing)"
    root = Path(str(doc.get("sandbox_root") or ""))
    if os.path.normcase(os.path.abspath(root)) != os.path.normcase(
            os.path.abspath(str(sandbox_root))):
        return None, (f"{SETUP_HINT} (it was set up for {root}, but the sandbox is "
                      f"{sandbox_root})")
    if not root.is_dir():
        return None, f"{SETUP_HINT} (the sandbox {root} is missing)"
    src = Path(str(doc.get("daedalus_src") or ""))
    if not (src / "daedalus" / "server.py").is_file():
        return None, f"{SETUP_HINT} (Daedalus's code is not at {src})"
    cred = Path(credential) if credential is not None else Path(
        str(doc.get("credential") or default_credential_path()))
    try:
        read(cred)
    except (OSError, SandboxError, UnicodeDecodeError) as exc:
        return None, f"{SETUP_HINT} (the {USER} credential cannot be read: {exc})"
    return SandboxSetup(USER, sid, python, root, src, cred), None


# ---- a process in a job object ------------------------------------------------------------------
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


class Contained:
    """One process tree in a kill-on-close job object."""

    def __init__(self, job, process, pid: int) -> None:
        self._job = job
        self._process = process
        self.pid = pid
        self._closed = False
        self._lock = threading.Lock()

    def poll(self) -> int | None:
        code = wintypes.DWORD()
        _check(_k32.GetExitCodeProcess(self._process, ctypes.byref(code)), "GetExitCodeProcess")
        return None if code.value == STILL_ACTIVE and self.alive() else code.value

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
            _k32.CloseHandle(self._process)
            _k32.CloseHandle(self._job)


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
    try:
        _check(_k32.SetInformationJobObject(job, JobObjectExtendedLimitInformation,
                                            ctypes.byref(info), ctypes.sizeof(info)),
               "SetInformationJobObject")
    except SandboxError:
        _k32.CloseHandle(job)
        raise
    return job


def spawn(argv: list, *, cwd, env: dict, stdout: Path, stderr: Path, limits: JobLimits,
          logon: tuple[str, str, str] | None) -> Contained:
    """Start ``argv`` SUSPENDED, put it in a new kill-on-close job, then resume it. With
    ``logon`` (user, domain, password) it runs as that user (CreateProcessWithLogonW);
    without, as this user (tests only - production always passes the sandbox user). Output
    goes to the two files; stdin is NUL. If it cannot be put in the job it is killed and
    this raises: nothing ever runs uncontained."""
    if not _WIN:
        raise SandboxError("contained runs need Windows")
    job = _make_job(limits)
    handles = []
    try:
        out = open(stdout, "wb")                         # noqa: SIM115 - closed below
        err = open(stderr, "wb")                         # noqa: SIM115
        nul = open(os.devnull, "rb")                     # noqa: SIM115
        handles = [out, err, nul]
        cmdline = subprocess.list2cmdline([str(a) for a in argv])
        if logon is None:
            proc = subprocess.Popen([str(a) for a in argv], cwd=str(cwd), env=env,
                                    stdin=nul, stdout=out, stderr=err, close_fds=True,
                                    creationflags=CREATE_SUSPENDED | CREATE_NO_WINDOW)
            dup = wintypes.HANDLE()
            me = _k32.GetCurrentProcess()
            _check(_k32.DuplicateHandle(me, int(proc._handle), me,   # noqa: SLF001
                                        ctypes.byref(dup), 0, False, 2), "DuplicateHandle")
            hproc, pid, resume = dup.value, proc.pid, ("process", dup.value)
        else:
            user, domain, password = logon
            si = _STARTUPINFOW()
            si.cb = ctypes.sizeof(si)
            si.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW
            si.wShowWindow = 0
            import msvcrt
            for f in handles:
                os.set_handle_inheritable(msvcrt.get_osfhandle(f.fileno()), True)
            si.hStdInput = msvcrt.get_osfhandle(nul.fileno())
            si.hStdOutput = msvcrt.get_osfhandle(out.fileno())
            si.hStdError = msvcrt.get_osfhandle(err.fileno())
            pi = _PROCESS_INFORMATION()
            line = ctypes.create_unicode_buffer(cmdline)
            _check(_adv.CreateProcessWithLogonW(
                user, domain, password, LOGON_WITH_PROFILE, str(argv[0]), line,
                CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | CREATE_NEW_CONSOLE,
                _env_block(env), str(cwd), ctypes.byref(si), ctypes.byref(pi)),
                "CreateProcessWithLogonW")
            hproc, pid, resume = pi.hProcess, pi.dwProcessId, ("thread", pi.hThread)
        if not _k32.AssignProcessToJobObject(job, hproc):
            error = ctypes.get_last_error()
            _k32.TerminateProcess(hproc, 1)
            raise SandboxError(f"the process could not be contained (AssignProcessToJobObject "
                               f"error {error}); it was killed before it ran")
        if resume[0] == "thread":
            _k32.ResumeThread(resume[1])
            _k32.CloseHandle(resume[1])
        else:
            _ntdll.NtResumeProcess(resume[1])
        return Contained(job, hproc, pid)
    except BaseException:
        _k32.TerminateJobObject(job, 1)
        _k32.CloseHandle(job)
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
        logon: tuple[str, str, str] | None, spawner=None) -> RunResult:
    """Run to completion (or ``timeout``) contained; the whole tree is killed either way,
    so nothing it started outlives the call. Output is read back from its files."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stdout, stderr = out_dir / "stdout.txt", out_dir / "stderr.txt"
    proc = (spawner or spawn)(argv, cwd=cwd, env=env, stdout=stdout, stderr=stderr,
                              limits=limits, logon=logon)
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


# ---- the build Daedalus --------------------------------------------------------------------------
class BuildDaedalus:
    """A SECOND Daedalus, only for the Builds division, started per build as ``pionir-builds``
    on :8772 with a fresh bearer token, its roots restricted to the sandbox, its state and
    its temporary folder (where Daedalus makes its worktrees) inside the sandbox, in a
    kill-on-close job - and stopped (the whole tree killed) when the build ends, or at its
    ``not_after`` plus a grace whatever happens. Daedalus's own code is only read, never
    changed: everything is set through its environment (DAEDALUS_PORT, DAEDALUS_TOKEN,
    DAEDALUS_WORKSPACE_ROOTS, DAEDALUS_STATE_DIR, DAEDALUS_POLICY)."""

    def __init__(self, setup: SandboxSetup, *, port: int = BUILD_PORT,
                 model: str = "qwen3-coder:30b", ollama: str = "http://127.0.0.1:11434",
                 spawner=None, opener=None, clock=time.time, sleep=time.sleep,
                 start_timeout: float = 90.0, kill_grace: float = 180.0,
                 git_dir: Path | None = None) -> None:
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
            "OLLAMA_HOST": self.ollama,
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(gitconfig),
            "GIT_TERMINAL_PROMPT": "0"})

    def start(self, *, not_after: float) -> str:
        """Start it and wait until it answers as ITS configuration (roots = the sandbox,
        state inside it) and accepts ITS token. Returns the token. Raises SandboxError."""
        if self.proc is not None:
            raise SandboxError("the build Daedalus is already running")
        if self._answers():
            raise SandboxError(f"something already answers on 127.0.0.1:{self.port}; the "
                               "build Daedalus is started only on a free port")
        token = secrets.token_urlsafe(32)
        state = self.setup.state_dir
        logs = state / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        env = self.environment(token)
        self.proc = self._spawn(
            [str(self.setup.python), "-m", "daedalus.server"],
            cwd=self.setup.daedalus_src, env=env, stdout=logs / "daedalus.out.txt",
            stderr=logs / "daedalus.err.txt",
            limits=JobLimits(active_processes=32, job_memory_mb=8192, cpu_seconds=0),
            logon=self.setup.logon())
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
        """Kill the whole tree (idempotent)."""
        timer, self._timer = self._timer, None
        if timer is not None and timer is not threading.current_thread():
            timer.cancel()
        proc, self.proc = self.proc, None
        self.token = None
        if proc is not None:
            proc.close()


def info(setup: SandboxSetup | None) -> dict[str, Any]:
    if setup is None:
        return {"configured": False}
    return {"configured": True, "user": setup.user, "python": str(setup.python),
            "sandbox_root": str(setup.sandbox_root)}
