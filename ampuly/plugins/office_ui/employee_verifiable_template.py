"""D-package verifiable templates and explicit publish checks.

Pure contract: no I/O, no Runtime, no auto-publish, no copying of identity,
credentials, grants or source-project access. Templates lower the blank-page
barrier; they do not replace authorization or acceptance.
"""
from __future__ import annotations

import copy
import hashlib
import re
from typing import Any, Iterable, Mapping

from .ai_employee_contract import AIEmployeeContractError, _secret_shaped, canonical_json
from .employee_composition import check_approval

RULES_VERSION = "d-template-publish-v1"
SCHEMA_VERSION = 1

_REQUIRED = (
    "template_id",
    "schema_version",
    "rules_version",
    "five_sections",
    "applicable_inputs",
    "employee_version_req",
    "workflow_version_req",
    "project_context_slots",
    "samples",
    "output_assertions",
    "dependencies",
    "failure_examples",
    "verified_run_scope",
)
_FIVE = ("role", "knowledge", "sop", "output", "permissions")
_FORBIDDEN_TOP = frozenset({
    "credentials", "tool_grants", "grants", "secret", "password", "api_key",
    "source_project_access", "access_token", "credential_ref_values",
})
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_ABS_PATH = re.compile(r"(?:[A-Za-z]:\\|/)(?:Users|home|Documents)[/\\]", re.I)


def _err(code: str, message: str, location: str = "", **details: Any) -> AIEmployeeContractError:
    return AIEmployeeContractError(code, message, path=location, details=details or None)


_DIGEST_KEYS = frozenset({
    "fingerprint", "template_digest", "digest", "hash", "sha256", "evidence",
    "package_fingerprint", "release_fingerprint", "revision_fingerprint",
})


def _is_digest_key(key: str) -> bool:
    return key in _DIGEST_KEYS or key.endswith("_fingerprint") or key.endswith("_digest") or key.endswith("_hash")


def _reject_secrets(value: Any, where: str) -> None:
    """Reject literal secrets and personal paths; never treat digests as secrets."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            path = f"{where}.{key}" if where else str(key)
            if _is_digest_key(str(key)):
                if isinstance(item, str) and _ABS_PATH.search(item):
                    raise _err("template_personal_path", "模板不得含可直接使用的个人路径。", path)
                continue
            if key != "credential_ref" and _secret_shaped(str(key), item):
                raise _err("template_secret_literal", "模板不得含字面密钥，只允许引用位。", path)
            _reject_secrets(item, path)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            path = f"{where}[{i}]"
            if isinstance(item, (dict, list)):
                _reject_secrets(item, path)
            elif isinstance(item, str) and _ABS_PATH.search(item):
                raise _err("template_personal_path", "模板不得含可直接使用的个人路径。", path)
    elif isinstance(value, str) and _ABS_PATH.search(value):
        raise _err("template_personal_path", "模板不得含可直接使用的个人路径。", where)


def validate_template(template: Any, *, for_catalog: bool = False) -> dict[str, Any]:
    """Validate a verifiable template record; return a normalized copy."""
    if type(template) is not dict:
        raise _err("template_invalid", "模板必须是对象。")
    missing = [k for k in _REQUIRED if k not in template]
    if missing:
        raise _err("template_field_missing", "模板缺少必填字段。", missing[0], missing=missing)
    for key in template:
        if key in _FORBIDDEN_TOP or _secret_shaped(str(key), None):
            raise _err("template_forbidden_payload", "模板不得携带凭据、授权或源项目访问。", str(key))
    if template.get("schema_version") != SCHEMA_VERSION:
        raise _err("template_schema_unsupported", "不支持的模板 schema_version。", "schema_version")
    if template.get("rules_version") != RULES_VERSION:
        raise _err("template_rules_mismatch", "模板规则版本不匹配。", "rules_version")
    if type(template["template_id"]) is not str or not _ID.fullmatch(template["template_id"]):
        raise _err("template_id_invalid", "template_id 无效。", "template_id")
    sections = template["five_sections"]
    if type(sections) is not dict or set(sections) != set(_FIVE):
        raise _err("template_five_sections_invalid", "五项配置必须恰好包含 role/knowledge/sop/output/permissions。", "five_sections")
    for name in ("applicable_inputs", "output_assertions", "failure_examples", "dependencies",
                 "project_context_slots", "samples", "verified_run_scope"):
        if type(template[name]) is not list:
            raise _err("template_field_invalid", f"{name} 必须是列表。", name)
    for name in ("applicable_inputs", "output_assertions", "failure_examples"):
        if not template[name]:
            raise _err("template_field_empty", f"{name} 不能为空。", name)
    emp = template["employee_version_req"]
    if type(emp) is not dict or not emp.get("package_id"):
        raise _err("template_employee_req_invalid", "员工版本要求须含 package_id。", "employee_version_req")
    wf = template["workflow_version_req"]
    if type(wf) is not dict or "workflow_id" not in wf:
        raise _err("template_workflow_req_invalid", "流程版本要求须含 workflow_id（可空串表示未绑定）。", "workflow_version_req")
    _reject_secrets(template, "template")
    scope = template["verified_run_scope"]
    if for_catalog and not scope:
        raise _err("template_not_evidenced", "未举证的模板不得列入产品目录。", "verified_run_scope")
    if scope and not template["samples"]:
        raise _err("template_samples_required", "已验证范围非空时必须附带样例。", "samples")
    out = copy.deepcopy(template)
    out["template_digest"] = hashlib.sha256(canonical_json({
        k: out[k] for k in _REQUIRED
    })).hexdigest()
    return out


def instantiate_to_project_draft(
    template: Mapping[str, Any],
    *,
    target_project_id: str,
    actor: str,
) -> dict[str, Any]:
    """Copy a template into a project-local draft with gaps, never grants."""
    if type(target_project_id) is not str or not target_project_id.strip():
        raise _err("template_project_invalid", "目标项目编号必填。", "target_project_id")
    if type(actor) is not str or not actor.strip():
        raise _err("template_actor_invalid", "操作者必填。", "actor")
    validated = validate_template(dict(template), for_catalog=False)
    gaps: list[dict[str, str]] = []
    for slot in validated["project_context_slots"]:
        if type(slot) is not dict:
            raise _err("template_slot_invalid", "上下文槽位必须是对象。", "project_context_slots")
        code = str(slot.get("gap_if_empty") or "context_slot_unfilled")
        gaps.append({
            "code": code,
            "message": str(slot.get("label") or slot.get("name") or "需要项目内配置"),
            "slot": str(slot.get("name") or slot.get("key") or "slot"),
        })
    for dep in validated["dependencies"]:
        if type(dep) is dict and dep.get("kind") in {"grant", "capability", "tool_grant"}:
            gaps.append({
                "code": "grant_not_copied",
                "message": "授权不会随模板复制，须在目标项目重新授予。",
                "slot": str(dep.get("name") or dep.get("id") or "grant"),
            })
        elif type(dep) is dict and dep.get("kind") == "knowledge":
            gaps.append({
                "code": "knowledge_not_copied",
                "message": "知识引用不会携带原项目访问，须在目标项目重新挂接。",
                "slot": str(dep.get("ref") or dep.get("name") or "knowledge"),
            })
    draft = {
        "project_id": target_project_id,
        "from_template_id": validated["template_id"],
        "from_template_digest": validated["template_digest"],
        "status": "draft",
        "five_sections": copy.deepcopy(validated["five_sections"]),
        "employee_version_req": copy.deepcopy(validated["employee_version_req"]),
        "workflow_version_req": copy.deepcopy(validated["workflow_version_req"]),
        "project_context_slots": copy.deepcopy(validated["project_context_slots"]),
        "output_assertions": copy.deepcopy(validated["output_assertions"]),
        "created_by": actor,
        "tool_grants": {},
        "credentials": None,
        "source_project_id": None,
    }
    return {
        "draft": draft,
        "gaps": gaps,
        "copied_credentials": False,
        "copied_grants": False,
        "copied_source_project_access": False,
        "rules_version": RULES_VERSION,
    }


def validate_formal_publish(
    *,
    composition_record: Mapping[str, Any],
    approval: Any,
    admission: Mapping[str, Any] | None = None,
    knowledge_grant_current: Iterable[str] | None = None,
    allow_draft_pin: bool = False,
    required_layers: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Gate for formal publish: approval current, deps present, no draft fallback."""
    if type(composition_record) is not dict:
        raise _err("publish_composition_invalid", "正式发布需要组合记录。")
    if "composition_id" not in composition_record:
        raise _err("publish_composition_invalid", "正式发布需要完整组合记录。")
    pin = composition_record.get("employee_pin")
    if isinstance(pin, dict):
        status = pin.get("status")
        if status and status != "published" and not allow_draft_pin:
            raise _err("publish_draft_pin_refused", "正式发布拒绝草稿 pin，且不会回退到最新版。", "employee_pin")
        if pin.get("draft_digest"):
            raise _err("publish_draft_pin_refused", "正式发布拒绝带 draft_digest 的 pin。", "employee_pin")
    if composition_record.get("purpose") == "isolated_trial":
        raise _err("publish_trial_refused", "隔离试跑组合不能满足正式发布。", "purpose")
    check_approval(approval, dict(composition_record))
    required = list(required_layers or composition_record.get("required_layers") or [])
    overlays = list(composition_record.get("overlays") or [])
    present_layers = {o.get("layer") for o in overlays if isinstance(o, dict)}
    for layer in required:
        if layer not in present_layers:
            raise _err("publish_dependency_missing", "正式组合缺少必需覆盖层，不会回退空配置。", str(layer))
    effective = composition_record.get("effective") or {}
    refs = effective.get("knowledge_refs") or []
    if knowledge_grant_current is not None:
        allowed = set(knowledge_grant_current)
        for row in refs:
            if not isinstance(row, dict):
                continue
            key = str(row.get("ref") or "")
            if key and key not in allowed:
                raise _err("publish_dependency_missing", "知识引用当前不可用，不会回退草稿或最新。", key)
    gaps = effective.get("capability_gaps") or []
    if gaps:
        raise _err("publish_dependency_missing", "能力缺口未闭合，拒绝发布。", "capability_gaps")
    adm = dict(admission or {})
    if admission is not None and not adm.get("allowed", False):
        raise _err("publish_admission_denied", "当前准入条件不满足。", "admission")
    return {
        "ok": True,
        "composition_id": composition_record["composition_id"],
        "effects": {"publish_performed": False, "grants_changed": False},
        "rules_version": RULES_VERSION,
    }


def assert_reviewed_config_unchanged(approval: Any, composition_record: Mapping[str, Any]) -> None:
    """Case 13 helper: any reviewed-config drift invalidates the old approval."""
    check_approval(approval, dict(composition_record))


def selection_impact(
    *,
    before_default_ref: str,
    after_default_ref: str,
    historical_pins: Mapping[str, Mapping[str, Any]],
    historical_pins_after: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Publish/rollback may change only the default ref for new tasks."""
    after_pins = dict(historical_pins_after if historical_pins_after is not None else historical_pins)
    for task_id, pin in historical_pins.items():
        other = after_pins.get(task_id)
        if other is None or canonical_json(dict(pin)) != canonical_json(dict(other)):
            raise _err(
                "historical_pin_rewrite_refused",
                "发布或回退不得重写历史任务配置或事实。",
                str(task_id),
            )
    return {
        "new_task_default_changed": before_default_ref != after_default_ref,
        "historical_rewritten": False,
        "before_default_ref": before_default_ref,
        "after_default_ref": after_default_ref,
        "rules_version": RULES_VERSION,
    }




def apply_formal_publish_gate(
    composition_gate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Default-off formal composition gate for real publish paths.

    ``None`` / empty → skip (legacy publish unchanged). Non-empty must carry
    ``composition_record`` + ``approval``; then ``validate_formal_publish`` runs.
    Never performs publication I/O (``publish_performed`` stays False here).
    """
    if composition_gate is None or composition_gate == {}:
        return {
            "ok": True,
            "skipped": True,
            "reason": "gate_off",
            "rules_version": RULES_VERSION,
            "effects": {"publish_performed": False},
        }
    if type(composition_gate) is not dict:
        raise _err("publish_gate_invalid", "正式组合门禁参数无效。")
    record = composition_gate.get("composition_record")
    approval = composition_gate.get("approval")
    if record is None or approval is None:
        raise _err(
            "publish_gate_incomplete",
            "启用正式组合门禁时必须同时提供 composition_record 与 approval。",
        )
    result = validate_formal_publish(
        composition_record=record,  # type: ignore[arg-type]
        approval=approval,
        admission=composition_gate.get("admission"),
        knowledge_grant_current=composition_gate.get("knowledge_grant_current"),
        allow_draft_pin=bool(composition_gate.get("allow_draft_pin", False)),
        required_layers=composition_gate.get("required_layers"),
    )
    out = dict(result)
    out["skipped"] = False
    out["reason"] = "gate_enforced"
    return out


def knowledge_revoke_impact(
    *,
    revoked_ref: str,
    catalog_refs: Iterable[Mapping[str, Any]],
    historical_explanations: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Locate template/composition refs to stop; keep history text without body."""
    if type(revoked_ref) is not str or not revoked_ref.strip():
        raise _err("knowledge_ref_invalid", "撤销目标无效。", "revoked_ref")
    blocked: list[str] = []
    for row in catalog_refs:
        if not isinstance(row, dict):
            continue
        refs = row.get("knowledge_refs") or row.get("refs") or []
        hit = False
        if revoked_ref in refs:
            hit = True
        for item in refs:
            if isinstance(item, dict) and item.get("ref") == revoked_ref:
                hit = True
            elif item == revoked_ref:
                hit = True
        if hit:
            blocked.append(str(row.get("id") or row.get("template_id") or row.get("composition_id") or ""))
    history: list[dict[str, str]] = []
    for row in historical_explanations:
        if not isinstance(row, dict):
            continue
        history.append({
            "id": str(row.get("id") or ""),
            "explanation": str(row.get("explanation") or row.get("message") or "该知识来源已失效。"),
            # deliberately drop any content/body keys
        })
    return {
        "revoked_ref": revoked_ref,
        "block_new_refs": [b for b in blocked if b],
        "history": history,
        "content_included": False,
        "rules_version": RULES_VERSION,
    }
