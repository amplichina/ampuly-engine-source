"""Pure AE00 4.3 attempt envelopes; internal draft, not a wire or admission API.

Every identity, reference and timestamp is an explicit host input. No current
instance, configuration, clock, identifier generator, file or runtime is read.
The digest binds canonical bytes to the explicit project/order/step/attempt
identity. It proves local structure/integrity, NOT trusted origin, publication,
authorization, payment, human duty, input access or readiness.

AE00 does not fully type all references/empty values. This leaf's narrow internal
conventions are documented in INTEGRATION-NOTES.txt; they do not amend AE00's
external wire contract. Only the executor slot is supported. Progress and
persistence are deliberately absent.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import InitVar, dataclass, fields
from datetime import datetime
from typing import Any

from .ai_employee_contract import AIEmployeeContractError, MAX_MANIFEST_BYTES, canonical_json
from .ai_employee_run_contract import FrozenEmployeePins

MAX_INPUT_REFS = 500  # Parser ceiling, not permission or concurrent work.
_PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:+-]{0,127}\Z")
_RUN = re.compile(r"er-[a-f0-9]{16}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})?\Z")
_ENVELOPE = frozenset({"run_id", "order_id", "order_task_id", "parent_order_id",
                      "workflow_id", "workflow_version", "node_id", "binding_slot",
                      "attempt", "input_refs", "artifact_contract_ref", "budget_id",
                      "pinned_at", "baseline_ref", "rework_of"})
_BASELINE = frozenset({"order_task_id", "org_snapshot_sha256", "manifest_sha256", "pin_source"})
_INPUT = frozenset({"artifact_id", "version", "content_hash", "project_id", "kind"})
_KINDS = frozenset({"order_input", "supplement", "approved_content"})


def _fail(code: str, message: str, path: str = "") -> None:
    raise AIEmployeeContractError(code, message, path)


def _text(value: Any, pattern: re.Pattern, path: str) -> None:
    if type(value) is not str or not pattern.fullmatch(value):
        _fail("employee_run_envelope_invalid", "运行引用必须为明确的有界受控标识，不接受文件路径。", path)


@dataclass(frozen=True, slots=True, repr=False)
class EmployeeRunIdentity:
    """Expected associated identity, not evidence that the host authenticated it.

    project_id and step_task_id are associated identity only: AE00 4.3 does not
    define these additional keys in the employee_run body. order_task_id names
    the order anchor, never the step task. The caller supplies every value.
    """
    project_id: str
    order_id: str
    order_task_id: str
    step_task_id: str
    workflow_id: str
    workflow_version: str
    node_id: str
    binding_slot: str
    attempt: int
    run_id: str

    def __post_init__(self) -> None:
        _text(self.project_id, _PROJECT, "identity.project_id")
        for name in ("order_id", "order_task_id", "step_task_id", "workflow_id", "node_id"):
            _text(getattr(self, name), _REF, "identity." + name)
        _text(self.workflow_version, _VERSION, "identity.workflow_version")
        _text(self.run_id, _RUN, "identity.run_id")
        if type(self.binding_slot) is not str or self.binding_slot != "executor":
            _fail("employee_binding_collaborator_unsupported", "首期只接受 executor 席位。")
        if type(self.attempt) is not int or not 1 <= self.attempt <= 2**63 - 1:
            _fail("employee_run_attempt_invalid", "attempt 必须为正整数，不接受布尔值。")


def _identity(identity: Any) -> dict[str, Any]:
    if type(identity) is not EmployeeRunIdentity:
        _fail("employee_run_scope_invalid", "需要显式的订单及步骤关联身份。")
    # Revalidate even if a caller bypassed frozen dataclass assignment. Nothing
    # in this check authenticates identity values or grants an admission.
    identity.__post_init__()
    return {field.name: getattr(identity, field.name) for field in fields(identity)}


def _digest(raw: bytes, identity: EmployeeRunIdentity) -> str:
    associated = canonical_json(_identity(identity))
    return hashlib.sha256(b"ai-employee-run-envelope:v1\0" + associated + b"\0" + raw).hexdigest()


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("employee_run_envelope_invalid", "运行 JSON 不能含重复键。")
        result[key] = value
    return result


def _decode(raw: Any) -> dict[str, Any]:
    if type(raw) is not bytes or len(raw) > MAX_MANIFEST_BYTES:
        _fail("employee_run_envelope_invalid", "恢复需要有界的原始冻结 bytes。")

    def bad_constant(_: str) -> None:
        _fail("employee_run_envelope_invalid", "运行 JSON 不能含非有限数字。")

    invalid_bytes = False
    try:
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=bad_constant)
        if type(obj) is not dict or canonical_json(obj) != raw:
            _fail("employee_run_envelope_invalid", "恢复必须使用原规范化运行对象字节。")
    except (ValueError, UnicodeError, RecursionError):
        invalid_bytes = True
    if invalid_bytes:
        raise AIEmployeeContractError("employee_run_envelope_invalid", "运行冻结字节结构无效。") from None
    return obj


def _inputs(rows: Any, project_id: str) -> None:
    canonical_json(rows)
    if type(rows) is not list or len(rows) > MAX_INPUT_REFS:
        _fail("employee_run_input_invalid", "受控输入引用必须为有界数组。", "input_refs")
    seen: set[bytes] = set()
    hashes: dict[tuple[str, bytes], str] = {}
    for row in rows:
        if type(row) is not dict or set(row) != _INPUT:
            _fail("employee_run_input_invalid", "输入引用缺字段或包含未声明字段。", "input_refs")
        _text(row["artifact_id"], _REF, "input_refs.artifact_id")
        _text(row["content_hash"], _SHA, "input_refs.content_hash")
        if type(row["project_id"]) is not str or row["project_id"] != project_id:
            _fail("project_scope_denied", "输入引用不属于本次冻结项目。", "input_refs.project_id")
        version = row["version"]
        if type(version) is int:
            if not 1 <= version <= 2**63 - 1:
                _fail("employee_run_input_invalid", "输入版本必须为正整数或明确版本标识。", "input_refs.version")
        elif type(version) is str:
            _text(version, _VERSION, "input_refs.version")
        else:
            _fail("employee_run_input_invalid", "输入版本不接受布尔值、浮点或空值。", "input_refs.version")
        if type(row["kind"]) is not str or row["kind"] not in _KINDS:
            _fail("employee_run_input_invalid", "输入引用类型无效。", "input_refs.kind")
        encoded = canonical_json(row)
        if encoded in seen:
            _fail("employee_run_input_invalid", "输入引用不能重复。", "input_refs")
        seen.add(encoded)
        key = row["artifact_id"], canonical_json(version)
        if key in hashes and hashes[key] != row["content_hash"]:
            _fail("employee_run_input_invalid", "同一输入版本不能指向不同内容。", "input_refs.content_hash")
        hashes[key] = row["content_hash"]
        # Different kinds and explicitly different versions remain distinct;
        # this list is never collapsed into a lossy authorization map.


def _validate(obj: dict[str, Any], identity: EmployeeRunIdentity,
              pins: FrozenEmployeePins, previous: FrozenEmployeeRunEnvelope | None) -> None:
    associated = _identity(identity)
    if type(pins) is not FrozenEmployeePins:
        _fail("employee_run_pin_missing", "必须提供原固定实例席位，不接受当前实例或客户端字典。")
    if pins.project_id != identity.project_id or pins.workflow_id != identity.workflow_id:
        _fail("project_scope_denied", "固定席位与运行项目流程范围不一致。")
    pin = pins.for_node(identity.node_id)  # Reuse the existing strict pin parser.
    if set(obj) != _ENVELOPE | set(pin):
        _fail("employee_run_envelope_invalid", "运行依据缺字段或包含未声明字段。")
    for name in ("run_id", "order_id", "order_task_id", "workflow_id", "workflow_version",
                 "node_id", "binding_slot", "attempt"):
        if canonical_json(obj[name]) != canonical_json(associated[name]):
            _fail("employee_run_scope_invalid", "运行依据与显式关联身份不一致。", name)
    if canonical_json({key: obj[key] for key in pin}) != canonical_json(pin):
        _fail("employee_run_pin_conflict", "运行依据不是该节点的原固定席位。")
    parent = obj["parent_order_id"]
    if type(parent) is not str:
        _fail("employee_run_envelope_invalid", "无父订单的内部表示为明确空字符串。", "parent_order_id")
    if parent:
        _text(parent, _REF, "parent_order_id")
        if parent == identity.order_id:
            _fail("employee_run_scope_invalid", "订单不能作为自身的父订单。", "parent_order_id")
    for name in ("artifact_contract_ref", "budget_id"):
        _text(obj[name], _REF, name)
    stamp = obj["pinned_at"]
    if type(stamp) is not str or not _STAMP.fullmatch(stamp):
        _fail("employee_run_envelope_invalid", "pinned_at 必须为显式 ISO 日期时间。", "pinned_at")
    invalid_stamp = False
    try:
        datetime.fromisoformat(stamp[:-1] + "+00:00" if stamp.endswith("Z") else stamp)
    except ValueError:
        invalid_stamp = True
    if invalid_stamp:
        raise AIEmployeeContractError("employee_run_envelope_invalid", "冻结时间无效。", "pinned_at") from None
    baseline = obj["baseline_ref"]
    if type(baseline) is not dict or set(baseline) != _BASELINE:
        _fail("employee_run_baseline_invalid", "订单基线引用缺字段或包含未声明字段。", "baseline_ref")
    if type(baseline["order_task_id"]) is not str or baseline["order_task_id"] != identity.order_task_id:
        _fail("employee_run_scope_invalid", "基线不属于同一订单任务。", "baseline_ref.order_task_id")
    for name in ("org_snapshot_sha256", "manifest_sha256"):
        _text(baseline[name], _SHA, "baseline_ref." + name)
    if type(baseline["pin_source"]) is not str or baseline["pin_source"] not in {"launch", "explicit_rebase"}:
        # Missing organization provenance remains legacy_unpinned in the host;
        # this complete-envelope leaf must not manufacture a substitute hash.
        _fail("employee_run_baseline_invalid", "需要完整显式基线；旧未冻结依据不由此模块补造。", "baseline_ref.pin_source")
    _inputs(obj["input_refs"], identity.project_id)
    if identity.attempt == 1:
        if type(obj["rework_of"]) is not str or obj["rework_of"] != "" or previous is not None:
            _fail("employee_run_previous_invalid", "首次 attempt 必须明确无前一轮。", "rework_of")
    else:
        if type(previous) is not FrozenEmployeeRunEnvelope:
            _fail("employee_run_previous_invalid", "返修需要已核验的前一轮完整依据。", "rework_of")
        old_identity = _identity(previous.identity)
        current_chain = {key: associated[key] for key in associated if key not in {"attempt", "run_id"}}
        old_chain = {key: old_identity[key] for key in old_identity if key not in {"attempt", "run_id"}}
        if (canonical_json(current_chain) != canonical_json(old_chain)
                or identity.attempt != previous.identity.attempt + 1
                or identity.run_id == previous.identity.run_id
                or type(obj["rework_of"]) is not str or obj["rework_of"] != previous.identity.run_id):
            _fail("employee_run_previous_invalid", "返修身份或 attempt 与前一轮不匹配。", "rework_of")
        if canonical_json(parent) != canonical_json(previous.to_dict()["parent_order_id"]):
            _fail("employee_run_previous_invalid", "同一返修链不能静默更换父订单。", "parent_order_id")


@dataclass(frozen=True, slots=True, repr=False)
class FrozenEmployeeRunEnvelope:
    identity: EmployeeRunIdentity
    _pins: FrozenEmployeePins
    _raw: bytes
    fingerprint: str
    previous: InitVar[FrozenEmployeeRunEnvelope | None] = None

    def __post_init__(self, previous: FrozenEmployeeRunEnvelope | None) -> None:
        _text(self.fingerprint, _SHA, "envelope_fingerprint")
        obj = _decode(self._raw)
        _validate(obj, self.identity, self._pins, previous)
        if _digest(self._raw, self.identity) != self.fingerprint:
            _fail("fingerprint_conflict", "运行字节或关联身份与可信保留摘要不一致。")

    @property
    def validation_scope(self) -> str:
        return "structure_and_integrity_only"

    def to_bytes(self) -> bytes:
        return self._raw

    def to_dict(self) -> dict[str, Any]:
        return _decode(self._raw)  # Fresh tree; no caller can edit retained bytes.


def build_employee_run_envelope(identity: EmployeeRunIdentity, pins: FrozenEmployeePins, *,
                                parent_order_id: str, input_refs: list[dict[str, Any]],
                                artifact_contract_ref: str, budget_id: str, pinned_at: str,
                                baseline_ref: dict[str, Any], rework_of: str,
                                previous: FrozenEmployeeRunEnvelope | None = None) -> FrozenEmployeeRunEnvelope:
    """Build from explicit already-frozen host inputs; do not resolve or admit.

    New attempts may carry explicit new inputs/refs/pins only because the caller
    supplies them. This is not authorization for rebasing an order or replacing
    its immutable baseline. The host must enforce those decisions separately.
    """
    associated = _identity(identity)
    if type(pins) is not FrozenEmployeePins:
        _fail("employee_run_pin_missing", "缺少原固定实例席位。")
    # Scope before node lookup: a foreign snapshot's node membership must not
    # affect the refusal or leak through binding_unresolved.
    if pins.project_id != identity.project_id or pins.workflow_id != identity.workflow_id:
        _fail("project_scope_denied", "固定席位与运行项目流程范围不一致。")
    pin = pins.for_node(identity.node_id)
    obj = {key: associated[key] for key in ("run_id", "order_id", "order_task_id",
        "workflow_id", "workflow_version", "node_id", "binding_slot", "attempt")}
    obj.update({"parent_order_id": parent_order_id, "input_refs": input_refs,
                "artifact_contract_ref": artifact_contract_ref, "budget_id": budget_id,
                "pinned_at": pinned_at, "baseline_ref": baseline_ref, "rework_of": rework_of})
    obj.update(pin)
    raw = canonical_json(obj)
    return FrozenEmployeeRunEnvelope(identity, pins, raw, _digest(raw, identity), previous)


def restore_employee_run_envelope(raw: bytes, *, identity: EmployeeRunIdentity,
                                  pins: FrozenEmployeePins, expected_fingerprint: str,
                                  previous: FrozenEmployeeRunEnvelope | None = None) -> FrozenEmployeeRunEnvelope:
    """Restore original bytes+expected identity+old pins+independently retained digest.

    No current resolver, timestamp or package is consulted. The caller owns the
    provenance of expected_fingerprint, identity, pins and previous. Supplying a
    digest recomputed from the same bytes is not source authentication.
    """
    return FrozenEmployeeRunEnvelope(identity, pins, raw, expected_fingerprint, previous)
