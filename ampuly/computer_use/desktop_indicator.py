"""Local, attempt-scoped notice for real Windows computer use.

This is a visibility/input fence, never an execution permission. It opens no
browser or network endpoint, reads no screen, and cannot approve an Effect.
Physical controls can only reduce the existing desktop authority.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from threading import Event, RLock
import uuid


class IndicatorUnavailable(PermissionError):
    code = 'desktop_indicator_unavailable'


@dataclass
class _Lease:
    key: str
    task: object
    node: str
    loop: object
    blocked: Event = field(default_factory=Event)
    stopped: Event = field(default_factory=Event)
    pending_control: Event = field(default_factory=Event)
    scope_key: str = ''


class IndicatorHost:
    """At most one native display thread per Engine process.

    Late clicks/cleanup keep their displayed lease key; they cannot stop or
    hide a newer attempt. The original desktop-operation lock serializes use.
    """
    def __init__(self, renderer_factory=None):
        self._factory = renderer_factory
        self._renderer = None
        self._rows = {}
        self._active = None
        self._lock = RLock()

    def show(self, lease, text, callback):
        with self._lock:
            if lease.key not in self._rows and len(self._rows) >= 64:
                raise IndicatorUnavailable('desktop_indicator_capacity')
            self._rows[lease.key] = (lease, callback)
            if self._renderer is None:
                factory = self._factory
                if factory is None:
                    from ampuly.computer_use.windows_desktop_indicator import WindowsDesktopIndicator
                    factory = WindowsDesktopIndicator
                self._renderer = factory(self._control)
            self._active = lease.key
            renderer = self._renderer
        try:
            renderer.show(lease.key, text)
            if not self.visible(lease):
                raise IndicatorUnavailable('desktop_indicator_unavailable')
        except Exception as exc:
            with self._lock:
                lease.blocked.set()
                if self._active == lease.key:
                    self._active = None
            raise IndicatorUnavailable('desktop_indicator_unavailable') from exc

    def _control(self, key, action):
        if action not in {'takeover', 'stop'}:
            return
        with self._lock:
            row = self._rows.get(key)
            if self._active != key or row is None:
                return
            lease, callback = row
            if lease.pending_control.is_set():
                return
            # Synchronous thread-safe fence before the event loop handles the
            # original lease transition. No more input can be admitted meanwhile.
            lease.blocked.set()
            lease.pending_control.set()
            if action == 'stop':
                lease.stopped.set()
        callback(action)

    def visible(self, lease):
        with self._lock:
            return (self._active == lease.key and self._renderer is not None
                    and self._renderer.healthy and lease.key in self._rows)

    def pause(self, lease):
        with self._lock:
            lease.blocked.set()
            if self._active == lease.key:
                self._active = None
                renderer = self._renderer
            else:
                renderer = None
        if renderer is not None:
            try:
                renderer.hide()
            except Exception:
                # A timed-out display may still own windows. Close only this
                # renderer; never leave an uncloseable notice thread behind.
                renderer.close()

    def release(self, lease):
        self.pause(lease)
        with self._lock:
            self._rows.pop(lease.key, None)
            if not self._rows and self._renderer is not None:
                renderer = self._renderer
            else:
                renderer = None
        if renderer is not None:
            renderer.close()
            with self._lock:
                if self._renderer is renderer:
                    self._renderer = None


_HOST = IndicatorHost()


class DesktopOperationIndicator:
    def __init__(self, manager, *, host=None):
        self.manager = manager
        self.host = _HOST if host is None else host
        self._leases = {}

    def _current(self, task):
        key, _apps = self.manager._scope(task)
        attempt = self.manager._lifecycle.observed_owner(task, key)
        if not attempt:
            raise IndicatorUnavailable('desktop_indicator_attempt_unavailable')
        return attempt

    def before_operation(self, task, operation):
        from ampuly.computer_use import desktop_bridge
        from ampuly.plugins.office_ui.desktop_connector import _step
        attempt = self._current(task)
        scope_key = self.manager._scope(task)[0]
        lease = self._leases.get(attempt)
        if lease is None:
            if len(self._leases) >= 64:
                raise IndicatorUnavailable('desktop_indicator_capacity')
            lease = _Lease(uuid.uuid4().hex, task, _step(task)[1], asyncio.get_running_loop(), scope_key=scope_key)
            self._leases[attempt] = lease
        if lease.stopped.is_set():
            raise IndicatorUnavailable('desktop_stopped_by_local_user')
        if lease.pending_control.is_set():
            raise IndicatorUnavailable('desktop_local_control_pending')
        if lease.scope_key != scope_key:
            raise IndicatorUnavailable('desktop_indicator_scope_changed')
        control = desktop_bridge.input_block(task, lease.node)
        if control == 'agent_not_in_control':
            self.host.pause(lease)
            return
        if control and operation != 'desktop_observe':
            raise IndicatorUnavailable(control)
        # Fresh-look observations may run with the original input hold. They
        # show a viewing notice but cannot authorize keyboard/mouse input.
        title = ' '.join(str(getattr(task, 'title', '') or '').split())[:72]
        phase = '正在查看你的电脑' if operation == 'desktop_observe' else '正在操作你的电脑'
        text = f'Ampuly {phase}' + (f' · {title}' if title else '')
        def clicked(action):
            try:
                lease.loop.call_soon_threadsafe(self._apply_control, attempt, lease, action)
            except RuntimeError:
                lease.blocked.set()
        if not control:
            lease.blocked.clear()
        else:
            lease.blocked.set()
        self.host.show(lease, text, clicked)

    def after_operation(self, task):
        from ampuly.computer_use import desktop_bridge
        attempt = self._current(task)
        lease = self._leases.get(attempt)
        if lease is not None and not lease.stopped.is_set() and not lease.pending_control.is_set():
            if desktop_bridge.agent_may_input(task, lease.node) and self.host.visible(lease):
                lease.blocked.clear()
            else:
                self.host.pause(lease)

    def _apply_control(self, attempt, lease, action):
        from ampuly.computer_use import desktop_bridge
        if self._leases.get(attempt) is not lease:
            return
        self.manager._lifecycle.hold_input('local_desktop_' + action)
        self.manager.ui_frames.revoke_task(getattr(lease.task, 'id', None))
        try:
            desktop_bridge.request_takeover(lease.task, lease.node, source='native_indicator_' + action)
        finally:
            self.host.pause(lease)
            lease.pending_control.clear()
        # Stop means stop this attempt's COMPUTER operations, not silently
        # mark a business task complete/cancelled or approve a business action.
        # The event is not cleared by Hand Back; a new attempt is required.

    def input_allowed(self, task):
        try:
            lease = self._leases.get(self._current(task))
            return (lease is not None and not lease.blocked.is_set()
                    and lease.scope_key == self.manager._scope(task)[0]
                    and not lease.stopped.is_set() and not lease.pending_control.is_set()
                    and self.host.visible(lease))
        except IndicatorUnavailable:
            return False

    def pause_task(self, task_id):
        for lease in tuple(self._leases.values()):
            if getattr(lease.task, 'id', None) == task_id:
                self.host.pause(lease)

    def revoke_attempt(self, attempt):
        lease = self._leases.get(attempt)
        if lease is not None:
            self.host.release(lease)
            if self._leases.get(attempt) is lease:
                self._leases.pop(attempt)

    def close(self):
        for attempt in tuple(self._leases):
            self.revoke_attempt(attempt)
