"""Private, opt-in observation conversion; no producer or authority claim.

No caller is wired by this module. None preserves an absent observation only;
a future caller requiring a creation fact must reject absent source itself.
The carrier can be constructed by a caller and is not an unforgeable token.
"""

from __future__ import annotations

from typing import Any

from .order_launch_claim_source import (
    OriginalOrderLaunchClaim, validate_order_launch_claim,
)
from ...database.order_task_origin import (
    OrderTaskOrigin, OrderTaskOriginError, validate_order_task_origin,
)


def order_task_origin_from_claim(
    source: OriginalOrderLaunchClaim | None, *, project_id: str,
    order_id: str, outbox_id: str, workflow_id: str, workflow_version: str,
    fields: dict[str, Any], model_plan: dict[str, Any] | None,
) -> OrderTaskOrigin | None:
    """Validate original bytes, then copy the original eight observation values.

    Empty original version remains empty. This neither reads an original claim
    nor validates its event chain, grants permission, writes a fact, or derives
    a Task/Run identity. No raw bytes enter the returned observation.

    Ordinary failures become the original fixed invalid_source error after
    this function's handler. BaseException propagates unchanged. This does not
    clean an active caller exception or make traceback locals public-safe.
    """
    if source is None:
        return None

    failed = False
    result = None
    try:
        validate_order_launch_claim(
            source, project_id=project_id, order_id=order_id,
            outbox_id=outbox_id, workflow_id=workflow_id,
            workflow_version=workflow_version, fields=fields,
            model_plan=model_plan,
        )
        result = OrderTaskOrigin(
            project_id=source.project_id,
            order_id=source.order_id,
            outbox_id=source.outbox_id,
            workflow_id=source.workflow_id,
            workflow_version=source.workflow_version,
            launch_payload_sha256=source.launch_payload_sha256,
            claim_event_id=source.claim_event_id,
            event_chain_head_sha256=source.event_chain_head_sha256,
        )
        validate_order_task_origin(result, project_id=project_id)
    except Exception:
        failed = True
    if failed:
        raise OrderTaskOriginError("invalid_source") from None
    return result
