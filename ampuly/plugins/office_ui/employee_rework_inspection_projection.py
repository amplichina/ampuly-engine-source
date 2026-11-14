"""M4 metadata observation only: no confirmation, authorization, or dispatch.

The host binds all scope identities before calling this pure leaf. Only the
human_review_rework_to record is publicly projected; gate request metadata is
private reread material, never a fallback source. Completeness and digests are
not approval. Nothing reads files/DB/tools/clock or changes runtime state.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .ai_employee_contract import AIEmployeeContractError, canonical_json, sha256_json

INSPECT_ACTION = "human_review_rework_inspect"
MAX_PUBLIC_INTEGER = 2**53 - 1
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,256}\Z", re.ASCII)
_SHA = re.compile(r"[a-f0-9]{64}\Z", re.ASCII)
_ID_FIELDS = ("record_id", "checkpoint_id", "review_node_id", "node_id", "request_id")
_HASH_FIELDS = ("payload_sha256", "revision_note_hash")
_PRODUCER_GAPS = (
    "authoritative_attempt_source_unavailable",
    "permanent_request_mapping_unavailable",
)


def _fail(code: str) -> None:
    raise AIEmployeeContractError(code, "返工观察输入无效；未回显原记录或内部数据。") from None


def _id(value: Any) -> bool:
    return type(value) is str and _ID.fullmatch(value) is not None


def _sha(value: Any) -> bool:
    return type(value) is str and _SHA.fullmatch(value) is not None


def _integer(value: Any) -> bool:
    return type(value) is int and 1 <= value <= MAX_PUBLIC_INTEGER


def _records_copy(human_review_rework_to: dict | None,
                  gate_harness_rework_request: dict | None) -> tuple[dict, bytes]:
    if any(value is not None and type(value) is not dict
           for value in (human_review_rework_to, gate_harness_rework_request)):
        _fail("employee_rework_observation_invalid")
    try:
        # One combined ceiling: <=2 MiB / depth64 / 50k nodes, not two separate
        # allowances. JSON copy is detached, strict, and never coerces objects.
        raw = canonical_json({"human_review_rework_to": human_review_rework_to,
                              "gate_harness_rework_request": gate_harness_rework_request})
        return json.loads(raw), raw
    except Exception:
        _fail("employee_rework_observation_invalid")


def private_rework_records_digest(*, human_review_rework_to: dict | None,
                                  gate_harness_rework_request: dict | None) -> str:
    """Internal reread digest only; MUST NOT be included in any public/RPC DTO.

    This includes full private records but does not authenticate their scope,
    persistence, producer, or effect. The host must retain those bindings.
    """
    _, raw = _records_copy(human_review_rework_to, gate_harness_rework_request)
    return hashlib.sha256(raw).hexdigest()


def _field(record: dict, name: str, validator: Any, gaps: list[str]) -> Any:
    if name not in record:
        gaps.append(name + "_missing")
        return None
    value = record[name]
    if not validator(value):
        gaps.append(name + "_invalid")
        return None
    return value


def project_rework_observation(*, project_id: str, order_id: str, task_id: str,
                               order_run_task_id: str,
                               human_review_rework_to: dict | None,
                               gate_harness_rework_request: dict | None) -> dict:
    """Project safe observations from host-selected metadata, granting nothing.

    order_run_task_id is order.launched_task_id, NOT EmployeeRun/DelegationRun.
    node_id is a rework target node, not task_id. No identity is inferred from
    metadata. A recorded dispatched=True does not prove successful dispatch.
    """
    if not all(_id(value) for value in (project_id, order_id, task_id, order_run_task_id)):
        _fail("employee_rework_observation_scope_invalid")
    records, _ = _records_copy(human_review_rework_to, gate_harness_rework_request)
    record = records["human_review_rework_to"]
    gaps = list(_PRODUCER_GAPS)
    result = {
        "project_id": project_id, "order_id": order_id, "task_id": task_id,
        "order_run_task_id": order_run_task_id,
        **{name: None for name in _ID_FIELDS + _HASH_FIELDS},
        "record_attempt": None, "reviewed_artifact": None,
        "dispatch_observation": "no_record" if record is None else "unknown",
        "missing_evidence": gaps, "allowed_actions": [INSPECT_ACTION],
        "confirm_available": False,
    }
    if record is None:
        gaps.append("rework_record_missing")
    else:
        for name in _ID_FIELDS:
            result[name] = _field(record, name, _id, gaps)
        for name in _HASH_FIELDS:
            result[name] = _field(record, name, _sha, gaps)
        result["record_attempt"] = _field(record, "attempt", _integer, gaps)
        artifact_id = _field(record, "reviewed_artifact_id", _id, gaps)
        version = _field(record, "reviewed_artifact_version", _integer, gaps)
        content_hash = _field(record, "reviewed_content_hash", _sha, gaps)
        if artifact_id is not None and version is not None and content_hash is not None:
            result["reviewed_artifact"] = {
                "artifact_id": artifact_id, "version": version, "content_hash": content_hash,
            }
        if "dispatched" not in record:
            gaps.append("dispatched_missing")
        elif type(record["dispatched"]) is not bool:
            gaps.append("dispatched_invalid")
        else:
            result["dispatch_observation"] = "dispatch_recorded" if record["dispatched"] else "not_dispatched"
    # Only this finite public projection contributes. No raw/private digest,
    # timestamp, person, feedback, or gate metadata affects the public revision.
    result["projection_revision"] = sha256_json(result)
    return result
