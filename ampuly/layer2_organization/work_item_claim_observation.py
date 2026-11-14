"""Private, synchronous observation of a returned durable WorkItem claim.

This captures selected scalars, not an atomic CAS receipt, an admission grant,
or a project/source proof.  There are no I/O or scheduling effects here.
"""

from __future__ import annotations

from dataclasses import dataclass
import inspect
from typing import Any

from ..core.models import DelegationWorkItem, Phase


_MAX_TEXT_BYTES = 512
_MAX_INT = 2**63 - 1
_INVALID_MESSAGE = "work-item claim observation invalid"


@dataclass(frozen=True, slots=True, repr=False)
class WorkItemClaimExpectation:
    work_item_id: str
    run_id: str
    role_runtime_session_id: str
    seat_id: str
    task_id: str
    work_item_revision: int
    controller_owner_token: str
    controller_lease_generation: int


@dataclass(frozen=True, slots=True, repr=False)
class WorkItemClaimObservation:
    work_item_id: str
    run_id: str
    role_runtime_session_id: str
    seat_id: str
    task_id: str
    work_item_revision: int
    controller_owner_token: str
    controller_lease_generation: int
    attempt_seq: int


def _invalid() -> None:
    raise RuntimeError(_INVALID_MESSAGE) from None


def _text(value: Any) -> str:
    if type(value) is not str or not value or len(value) > _MAX_TEXT_BYTES:
        _invalid()
    if value.strip() != value:
        _invalid()
    encoding_failed = False
    encoded = b""
    try:
        encoded = value.encode("utf-8")
    except Exception:
        encoding_failed = True
    if encoding_failed or len(encoded) > _MAX_TEXT_BYTES:
        _invalid()
    return value


def _integer(value: Any, *, minimum: int) -> int:
    if type(value) is not int or value < minimum or value > _MAX_INT:
        _invalid()
    return value


def _validate_fields(value: Any) -> None:
    _text(value.work_item_id)
    _text(value.run_id)
    _text(value.role_runtime_session_id)
    _text(value.seat_id)
    _text(value.task_id)
    _integer(value.work_item_revision, minimum=0)
    _text(value.controller_owner_token)
    _integer(value.controller_lease_generation, minimum=1)


def prepare_work_item_claim_expectation(
    *,
    work_item_id: str,
    run_id: str,
    role_runtime_session_id: str,
    seat_id: str,
    task_id: str,
    work_item_revision: int,
    controller_owner_token: str,
    controller_lease_generation: int,
) -> WorkItemClaimExpectation:
    expected = WorkItemClaimExpectation(
        work_item_id=work_item_id,
        run_id=run_id,
        role_runtime_session_id=role_runtime_session_id,
        seat_id=seat_id,
        task_id=task_id,
        work_item_revision=work_item_revision,
        controller_owner_token=controller_owner_token,
        controller_lease_generation=controller_lease_generation,
    )
    _validate_fields(expected)
    return expected


def capture_work_item_claim_observation(
    persisted: DelegationWorkItem,
    *,
    expected: WorkItemClaimExpectation,
) -> WorkItemClaimObservation:
    if type(expected) is not WorkItemClaimExpectation:
        _invalid()
    _validate_fields(expected)
    if type(persisted) is not DelegationWorkItem:
        _invalid()
    if persisted.phase is not Phase.RUNNING or type(persisted.metadata) is not dict:
        _invalid()
    metadata = persisted.metadata
    direct_matches = (
        (persisted.work_item_id, expected.work_item_id),
        (persisted.run_id, expected.run_id),
        (persisted.role_runtime_session_id, expected.role_runtime_session_id),
        (persisted.claimed_by_role_runtime_session_id, expected.role_runtime_session_id),
        (persisted.seat_id, expected.seat_id),
        (persisted.claimed_by_seat_id, expected.seat_id),
        (metadata.get("claimed_by_role_session_id"), expected.role_runtime_session_id),
        (metadata.get("claimed_task_id"), expected.task_id),
        (metadata.get("company_run_controller_owner_token"), expected.controller_owner_token),
    )
    for actual, wanted in direct_matches:
        if _text(actual) != wanted:
            _invalid()
    if _integer(metadata.get("claimed_work_item_revision"), minimum=0) != expected.work_item_revision:
        _invalid()
    if _integer(metadata.get("company_run_controller_lease_generation"), minimum=1) != expected.controller_lease_generation:
        _invalid()
    attempt_seq = metadata.get("attempt_seq")
    if type(attempt_seq) is not int or attempt_seq < 1:
        _invalid()
    for field in ("dispatch_hold", "queued_behind_session"):
        if field in metadata and (type(metadata[field]) is not str or metadata[field] != ""):
            _invalid()
    return WorkItemClaimObservation(
        work_item_id=expected.work_item_id,
        run_id=expected.run_id,
        role_runtime_session_id=expected.role_runtime_session_id,
        seat_id=expected.seat_id,
        task_id=expected.task_id,
        work_item_revision=expected.work_item_revision,
        controller_owner_token=expected.controller_owner_token,
        controller_lease_generation=expected.controller_lease_generation,
        attempt_seq=attempt_seq,
    )


def validate_claim_observation_sink(sink: Any) -> None:
    inspection_failed = False
    valid = False
    try:
        valid = callable(sink) and not (
            inspect.iscoroutinefunction(sink)
            or inspect.isasyncgenfunction(sink)
            or inspect.iscoroutinefunction(getattr(sink, "__call__", None))
            or inspect.isasyncgenfunction(getattr(sink, "__call__", None))
        )
    except Exception:
        inspection_failed = True
    if inspection_failed or not valid:
        _invalid()


def deliver_work_item_claim_observation(
    sink: Any,
    observation: WorkItemClaimObservation,
) -> None:
    validate_claim_observation_sink(sink)
    if type(observation) is not WorkItemClaimObservation:
        _invalid()
    _validate_fields(observation)
    if type(observation.attempt_seq) is not int or observation.attempt_seq < 1:
        _invalid()
    result = sink(observation)
    if result is None:
        return
    if inspect.iscoroutine(result):
        result.close()
    _invalid()
