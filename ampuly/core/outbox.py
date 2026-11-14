"""One dispatcher for the durable Outbox tables, keyed on ``event_type``.

Two transactional Outboxes exist: the Effect Journal's and the business
layer's. Each was drained by a reader that hard-coded its own event type in
SQL, which made them two closed paths rather than a bus -- and a row whose
type nobody read stayed pending forever, so the pending queue slowly stopped
meaning "work waiting".

The dispatcher holds consumers registered per ``event_type``; a source knows
how to list and mark one table's rows. Delivery is in sequence order, and one
row ends in exactly one of three ways: delivered and marked published, raised
and marked failed (still pending, for a later pass), or nobody registered for
its type and it is marked not-consumed with the reason.

This is dispatch, not a delivery guarantee. Durability stays the source
table's, a failed row has no backoff or attempt cap, and a not-consumed row is
a decision to stop waiting rather than a claim that the event did not matter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Protocol, Sequence

from loguru import logger

# The status a row takes when its type has no reader. Distinct from published,
# because nothing was delivered, and from failed, because nobody will retry.
OUTBOX_NO_CONSUMER = "no_consumer"


@dataclass(frozen=True)
class OutboxMessage:
    """One pending row: what the dispatcher routes on, plus the raw record."""

    outbox_id: str
    event_type: str
    record: Any


class OutboxSource(Protocol):
    """One Outbox table, as the dispatcher needs it."""

    name: str

    async def list_pending(self, *, limit: int) -> Sequence[OutboxMessage]: ...

    async def mark_published(self, outbox_id: str) -> bool: ...

    async def mark_failed(self, outbox_id: str, error: str) -> bool: ...

    async def mark_no_consumer(self, outbox_id: str, reason: str) -> bool: ...


@dataclass
class OutboxDrainResult:
    """What one pass did, counted by how each row ended."""

    published: int = 0
    failed: int = 0
    not_consumed: int = 0
    delivered: list[Any] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "published": self.published,
            "failed": self.failed,
            "not_consumed": self.not_consumed,
        }


class OutboxDispatcher:
    """Routes durable Outbox rows to the consumer registered for their type."""

    def __init__(self) -> None:
        self._consumers: dict[str, Callable[[Any], Awaitable[Any]]] = {}

    def register(
        self,
        event_type: str,
        consumer: Callable[[Any], Awaitable[Any]],
        *,
        replace: bool = False,
    ) -> None:
        clean = str(event_type or "").strip()
        if not clean:
            raise ValueError("an outbox consumer must name the event type it reads")
        if not replace and clean in self._consumers:
            # Two readers for one type is not a fan-out here; it is a mistake
            # in which the second silently wins.
            raise ValueError(f"an outbox consumer is already registered for {clean!r}")
        self._consumers[clean] = consumer

    def register_many(
        self,
        event_types: Iterable[str],
        consumer: Callable[[Any], Awaitable[Any]],
        *,
        replace: bool = False,
    ) -> None:
        for event_type in event_types:
            self.register(event_type, consumer, replace=replace)

    def registered_types(self) -> frozenset[str]:
        return frozenset(self._consumers)

    async def drain(self, source: OutboxSource, *, limit: int = 100) -> OutboxDrainResult:
        result = OutboxDrainResult()
        for message in await source.list_pending(limit=limit):
            consumer = self._consumers.get(message.event_type)
            if consumer is None:
                reason = f"no consumer registered for event type {message.event_type!r}"
                if await source.mark_no_consumer(message.outbox_id, reason):
                    result.not_consumed += 1
                logger.warning(
                    "Outbox {} left record {} unconsumed: {}",
                    getattr(source, "name", "outbox"), message.outbox_id, reason,
                )
                continue
            try:
                value = await consumer(message.record)
            except Exception as exc:
                await source.mark_failed(message.outbox_id, str(exc))
                result.failed += 1
                logger.opt(exception=True).error(
                    "Outbox {} could not deliver record {} of type {}",
                    getattr(source, "name", "outbox"), message.outbox_id, message.event_type,
                )
                continue
            if await source.mark_published(message.outbox_id):
                result.published += 1
            result.delivered.append(value)
        return result
