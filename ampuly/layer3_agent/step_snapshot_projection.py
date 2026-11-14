"""H01a adapter: bounded observations + the existing event writer.

Only main Runtime requests use this; no new store, policy, budget or provider
route. Private declaration_snapshot helpers are reused intentionally: one
bounded encoder, not a second permissive JSON serializer. They need contract
tests before adoption. No fingerprint here proves loaded implementation.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..core.step_snapshot import (
    StepSnapshot, StepSnapshotError, current_native_harness_turn, observed_id,
)
from ..layer4_tools.declaration_snapshot import _Budget, _schema, _allowed


def _bounded_json(value: dict) -> bytes:
    try:
        encoded, _ = _schema(value, _Budget())
        return encoded
    except Exception:
        pass
    # Outside except: fixed error with no schema/default/private input context.
    raise StepSnapshotError("step_snapshot_schema_invalid")


@dataclass(frozen=True, slots=True)
class PreparedToolOffer:
    names: tuple[str, ...]
    schema_sha256: str
    capability_sha256: str
    declaration_epoch: int
    gaps: tuple[str, ...]
    _wire_json: bytes = field(repr=False)

    def wire_tools(self) -> list[dict[str, Any]]:
        """A fresh copy from bounded bytes, not a reference into the registry."""
        return json.loads(self._wire_json)["tools"]

    def assert_executor_names(self, actual: frozenset[str] | None) -> None:
        if type(actual) is not frozenset or actual != frozenset(self.names):
            raise StepSnapshotError("step_snapshot_offer_mismatch")


def prepare_tool_offer(registry: Any, wire_tools: Any) -> PreparedToolOffer:
    """Fingerprint precisely the Runtime-to-provider schema and matching gate.

Declarations are observed from the original registry. Implementation identity
is NOT pinned; two reads are NOT an atomic lease. Later revocation still wins.
"""
    code = "step_snapshot_schema_invalid"
    try:
        if type(wire_tools) is not list or len(wire_tools) > 500:
            raise StepSnapshotError(code)
        raw = _bounded_json({"tools": wire_tools})
        detached = json.loads(raw)["tools"]
        names: list[str] = []
        parameters: list[Any] = []
        for tool in detached:
            if type(tool) is not dict or tool.get("type") != "function":
                raise StepSnapshotError(code)
            function = tool.get("function")
            if type(function) is not dict or "parameters" not in function:
                raise StepSnapshotError(code)
            names.append(function.get("name"))
            parameters.append(function["parameters"])
        ordered = _allowed(names)  # Exact bounded names, no duplicates/coercion.
        declarations = registry.observe_registered_declarations(names)
        if tuple(row.name for row in declarations.declarations) != ordered:
            raise StepSnapshotError("step_snapshot_offer_mismatch")
        digest = hashlib.sha256(b"ampuly-h01a-capability-observation-v1\0")
        budget = _Budget()  # Shared, not a new allowance for every tool.
        gaps: set[str] = set()
        for value, row in zip(parameters, declarations.declarations):
            encoded, _ = _schema(value, budget)
            if encoded != row.parameters_json:
                raise StepSnapshotError("step_snapshot_offer_mismatch")
            for part in (row.name.encode("utf-8"), row.parameters_json,
                         b"missing" if row.return_schema_json is None else row.return_schema_json):
                digest.update(len(part).to_bytes(8, "big"))
                digest.update(part)
            gaps.update(row.gaps)
        return PreparedToolOffer(ordered or (), hashlib.sha256(raw).hexdigest(), digest.hexdigest(),
            declarations.epoch, tuple(sorted(gaps)), raw)
    except StepSnapshotError as error:
        code = error.code
    except Exception:
        pass
    raise StepSnapshotError(code)


def _counter(value: Any, *, minimum: int = 0) -> int | None:
    return value if type(value) is int and minimum <= value <= 2**63 - 1 else None


def snapshot_main_request(*, task: Any, runtime_session_id: str, canonical_turn_id: str,
                          iteration: int, request_sequence: int, retrying: bool,
                          model_plan: dict | None, offer: PreparedToolOffer) -> StepSnapshot:
    if (_counter(iteration, minimum=1) is None or _counter(request_sequence, minimum=1) is None
            or type(retrying) is not bool):
        raise StepSnapshotError("step_snapshot_invalid")
    metadata = getattr(task, "metadata", None)
    metadata = metadata if type(metadata) is dict else {}
    employee = metadata.get("employee_run")
    employee = employee if type(employee) is dict else {}
    candidates = {
        "task_id": getattr(task, "id", None), "task_session_id": getattr(task, "session_id", None),
        "runtime_session_id": runtime_session_id, "canonical_turn_id": canonical_turn_id,
        "task_project_id": getattr(task, "project_id", None),
        "task_run_id": metadata.get("run_id"), "delegation_run_id": metadata.get("delegation_run_id"),
        "linked_work_item_id": getattr(task, "linked_work_item_id", None),
        "budget_id_reference": metadata.get("budget_id"), "employee_run_id": employee.get("run_id"),
    }
    turn = current_native_harness_turn(task)
    if turn is not None:
        candidates.update({
            "harness_session_id": turn.harness_session_id, "harness_turn_id": turn.harness_turn_id,
            "harness_run_id": turn.run_id, "harness_project_id": turn.project_id,
            "harness_customer_id": turn.customer_id, "harness_environment_id": turn.environment_id,
        })
    else:
        candidates.update({"harness_session_id": None, "harness_turn_id": None, "harness_run_id": None,
                           "harness_project_id": None, "harness_customer_id": None, "harness_environment_id": None})
    missing = {name for name, value in candidates.items() if observed_id(value) is None}
    missing.update({"actual_dispatch_model", "transport_request_not_observed", "execution_source_contract",
                    "scope_authorization_not_asserted", "budget_authorization_not_asserted", "tool_implementation_not_fixed",
                    "tool_loaded_version", "tool_dependency_version"})
    missing.update(offer.gaps)
    plan_digest = None
    if type(model_plan) is dict:
        plan_digest = hashlib.sha256(_bounded_json(model_plan)).hexdigest()
    else:
        missing.add("model_plan")
    retry_count = _counter(getattr(task, "retry_count", None))
    employee_attempt = _counter(employee.get("attempt"), minimum=1)
    if retry_count is None:
        missing.add("task_retry_count")
    if employee_attempt is None:
        missing.add("employee_attempt")
    identity = tuple((name, value) for name, raw in candidates.items() if (value := observed_id(raw)) is not None)
    return StepSnapshot("ss-" + uuid.uuid4().hex, identity, iteration, request_sequence, retrying,
        retry_count, employee_attempt, plan_digest, offer.schema_sha256, offer.capability_sha256,
        offer.names, offer.declaration_epoch, tuple(sorted(missing)))


async def persist_step_snapshot(store: Any, *, runtime_session_id: str, snapshot: StepSnapshot) -> None:
    """Use the original writer; no silent no-store or write-failure success.

Keep this outside the model retry try/except. A persistence failure must not
be interpreted as a provider rejection or cause a model request/retry.
"""
    if (observed_id(runtime_session_id) is None
            or dict(snapshot.identity).get("runtime_session_id") != runtime_session_id):
        raise StepSnapshotError("step_snapshot_invalid")
    writer = getattr(store, "save_runtime_event", None)
    if not callable(writer):
        raise StepSnapshotError("step_snapshot_store_unavailable")
    try:
        await writer(runtime_session_id, "step_snapshot.created", snapshot.to_payload())
        return
    except Exception:
        pass
    # CancelledError/BaseException propagate. No raw database error is echoed.
    raise StepSnapshotError("step_snapshot_persistence_failed")
