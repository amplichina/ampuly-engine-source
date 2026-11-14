"""Direct-task scope and allowance binding (4.8 H01).

A direct task has no business order. Its allowance belongs to the project and is
opened once, then reused by every turn of the conversation. Keeping this small
composer out of ``ws_handler`` also keeps that canonical source under its
bounded-size guard.
"""
from __future__ import annotations

from typing import Any


def bind_direct_task_budget(
    *,
    engine: Any,
    project_id: str,
    actor: str,
    customer_id: str = "",
    environment_id: str = "",
) -> str:
    """Open or reuse the project's direct-task allowance; return its id.

    Raises :class:`MasterSafetyStop` on any failure: the caller must not fall
    back to an unbudgeted single-JSON call.
    """
    from .business_store import BusinessStore
    from .master_runtime import MasterSafetyStop

    clean_actor = str(actor or "").strip()
    if not clean_actor:
        raise MasterSafetyStop("Direct task budget requires the owner identity; no model call was started")
    try:
        store = BusinessStore(engine.ampuly_home)
        budget = store.ensure_direct_task_budget(
            project_id=project_id,
            customer_id=str(customer_id or ""),
            environment_id=str(environment_id or ""),
            actor=clean_actor,
        )
    except Exception as exc:
        raise MasterSafetyStop("Direct task budget could not be opened; no model call was started") from exc
    budget_id = str((budget or {}).get("budget_id", "") or "")
    if not budget_id:
        raise MasterSafetyStop("Direct task budget returned no allowance; no model call was started")
    return budget_id


def apply_direct_source(task: Any, *, project_id: str, workspace_root: str, budget_id: str) -> dict[str, str]:
    """Validate and stamp a direct task's source metadata; refuses business keys."""
    from ampuly.core.direct_task_source import direct_task_source

    metadata = dict(getattr(task, "metadata", {}) or {})
    source = direct_task_source(
        kind="direct_task",
        project_id=project_id,
        workspace_root=workspace_root,
        budget_id=budget_id,
        customer_id=str(metadata.get("customer_id", "") or ""),
        environment_id=str(metadata.get("environment_id", "") or ""),
        metadata=metadata,
    )
    update = source.to_metadata()
    task.metadata = {**metadata, **update}
    return update
