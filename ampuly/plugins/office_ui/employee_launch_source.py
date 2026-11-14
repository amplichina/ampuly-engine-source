"""Original employee claim transport, distinct from the legacy launch source.

Produced only inside BusinessStore's original query-only read transaction.
Neither this carrier nor a hash is authorization. The WS host re-observes the
same pending claim immediately before its normal insert-only Task creation.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .ai_employee_contract import canonical_json
from .employee_order_claim_restore import RestoredOriginalEmployeeOrderClaimPins
from .employee_order_launch_anchor import OrderLaunchAnchorError, _MAX_JSON_BYTES
from ...database.order_task_origin import OrderTaskOrigin, validate_order_task_origin


def validate_employee_payload(payload, *, project_id: str, order_id: str, workflow_id: str):
    from .ai_employee_run_contract import restore_employee_pins
    from .employee_order_launch_anchor import _identity
    if (type(payload) is not dict or type(payload.get("schema_version")) is not int
            or payload["schema_version"] != 1 or payload.get("command") != "workflow.launch"
            or payload.get("project_id") != project_id or payload.get("order_id") != order_id
            or payload.get("workflow_id") != workflow_id
            or type(payload.get("fields")) is not dict or type(payload.get("model_plan")) is not dict):
        raise OrderLaunchAnchorError("invalid_source")
    version = _identity(payload.get("workflow_version"))
    snapshot = restore_employee_pins(canonical_json(payload.get("employee_pins")),
        project_id=project_id, workflow_id=workflow_id,
        expected_fingerprint=payload.get("employee_pins_fingerprint"))
    if not snapshot.to_dict():
        raise OrderLaunchAnchorError("invalid_source")
    return version, payload["fields"], payload["model_plan"], snapshot


@dataclass(frozen=True, repr=False)
class EmployeeOrderLaunchSource:
    restored: RestoredOriginalEmployeeOrderClaimPins
    launch_payload_bytes: bytes

    @property
    def payload(self) -> dict:
        return json.loads(self.launch_payload_bytes)


def capture_employee_launch_source(restored, payload_raw: str) -> EmployeeOrderLaunchSource:
    if type(restored) is not RestoredOriginalEmployeeOrderClaimPins or type(payload_raw) is not str:
        raise OrderLaunchAnchorError("invalid_source")
    raw = payload_raw.encode("utf-8")
    if not raw or len(raw) > _MAX_JSON_BYTES or hashlib.sha256(raw).hexdigest() != restored.source.launch_payload_sha256:
        raise OrderLaunchAnchorError("invalid_source")
    result = EmployeeOrderLaunchSource(restored, raw)
    validate_employee_launch_source(result, project_id=restored.source.project_id,
        order_id=restored.source.order_id, outbox_id=restored.source.outbox_id,
        workflow_id=restored.source.workflow_id, workflow_version=restored.source.workflow_version)
    return result


def validate_employee_launch_source(value, *, project_id: str, order_id: str, outbox_id: str,
                                   workflow_id: str, workflow_version: str,
                                   fields: dict | None = None, model_plan: dict | None = None) -> None:
    if type(value) is not EmployeeOrderLaunchSource or type(value.restored) is not RestoredOriginalEmployeeOrderClaimPins:
        raise OrderLaunchAnchorError("invalid_source")
    source, pins = value.restored.source, value.restored.snapshot
    if ((source.project_id, source.order_id, source.outbox_id, source.workflow_id, source.workflow_version)
            != (project_id, order_id, outbox_id, workflow_id, workflow_version)
            or type(value.launch_payload_bytes) is not bytes or not 0 < len(value.launch_payload_bytes) <= _MAX_JSON_BYTES
            or hashlib.sha256(value.launch_payload_bytes).hexdigest() != source.launch_payload_sha256):
        raise OrderLaunchAnchorError("invalid_source")
    payload = value.payload
    validate_employee_payload(payload, project_id=project_id, order_id=order_id, workflow_id=workflow_id)
    if (type(payload) is not dict or payload.get("schema_version") != 1 or payload.get("command") != "workflow.launch"
            or any(payload.get(key) != expected for key, expected in (("project_id", project_id), ("order_id", order_id),
                ("workflow_id", workflow_id), ("workflow_version", workflow_version)))
            or canonical_json(payload.get("employee_pins")) != pins.to_bytes()
            or payload.get("employee_pins_fingerprint") != pins.fingerprint
            or type(payload.get("fields")) is not dict or type(payload.get("model_plan")) is not dict):
        raise OrderLaunchAnchorError("invalid_source")
    if fields is not None and canonical_json(fields) != canonical_json(payload["fields"]):
        raise OrderLaunchAnchorError("invalid_source")
    if model_plan is not None and canonical_json(model_plan) != canonical_json(payload["model_plan"]):
        raise OrderLaunchAnchorError("invalid_source")


def employee_task_origin(source: EmployeeOrderLaunchSource, **scope) -> OrderTaskOrigin:
    validate_employee_launch_source(source, **scope)
    original = source.restored.source
    value = OrderTaskOrigin(**{name: getattr(original, name) for name in (
        "project_id", "order_id", "outbox_id", "workflow_id", "workflow_version",
        "launch_payload_sha256", "claim_event_id", "event_chain_head_sha256")})
    validate_order_task_origin(value, project_id=scope["project_id"])
    return value
