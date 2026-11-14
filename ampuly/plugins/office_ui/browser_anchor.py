"""Deterministic browser-to-order anchoring for the owner console.

A browser runtime is keyed by the Task that operates it.  In company mode
that Task is usually a role work item, while the owner reasons about the
user-facing order.  The mapping must come from the durable runtime identity
graph: ``origin_task_id`` alone is model-shapeable metadata and cannot choose
between two orders for the same customer.
"""

from __future__ import annotations

from typing import Any

from ampuly.layer2_organization.company_runtime_identity import (
    is_company_runtime_task,
    load_company_runtime_identity_index,
)


EMPTY_BROWSER_ANCHOR = {"order_id": "", "anchor_session_id": ""}
EMPTY_BROWSER_NODE = {"node_id": "", "node_name": ""}


def _text(value: Any) -> str:
    return str(value or "").strip()


async def resolve_browser_order_anchor(
    store: Any,
    *,
    task_id: str,
    project_id: str = "",
) -> dict[str, str]:
    """Return the unique durable order/session owning ``task_id``.

    Plain task-mode browser sessions deliberately have no order anchor.  A
    company work item is shown only when its own durable Task identity and
    any declared ``origin_task_id`` resolve to the same runtime.  Missing,
    ambiguous, cross-project, or stale evidence fails closed.
    """

    empty = dict(EMPTY_BROWSER_ANCHOR)
    requested_task_id = _text(task_id)
    requested_project_id = _text(project_id)
    getter = getattr(store, "get_task", None)
    if not requested_task_id or not callable(getter):
        return empty

    try:
        task = await getter(requested_task_id)
    except Exception:  # noqa: BLE001 - unreadable durable identity is no identity
        return empty
    if task is None:
        return empty

    task_project_id = _text(getattr(task, "project_id", "")) or "default"
    project = requested_project_id or task_project_id
    if task_project_id != project or not is_company_runtime_task(task):
        return empty

    try:
        index = await load_company_runtime_identity_index(store, project)
        identity = index.resolve(task_id=requested_task_id)
    except Exception:  # noqa: BLE001 - console projection must fail closed
        return empty
    if identity is None or identity.project_id != project:
        return empty

    order_id = _text(identity.ui_anchor_task_id)
    runtime_session_id = _text(identity.runtime_session_id)
    if not order_id or not runtime_session_id:
        return empty

    metadata = dict(getattr(task, "metadata", {}) or {})
    declared_origin = _text(metadata.get("origin_task_id"))
    if declared_origin:
        declared_identity = index.resolve(task_id=declared_origin)
        if declared_identity is None or declared_identity.runtime_session_id != runtime_session_id:
            return empty
        if _text(declared_identity.ui_anchor_task_id) != order_id:
            return empty

    return {"order_id": order_id, "anchor_session_id": runtime_session_id}


async def resolve_browser_node(store: Any, *, task_id: str, project_id: str = "") -> dict[str, str]:
    """Q05 B5: the flow step whose work item opened a browser session -- its id and name as the flow has them.

    Read from the work item's own durable record (``workflow_node``, written when its turn started), in this project
    only. Empty outside a flow, for another project's task, or when the record cannot be read.
    """
    empty = dict(EMPTY_BROWSER_NODE)
    requested_task_id = _text(task_id)
    getter = getattr(store, "get_task", None)
    if not requested_task_id or not callable(getter):
        return empty
    try:
        task = await getter(requested_task_id)
    except Exception:  # noqa: BLE001 - an unreadable record names no step
        return empty
    if task is None or (_text(getattr(task, "project_id", "")) or "default") != (_text(project_id) or "default"):
        return empty
    node = dict(getattr(task, "metadata", {}) or {}).get("workflow_node")
    if not isinstance(node, dict):
        return empty
    return {"node_id": _text(node.get("id"))[:80], "node_name": _text(node.get("name"))[:80]}
