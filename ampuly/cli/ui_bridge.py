"""Hand a CLI request to the running ``ampuly ui`` instead of booting a second engine.

The console (``ampuly ui``) is the control plane: it owns the runtime database,
the approval cards, the duty patrol and the pager. A CLI ``exec`` that boots
its own engine runs beside it with a second controller, its own approval
loop and none of the console's visibility - the state the team called "two
engine processes". When a console is reachable on the local port, the CLI
opens the same WebSocket the browser uses, creates a session, sends the
message and relays what comes back. Approvals stay where they belong: a
pending card is announced with the console URL rather than answered from
the terminal. When no console is listening the caller falls back to the
in-process path unchanged.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

TERMINAL_STATUSES = frozenset({"done", "delivered", "failed", "cancelled", "canceled"})
DEFAULT_PORT = 8765
_CONNECT_TIMEOUT = 1.5
_READ_TIMEOUT = 10.0
# A console that is also serving a browser blocks its event loop in bursts, so
# the handshake can take a couple of seconds.  1.5 s made half of the read
# commands fall back to building their own engine (2026-09-06), which costs
# ten.  With no console at all the connection is refused immediately, so a
# longer budget only ever waits when there is something worth waiting for.
_READ_CONNECT_TIMEOUT = 6.0
# Read actions the CLI may serve from a live console instead of building its
# own engine.  Everything that mutates stays on the explicit control path.
READ_ACTIONS = frozenset({
    "session_list",
    "project_list_read",
    "session_detail_read",
    "project_show_read",
    "agent_list_read",
    "agent_detail_read",
})
_PING_INTERVAL = 20.0


@dataclass
class BridgeResult:
    task_id: str
    status: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    waited_on_human: bool = False
    console_url: str = ""
    error: str = ""

    @property
    def final_text(self) -> str:
        for message in reversed(self.messages):
            content = str(message.get("content", "") or "").strip()
            if content and str(message.get("sender", "") or "") != "user":
                return content
        return ""


# Where a console is listening, written by the console into its own home once it is up (and removed when it stops).
# A command typed in a terminal gets neither the launcher's AMPULY_UI_PORT nor anything else from it -- but it resolves
# the same home, so it finds *this* home's console and never another home's. Until 2026-09-25 this module defaulted to
# 8765 while the Ampuly launcher serves on 8766: every bridged command fell back to building a second engine, and an
# old console of another home on 8765 would have been the one answering.
CONSOLE_ADDRESS_FILE = "console-address.json"


def _default_home() -> Path:
    from ampuly.core.config import get_ampuly_home

    return get_ampuly_home()


def publish_console_address(home: Path, *, host: str, port: int) -> Path:
    """Record where this home's console listens. Written whole, so a reader never sees half of it."""
    path = Path(home) / CONSOLE_ADDRESS_FILE
    record = {
        "port": int(port),
        "host": str(host),
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    partial = path.with_name(f".{CONSOLE_ADDRESS_FILE}.{os.getpid()}.tmp")
    partial.write_text(json.dumps(record), encoding="utf-8")
    os.replace(partial, path)
    return path


def forget_console_address(home: Path, *, pid: int | None = None) -> None:
    """Remove the record, but only the one this process wrote: a second console may have replaced it since."""
    path = Path(home) / CONSOLE_ADDRESS_FILE
    try:
        written_by = json.loads(path.read_text(encoding="utf-8")).get("pid")
    except (OSError, ValueError, AttributeError):
        return
    if written_by == (os.getpid() if pid is None else pid):
        try:
            path.unlink()
        except OSError:
            pass


def published_port(home: Path | None = None) -> int | None:
    """The port this home's console said it listens on, or ``None`` when it said nothing usable."""
    try:
        path = Path(home if home is not None else _default_home()) / CONSOLE_ADDRESS_FILE
        port = json.loads(path.read_text(encoding="utf-8")).get("port")
    except (OSError, ValueError, AttributeError):
        return None
    return port if isinstance(port, int) and 0 < port < 65536 else None


def _resolved_port(port: int | None) -> int:
    if port:
        return int(port)
    configured = os.environ.get("AMPULY_UI_PORT", "").strip()
    if configured:
        return int(configured)
    return published_port() or DEFAULT_PORT


def console_port(port: int | None = None) -> int:
    """This home's console port: the one given, else ``AMPULY_UI_PORT``, else what the console published, else 8765."""
    return _resolved_port(port)


def console_ws_url(port: int | None = None) -> str:
    return f"ws://127.0.0.1:{_resolved_port(port)}/ws"


def console_http_url(port: int | None = None) -> str:
    return f"http://localhost:{_resolved_port(port)}"


def bridge_disabled() -> bool:
    return str(os.environ.get("AMPULY_CLI_STANDALONE", "") or "").strip().lower() in {"1", "true", "yes", "on"}


def _message_targets_task(payload: dict[str, Any], task_id: str) -> bool:
    if str(payload.get("task_id", "") or "") == task_id:
        return True
    channel = str(payload.get("channel_id", "") or "")
    return channel == f"session:{task_id}"


async def exec_via_console(
    *,
    prompt: str,
    project_id: str,
    mode: str = "task",
    company_profile: str | None = None,
    preferred_agent: str | None = None,
    org_id: str | None = None,
    session_id: str | None = None,
    native_approval_level: str | None = None,
    port: int | None = None,
    on_event: Callable[[str, dict[str, Any]], Awaitable[None] | None] | None = None,
    connect: Callable[..., Any] | None = None,
) -> BridgeResult | None:
    """Run one prompt through a live console; None when no console answers.

    ``on_event(kind, payload)`` receives ``message`` (assistant/system text),
    ``waiting`` (a pending decision card) and ``status`` (task status change)
    as they arrive. ``connect`` is injectable for tests.
    """
    if connect is not None:
        # Tests and embedded callers can provide a transport without pulling
        # the optional websocket client into the CLI environment.
        connector = connect
    else:
        try:
            import websockets
        except ImportError:  # pragma: no cover - dependency of the console itself
            return None
        connector = websockets.connect
    url = console_ws_url(port)
    try:
        socket = await asyncio.wait_for(connector(url), timeout=_CONNECT_TIMEOUT)
    except Exception:
        return None

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        if on_event is None:
            return
        outcome = on_event(kind, payload)
        if asyncio.iscoroutine(outcome):
            await outcome

    async def send(payload: dict[str, Any]) -> None:
        await socket.send(json.dumps(payload, ensure_ascii=False))

    async def receive() -> dict[str, Any]:
        raw = await socket.recv()
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    result = BridgeResult(task_id=str(session_id or ""), status="pending", console_url=console_http_url(port))
    try:
        if not result.task_id:
            create: dict[str, Any] = {
                "type": "create_session",
                "project_id": project_id,
                "title": (prompt.strip().splitlines() or [""])[0][:60] or "CLI",
                "exec_mode": mode,
            }
            if company_profile:
                create["company_profile"] = company_profile
            if preferred_agent:
                create["preferred_agent"] = preferred_agent
            if org_id:
                create["org_id"] = org_id
            if native_approval_level:
                create["native_approval_level"] = native_approval_level
            await send(create)
            while True:
                envelope = await asyncio.wait_for(receive(), timeout=30.0)
                if envelope.get("type") != "ack":
                    continue
                ack = dict(envelope.get("payload", {}) or {})
                if ack.get("action") != "create_session":
                    continue
                if not ack.get("ok"):
                    raise RuntimeError(str(ack.get("error", "") or "create_session_failed"))
                result.task_id = str(ack.get("task_id", "") or "")
                break
            if not result.task_id:
                raise RuntimeError("console did not return a task id")

        await send({
            "type": "session_send",
            "project_id": project_id,
            "task_id": result.task_id,
            "content": prompt,
            "attachments": [],
        })
        await emit("status", {"task_id": result.task_id, "status": "sent"})

        last_ping = asyncio.get_running_loop().time()
        while True:
            try:
                envelope = await asyncio.wait_for(receive(), timeout=_PING_INTERVAL)
            except asyncio.TimeoutError:
                now = asyncio.get_running_loop().time()
                if now - last_ping >= _PING_INTERVAL:
                    await send({"type": "ping"})
                    last_ping = now
                continue
            kind = str(envelope.get("type", "") or "")
            payload = dict(envelope.get("payload", {}) or {})
            if kind == "ack" and payload.get("action") == "session_send" and payload.get("ok") is False:
                raise RuntimeError(str(payload.get("error", "") or "session_send_failed"))
            if kind == "auth" and payload.get("required") is True and not payload.get("account"):
                # The console got its first account while this relay was waiting. Pushes reach logged-in owners only,
                # so the terminal status would never arrive here and the command would wait for ever. The run goes on
                # inside the console; say where to look.
                result.status = "failed"
                result.error = "console_login_required"
                await emit("status", {"task_id": result.task_id, "status": result.status, "error": result.error})
                return result
            if not _message_targets_task(payload, result.task_id):
                continue
            if kind == "session_message":
                if payload.get("checkpoint_id") and str(payload.get("checkpoint_status", "pending") or "pending") == "pending":
                    result.waited_on_human = True
                    await emit("waiting", payload)
                    continue
                if str(payload.get("sender", "") or "") == "user":
                    continue
                result.messages.append(payload)
                await emit("message", payload)
                continue
            if kind in {"session_updated", "board_task_status_changed", "session_runtime_control"}:
                status = str(payload.get("status", "") or "").strip().lower()
                if not status:
                    continue
                result.status = status
                await emit("status", payload)
                if status in TERMINAL_STATUSES:
                    return result
    except Exception as exc:
        # Once a task has been handed to the console, never fall back to a
        # second private engine: that would create two controllers for one
        # run.  Convert transport loss into an explicit, non-sensitive result
        # so the CLI can point the operator back to the console for recovery.
        error_name = type(exc).__name__
        transport_error = (
            isinstance(exc, (ConnectionError, OSError, EOFError, asyncio.IncompleteReadError))
            or "ConnectionClosed" in error_name
            or "WebSocket" in error_name
        )
        if transport_error:
            result.status = "connection_lost"
            result.error = "console_connection_lost"
            await emit("status", {"task_id": result.task_id, "status": result.status, "error": result.error})
        else:
            result.status = "failed"
            result.error = "console_request_failed"
            await emit("status", {"task_id": result.task_id, "status": result.status, "error": result.error})
        return result
    finally:
        try:
            await socket.close()
        except Exception:
            pass


async def read_via_console(
    *,
    action: str,
    payload: dict[str, Any] | None = None,
    port: int | None = None,
    connect: Callable[..., Any] | None = None,
) -> dict[str, Any] | None:
    """Ask the live console for a read-only view; ``None`` when none is running.

    Only read actions belong here: the caller falls back to a local engine
    when the console is absent, so a mutating action must never take this
    path (a fallback would then run it twice).
    """
    if action not in READ_ACTIONS:
        raise ValueError(f"{action!r} is not a console read action")
    if bridge_disabled():
        return None
    if connect is not None:
        connector = connect
    else:
        try:
            import websockets
        except ImportError:  # pragma: no cover - optional console dependency
            return None
        connector = websockets.connect
    try:
        socket = await asyncio.wait_for(connector(console_ws_url(port)), timeout=_READ_CONNECT_TIMEOUT)
    except Exception:
        return None
    try:
        await socket.send(json.dumps({"type": action, **dict(payload or {})}))
        deadline = asyncio.get_event_loop().time() + _READ_TIMEOUT
        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(socket.recv(), timeout=_READ_TIMEOUT)
            except Exception:
                return None
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if message.get("type") != "ack":
                continue
            body = dict(message.get("payload", {}) or {})
            if str(body.get("action", "") or "") != action:
                continue
            return body
        return None
    finally:
        try:
            await socket.close()
        except Exception:
            pass


async def session_control_via_console(
    *,
    action: str,
    project_id: str,
    task_id: str,
    session_id: str | None = None,
    runtime_session_id: str | None = None,
    checkpoint_id: str | None = None,
    content: str | None = None,
    port: int | None = None,
    connect: Callable[..., Any] | None = None,
) -> BridgeResult | None:
    """Send a stop/resume command to the live console when one is available.

    The service fallback remains responsible for offline CLI use. Once a
    console accepts the command, transport loss is returned explicitly so the
    caller never starts a second controller for the same run.
    """
    if action not in {"session_stop", "session_resume"} or not str(task_id or "").strip():
        return None
    if connect is not None:
        connector = connect
    else:
        try:
            import websockets
        except ImportError:  # pragma: no cover - optional console dependency
            return None
        connector = websockets.connect
    try:
        socket = await asyncio.wait_for(connector(console_ws_url(port)), timeout=_CONNECT_TIMEOUT)
    except Exception:
        return None

    async def receive() -> dict[str, Any]:
        raw = await socket.recv()
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    result = BridgeResult(task_id=str(task_id).strip(), status="pending", console_url=console_http_url(port))
    payload: dict[str, Any] = {
        "type": action,
        "project_id": str(project_id or "default").strip() or "default",
        "task_id": result.task_id,
    }
    for key, value in (
        ("session_id", session_id),
        ("runtime_session_id", runtime_session_id),
        ("checkpoint_id", checkpoint_id),
        ("content", content),
    ):
        if str(value or "").strip():
            payload[key] = str(value).strip()
    try:
        await socket.send(json.dumps(payload, ensure_ascii=False))
        while True:
            envelope = await asyncio.wait_for(receive(), timeout=30.0)
            if envelope.get("type") != "ack":
                continue
            ack = dict(envelope.get("payload", {}) or {})
            if str(ack.get("action", "") or "") != action:
                continue
            acknowledged_task_id = str(ack.get("task_id", "") or "").strip()
            if acknowledged_task_id:
                result.task_id = acknowledged_task_id
            if ack.get("ok"):
                result.status = str(ack.get("status", "") or "").strip().lower()
                if not result.status:
                    result.status = "stopped" if action == "session_stop" else "resumed"
            else:
                result.status = "failed"
                raw_error = str(ack.get("error", "") or "").strip().lower()
                result.error = raw_error if raw_error and all(ch.isalnum() or ch in "_-:" for ch in raw_error) else "console_request_rejected"
            return result
    except Exception as exc:
        error_name = type(exc).__name__
        transport_error = (
            isinstance(exc, (ConnectionError, OSError, EOFError, asyncio.IncompleteReadError))
            or "ConnectionClosed" in error_name
            or "WebSocket" in error_name
        )
        result.status = "connection_lost" if transport_error else "failed"
        result.error = "console_connection_lost" if transport_error else "console_request_failed"
        return result
    finally:
        try:
            await socket.close()
        except Exception:
            pass
