"""Turn one admitted declarative connector into bounded Harness tools.

The connector candidate lifecycle proves *what* may be called.  This module
is the deliberately small bridge that lets the existing NativeRuntime call
that contract:

* reads execute through the declarative adapter and an injected transport;
* writes never call the transport here.  They create a proposal through the
  caller's Effect boundary;
* credentials remain environment references and are never returned;
* the write idempotency identity comes from the trusted runtime task and the
  canonical parameters, never from model-authored arguments.

It is intentionally transport- and Engine-agnostic.  Production binds the
real Effect proposal service; deterministic trials bind a local journal.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections.abc import Callable, Mapping
from typing import Any

from ampuly.layer4_tools.registry import (
    COMPANY_EFFECT_NO_LOCAL_FS,
    COMPANY_EFFECT_RUNTIME_INTERNAL,
    ToolDefinition,
    ToolInvocationValidationError,
)

from .connector_contract import READ, WRITE, Operation
from .connector_runtime import DeclarativeConnectorAdapter


EnvironmentResolver = Callable[[], Mapping[str, str]]
EnabledCheck = Callable[[], bool | tuple[bool, str]]
WriteProposer = Callable[..., Any]


def connector_tool_name(connector: str, operation: str) -> str:
    return f"connector_{connector}_{operation}"


def create_declarative_connector_tools(
    adapter: DeclarativeConnectorAdapter,
    *,
    environment: EnvironmentResolver,
    transport: Callable[..., Any],
    enabled: EnabledCheck | None = None,
    propose_write: WriteProposer | None = None,
) -> list[ToolDefinition]:
    """Create one tool per declared operation without widening its contract."""

    contract = adapter.contract
    tools: list[ToolDefinition] = []
    for operation in contract.operations:
        name = connector_tool_name(contract.name, operation.op_id)
        parameters = _parameters(operation)
        if operation.kind == READ:
            tools.append(ToolDefinition(
                name=name,
                description=(operation.summary or f"Read through {contract.name}.{operation.op_id}"),
                parameters=parameters,
                func=_read_func(
                    adapter, operation, environment=environment,
                    transport=transport, enabled=enabled,
                ),
                category="connector",
                concurrency_safe=True,
                read_only=True,
                company_effect_kind=COMPANY_EFFECT_NO_LOCAL_FS,
                permission_effects=["network_access"],
            ))
            continue

        tools.append(ToolDefinition(
            name=name,
            description=(
                (operation.summary or f"Write through {contract.name}.{operation.op_id}")
                + " This only creates a governed Effect proposal; it never writes directly."
            ),
            parameters=parameters,
            func=_write_func(
                adapter, operation, enabled=enabled, propose_write=propose_write,
            ),
            category="connector",
            concurrency_safe=True,
            read_only=False,
            company_effect_kind=COMPANY_EFFECT_RUNTIME_INTERNAL,
            permission_effects=["runtime_internal"],
        ))
    return tools


def _read_func(
    adapter: DeclarativeConnectorAdapter,
    operation: Operation,
    *,
    environment: EnvironmentResolver,
    transport: Callable[..., Any],
    enabled: EnabledCheck | None,
) -> Callable[..., Any]:
    contract = adapter.contract

    async def run(task: Any = None, **arguments: Any) -> dict[str, Any]:
        _require_enabled(enabled, contract.name)
        response = await asyncio.to_thread(
            adapter.execute,
            operation.op_id,
            arguments,
            environment=dict(environment()),
            transport=transport,
        )
        receipt = {
            "connector": contract.name,
            "connector_version": contract.version,
            "contract_fingerprint": contract.fingerprint(),
            "operation": operation.op_id,
            "effect": READ,
            "status": response.status,
            "status_code": response.status_code,
            "body": response.body,
            "detail": response.detail,
        }
        _record_task_receipt(task, receipt)
        return receipt

    return run


def _write_func(
    adapter: DeclarativeConnectorAdapter,
    operation: Operation,
    *,
    enabled: EnabledCheck | None,
    propose_write: WriteProposer | None,
) -> Callable[..., Any]:
    contract = adapter.contract

    async def run(task: Any = None, **arguments: Any) -> dict[str, Any]:
        _require_enabled(enabled, contract.name)
        if propose_write is None:
            raise ToolInvocationValidationError(
                "connector write has no Effect proposal boundary",
                code="connector_effect_gateway_required",
                correction="Bind this connector to the SuperOnes Effect Gateway before retrying.",
            )
        anchor = _task_anchor(task)
        canonical = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(
            f"{contract.fingerprint()}\n{operation.op_id}\n{anchor}\n{canonical}".encode("utf-8")
        ).hexdigest()
        idempotency_key = f"connector:{contract.name}:{operation.op_id}:{digest}"
        proposal = propose_write(
            connector=contract.name,
            connector_version=contract.version,
            contract_fingerprint=contract.fingerprint(),
            operation=operation.op_id,
            params=dict(arguments),
            idempotency_key=idempotency_key,
            task=task,
        )
        if inspect.isawaitable(proposal):
            proposal = await proposal
        if not isinstance(proposal, dict):
            raise RuntimeError("Effect proposal boundary returned no structured receipt")
        receipt = {
            "connector": contract.name,
            "connector_version": contract.version,
            "contract_fingerprint": contract.fingerprint(),
            "operation": operation.op_id,
            "effect": WRITE,
            "idempotency_key": idempotency_key,
            **proposal,
        }
        _record_task_receipt(task, receipt)
        return receipt

    return run


def _parameters(operation: Operation) -> dict[str, Any]:
    properties = {
        name: {"description": f"Declared parameter {name}"}
        for name in operation.params
    }
    return {
        "type": "object",
        "properties": properties,
        "required": list(operation.params),
        "additionalProperties": False,
    }


def _require_enabled(check: EnabledCheck | None, connector: str) -> None:
    if check is None:
        return
    result = check()
    allowed, reason = (result if isinstance(result, tuple) else (bool(result), ""))
    if not allowed:
        raise ToolInvocationValidationError(
            f"connector {connector} is disabled" + (f": {reason}" if reason else ""),
            code="connector_disabled",
            correction="Ask the owner to enable the admitted connector before retrying.",
        )


def _task_anchor(task: Any) -> str:
    if task is None:
        raise ToolInvocationValidationError(
            "connector write requires a trusted runtime task",
            code="connector_runtime_context_required",
        )
    fields = (
        str(getattr(task, "project_id", "") or "").strip(),
        str(getattr(task, "session_id", "") or "").strip(),
        str(getattr(task, "id", "") or "").strip(),
    )
    if not all(fields):
        raise ToolInvocationValidationError(
            "connector write task has no durable project/session/task anchor",
            code="connector_runtime_anchor_required",
        )
    return ":".join(fields)


def _record_task_receipt(task: Any, result: dict[str, Any]) -> None:
    if task is None:
        return
    metadata = dict(getattr(task, "metadata", {}) or {})
    rows = list(metadata.get("component_receipts", []) or [])
    # The model may need the body, but the durable workflow receipt needs the
    # identity and outcome, not an unbounded copy of customer data.
    allowed = {
        "connector", "connector_version", "contract_fingerprint", "operation",
        "effect", "status", "status_code", "detail", "effect_id",
        "checkpoint_id", "awaiting_owner_approval", "idempotency_key",
    }
    rows.append({key: value for key, value in result.items() if key in allowed})
    metadata["component_receipts"] = rows[-32:]
    task.metadata = metadata
