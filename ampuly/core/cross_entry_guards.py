"""Child-task caps and resume generations shared by the direct and employee entries.

The numbers match the workflow order cap: eight children of one parent, two of
them not yet ended, and a new child whose depth has reached 32 is refused.
Root depth is 0. A stale resume generation or attempt must not open a task.

The implementation lives here. ``ampuly.plugins.office_ui.cross_entry_guards``
re-exports it. Core and organization code import this module, not the plugin.
"""
from __future__ import annotations

from typing import Any

CROSS_ENTRY_SOURCES = frozenset({"direct_task", "employee_delegate"})
MAX_CHILD_TASKS = 8
MAX_LIVE_CHILD_TASKS = 2
MAX_CHILD_DEPTH = 32
ENDED_TASK_STATUSES = frozenset({"done", "failed", "cancelled", "closed", "rejected"})


class CrossEntryRefused(RuntimeError):
    """A direct or employee child was refused before it was started."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _status_name(value: Any) -> str:
    status = getattr(value, "status", value)
    raw = getattr(status, "value", status)
    return str(raw or "").strip().lower()


def assert_cross_entry_child_capacity(*, child_statuses: Any, depth: Any) -> None:
    """Refuse a child that would pass the workflow fan-out caps.

    ``depth`` is the depth the new child would be recorded at. Depth is
    checked before the census, so a child at depth 32 is
    ``workflow_child_limit`` even when the parent has no children yet.
    """
    if type(depth) is not int or depth < 0:
        raise CrossEntryRefused("workflow_child_parent_invalid")
    if depth >= MAX_CHILD_DEPTH:
        raise CrossEntryRefused("workflow_child_limit")
    statuses = [_status_name(item) for item in child_statuses]
    if len(statuses) >= MAX_CHILD_TASKS:
        raise CrossEntryRefused("workflow_child_limit")
    live = sum(1 for item in statuses if item not in ENDED_TASK_STATUSES)
    if live >= MAX_LIVE_CHILD_TASKS:
        raise CrossEntryRefused("workflow_child_concurrency")


def entry_resume_refusal(
    metadata: Any,
    *,
    offered_generation: Any = None,
    offered_attempt: Any = None,
) -> str:
    """Empty when the offer may proceed. A stale offer names the refusal.

    Mirrors the company lease check: a recorded generation above zero rejects
    an offer that is not strictly newer. The same rule applies to the recorded
    resume attempt, or the employee attempt when no resume attempt was stored.
    Other execution sources are not this check.
    """
    meta = dict(metadata or {})
    if str(meta.get("execution_source") or "") not in CROSS_ENTRY_SOURCES:
        return ""
    if offered_generation is not None:
        try:
            prior = int(meta.get("resume_controller_lease_generation") or 0)
        except (TypeError, ValueError):
            prior = 0
        try:
            offered = int(offered_generation)
        except (TypeError, ValueError):
            offered = 0
        if prior > 0 and offered <= prior:
            return "resume_generation_stale"
    if offered_attempt is not None:
        if meta.get("resume_attempt") not in (None, ""):
            raw = meta.get("resume_attempt")
        else:
            raw = meta.get("employee_attempt")
        try:
            prior_attempt = int(raw or 0)
        except (TypeError, ValueError):
            prior_attempt = 0
        try:
            offered = int(offered_attempt)
        except (TypeError, ValueError):
            offered = 0
        if prior_attempt > 0 and offered <= prior_attempt:
            return "resume_attempt_stale"
    return ""


async def admit_cross_entry_child(
    store: Any,
    parent_id: str,
    *,
    entry: str,
    project_id: str,
    extra_statuses: Any = None,
    parent: Any = None,
) -> int:
    """Return the child depth, or refuse before the child exists.

    Store children and ``extra_statuses`` (in-memory subagent states that have
    not been written as tasks) are one census. An unreadable census is not a
    grant. A parent object is used only when the store has no row for it.
    """
    if store is None:
        raise CrossEntryRefused("workflow_child_capacity_unreadable")
    loaded = None
    getter = getattr(store, "get_task", None)
    if not callable(getter):
        if parent is None:
            raise CrossEntryRefused("workflow_child_parent_invalid")
    else:
        try:
            loaded = await getter(parent_id)
        except Exception as exc:
            raise CrossEntryRefused("workflow_child_parent_invalid") from exc
    chosen = loaded if loaded is not None else parent
    if chosen is None or str(getattr(chosen, "id", "") or "") != str(parent_id or ""):
        raise CrossEntryRefused("workflow_child_parent_invalid")
    parent_project = str(getattr(chosen, "project_id", "") or "")
    if project_id and parent_project and parent_project != str(project_id):
        raise CrossEntryRefused("workflow_child_parent_invalid")
    meta = dict(getattr(chosen, "metadata", {}) or {})
    source = str(meta.get("execution_source") or "")
    if source not in CROSS_ENTRY_SOURCES or (entry and source != str(entry)):
        raise CrossEntryRefused("workflow_child_parent_invalid")
    try:
        parent_depth = int(meta.get("entry_child_depth") or 0)
    except (TypeError, ValueError):
        raise CrossEntryRefused("workflow_child_parent_invalid") from None
    if type(parent_depth) is not int or parent_depth < 0:
        raise CrossEntryRefused("workflow_child_parent_invalid")
    child_depth = parent_depth + 1
    lister = getattr(store, "get_tasks", None)
    if not callable(lister):
        raise CrossEntryRefused("workflow_child_capacity_unreadable")
    try:
        rows = await lister(project_id=parent_project or project_id, parent_id=str(parent_id))
    except TypeError:
        try:
            rows = await lister(parent_id=str(parent_id))
        except Exception as exc:
            raise CrossEntryRefused("workflow_child_capacity_unreadable") from exc
    except Exception as exc:
        raise CrossEntryRefused("workflow_child_capacity_unreadable") from exc
    if rows is None:
        raise CrossEntryRefused("workflow_child_capacity_unreadable")
    statuses = [_status_name(row) for row in rows]
    statuses.extend(_status_name(item) for item in (extra_statuses or []))
    assert_cross_entry_child_capacity(child_statuses=statuses, depth=child_depth)
    return child_depth
