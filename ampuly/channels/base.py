"""Base channel interface for external messaging platforms."""

from __future__ import annotations

from abc import ABC, abstractmethod
import copy
import json
from datetime import datetime
from typing import Any

from loguru import logger

from ampuly.channels.inbound_entry import InboundDecision, InboundNotSupported, accept_inbound
from ampuly.channels.inbound_screen import stamp_screen
from ampuly.channels.session import ChannelSessionMapping
from ampuly.core.models import SystemMessage, UserMessage
from ampuly.layer0_interaction.message_bus import MessageBus


class BaseChannel(ABC):
    name: str = "base"
    dependency_name: str | None = None

    def __init__(self, config: Any, bus: MessageBus):
        self.config = config
        self.bus = bus
        self._running = False
        self._started_at: datetime | None = None
        self._last_error: str = ""
        self._status_reason: str = ""
        self.last_outbound: dict[str, Any] | None = None
        self._coalesce_epoch = 0
        self._coalesce_stopped = False
        self._coalesce_flush_task: Any = None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def last_error(self) -> str:
        return self._last_error

    def set_status_reason(self, reason: str = "") -> None:
        self._status_reason = reason.strip()

    def set_last_error(self, error: Exception | str | None) -> None:
        self._last_error = str(error or "").strip()
        if self._last_error:
            logger.warning("{} runtime error: {}", self.name, self._last_error)
        else:
            logger.debug("{} runtime error cleared", self.name)

    def mark_started(self) -> None:
        self._running = True
        self._coalesce_stopped = False
        self._started_at = datetime.now()
        self.set_last_error("")

    def mark_stopped(self) -> None:
        self._running = False
        self._coalesce_stopped = True
        self._coalesce_epoch += 1
        task = self._coalesce_flush_task
        if task is not None and not task.done():
            task.cancel()
        buf = getattr(self, "_inbound_coalesce", None)
        if buf is not None:
            buf.flush_all()  # discard, never replay captions or deliver after shutdown

    def inbound_dedup(self) -> Any:
        """This channel's memory of what it has already handled.

        Built lazily and shared per channel instance. Durable when the store
        can be opened, in-memory when it cannot -- a channel that stopped
        accepting customer messages because its bookkeeping file was unhappy
        would have turned a small problem into an outage.
        """
        existing = getattr(self, "_inbound_dedup", None)
        if existing is not None:
            return existing
        from ampuly.channels.inbound_dedup import InboundDedup, default_path

        # Durable only where somebody said where. Resolving a home implicitly
        # would make a test run write into the real installation, and a store
        # that appears in places nobody chose is worse than an in-memory one.
        home = getattr(self, "ampuly_home", None)
        dedup = InboundDedup(self.name, path=default_path(home) if home else None)
        self._inbound_dedup = dedup
        return dedup

    def inbound_coalesce(self) -> Any:
        """Optional short-window media+text merge. Default off (safe).

        When ``AMPULY_INBOUND_COALESCE=1`` (or constructed enabled), same-session
        image/file events wait briefly for a caption before becoming a turn.
        Commands bypass. Not a Runtime / scheduler loop — one-shot debounce.
        """
        existing = getattr(self, "_inbound_coalesce", None)
        if existing is not None:
            return existing
        from ampuly.channels.inbound_coalesce import InboundCoalesceBuffer

        buf = InboundCoalesceBuffer(enabled=None)
        self._inbound_coalesce = buf
        return buf

    def inbound_coalesce_scope(self, normalized: dict[str, Any] | None = None) -> dict[str, str] | None:
        """Server binding hook; unbound channels pass events through separately.

        Runtime owners may bind a canonical scope_id and binding_generation on
        the channel instance. Neither comes from the external message metadata.
        A channel with no authoritative binding must not guess one from chat IDs.
        """
        resolver = getattr(self, "_coalesce_scope_resolver", None)
        scope = resolver(normalized or {}) if callable(resolver) else (None if getattr(self, "coalesce_runtime_context", None) is not None else getattr(self, "coalesce_scope", None))
        if not isinstance(scope, dict):
            return None
        if not all(isinstance(scope.get(k), str) and scope[k].strip()
                   for k in ("scope_id", "binding_generation")):
            return None
        return {str(k): str(v) for k, v in scope.items() if isinstance(v, str)}

    def _coalesce_identity(self, normalized: dict[str, Any]) -> tuple[str, ...] | None:
        account = self.account_identity()
        scope = self.inbound_coalesce_scope(normalized)
        runtime = getattr(self, "coalesce_runtime_context", None)
        if runtime is not None and str(runtime.get("account_id") or "") != str(account):
            return None
        if not account or not scope or self._coalesce_stopped:
            return None
        metadata = dict(normalized.get("metadata") or {})
        thread = str(normalized.get("thread_id") or metadata.get("thread_id") or "")
        reply = str(normalized.get("reply_to") or metadata.get("reply_to") or "")
        route = self.map_session(chat_id=str(normalized.get("chat_id") or ""),
                                 thread_id=thread, reply_to=reply, metadata=metadata)
        # Message-provided labels can only further partition a trusted binding;
        # they never establish scope or replace the server account/generation.
        partitions = {k: [normalized.get(k), metadata.get(k)] for k in (
            "thread_id", "topic_id", "account_id", "enterprise_id", "project_id",
            "customer_id", "order_id", "scope_id", "binding_generation")}
        return (str(account), json.dumps(scope, sort_keys=True), str(self._coalesce_epoch),
                json.dumps(route, sort_keys=True), str(self.build_session_key_override(normalized) or "<default-route>"),
                json.dumps(partitions, sort_keys=True, default=str))

    async def _release_coalesced(self, event: Any) -> bool:
        """Recheck authorization/binding without remembering an accepted ID twice."""
        sources = event.source_events or [event]
        for source in sources:
            normalized = source.normalized
            if (self._coalesce_stopped or not normalized
                    or not self.should_accept_inbound(normalized)
                    or self._coalesce_identity(normalized) != source.merge_identity):
                logger.warning("{} inbound coalesce release refused: authorization/binding changed", self.name)
                return False
        meta = copy.deepcopy(event.metadata)
        meta["attachments"] = copy.deepcopy(event.attachments)
        normalized = {**copy.deepcopy(event.normalized), "content": event.content,
                      "metadata": meta, "attachments": copy.deepcopy(event.attachments)}
        await self.publish_inbound(sender_id=event.sender_id, chat_id=event.chat_id,
                                   content=event.content, attachments=event.attachments,
                                   metadata=meta, session_key=self.build_session_key_override(normalized))
        return True

    def get_required_config_fields(self) -> list[str]:
        return []

    def get_missing_config_fields(self) -> list[str]:
        missing: list[str] = []
        for field_name in self.get_required_config_fields():
            value = getattr(self.config, field_name, None)
            if value is None:
                missing.append(field_name)
            elif isinstance(value, str) and not value.strip():
                missing.append(field_name)
            elif isinstance(value, list) and not value:
                missing.append(field_name)
        return missing

    def is_configured(self) -> bool:
        return not self.get_missing_config_fields()

    def account_identity(self) -> str:
        """Which account this channel is running as, if it can say.

        Not a secret: an app id is the public half of the pair, the half
        printed in a vendor console. It exists so a caller can ask "is this
        the account this customer's messages are supposed to go out under"
        without anyone handling the secret half.

        The default is "" -- this channel cannot say. A caller that needs the
        answer should treat that as unknown, never as a match.
        """
        return ""

    def is_allowed(self, sender_id: str) -> bool:
        allow_list = getattr(self.config, "allow_from", [])
        if not allow_list:
            logger.warning("{}: allow_from is empty — all access denied", self.name)
            return False
        if "*" in allow_list:
            return True
        sender = str(sender_id)
        return sender in allow_list or any(part in allow_list for part in sender.split("|") if part)

    def describe_capability(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "dependency": self.dependency_name,
            "running": self.is_running,
            "configured": self.is_configured(),
            "missing_config": self.get_missing_config_fields(),
            "last_error": self.last_error,
            "status_reason": self._status_reason,
            "started_at": self._started_at.isoformat() if self._started_at else "",
        }

    def decide_inbound(self, normalized: dict[str, Any], *, message_id: str = "") -> InboundDecision:
        """The four decisions, asked of the one door that makes them.

        Duplicate, allowed, who, which order: every channel used to
        answer these itself, in its own order, which is where a
        decision drifts. This asks ``accept_inbound`` with this
        channel's own dedup and its own allow list, so neither answer
        changes -- only the number of places that decide.

        No business store is passed, on purpose: no channel binds a
        message to an order today, and starting here would record a
        customer against an order in a place nobody reads.
        """
        meta = dict(normalized.get("metadata", {}) or {})
        try:
            return accept_inbound(
                channel=self.name,
                message_id=str(message_id or meta.get("message_id", "") or ""),
                sender_id=str(normalized.get("sender_id", "") or ""),
                content=str(normalized.get("content", "") or ""),
                reply_to=str(normalized.get("reply_to", "") or ""),
                thread_id=str(normalized.get("thread_id", "") or ""),
                dedup=self.inbound_dedup(),
                is_allowed=self.is_allowed,
            )
        except InboundNotSupported as exc:
            # Said, not raised. Feishu calls this from a coroutine whose
            # exception nobody retrieves, where raising would drop a
            # customer message with no log at all -- the exact silence
            # this door exists to prevent.
            logger.warning("{} inbound refused: {}", self.name, exc)
            return InboundDecision(accepted=False, reason=str(exc))

    def should_accept_inbound(self, normalized: dict[str, Any]) -> bool:
        sender_id = str(normalized.get("sender_id", "") or "")
        return bool(sender_id) and self.is_allowed(sender_id)

    def build_session_key_override(self, normalized: dict[str, Any]) -> str | None:
        _ = normalized
        return None

    def build_inbound_metadata(self, normalized: dict[str, Any], attachments: list[Any]) -> dict[str, Any]:
        metadata = dict(normalized.get("metadata", {}) or {})
        # Explicit adapter-normalized route fields outrank external metadata.
        for key in ("chat_id", "sender_id", "reply_to", "thread_id"):
            if key in normalized:
                metadata[key] = str(normalized.get(key) or "")
            else:
                metadata.setdefault(key, "")
        metadata["attachments"] = list(attachments or [])
        # Channel content is data supplied by an external party.  Do not let
        # an inbound payload mark itself as trusted through metadata; identity
        # and tool approval remain separate policy decisions.
        metadata["content_trust"] = "untrusted_external"
        metadata["content_trust_source"] = "channel_boundary"
        # "From outside" is one bit; what the message is trying to do is
        # another. The screen records the shape (instruction override, a
        # request for approval or credentials, links, code) without changing
        # the content or granting anything.
        stamp_screen(metadata, normalized.get("content", ""))
        return metadata

    def map_session(
        self,
        *,
        chat_id: str,
        thread_id: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        route = ChannelSessionMapping.derive(
            channel=self.name,
            chat_id=chat_id,
            thread_id=thread_id,
            reply_to=reply_to,
            metadata=metadata,
        )
        return {
            "session_id": route.session_id,
            "chat_id": route.chat_id,
            "thread_id": route.thread_id,
            "reply_to": route.reply_to,
        }

    async def publish_inbound(
        self,
        *,
        sender_id: str,
        chat_id: str,
        content: str,
        attachments: list[Any] | None = None,
        metadata: dict[str, Any] | None = None,
        session_key: str | None = None,
    ) -> None:
        if not self.is_allowed(sender_id):
            logger.warning("Access denied for sender {} on channel {}", sender_id, self.name)
            return
        meta = dict(metadata or {})
        # This path is also used by adapters that already normalized their
        # payload.  Re-assert the boundary marker here so callers cannot
        # bypass the trust classification by supplying custom metadata.
        meta["content_trust"] = "untrusted_external"
        meta["content_trust_source"] = "channel_boundary"
        stamp_screen(meta, content)
        route = self.map_session(
            chat_id=chat_id,
            thread_id=str(meta.get("thread_id", "") or ""),
            reply_to=str(meta.get("reply_to", "") or ""),
            metadata=meta,
        )
        await self.bus.publish_inbound(
            UserMessage(
                channel=self.name,
                user_id=str(sender_id),
                content=content,
                attachments=attachments or [],
                session_id=session_key or route["session_id"],
                metadata={
                    "chat_id": route["chat_id"],
                    "sender_id": str(sender_id),
                    "reply_to": route["reply_to"],
                    "thread_id": route["thread_id"],
                    "attachments": list(attachments or []),
                    **meta,
                },
            )
        )

    async def publish_normalized(self, normalized: dict[str, Any]) -> bool:
        if self._coalesce_stopped:
            return False
        sender_id = str(normalized.get("sender_id", "") or "")
        chat_id = str(normalized.get("chat_id", "") or "")
        content = str(normalized.get("content", "") or "")
        attachments = list(normalized.get("attachments", []) or [])
        if not sender_id or not chat_id:
            logger.debug("{} inbound payload missing sender/chat identifiers: {}", self.name, normalized)
            return False
        if not self.should_accept_inbound(normalized):
            logger.debug("{} inbound payload rejected by policy: {}", self.name, normalized)
            return False
        metadata = self.build_inbound_metadata(normalized, attachments)
        buf = self.inbound_coalesce()
        identity = self._coalesce_identity(normalized) if buf.enabled else None
        if not buf.enabled or identity is None:
            await self.publish_inbound(
                sender_id=sender_id,
                chat_id=chat_id,
                content=content,
                attachments=attachments,
                metadata=metadata,
                session_key=self.build_session_key_override(normalized),
            )
            return True
        from ampuly.channels.inbound_coalesce import CoalesceEvent

        offered = buf.offer(
            CoalesceEvent(
                channel=self.name,
                chat_id=chat_id,
                sender_id=sender_id,
                content=content,
                attachments=attachments,
                message_id=str(metadata.get("message_id", "") or ""),
                metadata=metadata,
                merge_identity=identity,
                normalized=copy.deepcopy(normalized),
            )
        )
        for item in offered.emit:
            await self._release_coalesced(item)
        # Only held media needs the one-shot flush; a sticky caption that was
        # already emitted waits for nothing (live check 2026-11-01: each text
        # message used to leave a no-op sleeping task behind).
        if offered.held and buf.has_pending_media():
            self._schedule_coalesce_flush()
        return True

    def _schedule_coalesce_flush(self) -> None:
        """One-shot debounce flush for held media; not a standing execution loop."""
        if getattr(self, "_coalesce_flush_scheduled", False):
            return
        try:
            import asyncio

            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._coalesce_flush_scheduled = True

        async def _flush_once() -> None:
            try:
                buf = self.inbound_coalesce()
                await asyncio.sleep((buf.window_ms / 1000.0) + 0.05)
                for item in buf.flush_due():
                    await self._release_coalesced(item)
            finally:
                self._coalesce_flush_scheduled = False
                buf = self.inbound_coalesce()
                if not self._coalesce_stopped and buf.has_pending_media():
                    self._schedule_coalesce_flush()

        self._coalesce_flush_task = loop.create_task(_flush_once())

    @abstractmethod
    async def start(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def stop(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def send(self, message: SystemMessage) -> None:
        raise NotImplementedError
