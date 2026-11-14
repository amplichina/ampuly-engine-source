"""Fail-closed Windows desktop target and input driver for P2.

The window/DPI/UIPI discipline is adapted from OpenAdapt Flow's MIT-licensed
``Win32WindowClient`` (Copyright 2026 OpenAdapt.AI / MLDSAI Inc.).  SuperOnes
keeps only the fixed atomic-input substrate and binds it to its own Gateway,
capability and process protocol; OpenAdapt's remote execution/``exec`` surface
is deliberately not imported.

This module separates policy from Win32 bindings.  ``WindowsDesktopDriver`` is
fully testable with a fake ``DesktopWin32Api`` on any platform.  The native API
will sit in the dedicated Helper process, never in the model process.
"""
from __future__ import annotations

import os
import re
import struct
import time
import zlib
from dataclasses import dataclass
import hashlib
from typing import Protocol

from ampuly.computer_use.contracts import Geometry


class DesktopNativeRefused(PermissionError):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


@dataclass(frozen=True)
class WindowTarget:
    hwnd: int
    pid: int
    process_name: str
    title: str
    display_id: str
    dpi_scale: float
    left: int
    top: int
    width: int
    height: int
    process_started: int
    foreground: bool
    visible: bool
    minimized: bool
    elevated: bool | None

    @property
    def identity(self) -> str:
        return f"hwnd:{self.hwnd}:pid:{self.pid}:started:{self.process_started}"

    def geometry(self, *, expires_at: float) -> Geometry:
        return Geometry(
            window_id=self.identity,
            display_id=self.display_id,
            dpi_scale=self.dpi_scale,
            width=self.width,
            height=self.height,
            expires_at=expires_at,
            origin_x=self.left,
            origin_y=self.top,
        )


@dataclass(frozen=True)
class CapturedWindow:
    png: bytes
    width: int
    height: int
    sha256: str
    masked_regions: tuple[tuple[int, int, int, int], ...]


class DesktopWin32Api(Protocol):
    def find_windows(self, process_name: str, title: str = "") -> list[WindowTarget]: ...
    def probe_window(self, hwnd: int) -> WindowTarget | None: ...
    def send_click(self, x: int, y: int, *, button: str = 'left', count: int = 1) -> None: ...
    def send_drag(self, x: int, y: int, to_x: int, to_y: int) -> None: ...
    def send_scroll(self, x: int, y: int, delta: int, *, horizontal: bool = False) -> None: ...
    def send_text(self, text: str) -> None: ...
    def capture_bgra(self, hwnd: int, width: int, height: int) -> bytes: ...


_IDENTITY = re.compile(r"^hwnd:(\d+):pid:(\d+):started:(\d+)$")


def foreground_is_foreign(pinned_hwnd: int, foreground_hwnd: int | None) -> bool | None:
    """True when some other window is in front. None when the answer is not known.

    None must not be treated as a takeover. A zero or missing foreground hwnd
    is unknown, not proof that a person took the desktop.
    """
    if foreground_hwnd is None:
        return None
    try:
        pinned, front = int(pinned_hwnd), int(foreground_hwnd)
    except (TypeError, ValueError):
        return None
    if pinned <= 0 or front <= 0:
        return None
    return front != pinned


def foreign_foreground(window_id: str) -> bool | None:
    """Whether the OS foreground hwnd is not this pinned window.

    None on non-Windows, on an unreadable hwnd, or when the foreground hwnd
    itself cannot be read. Only a positive different hwnd is a takeover.
    """
    match = _IDENTITY.fullmatch(str(window_id or ""))
    if match is None or os.name != "nt":
        return None
    pinned = int(match.group(1))
    if pinned <= 0:
        return None
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetForegroundWindow.restype = wintypes.HWND
        front = int(user32.GetForegroundWindow() or 0)
    except Exception:
        return None
    return foreground_is_foreign(pinned, front if front > 0 else None)


def _same_process_name(expected: str, actual: str) -> bool:
    # Accept either separator so policy tests and packaged clients behave the
    # same on Windows and non-Windows CI.
    a = re.split(r"[/\\]", str(expected))[-1].casefold()
    b = re.split(r"[/\\]", str(actual))[-1].casefold()
    if a.endswith(".exe"):
        a = a[:-4]
    if b.endswith(".exe"):
        b = b[:-4]
    return bool(a) and a == b


class WindowsDesktopDriver:
    """Atomic input into one exact, foreground, non-elevated window."""

    def __init__(self, api: DesktopWin32Api, *, clock=time.time) -> None:
        self._api = api
        self._clock = clock

    def observe(self, process_name: str, title: str = "", *, ttl_sec: float = 5.0) -> WindowTarget:
        matches = self._api.find_windows(process_name, title)
        if len(matches) != 1:
            raise DesktopNativeRefused("window_not_unique", f"found={len(matches)}")
        target = matches[0]
        self._require_usable(target)
        if not _same_process_name(process_name, target.process_name):
            raise DesktopNativeRefused("window_process_mismatch")
        return target

    def current_geometry(self, expected: Geometry) -> tuple[WindowTarget, Geometry]:
        match = _IDENTITY.fullmatch(str(expected.window_id or ""))
        if match is None:
            raise DesktopNativeRefused("invalid_window_identity")
        hwnd, pid, started = (int(value) for value in match.groups())
        target = self._api.probe_window(hwnd)
        if target is None:
            raise DesktopNativeRefused("window_gone")
        self._require_usable(target)
        if (target.pid, target.process_started) != (pid, started):
            raise DesktopNativeRefused("window_identity_changed")
        now_geometry = target.geometry(expires_at=expected.expires_at)
        if self._clock() >= expected.expires_at:
            raise DesktopNativeRefused("geometry_expired")
        if not expected.matches(now_geometry):
            raise DesktopNativeRefused("geometry_changed")
        return target, now_geometry

    def execute(self, kind: str, target: dict, expected: Geometry, *, text: str = "") -> Geometry:
        window, current = self.current_geometry(expected)
        if kind == "wait":
            seconds = max(0.0, min(float(target.get("seconds", 0.0) or 0.0), 5.0))
            time.sleep(seconds)
            return self.current_geometry(expected)[1]
        if kind == 'type_draft':
            if not text:
                raise DesktopNativeRefused('text_ref_empty')
            # Vision's type carries a secret length, not a new coordinate.
            # Keep the already selected field in the exact pinned window.
            if 'x' in target or 'y' in target:
                x, y = self._point(window, target)
                self._api.send_click(x, y)
            self._assert_same_target(expected)
            self._api.send_text(text)
            return self._assert_same_target(expected)
        x, y = self._point(window, target)
        if kind == "select_candidate":
            variant = target.get('click', '')
            if 'to' in target:
                if variant not in (None, '', 'single') or type(target['to']) is not dict:
                    raise DesktopNativeRefused('invalid_pointer_variant')
                end_x, end_y = self._point(window, target['to'])
                self._api.send_drag(x, y, end_x, end_y)
            elif variant in (None, '', 'single'):
                self._api.send_click(x, y)
            elif variant == 'double':
                self._api.send_click(x, y, count=2)
            elif variant == 'right':
                self._api.send_click(x, y, button='right')
            else:
                raise DesktopNativeRefused('invalid_pointer_variant')
        elif kind == "scroll":
            direction = str(target.get("direction", "down") or "down").lower()
            if direction not in {'up', 'down', 'left', 'right'}:
                raise DesktopNativeRefused('invalid_scroll_direction')
            delta = 120 if direction in {'up', 'right'} else -120
            if direction in {'left', 'right'}:
                self._api.send_scroll(x, y, delta, horizontal=True)
            else:
                self._api.send_scroll(x, y, delta)
        else:
            raise DesktopNativeRefused("kind_not_for_native_helper", kind)
        return self._assert_same_target(expected)

    def capture(self, expected: Geometry, masks: list[tuple[int, int, int, int]] | None = None) -> CapturedWindow:
        window, _ = self.current_geometry(expected)
        bounded = _validated_masks(masks or [], window.width, window.height)
        bgra = self._api.capture_bgra(window.hwnd, window.width, window.height)
        expected_bytes = window.width * window.height * 4
        if len(bgra) != expected_bytes:
            raise DesktopNativeRefused("capture_size_mismatch")
        self._assert_same_target(expected)
        png = _encode_png_from_bottom_up_bgra(bgra, window.width, window.height, bounded)
        return CapturedWindow(
            png=png,
            width=window.width,
            height=window.height,
            sha256=hashlib.sha256(png).hexdigest(),
            masked_regions=bounded,
        )

    def _assert_same_target(self, expected: Geometry) -> Geometry:
        return self.current_geometry(expected)[1]

    @staticmethod
    def _point(window: WindowTarget, target: dict) -> tuple[int, int]:
        try:
            rel_x, rel_y = float(target["x"]), float(target["y"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DesktopNativeRefused("invalid_target_point") from exc
        if not (0 <= rel_x < window.width and 0 <= rel_y < window.height):
            raise DesktopNativeRefused("target_outside_window")
        pixel_x, pixel_y = round(rel_x), round(rel_y)
        if not (0 <= pixel_x < window.width and 0 <= pixel_y < window.height):
            raise DesktopNativeRefused("target_outside_window")
        return window.left + pixel_x, window.top + pixel_y

    @staticmethod
    def _require_usable(target: WindowTarget) -> None:
        if not target.visible or target.minimized or target.width <= 0 or target.height <= 0:
            raise DesktopNativeRefused("window_not_interactable")
        if not target.foreground:
            raise DesktopNativeRefused("window_not_foreground")
        if target.elevated is not False:
            # UIPI silently drops input into a higher-integrity or unknown
            # target. Unknown therefore fails closed.
            raise DesktopNativeRefused("target_elevation_not_safe")


def _validated_masks(values: list[tuple[int, int, int, int]], width: int, height: int) -> tuple[tuple[int, int, int, int], ...]:
    masks: list[tuple[int, int, int, int]] = []
    for value in values:
        if not isinstance(value, (tuple, list)) or len(value) != 4:
            raise DesktopNativeRefused("invalid_mask_region")
        x, y, w, h = (int(item) for item in value)
        if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > width or y + h > height:
            raise DesktopNativeRefused("mask_outside_window")
        masks.append((x, y, w, h))
    return tuple(masks)


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)


def _encode_png_from_bottom_up_bgra(
    bgra: bytes, width: int, height: int, masks: tuple[tuple[int, int, int, int], ...] = (),
) -> bytes:
    """Encode a GDI bottom-up BGRA buffer after irreversible black masking."""
    if width <= 0 or height <= 0 or len(bgra) != width * height * 4:
        raise DesktopNativeRefused("capture_size_mismatch")
    rows: list[bytes] = []
    for y in range(height):
        source_y = height - 1 - y
        row = bytearray(bgra[source_y * width * 4:(source_y + 1) * width * 4])
        for x, mask_y, mask_w, mask_h in masks:
            if mask_y <= y < mask_y + mask_h:
                for px in range(x, x + mask_w):
                    offset = px * 4
                    row[offset:offset + 4] = b"\x00\x00\x00\xff"
        rgb = bytearray()
        for offset in range(0, len(row), 4):
            b, g, r, _a = row[offset:offset + 4]
            rgb.extend((r, g, b))
        rows.append(b"\x00" + bytes(rgb))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header) + _png_chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + _png_chunk(b"IEND", b"")


class NativeDesktopWin32Api:
    """Small stdlib-only Win32 binding used by the dedicated Helper process.

    It intentionally exposes no shell, process launch, clipboard or file API.
    Every SendInput burst must be complete; a partial count is a failure.
    """

    def __init__(self) -> None:
        if os.name != "nt":
            raise DesktopNativeRefused("windows_required")
        import ctypes
        from ctypes import wintypes

        self.c = ctypes
        self.w = wintypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self._bind()
        awareness = self.c.c_void_p(-4)  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        if not self.user32.SetProcessDpiAwarenessContext(awareness):
            # ERROR_ACCESS_DENIED means the process already chose a context;
            # verify it rather than silently accepting DPI virtualization.
            current = self.user32.GetThreadDpiAwarenessContext()
            if self.user32.GetAwarenessFromDpiAwarenessContext(current) != 2:
                raise DesktopNativeRefused("per_monitor_dpi_unavailable")

    def _bind(self) -> None:
        c, w, u, k, a = self.c, self.w, self.user32, self.kernel32, self.advapi32

        class POINT(c.Structure):
            _fields_ = [("x", w.LONG), ("y", w.LONG)]

        class RECT(c.Structure):
            _fields_ = [("left", w.LONG), ("top", w.LONG), ("right", w.LONG), ("bottom", w.LONG)]

        class MONITORINFOEXW(c.Structure):
            _fields_ = [("cbSize", w.DWORD), ("rcMonitor", RECT), ("rcWork", RECT),
                        ("dwFlags", w.DWORD), ("szDevice", w.WCHAR * 32)]

        class MOUSEINPUT(c.Structure):
            _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("mouseData", w.DWORD),
                        ("dwFlags", w.DWORD), ("time", w.DWORD), ("dwExtraInfo", c.POINTER(c.c_ulong))]

        class KEYBDINPUT(c.Structure):
            _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD),
                        ("time", w.DWORD), ("dwExtraInfo", c.POINTER(c.c_ulong))]

        class INPUTUNION(c.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

        class INPUT(c.Structure):
            _anonymous_ = ("u",)
            _fields_ = [("type", w.DWORD), ("u", INPUTUNION)]

        class BITMAPINFOHEADER(c.Structure):
            _fields_ = [("biSize", w.DWORD), ("biWidth", w.LONG), ("biHeight", w.LONG),
                        ("biPlanes", w.WORD), ("biBitCount", w.WORD), ("biCompression", w.DWORD),
                        ("biSizeImage", w.DWORD), ("biXPelsPerMeter", w.LONG), ("biYPelsPerMeter", w.LONG),
                        ("biClrUsed", w.DWORD), ("biClrImportant", w.DWORD)]

        class BITMAPINFO(c.Structure):
            _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", w.DWORD * 3)]

        self.POINT, self.RECT, self.MONITORINFOEXW = POINT, RECT, MONITORINFOEXW
        self.MOUSEINPUT, self.KEYBDINPUT, self.INPUT = MOUSEINPUT, KEYBDINPUT, INPUT
        self.BITMAPINFOHEADER, self.BITMAPINFO = BITMAPINFOHEADER, BITMAPINFO
        self.ENUMPROC = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        u.EnumWindows.argtypes, u.EnumWindows.restype = [self.ENUMPROC, w.LPARAM], w.BOOL
        for name, args, result in (
            ("IsWindow", [w.HWND], w.BOOL), ("IsWindowVisible", [w.HWND], w.BOOL),
            ("IsIconic", [w.HWND], w.BOOL), ("GetForegroundWindow", [], w.HWND),
            ("GetWindowTextLengthW", [w.HWND], c.c_int),
            ("GetClientRect", [w.HWND, c.POINTER(RECT)], w.BOOL),
            ("ClientToScreen", [w.HWND, c.POINTER(POINT)], w.BOOL),
            ("GetWindowThreadProcessId", [w.HWND, c.POINTER(w.DWORD)], w.DWORD),
            ("GetDpiForWindow", [w.HWND], w.UINT),
            ("MonitorFromWindow", [w.HWND, w.DWORD], w.HMONITOR),
            ("GetMonitorInfoW", [w.HMONITOR, c.POINTER(MONITORINFOEXW)], w.BOOL),
            ("SendInput", [w.UINT, c.POINTER(INPUT), c.c_int], w.UINT),
            ("GetSystemMetrics", [c.c_int], c.c_int),
        ):
            fn = getattr(u, name); fn.argtypes, fn.restype = args, result
        u.GetWindowTextW.argtypes, u.GetWindowTextW.restype = [w.HWND, w.LPWSTR, c.c_int], c.c_int
        u.SetProcessDpiAwarenessContext.argtypes, u.SetProcessDpiAwarenessContext.restype = [c.c_void_p], w.BOOL
        u.GetThreadDpiAwarenessContext.argtypes, u.GetThreadDpiAwarenessContext.restype = [], c.c_void_p
        u.GetAwarenessFromDpiAwarenessContext.argtypes = [c.c_void_p]
        u.GetAwarenessFromDpiAwarenessContext.restype = c.c_int
        u.GetDC.argtypes, u.GetDC.restype = [w.HWND], w.HDC
        u.ReleaseDC.argtypes, u.ReleaseDC.restype = [w.HWND, w.HDC], c.c_int
        u.PrintWindow.argtypes, u.PrintWindow.restype = [w.HWND, w.HDC, w.UINT], w.BOOL
        g = self.gdi32
        g.CreateCompatibleDC.argtypes, g.CreateCompatibleDC.restype = [w.HDC], w.HDC
        g.CreateCompatibleBitmap.argtypes, g.CreateCompatibleBitmap.restype = [w.HDC, c.c_int, c.c_int], w.HBITMAP
        g.SelectObject.argtypes, g.SelectObject.restype = [w.HDC, w.HGDIOBJ], w.HGDIOBJ
        g.BitBlt.argtypes = [w.HDC, c.c_int, c.c_int, c.c_int, c.c_int, w.HDC, c.c_int, c.c_int, w.DWORD]
        g.BitBlt.restype = w.BOOL
        g.GetDIBits.argtypes = [w.HDC, w.HBITMAP, w.UINT, w.UINT, c.c_void_p, c.POINTER(BITMAPINFO), w.UINT]
        g.GetDIBits.restype = c.c_int
        g.DeleteObject.argtypes, g.DeleteObject.restype = [w.HGDIOBJ], w.BOOL
        g.DeleteDC.argtypes, g.DeleteDC.restype = [w.HDC], w.BOOL
        k.OpenProcess.argtypes, k.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
        k.CloseHandle.argtypes, k.CloseHandle.restype = [w.HANDLE], w.BOOL
        k.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD)]
        k.QueryFullProcessImageNameW.restype = w.BOOL
        k.GetProcessTimes.argtypes = [w.HANDLE, c.POINTER(w.FILETIME), c.POINTER(w.FILETIME), c.POINTER(w.FILETIME), c.POINTER(w.FILETIME)]
        k.GetProcessTimes.restype = w.BOOL
        a.OpenProcessToken.argtypes, a.OpenProcessToken.restype = [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)], w.BOOL
        a.GetTokenInformation.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)]
        a.GetTokenInformation.restype = w.BOOL

    def find_windows(self, process_name: str, title: str = "") -> list[WindowTarget]:
        hwnds: list[int] = []

        @self.ENUMPROC
        def collect(hwnd, _):
            hwnds.append(int(hwnd)); return True

        self.user32.EnumWindows(collect, 0)
        matches: list[WindowTarget] = []
        for hwnd in hwnds:
            target = self.probe_window(hwnd)
            if target and target.visible and target.title and _same_process_name(process_name, target.process_name):
                if not title or target.title.casefold() == title.casefold():
                    matches.append(target)
        return matches

    def probe_window(self, hwnd: int) -> WindowTarget | None:
        c, w, u = self.c, self.w, self.user32
        if not u.IsWindow(w.HWND(hwnd)):
            return None
        pid = w.DWORD()
        u.GetWindowThreadProcessId(w.HWND(hwnd), c.byref(pid))
        if pid.value <= 0:
            return None
        length = u.GetWindowTextLengthW(w.HWND(hwnd))
        title_buf = c.create_unicode_buffer(max(1, length + 1))
        u.GetWindowTextW(w.HWND(hwnd), title_buf, len(title_buf))
        rect, origin = self.RECT(), self.POINT()
        if not u.GetClientRect(w.HWND(hwnd), c.byref(rect)) or not u.ClientToScreen(w.HWND(hwnd), c.byref(origin)):
            return None
        monitor = u.MonitorFromWindow(w.HWND(hwnd), 2)
        info = self.MONITORINFOEXW(); info.cbSize = c.sizeof(info)
        if not monitor or not u.GetMonitorInfoW(monitor, c.byref(info)):
            return None
        process_name, started, elevated = self._process_facts(int(pid.value))
        return WindowTarget(
            hwnd=int(hwnd), pid=int(pid.value), process_name=process_name, title=title_buf.value,
            display_id=str(info.szDevice), dpi_scale=round(float(u.GetDpiForWindow(w.HWND(hwnd)) or 96) / 96.0, 4),
            left=int(origin.x), top=int(origin.y), width=int(rect.right - rect.left), height=int(rect.bottom - rect.top),
            process_started=started, foreground=int(u.GetForegroundWindow() or 0) == int(hwnd),
            visible=bool(u.IsWindowVisible(w.HWND(hwnd))), minimized=bool(u.IsIconic(w.HWND(hwnd))), elevated=elevated,
        )

    def _process_facts(self, pid: int) -> tuple[str, int, bool | None]:
        c, w, k = self.c, self.w, self.kernel32
        handle = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return "", 0, None
        try:
            size = w.DWORD(32768); buf = c.create_unicode_buffer(size.value)
            name = re.split(r"[/\\]", buf.value)[-1] if k.QueryFullProcessImageNameW(handle, 0, buf, c.byref(size)) else ""
            created, exited, kernel, user = w.FILETIME(), w.FILETIME(), w.FILETIME(), w.FILETIME()
            started = ((created.dwHighDateTime << 32) | created.dwLowDateTime) if k.GetProcessTimes(handle, c.byref(created), c.byref(exited), c.byref(kernel), c.byref(user)) else 0
            token = w.HANDLE()
            elevated: bool | None = None
            if self.advapi32.OpenProcessToken(handle, 0x0008, c.byref(token)):
                try:
                    value, needed = w.DWORD(), w.DWORD()
                    if self.advapi32.GetTokenInformation(token, 20, c.byref(value), c.sizeof(value), c.byref(needed)):
                        elevated = bool(value.value)
                finally:
                    k.CloseHandle(token)
            return name, int(started), elevated
        finally:
            k.CloseHandle(handle)

    def _send(self, inputs: list) -> None:
        array = (self.INPUT * len(inputs))(*inputs)
        sent = self.user32.SendInput(len(inputs), array, self.c.sizeof(self.INPUT))
        if int(sent) != len(inputs):
            raise DesktopNativeRefused("input_delivery_incomplete", f"sent={int(sent)}/{len(inputs)}")

    def _absolute(self, x: int, y: int) -> tuple[int, int]:
        u = self.user32
        left, top = u.GetSystemMetrics(76), u.GetSystemMetrics(77)
        width, height = u.GetSystemMetrics(78), u.GetSystemMetrics(79)
        if width <= 1 or height <= 1 or not (left <= x < left + width and top <= y < top + height):
            raise DesktopNativeRefused("point_outside_virtual_desktop")
        return round((x - left) * 65535 / (width - 1)), round((y - top) * 65535 / (height - 1))

    def send_click(self, x: int, y: int, *, button: str = 'left', count: int = 1) -> None:
        if button not in {'left', 'right'} or type(count) is not int or count not in {1, 2}:
            raise DesktopNativeRefused('invalid_pointer_variant')
        nx, ny = self._absolute(x, y); extra = self.c.POINTER(self.c.c_ulong)()
        flags = 0x8000 | 0x4000 | 0x0001
        move = self.INPUT(type=0, mi=self.MOUSEINPUT(nx, ny, 0, flags, 0, extra))
        down_flag, up_flag = (0x0002, 0x0004) if button == 'left' else (0x0008, 0x0010)
        records = [move]
        for _ in range(count):
            records.extend([self.INPUT(type=0, mi=self.MOUSEINPUT(nx, ny, 0, flags | down_flag, 0, extra)),
                            self.INPUT(type=0, mi=self.MOUSEINPUT(nx, ny, 0, flags | up_flag, 0, extra))])
        self._send(records)

    def send_drag(self, x: int, y: int, to_x: int, to_y: int) -> None:
        nx, ny = self._absolute(x, y)
        end_x, end_y = self._absolute(to_x, to_y)
        extra = self.c.POINTER(self.c.c_ulong)()
        flags = 0x8000 | 0x4000 | 0x0001
        self._send([
            self.INPUT(type=0, mi=self.MOUSEINPUT(nx, ny, 0, flags, 0, extra)),
            self.INPUT(type=0, mi=self.MOUSEINPUT(nx, ny, 0, flags | 0x0002, 0, extra)),
            self.INPUT(type=0, mi=self.MOUSEINPUT(end_x, end_y, 0, flags, 0, extra)),
            self.INPUT(type=0, mi=self.MOUSEINPUT(end_x, end_y, 0, flags | 0x0004, 0, extra)),
        ])

    def send_scroll(self, x: int, y: int, delta: int, *, horizontal: bool = False) -> None:
        nx, ny = self._absolute(x, y); extra = self.c.POINTER(self.c.c_ulong)()
        flags = 0x8000 | 0x4000 | 0x0001
        self._send([
            self.INPUT(type=0, mi=self.MOUSEINPUT(nx, ny, 0, flags, 0, extra)),
            self.INPUT(type=0, mi=self.MOUSEINPUT(nx, ny, int(delta) & 0xFFFFFFFF,
                0x1000 if horizontal else 0x0800, 0, extra)),
        ])

    def send_text(self, text: str) -> None:
        extra = self.c.POINTER(self.c.c_ulong)(); inputs = []
        raw = str(text).encode("utf-16-le")
        for index in range(0, len(raw), 2):
            unit = int.from_bytes(raw[index:index + 2], "little")
            inputs.append(self.INPUT(type=1, ki=self.KEYBDINPUT(0, unit, 0x0004, 0, extra)))
            inputs.append(self.INPUT(type=1, ki=self.KEYBDINPUT(0, unit, 0x0004 | 0x0002, 0, extra)))
        if inputs:
            self._send(inputs)

    def capture_bgra(self, hwnd: int, width: int, height: int) -> bytes:
        c, w, u, g = self.c, self.w, self.user32, self.gdi32
        source = u.GetDC(w.HWND(hwnd))
        if not source:
            raise DesktopNativeRefused("capture_dc_unavailable")
        memory = bitmap = old = None
        try:
            memory = g.CreateCompatibleDC(source)
            bitmap = g.CreateCompatibleBitmap(source, width, height)
            if not memory or not bitmap:
                raise DesktopNativeRefused("capture_surface_unavailable")
            old = g.SelectObject(memory, bitmap)
            painted = bool(u.PrintWindow(w.HWND(hwnd), memory, 0x00000003))
            if not painted:
                painted = bool(g.BitBlt(memory, 0, 0, width, height, source, 0, 0, 0x00CC0020))
            if not painted:
                raise DesktopNativeRefused("capture_failed")
            info = self.BITMAPINFO()
            info.bmiHeader = self.BITMAPINFOHEADER(
                c.sizeof(self.BITMAPINFOHEADER), width, height, 1, 32, 0,
                width * height * 4, 0, 0, 0, 0,
            )
            buffer = (c.c_ubyte * (width * height * 4))()
            lines = g.GetDIBits(memory, bitmap, 0, height, buffer, c.byref(info), 0)
            if int(lines) != height:
                raise DesktopNativeRefused("capture_pixels_incomplete")
            return bytes(buffer)
        finally:
            if old and memory:
                g.SelectObject(memory, old)
            if bitmap:
                g.DeleteObject(bitmap)
            if memory:
                g.DeleteDC(memory)
            u.ReleaseDC(w.HWND(hwnd), source)
