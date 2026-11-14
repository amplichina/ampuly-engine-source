"""Pure AE00 employee-pin snapshots, not launch authorization or run storage.

Inputs must be supplied by the server's already-resolved launch adapter. Checking
their key/source/identity consistency does not authenticate their origin. This
module neither resolves an instance nor loads current packages, grants, config,
human status, money usage or files. Restore consumes only the original canonical
bytes and an independently retained, scope-bound snapshot fingerprint.

This module implements only employee_pins[node_id]. The companion
ai_employee_run_envelope module adds a pure per-attempt identity envelope;
neither module implements progress, persistence or runtime authorization.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .ai_employee_contract import (
    AIEmployeeContractError, MAX_MANIFEST_BYTES, canonical_json,
    validate_package_manifest,
)
from .ai_employee_instance_contract import (
    _at_least, effective_limits, validate_binding, validate_instance,
)

MAX_PINS = 500  # A bounded parser ceiling, not a grant or runtime concurrency limit.
_PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_INSTANCE = re.compile(r"ei-[a-f0-9]{16}\Z")
_PACKAGE = re.compile(r"[a-z0-9][a-z0-9_-]{2,63}\Z")
_SKILL = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_MONEY = re.compile(r"(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,6})?\Z")
_SLOTS = frozenset({"submitter", "rule_confirmer", "reviewer", "approver",
                    "escalation", "backup", "delivery_authorizer"})
_LIMIT_REQUIRED = frozenset({"max_steps", "max_model_calls", "max_wall_clock_s",
                             "max_rework", "max_retries"})
_PIN_REQUIRED = frozenset({"instance_id", "instance_revision", "revision_fingerprint",
                          "package_id", "package_version", "package_fingerprint",
                          "effective_limits"})
_PIN_OPTIONAL = frozenset({"skill_tree_sha256", "knowledge_versions",
                          "human_assignments", "reviewer_slot", "collaborator_pins",
                          "binding_revision",
                          # C wiring: composition digests + live-recheck capabilities
                          "composition_id", "effective_config_digest",
                          "composition_rules_version", "composition_capabilities",
                          "composition_disabled_actions"})


def _fail(code: str, message: str, path: str = "") -> None:
    raise AIEmployeeContractError(code, message, path)


def _text(value: Any, path: str, pattern: re.Pattern[str] | None = None) -> None:
    if (type(value) is not str or not value.strip() or len(value) > 256
            or pattern is not None and not pattern.fullmatch(value)):
        _fail("employee_run_pin_invalid", "冻结引用格式无效。", path)


def _scope(project_id: Any, workflow_id: Any) -> None:
    if (type(project_id) is not str or not _PROJECT.fullmatch(project_id)
            or type(workflow_id) is not str or not _REF.fullmatch(workflow_id)):
        _fail("project_scope_denied", "冻结项目与流程范围无效。")


def _problems(problems: list[dict[str, Any]]) -> None:
    errors = [item for item in problems if item.get("severity") == "error"]
    if errors:
        first = errors[0]
        _fail(first["code"], "冻结输入未满足结构合同。", first.get("path", ""))


def _validate_pin(pin: Any, path: str) -> None:
    if type(pin) is not dict or not _PIN_REQUIRED <= pin.keys() or pin.keys() - (_PIN_REQUIRED | _PIN_OPTIONAL):
        _fail("employee_run_pin_invalid", "冻结席位缺少字段或含未声明字段。", path)
    for name, pattern in (("instance_id", _INSTANCE), ("revision_fingerprint", _SHA),
                          ("package_id", _PACKAGE), ("package_fingerprint", _SHA)):
        _text(pin[name], f"{path}.{name}", pattern)
    _text(pin["package_version"], f"{path}.package_version")
    if type(pin["instance_revision"]) is not int or not 1 <= pin["instance_revision"] <= 2**63 - 1:
        _fail("employee_run_pin_invalid", "实例修订必须为正整数，不接受布尔值。", path)
    limits = pin["effective_limits"]
    effective_limits(limits)  # Reuse strict integer/decimal validation; no enforcement.
    if not _LIMIT_REQUIRED <= limits.keys():
        _fail("employee_run_pin_invalid", "缺少必填有效上限，不能解释为无限。", path)
    for name, key_pattern, value_pattern in (("skill_tree_sha256", _SKILL, _SHA),
                                             ("knowledge_versions", _REF, None)):
        if name not in pin:
            continue
        rows = pin[name]
        if type(rows) is not dict or len(rows) > 500:
            _fail("employee_run_pin_invalid", "冻结引用映射超出范围。", f"{path}.{name}")
        for key, value in rows.items():
            _text(key, f"{path}.{name}", key_pattern)
            _text(value, f"{path}.{name}.{key}", value_pattern)
    assignments = pin.get("human_assignments", {})
    if type(assignments) is not dict or assignments.keys() - _SLOTS:
        _fail("employee_run_pin_invalid", "冻结人员槽位无效。", path)
    for slot, employee_id in assignments.items():
        _text(employee_id, f"{path}.human_assignments.{slot}", _REF)
    if "reviewer_slot" in pin:
        slot = pin["reviewer_slot"]
        if type(slot) is not str or slot not in _SLOTS or slot not in assignments:
            _fail("employee_binding_reviewer_missing", "没有固定的审核槽位人员。", path)
    if "collaborator_pins" in pin and (type(pin["collaborator_pins"]) is not dict or pin["collaborator_pins"]):
        _fail("employee_binding_collaborator_unsupported", "首期不支持协作者冻结。", path)
    for digest_name in ("composition_id", "effective_config_digest"):
        if digest_name in pin:
            _text(pin[digest_name], f"{path}.{digest_name}", _SHA)
    if "composition_rules_version" in pin:
        ver = pin["composition_rules_version"]
        if type(ver) is not str or not ver.strip() or len(ver) > 64:
            _fail("employee_run_pin_invalid", "组合规则版本无效。", f"{path}.composition_rules_version")
    if "composition_capabilities" in pin:
        caps = pin["composition_capabilities"]
        if type(caps) is not dict or len(caps) > 500:
            _fail("employee_run_pin_invalid", "组合能力映射无效。", f"{path}.composition_capabilities")
        for tool, actions in caps.items():
            _text(tool, f"{path}.composition_capabilities", _REF)
            if type(actions) is not list or len(actions) > 64:
                _fail("employee_run_pin_invalid", "组合能力动作列表无效。", f"{path}.composition_capabilities.{tool}")
            for action in actions:
                _text(action, f"{path}.composition_capabilities.{tool}", _REF)
    if "composition_disabled_actions" in pin:
        disabled = pin["composition_disabled_actions"]
        if type(disabled) is not list or len(disabled) > 500:
            _fail("employee_run_pin_invalid", "组合禁用动作列表无效。", f"{path}.composition_disabled_actions")
        for item in disabled:
            _text(item, f"{path}.composition_disabled_actions", _REF)


def _validate_pins(pins: Any) -> None:
    canonical_json(pins)
    if type(pins) is not dict or len(pins) > MAX_PINS:
        _fail("employee_run_pin_invalid", "节点冻结映射超出范围。")
    for node_id, pin in pins.items():
        _text(node_id, "employee_pins", _REF)
        _validate_pin(pin, f"employee_pins.{node_id}")


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("employee_run_pin_invalid", "冻结 JSON 不能含重复键。")
        result[key] = value
    return result


def _decode(raw: Any) -> dict[str, Any]:
    if type(raw) is not bytes or len(raw) > MAX_MANIFEST_BYTES:
        _fail("employee_run_pin_invalid", "冻结字节必须为有界 bytes。")
    def bad_constant(_: str) -> None:
        _fail("employee_run_pin_invalid", "冻结 JSON 不能含非有限数字。")
    try:
        pins = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=bad_constant)
        _validate_pins(pins)
        if canonical_json(pins) != raw:
            _fail("employee_run_pin_invalid", "恢复必须使用原规范化冻结字节。")
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise AIEmployeeContractError("employee_run_pin_invalid", "冻结字节结构无效。") from exc
    return pins


def _digest(raw: bytes, project_id: str, workflow_id: str) -> str:
    # This is a local integrity digest with associated scope, NOT an authority
    # signature or proof that an instance/package was actually read by the host.
    scope = canonical_json({"project_id": project_id, "workflow_id": workflow_id})
    return hashlib.sha256(b"ai-employee-pins:v1\0" + scope + b"\0" + raw).hexdigest()


@dataclass(frozen=True, slots=True)
class FrozenEmployeePins:
    project_id: str
    workflow_id: str
    _raw: bytes
    fingerprint: str

    def __post_init__(self) -> None:
        _scope(self.project_id, self.workflow_id)
        _decode(self._raw)
        _text(self.fingerprint, "snapshot_fingerprint", _SHA)
        if _digest(self._raw, self.project_id, self.workflow_id) != self.fingerprint:
            _fail("fingerprint_conflict", "冻结字节或范围与保留摘要不一致。")

    @property
    def validation_scope(self) -> str:
        return "structure_and_integrity_only"

    def to_bytes(self) -> bytes:
        return self._raw

    def to_dict(self) -> dict[str, Any]:
        return _decode(self._raw)  # A fresh detached tree; no mutable cached JSON.

    def for_node(self, node_id: str) -> dict[str, Any]:
        _text(node_id, "node_id", _REF)
        rows = self.to_dict()
        if node_id not in rows:
            _fail("binding_unresolved", "该节点没有本次冻结绑定。")
        return rows[node_id]


@dataclass(frozen=True, slots=True)
class ResolvedPinInput:
    """Convenience carrier only; constructing it does NOT create trusted origin."""
    node_id: str
    resolution: dict[str, Any]
    binding: dict[str, Any]
    package_manifest: dict[str, Any]
    requires_review: bool = False
    # Canvas/mapping revision observed at freeze time. Kept off binding/resolution
    # dicts so validate_binding / resolution shape stay strict.
    binding_revision: int | None = None
    # Optional C-package composition record assembled by the host at freeze.
    # Only digests + capability map are copied onto the run pin.
    composition: dict[str, Any] | None = None


def build_employee_pin(resolution: Any, binding: Any, package_manifest: Any, *,
                       project_id: str, workflow_id: str, node_id: str,
                       platform_limits: dict[str, Any], order_limits: dict[str, Any],
                       requires_review: bool = False,
                       binding_revision: int | None = None,
                       composition: dict[str, Any] | None = None) -> dict[str, Any]:
    """Check one ALREADY resolved fixed revision; do not select or authorize it.

    The host must independently verify immutable package bytes/fingerprint,
    full Skill tree observations, readiness, ownership, exclusive node roles
    and current grants before launch. A declared tree hash is not an observation.
    Draft synthetic manifests are structurally checkable, never launch-approved.
    """
    _scope(project_id, workflow_id)
    _text(node_id, "node_id", _REF)
    canonical_json(resolution)
    if type(resolution) is not dict or set(resolution) != {"instance", "source", "key"}:
        _fail("binding_unresolved", "需要服务端已解析的固定席位结构。")
    key = {"project_id": project_id, "workflow_id": workflow_id,
           "node_id": node_id, "binding_slot": "executor"}
    if type(resolution["key"]) is not dict or resolution["key"] != key:
        _fail("project_scope_denied", "固定绑定不在本项目流程节点席位。")
    if type(resolution["source"]) is not str or resolution["source"] not in {"human_choice", "single_candidate"}:
        _fail("binding_unresolved", "已解析来源标识无效。")
    instance = resolution["instance"]
    if type(instance) is not dict or instance.get("project_id") != project_id:
        _fail("project_scope_denied", "固定实例不在本项目。")
    if type(requires_review) is not bool:
        _fail("employee_run_pin_invalid", "审核节点标识必须为布尔值。")
    package_problems = validate_package_manifest(package_manifest, require_complete=False)
    _problems(package_problems)
    # Incomplete evaluation evidence may remain a warning in a synthetic draft,
    # but the exact fields used to construct pins cannot be absent. In particular
    # a platform cap cannot silently fill an unknown package cap.
    pin_fields = {"schema_version", "package_id", "version", "package_fingerprint",
                  "human_slots", "loop_policy"}
    for problem in package_problems:
        prefix = problem.get("path", "").split(".", 1)[0].split("[", 1)[0]
        if problem.get("code") == "employee_package_required" and prefix in pin_fields:
            _fail("employee_run_pin_invalid", "固定包缺少冻结所需字段。", problem["path"])
    _problems(validate_instance(instance, package_manifest, expected_project_id=project_id))
    _problems(validate_binding(binding, package_manifest, requires_review=requires_review))
    constraint = binding["version_constraint"]
    mode = constraint["mode"]
    if (mode == "exact" and instance["package_version"] != constraint["version"]
            or mode == "at_least" and not _at_least(instance["package_version"], constraint["version"])):
        _fail("employee_binding_version_unavailable", "固定实例版本不满足显式绑定。")
    assignments = {slot: row["employee_id"] for slot, row in instance.get("human_assignments", {}).items()}
    for row in package_manifest.get("human_slots", []):
        if row.get("required") is True and row["slot"] not in assignments:
            _fail("human_slot_unfilled", "固定实例缺少包要求的人员槽位。")
    knowledge: dict[str, str] = {}
    # AE00 stores only ref->version here. kind/access_scope remain bound by the
    # full instance revision_fingerprint; this lossy map is NOT an access grant.
    for row in instance.get("knowledge_refs", []):
        ref, version = row["ref"], row["version"]
        if ref in knowledge and knowledge[ref] != version:
            _fail("employee_run_pin_invalid", "同一知识引用不能冻结为多个版本。")
        knowledge[ref] = version
    pin = {name: instance[name] for name in ("instance_id", "instance_revision", "revision_fingerprint",
                                            "package_id", "package_version", "package_fingerprint")}
    pin.update({"skill_tree_sha256": {row["skill_name"]: row["tree_fingerprint"] for row in package_manifest.get("skills", [])},
                "knowledge_versions": knowledge, "human_assignments": assignments,
                "effective_limits": effective_limits(platform_limits, package_manifest["loop_policy"],
                                                     instance.get("limits", {}), order_limits),
                "collaborator_pins": {}})
    if "reviewer_slot" in binding:
        pin["reviewer_slot"] = binding["reviewer_slot"]
    # Optional canvas/mapping revision at freeze time. Hosts pass it via
    # ResolvedPinInput.binding_revision (not via binding/resolution dicts).
    if binding_revision is not None:
        if (type(binding_revision) is not int or isinstance(binding_revision, bool)
                or not 0 <= binding_revision <= 2**53 - 1):
            _fail("employee_run_pin_invalid", "绑定修订必须为非负安全整数。")
        pin["binding_revision"] = binding_revision
    if composition is not None:
        if type(composition) is not dict:
            _fail("employee_run_pin_invalid", "组合记录必须为对象。")
        if type(composition.get("composition_id")) is not str or not _SHA.fullmatch(composition["composition_id"]):
            _fail("employee_run_pin_invalid", "组合标识无效。")
        if type(composition.get("effective_config_digest")) is not str or not _SHA.fullmatch(composition["effective_config_digest"]):
            _fail("employee_run_pin_invalid", "组合配置摘要无效。")
        rules = composition.get("rules_version")
        if type(rules) is not str or not rules.strip() or len(rules) > 64:
            _fail("employee_run_pin_invalid", "组合规则版本无效。")
        effective = composition.get("effective")
        if type(effective) is not dict or type(effective.get("capabilities")) is not dict:
            _fail("employee_run_pin_invalid", "组合缺少有效能力映射。")
        caps = effective["capabilities"]
        for tool, actions in caps.items():
            if type(tool) is not str or type(actions) is not list or any(type(a) is not str for a in actions):
                _fail("employee_run_pin_invalid", "组合能力映射格式无效。")
        disabled = composition.get("disabled_actions", [])
        if type(disabled) is not list or any(type(x) is not str for x in disabled):
            _fail("employee_run_pin_invalid", "组合禁用动作格式无效。")
        pin["composition_id"] = composition["composition_id"]
        pin["effective_config_digest"] = composition["effective_config_digest"]
        pin["composition_rules_version"] = rules
        pin["composition_capabilities"] = {tool: list(actions) for tool, actions in caps.items()}
        pin["composition_disabled_actions"] = list(disabled)
    _validate_pins({node_id: pin})
    return json.loads(canonical_json(pin))


def freeze_employee_pins(resolved_nodes: list[ResolvedPinInput], *, project_id: str,
                         workflow_id: str, expected_bound_node_ids: list[str],
                         platform_limits: dict[str, Any], order_limits: dict[str, Any]) -> FrozenEmployeePins:
    """Freeze exactly the host's explicit bound-node set; never discover bindings."""
    _scope(project_id, workflow_id)
    if type(resolved_nodes) is not list or len(resolved_nodes) > MAX_PINS or type(expected_bound_node_ids) is not list:
        _fail("employee_run_pin_invalid", "冻结节点输入超出范围。")
    canonical_json(expected_bound_node_ids)
    for node_id in expected_bound_node_ids:
        _text(node_id, "expected_bound_node_ids", _REF)
    if len(expected_bound_node_ids) > MAX_PINS or len(set(expected_bound_node_ids)) != len(expected_bound_node_ids):
        _fail("employee_run_pin_invalid", "绑定节点集合超界或重复。")
    # Validate even for an empty node set: missing caps must never be a grant.
    effective_limits(platform_limits, order_limits)
    pins: dict[str, Any] = {}
    for row in resolved_nodes:
        if type(row) is not ResolvedPinInput:
            _fail("employee_run_pin_invalid", "需要明确的已解析节点输入。")
        _text(row.node_id, "node_id", _REF)
        if row.node_id in pins:
            _fail("employee_run_pin_invalid", "同节点不能冻结两个主责。")
        pins[row.node_id] = build_employee_pin(row.resolution, row.binding, row.package_manifest,
            project_id=project_id, workflow_id=workflow_id, node_id=row.node_id,
            platform_limits=platform_limits, order_limits=order_limits,
            requires_review=row.requires_review,
            binding_revision=row.binding_revision,
            composition=row.composition)
    if set(pins) != set(expected_bound_node_ids):
        _fail("binding_unresolved", "已解析节点与本次显式绑定集合不一致。")
    raw = canonical_json(pins)
    return FrozenEmployeePins(project_id, workflow_id, raw, _digest(raw, project_id, workflow_id))


def restore_employee_pins(raw: bytes, *, project_id: str, workflow_id: str,
                          expected_fingerprint: str) -> FrozenEmployeePins:
    """Restore original bytes+scope+retained digest; never re-resolve current data.

    The host owns the retention/provenance of expected_fingerprint and scope.
    A client-supplied hash recomputed from these same bytes is NOT trusted proof.
    """
    return FrozenEmployeePins(project_id, workflow_id, raw, expected_fingerprint)


def pin_for_running_attempt(snapshot: FrozenEmployeePins, node_id: str, *,
                            current_canvas_binding_revision: int | None = None) -> dict[str, Any]:
    """Return the start-time pin for a live attempt.

    ``current_canvas_binding_revision`` is observational only. A newer canvas
    mapping after freeze must never replace the restored pin. When both the
    frozen pin and the canvas advertise a revision and they differ, the frozen
    pin still wins; the drift is reported via ``canvas_binding_revision_drift``.
    """
    if type(snapshot) is not FrozenEmployeePins:
        _fail("employee_run_pin_invalid", "需要固定快照，不接受当前画布解析。")
    if current_canvas_binding_revision is not None and (
            type(current_canvas_binding_revision) is not int
            or isinstance(current_canvas_binding_revision, bool)
            or not 0 <= current_canvas_binding_revision <= 2**53 - 1):
        _fail("employee_run_pin_invalid", "当前画布绑定修订必须为非负安全整数。")
    return snapshot.for_node(node_id)


def canvas_binding_revision_drift(snapshot: FrozenEmployeePins, node_id: str, *,
                                  current_canvas_binding_revision: int | None) -> bool:
    """True when the canvas mapping revision differs from the frozen start revision.

    Drift does not authorize re-resolution. Callers must keep
    ``pin_for_running_attempt`` and treat drift as UI/ops signal only.
    """
    if type(snapshot) is not FrozenEmployeePins:
        _fail("employee_run_pin_invalid", "需要固定快照，不接受当前画布解析。")
    if current_canvas_binding_revision is None:
        return False
    if (type(current_canvas_binding_revision) is not int
            or isinstance(current_canvas_binding_revision, bool)
            or not 0 <= current_canvas_binding_revision <= 2**53 - 1):
        _fail("employee_run_pin_invalid", "当前画布绑定修订必须为非负安全整数。")
    frozen = snapshot.for_node(node_id).get("binding_revision")
    if frozen is None:
        return False
    return frozen != current_canvas_binding_revision


def frozen_reviewer_assignment(snapshot: FrozenEmployeePins, node_id: str) -> tuple[str, str]:
    """Return a selector, NOT release permission; host must check live human duty."""
    if type(snapshot) is not FrozenEmployeePins:
        _fail("employee_run_pin_invalid", "需要固定快照，不接受当前实例。")
    pin = snapshot.for_node(node_id)
    if "reviewer_slot" not in pin:
        _fail("employee_binding_reviewer_missing", "本次冻结没有审核槽位。")
    slot = pin["reviewer_slot"]
    return slot, pin["human_assignments"][slot]


def parse_optional_cost(amount: Any) -> Decimal | None:
    """Normalize an observation only. Unknown stays None, never a zero bill.

    No progress field/schema, pricing source, reconciled bill or spending grant
    is introduced. A known numeric value is not proof of payment or authority.
    """
    if amount is None:
        return None
    if type(amount) is not str or not _MONEY.fullmatch(amount):
        _fail("employee_run_pin_invalid", "费用观测必须为非负有限小数字符串或未知。")
    return Decimal(amount)
