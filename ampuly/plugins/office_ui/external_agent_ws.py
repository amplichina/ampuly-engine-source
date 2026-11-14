"""Owner console entry for host-authorized external-agent tasks.

The host provisions ``config/external_agents/<principal_id>.json`` beneath its
AMPULY home. Its only top-level fields are ``enabled``, ``expires_at`` (timezone
aware ISO 8601), ``secret_env`` and ``binding`` (ExternalAgentBinding fields).
This module only reads that file. Requests cannot install grants, choose a
workspace/budget/provider, open another Engine, or approve delivery effects.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hmac
import json
import os
from pathlib import Path
import re
from typing import Any

from ampuly.channels.external_agent_ingress import ExternalAgentEnvelope, IngressRejected
from ampuly.core.external_agent_service import ExternalAgentBinding, ExternalAgentRefused, ExternalAgentService
from ampuly.core.external_agent_readback import ExternalAgentReadbackError, ReadbackAdapter, require_readback_adapter
from ampuly.database.external_agent_tasks import ExternalAgentTaskRefused

_PRINCIPAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_GRANT_LIMIT = 64 * 1024
_ENVELOPE_FIELDS = frozenset({
    "message_id", "project_id", "sequence", "goal", "materials_digest", "signature", "channel",
})


@dataclass(frozen=True)
class _HostGrant:
    binding: ExternalAgentBinding
    secret: bytes
    document: dict[str, Any]


def _owner(handler: Any, ws: Any) -> None:
    from .ws_handler import _ws_owner_actor

    actor = _ws_owner_actor(handler, ws)
    # The helper returns other authenticated roles too; a login is not a grant.
    if not actor or actor.get("role") != "owner" or not actor.get("login"):
        raise ExternalAgentRefused("owner_required")


def _host_home(handler: Any) -> Path:
    root = getattr(handler, "_root_engine", None)
    if root is None:
        root = getattr(getattr(handler, "services_context", None), "root_engine", None)
    if root is None:
        root = getattr(handler, "engine", None)
    raw = getattr(root, "ampuly_home", None)
    if not isinstance(raw, (str, Path)) or not Path(raw).is_absolute():
        raise ExternalAgentRefused("host_home_unavailable")
    try:
        home = Path(raw).resolve(strict=True)
        if not home.is_dir():
            raise OSError("not a directory")
        return home
    except OSError:
        raise ExternalAgentRefused("host_home_unavailable") from None


def _load_grant(home: Path, principal: str) -> _HostGrant:
    if type(principal) is not str or not _PRINCIPAL.fullmatch(principal):
        raise ExternalAgentRefused("invalid_external_principal")
    path = home / "config" / "external_agents" / (principal + ".json")
    try:
        # A fixed filename does not authorize an escaped symlink/junction.
        if path.resolve(strict=True) != path or not path.is_file():
            raise ExternalAgentRefused("external_agent_grant_path")
        with path.open("rb") as stream:
            raw = stream.read(_GRANT_LIMIT + 1)
        if len(raw) > _GRANT_LIMIT:
            raise ExternalAgentRefused("external_agent_grant_invalid")
        doc = json.loads(raw)
    except (OSError, json.JSONDecodeError, UnicodeError):
        raise ExternalAgentRefused("external_agent_grant_unavailable") from None
    if type(doc) is not dict or set(doc) != {"enabled", "expires_at", "secret_env", "binding"}:
        raise ExternalAgentRefused("external_agent_grant_invalid")
    if doc["enabled"] is not True:
        raise ExternalAgentRefused("external_agent_grant_disabled")
    try:
        expires = datetime.fromisoformat(doc["expires_at"])
        if expires.tzinfo is None or expires.utcoffset() is None:
            raise ValueError("timezone required")
    except (TypeError, ValueError):
        raise ExternalAgentRefused("external_agent_grant_invalid") from None
    if expires <= datetime.now(timezone.utc):
        raise ExternalAgentRefused("external_agent_grant_expired")
    name = doc["secret_env"]
    if type(name) is not str or not _ENV_NAME.fullmatch(name):
        raise ExternalAgentRefused("external_agent_grant_invalid")
    secret = os.environ.get(name, "")
    if not secret:
        raise ExternalAgentRefused("external_agent_secret_unavailable")
    try:
        if type(doc["binding"]) is not dict:
            raise TypeError("binding required")
        binding = ExternalAgentBinding(**doc["binding"])
    except (TypeError, ValueError):
        raise ExternalAgentRefused("external_agent_grant_invalid") from None
    if binding.principal_id != principal:
        raise ExternalAgentRefused("external_agent_principal_mismatch")
    return _HostGrant(binding=binding, secret=secret.encode("utf-8"), document=doc)


def register_external_agent_readback(engine: Any, provider_name: str, adapter: ReadbackAdapter) -> None:
    """Host SDK hook for an independent recipient read; never a send receipt.

    Explicit ``trusted_sync`` or ``async_io`` adapters receive ReadbackRequest
    with query scope and limits, and return a complete ReadbackBatch. Only host
    code may register one; this does not provision a real platform adapter.
    """
    if type(provider_name) is not str or not provider_name.strip():
        raise ExternalAgentRefused("invalid_readback_adapter")
    try:
        require_readback_adapter(adapter)
    except ExternalAgentReadbackError as exc:
        raise ExternalAgentRefused(exc.code) from None
    registry = getattr(engine, "_external_agent_readback_adapters", None)
    if registry is None:
        registry = {}
        engine._external_agent_readback_adapters = registry
    if type(registry) is not dict:
        raise ExternalAgentRefused("invalid_readback_registry")
    current = registry.get(provider_name)
    if current is not None and current is not adapter:
        if type(current) is not ReadbackAdapter:
            raise ExternalAgentRefused("invalid_readback_registry")
        if current.active:
            raise ExternalAgentRefused("external_agent_readback_busy")
    registry[provider_name] = adapter


def _service(handler: Any, principal: str, *, expected_grant: _HostGrant | None = None) -> ExternalAgentService:
    home = _host_home(handler)
    grant = _load_grant(home, principal)
    if expected_grant is not None and (grant.document != expected_grant.document
            or not hmac.compare_digest(grant.secret, expected_grant.secret)):
        raise ExternalAgentRefused("external_agent_grant_changed")

    def reload_binding() -> ExternalAgentBinding:
        # Effects can be approved after the submitting socket has closed. Their
        # durable authority is the host grant and Effect approval, not a socket.
        if getattr(handler, "_shutting_down", False):
            raise ExternalAgentRefused("external_agent_stopped")
        current = _load_grant(home, principal)
        if current.document != grant.document or not hmac.compare_digest(current.secret, grant.secret):
            raise ExternalAgentRefused("external_agent_grant_changed")
        return current.binding

    # loaded() only returns a running root/delegate. require() could initialize
    # a second engine and drain its outbox, so this entry never calls it.
    engine = handler._project_engines().loaded(grant.binding.project_id)
    if engine is None or not handler._store_is_ready(getattr(engine, "store", None)):
        raise ExternalAgentRefused("ready_native_engine_required")
    engine_home = getattr(engine, "ampuly_home", None)
    if not isinstance(engine_home, (str, Path)) or Path(engine_home).resolve() != home:
        raise ExternalAgentRefused("host_home_mismatch")
    business = handler._gate_business()
    if business is None:
        raise ExternalAgentRefused("host_business_unavailable")
    adapters = getattr(engine, "_external_agent_readback_adapters", {})
    if type(adapters) is not dict:
        raise ExternalAgentRefused("invalid_readback_registry")
    readback = adapters.get(grant.binding.provider_name)
    try:
        if readback is not None:
            require_readback_adapter(readback)
    except ExternalAgentReadbackError as exc:
        raise ExternalAgentRefused(exc.code) from None
    return ExternalAgentService(engine=engine, business_store=business,
                                binding_loader=reload_binding, secret=grant.secret, readback=readback)


def _error_code(exc: Exception) -> str:
    if isinstance(exc, (ExternalAgentRefused, ExternalAgentTaskRefused)):
        return exc.code
    if isinstance(exc, IngressRejected):
        return exc.reason
    return "external_agent_unavailable"


async def _background_action(handler: Any, ws: Any, service: ExternalAgentService,
                             operation: str, message_id: str, ready: asyncio.Event) -> None:
    action = "external_agent_" + operation
    try:
        # The scheduling receipt must precede a fast result on the same socket.
        await ready.wait()
        result = await getattr(service, operation)(message_id)
    except asyncio.CancelledError:
        try:
            await handler._send_ack(ws, ok=False, action=action, phase="result", operation_finished=False,
                                    message_id=message_id, error="external_agent_cancelled")
        except Exception:
            pass  # A closed socket must not turn cancellation into an error.
        raise
    except Exception as exc:
        await handler._send_ack(ws, ok=False, action=action, phase="result", operation_finished=True,
                                message_id=message_id, error=_error_code(exc))
    else:
        await handler._send_ack(ws, ok=True, action=action, phase="result", operation_finished=True, **result)


async def _handle(handler: Any, ws: Any, data: dict, operation: str) -> None:
    action = "external_agent_" + operation
    try:
        _owner(handler, ws)
        if getattr(handler, "_shutting_down", False):
            raise ExternalAgentRefused("external_agent_stopped")
        allowed = {"type", "principal_id", "envelope" if operation == "submit" else "message_id"}
        if type(data) is not dict or set(data) - allowed or data.get("type", action) != action:
            raise ExternalAgentRefused("invalid_external_agent_request")
        principal = data.get("principal_id")
        if type(principal) is not str or not _PRINCIPAL.fullmatch(principal):
            raise ExternalAgentRefused("invalid_external_principal")
        if operation == "submit":
            raw = data.get("envelope")
            if type(raw) is not dict or set(raw) - _ENVELOPE_FIELDS:
                raise ExternalAgentRefused("invalid_external_agent_envelope")
            try:
                argument = ExternalAgentEnvelope(**raw)
            except TypeError:
                raise ExternalAgentRefused("invalid_external_agent_envelope") from None
        else:
            argument = data.get("message_id")
            if (type(argument) is not str or not argument or argument != argument.strip()
                    or len(argument.encode("utf-8")) > 256 or any(ord(c) < 32 for c in argument)):
                raise ExternalAgentRefused("invalid_message_id")
        service = _service(handler, principal)
        if operation in {"execute", "deliver"}:
            # Resolve the persisted task before scheduling. The connection's
            # single inbox worker remains free for status, approval and stop.
            state = await service.status(argument)
            _owner(handler, ws)
            if getattr(handler, "_shutting_down", False):
                raise ExternalAgentRefused("external_agent_stopped")
            ready = asyncio.Event()
            coro = _background_action(handler, ws, service, operation, argument, ready)
            try:
                background = handler._track_session(state["task_id"], coro,
                                                    project_id=state["project_id"], engine=service.engine)
            except BaseException:
                coro.close()
                raise
            try:
                await handler._send_ack(ws, ok=True, action=action, phase="accepted", operation_finished=False,
                                        status="scheduled", task_id=state["task_id"],
                                        project_id=state["project_id"], message_id=argument)
            except BaseException:
                background.cancel()
                raise
            ready.set()
            return
        result = await getattr(service, operation)(argument)
    except Exception as exc:
        # A raw exception can contain credentials/configuration. Stable codes
        # are sufficient for this boundary; cancellation remains cancellation.
        await handler._send_ack(ws, ok=False, action=action, error=_error_code(exc))
        return
    await handler._send_ack(ws, ok=True, action=action, **result)


async def handle_external_agent_submit(handler: Any, ws: Any, data: dict) -> None:
    await _handle(handler, ws, data, "submit")


async def handle_external_agent_status(handler: Any, ws: Any, data: dict) -> None:
    await _handle(handler, ws, data, "status")


async def handle_external_agent_execute(handler: Any, ws: Any, data: dict) -> None:
    await _handle(handler, ws, data, "execute")


async def handle_external_agent_deliver(handler: Any, ws: Any, data: dict) -> None:
    await _handle(handler, ws, data, "deliver")
