"""Blue desktop edges and a non-activating local control bar (Windows only).

Written for Ampuly using documented Win32 APIs. No vendor executable, addon,
hook, configuration or user profile is loaded. One bounded native UI thread;
all windows/resources are private to it. Border windows are click-through.
"""
from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
import math
import os
import queue
from threading import Event, Thread
import time
import uuid


class WindowsDesktopIndicator:
    def __init__(self, on_control):
        self._on_control = on_control
        self._queue = queue.Queue(maxsize=16)
        self._ready, self._shown = Event(), Event()
        self._thread = None
        self._thread_id = 0
        self._failed = False
        self.failure_type = ''

    @property
    def healthy(self):
        return (not self._failed and self._shown.is_set()
                and self._thread is not None and self._thread.is_alive())

    def _request(self, operation, *args):
        if os.name != 'nt' or (self._failed and operation != 'close'):
            raise RuntimeError('desktop_indicator_unavailable')
        if self._thread is None:
            if operation != 'show':
                return
            self._thread = Thread(target=self._run, name='ampuly-desktop-indicator', daemon=True)
            self._thread.start()
        if not self._ready.wait(2) or not self._thread.is_alive():
            self._failed = True
            raise RuntimeError('desktop_indicator_start_failed')
        done, failure = Event(), []
        self._queue.put_nowait((operation, args, done, failure))
        if not self._post(self._thread_id) or not done.wait(2) or failure:
            self._failed = True
            self._shown.clear()
            raise RuntimeError('desktop_indicator_command_failed')

    def show(self, key, text):
        if type(key) is not str or len(key) != 32 or type(text) is not str or len(text) > 160:
            raise ValueError('desktop_indicator_invalid_notice')
        self._request('show', key, text)

    def hide(self):
        self._request('hide')

    def close(self):
        if self._thread is None:
            return
        if not self._thread.is_alive():
            self._shown.clear()
            return
        self._request('close')
        self._thread.join(2)
        if self._thread.is_alive():
            self._failed = True
            raise RuntimeError('desktop_indicator_close_incomplete')

    def _run(self):
        native = None
        try:
            native = _NativeDisplay(self._on_control, self._shown)
            self._thread_id = native.k.GetCurrentThreadId()
            self._post = lambda thread: bool(native.u.PostThreadMessageW(thread, 0x8001, 0, 0))
            message = w.MSG()
            native.u.PeekMessageW(c.byref(message), None, 0, 0, 0)
            self._ready.set()
            running = True
            while running:
                status = native.u.GetMessageW(c.byref(message), None, 0, 0)
                if status <= 0:
                    break
                if message.message == 0x8001:
                    while not self._queue.empty():
                        operation, args, done, failure = self._queue.get_nowait()
                        try:
                            if operation == 'show':
                                native.show(*args)
                            elif operation == 'hide':
                                native.hide()
                            else:
                                native.hide()
                                running = False
                        except Exception as exc:
                            self.failure_type = type(exc).__name__
                            failure.append('native_display_failed')
                            self._failed = True
                            self._shown.clear()
                            running = False
                        finally:
                            done.set()
                else:
                    native.u.TranslateMessage(c.byref(message))
                    native.u.DispatchMessageW(c.byref(message))
        except Exception as exc:
            self.failure_type = type(exc).__name__
            self._failed = True
        finally:
            self._shown.clear()
            if native is not None:
                native.close()
            self._ready.set()


def edge_pixels(width, height, side):
    """Premultiplied BGRA, transparent toward the inside; bounded strip only."""
    if (type(width) is not int or type(height) is not int or not 0 < width <= 32768
            or not 0 < height <= 32768 or min(width, height) > 24
            or width * height > 24 * 32768 or side not in {'left', 'right', 'top', 'bottom'}):
        raise ValueError('desktop_indicator_invalid_edge')
    size = width if side in {'left', 'right'} else height
    def pixel(index):
        distance = index if side in {'left', 'top'} else size - index - 1
        alpha = round(210 * (1 - distance / size) ** 2)
        return bytes((alpha, round(0.53 * alpha), round(0.18 * alpha), alpha))
    if side in {'left', 'right'}:
        return b''.join(pixel(x) for x in range(width)) * height
    return b''.join(pixel(y) * width for y in range(height))


class _NativeDisplay:
    def __init__(self, on_control, shown):
        self.on_control, self.shown = on_control, shown
        self.u = c.WinDLL('user32', use_last_error=True)
        self.g = c.WinDLL('gdi32', use_last_error=True)
        self.k = c.WinDLL('kernel32', use_last_error=True)
        self.windows, self.surfaces = [], []
        self.bar, self.key, self.text, self.font = None, '', '', None
        self._hiding = False
        self.dpi_previous = None
        self._signatures()
        self.instance = self.k.GetModuleHandleW(None)
        self.class_name = 'AmpulyDesktopNotice-' + uuid.uuid4().hex
        self._procedure = self.WNDPROC(self._window_proc)
        definition = self.WNDCLASS()
        definition.lpfnWndProc = self._procedure
        definition.hInstance = self.instance
        definition.lpszClassName = self.class_name
        if not self.u.RegisterClassW(c.byref(definition)):
            raise OSError('desktop_indicator_class_failed')
        if hasattr(self.u, 'SetThreadDpiAwarenessContext'):
            self.dpi_previous = self.u.SetThreadDpiAwarenessContext(c.c_void_p(-4))

    def _signatures(self):
        self.WNDPROC = c.WINFUNCTYPE(c.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
        class WNDCLASS(c.Structure):
            _fields_ = [('style', w.UINT), ('lpfnWndProc', self.WNDPROC), ('cbClsExtra', c.c_int),
                ('cbWndExtra', c.c_int), ('hInstance', w.HINSTANCE), ('hIcon', w.HICON),
                ('hCursor', w.HANDLE), ('hbrBackground', w.HBRUSH), ('lpszMenuName', w.LPCWSTR), ('lpszClassName', w.LPCWSTR)]
        class PAINT(c.Structure):
            _fields_ = [('hdc', w.HDC), ('erase', w.BOOL), ('rect', w.RECT), ('restore', w.BOOL),
                ('update', w.BOOL), ('reserved', c.c_byte * 32)]
        class BITMAP(c.Structure):
            _fields_ = [('size', w.DWORD), ('width', c.c_long), ('height', c.c_long), ('planes', w.WORD),
                ('bits', w.WORD), ('compression', w.DWORD), ('bytes', w.DWORD), ('xppm', c.c_long),
                ('yppm', c.c_long), ('colors', w.DWORD), ('important', w.DWORD), ('palette', w.DWORD * 3)]
        class BLEND(c.Structure):
            _fields_ = [('operation', c.c_byte), ('flags', c.c_byte), ('alpha', c.c_ubyte), ('format', c.c_byte)]
        self.WNDCLASS, self.PAINT, self.BITMAP, self.BLEND = WNDCLASS, PAINT, BITMAP, BLEND
        for name, result, args in [
            ('RegisterClassW', w.ATOM, [c.POINTER(WNDCLASS)]),
            ('UnregisterClassW', w.BOOL, [w.LPCWSTR, w.HINSTANCE]),
            ('CreateWindowExW', w.HWND, [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, c.c_int, c.c_int,
                c.c_int, c.c_int, w.HWND, w.HMENU, w.HINSTANCE, c.c_void_p]),
            ('DefWindowProcW', c.c_ssize_t, [w.HWND, w.UINT, w.WPARAM, w.LPARAM]),
            ('DestroyWindow', w.BOOL, [w.HWND]), ('ShowWindow', w.BOOL, [w.HWND, c.c_int]),
            ('IsWindow', w.BOOL, [w.HWND]), ('GetClientRect', w.BOOL, [w.HWND, c.POINTER(w.RECT)]),
            ('BeginPaint', w.HDC, [w.HWND, c.POINTER(PAINT)]), ('EndPaint', w.BOOL, [w.HWND, c.POINTER(PAINT)]),
            ('FillRect', c.c_int, [w.HDC, c.POINTER(w.RECT), w.HBRUSH]),
            ('DrawTextW', c.c_int, [w.HDC, w.LPCWSTR, c.c_int, c.POINTER(w.RECT), w.UINT]),
            ('InvalidateRect', w.BOOL, [w.HWND, c.c_void_p, w.BOOL]),
            ('SetTimer', c.c_size_t, [w.HWND, c.c_size_t, w.UINT, c.c_void_p]),
            ('KillTimer', w.BOOL, [w.HWND, c.c_size_t]),
            ('PostThreadMessageW', w.BOOL, [w.DWORD, w.UINT, w.WPARAM, w.LPARAM]),
            ('GetMessageW', c.c_int, [c.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]),
            ('PeekMessageW', w.BOOL, [c.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT]),
            ('TranslateMessage', w.BOOL, [c.POINTER(w.MSG)]), ('DispatchMessageW', c.c_ssize_t, [c.POINTER(w.MSG)]),
            ('SendMessageW', c.c_ssize_t, [w.HWND, w.UINT, w.WPARAM, w.LPARAM]),
            ('SetWindowDisplayAffinity', w.BOOL, [w.HWND, w.DWORD]),
            ('UpdateLayeredWindow', w.BOOL, [w.HWND, w.HDC, c.POINTER(w.POINT), c.POINTER(w.SIZE),
                w.HDC, c.POINTER(w.POINT), w.DWORD, c.POINTER(BLEND), w.DWORD]),
        ]:
            function = getattr(self.u, name);function.restype = result;function.argtypes = args
        self.k.GetModuleHandleW.argtypes, self.k.GetModuleHandleW.restype = [w.LPCWSTR], w.HMODULE
        self.k.GetCurrentThreadId.restype = w.DWORD
        if hasattr(self.u, 'SetThreadDpiAwarenessContext'):
            self.u.SetThreadDpiAwarenessContext.argtypes = [c.c_void_p]
            self.u.SetThreadDpiAwarenessContext.restype = c.c_void_p
        for name, result, args in [
            ('CreateCompatibleDC', w.HDC, [w.HDC]), ('DeleteDC', w.BOOL, [w.HDC]),
            ('SelectObject', w.HANDLE, [w.HDC, w.HANDLE]), ('DeleteObject', w.BOOL, [w.HANDLE]),
            ('CreateSolidBrush', w.HBRUSH, [w.DWORD]), ('SetTextColor', w.DWORD, [w.HDC, w.DWORD]),
            ('SetBkMode', c.c_int, [w.HDC, c.c_int]),
            ('CreateDIBSection', w.HBITMAP, [w.HDC, c.POINTER(BITMAP), w.UINT, c.POINTER(c.c_void_p), w.HANDLE, w.DWORD]),
            ('CreateFontW', w.HANDLE, [c.c_int]*5+[w.DWORD]*8+[w.LPCWSTR]),
        ]:
            function = getattr(self.g, name);function.restype = result;function.argtypes = args

    def _monitors(self):
        result = []
        class INFO(c.Structure):
            _fields_ = [('size', w.DWORD), ('monitor', w.RECT), ('work', w.RECT), ('flags', w.DWORD)]
        callback_type = c.WINFUNCTYPE(w.BOOL, w.HANDLE, w.HDC, c.POINTER(w.RECT), w.LPARAM)
        self.u.GetMonitorInfoW.argtypes, self.u.GetMonitorInfoW.restype = [w.HANDLE, c.POINTER(INFO)], w.BOOL
        def visit(handle, _dc, _rect, _data):
            info = INFO();info.size = c.sizeof(INFO)
            if not self.u.GetMonitorInfoW(handle, c.byref(info)):
                return False
            rect = info.monitor
            result.append((rect.left, rect.top, rect.right-rect.left, rect.bottom-rect.top, bool(info.flags & 1)))
            return len(result) <= 8
        callback = callback_type(visit)
        self.u.EnumDisplayMonitors.argtypes, self.u.EnumDisplayMonitors.restype = [w.HDC, c.c_void_p, callback_type, w.LPARAM], w.BOOL
        if not self.u.EnumDisplayMonitors(None, None, callback, 0) or not 1 <= len(result) <= 8:
            raise OSError('desktop_indicator_monitors_unavailable')
        if any(not 100 <= width <= 32768 or not 100 <= height <= 32768 for _, _, width, height, _ in result):
            raise OSError('desktop_indicator_monitor_bounds')
        return result

    def _window(self, title, x, y, width, height, *, edge=False):
        # NOACTIVATE and transparent borders preserve the controlled app's
        # foreground identity. Only the status bar's real buttons accept input.
        flags = 0x08000000 | 0x00000080 | 0x00000008
        if edge:flags |= 0x00080000 | 0x00000020
        hwnd = self.u.CreateWindowExW(flags, self.class_name, title, 0x80000000,
            x, y, width, height, None, None, self.instance, None)
        if not hwnd:raise OSError('desktop_indicator_window_failed')
        self.windows.append(hwnd)
        # Best effort for old Windows versions; captures still target only the
        # original task HWND. This is a property of our own windows, not policy.
        self.u.SetWindowDisplayAffinity(hwnd, 0x11)
        return hwnd

    def _edge(self, x, y, width, height, side):
        hwnd = self._window('Ampuly computer-use border', x, y, width, height, edge=True)
        dc = self.g.CreateCompatibleDC(None)
        if not dc:raise OSError('desktop_indicator_surface_failed')
        bitmap = self.BITMAP();bitmap.size = 40;bitmap.width = width;bitmap.height = -height
        bitmap.planes = 1;bitmap.bits = 32
        pointer = c.c_void_p()
        handle = self.g.CreateDIBSection(dc, c.byref(bitmap), 0, c.byref(pointer), None, 0)
        if not handle:
            self.g.DeleteDC(dc);raise OSError('desktop_indicator_bitmap_failed')
        old = self.g.SelectObject(dc, handle)
        self.surfaces.append((hwnd, dc, handle, old, x, y, width, height))
        data = edge_pixels(width, height, side);c.memmove(pointer, data, len(data))

    def show(self, key, text):
        if self.key == key and self.bar and self.shown.is_set():
            self.text = text;self.u.InvalidateRect(self.bar, None, False);return
        self.hide();self.key, self.text = key, text
        monitors = self._monitors()
        for x, y, width, height, _primary in monitors:
            self._edge(x, y, 24, height, 'left')
            self._edge(x+width-24, y, 24, height, 'right')
            self._edge(x, y, width, 24, 'top')
            self._edge(x, y+height-24, width, 24, 'bottom')
        x, y, width, _height, _primary = next((m for m in monitors if m[4]), monitors[0])
        bar_width = min(760, width-24)
        self.bar = self._window('Ampuly 正在操作你的电脑', x+(width-bar_width)//2, y+12, bar_width, 56)
        self.font = self.g.CreateFontW(-16, 0, 0, 0, 500, 0, 0, 0, 1, 0, 0, 4, 0, 'Microsoft YaHei UI')
        for identity, label, left, button_width in [(1001, '立即接管', bar_width-240, 100), (1002, '停止电脑操作', bar_width-132, 120)]:
            button = self.u.CreateWindowExW(0x08000000, 'BUTTON', label, 0x50008000,
                left, 10, button_width, 36, self.bar, w.HMENU(identity), self.instance, None)
            if not button:raise OSError('desktop_indicator_control_failed')
            self.u.SendMessageW(button, 0x0030, self.font or 0, 1)
        self._pulse()
        self.u.ShowWindow(self.bar, 4)
        if not self.u.SetTimer(self.bar, 1, 160, None):raise OSError('desktop_indicator_timer_failed')
        self.shown.set()

    def _pulse(self):
        alpha = round(205+40*math.sin(time.monotonic()*2*math.pi/4.5))
        for hwnd, dc, _bitmap, _old, x, y, width, height in self.surfaces:
            blend = self.BLEND(0, 0, alpha, 1)
            if not self.u.UpdateLayeredWindow(hwnd, None, c.byref(w.POINT(x, y)), c.byref(w.SIZE(width, height)),
                    dc, c.byref(w.POINT(0, 0)), 0, c.byref(blend), 2):
                raise OSError('desktop_indicator_update_failed')
            self.u.ShowWindow(hwnd, 4)

    def _window_proc(self, hwnd, message, wp, lp):
        try:
            if message == 0x0002 and not self._hiding and self.key:  # WM_DESTROY
                self.shown.clear();self.on_control(self.key, 'takeover');return 0
            if message == 0x0021:return 3  # MA_NOACTIVATE
            if message == 0x0084 and hwnd != self.bar:return -1  # HTTRANSPARENT
            if hwnd == self.bar and message == 0x0111 and (wp & 0xffff) in {1001, 1002}:
                self.on_control(self.key, 'takeover' if (wp & 0xffff) == 1001 else 'stop');return 0
            if hwnd == self.bar and message == 0x0010:
                self.on_control(self.key, 'takeover');return 0
            if hwnd == self.bar and message == 0x0113:
                if not all(self.u.IsWindow(window) for window in self.windows):
                    raise OSError('desktop_indicator_window_lost')
                self._pulse();return 0
            if hwnd == self.bar and message == 0x007e:  # display topology changed
                self.shown.clear();self.on_control(self.key, 'takeover');return 0
            if hwnd == self.bar and message == 0x000f:
                paint = self.PAINT();dc = self.u.BeginPaint(hwnd, c.byref(paint));brush = None;old_font = None
                try:
                    rect = w.RECT();self.u.GetClientRect(hwnd, c.byref(rect))
                    brush = self.g.CreateSolidBrush(0x00291c12);self.u.FillRect(dc, c.byref(rect), brush)
                    self.g.SetBkMode(dc, 1);self.g.SetTextColor(dc, 0x00fff5eb)
                    if self.font:old_font = self.g.SelectObject(dc, self.font)
                    rect.left = 16;rect.right = max(20, rect.right-254)
                    self.u.DrawTextW(dc, self.text, -1, c.byref(rect), 0x0004|0x0020|0x8000)
                finally:
                    if old_font:self.g.SelectObject(dc, old_font)
                    if brush:self.g.DeleteObject(brush)
                    self.u.EndPaint(hwnd, c.byref(paint))
                return 0
        except Exception:
            self.shown.clear()
            if self.key:self.on_control(self.key, 'takeover')
            return 0
        return self.u.DefWindowProcW(hwnd, message, wp, lp)

    def hide(self):
        self.shown.clear()
        self._hiding = True
        if self.bar:self.u.KillTimer(self.bar, 1)
        for hwnd in reversed(self.windows):self.u.DestroyWindow(hwnd)
        self.windows.clear();self.bar = None
        for _hwnd, dc, bitmap, old, *_rest in self.surfaces:
            self.g.SelectObject(dc, old);self.g.DeleteObject(bitmap);self.g.DeleteDC(dc)
        self.surfaces.clear()
        if self.font:self.g.DeleteObject(self.font);self.font = None
        self.key = ''
        self._hiding = False

    def close(self):
        self.hide()
        self.u.UnregisterClassW(self.class_name, self.instance)
        if self.dpi_previous:self.u.SetThreadDpiAwarenessContext(self.dpi_previous)
