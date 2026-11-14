"""Opt-in owner-console projection of future, already-authorized observations.

Not a capture API, model disclosure grant, input capability or durable ledger.
Only the original runtime can publish a PNG; HTTP reads never start a helper.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import secrets
import time

from ampuly.computer_use.contracts import Geometry
from ampuly.computer_use.desktop_frames import FrameRefused, clean_masks, validate_png

VIEW_TTL = 60.0
FRAME_TTL = 2.0
MAX_VIEWS = 4
MAX_OBSERVATIONS = 32


class DesktopViewRefused(PermissionError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass
class _View:
    viewer: tuple
    anchor: tuple
    fresh_viewer: object
    expires_at: float
    timer: object
    sequence: int
    count: int = 0
    png: bytes | None = None
    produced_at: float = 0.0
    width: int = 0
    height: int = 0


class DesktopUiFrames:
    def __init__(self, manager, *, clock=time.time):
        self.manager, self.clock = manager, clock
        self._observed, self._views = {}, {}
        self._sequence = 0

    def _anchor(self, task, geometry):
        from ampuly.computer_use import desktop_bridge
        key, _apps = self.manager._scope(task)
        if not self.manager.enabled():
            raise DesktopViewRefused('desktop_view_channel_closed')
        switch = self.manager.read_switch('desktop_channel')
        if (type(switch) is not dict or switch.get('connector') != 'desktop_channel'
                or type(switch.get('actor')) is not str or not switch['actor']
                or type(switch.get('at')) is not str or not switch['at']):
            raise DesktopViewRefused('desktop_view_channel_unanchored')
        revision = hashlib.sha256(json.dumps({k: switch[k] for k in
            ('connector', 'explicit', 'enabled', 'actor', 'at')}, sort_keys=True).encode()).hexdigest()
        attempt = self.manager._lifecycle.observed_owner(task, key)
        lease = desktop_bridge.readonly_lease(task)
        if attempt is None or lease is None or not geometry.matches(lease['geometry']):
            raise DesktopViewRefused('desktop_view_no_active_window')
        # Geometry expiry is a coordinate lease, not a reason to extend a UI grant.
        return (key, attempt, lease['identity'], geometry.window_id, geometry.display_id,
                geometry.dpi_scale, geometry.width, geometry.height, geometry.origin_x, geometry.origin_y, revision)

    def _live(self, task, view):
        observed = self._observed.get(getattr(task, 'id', None))
        if (not self.clock() < view.expires_at or observed is None
                or view.fresh_viewer() is not True
                or self._anchor(task, observed[0]) != view.anchor):
            raise DesktopViewRefused('desktop_view_expired_or_changed')
        return observed

    def open(self, task, viewer, fresh_viewer):
        if type(viewer) is not tuple or not viewer or not callable(fresh_viewer) or fresh_viewer() is not True:
            raise DesktopViewRefused('desktop_view_owner_required')
        self._prune()
        observed = self._observed.get(getattr(task, 'id', None))
        if observed is None:
            raise DesktopViewRefused('desktop_view_wait_for_observation')
        anchor = self._anchor(task, observed[0])
        # A click starts a new, empty view. Never replay an earlier screenshot.
        for view_id, row in list(self._views.items()):
            if row.viewer == viewer:
                self.close(view_id, viewer)
        if len(self._views) >= MAX_VIEWS:
            raise DesktopViewRefused('desktop_view_capacity')
        view_id = secrets.token_hex(16)
        timer = asyncio.get_running_loop().call_later(VIEW_TTL, self._expire, view_id)
        expires = self.clock() + VIEW_TTL
        self._views[view_id] = _View(viewer, anchor, fresh_viewer, expires, timer, self._sequence)
        return {'view_id': view_id, 'expires_at': expires, 'frame_ttl_seconds': FRAME_TTL,
                'max_observations': MAX_OBSERVATIONS, 'read_only': True, 'status': 'waiting_for_new_observation'}

    def observe(self, task, geometry, png, masks):
        """Called only after the original model frame vault accepted this capture."""
        try:
            validate_png(png, geometry)
            clean_masks(masks, geometry)
            anchor = self._anchor(task, geometry)
            self._sequence += 1
            task_id = task.id
            previous = self._observed.get(task_id)
            if previous is not None and previous[1] != anchor:
                self.revoke_task(task_id)
            # No PNG in this metadata. At most the runtime's existing 64 scopes.
            if task_id not in self._observed and len(self._observed) >= 64:
                return
            self._observed[task_id] = (geometry, anchor)
            self._prune()
            for view_id, row in list(self._views.items()):
                if row.anchor[0:2] != anchor[0:2]:
                    continue
                try:
                    self._live(task, row)
                except Exception:
                    self._expire(view_id)
                    continue
                if row.count >= MAX_OBSERVATIONS:
                    self._expire(view_id)
                    continue
                # Observation-driven, bounded at 1 Hz, never a second capture loop.
                if row.png is not None and self.clock() - row.produced_at < 1.0:
                    continue
                row.png, row.produced_at = png, self.clock()
                row.width, row.height = geometry.width, geometry.height
                row.sequence, row.count = self._sequence, row.count + 1
        except Exception:
            # Failure of an optional UI projection cannot change model execution.
            self.revoke_task(getattr(task, 'id', None))

    def read(self, task, view_id, viewer):
        self._prune()
        row = self._views.get(view_id)
        if row is None or row.viewer != viewer:
            raise DesktopViewRefused('desktop_view_unavailable')
        try:
            self._live(task, row)
        except Exception:
            self._expire(view_id)
            raise DesktopViewRefused('desktop_view_expired_or_changed') from None
        if row.png is None or not 0 <= self.clock() - row.produced_at < FRAME_TTL:
            # Clear stale pixels instead of presenting a retained frame as current.
            row.png = None
            return None
        return (row.png, {'sequence': row.sequence, 'produced_at': row.produced_at,
                          'width': row.width, 'height': row.height,
                          'remaining_ms': max(0, int((row.produced_at + FRAME_TTL - self.clock()) * 1000)),
                          'sha256': hashlib.sha256(row.png).hexdigest(), 'expires_at': row.expires_at})

    def close(self, view_id, viewer):
        row = self._views.get(view_id)
        if row is not None and row.viewer == viewer:
            self._expire(view_id)

    def _expire(self, view_id):
        row = self._views.pop(view_id, None)
        if row is not None:
            row.timer.cancel()
            row.png = None

    def _prune(self):
        for view_id, row in list(self._views.items()):
            if not self.clock() < row.expires_at:
                self._expire(view_id)

    def revoke_task(self, task_id):
        observed = self._observed.pop(task_id, None)
        if observed is not None:
            for view_id, row in list(self._views.items()):
                if row.anchor[0:2] == observed[1][0:2]:
                    self._expire(view_id)

    def revoke_attempt(self, attempt):
        for task_id, (_geometry, anchor) in list(self._observed.items()):
            if anchor[1] == attempt:
                self.revoke_task(task_id)

    def close_all(self):
        for view_id in list(self._views):
            self._expire(view_id)
        self._observed.clear()
