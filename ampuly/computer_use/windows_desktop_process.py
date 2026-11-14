"""Parent-side lifecycle for the independent Windows Desktop Helper."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from ampuly.computer_use.contracts import Geometry
from ampuly.computer_use.desktop_helper import (
    DesktopHelper,
    HelperCapabilityBroker,
    HelperRefused,
    HelperRequest,
    HelperResult,
    HelperView,
    SecureInputBroker,
)
from ampuly.computer_use.windows_desktop_worker import PROTOCOL
from ampuly.computer_use.windows_helper_job import attach_kill_on_close_job, close_job


_ENV_ALLOW = frozenset({"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "PYTHONIOENCODING"})


def _default_worker_command() -> tuple[str, ...]:
    """Start the fixed worker under ``python -I`` and the package that is running.

    ``-I`` drops the empty path entry and ``PYTHONPATH``, so a source checkout
    would otherwise fail to import ``ampuly``. The only directory added is the
    parent of this package, taken from this file, not from the parent
    environment or a model argument.
    """
    root = str(Path(__file__).resolve().parents[2])
    bootstrap = (
        "import sys\n"
        "root = sys.argv[1]\n"
        "if root not in sys.path:\n"
        "    sys.path.insert(0, root)\n"
        "from ampuly.computer_use.windows_desktop_worker import main\n"
        "raise SystemExit(main())\n"
    )
    return (sys.executable, "-I", "-c", bootstrap, root)


class WindowsDesktopHelperProcess(DesktopHelper):
    """Launch a fresh fixed-vocabulary helper for each admitted atomic input.

    A fresh process prevents state or secrets leaking from one action to the
    next.  OS token restriction is the next P2 slice; this slice proves the
    protocol/lifecycle boundary and never claims that ordinary child-process
    separation alone is a sandbox.
    """

    def __init__(
        self,
        broker: HelperCapabilityBroker,
        texts: SecureInputBroker,
        *,
        timeout_sec: float = 10.0,
        command: Sequence[str] | None = None,
        allowed_apps: Sequence[str] | None = None,
        before_input=None,
    ) -> None:
        self._broker = broker
        self._texts = texts
        self._timeout = max(0.1, min(float(timeout_sec), 30.0))
        self._command = tuple(command or _default_worker_command())
        self._killed = False
        self._active: asyncio.subprocess.Process | None = None
        self._allowed_apps = tuple(allowed_apps or ())
        self._window_id = ''
        self._before_input = before_input
        self._execute_grant_delivered = False
        self.refusal = ""

    @property
    def window_id(self) -> str:
        return self._window_id

    def _note_worker_refusal(self, value: dict[str, Any]) -> bool:
        reason = str(value.get("reason") or "")
        if value.get("phase") == "result" and reason == "window_identity_changed":
            self.refusal = "window_identity_changed"
            return True
        return False

    async def observe(self) -> HelperView | None:
        """The same fixed worker reads only an explicitly allowed window.

        First observation pins HWND/PID/process-start for this task context.
        Later observations refresh coordinates but cannot switch that window.
        No input capability or secret is sent on this read-only protocol.
        """
        self.refusal = ""
        if self._killed or self._active is not None or not 1 <= len(self._allowed_apps) <= 8:
            return None
        from ampuly.computer_use.desktop_bridge import app_allowed
        env = {key: value for key, value in os.environ.items() if key.upper() in _ENV_ALLOW}
        env['PYTHONIOENCODING'] = 'utf-8'
        process, job = None, None
        try:
            process = await asyncio.create_subprocess_exec(*self._command,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=env)
            self._active = process
            job = attach_kill_on_close_job(process.pid)
            await self._send(process, {'protocol': PROTOCOL, 'mode': 'observe',
                'allowed_apps': list(self._allowed_apps), 'expected_window_id': self._window_id})
            value = await self._receive(process)
            if self._note_worker_refusal(value):
                return None
            if (value.get('phase') != 'observation' or value.get('restricted_token') is not True
                    or not app_allowed(value.get('app'), self._allowed_apps)):
                return None
            geometry = Geometry(**dict(value.get('geometry') or {}))
            title, digest = value.get('title'), value.get('screen_hash')
            import re
            import time
            if self._window_id and geometry.window_id != self._window_id:
                self.refusal = "window_identity_changed"
                return None
            if (not re.fullmatch(r'hwnd:[1-9][0-9]*:pid:[1-9][0-9]*:started:[1-9][0-9]*', geometry.window_id)
                    or type(title) is not str or len(title) > 200
                    or type(digest) is not str or re.fullmatch(r'[a-f0-9]{64}', digest) is None
                    or type(geometry.width) is not int or type(geometry.height) is not int
                    or geometry.width <= 0 or geometry.height <= 0
                    or not 0 < geometry.width * geometry.height <= 4_194_304
                    or not time.time() < geometry.expires_at <= time.time() + 6.0):
                return None
            self._window_id = geometry.window_id
            return HelperView(app=value['app'], title=title, geometry=geometry, screen_hash=digest)
        except Exception:
            return None
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            close_job(job)
            self._active = None

    async def capture_frame(self, geometry: Geometry, masks: Any):
        """Capture one current, owner-approved frame through the same worker.

        The parent passes only the reviewed geometry and masks; the worker
        re-pins the foreground window and returns PNG bytes plus the exact
        observation it captured. No page text is sent.
        """
        self.refusal = ""
        if self._killed or self._active is not None or not 1 <= len(self._allowed_apps) <= 8:
            return None
        from ampuly.computer_use.desktop_bridge import app_allowed
        env = {key: value for key, value in os.environ.items() if key.upper() in _ENV_ALLOW}
        env['PYTHONIOENCODING'] = 'utf-8'
        process, job = None, None
        try:
            process = await asyncio.create_subprocess_exec(*self._command,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=env)
            self._active = process
            job = attach_kill_on_close_job(process.pid)
            await self._send(process, {'protocol': PROTOCOL, 'mode': 'capture',
                'allowed_apps': list(self._allowed_apps), 'expected_window_id': self._window_id,
                'masks': [list(row) for row in list(masks or [])]})
            value = await self._receive(process)
            if self._note_worker_refusal(value):
                return None
            if (value.get('phase') != 'frame' or value.get('restricted_token') is not True
                    or not app_allowed(value.get('app'), self._allowed_apps)
                    or type(value.get('png')) is not str):
                return None
            current = Geometry(**dict(value.get('geometry') or {}))
            title, digest = value.get('title'), value.get('screen_hash')
            png = base64.b64decode(value['png'].encode('ascii'), validate=True)
            if self._window_id and current.window_id != self._window_id:
                self.refusal = "window_identity_changed"
                return None
            if (current.window_id != geometry.window_id or current.width != geometry.width
                    or current.height != geometry.height
                    or type(title) is not str or len(title) > 200
                    or type(digest) is not str
                    or not png.startswith(b'\x89PNG\r\n\x1a\n')):
                return None
            self._window_id = current.window_id
            return HelperView(app=value['app'], title=title, geometry=current, screen_hash=digest), png, tuple(tuple(row) for row in masks or [])
        except Exception:
            return None
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            close_job(job)
            self._active = None

    async def kill(self) -> None:
        self._killed = True
        process = self._active
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()

    async def execute(self, request: HelperRequest) -> HelperResult:
        self.refusal = ""
        if self._killed:
            return HelperResult(status="killed", reason="kill_switch")
        env = {key: value for key, value in os.environ.items() if key.upper() in _ENV_ALLOW}
        env["PYTHONIOENCODING"] = "utf-8"
        started = False
        self._execute_grant_delivered = False
        process: asyncio.subprocess.Process | None = None
        job = None
        try:
            process = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
            self._active = process
            job = attach_kill_on_close_job(process.pid)
            await self._send(process, {
                "protocol": PROTOCOL,
                "action": {
                    "kind": request.kind,
                    "target": dict(request.target),
                    "geometry": asdict(self._expected_geometry(request.token)),
                },
            })
            ready = await self._receive(process)
            if ready.get("phase") == "result":
                self._note_worker_refusal(ready)
                return self._result(ready, input_sent=False)
            if ready.get("phase") != "ready":
                return HelperResult(status="refused", reason="worker_not_ready")
            if ready.get("restricted_token") is not True:
                return HelperResult(status="refused", reason="worker_not_restricted")
            geometry_now = Geometry(**dict(ready.get("geometry") or {}))
            try:
                self._broker.redeem(request.token, kind=request.kind, geometry_now=geometry_now)
            except HelperRefused as exc:
                return HelperResult(status="refused", reason=exc.reason, geometry_now=geometry_now)
            secret = ""
            if request.kind == "type_draft":
                try:
                    secret = self._texts.take(request.text_ref)
                except HelperRefused as exc:
                    return HelperResult(status="refused", reason=exc.reason, geometry_now=geometry_now)
            if self._before_input is not None and self._before_input() is not True:
                return HelperResult(status='refused', reason='desktop_channel_closed', input_sent=False)
            await self._send(process, {"protocol": PROTOCOL, "execute": True, "secret_text": secret})
            boundary = await self._receive(process)
            if boundary.get("phase") == "result" and str(boundary.get("status") or "") == "refused":
                # A parsed refusal before input_starting is the only receipt that
                # proves the grant did not become an OS input.
                return self._result(boundary, input_sent=False)
            if boundary.get("phase") != "input_starting":
                return HelperResult(status="unknown", reason="helper_lost_after_execute_grant", input_sent=True)
            started = True
            final = await self._receive(process)
            return self._result(final, input_sent=True)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, BrokenPipeError, ConnectionError):
            delivered = started or self._execute_grant_delivered
            return HelperResult(
                status="unknown" if delivered else "killed",
                reason="helper_lost_after_input" if started else (
                    "helper_lost_after_execute_grant" if delivered else "helper_lost_before_input"
                ),
                input_sent=delivered,
            )
        except Exception:
            delivered = started or self._execute_grant_delivered
            return HelperResult(
                status="unknown" if delivered else "refused",
                reason="helper_protocol_error_after_input" if started else (
                    "helper_lost_after_execute_grant" if delivered else "helper_protocol_error"
                ),
                input_sent=delivered,
            )
        finally:
            self._broker.discard(request.token, reason="worker_ended_without_redemption")
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            close_job(job)
            self._active = None

    def _expected_geometry(self, token: str) -> Geometry:
        # Deliberately read-only: redemption remains the single operation that
        # consumes authority. Tokens are random and this object is not exposed
        # to tools or models.
        return self._broker.inspect(token).geometry

    async def _send(self, process: asyncio.subprocess.Process, payload: dict[str, Any]) -> None:
        if process.stdin is None:
            raise BrokenPipeError
        wire = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(wire) > 65536:
            raise HelperRefused("worker_message_too_large")
        process.stdin.write(wire)
        if payload.get("execute") is True:
            # Bytes are in the transport. A lost drain or boundary is unknown,
            # not a before-effect the parent may retry.
            self._execute_grant_delivered = True
        await asyncio.wait_for(process.stdin.drain(), timeout=self._timeout)

    async def _receive(self, process: asyncio.subprocess.Process) -> dict[str, Any]:
        if process.stdout is None:
            raise asyncio.IncompleteReadError(b"", 1)
        line = await asyncio.wait_for(process.stdout.readline(), timeout=self._timeout)
        if not line or len(line) > 65536:
            raise asyncio.IncompleteReadError(line, 1)
        payload = json.loads(line)
        if not isinstance(payload, dict) or payload.get("protocol") != PROTOCOL:
            raise ValueError("worker protocol mismatch")
        return payload

    @staticmethod
    def _result(payload: dict[str, Any], *, input_sent: bool) -> HelperResult:
        status = str(payload.get("status", "") or "")
        if status not in {"applied", "refused", "unknown", "killed"}:
            status = "unknown" if input_sent else "refused"
        reason = str(payload.get("reason", "") or "")
        if input_sent and status in {"refused", "killed"}:
            # The worker flushed its irreversible boundary before the final
            # driver call. A later refusal may be a post-input geometry/focus
            # check, so the parent cannot downgrade it to before-effect.
            status = "unknown"
            reason = "worker_failed_after_input_boundary"
        geometry = payload.get("geometry")
        return HelperResult(
            status=status,
            reason=reason,
            input_sent=input_sent,
            geometry_now=Geometry(**geometry) if isinstance(geometry, dict) else None,
        )
