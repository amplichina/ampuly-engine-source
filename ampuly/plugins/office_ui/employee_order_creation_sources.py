"""Private historical creation-link matching, never employee admission.

Caller-supplied identity is only structurally validated. Original Task/Run
facts establish the bounded historical link, not a business attempt, current
entity presence, trusted caller, pins selection or permission to execute.
"""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3

from .ai_employee_run_envelope import EmployeeRunIdentity, _identity
from ...database import order_task_origin as _task
from ...database import order_run_origin as _run


_MESSAGES = {
    "invalid_source": "原订单创建来源无法核验。",
    "unavailable": "原订单创建来源暂不可用。",
    "conflict": "原订单创建来源与员工身份不一致。",
}


class EmployeeOrderCreationSourcesError(ValueError):
    """Fixed ordinary diagnostic without private SQL, identifiers or values."""

    def __init__(self, code: str) -> None:
        self.code = code if type(code) is str and code in _MESSAGES else "invalid_source"
        super().__init__(_MESSAGES[self.code])


@dataclass(frozen=True, repr=False)
class MatchedCreationSources:
    """Caller-constructible observation; never proof of full employee identity."""

    identity: EmployeeRunIdentity
    task_fact: _task.OrderTaskCreationFact
    run_fact: _run.OrderRunCreationFact

    @property
    def validation_scope(self) -> str:
        return "creation_link_only"

    @property
    def is_authorized(self) -> bool:
        return False


def _failure_code(error: Exception) -> str:
    if type(error) in (
        EmployeeOrderCreationSourcesError,
        _task.OrderTaskOriginError,
        _run.OrderRunOriginError,
    ):
        return error.code
    return "unavailable"


def _read(
    conn: sqlite3.Connection, *, identity: EmployeeRunIdentity,
    delegation_run_id: str,
) -> MatchedCreationSources:
    if type(identity) is not EmployeeRunIdentity:
        raise EmployeeOrderCreationSourcesError("invalid_source")
    invalid_identity = False
    try:
        # Reuse the original ten-field validator, including frozen bypasses.
        # Its dictionary is not a replacement source or an authority token.
        _identity(identity)
    except Exception:
        invalid_identity = True
    if invalid_identity:
        raise EmployeeOrderCreationSourcesError("invalid_source") from None
    _task._project_id(identity.project_id)
    delegation_run_id = _task._text(delegation_run_id)
    # Explicit parameter independence does not require value inequality with
    # identity.run_id. Neither ID is derived or substituted for the other.
    _task._connection(conn, write=False)
    _task._project_connection(conn, identity.project_id)
    rows = _task._rows(conn, "PRAGMA query_only", ())
    if (len(rows) != 1 or len(rows[0]) != 1
            or type(rows[0][0]) is not int or rows[0][0] != 1):
        raise EmployeeOrderCreationSourcesError("invalid_source")

    run_fact = _run.read_order_run_creation(
        conn, project_id=identity.project_id, run_id=delegation_run_id,
    )
    if run_fact is None:
        raise EmployeeOrderCreationSourcesError("unavailable")
    if type(run_fact) is not _run.OrderRunCreationFact:
        raise EmployeeOrderCreationSourcesError("invalid_source")
    # The original Run reader's own Task-fact read remains intact. This second
    # read selects the employee identity's order Task, on the very same conn.
    task_fact = _task.read_order_task_creation(
        conn, project_id=identity.project_id, task_id=identity.order_task_id,
    )
    if task_fact is None:
        raise EmployeeOrderCreationSourcesError("unavailable")
    if (type(task_fact) is not _task.OrderTaskCreationFact
            or type(task_fact.origin) is not _task.OrderTaskOrigin):
        raise EmployeeOrderCreationSourcesError("invalid_source")

    origin = task_fact.origin
    if (run_fact.run_id != delegation_run_id
            or run_fact.project_id != identity.project_id
            or run_fact.source_task_id != identity.order_task_id
            or run_fact.source_task_fact_id != task_fact.fact_id
            or task_fact.task_id != identity.order_task_id
            or origin.project_id != identity.project_id
            or origin.order_id != identity.order_id
            or origin.workflow_id != identity.workflow_id
            or not origin.workflow_version
            or origin.workflow_version != identity.workflow_version):
        raise EmployeeOrderCreationSourcesError("conflict")
    return MatchedCreationSources(identity, task_fact, run_fact)


def read_employee_order_creation_sources(
    conn: sqlite3.Connection, *, identity: EmployeeRunIdentity,
    delegation_run_id: str,
) -> MatchedCreationSources:
    """Read only in the caller-owned, exact, query-only SQLite transaction.

    This function owns no connection/transaction lifecycle or migration. It
    returns the original identity/fact objects without supplementing versions,
    generating attempts or treating current metadata as an original source.
    Ordinary failures are fixed after handlers; cancellation is not caught.
    The private traceback may still contain locals; this is not global error
    sanitization for callers already executing inside their own exception.
    """
    result = None
    failure = None
    try:
        result = _read(conn, identity=identity, delegation_run_id=delegation_run_id)
    except Exception as error:
        failure = _failure_code(error)
    if failure is not None:
        raise EmployeeOrderCreationSourcesError(failure) from None
    return result
