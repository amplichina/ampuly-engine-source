"""Strict human-review rework preparation; no runtime authority is granted here.

The host owns current-account, original-order, Stop, budget and controller
checks. The Store must call the same validator again on its transaction's
persisted record and append the receipt in that transaction. An accepted
dataclass or a digest is NOT a permit and MUST NOT be accepted from RPC.

All graph identities come from the frozen plan plus durable WorkItem scope_key
and runtime links. A node id is never treated as a projection id. This leaf
does not read a file/database, create a process, call a model or dispatch work.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
import unicodedata
from typing import Any


ACTION = "human_review_rework_confirm"
OPERATION = "human_review_rework"
MAX_NODES = 500
MAX_EDGES = 10000
MAX_NOTE_BYTES = 16384
MAX_RECORD_BYTES = 65536
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\Z", re.ASCII)
_SHA = re.compile(r"[a-f0-9]{64}\Z", re.ASCII)
_RECORD = re.compile(r"rr-[a-f0-9]{16}\Z", re.ASCII)
_RUN = re.compile(r"er-[a-f0-9]{16}\Z", re.ASCII)
_PUBLIC_KEYS = frozenset({
    "type", "project_id", "order_id", "task_id", "review_node_id",
    "rework_record_id", "expected_attempt", "reviewed_artifact_id",
    "reviewed_artifact_version", "reviewed_content_hash", "revision_note",
    "revision_note_hash", "request_id",
})
_SCOPE_KEYS = (
    "project_id", "order_id", "order_task_id", "delegation_run_id",
    "review_node_id", "review_task_id", "review_work_item_id", "target_node_id",
    "target_task_id", "target_work_item_id", "checkpoint_id",
)
_MATERIAL_KEYS = ("reviewed_artifact_id", "reviewed_artifact_version", "reviewed_content_hash")


class HumanReviewReworkError(ValueError):
    """Finite code only; raw feedback, account tokens and records stay private."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str = "rework_invalid") -> None:
    raise HumanReviewReworkError(code) from None


def _id(value: Any) -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        _fail()
    return value


def _sha(value: Any) -> str:
    if type(value) is not str or _SHA.fullmatch(value) is None:
        _fail()
    return value


def _integer(value: Any, *, zero: bool = False) -> int:
    if type(value) is not int or not (0 if zero else 1) <= value <= 2**53 - 1:
        _fail()
    return value


def _canonical(value: Any) -> bytes:
    # Inputs to this helper have a finite shape; reject custom values before
    # JSON encoders can invoke user hooks or coerce non-JSON objects.
    count = 0
    stack = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        count += 1
        if depth > 12 or count > 12000:
            _fail()
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                _fail()
            stack.extend((child, depth + 1) for child in item.values())
        elif type(item) is list:
            stack.extend((child, depth + 1) for child in item)
        elif type(item) is float:
            if not math.isfinite(item):
                _fail()
        elif type(item) not in (str, int, bool, type(None)):
            _fail()
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError):
        _fail()
    if len(raw) > MAX_RECORD_BYTES:
        _fail()
    return raw


def normalize_revision_note(value: Any) -> str:
    if type(value) is not str:
        _fail()
    value = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n")).strip()
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        _fail()
    if (not 1 <= size <= MAX_NOTE_BYTES
            or any(ord(char) < 32 and char not in "\n\t" or ord(char) == 127 for char in value)):
        _fail()
    return value


def revision_note_sha256(value: Any) -> str:
    return hashlib.sha256(normalize_revision_note(value).encode("utf-8")).hexdigest()


def validate_confirm_request(value: Any) -> dict[str, Any]:
    if (type(value) is not dict or not _PUBLIC_KEYS <= value.keys()
            or value.keys() - (_PUBLIC_KEYS | {"target_node_id"})
            or value.get("type") != ACTION):
        _fail()
    _canonical(value)
    result = dict(value)
    for key in _PUBLIC_KEYS - {"type", "expected_attempt", "reviewed_artifact_version",
                              "revision_note", "revision_note_hash", "reviewed_content_hash"}:
        _id(result[key])
    if _RECORD.fullmatch(result["rework_record_id"]) is None:
        _fail()
    if "target_node_id" in result:
        _id(result["target_node_id"])
    _integer(result["expected_attempt"])
    _integer(result["reviewed_artifact_version"])
    _sha(result["reviewed_content_hash"])
    _sha(result["revision_note_hash"])
    result["revision_note"] = normalize_revision_note(result["revision_note"])
    if revision_note_sha256(result["revision_note"]) != result["revision_note_hash"]:
        _fail("revision_note_mismatch")
    return result


@dataclass(frozen=True, slots=True)
class ReworkScope:
    """Expected host-resolved identity; the transaction must re-read it."""

    project_id: str
    order_id: str
    order_task_id: str
    delegation_run_id: str
    review_node_id: str
    review_task_id: str
    review_work_item_id: str
    target_node_id: str
    target_task_id: str
    target_work_item_id: str
    checkpoint_id: str

    def validate(self) -> None:
        if type(self) is not ReworkScope:
            _fail()
        for key in _SCOPE_KEYS:
            _id(getattr(self, key))
        if (self.review_node_id == self.target_node_id
                or self.review_task_id == self.target_task_id
                or self.review_work_item_id == self.target_work_item_id):
            _fail("rework_target_invalid")


def build_pending_record(*, scope: ReworkScope, expected_attempt: int,
                         previous_employee_run_id: str, reviewed_artifact_id: str,
                         reviewed_artifact_version: int, reviewed_content_hash: str,
                         revision_note: str, source_refusal_fact_id: str,
                         source_review_attempt: int, source_review_epoch: int) -> dict[str, Any]:
    """Prepare a record only AFTER the host has retained the reviewed package.

    Must be persisted alongside the existing controller refusal fact and FAILED
    transition. Callers may not synthesize missing material from a button click
    or retrospectively label the latest mutable artifact as the reviewed one.
    """
    scope.validate()
    _integer(expected_attempt)
    _integer(source_review_attempt)
    _integer(source_review_epoch)
    if type(previous_employee_run_id) is not str or _RUN.fullmatch(previous_employee_run_id) is None:
        _fail()
    _id(reviewed_artifact_id)
    _integer(reviewed_artifact_version)
    _sha(reviewed_content_hash)
    _id(source_refusal_fact_id)
    note = normalize_revision_note(revision_note)
    identity = {key: getattr(scope, key) for key in _SCOPE_KEYS}
    identity.update(expected_attempt=expected_attempt, previous_employee_run_id=previous_employee_run_id,
                    source_refusal_fact_id=source_refusal_fact_id,
                    source_review_attempt=source_review_attempt, source_review_epoch=source_review_epoch,
                    reviewed_artifact_id=reviewed_artifact_id,
                    reviewed_artifact_version=reviewed_artifact_version,
                    reviewed_content_hash=reviewed_content_hash,
                    revision_note_hash=revision_note_sha256(note))
    digest = hashlib.sha256(_canonical(identity)).hexdigest()
    return {**identity, "record_id": "rr-" + digest[:16], "record_identity_sha256": digest,
            "schema_version": 1, "dispatched": False, "revision_note": note}


def _record_identity(record: dict) -> str:
    required = set(_SCOPE_KEYS) | {"expected_attempt", "previous_employee_run_id",
        "source_refusal_fact_id", "source_review_attempt", "source_review_epoch",
        "reviewed_artifact_id", "reviewed_artifact_version", "reviewed_content_hash", "revision_note_hash"}
    if not required <= record.keys():
        _fail("rework_source_unavailable")
    return hashlib.sha256(_canonical({key: record[key] for key in required})).hexdigest()


@dataclass(frozen=True, slots=True)
class PreparedHumanRework:
    """Bound values for the named Store operation, never a standalone permit."""

    request_id: str
    record_id: str
    payload_sha256: str
    previous_employee_run_id: str
    previous_attempt: int
    note: str
    note_hash: str
    already_applied: bool


def prepare_confirmation(*, request: Any, record: Any, scope: ReworkScope,
                         material: dict[str, Any], current_employee_run_id: str,
                         current_employee_attempt: int, current_review_attempt: int,
                         current_review_epoch: int, rework_count: int,
                         max_rework: int, receipt: dict[str, Any] | None = None) -> PreparedHumanRework:
    """Pure preflight; run again against same-transaction persisted inputs.

    Current owner, order status, Stop, reviewer retirement and money are NOT
    expressible as a caller-provided `allowed=True`; those are host checks.
    A receipt is accepted only as a Store-loaded append-only confirmation.
    """
    scope.validate()
    request = validate_confirm_request(request)
    if type(record) is not dict or type(material) is not dict or set(material) != set(_MATERIAL_KEYS):
        _fail("rework_source_unavailable")
    _canonical(record)
    _canonical(material)
    if (record.get("schema_version") != 1 or type(record.get("schema_version")) is not int
            or type(record.get("dispatched")) is not bool):
        _fail("rework_source_unavailable")
    for key in ("expected_attempt", "source_review_attempt", "source_review_epoch", "reviewed_artifact_version"):
        _integer(record.get(key))
    for key in ("reviewed_content_hash", "revision_note_hash", "record_identity_sha256"):
        _sha(record.get(key))
    _id(record.get("reviewed_artifact_id"))
    _id(record.get("source_refusal_fact_id"))
    if (type(record.get("previous_employee_run_id")) is not str
            or _RUN.fullmatch(record["previous_employee_run_id"]) is None):
        _fail("rework_source_unavailable")
    _id(material["reviewed_artifact_id"])
    _integer(material["reviewed_artifact_version"])
    _sha(material["reviewed_content_hash"])
    if any(record.get(key) != getattr(scope, key) or type(record.get(key)) is not str for key in _SCOPE_KEYS):
        _fail("rework_stale")
    identity = _record_identity(record)
    if record.get("record_identity_sha256") != identity or record.get("record_id") != "rr-" + identity[:16]:
        _fail("rework_source_unavailable")
    if (request["project_id"] != scope.project_id or request["order_id"] != scope.order_id
            or request["task_id"] != scope.order_task_id or request["review_node_id"] != scope.review_node_id
            or request["rework_record_id"] != record["record_id"]
            or request.get("target_node_id", scope.target_node_id) != scope.target_node_id):
        _fail("rework_stale")
    for key in _MATERIAL_KEYS:
        if request[key] != record.get(key) or material[key] != record.get(key):
            _fail("rework_stale")
    if (request["revision_note_hash"] != record.get("revision_note_hash")
            or request["revision_note"] != record.get("revision_note")):
        _fail("revision_note_mismatch")
    normalized = {**request, "target_node_id": scope.target_node_id}
    digest = hashlib.sha256(_canonical(normalized)).hexdigest()
    if receipt is not None:
        if type(receipt) is not dict:
            _fail("rework_source_unavailable")
        if receipt.get("request_id") == request["request_id"]:
            if receipt.get("payload_sha256") != digest:
                _fail("request_id_reused")
            if (receipt.get("record_id") != record["record_id"] or receipt.get("project_id") != scope.project_id
                    or receipt.get("order_id") != scope.order_id or record["dispatched"] is not True):
                _fail("rework_source_unavailable")
            return PreparedHumanRework(request["request_id"], record["record_id"], digest,
                record["previous_employee_run_id"], record["expected_attempt"],
                request["revision_note"], request["revision_note_hash"], True)
        _fail("rework_stale")
    if record["dispatched"]:
        _fail("rework_stale")
    for value in (current_employee_attempt, current_review_attempt, current_review_epoch):
        _integer(value)
    _integer(max_rework, zero=True)
    _integer(rework_count, zero=True)
    if (current_employee_attempt != record["expected_attempt"]
            or request["expected_attempt"] != current_employee_attempt
            or current_employee_run_id != record["previous_employee_run_id"]
            or current_review_attempt != record["source_review_attempt"]
            or current_review_epoch != record["source_review_epoch"]):
        _fail("rework_stale")
    if rework_count >= max_rework:
        _fail("rework_limit_reached")
    return PreparedHumanRework(request["request_id"], record["record_id"], digest,
        current_employee_run_id, current_employee_attempt, request["revision_note"],
        request["revision_note_hash"], False)


@dataclass(frozen=True, slots=True)
class ReworkGraphItem:
    projection_id: str
    work_item_id: str
    task_id: str
    node_id: str
    dependency_projection_ids: tuple[str, ...]
    phase: str
    attempt_seq: int
    attempt_settled: bool
    last_transition_reason: str


@dataclass(frozen=True, slots=True)
class ReworkBranch:
    target: ReworkGraphItem
    review: ReworkGraphItem
    reset: tuple[ReworkGraphItem, ...]
    untouched_not_started: tuple[ReworkGraphItem, ...]
    all_items: tuple[ReworkGraphItem, ...]


def resolve_rework_branch(items: tuple[ReworkGraphItem, ...], *, target_node_id: str,
                          review_node_id: str) -> ReworkBranch:
    """Resolve the exact frozen DAG branch; duplicate scope keys are errors.

    Hosts construct each item from joined durable WorkItem/runtime-link/Task
    rows, never by first-id-wins matching or client projection names. A parked
    unstarted descendant may be READY or WAITING_DEPENDENCIES with attempt 0:
    the original refusal's dependency-frontier refresh moves READY to the
    latter. Preserve either state; neither is an executed attempt to reset.
    """
    if type(items) is not tuple or not 2 <= len(items) <= MAX_NODES:
        _fail("rework_graph_invalid")
    _id(target_node_id)
    _id(review_node_id)
    by_projection: dict[str, ReworkGraphItem] = {}
    by_node: dict[str, ReworkGraphItem] = {}
    work_ids: set[str] = set()
    task_ids: set[str] = set()
    edges = 0
    for item in items:
        if type(item) is not ReworkGraphItem:
            _fail("rework_graph_invalid")
        for value in (item.projection_id, item.work_item_id, item.task_id):
            _id(value)
        if type(item.node_id) is not str:
            _fail("rework_graph_invalid")
        if item.node_id:
            _id(item.node_id)
        if (item.projection_id in by_projection or item.node_id and item.node_id in by_node
                or item.work_item_id in work_ids or item.task_id in task_ids
                or type(item.dependency_projection_ids) is not tuple
                or type(item.phase) is not str or type(item.attempt_settled) is not bool
                or type(item.last_transition_reason) is not str):
            _fail("rework_graph_invalid")
        _integer(item.attempt_seq, zero=True)
        for dependency in item.dependency_projection_ids:
            _id(dependency)
        if len(set(item.dependency_projection_ids)) != len(item.dependency_projection_ids):
            _fail("rework_graph_invalid")
        edges += len(item.dependency_projection_ids)
        if edges > MAX_EDGES:
            _fail("rework_graph_invalid")
        by_projection[item.projection_id] = item
        if item.node_id:
            by_node[item.node_id] = item
        work_ids.add(item.work_item_id)
        task_ids.add(item.task_id)
    target, review = by_node.get(target_node_id), by_node.get(review_node_id)
    if target is None or review is None or target is review:
        _fail("rework_target_invalid")
    dependents: dict[str, list[str]] = {key: [] for key in by_projection}
    incoming = {key: len(value.dependency_projection_ids) for key, value in by_projection.items()}
    for item in items:
        for dependency in item.dependency_projection_ids:
            if dependency not in by_projection or dependency == item.projection_id:
                _fail("rework_graph_invalid")
            dependents[dependency].append(item.projection_id)
    ready = sorted(key for key, count in incoming.items() if count == 0)
    order: list[str] = []
    while ready:
        key = ready.pop(0)
        order.append(key)
        for dependent in sorted(dependents[key]):
            incoming[dependent] -= 1
            if incoming[dependent] == 0:
                ready.append(dependent)
    if len(order) != len(items):
        _fail("rework_graph_invalid")
    affected = {target.projection_id}
    pending = [target.projection_id]
    while pending:
        for dependent in dependents[pending.pop()]:
            if dependent not in affected:
                affected.add(dependent)
                pending.append(dependent)
    if review.projection_id not in affected:
        _fail("rework_target_not_upstream")
    if (review.phase != "failed" or review.last_transition_reason != "human_review_rejected"
            or review.attempt_seq < 1 or not review.attempt_settled):
        _fail("rework_stale")
    reset, untouched = [], []
    for projection in order:
        if projection not in affected:
            continue
        item = by_projection[projection]
        if item.phase in {"ready", "waiting_dependencies"} and item.attempt_seq == 0 and not item.attempt_settled:
            if item is target or item is review:
                _fail("rework_stale")
            untouched.append(item)
        elif item.attempt_seq > 0 and item.attempt_settled and (
            item.phase in {"approved", "awaiting_human", "awaiting_manager_review"}
            or item is review and item.phase == "failed"
        ):
            reset.append(item)
        else:
            # In-flight work, Stop/cancelled, another human failure or unknown
            # state cannot be swept up by one person's review confirmation.
            _fail("rework_branch_busy_or_changed")
    return ReworkBranch(target, review, tuple(reset), tuple(untouched), items)


def allows_failed_review_reopen(*, operation: str, work_item_id: str,
                               expected_work_item_id: str, source_work_item_id: str,
                               previous_phase: str, target_phase: str,
                               expected_phases: tuple[str, ...], transition_reason: str,
                               prepared: PreparedHumanRework) -> bool:
    """Narrow additional Store exception after same-transaction revalidation.

    This MUST NOT replace the existing executive OR-1 branch. It only permits
    the original human-review source to move FAILED -> READY_FOR_REWORK after
    explicit confirmation; it never marks a human rejection APPROVED.
    """
    return bool(type(prepared) is PreparedHumanRework and not prepared.already_applied
        and operation == OPERATION and work_item_id == expected_work_item_id == source_work_item_id
        and previous_phase == "failed" and target_phase == "ready_for_rework"
        and expected_phases == ("failed",) and transition_reason == "human_review_rejected")
