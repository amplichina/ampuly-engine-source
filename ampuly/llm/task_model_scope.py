"""Frozen task model policy shared by every Harness entry point.

The runtime consumes server-owned task metadata, not the office UI. Building
an order plan remains an application responsibility; parsing and entering its
provider scope belongs here. This extraction does not grant permissions, add
a budget, invent an order, or change the existing missing-plan refusal.
"""
from __future__ import annotations

from typing import Any

SCHEMA = 1


def plan_from_task(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    """Read the supported pinned plan; leave invalid/unknown schemas absent."""
    found = dict(metadata or {}).get("model_plan")
    if not isinstance(found, dict) or not found:
        return None
    try:
        if int(found.get("schema", 0) or 0) != SCHEMA:
            return None
    except (TypeError, ValueError):
        return None
    return dict(found)


def plan_scope_for(task: Any) -> Any:
    """Enter the original provider scope without requiring a UI or workflow.

    Keep the order marker for existing order work: an order with no frozen
    plan must still be refused by the provider, not use live model settings.
    A direct task is not assigned a synthetic order. Its separate scope and
    budget admission remain the caller's responsibility.
    """
    from ampuly.layer4_tools.spend_guard import BUDGET_ORDER_KEY, is_order_work
    from ampuly.llm.provider import model_plan_scope

    metadata = dict(getattr(task, "metadata", {}) or {}) if task is not None else {}
    order_id = ""
    if is_order_work(metadata):
        order_id = (str(metadata.get("order_id", "") or "").strip()
                    or str(metadata.get(BUDGET_ORDER_KEY, "") or "").strip())
    return model_plan_scope(plan_from_task(metadata), order_id=order_id)
