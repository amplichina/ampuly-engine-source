"""Compatibility shim. Child caps and resume checks live in ``ampuly.core.cross_entry_guards``."""
from __future__ import annotations

from ampuly.core.cross_entry_guards import (
    CROSS_ENTRY_SOURCES,
    ENDED_TASK_STATUSES,
    MAX_CHILD_DEPTH,
    MAX_CHILD_TASKS,
    MAX_LIVE_CHILD_TASKS,
    CrossEntryRefused,
    admit_cross_entry_child,
    assert_cross_entry_child_capacity,
    entry_resume_refusal,
)

__all__ = [
    "CROSS_ENTRY_SOURCES",
    "ENDED_TASK_STATUSES",
    "MAX_CHILD_DEPTH",
    "MAX_CHILD_TASKS",
    "MAX_LIVE_CHILD_TASKS",
    "CrossEntryRefused",
    "admit_cross_entry_child",
    "assert_cross_entry_child_capacity",
    "entry_resume_refusal",
]
