"""One-action Windows Desktop Helper worker.

The parent owns authorization and secrets.  This process receives one bounded
action, proves the current window geometry, waits for the parent's execute
grant, emits an ``input_starting`` boundary, performs one atomic driver call,
and exits.  It has no shell/clipboard/file-dialog vocabulary.
"""
from __future__ import annotations

import base64
import json
import sys
import re
import time
from dataclasses import asdict
from typing import Any

from ampuly.computer_use.contracts import Geometry
from ampuly.computer_use.windows_desktop_native import (
    DesktopNativeRefused,
    NativeDesktopWin32Api,
    WindowsDesktopDriver,
)
from ampuly.computer_use.windows_restricted_token import restrict_current_thread

PROTOCOL = "superones.desktop-helper.v1"


def _write(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _read() -> dict[str, Any]:
    line = sys.stdin.readline(65537)
    if not line or len(line) > 65536:
        raise DesktopNativeRefused("worker_protocol_input_invalid")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise DesktopNativeRefused("worker_protocol_input_invalid")
    return value


def observe_only(initial):
    """One restricted read; never accepts coordinates, a secret or execute."""
    if set(initial) != {'protocol', 'mode', 'allowed_apps', 'expected_window_id'}:
        raise DesktopNativeRefused('worker_observation_invalid')
    apps, expected = initial['allowed_apps'], initial['expected_window_id']
    if (type(apps) is not list or not 1 <= len(apps) <= 8
            or any(type(app) is not str or not 1 <= len(app) <= 80
                or any(c in '\\/:*?"<>|\r\n\t' or ord(c) < 32 for c in app) for app in apps)
            or type(expected) is not str or expected and
                re.fullmatch(r'hwnd:[1-9][0-9]*:pid:[1-9][0-9]*:started:[1-9][0-9]*', expected) is None):
        raise DesktopNativeRefused('worker_observation_invalid')
    restrict_current_thread()
    api = NativeDesktopWin32Api()
    driver = WindowsDesktopDriver(api)
    matches = [target for app in apps for target in api.find_windows(app) if target.foreground]
    if len(matches) != 1:
        raise DesktopNativeRefused('window_not_unique')
    target = driver.observe(matches[0].process_name, matches[0].title)
    if expected and target.identity != expected:
        raise DesktopNativeRefused('window_identity_changed')
    if not 0 < target.width * target.height <= 4_194_304:
        raise DesktopNativeRefused('capture_size_limit')
    geometry = target.geometry(expires_at=time.time() + 5.0)
    screen = driver.capture(geometry)
    after = api.probe_window(target.hwnd)
    if after is None or after.identity != target.identity or after.title != target.title:
        raise DesktopNativeRefused('desktop_snapshot_changed')
    _write({'protocol': PROTOCOL, 'phase': 'observation', 'restricted_token': True,
        'app': target.process_name, 'title': target.title[:200],
        'geometry': asdict(geometry), 'screen_hash': screen.sha256})


def capture_only(initial):
    """Capture one current frame after the parent obtained an owner policy.

    The worker still pins the foreground window and returns no page text; only
    the reviewed image bytes, geometry and hash leave this process.
    """
    if set(initial) != {'protocol', 'mode', 'allowed_apps', 'expected_window_id', 'masks'}:
        raise DesktopNativeRefused('worker_capture_invalid')
    apps, expected, masks = initial['allowed_apps'], initial['expected_window_id'], initial['masks']
    if (type(apps) is not list or not 1 <= len(apps) <= 8
            or any(type(app) is not str or not 1 <= len(app) <= 80
                or any(c in '\\/:*?"<>|\r\n\t' or ord(c) < 32 for c in app) for app in apps)
            or type(expected) is not str or expected and
                re.fullmatch(r'hwnd:[1-9][0-9]*:pid:[1-9][0-9]*:started:[1-9][0-9]*', expected) is None
            or type(masks) is not list or len(masks) > 32
            or any(type(row) is not list or len(row) != 4 or any(type(v) is not int for v in row) for row in masks)):
        raise DesktopNativeRefused('worker_capture_invalid')
    restrict_current_thread()
    api = NativeDesktopWin32Api()
    driver = WindowsDesktopDriver(api)
    matches = [target for app in apps for target in api.find_windows(app) if target.foreground]
    if len(matches) != 1:
        raise DesktopNativeRefused('window_not_unique')
    target = driver.observe(matches[0].process_name, matches[0].title)
    if expected and target.identity != expected:
        raise DesktopNativeRefused('window_identity_changed')
    if not 0 < target.width * target.height <= 4_194_304:
        raise DesktopNativeRefused('capture_size_limit')
    geometry = target.geometry(expires_at=time.time() + 5.0)
    screen = driver.capture(geometry, [tuple(row) for row in masks])
    after = api.probe_window(target.hwnd)
    if after is None or after.identity != target.identity or after.title != target.title:
        raise DesktopNativeRefused('desktop_snapshot_changed')
    _write({'protocol': PROTOCOL, 'phase': 'frame', 'restricted_token': True,
        'app': target.process_name, 'title': target.title[:200], 'geometry': asdict(geometry),
        'screen_hash': screen.sha256, 'png': base64.b64encode(screen.png).decode('ascii')})


def main() -> int:
    try:
        initial = _read()
        if initial.get("protocol") != PROTOCOL:
            raise DesktopNativeRefused("worker_protocol_mismatch")
        if initial.get('mode') == 'observe':
            observe_only(initial)
            return 0
        if initial.get('mode') == 'capture':
            capture_only(initial)
            return 0
        action = initial.get("action")
        if not isinstance(action, dict):
            raise DesktopNativeRefused("worker_action_invalid")
        kind = str(action.get("kind", "") or "")
        if kind not in {"select_candidate", "scroll", "type_draft", "wait"}:
            raise DesktopNativeRefused("kind_not_for_native_helper", kind)
        target = action.get("target")
        if not isinstance(target, dict):
            raise DesktopNativeRefused("worker_target_invalid")
        geometry = Geometry(**dict(action.get("geometry") or {}))
        restrict_current_thread()
        driver = WindowsDesktopDriver(NativeDesktopWin32Api())
        _, current = driver.current_geometry(geometry)
        _write({"protocol": PROTOCOL, "phase": "ready", "geometry": asdict(current), "restricted_token": True})

        grant = _read()
        if grant.get("protocol") != PROTOCOL or grant.get("execute") is not True:
            raise DesktopNativeRefused("worker_execute_grant_missing")
        text = str(grant.get("secret_text", "") or "") if kind == "type_draft" else ""
        # The parent treats loss after this flushed boundary as unknown.  It is
        # intentionally conservative: the worker may still refuse on its last
        # geometry/foreground check, but it can never report a sent input as a
        # clean before-effect failure.
        _write({"protocol": PROTOCOL, "phase": "input_starting"})
        after = driver.execute(kind, target, geometry, text=text)
        _write({"protocol": PROTOCOL, "phase": "result", "status": "applied", "geometry": asdict(after)})
        return 0
    except DesktopNativeRefused as exc:
        _write({"protocol": PROTOCOL, "phase": "result", "status": "refused", "reason": exc.reason})
        return 2
    except Exception:
        # No exception text: native errors and paths are not part of the
        # product protocol. The parent records the typed stable reason.
        _write({"protocol": PROTOCOL, "phase": "result", "status": "unknown", "reason": "worker_internal_error"})
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
