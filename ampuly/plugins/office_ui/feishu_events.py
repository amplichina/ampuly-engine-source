"""Inbound Feishu events over the SDK's long connection (no public URL needed).

The console runs on a Windows machine behind NAT, so a webhook is not an
option; Feishu's event subscription in "long connection" mode has the app
dial out and receive events (card actions, messages, read receipts) over a
WebSocket the SDK maintains. That SDK (``lark-oapi``) is optional: without
it this module reports "not installed" and the gate keeps working from the
console; with it, :class:`FeishuLongConnection` runs the client in a
background thread and hands every event to the gate service as a plain
dict, the same shape :func:`feishu_gate.parse_event` expects.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, Awaitable, Callable

EventHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]


def sdk_available() -> bool:
    try:
        import lark_oapi  # noqa: F401
    except ImportError:
        return False
    return True


def _to_plain(obj: Any) -> dict[str, Any]:
    """SDK event objects -> plain dicts (via the SDK's own JSON marshalling)."""
    try:
        import lark_oapi as lark

        text = lark.JSON.marshal(obj)
        data = json.loads(text) if text else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        try:
            return json.loads(json.dumps(obj, default=lambda o: getattr(o, "__dict__", str(o))))
        except Exception:
            return {}


class FeishuLongConnection:
    """Owns one SDK websocket client on a daemon thread; forwards events to the loop."""

    def __init__(self, app_id: str, app_secret: str, handler: EventHandler, *, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self._handler = handler
        self._loop = loop
        self._thread: threading.Thread | None = None
        self._client: Any = None
        self.status = "idle"
        self.last_error = ""
        self.received = 0

    # ── event plumbing ──
    def _dispatch(self, event: dict[str, Any]) -> None:
        self.received += 1
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(self._safe_handle(event), loop)

    async def _safe_handle(self, event: dict[str, Any]) -> None:
        try:
            await self._handler(event)
        except Exception as exc:  # never let one bad event kill the listener
            self.last_error = f"{type(exc).__name__}: {exc}"

    def _on_message(self, data: Any) -> None:
        payload = _to_plain(data)
        payload.setdefault("header", {}).setdefault("event_type", "im.message.receive_v1")
        self._dispatch(payload)

    def _on_read(self, data: Any) -> None:
        payload = _to_plain(data)
        payload.setdefault("header", {}).setdefault("event_type", "im.message.message_read_v1")
        self._dispatch(payload)

    def _on_card_action(self, data: Any) -> Any:
        payload = _to_plain(data)
        payload["type"] = "card.action.trigger"
        self._dispatch(payload)
        return None  # let the card stay; the service patches it after deciding

    # ── lifecycle ──
    def start(self) -> bool:
        if not sdk_available():
            self.status = "sdk_missing"
            self.last_error = "pip install lark-oapi"
            return False
        if self._thread and self._thread.is_alive():
            return True
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = None
        try:
            import lark_oapi as lark

            builder = lark.EventDispatcherHandler.builder("", "")
            builder = builder.register_p2_im_message_receive_v1(self._on_message)
            if hasattr(builder, "register_p2_im_message_message_read_v1"):
                builder = builder.register_p2_im_message_message_read_v1(self._on_read)
            if hasattr(builder, "register_p2_card_action_trigger"):
                builder = builder.register_p2_card_action_trigger(self._on_card_action)
            dispatcher = builder.build()
            # INFO logs the credential-bearing WebSocket endpoint URL.
            self._client = lark.ws.Client(self.app_id, self.app_secret, event_handler=dispatcher, log_level=lark.LogLevel.WARNING)
        except Exception as exc:
            self.status = "error"
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False

        def run() -> None:
            try:
                # The SDK captures ``asyncio.get_event_loop()`` at import time,
                # which on the console's main thread is the running server loop.
                # Give the client its own loop on this thread instead.
                import lark_oapi.ws.client as ws_client

                own_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(own_loop)
                ws_client.loop = own_loop
                self.status = "connected"
                self._client.start()
            except Exception as exc:
                self.status = "error"
                self.last_error = f"{type(exc).__name__}: {exc}"

        self._thread = threading.Thread(target=run, name="feishu-long-connection", daemon=True)
        self._thread.start()
        return True

    def status_payload(self) -> dict[str, Any]:
        return {"status": self.status, "last_error": self.last_error, "received": self.received, "sdk": sdk_available()}
