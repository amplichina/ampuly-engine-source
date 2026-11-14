"""Resolve first-Run expectations from original durable creation records.

Only the caller's existing native transaction is read. The observation is a
selector, not a grant: every creation record and first-Run linkage is re-read
by the original readers. This leaf neither owns a transaction nor checks live
controller, Stop, employee or budget policy. The admission writer must recheck
the returned expectations in its own fenced transaction before any execution.
"""
from __future__ import annotations

from dataclasses import dataclass
import sqlite3

from . import order_root_admission as _admission
from . import order_root_task_binding as _binding
from . import order_task_origin as _task
from ..layer2_organization import first_run_source_observation as _observation


@dataclass(frozen=True, slots=True, repr=False)
class FirstRunSourceBasis:
    """Constructible historical expectations; never an execution capability."""

    observation: _observation.FirstRunSourceObservation
    expected_origin: _task.OrderTaskOrigin
    source_task_fact_id: str
    run_creation_fact_id: str
    root_creation_fact_id: str
    root_task_binding_fact_id: str


def _read(conn: sqlite3.Connection, observation) -> FirstRunSourceBasis:
    values = _observation._observation_values(observation)
    project_id, session_id, source_task_id, run_id, root_id, runtime_id = values
    # Preserve the original main-only/no-TEMP/native transaction constraints.
    _admission._connection(conn, write=False)
    _task._project_connection(conn, project_id)
    binding = _binding.read_order_root_task_binding(
        conn, project_id=project_id, run_id=run_id,
    )
    if binding is None:
        raise _admission.OrderRootAdmissionError("unavailable")
    if type(binding) is not _binding.OrderRootTaskBindingFact:
        raise _admission.OrderRootAdmissionError("invalid_source")
    # No caller supplies an origin or binding fact ID. The original chain
    # verifies native digests and requires this Run to be the unique first Run.
    task, run, root, checked_binding = _admission._chain(
        conn, project_id=project_id, session_id=session_id,
        source_task_id=source_task_id, run_id=run_id,
        root_work_item_id=root_id, runtime_task_id=runtime_id,
        root_task_binding_fact_id=binding.fact_id,
    )
    if checked_binding != binding:
        raise _admission.OrderRootAdmissionError("invalid_source")
    # Carry a fresh scalar-only selector. Do not retain a mutable Task, Run,
    # root object or an object obtained from arbitrary metadata.
    selected = _observation.FirstRunSourceObservation(*values)
    return FirstRunSourceBasis(
        selected, task.origin, task.fact_id, run.fact_id, root.fact_id,
        checked_binding.fact_id,
    )


def read_first_run_source_basis_on_connection(
    conn: sqlite3.Connection, *, observation: _observation.FirstRunSourceObservation,
) -> FirstRunSourceBasis:
    """Read four protected source records without transaction control or I/O.

    A missing fact/schema is unavailable. Invalid native bytes, ambiguous first
    Run, linkage mismatch or malformed selector fails closed. BaseException is
    propagated unchanged. Success is historical source correlation only.
    """
    result = None
    failure = None
    try:
        _observation._observation_values(observation)
    except Exception:
        failure = "invalid_source"
    if failure is not None:
        raise _admission.OrderRootAdmissionError(failure) from None
    try:
        result = _read(conn, observation)
    except Exception as error:
        # The original source reader errors share the existing fixed vocabulary.
        # Unknown native/dependency failures are unavailable, never authority.
        failure = _admission._failure_code(error)
    if failure is not None:
        raise _admission.OrderRootAdmissionError(failure) from None
    return result
