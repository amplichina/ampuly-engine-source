"""Workflow employee nodes publish the same fixed delivery as a direct employee.

The company scheduler still owns phase changes. This writer adds the shared
artifact and handoff facts. A storage failure leaves the turn blocked instead
of looking delivered.
"""
from __future__ import annotations

from typing import Any
import asyncio

from ampuly.core.employee_output_contract import EmployeeOutputContractError, output_validator, checked_output
from ampuly.core.governed_delivery import ENTRY_WORKFLOW, assert_entry_identity, write_fixed_artifact, write_fixed_handoff
from ampuly.core.models import TaskStatus
from ampuly.core.workflow_node_delivery import workflow_node_delivery_kind

__all__ = [
    "publish_workflow_employee_delivery",
    "workflow_node_delivery_kind",
]


def _status(result: Any) -> str:
    raw = getattr(result, "status", "")
    return str(getattr(raw, "value", raw) or "")


async def publish_workflow_employee_delivery(store: Any, turn: Any, result: Any) -> None:
    """Validate and write every required output before Harness records DONE.

    A partial/unknown write is retained as evidence, never retried here. The
    caller owns final task persistence and the original execution authority.
    """
    content = getattr(result, "content", None)
    if result is None or _status(result) != TaskStatus.DONE.value:
        return
    identity = turn.basis.identity
    manifest = turn.basis.package_manifest()
    instance = turn.basis.instance_snapshot()
    task = turn.task
    contract = manifest.get("artifact_contract", {}) if type(manifest) is dict else {}
    metadata = dict(getattr(task, "metadata", {}) or {})
    metadata.update(employee_output_confirmed=False, employee_output_receipts=[],
                    employee_delivered=False, employee_acceptance_state="pending")
    metadata.pop("governed_handoff_id", None)
    task.metadata = metadata

    def block(state: str, code: str) -> None:
        progress = metadata.get("employee_run_progress")
        if type(progress) is dict:
            progress.update(state="outcome_pending" if state == "outcome_unknown" else "waiting_decision",
                            wait_reason="disposition_pending")
        metadata["employee_delivery_state"] = state
        metadata["employee_delivery_error"] = code
        task.metadata = metadata
        result.status = TaskStatus.BLOCKED
        result.artifacts = {**dict(getattr(result, "artifacts", {}) or {}),
            "employee_outputs": list(metadata["employee_output_receipts"]),
            "employee_output_confirmed": False}

    try:
        _, validator = output_validator(manifest)
        deliveries = checked_output(manifest, content, validator)
    except EmployeeOutputContractError as exc:
        block("contract_failed", exc.code)
        return
    assert_entry_identity(ENTRY_WORKFLOW, order_id=identity.order_id,
                          workflow_id=identity.workflow_id, node_id=identity.node_id)
    if (store is None or not callable(getattr(store, "record_artifact", None))
            or not callable(getattr(store, "save_handoff_record", None))):
        block("outcome_unknown", "employee_delivery_store_unavailable")
        return
    budget_id = str(metadata.get("budget_id") or "")
    session_id = str(getattr(task, "session_id", "") or "")
    turn_id = str(metadata.get("harness_turn_id") or "")
    work_item_id = str(getattr(task, "linked_work_item_id", "") or metadata.get("work_item_id") or "")
    criteria = contract.get("acceptance_criteria", []) if type(contract) is dict else []
    from .role_delivery_check import role_tool_receipt

    role_receipt = role_tool_receipt(manifest if type(manifest) is dict else {}, content)
    refs = []
    for delivery in deliveries:
        try:
            ref = await write_fixed_artifact(
                store,
                entry=ENTRY_WORKFLOW,
                project_id=identity.project_id,
                task_id=task.id,
                role_id=str(instance.get("instance_id") or ""),
                name=delivery["name"],
                content=delivery["content"],
                format_=delivery["format"],
                budget_id=budget_id,
                harness_session_id=session_id,
                harness_turn_id=turn_id,
                order_id=identity.order_id,
                workflow_id=identity.workflow_id,
                node_id=identity.node_id,
                work_item_id=work_item_id,
                extra_details={
                    "instance_id": instance.get("instance_id", ""),
                    "instance_revision": instance.get("instance_revision", ""),
                    "package_fingerprint": instance.get("package_fingerprint", ""),
                    "output_schema": manifest.get("output_schema", {}) if type(manifest) is dict else {},
                    "artifact_contract": contract if type(contract) is dict else {},
                },
            )
        except asyncio.CancelledError:
            block("outcome_unknown", "employee_delivery_interrupted")
            raise
        if ref is None:
            block("outcome_unknown", "employee_delivery_write_unconfirmed")
            return
        refs.append(ref)
        metadata["employee_output_receipts"] = list(refs)
    handoff_id, recorded = ("", False)
    try:
        handoff_id, recorded = await write_fixed_handoff(
            store,
            entry=ENTRY_WORKFLOW,
            project_id=identity.project_id,
            task_id=task.id,
            session_id=session_id,
            from_role=str(instance.get("instance_id") or ""),
            summary=content,
            artifacts=refs,
            budget_id=budget_id,
            harness_turn_id=turn_id,
            limits=dict(getattr(turn, "limits", {}) or {}),
            budget={},
            output_schema_checked=True,
            attempt=int(getattr(identity, "attempt", 1) or 1),
            order_id=identity.order_id,
            workflow_id=identity.workflow_id,
            workflow_version=str(getattr(identity, "workflow_version", "") or ""),
            node_id=identity.node_id,
            work_item_id=work_item_id,
            contract=contract if type(contract) is dict else None,
            role_receipt=role_receipt,
            extra_payload={
                "acceptance_criteria": list(criteria) if type(criteria) is list else [],
                "instance_id": instance.get("instance_id", ""),
                "package_id": str(manifest.get("package_id") or "") if type(manifest) is dict else "",
                "package_fingerprint": instance.get("package_fingerprint", ""),
            },
        )
    except asyncio.CancelledError:
        block("outcome_unknown", "employee_delivery_interrupted")
        raise
    if not recorded:
        block("outcome_unknown", "employee_handoff_write_unconfirmed")
        return
    metadata["employee_delivery_state"] = "recorded"
    metadata.pop("employee_delivery_error", None)
    metadata["employee_output_confirmed"] = True
    # Bytes stored are not acceptance. DONE stays pending until the owner accepts.
    metadata["employee_acceptance_state"] = "pending"
    metadata["employee_delivered"] = False
    metadata["governed_handoff_id"] = handoff_id
    task.metadata = metadata
    result.artifacts = {**dict(getattr(result, "artifacts", {}) or {}),
        "employee_outputs": refs, "employee_output_confirmed": True}
