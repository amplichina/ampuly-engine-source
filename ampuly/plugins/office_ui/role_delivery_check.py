"""Tool checks for the document and customer-service roles.

The document path calls the shipped DOCX generator and structure checker.
The customer-service path calls the draft receipt. Neither path sends,
and neither path records human acceptance.
"""
from __future__ import annotations

import json
import re
from typing import Any

from ampuly.core.delivery_acceptance import classify_role

_HASH = re.compile(r"^[a-fA-F0-9]{64}$")


def role_tool_receipt(manifest: Any, content: str) -> dict[str, Any] | None:
    """None for a generic role. Document and customer-service always return a receipt."""
    if type(manifest) is not dict or type(content) is not str:
        return None
    contract = manifest.get("artifact_contract")
    role = classify_role(
        str(manifest.get("package_id") or ""),
        contract if type(contract) is dict else None,
    )
    if role == "document":
        return document_tool_receipt(content)
    if role == "customer_service":
        return customer_service_tool_receipt(content)
    return None


def document_tool_receipt(content: str) -> dict[str, Any]:
    """Run generate_docx and check_docx when the body is a bounded spec.

    A model claim that the document was accepted or sent fails the receipt.
    Bytes of the generated file are not copied into the receipt.
    """
    receipt: dict[str, Any] = {
        "tool": "check_docx",
        "passed": False,
        "delivery_status": "not_sent",
        "human_approval_granted": False,
        "code": "role_tool_failed",
    }
    try:
        value = json.loads(content)
    except (ValueError, TypeError):
        return receipt
    if type(value) is not dict:
        return receipt
    if (
        value.get("human_approval_granted") is True
        or value.get("human_acceptance_status") == "accepted"
        or value.get("delivery_status") in {"delivered", "sent", "sent_waiting_receipt"}
    ):
        receipt["code"] = "role_claimed_delivery"
        return receipt
    spec = value.get("spec") if type(value.get("spec")) is dict else value
    approved = value.get("approved_content")
    if type(spec) is not dict or type(approved) is not dict:
        return receipt
    if "title" not in spec or "sections" not in spec:
        return receipt
    from .doc_tools.generate_docx import generate_docx

    try:
        generated = generate_docx(spec, approved_content=approved)
    except Exception:
        receipt["code"] = "role_tool_failed"
        return receipt
    report = generated.get("check_report") if type(generated) is dict else None
    passed = (
        type(generated) is dict
        and generated.get("status") == "success"
        and type(report) is dict
        and report.get("status") == "success"
        and generated.get("delivery_status", "not_sent") != "sent"
    )
    digest = str((generated or {}).get("content_hash") or "")
    if not _HASH.fullmatch(digest):
        passed = False
    receipt["passed"] = passed
    receipt["code"] = "" if passed else "role_tool_failed"
    if passed:
        receipt["content_hash"] = digest.lower()
    return receipt


def customer_service_tool_receipt(content: str) -> dict[str, Any]:
    """The customer-service checker owns the controlled draft receipt."""
    from ampuly.components.customer_service.core.controlled_receipt import draft_receipt

    try:
        value = json.loads(content)
    except (ValueError, TypeError):
        value = None
    return draft_receipt(value)
