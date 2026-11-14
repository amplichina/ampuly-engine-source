"""Private pending legacy-claim transport, not authorization or creation proof.

Only the host's existing query-only transaction supplies original source bytes.
The carrier is an observation of that snapshot, not an unforgeable capability,
an independently retained runtime identity, or a durable Task/Run fact. Never
publish its raw bytes, fields, or a serialization in logs, receipts or metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import sqlite3
from typing import Any

from .ai_employee_contract import canonical_json
from .employee_order_launch_anchor import (
    OrderLaunchAnchorError, _ReadBudget, _identity, _rows, _json_dict,
    _MAX_JSON_BYTES, _SHA256,
)
from .employee_order_claim_pins import _read_claim_events


@dataclass(frozen=True, repr=False)
class OriginalOrderLaunchClaim:
    """Original private pending bytes; neither permission nor a runtime anchor."""

    project_id: str
    order_id: str
    outbox_id: str
    workflow_id: str
    workflow_version: str
    launch_payload_bytes: bytes
    launch_payload_sha256: str
    claim_event_id: str
    event_chain_head_sha256: str


def _canonical(value: Any) -> bytes:
    failed = False
    result = None
    try:
        result = canonical_json(value)
    except Exception:
        failed = True
    if failed or type(result) is not bytes:
        raise OrderLaunchAnchorError("invalid_source") from None
    if len(result) > _MAX_JSON_BYTES:
        raise OrderLaunchAnchorError("read_limit")
    return result


def _legacy_payload(
    payload: dict[str, Any], *, project_id: str, order_id: str,
    workflow_id: str,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    if type(payload) is not dict:
        raise OrderLaunchAnchorError("invalid_source")
    # The original bounded codec rejects nested custom objects, cycles,
    # non-finite numbers and non-string keys without coercing them.
    _canonical(payload)
    if (type(payload.get("schema_version")) is not int or payload["schema_version"] != 1
            or type(payload.get("command")) is not str or payload["command"] != "workflow.launch"
            or _identity(payload.get("project_id")) != project_id
            or _identity(payload.get("order_id")) != order_id
            or _identity(payload.get("workflow_id")) != workflow_id):
        raise OrderLaunchAnchorError("invalid_source")
    version = _identity(payload.get("workflow_version", ""), empty=True)
    pins = payload.get("employee_pins", {})
    fingerprint = payload.get("employee_pins_fingerprint", "")
    if type(pins) is not dict or pins or type(fingerprint) is not str or fingerprint != "":
        raise OrderLaunchAnchorError("invalid_source")
    fields = payload.get("fields", {})
    model_plan = payload.get("model_plan", {})
    if type(fields) is not dict or type(model_plan) is not dict:
        raise OrderLaunchAnchorError("invalid_source")
    return version, fields, model_plan


def _capture_legacy_order_launch_claim(
    conn: sqlite3.Connection, *, budget: _ReadBudget,
    project_id: str, order_id: str, outbox_id: str, workflow_id: str,
    payload_raw: str, payload: dict[str, Any],
) -> OriginalOrderLaunchClaim:
    project_id = _identity(project_id, scope=True)
    order_id = _identity(order_id, scope=True)
    outbox_id = _identity(outbox_id, scope=True)
    workflow_id = _identity(workflow_id, scope=True)
    if type(budget) is not _ReadBudget:
        raise OrderLaunchAnchorError("invalid_source")
    if not isinstance(conn, sqlite3.Connection) or not conn.in_transaction:
        raise OrderLaunchAnchorError("transaction_required")
    if (conn.row_factory is not None and conn.row_factory is not sqlite3.Row) or conn.text_factory is not str:
        raise OrderLaunchAnchorError("invalid_source")
    query_only = _rows(conn, "PRAGMA query_only", (), 2)
    if len(query_only) != 1 or len(query_only[0]) != 1 or query_only[0][0] != 1:
        raise OrderLaunchAnchorError("transaction_required")
    if type(payload_raw) is not str:
        raise OrderLaunchAnchorError("invalid_source")
    if len(payload_raw) > _MAX_JSON_BYTES:
        raise OrderLaunchAnchorError("read_limit")
    payload_bytes = None
    try:
        payload_bytes = payload_raw.encode("utf-8")
    except UnicodeError:
        pass
    if payload_bytes is None:
        raise OrderLaunchAnchorError("invalid_source")
    if not payload_bytes or len(payload_bytes) > _MAX_JSON_BYTES:
        raise OrderLaunchAnchorError("read_limit")
    # Original row bytes have already been charged by the owner. Re-decode
    # conservatively charges JSON nodes to the SAME budget, never a reset.
    original = _json_dict(payload_raw, budget)
    if _canonical(original) != _canonical(payload):
        raise OrderLaunchAnchorError("invalid_source")
    version, _, _ = _legacy_payload(
        original, project_id=project_id, order_id=order_id, workflow_id=workflow_id,
    )
    payload_sha = hashlib.sha256(payload_bytes).hexdigest()
    claim_id, pins_id, head_sha = _read_claim_events(
        conn, order_id=order_id, outbox_id=outbox_id, payload_sha=payload_sha,
        budget=budget, pins_source=None, project_id=project_id,
        workflow_id=workflow_id, workflow_version=version,
    )
    if pins_id != "":
        raise OrderLaunchAnchorError("invalid_source")
    return OriginalOrderLaunchClaim(
        project_id=project_id, order_id=order_id, outbox_id=outbox_id,
        workflow_id=workflow_id, workflow_version=version,
        launch_payload_bytes=payload_bytes, launch_payload_sha256=payload_sha,
        claim_event_id=claim_id, event_chain_head_sha256=head_sha,
    )


def capture_legacy_order_launch_claim(
    conn: sqlite3.Connection, *, budget: _ReadBudget,
    project_id: str, order_id: str, outbox_id: str, workflow_id: str,
    payload_raw: str, payload: dict[str, Any],
) -> OriginalOrderLaunchClaim:
    """Capture already scoped original rows in the host's SAME read transaction.

    This is an internal producer seam, not validation of caller-supplied truth.
    The owner must have verified launching/pending rows and their exact scope
    before calling. Missing original anchors do not permit a legacy fallback.
    """
    failure = None
    result = None
    try:
        result = _capture_legacy_order_launch_claim(
            conn, budget=budget, project_id=project_id, order_id=order_id,
            outbox_id=outbox_id, workflow_id=workflow_id,
            payload_raw=payload_raw, payload=payload,
        )
    except OrderLaunchAnchorError as exc:
        failure = exc.code if type(exc) is OrderLaunchAnchorError else "unavailable"
    except Exception:
        failure = "unavailable"
    if failure is not None:
        raise OrderLaunchAnchorError(failure) from None
    return result


def _validate_order_launch_claim(
    source: OriginalOrderLaunchClaim, *, project_id: str, order_id: str,
    outbox_id: str, workflow_id: str, workflow_version: str,
    fields: dict[str, Any], model_plan: dict[str, Any] | None,
) -> None:
    project_id = _identity(project_id, scope=True)
    order_id = _identity(order_id, scope=True)
    outbox_id = _identity(outbox_id, scope=True)
    workflow_id = _identity(workflow_id, scope=True)
    workflow_version = _identity(workflow_version, empty=True, scope=True)
    if type(source) is not OriginalOrderLaunchClaim:
        raise OrderLaunchAnchorError("invalid_source")
    if (_identity(source.project_id) != project_id or _identity(source.order_id) != order_id
            or _identity(source.outbox_id) != outbox_id or _identity(source.workflow_id) != workflow_id):
        raise OrderLaunchAnchorError("invalid_source")
    original_version = _identity(source.workflow_version, empty=True)
    _identity(source.claim_event_id)
    for digest in (source.launch_payload_sha256, source.event_chain_head_sha256):
        if type(digest) is not str or _SHA256.fullmatch(digest) is None:
            raise OrderLaunchAnchorError("invalid_source")
    if type(source.launch_payload_bytes) is not bytes:
        raise OrderLaunchAnchorError("invalid_source")
    if not source.launch_payload_bytes or len(source.launch_payload_bytes) > _MAX_JSON_BYTES:
        raise OrderLaunchAnchorError("read_limit")
    if hashlib.sha256(source.launch_payload_bytes).hexdigest() != source.launch_payload_sha256:
        raise OrderLaunchAnchorError("invalid_source")
    raw = None
    try:
        raw = source.launch_payload_bytes.decode("utf-8")
    except UnicodeError:
        pass
    if raw is None:
        raise OrderLaunchAnchorError("invalid_source")
    payload = _json_dict(raw, _ReadBudget())
    version, original_fields, original_plan = _legacy_payload(
        payload, project_id=project_id, order_id=order_id, workflow_id=workflow_id,
    )
    if version != original_version:
        raise OrderLaunchAnchorError("invalid_source")
    if original_version and workflow_version and original_version != workflow_version:
        raise OrderLaunchAnchorError("invalid_source")
    if type(fields) is not dict or (model_plan is not None and type(model_plan) is not dict):
        raise OrderLaunchAnchorError("invalid_source")
    if _canonical(fields) != _canonical(original_fields):
        raise OrderLaunchAnchorError("invalid_source")
    if model_plan is not None:
        _canonical(model_plan)
    if original_plan and (model_plan is None or _canonical(original_plan) != _canonical(model_plan)):
        raise OrderLaunchAnchorError("invalid_source")


def validate_order_launch_claim(
    source: OriginalOrderLaunchClaim, *, project_id: str, order_id: str,
    outbox_id: str, workflow_id: str, workflow_version: str,
    fields: dict[str, Any], model_plan: dict[str, Any] | None,
) -> None:
    """Recheck private bytes against this launch before effects; success is None.

    An empty original version stays empty. An empty original model plan allows
    legacy dynamic fallback, without claiming frozen model routing. No database,
    current resolver, permission, Task/Run identity or effect is consulted here.
    """
    failure = None
    try:
        _validate_order_launch_claim(
            source, project_id=project_id, order_id=order_id, outbox_id=outbox_id,
            workflow_id=workflow_id, workflow_version=workflow_version,
            fields=fields, model_plan=model_plan,
        )
    except OrderLaunchAnchorError as exc:
        failure = exc.code if type(exc) is OrderLaunchAnchorError else "invalid_source"
    except Exception:
        failure = "invalid_source"
    if failure is not None:
        raise OrderLaunchAnchorError(failure) from None
