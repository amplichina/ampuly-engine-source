"""Bind admitted project connectors into the existing Engine/Harness runtime."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ampuly.effects import ApprovalType

from .connector_candidate import (
    ConnectorCandidateError,
    candidate_by_id,
    list_registrations,
    project_candidate_root,
)
from .connector_effect import DeclarativeConnectorEffectProvider
from .connector_runtime import ConnectorRequest, DeclarativeConnectorAdapter
from .connector_tools import create_declarative_connector_tools


_MAX_RESPONSE_BYTES = 1_000_000


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def bounded_http_transport(request: ConnectorRequest) -> tuple[int, str]:
    """One bounded HTTP call; credentials are never followed to a redirect."""

    body = None
    if request.body is not None:
        body = json.dumps(request.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    outgoing = Request(
        request.url,
        data=body,
        headers=dict(request.headers),
        method=request.method,
    )
    opener = build_opener(_NoRedirect())
    try:
        response = opener.open(outgoing, timeout=request.timeout_sec)
    except HTTPError as exc:
        raw = exc.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise RuntimeError("connector response exceeds 1000000 bytes") from exc
        return int(exc.code), raw.decode("utf-8", errors="replace")
    with response:
        raw = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise RuntimeError("connector response exceeds 1000000 bytes")
        return int(response.status), raw.decode("utf-8", errors="replace")


def bind_registered_connectors(
    engine: Any,
    business: Any,
    project_id: str,
    *,
    transport: Callable[[ConnectorRequest], tuple[int, str]] | None = None,
) -> list[dict[str, Any]]:
    """Install governed connector tools/providers for one exact project.

    Registration remains default-disabled: an admitted connector is usable
    only after the owner has created an explicit enabled switch record.
    """

    clean_project = str(project_id or "default").strip() or "default"
    engine_project = str(getattr(engine, "project_id", "") or "default").strip() or "default"
    if engine_project != clean_project:
        raise ConnectorCandidateError("connector project scope differs from the active engine")
    home = Path(getattr(engine, "ampuly_home", "")).resolve()
    root = project_candidate_root(home, clean_project)
    registrations = list_registrations(root / "registry")
    gateway = getattr(engine, "effect_gateway", None)
    registry = getattr(engine, "tool_registry", None)
    if gateway is None or registry is None:
        raise ConnectorCandidateError("active engine has no Effect Gateway or tool registry")
    existing_providers = {provider.name for provider in gateway.providers()}
    bound: list[dict[str, Any]] = []
    for entry in registrations:
        if str(entry.get("status", "")) != "registered":
            continue
        candidate = candidate_by_id(root, str(entry.get("candidate_id", "") or ""))
        if candidate.fingerprint != str(entry.get("candidate_fingerprint", "") or ""):
            raise ConnectorCandidateError("registered connector fingerprint differs from its candidate")
        adapter = DeclarativeConnectorAdapter(candidate.contract)
        name = candidate.contract.name

        def environment(contract=candidate.contract):
            names = [contract.base_url_env]
            if contract.credential_env:
                names.append(contract.credential_env)
            return {key: os.environ.get(key, "") for key in names}

        def enabled(connector=name):
            switch = business.connector_switch(connector)
            allowed = bool(switch.get("explicit")) and bool(switch.get("enabled"))
            reason = str(switch.get("reason", "") or "not explicitly enabled by owner")
            return allowed, reason

        provider = DeclarativeConnectorEffectProvider(
            adapter, environment=environment,
            transport=transport or bounded_http_transport,
            enabled=enabled,
        )
        if provider.name not in existing_providers:
            gateway.register(provider)
            existing_providers.add(provider.name)

        async def propose_write(
            *, connector: str, connector_version: str,
            contract_fingerprint: str, operation: str, params: dict[str, Any],
            idempotency_key: str, task: Any, _provider: str = provider.name,
        ) -> dict[str, Any]:
            metadata = dict(getattr(task, "metadata", {}) or {})
            run_id = str(metadata.get("delegation_run_id") or "").strip()
            session_id = str(getattr(task, "session_id", "") or "").strip()
            publication = await engine.propose_effect_for_approval(
                provider=_provider,
                kind="connector.write",
                payload={
                    "connector": connector,
                    "connector_version": connector_version,
                    "contract_fingerprint": contract_fingerprint,
                    "operation": operation,
                    "params": dict(params),
                },
                idempotency_key=idempotency_key,
                prompt=f"是否允许 {connector}.{operation} 执行这次外部写入？",
                preview={"connector": connector, "operation": operation, "params": dict(params)},
                approval_type=ApprovalType.TECHNICAL,
                run_id=run_id,
                customer_id=str(metadata.get("customer_id") or metadata.get("order_id") or "").strip(),
                environment_id=str(metadata.get("environment_id") or "production").strip(),
                correlation_id=run_id or session_id,
                causation_id=str(getattr(task, "id", "") or "").strip(),
                task_id=str(getattr(task, "id", "") or "").strip(),
                session_id=session_id,
            )
            return {
                "status": publication.effect.state.value,
                "effect_id": publication.effect.effect_id,
                "checkpoint_id": publication.checkpoint.checkpoint_id,
                "awaiting_owner_approval": publication.effect.state.value == "awaiting_approval",
            }

        tools = create_declarative_connector_tools(
            adapter,
            environment=environment,
            transport=transport or bounded_http_transport,
            enabled=enabled,
            propose_write=propose_write,
        )
        for tool in tools:
            registry.register(tool)
        switch = business.connector_switch(name)
        bound.append({
            "connector": name,
            "version": candidate.contract.version,
            "candidate_fingerprint": candidate.fingerprint,
            "provider": provider.name,
            "tools": [tool.name for tool in tools],
            "enabled": bool(switch.get("explicit")) and bool(switch.get("enabled")),
        })
    return bound
