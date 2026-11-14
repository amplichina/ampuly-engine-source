"""Unified message bus — routes messages between channels and the system."""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Coroutine, Optional

from loguru import logger

from ampuly.core.models import SystemMessage, UserMessage


InboundHandler = Callable[[UserMessage], Coroutine[Any, Any, Optional[SystemMessage]]]


def _validate_creation_source_task_id(value: object) -> None:
    failed = False
    try:
        if (type(value) is not str or not value or len(value) > 256
                or value.strip() != value
                or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)
                or len(value.encode("utf-8")) > 256):
            raise ValueError
    except Exception:
        failed = True
    if failed:
        raise ValueError("creation source Task ID is invalid")


def _validate_first_owned_source_handoff_sink(value: object) -> None:
    """Validate only a private synchronous observation sink, never a permit."""
    from types import FunctionType

    if type(value) is not FunctionType or value.__code__.co_flags & (0x20 | 0x80 | 0x200):
        raise ValueError("first-owned source handoff sink invalid") from None


class MessageBus:
    """Async message bus for routing between interaction channels and the AMPULY engine.

    Channels publish inbound messages; the engine processes them and publishes outbound responses.
    """

    def __init__(self) -> None:
        self._inbound_queue: asyncio.Queue[UserMessage] | None = None
        self._outbound_queue: asyncio.Queue[SystemMessage] | None = None
        self._inbound_handler: InboundHandler | None = None
        self._running = False

    def _inbound(self) -> asyncio.Queue[UserMessage]:
        if self._inbound_queue is None:
            self._inbound_queue = asyncio.Queue()
        return self._inbound_queue

    def _outbound(self) -> asyncio.Queue[SystemMessage]:
        if self._outbound_queue is None:
            self._outbound_queue = asyncio.Queue()
        return self._outbound_queue

    def set_handler(self, handler: InboundHandler) -> None:
        self._inbound_handler = handler

    async def publish_inbound(self, message: UserMessage) -> None:
        await self._inbound().put(message)

    async def publish_outbound(self, message: SystemMessage) -> None:
        await self._outbound().put(message)

    async def get_response(self, timeout: float = 600.0) -> SystemMessage | None:
        try:
            return await asyncio.wait_for(self._outbound().get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    async def start(self) -> None:
        """Start processing inbound messages."""
        self._running = True
        while self._running:
            try:
                msg = await asyncio.wait_for(self._inbound().get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            if self._inbound_handler:
                try:
                    response = await self._inbound_handler(msg)
                    if response:
                        await self._outbound().put(response)
                except Exception as e:
                    logger.error(f"Message handler error: {e}")
                    await self._outbound().put(SystemMessage(
                        channel=msg.channel,
                        user_id=msg.user_id,
                        session_id=msg.session_id,
                        content=f"Error processing message: {e}",
                        message_type="reply",
                    ))

    def stop(self) -> None:
        self._running = False

    async def process_single(self, message: UserMessage, *,
                             creation_source_task_id: str | None = None,
                             _first_owned_source_handoff_sink: object = None) -> SystemMessage | None:
        """Process a single message synchronously (for CLI use)."""
        handoff_kwargs = {}
        if _first_owned_source_handoff_sink is not None:
            _validate_first_owned_source_handoff_sink(_first_owned_source_handoff_sink)
            if creation_source_task_id is None:
                raise RuntimeError("creation source Task is required for handoff") from None
            handoff_kwargs["_first_owned_source_handoff_sink"] = _first_owned_source_handoff_sink
        if creation_source_task_id is not None:
            _validate_creation_source_task_id(creation_source_task_id)
            handler = self._inbound_handler
            if handler is None:
                raise RuntimeError("creation source Task handler unavailable")
            return await handler(
                message, creation_source_task_id=creation_source_task_id, **handoff_kwargs,
            )
        if self._inbound_handler:
            return await self._inbound_handler(message)
        return None
