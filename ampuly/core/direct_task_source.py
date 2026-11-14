"""Direct-task source contract (4.8 H01).

A direct task has an explicit project, a workspace and a budget, and no business
order/flow/node. This module is the single place that decides whether a task's
metadata is a valid direct source; callers must not re-implement the checks.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

DIRECT_SOURCES = frozenset({"direct_task", "employee_delegate"})
# A direct source may not smuggle a business identity in through metadata.
_BUSINESS_KEYS = ("order_id", "flow_id", "workflow_id", "node_id", "work_item_id")


class DirectTaskSourceRefused(ValueError):
    """The metadata does not describe a complete, order-less direct task."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class DirectTaskSource:
    kind: str
    project_id: str
    workspace_root: str
    budget_id: str
    customer_id: str = ""
    environment_id: str = ""

    def to_metadata(self) -> dict[str, str]:
        return {
            "execution_source": self.kind,
            "project_id": self.project_id,
            "workspace_root": self.workspace_root,
            "budget_id": self.budget_id,
            "customer_id": self.customer_id,
            "environment_id": self.environment_id,
        }


def _required(value: Any, code: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise DirectTaskSourceRefused(code)
    return text


def direct_task_source(
    *,
    kind: str,
    project_id: Any,
    workspace_root: Any,
    budget_id: Any,
    customer_id: Any = "",
    environment_id: Any = "",
    metadata: Mapping[str, Any] | None = None,
) -> DirectTaskSource:
    """Validate and freeze one direct task's source.

    Refuses a missing project/workspace/budget, an unknown source kind, a
    conflicting ``execution_source``, and any business order/flow/node key.
    """
    source_kind = str(kind or "").strip()
    if source_kind not in DIRECT_SOURCES:
        raise DirectTaskSourceRefused("direct_task_source_invalid")
    project = _required(project_id, "direct_task_project_missing")
    workspace = _required(workspace_root, "direct_task_workspace_missing")
    budget = _required(budget_id, "direct_task_budget_missing")
    data = dict(metadata or {})
    declared = str(data.get("execution_source", "") or "").strip()
    if declared and declared != source_kind:
        raise DirectTaskSourceRefused("direct_task_source_conflict")
    present = sorted(key for key in _BUSINESS_KEYS if str(data.get(key, "") or "").strip())
    if present:
        raise DirectTaskSourceRefused("direct_task_business_source_present")
    return DirectTaskSource(
        kind=source_kind,
        project_id=project,
        workspace_root=workspace,
        budget_id=budget,
        customer_id=str(customer_id or "").strip(),
        environment_id=str(environment_id or "").strip(),
    )
