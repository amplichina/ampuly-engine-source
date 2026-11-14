"""H01a: immutable observations, never permission or dispatch proof.

Standard library only. No prompt, config, schema body, arguments, credential,
workspace path, actor token or controller token is stored in StepSnapshot.
The private turn binding is set only after the Provider's durable turn claim.
"""
from __future__ import annotations

import asyncio
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


class StepSnapshotError(ValueError):
    """Fixed diagnostics: callers must never attach private input to these."""

    def __init__(self, code: str) -> None:
        known = {
            "step_snapshot_invalid", "step_snapshot_schema_invalid",
            "step_snapshot_offer_mismatch", "step_snapshot_store_unavailable",
            "step_snapshot_persistence_failed", "step_snapshot_turn_invalid",
        }
        self.code = code if type(code) is str and code in known else "step_snapshot_invalid"
        super().__init__(self.code)


def observed_id(value: Any) -> str | None:
    """Retain a bounded identifier, without coercion or invented defaults."""
    if type(value) is not str or not value.strip() or len(value) > 256:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    return value


@dataclass(frozen=True, slots=True)
class HarnessTurnObservation:
    harness_session_id: str
    harness_turn_id: str
    task_id: str
    run_id: str | None
    project_id: str | None
    customer_id: str | None
    environment_id: str | None


@dataclass(frozen=True, slots=True)
class _TurnBinding:
    observation: HarnessTurnObservation
    task: Any = field(repr=False, compare=False)
    owner: asyncio.Task = field(repr=False, compare=False)


_turn: ContextVar[_TurnBinding | None] = ContextVar("ampuly_native_harness_turn_observation", default=None)


@contextmanager
def bind_native_harness_turn(task: Any, *, session_id: str, turn_id: str,
                             run_id: str = "", project_id: str = "",
                             customer_id: str = "", environment_id: str = ""):
    """Internal Provider scope; not callable from model/tool metadata.

Async tasks inherit ContextVars. Requiring BOTH the original Task object and
the owner asyncio task prevents a child from claiming its parent's turn.
"""
    owner = asyncio.current_task()
    task_id = observed_id(getattr(task, "id", None))
    if owner is None or task_id is None or observed_id(session_id) is None or observed_id(turn_id) is None:
        raise StepSnapshotError("step_snapshot_turn_invalid")
    observation = HarnessTurnObservation(session_id, turn_id, task_id, observed_id(run_id),
        observed_id(project_id), observed_id(customer_id), observed_id(environment_id))
    token = _turn.set(_TurnBinding(observation, task, owner))
    try:
        yield observation
    finally:
        _turn.reset(token)


def current_native_harness_turn(task: Any) -> HarnessTurnObservation | None:
    bound = _turn.get()
    if bound is None or bound.task is not task:
        return None
    try:
        owner = asyncio.current_task()
    except RuntimeError:
        return None
    if owner is not bound.owner or getattr(task, "id", None) != bound.observation.task_id:
        return None
    return bound.observation


@dataclass(frozen=True, slots=True)
class StepSnapshot:
    """A pre-request observation. Does NOT assert that a model call happened."""
    snapshot_id: str
    identity: tuple[tuple[str, str], ...]
    iteration: int
    request_sequence: int
    retrying: bool
    task_retry_count: int | None
    employee_attempt: int | None
    model_plan_observed_sha256: str | None
    tool_schema_sha256: str
    capability_sha256: str
    offered_tools: tuple[str, ...]
    declaration_epoch: int
    missing: tuple[str, ...]
    schema: int = 1

    def __post_init__(self) -> None:
        identity_fields = {
            "task_id", "task_session_id", "runtime_session_id", "canonical_turn_id", "task_project_id",
            "task_run_id", "delegation_run_id", "linked_work_item_id", "budget_id_reference", "employee_run_id",
            "harness_session_id", "harness_turn_id", "harness_run_id", "harness_project_id",
            "harness_customer_id", "harness_environment_id",
        }
        missing_fields = identity_fields | {
            "actual_dispatch_model", "transport_request_not_observed", "execution_source_contract",
            "scope_authorization_not_asserted", "budget_authorization_not_asserted", "tool_implementation_not_fixed",
            "tool_loaded_version", "tool_dependency_version", "model_plan", "task_retry_count", "employee_attempt",
            "return_schema_unavailable", "schema_source_incomplete",
        }
        def counter(value, minimum=0):
            return type(value) is int and minimum <= value <= 2**63 - 1
        def digest(value):
            return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None
        valid = (
            type(self.snapshot_id) is str and re.fullmatch(r"ss-[0-9a-f]{32}", self.snapshot_id) is not None
            and type(self.schema) is int and self.schema == 1 and type(self.retrying) is bool
            and counter(self.iteration, 1) and counter(self.request_sequence, 1) and counter(self.declaration_epoch)
            and (self.task_retry_count is None or counter(self.task_retry_count))
            and (self.employee_attempt is None or counter(self.employee_attempt, 1))
            and (self.model_plan_observed_sha256 is None or digest(self.model_plan_observed_sha256))
            and digest(self.tool_schema_sha256) and digest(self.capability_sha256)
            and type(self.identity) is tuple and len(self.identity) <= len(identity_fields)
            and all(type(pair) is tuple and len(pair) == 2 and type(pair[0]) is str
                    and pair[0] in identity_fields and observed_id(pair[1]) is not None for pair in self.identity)
            and len({pair[0] for pair in self.identity}) == len(self.identity)
            and type(self.offered_tools) is tuple and len(self.offered_tools) <= 500
            and all(type(name) is str and re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", name) for name in self.offered_tools)
            and len(set(self.offered_tools)) == len(self.offered_tools)
            and type(self.missing) is tuple and len(self.missing) <= len(missing_fields)
            and all(type(name) is str and name in missing_fields for name in self.missing)
        )
        if not valid:
            raise StepSnapshotError("step_snapshot_invalid")

    def to_payload(self) -> dict[str, Any]:
        # An explicit allowlist, not asdict(Task/config/context) or raw metadata.
        return {
            "schema": self.schema, "snapshot_id": self.snapshot_id,
            "phase": "runtime_main_request_prepared", "identity": dict(self.identity),
            "iteration": self.iteration, "request_sequence": self.request_sequence,
            "retrying": self.retrying, "task_retry_count": self.task_retry_count,
            "employee_attempt": self.employee_attempt,
            "model_plan_observed_sha256": self.model_plan_observed_sha256,
            "actual_dispatch_model": None,
            "tool_schema_sha256": self.tool_schema_sha256,
            "capability_sha256": self.capability_sha256,
            "offered_tools": list(self.offered_tools), "declaration_epoch": self.declaration_epoch,
            "implementation_fixed": False, "missing": list(self.missing),
        }
