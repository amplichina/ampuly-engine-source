"""Windows process confinement for task-scoped shell execution.

The runtime's sandbox contract, as expressed by the bubblewrap wrapper it
uses on Linux, is three guarantees:

  1. the filesystem is read-only except the task workspace
  2. no network access unless explicitly granted
  3. the child process dies with its parent

Windows has no ``bwrap``, so every ``process_execute`` tool fell back to
asking a human for one-time approval — which made unattended operation
impossible (observed 2026-09-01: a design station sat blocked for 15 minutes
on a directory listing).

AppContainer expresses the same guarantees natively. A process launched into
one runs under its own package SID that, by default, holds no access to the
user's files at all; access is granted per directory through an ACL entry.
Network access is a capability that we deliberately never grant. A Job Object
with ``KILL_ON_JOB_CLOSE`` provides the die-with-parent property.

Verified on 2026-09-01: a confined ``cmd.exe`` writes inside the granted
workspace and is refused (exit 1, no file) outside it.

No administrator rights are required: creating an AppContainer profile and
granting a directory ACL are both per-user operations.
"""

from __future__ import annotations

import ctypes
import secrets
import subprocess
import sys
from ctypes import wintypes as wt
from pathlib import Path
from typing import Any

from loguru import logger

# One profile per installation. The name is stable so repeated runs reuse the
# same package SID (and therefore the ACLs already granted to it).
APP_CONTAINER_NAME = "SuperOnesEngineShellSandbox"
APP_CONTAINER_DISPLAY = "SuperOnes Engine shell sandbox"

_ALREADY_EXISTS = 0x800700B7

# Runtime-owned capture files. The prefix matches what the workspace layer
# treats as internal, so a transcript file never surfaces as a deliverable.
_CAPTURE_PREFIX = ".ampuly-write-sbx-"

EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_NO_WINDOW = 0x08000000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009

JobObjectExtendedLimitInformation = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

# Directories a confined shell must be able to read and execute from; without
# these the interpreter itself cannot start.
_SYSTEM_READ_PATHS = (r"C:\Windows", r"C:\Windows\System32")


class SandboxUnavailable(RuntimeError):
    """This host cannot provide the confinement the runtime asks for."""


if sys.platform == "win32":  # pragma: no branch - module is Windows-only
    _userenv = ctypes.WinDLL("userenv", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
else:  # pragma: no cover - imported for typing on other platforms
    _userenv = _kernel32 = _advapi32 = None  # type: ignore[assignment]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR),
        ("lpTitle", wt.LPWSTR), ("dwX", wt.DWORD), ("dwY", wt.DWORD),
        ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD), ("dwXCountChars", wt.DWORD),
        ("dwYCountChars", wt.DWORD), ("dwFillAttribute", wt.DWORD), ("dwFlags", wt.DWORD),
        ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", wt.HANDLE), ("hStdOutput", wt.HANDLE), ("hStdError", wt.HANDLE),
    ]


class _STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", _STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wt.HANDLE), ("hThread", wt.HANDLE),
        ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD),
    ]


class _SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [
        ("AppContainerSid", ctypes.c_void_p),
        ("Capabilities", ctypes.c_void_p),
        ("CapabilityCount", wt.DWORD),
        ("Reserved", wt.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wt.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wt.DWORD),
        ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
        ("PriorityClass", wt.DWORD),
        ("SchedulingClass", wt.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _bind_signatures() -> None:
    _userenv.CreateAppContainerProfile.argtypes = [
        wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, ctypes.c_void_p, wt.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _userenv.CreateAppContainerProfile.restype = ctypes.c_long
    _userenv.DeriveAppContainerSidFromAppContainerName.argtypes = [
        wt.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)
    ]
    _userenv.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
    _advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.LPWSTR)]
    _advapi32.ConvertSidToStringSidW.restype = wt.BOOL


_sid_cache: tuple[ctypes.c_void_p, str] | None = None


def sandbox_supported() -> bool:
    """Whether this host can confine a process with an AppContainer."""
    if sys.platform != "win32":
        return False
    try:
        container_sid()
    except Exception:
        return False
    return True


def container_sid() -> tuple[ctypes.c_void_p, str]:
    """Return (SID pointer, SID string) for this installation's container."""
    global _sid_cache
    if _sid_cache is not None:
        return _sid_cache
    if sys.platform != "win32":
        raise SandboxUnavailable("AppContainer is a Windows facility")
    _bind_signatures()

    sid = ctypes.c_void_p()
    hr = _userenv.CreateAppContainerProfile(
        APP_CONTAINER_NAME, APP_CONTAINER_NAME, APP_CONTAINER_DISPLAY,
        None, 0, ctypes.byref(sid),
    )
    if (hr & 0xFFFFFFFF) == _ALREADY_EXISTS:
        hr = _userenv.DeriveAppContainerSidFromAppContainerName(
            APP_CONTAINER_NAME, ctypes.byref(sid)
        )
    if hr < 0:
        raise SandboxUnavailable(
            f"could not obtain an AppContainer SID (hr=0x{hr & 0xFFFFFFFF:08X})"
        )

    text = wt.LPWSTR()
    if not _advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
        raise SandboxUnavailable("could not stringify the AppContainer SID")
    _sid_cache = (sid, str(text.value))
    return _sid_cache


def _icacls(path: str, grant: str) -> bool:
    result = subprocess.run(
        ["icacls", path, "/grant", grant],
        capture_output=True,
        # Only the return code is part of this contract. Native icacls emits
        # the system codepage, not necessarily Python's UTF-8 mode. Keep bytes
        # so the subprocess reader cannot crash decoding unused diagnostic text.
        text=False,
        creationflags=CREATE_NO_WINDOW,
    )
    return result.returncode == 0


def grant_workspace(workspace: str) -> bool:
    """Give the container full access to one workspace directory.

    This is the single writable surface the confined process gets; everything
    else stays inaccessible because the package SID was never granted it.
    """
    _sid, sid_text = container_sid()
    target = str(Path(workspace).resolve())
    ok = _icacls(target, f"*{sid_text}:(OI)(CI)F")
    if not ok:
        logger.warning(f"sandbox: could not grant workspace access to {target}")
    return ok


def ensure_system_read_access() -> None:
    """Let the container read and execute the system interpreters.

    Without this the confined process cannot even start ``cmd.exe``. Read and
    execute only: the container still cannot write into Windows.
    """
    _sid, sid_text = container_sid()
    for path in _SYSTEM_READ_PATHS:
        if Path(path).exists():
            _icacls(path, f"*{sid_text}:(RX)")


def run_shell_confined(
    command: str,
    *,
    cwd: str,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run a shell command inside the container and capture its output.

    Output is redirected to a runtime-owned file inside the workspace rather
    than piped: the container can write there by definition, and a file
    avoids the handle-inheritance and pipe-deadlock problems that come with
    capturing from a process running under a different security context.
    """
    workspace = Path(cwd).resolve()
    capture = workspace / f"{_CAPTURE_PREFIX}{secrets.token_hex(6)}.txt"
    wrapped = f'cmd.exe /c ({command}) > "{capture}" 2>&1'
    try:
        result = run_confined(wrapped, cwd=str(workspace), timeout=timeout, env=env)
        try:
            output = capture.read_text(encoding="utf-8", errors="replace")
        except OSError:
            output = ""
    finally:
        try:
            capture.unlink()
        except OSError:
            pass
    result["output"] = output
    return result


def run_confined(
    command: str,
    *,
    cwd: str,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run ``command`` inside the AppContainer, returning its result.

    The process is placed in a Job Object that terminates it when this handle
    closes, so a confined command cannot outlive the caller.

    ``env`` is the child's whole environment. Passing ``None`` to
    CreateProcessW inherited the engine's own — model keys and channel
    secrets included — so the container confined writes while handing over
    everything needed to spend them over the network. A caller that gives no
    environment gets the allow-listed slice, the same one shell.py passes.
    """
    if sys.platform != "win32":
        raise SandboxUnavailable("AppContainer is a Windows facility")
    if env is None:
        from ampuly.layer4_tools.execution_context import build_subprocess_env

        env = build_subprocess_env()
    env_blob = "".join(f"{k}={v}\0" for k, v in env.items()) + "\0"
    env_buffer = ctypes.create_unicode_buffer(env_blob)
    sid, _sid_text = container_sid()

    size = ctypes.c_size_t(0)
    _kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
    attr_buffer = (ctypes.c_byte * size.value)()
    attrs = ctypes.cast(attr_buffer, ctypes.c_void_p)
    if not _kernel32.InitializeProcThreadAttributeList(attrs, 1, 0, ctypes.byref(size)):
        raise SandboxUnavailable(
            f"InitializeProcThreadAttributeList failed ({ctypes.get_last_error()})"
        )

    capabilities = _SECURITY_CAPABILITIES()
    capabilities.AppContainerSid = sid
    capabilities.Capabilities = None
    # No capabilities are granted at all — in particular no network client,
    # which is how the "no network unless granted" half of the contract holds.
    capabilities.CapabilityCount = 0
    capabilities.Reserved = 0

    if not _kernel32.UpdateProcThreadAttribute(
        attrs, 0, ctypes.c_size_t(PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES),
        ctypes.byref(capabilities), ctypes.sizeof(capabilities), None, None,
    ):
        raise SandboxUnavailable(
            f"UpdateProcThreadAttribute failed ({ctypes.get_last_error()})"
        )

    startup = _STARTUPINFOEXW()
    startup.StartupInfo.cb = ctypes.sizeof(_STARTUPINFOEXW)
    startup.lpAttributeList = attrs
    process_info = _PROCESS_INFORMATION()

    job = _kernel32.CreateJobObjectW(None, None)
    if job:
        limits = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        _kernel32.SetInformationJobObject(
            job, JobObjectExtendedLimitInformation,
            ctypes.byref(limits), ctypes.sizeof(limits),
        )

    created = _kernel32.CreateProcessW(
        None,
        ctypes.create_unicode_buffer(command),
        None, None, False,
        EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT,
        ctypes.cast(env_buffer, ctypes.c_void_p),
        str(Path(cwd).resolve()),
        ctypes.byref(startup),
        ctypes.byref(process_info),
    )
    if not created:
        error = ctypes.get_last_error()
        if job:
            _kernel32.CloseHandle(job)
        raise SandboxUnavailable(f"CreateProcessW failed ({error})")

    if job:
        _kernel32.AssignProcessToJobObject(job, process_info.hProcess)

    wait_ms = 0xFFFFFFFF if timeout is None else int(max(1.0, timeout) * 1000)
    waited = _kernel32.WaitForSingleObject(process_info.hProcess, wait_ms)
    timed_out = waited == 0x00000102  # WAIT_TIMEOUT
    if timed_out:
        _kernel32.TerminateProcess(process_info.hProcess, 1)

    code = wt.DWORD()
    _kernel32.GetExitCodeProcess(process_info.hProcess, ctypes.byref(code))
    _kernel32.CloseHandle(process_info.hProcess)
    _kernel32.CloseHandle(process_info.hThread)
    if job:
        _kernel32.CloseHandle(job)

    return {
        "exit_code": int(code.value),
        "timed_out": timed_out,
        "sandbox": "appcontainer",
    }
