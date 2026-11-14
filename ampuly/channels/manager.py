"""Channel manager for Ampuly."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from ampuly.channels.base import BaseChannel
from ampuly.channels.provider_registry import PROVIDER_SPECS, ordered_provider_specs
from ampuly.core.config import AmpulyConfig
from ampuly.core.models import SystemMessage
from ampuly.layer0_interaction.message_bus import MessageBus


class ChannelManager:
    def __init__(self, config: AmpulyConfig, bus: MessageBus, ampuly_home: Any = None):
        self.config = config
        self.bus = bus
        # Where a channel may durably remember which inbound messages it has
        # already handled. Absent, that memory lives only in this process --
        # correct while it runs, forgotten across the restart that makes a
        # provider redeliver.
        self.ampuly_home = ampuly_home
        # Asked before every outbound message: may this connector send at all?
        # Injected, because "switched off by whom and why" is a business fact
        # this layer should not learn to read. Absent, everything sends, which
        # is what every installation already does.
        self.outbound_allowed: Any = None
        self.channels: dict[str, BaseChannel] = {}
        self.channel_errors: dict[str, str] = {}
        self._inbound_runtime_context: dict[str, str] | None = None
        self._inbound_scope_resolver: Any = None
        self._dispatch_task: asyncio.Task[Any] | None = None
        self._init_channels()

    def bind_inbound_scope(self, *, project_id: str, ampuly_home: Any,
                           scope_resolver: Any = None) -> None:
        """Bind server runtime identity, never a claimed order from wire metadata.

        A trusted per-event order/scope resolver is optional. Until one is
        available messages remain separate even when coalescing is enabled.
        Every rebind rotates the generation, fencing buffered old routes.
        """
        project = str(project_id or "").strip()
        if not project or ampuly_home is None:
            raise ValueError("inbound runtime scope requires project and home")
        home = str(Path(ampuly_home).resolve())
        self._inbound_runtime_context = {
            "project_id": project,
            "home_fingerprint": hashlib.sha256(home.encode()).hexdigest(),
            "binding_generation": uuid.uuid4().hex,
        }
        self._inbound_scope_resolver = scope_resolver
        for channel in self.channels.values():
            self._bind_channel_inbound_scope(channel)

    def _bind_channel_inbound_scope(self, channel: BaseChannel) -> None:
        context = self._inbound_runtime_context
        if context is None:
            return
        channel.coalesce_runtime_context = {
            **context, "account_id": str(channel.account_identity() or "")}
        resolver = self._inbound_scope_resolver
        if not callable(resolver):
            channel._coalesce_scope_resolver = None
            return
        def resolve(normalized: dict[str, Any]) -> dict[str, str] | None:
            current = resolver(channel, normalized)
            if not isinstance(current, dict):
                return None
            return {**current, **context, "binding_generation": json.dumps([context["binding_generation"], current.get("binding_generation")])}
        channel._coalesce_scope_resolver = resolve

    def _init_channels(self) -> None:
        for spec in ordered_provider_specs():
            cfg = getattr(self.config.channels, spec.name)
            if not cfg.enabled:
                continue
            channel = self._build_channel(spec.name, cfg, spec.module_name, spec.class_name)
            if channel is None:
                continue
            self.channels[spec.name] = channel
            logger.info("Channel configured: {}", spec.name)

    def _build_channel(self, key: str, cfg: Any, module_name: str, class_name: str) -> BaseChannel | None:
        try:
            module = __import__(module_name, fromlist=[class_name])
            cls = getattr(module, class_name)
            channel = cls(cfg, self.bus)
            if self.ampuly_home is not None:
                channel.ampuly_home = self.ampuly_home
            self._bind_channel_inbound_scope(channel)
            return channel
        except Exception as e:
            logger.warning("Channel {} not available: {}", key, e)
            self.channel_errors[key] = str(e)
            return None

    @property
    def enabled_channels(self) -> list[str]:
        return list(self.channels.keys())

    def get_channel(self, name: str) -> BaseChannel | None:
        return self.channels.get(name)

    def get_status(self, name: str) -> dict[str, Any]:
        spec = PROVIDER_SPECS[name]
        cfg = getattr(self.config.channels, name)
        enabled = bool(getattr(cfg, "enabled", False))
        channel = self.channels.get(name)
        capability = channel.describe_capability() if channel is not None else {
            "name": name,
            "delivery_mode": spec.delivery_mode,
            "available": False if spec.required_package and name in self.channel_errors else True,
            "configured": False,
            "missing_config": list(spec.required_config_fields),
            "running": False,
            "ready": False,
            "last_error": self.channel_errors.get(name, ""),
        }
        return {
            "name": name,
            "enabled": enabled,
            "delivery_mode": capability.get("delivery_mode", spec.delivery_mode),
            "available": capability.get("available", True),
            "configured": capability.get("configured", False),
            "ready": capability.get("ready", False),
            "running": capability.get("running", False),
            "missing_config": capability.get("missing_config", []),
            "last_error": capability.get("last_error", "") or self.channel_errors.get(name, ""),
            "bridge_required": spec.bridge_required,
            "extra_name": spec.extra_name,
            "required_package": spec.required_package,
        }

    def get_all_statuses(self) -> list[dict[str, Any]]:
        return [self.get_status(spec.name) for spec in ordered_provider_specs()]

    async def start_all(self) -> None:
        for name, channel in self.channels.items():
            try:
                await channel.start()
            except Exception as e:
                logger.warning("Failed to start channel {}: {}", name, e)
                channel.set_last_error(e)
        if self._dispatch_task is None:
            self._dispatch_task = asyncio.create_task(self._dispatch_outbound())

    async def stop_all(self) -> None:
        if self._dispatch_task:
            self._dispatch_task.cancel()
            self._dispatch_task = None
        for channel in self.channels.values():
            try:
                await channel.stop()
            except Exception:
                pass

    async def _dispatch_outbound(self) -> None:
        while True:
            message = await self.bus.get_response(timeout=1.0)
            if message is None:
                continue
            # A single failing send (network reset, malformed chat id, etc.) must not
            # terminate this loop — it is the only outbound consumer for every channel,
            # so one exception would silently stop all message delivery until restart.
            try:
                await self.dispatch_outbound(message)
            except Exception as exc:  # noqa: BLE001 - resilience of the dispatch loop
                logger.exception("Failed to dispatch outbound message: {}", exc)

    async def dispatch_outbound(self, message: SystemMessage) -> None:
        channel_name = message.channel or self.config.system.default_channel
        channel = self.channels.get(channel_name)
        if channel is None:
            logger.debug("No channel dispatcher for {}", channel_name)
            return
        refusal = self._switched_off(channel_name)
        if refusal:
            # Dropped on purpose and said out loud: a stop switch that
            # silently swallowed messages would be indistinguishable from a
            # broken channel.
            logger.warning("{} did not send: {}", channel_name, refusal)
            return
        await channel.send(message)

    def _switched_off(self, channel_name: str) -> str:
        if not callable(self.outbound_allowed):
            return ""
        try:
            return str(self.outbound_allowed(channel_name) or "").strip()
        except Exception as exc:  # noqa: BLE001 - not knowing is a reason to stop
            # An emergency stop that might not stop is not one. If the switch
            # cannot be read we do not know whether somebody hit it, and
            # sending in that state can reach a customer who asked for
            # silence. The two refusals are worded differently so an operator
            # fixes the right thing: "switched off" and "cannot tell" are not
            # the same problem.
            logger.error("could not read the {} switch; not sending: {}", channel_name, exc)
            return f"could not read the {channel_name} switch, so whether it is switched off is unknown"
