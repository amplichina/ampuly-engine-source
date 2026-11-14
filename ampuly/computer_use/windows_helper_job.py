"""Windows Job Object lifetime boundary for the Desktop Helper."""
from __future__ import annotations

import os

from ampuly.computer_use.windows_desktop_native import DesktopNativeRefused


def attach_kill_on_close_job(pid: int):
    if os.name != "nt":
        raise DesktopNativeRefused("windows_required")
    import ctypes
    from ctypes import wintypes as w

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class BASIC_LIMITS(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", w.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", w.DWORD),
            ("Affinity", ctypes.c_size_t), ("PriorityClass", w.DWORD), ("SchedulingClass", w.DWORD),
        ]

    class EXTENDED_LIMITS(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC_LIMITS), ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32.CreateJobObjectW.argtypes, kernel32.CreateJobObjectW.restype = [ctypes.c_void_p, w.LPCWSTR], w.HANDLE
    kernel32.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    kernel32.SetInformationJobObject.restype = w.BOOL
    kernel32.AssignProcessToJobObject.argtypes, kernel32.AssignProcessToJobObject.restype = [w.HANDLE, w.HANDLE], w.BOOL
    kernel32.OpenProcess.argtypes, kernel32.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
    kernel32.CloseHandle.argtypes, kernel32.CloseHandle.restype = [w.HANDLE], w.BOOL

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise DesktopNativeRefused("helper_job_create_failed")
    process = None
    try:
        limits = EXTENDED_LIMITS()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise DesktopNativeRefused("helper_job_policy_failed")
        process = kernel32.OpenProcess(0x0001 | 0x0100 | 0x0400, False, int(pid))
        if not process:
            raise DesktopNativeRefused("helper_process_open_failed")
        if not kernel32.AssignProcessToJobObject(job, process):
            raise DesktopNativeRefused("helper_job_assign_failed")
        return job
    except Exception:
        kernel32.CloseHandle(job)
        raise
    finally:
        if process:
            kernel32.CloseHandle(process)


def close_job(handle) -> None:
    if not handle or os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes as w
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes, kernel32.CloseHandle.restype = [w.HANDLE], w.BOOL
    kernel32.CloseHandle(handle)
