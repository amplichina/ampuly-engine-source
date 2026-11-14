"""How a workflow node ends, with no plugin dependency.

A bound employee writes governed delivery. A node with no bound employee ends
as the scheduler's work item. Writing a governed delivery there would invent
an employee handoff.

The implementation lives here. ``ampuly.plugins.office_ui.workflow_employee_delivery``
re-exports it. Company mode imports this module, not the plugin.
"""
from __future__ import annotations

from typing import Any


def workflow_node_delivery_kind(node: Any) -> str:
    """How this workflow node ends.

    A bound employee writes governed_delivery. A node with no bound employee
    ends as the scheduler's work item. Writing a governed delivery there would
    invent an employee handoff and a flow identity, which is a fake flow.
    """
    if isinstance(node, dict) and "employee_binding" in node:
        return "governed_delivery"
    return "work_item"
