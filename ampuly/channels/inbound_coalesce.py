"""Short-window coalesce for inbound media + text into one multimodal turn.

Independent Ampuly implementation. Idea only: same-session image/file events
and a following caption often arrive as two provider events; merge them into
one turn inside a short window. Commands bypass the window. Optional sticky
caption re-attaches a recent question onto a late image.

Not an execution loop, not a Runtime, not a ledger. Default disabled.
Enable with ``InboundCoalesceBuffer(enabled=True)`` or env ``AMPULY_INBOUND_COALESCE=1``.
"""
from __future__ import annotations

import os
import copy
import json
import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable


DEFAULT_WINDOW_MS = 1_200
DEFAULT_STICKY_CAPTION_MS = 3_000


def coalesce_enabled_from_env(default: bool = False) -> bool:
    raw = str(os.environ.get("AMPULY_INBOUND_COALESCE", "") or "").strip().lower()
    if not raw:
        return bool(default)
    return raw in {"1", "true", "yes", "on"}


def is_inbound_command(content: Any) -> bool:
    """Slash-style channel commands bypass the coalesce window."""
    text = str(content or "").strip()
    if not text:
        return False
    if text.startswith("/"):
        return True
    head = text.split(None, 1)[0].casefold()
    return head in {"help", "status", "cancel", "stop", "ping"}


def is_media_only(content: Any, attachments: Iterable[Any] | None) -> bool:
    text = str(content or "").strip()
    atts = list(attachments or [])
    return (not text) and bool(atts)


def session_key(channel: str, chat_id: str, sender_id: str) -> tuple[str, str, str]:
    """Typed components avoid delimiter collisions; never pool missing actors."""
    return (str(channel or ""), str(chat_id or ""), str(sender_id or ""))


@dataclass
class CoalesceEvent:
    channel: str
    chat_id: str
    sender_id: str
    content: str = ""
    attachments: list[Any] = field(default_factory=list)
    message_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    arrived_at_ms: float = 0.0
    # Supplied by the channel's server-owned scope/routing boundary, not a wire flag.
    merge_identity: tuple[str, ...] | None = None
    source_events: list["CoalesceEvent"] = field(default_factory=list, repr=False)
    normalized: dict[str, Any] = field(default_factory=dict, repr=False)

    def copy(self) -> "CoalesceEvent":
        return copy.deepcopy(self)


@dataclass
class CoalesceOfferResult:
    action: str  # emit | hold | bypass
    emit: list[CoalesceEvent] = field(default_factory=list)
    held: bool = False
    reason: str = ""


def _merge(media: CoalesceEvent, text: CoalesceEvent) -> CoalesceEvent:
    merged = text.copy()
    merged.attachments = list(media.attachments) + list(text.attachments)
    merged.message_id = text.message_id or media.message_id
    meta = dict(media.metadata)
    # Keep media provenance; text cannot replace its authorization/routing metadata.
    for key, value in text.metadata.items():
        meta.setdefault(key, copy.deepcopy(value))
    meta["message_id"] = merged.message_id
    meta["coalesced_sources"] = [
        {"message_id": e.message_id, "metadata": copy.deepcopy(e.metadata)}
        for e in (media, text)
    ]
    meta["coalesced"] = True
    meta["coalesced_from"] = [mid for mid in (media.message_id, text.message_id) if mid]
    merged.metadata = meta
    merged.source_events = [media.copy(), text.copy()]
    return merged


def _apply_sticky(caption: CoalesceEvent, media: CoalesceEvent) -> CoalesceEvent:
    out = media.copy()
    if not str(out.content or "").strip() and str(caption.content or "").strip():
        out.content = caption.content
    meta = dict(out.metadata)
    meta["sticky_caption"] = True
    meta["sticky_from_message_id"] = caption.message_id
    meta["coalesced_sources"] = [
        {"message_id": e.message_id, "metadata": copy.deepcopy(e.metadata)}
        for e in (caption, media)
    ]
    out.metadata = meta
    out.source_events = [caption.copy(), media.copy()]
    return out


@dataclass
class _Pending:
    event: CoalesceEvent
    kind: str  # media | caption


class InboundCoalesceBuffer:
    """Per-session short window for media/text merge. Injectable clock for tests."""

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        window_ms: int = DEFAULT_WINDOW_MS,
        sticky_caption_ms: int = DEFAULT_STICKY_CAPTION_MS,
        clock_ms: Callable[[], float] | None = None,
    ) -> None:
        self.enabled = coalesce_enabled_from_env(False) if enabled is None else bool(enabled)
        self.window_ms = max(0, int(window_ms))
        self.sticky_caption_ms = max(0, int(sticky_caption_ms))
        self._clock_ms = clock_ms or (lambda: time.monotonic() * 1000.0)
        self._pending: dict[tuple[str, ...], _Pending] = {}

    def _now(self, event: CoalesceEvent) -> float:
        return float(event.arrived_at_ms) if event.arrived_at_ms else float(self._clock_ms())

    def _expire_session(self, key: str, now: float) -> list[CoalesceEvent]:
        pending = self._pending.get(key)
        if pending is None:
            return []
        age = now - pending.event.arrived_at_ms
        if pending.kind == "media" and age > self.window_ms:
            taken = self._pending.pop(key, None)
            return [taken.event] if taken else []
        if pending.kind == "caption" and age > self.sticky_caption_ms:
            self._pending.pop(key, None)
            return []
        return []

    def offer(self, event: CoalesceEvent) -> CoalesceOfferResult:
        if not self.enabled:
            return CoalesceOfferResult(action="emit", emit=[event.copy()], reason="disabled")

        basic = session_key(event.channel, event.chat_id, event.sender_id)
        if not all(part.strip() for part in basic):
            return CoalesceOfferResult(action="emit", emit=[event.copy()], reason="missing_session")

        if not event.merge_identity or not all(isinstance(x, str) and x.strip() for x in event.merge_identity):
            return CoalesceOfferResult(action="emit", emit=[event.copy()], reason="unbound_scope")
        dimensions = {k: event.metadata.get(k) for k in (
            "thread_id", "topic_id", "account_id", "enterprise_id", "project_id",
            "customer_id", "order_id", "scope_id", "binding_generation")}
        key = basic + event.merge_identity + (json.dumps(dimensions, sort_keys=True, default=str),)
        now = self._now(event)
        if not math.isfinite(now):
            return CoalesceOfferResult(action="emit", emit=[event.copy()], reason="invalid_clock")
        # Prune caption-only sessions too; they do not own a timer.
        for old_key in list(self._pending):
            pending = self._pending[old_key]
            if pending.kind == "caption" and (now < pending.event.arrived_at_ms or now - pending.event.arrived_at_ms > self.sticky_caption_ms):
                self._pending.pop(old_key, None)
        event = event.copy()
        event.arrived_at_ms = now

        expired = self._expire_session(key, now)

        if is_inbound_command(event.content):
            flushed = self._pending.pop(key, None)
            emit = list(expired)
            if flushed and flushed.kind == "media":
                emit.append(flushed.event)
            emit.append(event)
            return CoalesceOfferResult(action="bypass", emit=emit, reason="command")

        result = self._offer_fresh(key, event, now)
        if expired:
            return CoalesceOfferResult(
                action="emit" if (expired or result.emit) and not (result.held and not result.emit and not expired) else result.action,
                emit=list(expired) + list(result.emit),
                held=result.held,
                reason=("expired+" + result.reason) if result.reason else "expired",
            )
        return result

    def _offer_fresh(self, key: str, event: CoalesceEvent, now: float) -> CoalesceOfferResult:
        pending = self._pending.get(key)
        media = is_media_only(event.content, event.attachments)

        if media:
            if pending is not None and pending.kind == "caption":
                age = now - pending.event.arrived_at_ms
                if 0 <= age <= self.sticky_caption_ms:
                    caption = self._pending.pop(key)
                    merged = _apply_sticky(caption.event, event)
                    return CoalesceOfferResult(action="emit", emit=[merged], reason="sticky_caption")
            emit: list[CoalesceEvent] = []
            if pending is not None and pending.kind == "media":
                old = self._pending.pop(key)
                emit.append(old.event)
            self._pending[key] = _Pending(event=event, kind="media")
            return CoalesceOfferResult(action="hold", emit=emit, held=True, reason="hold_media")

        if pending is not None and pending.kind == "media":
            age = now - pending.event.arrived_at_ms
            if 0 <= age <= self.window_ms:
                media_ev = self._pending.pop(key)
                merged = _merge(media_ev.event, event)
                return CoalesceOfferResult(action="emit", emit=[merged], reason="merged")
            old = self._pending.pop(key)
            if str(event.content or "").strip():
                self._pending[key] = _Pending(event=event, kind="caption")
            return CoalesceOfferResult(
                action="emit",
                emit=[old.event, event],
                held=bool(str(event.content or "").strip()),
                reason="media_timeout",
            )

        if str(event.content or "").strip():
            self._pending[key] = _Pending(event=event, kind="caption")
            return CoalesceOfferResult(action="emit", emit=[event], held=True, reason="sticky_ready")

        return CoalesceOfferResult(action="emit", emit=[event], reason="passthrough")

    def has_pending_media(self) -> bool:
        """True when some session holds media waiting for a caption.

        A held sticky caption alone needs no flush timer; only held media does.
        """
        return any(p.kind == "media" for p in self._pending.values())

    def flush_due(self, now_ms: float | None = None) -> list[CoalesceEvent]:
        now = float(self._clock_ms() if now_ms is None else now_ms)
        due: list[CoalesceEvent] = []
        for key, pending in list(self._pending.items()):
            if pending.kind != "media":
                continue
            if now - pending.event.arrived_at_ms >= self.window_ms:
                taken = self._pending.pop(key, None)
                if taken:
                    due.append(taken.event)
        return due

    def flush_all(self) -> list[CoalesceEvent]:
        out = [p.event for p in self._pending.values() if p.kind == "media"]
        self._pending.clear()
        return out
