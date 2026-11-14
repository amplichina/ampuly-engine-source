"""Drop optional Windows token privileges inside the fixed Helper thread.

This is defence in depth, not an application sandbox: the Helper's real
security boundary remains its fixed protocol and absence of shell/file APIs.
The restricted impersonation token keeps Medium integrity (required by UIPI
to drive ordinary Medium applications) while disabling optional token
privileges for every OS call made by the single worker thread.
"""
from __future__ import annotations

import os

from ampuly.computer_use.windows_desktop_native import DesktopNativeRefused


_TOKEN_HANDLE = None


def restrict_current_thread() -> None:
    global _TOKEN_HANDLE
    if os.name != "nt":
        raise DesktopNativeRefused("windows_required")
    import ctypes
    from ctypes import wintypes as w

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes, kernel32.GetCurrentProcess.restype = [], w.HANDLE
    kernel32.CloseHandle.argtypes, kernel32.CloseHandle.restype = [w.HANDLE], w.BOOL
    advapi32.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    advapi32.OpenProcessToken.restype = w.BOOL
    advapi32.CreateRestrictedToken.argtypes = [
        w.HANDLE, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, ctypes.c_void_p,
        w.DWORD, ctypes.c_void_p, ctypes.POINTER(w.HANDLE),
    ]
    advapi32.CreateRestrictedToken.restype = w.BOOL
    advapi32.DuplicateTokenEx.argtypes = [
        w.HANDLE, w.DWORD, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
        ctypes.POINTER(w.HANDLE),
    ]
    advapi32.DuplicateTokenEx.restype = w.BOOL
    advapi32.SetThreadToken.argtypes = [ctypes.c_void_p, w.HANDLE]
    advapi32.SetThreadToken.restype = w.BOOL

    own, restricted_primary, restricted = w.HANDLE(), w.HANDLE(), w.HANDLE()
    TOKEN_QUERY, TOKEN_DUPLICATE, TOKEN_IMPERSONATE = 0x0008, 0x0002, 0x0004
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), TOKEN_QUERY | TOKEN_DUPLICATE | TOKEN_IMPERSONATE, ctypes.byref(own),
    ):
        raise DesktopNativeRefused("helper_token_open_failed")
    try:
        # DISABLE_MAX_PRIVILEGE removes every optional privilege except
        # SeChangeNotifyPrivilege. No SIDs are added and integrity is not
        # lowered, so UIPI may still deliver input to ordinary user apps.
        if not advapi32.CreateRestrictedToken(
            own, 0x1, 0, None, 0, None, 0, None, ctypes.byref(restricted_primary),
        ):
            raise DesktopNativeRefused("helper_restricted_token_failed")
    finally:
        kernel32.CloseHandle(own)
    # SetThreadToken requires an impersonation token; CreateRestrictedToken
    # preserves the process token's primary type.
    if not advapi32.DuplicateTokenEx(
        restricted_primary, 0xF01FF, None, 2, 2, ctypes.byref(restricted),
    ):
        kernel32.CloseHandle(restricted_primary)
        raise DesktopNativeRefused("helper_restricted_token_duplicate_failed")
    kernel32.CloseHandle(restricted_primary)
    if not advapi32.SetThreadToken(None, restricted):
        kernel32.CloseHandle(restricted)
        raise DesktopNativeRefused("helper_thread_restriction_failed")
    _TOKEN_HANDLE = restricted
