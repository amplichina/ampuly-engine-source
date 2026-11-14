"""Private scalar observations of actual first-Run producer returns.

No I/O, durable linkage proof, admission, authority, or successful execution is
established here. Callers can construct carriers; the production seam, original
facts and a future strict writer remain necessary. Mutable source objects are
never retained in the frozen carriers.
"""

from __future__ import annotations

from dataclasses import dataclass
import inspect
from typing import Any

from ..core.models import DelegationWorkItem, Task
from ..database import order_task_origin as _task
from ..database.order_run_origin import validate_order_run_selector


_INVALID_MESSAGE = "first-run source observation invalid"


@dataclass(frozen=True, slots=True, repr=False)
class FirstRunSourceSelection:
    project_id: str
    session_id: str
    source_task_id: str


@dataclass(frozen=True, slots=True, repr=False)
class FirstRunRootSource:
    project_id: str
    session_id: str
    source_task_id: str
    run_id: str
    root_work_item_id: str


@dataclass(frozen=True, slots=True, repr=False)
class FirstRunSourceObservation:
    project_id: str
    session_id: str
    source_task_id: str
    run_id: str
    root_work_item_id: str
    execution_task_id: str


def _invalid() -> None:
    raise RuntimeError(_INVALID_MESSAGE) from None


def _identity(value: Any) -> str:
    failed = False
    result = None
    try:
        result = _task._text(value)
    except Exception:
        failed = True
    if failed:
        _invalid()
    return result


def _selection_values(*, project_id: Any, session_id: Any, source_task_id: Any) -> tuple[str, str, str]:
    failed = False
    result = None
    try:
        validate_order_run_selector(
            project_id=project_id, session_id=session_id, source_task_id=source_task_id,
        )
        result = (
            _task._text(project_id), _task._text(session_id), _task._text(source_task_id),
        )
    except Exception:
        failed = True
    if failed:
        _invalid()
    return result


def _root_values(root_source: FirstRunRootSource) -> tuple[str, str, str, str, str]:
    if type(root_source) is not FirstRunRootSource:
        _invalid()
    return (
        *_selection_values(
            project_id=root_source.project_id, session_id=root_source.session_id,
            source_task_id=root_source.source_task_id,
        ),
        _identity(root_source.run_id), _identity(root_source.root_work_item_id),
    )


def _observation_values(observation: FirstRunSourceObservation) -> tuple[str, str, str, str, str, str]:
    if type(observation) is not FirstRunSourceObservation:
        _invalid()
    return (
        *_selection_values(
            project_id=observation.project_id, session_id=observation.session_id,
            source_task_id=observation.source_task_id,
        ),
        _identity(observation.run_id), _identity(observation.root_work_item_id),
        _identity(observation.execution_task_id),
    )


def prepare_first_run_source_selection(
    *, project_id: str, session_id: str, source_task_id: str,
) -> FirstRunSourceSelection:
    values = _selection_values(
        project_id=project_id, session_id=session_id, source_task_id=source_task_id,
    )
    return FirstRunSourceSelection(*values)


def capture_first_run_root_source(
    *, selection: FirstRunSourceSelection, run_id: str,
    root_work_item: DelegationWorkItem,
) -> FirstRunRootSource:
    if type(selection) is not FirstRunSourceSelection:
        _invalid()
    selected = _selection_values(
        project_id=selection.project_id, session_id=selection.session_id,
        source_task_id=selection.source_task_id,
    )
    if type(root_work_item) is not DelegationWorkItem:
        _invalid()
    returned_run_id = _identity(run_id)
    if _identity(root_work_item.run_id) != returned_run_id:
        _invalid()
    root_work_item_id = _identity(root_work_item.work_item_id)
    return FirstRunRootSource(*selected, returned_run_id, root_work_item_id)


def capture_first_run_source_observation(
    *, root_source: FirstRunRootSource, execution_task: Task,
) -> FirstRunSourceObservation:
    root_values = _root_values(root_source)
    if type(execution_task) is not Task:
        _invalid()
    if _identity(execution_task.project_id) != root_source.project_id:
        _invalid()
    if _identity(getattr(execution_task, "linked_work_item_id", None)) != root_source.root_work_item_id:
        _invalid()
    execution_task_id = _identity(execution_task.id)
    return FirstRunSourceObservation(*root_values, execution_task_id)


def validate_first_run_source_sink(sink: Any) -> None:
    failed = False
    valid = False
    try:
        valid = callable(sink) and not (
            inspect.iscoroutinefunction(sink)
            or inspect.isasyncgenfunction(sink)
            or inspect.iscoroutinefunction(getattr(sink, "__call__", None))
            or inspect.isasyncgenfunction(getattr(sink, "__call__", None))
        )
    except Exception:
        failed = True
    if failed or not valid:
        _invalid()


def deliver_first_run_source_observation(
    sink: Any, observation: FirstRunSourceObservation,
) -> None:
    validate_first_run_source_sink(sink)
    _observation_values(observation)
    result = sink(observation)
    if result is None:
        return
    if inspect.iscoroutine(result):
        result.close()
    _invalid()
