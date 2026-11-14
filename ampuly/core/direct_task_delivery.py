"""Fixed output for a direct task that has a body of work to hand off.

A master-control chat reply is not this record. Callers pass the finished
text of a direct task. Order, flow, and node keys are refused. Storage
failure returns unconfirmed and does not call a model.

The implementation lives here. ``ampuly.plugins.office_ui.direct_task_delivery``
re-exports it. The engine imports this module, not the plugin.
"""
from __future__ import annotations

from typing import Any

from ampuly.core.governed_delivery import ENTRY_DIRECT, write_fixed_artifact, write_fixed_handoff


async def record_direct_fixed_output(
    store: Any,
    *,
    project_id: str,
    task_id: str,
    content: str,
    budget_id: str,
    session_id: str = "",
    harness_turn_id: str = "",
    name: str = "direct-output.txt",
    format_: str = "text",
    limits: dict[str, Any] | None = None,
    budget: dict[str, Any] | None = None,
    attempt: int = 1,
    order_id: str = "",
    workflow_id: str = "",
    node_id: str = "",
    work_item_id: str = "",
) -> tuple[dict[str, Any] | None, str, bool]:
    """Write the shared artifact and handoff. Returns unconfirmed on storage failure."""
    ref = await write_fixed_artifact(
        store,
        entry=ENTRY_DIRECT,
        project_id=project_id,
        task_id=task_id,
        role_id="direct",
        name=name,
        content=content,
        format_=format_,
        budget_id=budget_id,
        harness_session_id=session_id,
        harness_turn_id=harness_turn_id,
        order_id=order_id,
        workflow_id=workflow_id,
        node_id=node_id,
        work_item_id=work_item_id,
    )
    if ref is None:
        return None, "", False
    handoff_id, recorded = await write_fixed_handoff(
        store,
        entry=ENTRY_DIRECT,
        project_id=project_id,
        task_id=task_id,
        session_id=session_id,
        from_role="direct",
        summary=content,
        artifacts=[ref],
        budget_id=budget_id,
        harness_turn_id=harness_turn_id,
        limits=limits,
        budget=budget,
        output_schema_checked=False,
        attempt=attempt,
        order_id=order_id,
        workflow_id=workflow_id,
        node_id=node_id,
        work_item_id=work_item_id,
    )
    return ref, handoff_id, recorded


async def finish_direct_task_body(store: Any, task: Any, result: Any) -> Any:
    """Record one finished direct task that has a body of work to hand off.

    A master-control chat reply is not this record, and neither is an empty
    turn. Storage failure leaves the task unconfirmed. This function does not
    call a model.
    """
    from ampuly.core.governed_delivery import GovernedDeliveryRefused
    from ampuly.core.models import TaskStatus

    metadata = dict(getattr(task, "metadata", {}) or {})
    content = getattr(result, "content", None)
    status = getattr(result, "status", None)
    status_value = str(getattr(status, "value", status) or "")
    if (
        str(metadata.get("interface") or "") == "master_control"
        or str(metadata.get("execution_source") or "") != "direct_task"
        or status_value != TaskStatus.DONE.value
        or type(content) is not str
        or not content.strip()
    ):
        return result
    try:
        ref, handoff_id, recorded = await record_direct_fixed_output(
            store,
            project_id=str(getattr(task, "project_id", "") or metadata.get("project_id") or ""),
            task_id=str(getattr(task, "id", "") or ""),
            content=content,
            budget_id=str(metadata.get("budget_id") or ""),
            session_id=str(getattr(task, "session_id", "") or metadata.get("harness_session_id") or ""),
            harness_turn_id=str(metadata.get("harness_turn_id") or ""),
        )
    except GovernedDeliveryRefused:
        ref, handoff_id, recorded = None, "", False
    if ref is None or not recorded:
        return await _mark_direct_delivery_unconfirmed(store, task, result, metadata)
    metadata["direct_delivery_state"] = "recorded"
    metadata["direct_acceptance_state"] = "pending"
    metadata["direct_delivered"] = False
    metadata["governed_handoff_id"] = handoff_id
    metadata["direct_output_receipts"] = [ref]
    task.metadata = metadata
    saver = getattr(store, "save_task", None)
    if callable(saver):
        try:
            await saver(task)
        except Exception:
            return await _mark_direct_delivery_unconfirmed(store, task, result, metadata)
    return result


async def _mark_direct_delivery_unconfirmed(store: Any, task: Any, result: Any, metadata: dict[str, Any]) -> Any:
    from ampuly.core.models import TaskStatus

    metadata["direct_delivery_state"] = "outcome_unknown"
    metadata["direct_delivery_consult_started"] = False
    task.metadata = metadata
    try:
        task.status = TaskStatus.BLOCKED
    except Exception:
        pass
    result.status = TaskStatus.BLOCKED
    artifacts = dict(getattr(result, "artifacts", {}) or {})
    artifacts["direct_delivery_state"] = "outcome_unknown"
    result.artifacts = artifacts
    saver = getattr(store, "save_task", None)
    if callable(saver):
        try:
            await saver(task)
        except Exception:
            pass
    return result
