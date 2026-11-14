"""Restore an observed claim's original pins; never authorize its execution.

Carrier types and local digests are forgeable, not proof of observation. The
caller must obtain the source from the authenticated reader and separately
verify historical/current authorization, runtime identity and budget.
"""

from __future__ import annotations

from dataclasses import dataclass

from .employee_order_claim_pins import OriginalEmployeeOrderClaimPins
from .employee_order_launch_anchor import OrderLaunchAnchorError, _identity, _SHA256
from .ai_employee_run_contract import FrozenEmployeePins, restore_employee_pins


@dataclass(frozen=True, repr=False)
class RestoredOriginalEmployeeOrderClaimPins:
    """Keep source association and original snapshot together, not a grant.

    Never log either field or expose them as public DTOs. The original snapshot
    type's repr may contain private raw bytes even though this wrapper does not.
    """

    source: OriginalEmployeeOrderClaimPins
    snapshot: FrozenEmployeePins


def _restore_claim(
    source: OriginalEmployeeOrderClaimPins, *, project_id: str, order_id: str, outbox_id: str,
) -> RestoredOriginalEmployeeOrderClaimPins:
    if type(source) is not OriginalEmployeeOrderClaimPins:
        raise OrderLaunchAnchorError("invalid_source")
    for field, expected in (("project_id", project_id), ("order_id", order_id), ("outbox_id", outbox_id)):
        value = getattr(source, field)
        if type(value) is not str or value != expected:
            raise OrderLaunchAnchorError("invalid_source")
    for field in ("workflow_id", "workflow_version", "claim_event_id", "pins_event_id"):
        _identity(getattr(source, field))
    if source.claim_event_id == source.pins_event_id:
        raise OrderLaunchAnchorError("invalid_source")
    for field in ("launch_payload_sha256", "event_chain_head_sha256", "pins_fingerprint"):
        value = getattr(source, field)
        if type(value) is not str or _SHA256.fullmatch(value) is None:
            raise OrderLaunchAnchorError("invalid_source")
    if type(source.pins_bytes) is not bytes:
        raise OrderLaunchAnchorError("invalid_source")

    # Preserve the retained expectation. Never hash the input to invent it and
    # never freeze/re-resolve a current instance in place of these original bytes.
    snapshot = restore_employee_pins(
        source.pins_bytes, project_id=source.project_id, workflow_id=source.workflow_id,
        expected_fingerprint=source.pins_fingerprint,
    )
    if (type(snapshot) is not FrozenEmployeePins or snapshot.project_id != source.project_id
            or snapshot.workflow_id != source.workflow_id or snapshot.fingerprint != source.pins_fingerprint
            or snapshot.to_bytes() != source.pins_bytes):
        raise OrderLaunchAnchorError("invalid_source")
    rows = snapshot.to_dict()
    if type(rows) is not dict or not rows:
        raise OrderLaunchAnchorError("invalid_source")
    return RestoredOriginalEmployeeOrderClaimPins(source=source, snapshot=snapshot)


def restore_original_employee_order_claim_pins(
    source: OriginalEmployeeOrderClaimPins, *, project_id: str, order_id: str, outbox_id: str,
) -> RestoredOriginalEmployeeOrderClaimPins:
    """Validate original pin structure/integrity, not source authority or readiness."""
    project_id = _identity(project_id, scope=True)
    order_id = _identity(order_id, scope=True)
    outbox_id = _identity(outbox_id, scope=True)
    failed = False
    result = None
    try:
        result = _restore_claim(source, project_id=project_id, order_id=order_id, outbox_id=outbox_id)
    except Exception:
        # Original codec errors can contain private path/details/context. Do not
        # preserve that exception chain. Cancellation/BaseException still escape.
        failed = True
    if failed:
        raise OrderLaunchAnchorError("invalid_source") from None
    assert type(result) is RestoredOriginalEmployeeOrderClaimPins
    return result
