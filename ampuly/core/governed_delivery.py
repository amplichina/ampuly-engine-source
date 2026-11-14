"""One fixed-delivery caliber for the three execution entries.

Direct tasks, independent employees, and workflow employee nodes write the
same ArtifactRecord and HandoffRecord fields. A direct task or an independent
employee that carries an order, flow, or node is refused: those identities
belong only to a real workflow entry. Storage failures return unconfirmed;
they do not raise a second model call.
"""
from __future__ import annotations

import hashlib
from typing import Any

from ampuly.core.delivery_acceptance import consume_fixed_contract
from ampuly.core.models import ArtifactRecord, HandoffRecord

ENTRY_DIRECT = "direct_task"
ENTRY_EMPLOYEE = "employee_delegate"
ENTRY_WORKFLOW = "workflow_node"
ENTRIES = frozenset({ENTRY_DIRECT, ENTRY_EMPLOYEE, ENTRY_WORKFLOW})

COMMON_ARTIFACT_DETAILS = (
    "entry", "content", "content_sha256", "bytes", "revision", "media_type",
    "budget_id", "harness_session_id", "harness_turn_id",
)
COMMON_HANDOFF_PAYLOAD = (
    "entry", "task_id", "status", "budget_id", "limits", "budget",
    "harness_session_id", "harness_turn_id", "output_schema_checked",
    "artifacts", "attempt",
)
_FLOW_KEYS = ("order_id", "workflow_id", "node_id", "work_item_id")


class GovernedDeliveryRefused(ValueError):
    """The delivery does not match the shared entry contract."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def media_type_for(format_: str) -> str:
    kind = str(format_ or "").lower()
    if kind in {"json", "application/json"}:
        return "application/json"
    if kind in {"markdown", "md", "text/markdown"}:
        return "text/markdown"
    return "text/plain"


def assert_entry_identity(
    entry: str,
    *,
    order_id: str = "",
    workflow_id: str = "",
    node_id: str = "",
    work_item_id: str = "",
) -> dict[str, str]:
    """Freeze the business identity an entry is allowed to carry."""
    if entry not in ENTRIES:
        raise GovernedDeliveryRefused("governed_delivery_entry_invalid")
    flow = {
        "order_id": str(order_id or ""),
        "workflow_id": str(workflow_id or ""),
        "node_id": str(node_id or ""),
        "work_item_id": str(work_item_id or ""),
    }
    if entry == ENTRY_WORKFLOW:
        if not flow["workflow_id"].strip() or not flow["node_id"].strip():
            raise GovernedDeliveryRefused("governed_delivery_workflow_identity_missing")
        return flow
    if any(value.strip() for value in flow.values()):
        raise GovernedDeliveryRefused("governed_delivery_fake_flow")
    return {key: "" for key in _FLOW_KEYS}


def _text(value: Any, code: str) -> str:
    if type(value) is not str:
        raise GovernedDeliveryRefused(code)
    return value


def build_fixed_artifact(
    *,
    entry: str,
    project_id: str,
    task_id: str,
    role_id: str,
    name: str,
    content: str,
    format_: str = "text",
    budget_id: str = "",
    harness_session_id: str = "",
    harness_turn_id: str = "",
    revision: int = 1,
    order_id: str = "",
    workflow_id: str = "",
    node_id: str = "",
    work_item_id: str = "",
    extra_details: dict[str, Any] | None = None,
) -> tuple[ArtifactRecord, dict[str, Any]]:
    """Build one content-addressed artifact. The hash is computed here."""
    assert_entry_identity(
        entry, order_id=order_id, workflow_id=workflow_id,
        node_id=node_id, work_item_id=work_item_id,
    )
    project = _text(project_id, "governed_delivery_project_invalid").strip()
    task = _text(task_id, "governed_delivery_task_invalid").strip()
    artifact_name = _text(name, "governed_delivery_name_invalid").strip()
    body = _text(content, "governed_delivery_content_invalid")
    if not project or not task or not artifact_name:
        raise GovernedDeliveryRefused("governed_delivery_identity_missing")
    if type(revision) is not int or revision < 1:
        raise GovernedDeliveryRefused("governed_delivery_revision_invalid")
    raw = body.encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    kind = str(format_ or "").lower()
    details: dict[str, Any] = {}
    if type(extra_details) is dict:
        details.update(extra_details)
    details.update({
        "entry": entry,
        "content": body,
        "content_sha256": digest,
        "bytes": len(raw),
        "revision": revision,
        "media_type": media_type_for(kind),
        "budget_id": str(budget_id or ""),
        "harness_session_id": str(harness_session_id or ""),
        "harness_turn_id": str(harness_turn_id or ""),
    })
    record = ArtifactRecord(
        project_id=project,
        task_id=task,
        role_id=str(role_id or ""),
        name=artifact_name,
        artifact_type="json" if kind in {"json", "application/json"} else "text",
        location="",
        details=details,
    )
    ref = {
        "artifact_id": record.artifact_id,
        "name": record.name,
        "sha256": digest,
        "bytes": len(raw),
        "revision": revision,
    }
    return record, ref


def build_fixed_handoff(
    *,
    entry: str,
    project_id: str,
    task_id: str,
    session_id: str,
    from_role: str,
    summary: str,
    artifacts: list[dict[str, Any]],
    budget_id: str = "",
    harness_turn_id: str = "",
    limits: dict[str, Any] | None = None,
    budget: dict[str, Any] | None = None,
    output_schema_checked: bool = False,
    attempt: int = 1,
    to_role: str = "owner",
    order_id: str = "",
    workflow_id: str = "",
    workflow_version: str = "",
    node_id: str = "",
    work_item_id: str = "",
    extra_payload: dict[str, Any] | None = None,
    contract: dict[str, Any] | None = None,
    role_receipt: dict[str, Any] | None = None,
) -> HandoffRecord:
    """Build the handoff that cites those artifact refs and nothing fresher."""
    flow = assert_entry_identity(
        entry, order_id=order_id, workflow_id=workflow_id,
        node_id=node_id, work_item_id=work_item_id,
    )
    if type(artifacts) is not list:
        raise GovernedDeliveryRefused("governed_delivery_artifacts_invalid")
    if type(attempt) is not int or attempt < 1:
        raise GovernedDeliveryRefused("governed_delivery_attempt_invalid")
    if type(output_schema_checked) is not bool:
        raise GovernedDeliveryRefused("governed_delivery_check_invalid")
    payload: dict[str, Any] = {}
    if type(extra_payload) is dict:
        payload.update(extra_payload)
    payload.update({
        "entry": entry,
        "task_id": str(task_id or ""),
        "status": "done",
        "budget_id": str(budget_id or ""),
        "limits": dict(limits or {}),
        "budget": dict(budget or {}),
        "harness_session_id": str(session_id or ""),
        "harness_turn_id": str(harness_turn_id or ""),
        "output_schema_checked": output_schema_checked,
        "artifacts": list(artifacts),
        "attempt": attempt,
    })
    if entry == ENTRY_WORKFLOW:
        payload.update(flow)
        payload["workflow_version"] = str(workflow_version or "")
    else:
        for key in _FLOW_KEYS:
            payload.pop(key, None)
    # The origin consumes the contract here. A finished run stays pending
    # until a later acceptance decision; callers cannot stamp delivered.
    package_id = str(payload.get("package_id") or "")
    chosen_contract = contract if type(contract) is dict else (
        payload.get("artifact_contract") if type(payload.get("artifact_contract")) is dict else None
    )
    names = [item.get("name") for item in artifacts if type(item) is dict]
    payload.pop("role_receipt", None)
    payload["origin_check"] = consume_fixed_contract(
        contract=chosen_contract,
        artifact_names=names,
        role_receipt=role_receipt,
        package_id=package_id,
    )
    payload["acceptance_state"] = "pending"
    payload["delivered"] = False
    return HandoffRecord(
        project_id=str(project_id or ""),
        session_id=str(session_id or ""),
        task_id=str(task_id or ""),
        from_role=str(from_role or ""),
        to_role=str(to_role or "owner"),
        summary=str(summary or "")[:2000],
        payload=payload,
        requires_ack=True,
        status="sent",
    )


async def write_fixed_artifact(store: Any, **kwargs: Any) -> dict[str, Any] | None:
    """Persist one artifact. A missing writer or a failed write is unconfirmed."""
    save = getattr(store, "record_artifact", None)
    if not callable(save):
        return None
    record, ref = build_fixed_artifact(**kwargs)
    try:
        await save(record)
    except Exception:
        return None
    return ref


async def write_fixed_handoff(store: Any, **kwargs: Any) -> tuple[str, bool]:
    """Persist the handoff. A failed write is reported and not retried here."""
    save = getattr(store, "save_handoff_record", None)
    if not callable(save):
        return "", False
    record = build_fixed_handoff(**kwargs)
    try:
        await save(record)
    except Exception:
        return "", False
    return record.handoff_id, True
