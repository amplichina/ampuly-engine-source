"""Temporary, pure fail-closed boundary for unimplemented employee binding runs.

Saving a portable binding is not evidence that the executor can consume frozen
employee pins. No client flags, instance records, permissions or runtime state
can opt out of this guard. Remove it only with the actual server-side runtime
assembly, not by adding a metadata boolean.
"""
from __future__ import annotations

from typing import Any


EMPLOYEE_BINDING_RUNTIME_UNAVAILABLE = "employee_binding_runtime_unavailable"
EMPLOYEE_BINDING_RUNTIME_UNAVAILABLE_MESSAGE = (
    "AI 员工节点绑定的运行装配尚未接入；已保留草稿绑定，本次不能按旧角色或负责人执行。"
)


class EmployeeBindingRuntimeUnavailable(ValueError):
    code = EMPLOYEE_BINDING_RUNTIME_UNAVAILABLE

    def __init__(self) -> None:
        super().__init__(
            f"{self.code}: {EMPLOYEE_BINDING_RUNTIME_UNAVAILABLE_MESSAGE}"
        )


def employee_binding_runtime_findings(
    nodes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """One node-shaped finding per declared binding, including malformed ones.

    Only a real node key is inspected. Pins, booleans and assignments do not
    prove support. Structural validation belongs to the draft/package contract.
    """
    if not isinstance(nodes, list):
        raise TypeError("employee binding guard requires a node list")
    return [
        {
            "node_id": str(node.get("id", "") or ""),
            "field": "employee_binding",
            "code": EMPLOYEE_BINDING_RUNTIME_UNAVAILABLE,
            "message": EMPLOYEE_BINDING_RUNTIME_UNAVAILABLE_MESSAGE,
        }
        for node in nodes
        if isinstance(node, dict) and "employee_binding" in node
    ]


def assert_employee_binding_runtime_available(nodes: list[dict[str, Any]]) -> None:
    """Refuse before any preparation, grant, dispatch or model execution."""
    if employee_binding_runtime_findings(nodes):
        raise EmployeeBindingRuntimeUnavailable()
