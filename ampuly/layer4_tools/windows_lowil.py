"""Low-integrity process confinement for task-scoped shell execution.

The runtime's sandbox contract, as bubblewrap expresses it upstream, is three
guarantees: the filesystem is readable everywhere but writable only in the task
workspace; no network unless granted; the child dies with its parent.

Windows can give us the first and the third without administrator rights. It
cannot give us the second: ``\\Device\\Afd`` — the socket device — grants
Everyone, grants RESTRICTED CODE explicitly, and carries a Low mandatory label
with a no-write-up policy. Windows labels it that way *on purpose* so that
low-integrity sandboxes can still reach the network; it is the legacy of IE's
Protected Mode. Firewall rules cannot be scoped to an integrity level, only to
an executable path, a service, an AppContainer package SID, or a local user
account. So network confinement is not withheld here by choice, and callers are
told so rather than being allowed to assume otherwise: see ``guarantees()``.

An AppContainer *would* block the network, but only as a side effect of the same
access check that makes it deny reads — and a confined PowerShell there returns
0 entries, exit 0, for a directory holding 28 files. Wrong answers are worse for
an agent than a refusal, which is why this backend exists (see
``windows_sandbox.py`` for the AppContainer one, kept behind an explicit flag).

What this backend does confine, measured against a same-machine medium-integrity
baseline: writes outside the workspace are refused and the file does not appear
on disk — including through junctions planted inside the workspace, ``\\\\?\\``
device paths, 8.3 short names, alternate data streams, and HKCU registry writes.
Reads are byte-identical to the baseline.

Known gaps, documented rather than papered over:
  - no network confinement (above)
  - msys2/cygwin binaries (git-bash) cannot start: they need to create
    ``\\BaseNamedObjects\\msys-2.0-<hash>``, which is labelled Medium, so
    NtCreateDirectoryObject returns 0xC0000022. Native git.exe is unaffected.
  - ``%USERPROFILE%\\AppData\\LocalLow`` is writable, because Windows itself
    labels it Low. Enumerated by ``selftest_confinement`` so it reaches the
    acceptance report instead of being discovered by a customer.

Every Win32 function below declares .argtypes/.restype. Without them ctypes
passes and returns 32-bit ints, silently truncating every pointer on x64, and
the failure surfaces as a malformed structure rather than as a failed call.
"""

from __future__ import annotations

import ctypes
import locale
import os
import shutil
import subprocess
import sys
import threading
from contextlib import ExitStack, contextmanager
from ctypes import wintypes as wt
from pathlib import Path, PureWindowsPath
from typing import Any, Callable

from loguru import logger
from ampuly.layer4_tools.windows_lowil_environment import (
    FrozenLowilEnvironmentError,
    LowilEnvironmentSeal,
    canonical_environment_items,
    seal_lowil_environment,
)

# The Low integrity level. A process labelled with it may read almost anything
# but the mandatory policy (NO_WRITE_UP) denies it write access to any object
# labelled higher, which is every ordinary file on the machine.
LOW_INTEGRITY_SID = "S-1-16-4096"

TOKEN_ASSIGN_PRIMARY = 0x0001
TOKEN_DUPLICATE = 0x0002
TOKEN_QUERY = 0x0008
TOKEN_ADJUST_DEFAULT = 0x0080
TOKEN_ALL_ACCESS = 0xF01FF

SecurityImpersonation = 2
TokenPrimary = 1
TokenIntegrityLevel = 25
SE_GROUP_INTEGRITY = 0x00000020

CREATE_NO_WINDOW = 0x08000000
CREATE_SUSPENDED = 0x00000004
CREATE_UNICODE_ENVIRONMENT = 0x00000400
STARTF_USESTDHANDLES = 0x00000100
HANDLE_FLAG_INHERIT = 0x00000001
WAIT_TIMEOUT = 0x00000102

FILE_ATTRIBUTE_HIDDEN = 0x00000002

# Scratch directory for the redirected %TEMP%. The name matches what the
# workspace layer already treats as runtime-internal, so nothing here is ever
# indexed as a deliverable.
_SCRATCH_DIR = ".ampuly-tmp"

JobObjectExtendedLimitInformation = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

# Labelling one of these Low would hand every low-integrity process on the
# machine write access to it. A workspace path that resolves into one is a
# redirected path, not a workspace, and is refused.
_UNLABELLABLE = ("windows", "program files", "program files (x86)", "programdata")


class ConfinementUnavailable(RuntimeError):
    """This host cannot provide the confinement the runtime asks for."""


if sys.platform == "win32":  # pragma: no branch - module is Windows-only
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
else:  # pragma: no cover - imported for typing on other platforms
    _kernel32 = _advapi32 = None  # type: ignore[assignment]


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


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wt.HANDLE), ("hThread", wt.HANDLE),
        ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD),
    ]


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wt.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wt.BOOL),
    ]


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wt.DWORD)]


class _TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", _SID_AND_ATTRIBUTES)]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


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


_bound = False


def _bind_signatures() -> None:
    global _bound
    if _bound:
        return
    _kernel32.GetCurrentProcess.argtypes = []
    _kernel32.GetCurrentProcess.restype = wt.HANDLE
    _kernel32.CloseHandle.argtypes = [wt.HANDLE]
    _kernel32.CloseHandle.restype = wt.BOOL
    _kernel32.CreatePipe.argtypes = [
        ctypes.POINTER(wt.HANDLE), ctypes.POINTER(wt.HANDLE),
        ctypes.POINTER(_SECURITY_ATTRIBUTES), wt.DWORD,
    ]
    _kernel32.CreatePipe.restype = wt.BOOL
    _kernel32.SetHandleInformation.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD]
    _kernel32.SetHandleInformation.restype = wt.BOOL
    _kernel32.ReadFile.argtypes = [
        wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p,
    ]
    _kernel32.ReadFile.restype = wt.BOOL
    _kernel32.WriteFile.argtypes = [
        wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p,
    ]
    _kernel32.WriteFile.restype = wt.BOOL
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wt.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wt.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wt.BOOL
    _kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    _kernel32.WaitForSingleObject.restype = wt.DWORD
    _kernel32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wt.BOOL
    _kernel32.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
    _kernel32.TerminateProcess.restype = wt.BOOL
    _kernel32.ResumeThread.argtypes = [wt.HANDLE]
    _kernel32.ResumeThread.restype = wt.DWORD
    _kernel32.SetFileAttributesW.argtypes = [wt.LPCWSTR, wt.DWORD]
    _kernel32.SetFileAttributesW.restype = wt.BOOL
    _kernel32.GetSystemDirectoryW.argtypes = [wt.LPWSTR, wt.UINT]
    _kernel32.GetSystemDirectoryW.restype = wt.UINT

    _advapi32.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
    _advapi32.OpenProcessToken.restype = wt.BOOL
    _advapi32.DuplicateTokenEx.argtypes = [
        wt.HANDLE, wt.DWORD, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
        ctypes.POINTER(wt.HANDLE),
    ]
    _advapi32.DuplicateTokenEx.restype = wt.BOOL
    _advapi32.ConvertStringSidToSidW.argtypes = [
        wt.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
    ]
    _advapi32.ConvertStringSidToSidW.restype = wt.BOOL
    _advapi32.GetLengthSid.argtypes = [ctypes.c_void_p]
    _advapi32.GetLengthSid.restype = wt.DWORD
    _advapi32.SetTokenInformation.argtypes = [
        wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD,
    ]
    _advapi32.SetTokenInformation.restype = wt.BOOL
    _advapi32.CreateProcessAsUserW.argtypes = [
        wt.HANDLE, wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
        wt.BOOL, wt.DWORD, ctypes.c_void_p, wt.LPCWSTR,
        ctypes.POINTER(_STARTUPINFOW), ctypes.POINTER(_PROCESS_INFORMATION),
    ]
    _advapi32.CreateProcessAsUserW.restype = wt.BOOL
    _bound = True


def guarantees() -> dict[str, bool]:
    """What this backend actually enforces, for callers that decide on it.

    ``network`` is False and cannot be made True here; a caller that waives a
    human approval prompt on the strength of confinement must keep whatever
    gate it applies to commands that can reach the network.
    """
    return {"filesystem": True, "lifetime": True, "network": False}


def lowil_supported() -> bool:
    """Whether a Low integrity token can be built on this host."""
    if sys.platform != "win32":
        return False
    try:
        token = build_low_token()
    except Exception:
        return False
    _kernel32.CloseHandle(token)
    return True


def build_low_token() -> wt.HANDLE:
    """Duplicate this process's token and drop its integrity level to Low."""
    if sys.platform != "win32":
        raise ConfinementUnavailable("integrity levels are a Windows facility")
    _bind_signatures()

    own = wt.HANDLE()
    if not _advapi32.OpenProcessToken(
        _kernel32.GetCurrentProcess(),
        TOKEN_DUPLICATE | TOKEN_QUERY | TOKEN_ASSIGN_PRIMARY | TOKEN_ADJUST_DEFAULT,
        ctypes.byref(own),
    ):
        raise ConfinementUnavailable(
            f"OpenProcessToken failed ({ctypes.get_last_error()})"
        )
    try:
        duplicate = wt.HANDLE()
        if not _advapi32.DuplicateTokenEx(
            own, TOKEN_ALL_ACCESS, None, SecurityImpersonation, TokenPrimary,
            ctypes.byref(duplicate),
        ):
            raise ConfinementUnavailable(
                f"DuplicateTokenEx failed ({ctypes.get_last_error()})"
            )
    finally:
        _kernel32.CloseHandle(own)

    sid = ctypes.c_void_p()
    if not _advapi32.ConvertStringSidToSidW(LOW_INTEGRITY_SID, ctypes.byref(sid)):
        _kernel32.CloseHandle(duplicate)
        raise ConfinementUnavailable(
            f"could not build the Low integrity SID ({ctypes.get_last_error()})"
        )

    label = _TOKEN_MANDATORY_LABEL()
    label.Label.Sid = sid
    label.Label.Attributes = SE_GROUP_INTEGRITY
    size = ctypes.sizeof(label) + _advapi32.GetLengthSid(sid)
    if not _advapi32.SetTokenInformation(
        duplicate, TokenIntegrityLevel, ctypes.byref(label), size
    ):
        _kernel32.CloseHandle(duplicate)
        raise ConfinementUnavailable(
            f"could not lower the token to Low ({ctypes.get_last_error()})"
        )
    return duplicate


def _icacls(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["icacls", *args], capture_output=True, text=True,
        errors="replace", timeout=120, creationflags=CREATE_NO_WINDOW,
    )


def workspace_is_labelled(path: str) -> bool:
    """Whether the directory carries an inheritable Low mandatory label."""
    try:
        listing = _icacls(str(path)).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return False
    return "Low Mandatory Level" in listing or "低强制性级别" in listing


def prepare_workspace(workspace: str) -> bool:
    """Label the workspace Low so the confined process can write there.

    The path is resolved first: a confined process that plants a junction where
    its workspace is expected would otherwise redirect this label onto whatever
    the junction points at, and labelling somebody's home directory Low would
    hand write access to it to every low-integrity process on the machine. The
    label is read back rather than trusting icacls's exit code.
    """
    try:
        real = Path(os.path.realpath(str(workspace)))
    except OSError:
        return False
    if not real.is_dir():
        return False

    resolved = str(real)
    if real.parent == real:
        logger.warning(f"confinement: refusing to label a volume root ({resolved})")
        return False
    if real.name.lower() in _UNLABELLABLE or str(real).lower() == str(
        Path.home()
    ).lower():
        logger.warning(f"confinement: refusing to label a system location ({resolved})")
        return False

    if not workspace_is_labelled(resolved):
        result = _icacls(resolved, "/setintegritylevel", "(OI)(CI)L")
        if result.returncode != 0:
            logger.warning(
                f"confinement: could not label {resolved} Low: "
                f"{(result.stdout + result.stderr).strip()[:200]}"
            )
            return False
    # icacls reports success for paths it did not actually relabel, so the
    # readback is the check that matters.
    return workspace_is_labelled(resolved)


def _console_encoding() -> str:
    return locale.getpreferredencoding(False) or "mbcs"


def _native_system_directory() -> str:
    """A bounded OS path, never a value or fallback from the parent environment."""
    capacity = 32768
    try:
        buffer = ctypes.create_unicode_buffer(capacity)
        length = _kernel32.GetSystemDirectoryW(buffer, capacity)
        # An insufficient buffer returns the required capacity. Never accept a
        # truncated value or retry with an unbounded allocation.
        if type(length) is not int or not 0 < length < capacity:
            raise ValueError
        if buffer[length] != "\0" or "\0" in buffer[:length]:
            raise ValueError
        value = buffer.value
        path = PureWindowsPath(value)
        if (not path.is_absolute() or len(path.drive) != 2
                or path.drive[0].upper() not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                or path.drive[1] != ":" or ";" in value
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            raise ValueError
        return value
    except Exception:
        # The callers render this failure. Do not expose a path, native error
        # text or environment value, and do not downgrade to C:\\Windows.
        raise ConfinementUnavailable("Windows system directory is unavailable") from None


def confined_environment(
    workspace: str, base: dict[str, str] | None = None
) -> dict[str, str]:
    """Environment for a confined child: a writable TEMP, and a sane PATH.

    %TEMP% is a medium-integrity directory, so a confined process cannot write
    there. CPython's tempfile then falls back to the working directory without
    saying so and litters the deliverable folder, while PowerShell's Add-Type
    and anything else needing a scratch file simply fails. Redirecting TEMP into
    the workspace fixes both.

    System32 is put ahead of the rest of PATH because Git ships msys builds of
    `find`, `sort` and friends; those cannot start under a Low token, and the
    resulting 0xC0000142 looks like a sandbox fault rather than the wrong
    binary being picked up.
    """
    _bind_signatures()
    system32 = _native_system_directory()
    if base is None:
        # The callers in shell.py / python_exec.py hand over an environment
        # that already went through execution_context.build_subprocess_env;
        # a bare call (self-test, direct use) takes the same allow-listed
        # slice rather than the whole process environment.
        from ampuly.layer4_tools.execution_context import build_subprocess_env

        base = build_subprocess_env()
    env = _without_secrets(dict(base))
    scratch = Path(workspace) / _SCRATCH_DIR
    try:
        scratch.mkdir(parents=True, exist_ok=True)
        # Hidden, or it lists as a directory the agent did not create and did
        # not expect — the same phantom-deliverable problem a capture file has.
        _kernel32.SetFileAttributesW(str(scratch), FILE_ATTRIBUTE_HIDDEN)
    except OSError:
        scratch = Path(workspace)
    for name in ("TEMP", "TMP", "TMPDIR"):
        env[name] = str(scratch)
    path = env.get("PATH", "") or env.get("Path", "")
    if system32.lower() not in path.lower().split(os.pathsep)[:1]:
        env["PATH"] = os.pathsep.join([system32, path]) if path else system32
    env.pop("Path", None)
    return env


# Suffixes that mark a provider credential. Matched by suffix so a provider
# added to the settings page later is covered without editing this list.
_SECRET_SUFFIXES = ("_API_KEY", "_SECRETKEY", "_ACCESSKEY", "_SECRET_KEY")
# Credentials whose names do not follow the suffix convention.
_SECRET_NAMES = frozenset({"VOLC_ACCESSKEY", "VOLC_SECRETKEY"})


def _without_secrets(env: dict[str, str]) -> dict[str, str]:
    """Strip provider credentials before they reach a confined child.

    The confinement stops the child writing outside its workspace; it cannot
    stop it reaching the network, because Windows labels the socket device so
    that low-integrity processes may use it. So a child that inherits
    DEEPSEEK_API_KEY inherits a way to spend it. The engine keeps the keys for
    its own model calls; nothing a tool runs needs them.

    Since 2026-10-07 the first line is the allow list in
    execution_context.build_subprocess_env, which names AMPULY_COLLAB_RPC_TOKEN
    (the company runtime's agents talk to each other with it) explicitly. This
    deny list stays as the second line: it also covers a task manifest that
    hands a provider key down through ``env_vars``.
    """
    kept: dict[str, str] = {}
    for name, value in env.items():
        upper = name.upper()
        if upper in _SECRET_NAMES or upper.endswith(_SECRET_SUFFIXES):
            continue
        kept[name] = value
    return kept


def _drain(handle: wt.HANDLE, sink: list[bytes], on_chunk: Callable | None) -> None:
    """Read one pipe to EOF. Two of these run concurrently so neither blocks."""
    buffer = (ctypes.c_char * 8192)()
    read = wt.DWORD()
    while True:
        ok = _kernel32.ReadFile(handle, buffer, 8192, ctypes.byref(read), None)
        if not ok or read.value == 0:
            return
        chunk = bytes(buffer[: read.value])
        sink.append(chunk)
        if on_chunk is not None:
            try:
                on_chunk(chunk.decode(_console_encoding(), "replace"))
            except Exception:  # a progress callback must never kill the read
                pass


def freeze_lowil_environment(
    cwd: str, base: dict[str, str],
) -> LowilEnvironmentSeal:
    """Freeze bootstrap choices without creating/labeling any directory.

    The pre-existing backend resolver is separate and may have effects. This
    seal is consistency data, not an authorization or confinement proof.
    """
    # Validate before filtering or replacing bootstrap names: aliases must
    # not disappear in a way that conceals a malformed incoming environment.
    checked = dict(canonical_environment_items(tuple(base.items())))
    _bind_signatures()
    system_directory = _native_system_directory()
    env = _without_secrets(checked)
    path = next((value for name, value in env.items() if name.upper() == "PATH"), "")
    for name in tuple(env):
        if name.upper() in {"PATH", "TEMP", "TMP", "TMPDIR"}:
            env.pop(name)
    scratch = str(PureWindowsPath(cwd) / _SCRATCH_DIR)
    for name in ("TEMP", "TMP", "TMPDIR"):
        env[name] = scratch
    env["PATH"] = (
        path if path.split(";", 1)[0] == system_directory
        else system_directory + (";" + path if path else "")
    )
    return seal_lowil_environment(cwd, env, system_directory=system_directory)


@contextmanager
def _frozen_scratch_guard(seal: LowilEnvironmentSeal):
    """Use existing private native primitives; never lexical mkdir/label.

    Handles remain held across launch. SHARE_DELETE permits name replacement,
    so identity rechecks are observational, not an atomic filesystem snapshot.
    The placeholder is only a location for _open_parent, never an opened file.
    """
    held = ExitStack()
    try:
        from ampuly.layer4_tools.workspace_fs_windows import WindowsSecureWorkspace

        workspace = held.enter_context(WindowsSecureWorkspace(seal.cwd, seal.cwd))
        target = workspace.resolve(
            _SCRATCH_DIR + "/.environment-preflight", use_output_root=False,
        )
        scratch, _ = held.enter_context(workspace._open_parent(target, create_dirs=True))
        root_identity = workspace._identity(workspace.root_handle)
        scratch_identity = workspace._identity(scratch)

        def verify() -> None:
            try:
                if workspace._final_path(workspace.root_handle) != seal.cwd:
                    raise ConfinementUnavailable("Frozen low-integrity scratch is unavailable")
                if workspace._final_path(scratch) != seal.scratch_directory:
                    raise ConfinementUnavailable("Frozen low-integrity scratch is unavailable")
                with WindowsSecureWorkspace(seal.cwd, seal.cwd) as current:
                    if current._identity(current.root_handle) != root_identity:
                        raise ConfinementUnavailable("Frozen low-integrity scratch is unavailable")
                    with current._open_parent(target) as (current_scratch, _):
                        if current._identity(current_scratch) != scratch_identity:
                            raise ConfinementUnavailable("Frozen low-integrity scratch is unavailable")
                        if current._final_path(current_scratch) != seal.scratch_directory:
                            raise ConfinementUnavailable("Frozen low-integrity scratch is unavailable")

            except Exception:
                raise ConfinementUnavailable("Frozen low-integrity scratch is unavailable") from None

        verify()
    except BaseException as error:
        held.close()
        if not isinstance(error, Exception):
            raise
        raise ConfinementUnavailable("Frozen low-integrity scratch is unavailable") from None
    try:
        yield verify
    finally:
        held.close()


def run_confined(
    command: str,
    *,
    cwd: str,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
    on_progress: Callable | None = None,
    stdin_bytes: bytes | None = None,
    encoding: str | None = None,
    sealed_environment: LowilEnvironmentSeal | None = None,
) -> dict[str, Any]:
    if sealed_environment is None:
        return _run_confined(
            command, cwd=cwd, timeout=timeout, env=env,
            on_progress=on_progress, stdin_bytes=stdin_bytes, encoding=encoding,
        )
    if sys.platform != "win32":
        raise ConfinementUnavailable("integrity levels are a Windows facility")
    try:
        if type(sealed_environment) is not LowilEnvironmentSeal:
            raise FrozenLowilEnvironmentError("invalid seal")
        sealed_environment.verify(cwd=cwd)
        if env is not None:
            if canonical_environment_items(tuple(env.items())) != sealed_environment.items:
                raise FrozenLowilEnvironmentError("invalid frozen mapping")
    except (FrozenLowilEnvironmentError, AttributeError, TypeError):
        raise ConfinementUnavailable("Frozen low-integrity environment is unavailable; prepare a new approval") from None
    with _frozen_scratch_guard(sealed_environment) as launch_guard:
        return _run_confined(
            command, cwd=cwd, timeout=timeout, env=env,
            on_progress=on_progress, stdin_bytes=stdin_bytes, encoding=encoding,
            _sealed_environment=sealed_environment, _launch_guard=launch_guard,
        )


def _run_confined(
    command: str,
    *,
    cwd: str,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
    on_progress: Callable | None = None,
    stdin_bytes: bytes | None = None,
    encoding: str | None = None,
    _sealed_environment: LowilEnvironmentSeal | None = None,
    _launch_guard: Callable | None = None,
) -> dict[str, Any]:
    """Run ``command`` under a Low integrity token, capturing its output.

    Output is captured through anonymous pipes rather than a file in the
    workspace: a capture file shows up in the agent's own directory listings as
    a phantom deliverable, and it cannot carry stderr separately or stream
    progress while the command is still running.
    """
    if sys.platform != "win32":
        raise ConfinementUnavailable("integrity levels are a Windows facility")
    _bind_signatures()

    workspace = str(Path(cwd).resolve())
    # Environment/native bootstrap failure must precede token, pipe and Job
    # allocation. No process resources exist to leak at this refusal boundary.
    if _sealed_environment is None:
        resolved_env = confined_environment(workspace, env)
        blob = "".join(f"{k}={v}\0" for k, v in resolved_env.items()) + "\0"
        env_buffer = ctypes.create_unicode_buffer(blob)
    else:
        try:
            _sealed_environment.verify(cwd=workspace)
            if _launch_guard is None:
                raise FrozenLowilEnvironmentError("missing scratch guard")
            _launch_guard()
            # Copy the frozen raw bytes once; no decoding/re-encoding and no
            # implicit terminator added by create_unicode_buffer.
            env_buffer = (ctypes.c_uint16 * _sealed_environment.code_units).from_buffer_copy(
                _sealed_environment.block
            )
        except (FrozenLowilEnvironmentError, ValueError, TypeError, MemoryError):
            raise ConfinementUnavailable("Frozen low-integrity environment is unavailable; prepare a new approval") from None
    token = build_low_token()
    inheritable = _SECURITY_ATTRIBUTES()
    inheritable.nLength = ctypes.sizeof(_SECURITY_ATTRIBUTES)
    inheritable.lpSecurityDescriptor = None
    inheritable.bInheritHandle = True

    out_read, out_write = wt.HANDLE(), wt.HANDLE()
    err_read, err_write = wt.HANDLE(), wt.HANDLE()
    if not _kernel32.CreatePipe(
        ctypes.byref(out_read), ctypes.byref(out_write), ctypes.byref(inheritable), 0
    ) or not _kernel32.CreatePipe(
        ctypes.byref(err_read), ctypes.byref(err_write), ctypes.byref(inheritable), 0
    ):
        _kernel32.CloseHandle(token)
        raise ConfinementUnavailable(f"CreatePipe failed ({ctypes.get_last_error()})")
    # Our ends must not reach the child, or the pipes never report EOF.
    _kernel32.SetHandleInformation(out_read, HANDLE_FLAG_INHERIT, 0)
    _kernel32.SetHandleInformation(err_read, HANDLE_FLAG_INHERIT, 0)

    # A payload delivered over stdin — python_exec's company path feeds the
    # approved source that way, into `python -I -c <bootstrap>` — gets its own
    # pipe. The child inherits only the read end; we keep the write end.
    in_read = in_write = None
    if stdin_bytes is not None:
        in_read, in_write = wt.HANDLE(), wt.HANDLE()
        if not _kernel32.CreatePipe(
            ctypes.byref(in_read), ctypes.byref(in_write), ctypes.byref(inheritable), 0
        ):
            for handle in (out_read, out_write, err_read, err_write, token):
                _kernel32.CloseHandle(handle)
            raise ConfinementUnavailable(f"CreatePipe(stdin) failed ({ctypes.get_last_error()})")
        _kernel32.SetHandleInformation(in_write, HANDLE_FLAG_INHERIT, 0)

    startup = _STARTUPINFOW()
    startup.cb = ctypes.sizeof(_STARTUPINFOW)
    startup.dwFlags = STARTF_USESTDHANDLES
    startup.hStdInput = in_read
    startup.hStdOutput = out_write
    startup.hStdError = err_write
    process = _PROCESS_INFORMATION()

    job = _kernel32.CreateJobObjectW(None, None)
    if job:
        limits = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        _kernel32.SetInformationJobObject(
            job, JobObjectExtendedLimitInformation,
            ctypes.byref(limits), ctypes.sizeof(limits),
        )

    if _launch_guard is not None:
        try:
            _launch_guard()
        except BaseException:
            for handle in (out_read, out_write, err_read, err_write, in_read, in_write, token, job):
                if handle is not None:
                    _kernel32.CloseHandle(handle)
            raise

    # CREATE_NO_WINDOW, never DETACHED_PROCESS: PowerShell needs a console and
    # under DETACHED_PROCESS it exits 0 having done nothing at all — which looks
    # exactly like a command that ran and produced no output.
    started = _advapi32.CreateProcessAsUserW(
        token, None, ctypes.create_unicode_buffer(command), None, None, True,
        CREATE_NO_WINDOW | CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT,
        ctypes.cast(env_buffer, ctypes.c_void_p), workspace,
        ctypes.byref(startup), ctypes.byref(process),
    )
    error = ctypes.get_last_error()
    # The child owns the write ends now; we must drop ours or ReadFile blocks
    # forever waiting for a writer that is us.
    _kernel32.CloseHandle(out_write)
    _kernel32.CloseHandle(err_write)
    if in_read is not None:
        _kernel32.CloseHandle(in_read)
    if not started:
        for handle in (out_read, err_read, in_write, token):
            if handle is not None:
                _kernel32.CloseHandle(handle)
        if job:
            _kernel32.CloseHandle(job)
        raise ConfinementUnavailable(f"CreateProcessAsUserW failed ({error})")

    if job:
        _kernel32.AssignProcessToJobObject(job, process.hProcess)
    _kernel32.ResumeThread(process.hThread)

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    readers = [
        threading.Thread(target=_drain, args=(out_read, stdout_chunks, on_progress),
                         daemon=True),
        threading.Thread(target=_drain, args=(err_read, stderr_chunks, on_progress),
                         daemon=True),
    ]
    for reader in readers:
        reader.start()

    if in_write is not None:
        payload = stdin_bytes or b""

        def _feed() -> None:
            offset = 0
            written = wt.DWORD()
            buffer = ctypes.create_string_buffer(payload, len(payload))
            while offset < len(payload):
                ok = _kernel32.WriteFile(
                    in_write,
                    ctypes.addressof(buffer) + offset,
                    len(payload) - offset,
                    ctypes.byref(written),
                    None,
                )
                if not ok or written.value == 0:
                    break
                offset += written.value
            # EOF is what tells the child the payload is complete.
            _kernel32.CloseHandle(in_write)

        threading.Thread(target=_feed, daemon=True).start()

    limit = int(max(1.0, timeout or 90.0) * 1000)
    timed_out = _kernel32.WaitForSingleObject(process.hProcess, limit) == WAIT_TIMEOUT
    if timed_out:
        _kernel32.TerminateProcess(process.hProcess, 1)
        _kernel32.WaitForSingleObject(process.hProcess, 5000)

    for reader in readers:
        reader.join(timeout=10)

    code = wt.DWORD()
    _kernel32.GetExitCodeProcess(process.hProcess, ctypes.byref(code))
    for handle in (process.hProcess, process.hThread, out_read, err_read, token):
        _kernel32.CloseHandle(handle)
    if job:
        _kernel32.CloseHandle(job)

    enc = encoding or _console_encoding()
    return {
        "exit_code": int(code.value),
        "timed_out": timed_out,
        "pid": int(process.dwProcessId),
        "stdout": b"".join(stdout_chunks).decode(enc, "replace"),
        "stderr": b"".join(stderr_chunks).decode(enc, "replace"),
        "sandbox": "lowil",
    }


def run_shell_confined(
    argv: list[str] | str,
    *,
    cwd: str,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
    on_progress: Callable | None = None,
    stdin_bytes: bytes | None = None,
    encoding: str | None = None,
    sealed_environment: LowilEnvironmentSeal | None = None,
) -> dict[str, Any]:
    """Run an already-resolved argv (or a raw command line) confined.

    The argv the caller resolved is used as-is. Re-wrapping it in ``cmd.exe /c``
    here would silently swap the shell the runtime chose, which is how `find`
    once became Windows' string-search `find.exe` while the result still
    reported that PowerShell had run it.
    """
    if isinstance(argv, str):
        command = argv
    else:
        command = subprocess.list2cmdline(argv)
    return run_confined(
        command, cwd=cwd, timeout=timeout, env=env, on_progress=on_progress,
        stdin_bytes=stdin_bytes, encoding=encoding,
        **({"sealed_environment": sealed_environment} if sealed_environment is not None else {}),
    )


def deliver_artifact(source: str, destination: str) -> str:
    """Copy a confined process's output out, leaving the Low label behind.

    A move would carry the label with it and the delivered file would stay
    writable by any low-integrity process; a copy inherits the destination's
    label instead.
    """
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    if workspace_is_labelled(str(target)):
        _icacls(str(target), "/setintegritylevel", "M")
    return str(target)


def selftest_confinement(workspace: str) -> dict[str, Any]:
    """Prove the confinement works here, by observing effects rather than codes.

    Run before the runtime is allowed to treat confinement as available: a
    sandbox that reports itself present without confining is worse than none,
    because the permission layer waives its approval prompt on the strength of
    that report.
    """
    report: dict[str, Any] = {
        "filesystem": False, "lifetime": True, "network": False,
        "reads": False, "leaks": [], "detail": "",
    }
    if not prepare_workspace(workspace):
        report["detail"] = "workspace could not be labelled Low"
        return report

    root = Path(os.path.realpath(workspace))
    # A workspace can sit inside an already-Low tree (the DSH checkout is one),
    # where its parent is writable by design and proves nothing. Probe a fixed
    # medium-integrity location instead: a Low child must be refused there.
    public = Path(os.environ.get("PUBLIC") or r"C:\Users\Public")
    outside = public / f".confinement-probe-{os.getpid()}.txt"
    if public == root or public in root.parents:
        outside = root.parent / f".confinement-probe-{os.getpid()}.txt"
    try:
        inside = root / ".confinement-probe.txt"
        written = run_confined(
            f'cmd.exe /c echo confined > "{inside}"', cwd=str(root), timeout=60,
        )
        blocked = run_confined(
            f'cmd.exe /c echo breach > "{outside}"', cwd=str(root), timeout=60,
        )
        readable = run_confined(
            r'cmd.exe /c type C:\Windows\win.ini', cwd=str(root), timeout=60,
        )
        report["filesystem"] = (
            written["exit_code"] == 0 and inside.exists() and not outside.exists()
        )
        report["reads"] = readable["exit_code"] == 0 and bool(readable["stdout"].strip())
        if not report["filesystem"]:
            report["detail"] = (
                f"write inside exit={written['exit_code']} present={inside.exists()}; "
                f"write outside exit={blocked['exit_code']} present={outside.exists()}"
            )
        try:
            inside.unlink()
        except OSError:
            pass
    except ConfinementUnavailable as exc:
        report["detail"] = str(exc)
        return report
    finally:
        try:
            outside.unlink()
        except OSError:
            pass

    # Windows labels these Low itself, so a Low process can write them. They are
    # reported, not fixed: fixing them means deny-ACEs on system directories of
    # every customer machine.
    localow = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "AppData" / "LocalLow"
    if localow.is_dir():
        report["leaks"].append(str(localow))
    return report
