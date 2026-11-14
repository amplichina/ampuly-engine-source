"""Controlled receipt for a customer-service draft. Never a send authorization.

The host may record this only when the draft still requires review, produced
no effects, and did not claim delivery. A passing receipt is not acceptance.
"""
from __future__ import annotations

from typing import Any


def draft_receipt(output: Any) -> dict[str, Any]:
    """Check one draft object. The receipt itself stays not-sent."""
    base = {
        "tool": "customer_service_draft_check",
        "passed": False,
        "delivery_status": "not_sent",
        "human_approval_granted": False,
        "review_required": True,
        "code": "",
    }
    if type(output) is not dict:
        base["code"] = "role_tool_failed"
        return base
    claimed = (
        output.get("sending_authorized") is True
        or output.get("human_approval_granted") is True
        or output.get("delivery_status") in {"delivered", "sent", "sent_waiting_receipt"}
        or output.get("review_required") is False
    )
    effects = output.get("effects_produced")
    passed = (
        not claimed
        and output.get("review_required") is True
        and effects == []
        and output.get("sending_authorized") is not True
    )
    base["passed"] = passed
    if not passed:
        base["code"] = "role_claimed_delivery" if claimed or effects not in ([],) else "role_tool_failed"
        if effects not in ([],) and not claimed:
            base["code"] = "role_tool_failed"
    return base
