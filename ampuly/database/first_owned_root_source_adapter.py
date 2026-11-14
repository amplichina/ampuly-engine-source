"""Map historical first-Run expectations to the original fenced Store writer.

This does not grant admission, Stop/budget/employee policy, or start a worker.
Only the original Store owns the native transaction and its two active fences.
No caller-supplied writer/callback, metadata defaults, retry or fallback exists.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..core.models import Task
from ..layer2_organization import first_run_source_observation as _observation
from .store import AmpulyStore
from .first_run_source_basis import FirstRunSourceBasis
from . import order_task_origin as _task
from . import order_run_origin as _run
from . import order_root_origin as _root
from . import order_root_task_binding as _binding
from . import order_root_admission as _admission


_MISSING = object()
_OBSERVATION_FIELDS = (
    'project_id','session_id','source_task_id','run_id','root_work_item_id','execution_task_id',
)
_BASIS_FACT_FIELDS = (
    'source_task_fact_id','run_creation_fact_id','root_creation_fact_id','root_task_binding_fact_id',
)
_ECHO_IDENTITY_FIELDS = (
    'project_id','session_id','source_task_id','run_id','root_work_item_id','runtime_task_id',
)
_CODES = ('invalid_source','unavailable','conflict','outcome_unknown')


def _fail(code: str = 'invalid_source') -> None:
    raise _admission.OrderRootAdmissionError(code) from None


def _fields(value) -> dict:
    fields = object.__getattribute__(value,'__dict__')
    if (type(fields) is not dict or len(fields)>256
            or any(type(key) is not str for key in fields)):
        _fail()
    return fields


def _task_identity(task: Task) -> tuple[tuple[str, ...], dict, dict]:
    if type(task) is not Task:
        _fail()
    fields = _fields(task)
    metadata = fields.get('metadata')
    if (type(metadata) is not dict or len(metadata)>256
            or any(type(key) is not str for key in metadata)):
        _fail()
    # No metadata fallback for scope/session/root/runtime identity.
    values = tuple(_task._text(fields.get(name,_MISSING)) for name in (
        'project_id','session_id','id','linked_work_item_id',
    )) + (_task._text(metadata.get('delegation_run_id',_MISSING)),)
    return values,fields,metadata


@dataclass(frozen=True, slots=True, repr=False)
class _CapturedRequest:
    selectors: tuple[str, ...]
    fact_ids: tuple[str, ...]
    origin: _task.OrderTaskOrigin
    store_fields: dict
    task_fields: dict
    task_metadata: dict


def _capture(store, task, basis) -> _CapturedRequest:
    if type(store) is not AmpulyStore or type(basis) is not FirstRunSourceBasis:
        _fail()
    store_fields = _fields(store)
    raw_observation = basis.observation
    if type(raw_observation) is not _observation.FirstRunSourceObservation:
        _fail()
    # Scalar guards before original observation validation prevent unknown coercion.
    selectors = tuple(_task._text(getattr(raw_observation,name)) for name in _OBSERVATION_FIELDS)
    if selectors[0]!='default' or _task._text(store_fields.get('project_id',_MISSING))!=selectors[0]:
        _fail()
    if _observation._observation_values(raw_observation)!=selectors:
        _fail()
    values = _task._origin_values(basis.expected_origin,project_id=selectors[0])
    fact_ids = tuple(_task._digest(getattr(basis,name)) for name in _BASIS_FACT_FIELDS)
    project,session,source_id,run_id,root_id,execution_id = selectors
    # Consistency only, NOT a native source read or permission. All four must
    # match before any writer invocation; do not discover a bad unused ID later.
    task_fact_id = _task._fingerprint(values,source_id)
    run_fact_id = _run._fingerprint((run_id,project,session,source_id,task_fact_id))
    root_fact_id = _root._fingerprint((project,session,source_id,task_fact_id,run_id,run_fact_id,root_id))
    binding_fact_id = _binding._fingerprint((root_fact_id,project,run_id,root_id,
                                           execution_id,session,_binding.MATERIALIZATION_KIND))
    if fact_ids!=(task_fact_id,run_fact_id,root_fact_id,binding_fact_id):
        _fail()
    actual,task_fields,metadata = _task_identity(task)
    if actual!=(project,session,execution_id,root_id,run_id):
        _fail()
    # A private independent original DTO: no mutable caller origin across await.
    origin = _task.OrderTaskOrigin(*values)
    return _CapturedRequest(selectors,fact_ids,origin,store_fields,task_fields,metadata)


def _request_unchanged(store, task, captured: _CapturedRequest) -> None:
    if (type(store) is not AmpulyStore or _fields(store) is not captured.store_fields
            or _task._text(captured.store_fields.get('project_id',_MISSING))!=captured.selectors[0]):
        _fail('outcome_unknown')
    actual,fields,metadata = _task_identity(task)
    project,session,source_id,run_id,root_id,execution_id = captured.selectors
    if (fields is not captured.task_fields or metadata is not captured.task_metadata
            or actual!=(project,session,execution_id,root_id,run_id)):
        _fail('outcome_unknown')


def _echo(result, captured: _CapturedRequest) -> None:
    if type(result) is not _admission.FirstOwnedRootAdmissionFact:
        _fail('outcome_unknown')
    identities = tuple(_task._text(getattr(result,name)) for name in _ECHO_IDENTITY_FIELDS)
    fact_ids = tuple(_task._digest(getattr(result,name)) for name in _BASIS_FACT_FIELDS)
    if identities!=captured.selectors or fact_ids!=captured.fact_ids:
        _fail('outcome_unknown')


async def record_first_owned_root_from_basis(
    *, store: AmpulyStore, task: Task, basis: FirstRunSourceBasis,
) -> _admission.FirstOwnedRootAdmissionFact:
    """Call the original exact Store record once, using captured expectations.

    The Store re-reads all four protected native facts/current entities and
    awaits its original active-effect fence twice in its own owned transaction.
    No current role/seat/claim/gen/token, Stop or money permission is fabricated.
    Unknown write results never cause a retry; BaseException passes unchanged.
    """
    failure = None
    result = None
    write_started = False
    try:
        captured = _capture(store,task,basis)
        # Direct original base method, not an instance/subclass writer callback.
        write_started = True
        result = await AmpulyStore._record_first_owned_root_admission(
            store,task,source_task_id=captured.selectors[2],session_id=captured.selectors[1],
            expected_origin=captured.origin,expected_root_task_binding_fact_id=captured.fact_ids[3],
        )
        _echo(result,captured)
        _request_unchanged(store,task,captured)
    except Exception as error:
        if type(error) is _admission.OrderRootAdmissionError:
            code = error.code
            failure = code if type(code) is str and code in _CODES else (
                'outcome_unknown' if write_started else 'invalid_source')
            # Input/echo checks after the call may raise an original scalar error.
            # Only exact known Store errors carry its authoritative classification.
            if write_started and result is not None:
                failure = 'outcome_unknown'
        elif write_started:
            failure = 'outcome_unknown'
        else:
            failure = _admission._failure_code(error)
    # Construct outside the handler; never retain private original context/args.
    if failure is not None:
        raise _admission.OrderRootAdmissionError(failure) from None
    return result
