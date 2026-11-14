"""Read a bounded original console message after the host's strong fact checks.

Only immutable outbox bytes matching the already-verified Task/Run origin are
read. Current order fields, step descriptions and client Context are not used.
This is an input observation, not a launch/identity authorization mechanism.
"""
from __future__ import annotations
import hashlib

from .ai_employee_contract import canonical_json
from .employee_launch_source import validate_employee_payload
from .employee_order_launch_anchor import _ReadBudget, _one_record, _json_dict, _MAX_JSON_BYTES
from ampuly.components.customer_service.employee_native import ConsoleDraftInput


def read_console_draft_input(business, *, identity, launch, pins_fingerprint, revision_note=""):
    from ampuly.layer3_agent.employee_runtime import _stop
    headers = launch.get("source_headers", {})
    if (headers.get("project_id") != identity.project_id or headers.get("order_id") != identity.order_id
            or headers.get("workflow_id") != identity.workflow_id
            or headers.get("workflow_version") != identity.workflow_version
            or headers.get("outbox_id") != launch.get("command_id")):
        _stop("employee_scope_invalid")
    with business._connect() as conn:
        conn.execute("PRAGMA query_only = ON").close()
        conn.execute("BEGIN").close()
        budget = _ReadBudget()
        raw, = _one_record(conn, table="business_outbox", selector="outbox_id = ?",
            params=(headers["outbox_id"],), columns=(("payload_json", _MAX_JSON_BYTES),), budget=budget)
        if hashlib.sha256(raw.encode("utf-8")).hexdigest() != headers.get("launch_payload_sha256"):
            _stop("employee_basis_conflict")
        payload = _json_dict(raw, budget)
    version, fields, _, pins = validate_employee_payload(payload,
        project_id=identity.project_id, order_id=identity.order_id, workflow_id=identity.workflow_id)
    reference = {"artifact_id": "order-input-" + identity.order_id,
        "version": headers["launch_payload_sha256"],
        "content_hash": hashlib.sha256(canonical_json(fields)).hexdigest(),
        "project_id": identity.project_id, "kind": "order_input"}
    if (version != identity.workflow_version or pins.fingerprint != pins_fingerprint
            or launch.get("input_refs") != [reference]):
        _stop("employee_basis_conflict")
    text = fields.get("customer_service_message")
    if type(text) is not str or not text.strip() or len(text) > 2000:
        _stop("customer_service_console_message_required")
    return ConsoleDraftInput(project_id=identity.project_id, order_id=identity.order_id,
        order_task_id=identity.order_task_id, step_task_id=identity.step_task_id,
        node_id=identity.node_id, run_id=identity.run_id, attempt=identity.attempt,
        launch_payload_sha256=headers["launch_payload_sha256"], message=text, revision_note=revision_note)
