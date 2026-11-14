"""Read-only, instantaneous PID liveness; never proof of process ownership.

The standalone installer imports this stdlib-only module before ampuly is usable.
Windows must never use os.kill(pid, 0): zero is a console control event there.
ALIVE describes the current PID occupant, not the writer of an old PID file.
Callers may discard stale state only on DEAD; UNKNOWN must retain it.
"""

from __future__ import annotations

import ctypes
import errno
import os
from enum import Enum


class ProcessState(Enum):
    ALIVE = "alive"
    DEAD = "dead"
    UNKNOWN = "unknown"

    def __bool__(self) -> bool:
        raise TypeError("compare ProcessState explicitly; UNKNOWN is not DEAD")


def _windows_api():
    # Bind HANDLE as a pointer: ctypes' default c_int truncates 64-bit handles.
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    api.OpenProcess.restype = ctypes.c_void_p
    api.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    api.GetExitCodeProcess.restype = ctypes.c_int
    api.CloseHandle.argtypes = [ctypes.c_void_p]
    api.CloseHandle.restype = ctypes.c_int
    return api


def _windows_state(pid: int) -> ProcessState:
    try:
        api = _windows_api()
        handle = api.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION only.
        if not handle:
            # pid is already a nonzero DWORD and access flags are fixed. 87 is
            # the absent-PID result; access denied and every other error remain
            # UNKNOWN. Never retry with stronger access or privileges.
            return ProcessState.DEAD if ctypes.get_last_error() == 87 else ProcessState.UNKNOWN
    except (AttributeError, OSError, ValueError):
        return ProcessState.UNKNOWN

    state = ProcessState.UNKNOWN
    try:
        exit_code = ctypes.c_uint32()
        if api.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            # 259 can also be an application's exit code. Conservatively retain
            # the lock in that case; do not claim this is an ownership check.
            state = ProcessState.ALIVE if exit_code.value == 259 else ProcessState.DEAD
    except (AttributeError, OSError, ValueError):
        pass
    finally:
        try:
            if not api.CloseHandle(handle):
                state = ProcessState.UNKNOWN
        except (AttributeError, OSError, ValueError):
            state = ProcessState.UNKNOWN
    return state


def process_state(pid: int) -> ProcessState:
    """Observe one positive PID, with no signal/control event on Windows.

    Invalid IDs are UNKNOWN (not proof that a lock owner died). POSIX signal
    zero is an existence check: ESRCH means DEAD and EPERM means ALIVE.
    This snapshot does not make a later signal or unlink atomic, authenticate
    a PID-file writer, or detect PID reuse. Those need an ownership protocol.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return ProcessState.UNKNOWN
    if os.name == "nt":
        if pid > 0xFFFFFFFF:
            return ProcessState.UNKNOWN
        return _windows_state(pid)
    if os.name != "posix":
        return ProcessState.UNKNOWN
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return ProcessState.DEAD
        if exc.errno == errno.EPERM:
            return ProcessState.ALIVE
        return ProcessState.UNKNOWN
    except (OverflowError, ValueError):
        return ProcessState.UNKNOWN
    return ProcessState.ALIVE
