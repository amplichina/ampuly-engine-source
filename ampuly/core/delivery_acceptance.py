"""DONE is not acceptance.

A finished run may store artifacts and a handoff. That record stays pending
until the origin has consumed the fixed contract and a separate acceptance
decision is stored. This module does not call tools, the store, or a model.
"""
from __future__ import annotations

from typing import Any


def counts_as_delivered(acceptance_state: str, *, check_passed: bool, legacy: bool = False) -> bool:
    """A run status of done never counts. Legacy rows predate the check receipt."""
    if acceptance_state != "accepted":
        return False
    if legacy:
        return True
    return check_passed is True


def classify_role(package_id: str, contract: dict[str, Any] | None) -> str:
    """Document and customer-service are the two roles with a tool receipt."""
    pid = str(package_id or "").strip().lower().replace("_", "-")
    if pid in {"document-delivery", "document"}:
        return "document"
    if pid in {"customer-service", "customer-service-conversation"}:
        return "customer_service"
    if type(contract) is dict:
        rows = contract.get("deliverables")
        if type(rows) is list:
            for row in rows:
                if type(row) is not dict:
                    continue
                label = f"{row.get('format', '')} {row.get('name', '')}".lower()
                if "docx" in label:
                    return "document"
                if "conversation-draft" in label or "客服" in label:
                    return "customer_service"
    return "generic"


def consume_fixed_contract(
    *,
    contract: Any = None,
    artifact_names: list[Any] | None = None,
    role_receipt: Any = None,
    package_id: str = "",
) -> dict[str, Any]:
    """Read deliverables, checks, and acceptance criteria. Never marks accepted.

    The returned receipt is the origin's check. ``delivered`` stays false.
    """
    names = [name for name in (artifact_names or []) if type(name) is str and name.strip()]
    role = classify_role(package_id, contract if type(contract) is dict else None)
    required: list[str] = []
    criteria: list[str] = []
    checks = 0
    code = ""
    if type(contract) is dict:
        rows = contract.get("deliverables")
        if type(rows) is list:
            for row in rows:
                if type(row) is dict and row.get("required") is True:
                    name = row.get("name")
                    if type(name) is str and name.strip():
                        required.append(name)
        raw_criteria = contract.get("acceptance_criteria")
        if "acceptance_criteria" in contract:
            if type(raw_criteria) is not list or not raw_criteria or any(
                type(item) is not str or not item.strip() for item in raw_criteria
            ):
                code = "acceptance_criteria_missing"
            else:
                criteria = [item.strip() for item in raw_criteria]
        for key in ("checks", "checklist"):
            raw_checks = contract.get(key)
            if type(raw_checks) is list:
                checks += len(raw_checks)
    missing = [name for name in required if name not in names]
    check_passed = not missing and not code
    if missing and not code:
        code = "deliverable_missing"
    tool = ""
    tool_passed: bool | None = None
    if role in {"document", "customer_service"}:
        expected = "check_docx" if role == "document" else "customer_service_draft_check"
        if type(role_receipt) is not dict or role_receipt.get("tool") != expected:
            check_passed = False
            tool_passed = False
            code = code or "role_tool_failed"
        else:
            tool = expected
            controlled = (
                role_receipt.get("passed") is True
                and role_receipt.get("delivery_status") == "not_sent"
                and role_receipt.get("human_approval_granted") is False
            )
            tool_passed = controlled
            if role_receipt.get("passed") is not True:
                code = code or str(role_receipt.get("code") or "role_tool_failed")
            elif not controlled:
                code = code or "role_claimed_delivery"
            if not controlled:
                check_passed = False
    return {
        "kind": "origin_check",
        "role": role,
        "check_passed": check_passed is True,
        "acceptance_state": "pending",
        "delivered": False,
        "deliverable_names": list(required),
        "acceptance_criteria_count": len(criteria),
        "checks_consumed": checks,
        "tool": tool,
        "tool_passed": tool_passed,
        "delivery_status": "not_sent",
        "human_approval_granted": False,
        "code": code,
    }
