"""Bounded read-only reconstruction using the real synchronous get_order API.

No store/runtime is imported or constructed. Inject BusinessStore.get_order;
all reads carry an explicit project_id. This adapter never writes ancestry into
an existing order, changes an invocation key, or decides to relaunch a child.
"""
from __future__ import annotations

import copy
import hashlib
import inspect
import json
from typing import Any, Callable

from .workflow_invocation_guard import (
    MAX_WORKFLOW_INVOCATION_DEPTH,
    WorkflowInvocationRejected,
    _workflow_id,
    advance_workflow_ancestry,
)

PARENT_CONTRACT_FIELD = "_workflow_parent"
ANCESTRY_FIELD = "workflow_ancestry"


def _reject(code: str, message: str) -> None:
    raise WorkflowInvocationRejected(code, message)


def _policy(max_depth: int) -> None:
    if type(max_depth) is not int or not 0 <= max_depth <= MAX_WORKFLOW_INVOCATION_DEPTH:
        _reject("workflow_depth_policy_invalid", "Workflow invocation depth policy is invalid")


def _order(order: Any, project_id: str) -> tuple[str, str, dict[str, Any]]:
    if order is None:
        _reject("workflow_lineage_unavailable", "Workflow order is unavailable")
    order_id = _workflow_id(getattr(order, "order_id", None))
    workflow_id = _workflow_id(getattr(order, "workflow_id", None))
    if _workflow_id(getattr(order, "project_id", None)) != project_id:
        _reject("workflow_lineage_scope_conflict", "Workflow ancestry belongs to another project")
    fields = getattr(order, "fields", None)
    if not isinstance(fields, dict):
        _reject("workflow_lineage_invalid", "Workflow order fields are invalid")
    return order_id, workflow_id, fields


def _key(parent_task_id: str, node_id: str, target_workflow_id: str) -> str:
    # Matches the existing ws_handler._workflow_invocation_key, schema 1.
    raw = json.dumps({
        "schema_version": 1, "parent_task_id": parent_task_id,
        "node_id": node_id, "target_workflow_id": target_workflow_id,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "workflow-node:" + hashlib.sha256(raw).hexdigest()


def _parent(order: Any, workflow_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    if PARENT_CONTRACT_FIELD not in fields:
        # A durable node invocation is a child even if its contract was lost.
        if getattr(order, "invocation_key", "") or getattr(order, "source", "") == "intake_flow":
            _reject("workflow_lineage_unavailable", "Child workflow parent contract is unavailable")
        return None
    parent = fields[PARENT_CONTRACT_FIELD]
    if not isinstance(parent, dict) or type(parent.get("schema_version")) is not int or parent["schema_version"] != 1:
        _reject("workflow_lineage_invalid", "Child workflow parent contract is invalid")
    target = _workflow_id(parent.get("target_workflow_id"))
    task_id = _workflow_id(parent.get("parent_task_id"))
    node_id = _workflow_id(parent.get("node_id"))
    if target != workflow_id:
        _reject("workflow_lineage_invalid", "Child workflow target conflicts with its parent contract")
    if (getattr(order, "source", None) != "intake_flow"
            or getattr(order, "source_task_id", None) != task_id
            or getattr(order, "invocation_key", None) != _key(task_id, node_id, target)):
        _reject("workflow_lineage_invalid", "Child workflow invocation identity conflicts with its parent contract")
    if not parent.get("parent_order_id"):
        _reject("workflow_lineage_unavailable", "Child workflow parent order is unavailable")
    _workflow_id(parent["parent_order_id"])
    return parent


def reconstruct_workflow_ancestry(
    current_order: Any,
    *,
    project_id: str,
    get_order: Callable[..., Any | None],
    max_depth: int = MAX_WORKFLOW_INVOCATION_DEPTH,
) -> tuple[str, ...]:
    """Root-through-current IDs, verified against at most max_depth parent reads.

    Legacy child contracts need no new ancestry field: reconstruct from their
    real parent_order_id links. If a field exists, compare it with the actual
    prefix; it is never a shortcut around order reads. The current order counts
    toward the max_depth+1 visited-order bound. Missing data fails closed.
    """
    _policy(max_depth)
    project = _workflow_id(project_id)  # explicit; never defaults to "default"
    if not callable(get_order) or inspect.iscoroutinefunction(get_order):
        _reject("workflow_lineage_invalid", "A synchronous project-scoped order reader is required")
    reverse: list[tuple[str, dict[str, Any] | None]] = []
    seen_orders: set[str] = set()
    seen_flows: set[str] = set()
    order = current_order
    expected_order_id: str | None = None
    while True:
        order_id, workflow_id, fields = _order(order, project)
        if expected_order_id is not None and order_id != expected_order_id:
            _reject("workflow_lineage_invalid", "Order reader returned a different parent order")
        if order_id in seen_orders or workflow_id in seen_flows:
            _reject("workflow_invocation_cycle", "Workflow ancestry contains an order or flow cycle")
        seen_orders.add(order_id)
        seen_flows.add(workflow_id)
        parent = _parent(order, workflow_id, fields)
        reverse.append((workflow_id, parent))
        if parent is None:
            break
        if len(reverse) > max_depth:
            _reject("workflow_invocation_depth_exceeded", "Workflow invocation depth limit reached")
        expected_order_id = _workflow_id(parent["parent_order_id"])
        if expected_order_id in seen_orders:
            _reject("workflow_invocation_cycle", "Workflow ancestry contains an order cycle")
        try:
            order = get_order(expected_order_id, project_id=project)
        except Exception as exc:
            raise WorkflowInvocationRejected("workflow_lineage_unavailable", "Workflow parent order could not be read") from exc
        if inspect.isawaitable(order):
            if inspect.iscoroutine(order):
                order.close()
            _reject("workflow_lineage_invalid", "Order reader returned an asynchronous result")
    ordered = list(reversed(reverse))
    ancestry = tuple(row[0] for row in ordered)
    for index, (_workflow, parent) in enumerate(ordered):
        if parent is not None and ANCESTRY_FIELD in parent:
            claimed = parent[ANCESTRY_FIELD]
            if (not isinstance(claimed, (list, tuple)) or not claimed
                    or len(claimed) > max_depth + 1):
                _reject("workflow_lineage_invalid", "Stored workflow ancestry is invalid")
            if tuple(_workflow_id(item) for item in claimed) != ancestry[:index + 1]:
                _reject("workflow_lineage_invalid", "Stored workflow ancestry conflicts with its real parent orders")
    return ancestry


def preserve_child_invocation_fields(
    existing_child: Any,
    *,
    invoking_order: Any,
    project_id: str,
    invoking_task_id: str,
    node_id: str,
    target_workflow_id: str,
    get_order: Callable[..., Any | None],
    max_depth: int = MAX_WORKFLOW_INVOCATION_DEPTH,
) -> dict[str, Any]:
    """Validate a node retry, return the original fields without inserting keys.

    The caller also preserves child's source/source_task/invocation_key and
    customer/environment arguments. This function makes no lifecycle decision:
    cancelled, completed or unknown children must not be auto-relaunched here.
    """
    project = _workflow_id(project_id)
    invoking_id, current_flow, _fields = _order(invoking_order, project)
    _child_id, child_flow, fields = _order(existing_child, project)
    parent = _parent(existing_child, child_flow, fields)
    task_id = _workflow_id(invoking_task_id)
    node = _workflow_id(node_id)
    target = _workflow_id(target_workflow_id)
    if (parent is None or parent["parent_order_id"] != invoking_id
            or parent["parent_task_id"] != task_id or parent["node_id"] != node
            or child_flow != target):
        _reject("workflow_lineage_invalid", "Existing child belongs to another invocation")
    source = reconstruct_workflow_ancestry(invoking_order, project_id=project, get_order=get_order, max_depth=max_depth)
    expected = advance_workflow_ancestry(
        current_workflow_id=current_flow, target_workflow_id=target,
        ancestry=source, max_depth=max_depth,
    )
    actual = reconstruct_workflow_ancestry(existing_child, project_id=project, get_order=get_order, max_depth=max_depth)
    if actual != expected:
        _reject("workflow_lineage_invalid", "Existing child ancestry conflicts with the invoking order")
    return copy.deepcopy(fields)
