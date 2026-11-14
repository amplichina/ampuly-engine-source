"""Pure AE01 instance, readiness and portable binding contracts.

No storage, engine, process, network, adoption or state-transition effects live
here. Readiness observations must come from an authenticated server adapter,
not a client payload. Unknown observations remain gaps. This module resolves
bindings only for prepare/launch; a resumed attempt must use its frozen pins.
"""
from __future__ import annotations

import copy
import hashlib
import re
from datetime import datetime
from decimal import Decimal
from typing import Any

from .ai_employee_contract import (
    AIEmployeeContractError, LOOP_LIMITS, canonical_json,
    validate_package_manifest, _privacy_findings,
)

_INSTANCE = re.compile(r"ei-[a-f0-9]{16}\Z")
_PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_PACKAGE = re.compile(r"[a-z0-9][a-z0-9_-]{2,63}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_MONEY = re.compile(r"(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,6})?\Z")
_SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z")
_SOURCE_PATH = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*(?:(?:\.[A-Za-z_][A-Za-z0-9_-]*)|(?:\[(?:0|[1-9][0-9]*)\]))*\Z")
_SLOTS = {"submitter", "rule_confirmer", "reviewer", "approver", "escalation", "backup", "delivery_authorizer"}
_CHANNELS = {"console", "feishu", "dingtalk", "wecom", "email"}
_INSTANCE_REQUIRED = {
    "schema_version", "instance_id", "project_id", "package_id", "package_version",
    "package_fingerprint", "instance_revision", "revision_fingerprint",
    "display_name", "adoption", "experience_scope", "intake_state",
    "execution_state", "state_changed_at", "state_changed_by",
    "created_at", "created_by", "updated_at", "updated_by",
}
_INSTANCE_OPTIONAL = {
    "enterprise_id", "params", "knowledge_refs", "model_config", "capability_impls",
    "connector_refs", "human_assignments", "limits", "state_reason", "legacy_source",
    "change_note", "context_overlay",
}
_BINDING_REQUIRED = {"package_id", "version_constraint", "primary_slot"}
_BINDING_OPTIONAL = {
    "input_mapping", "param_overrides", "output_mapping", "reviewer_slot",
    "collaborators", "binding_note",
}


def _problem(code: str, path: str, message: str) -> dict[str, str]:
    return {"code": code, "path": path, "message": message, "severity": "error"}


def _declared_slots(package: dict[str, Any]) -> set[str]:
    rows = package.get("human_slots")
    return {row["slot"] for row in rows if type(row) is dict and type(row.get("slot")) is str} if type(rows) is list else set()


class _Check:
    def __init__(self) -> None:
        self.problems: list[dict[str, str]] = []

    def add(self, path: str, message: str, code: str = "instance_invalid") -> None:
        self.problems.append(_problem(code, path, message))

    def obj(self, value: Any, path: str, required: set[str] = frozenset(), allowed: set[str] | None = None) -> bool:
        if type(value) is not dict:
            self.add(path, "必须为标准 JSON 对象。")
            return False
        for key in sorted(required - value.keys()):
            self.add(f"{path}.{key}" if path else key, "缺少必填字段。", "instance_field_required")
        if allowed is not None:
            for key in sorted(value.keys() - allowed):
                self.add(f"{path}.{key}" if path else key, "存在合同未声明字段。", "instance_field_unknown")
        return True

    def text(self, value: Any, path: str, *, pattern: re.Pattern[str] | None = None, empty: bool = False, limit: int = 256) -> None:
        if type(value) is not str or len(value) > limit or (not empty and not value.strip()) or (pattern is not None and not pattern.fullmatch(value)):
            self.add(path, "文本为空、过长或格式无效。")

    def timestamp(self, value: Any, path: str) -> None:
        try:
            if type(value) is not str or len(value) > 64 or "T" not in value:
                raise ValueError
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            self.add(path, "必须为完整 ISO 8601 时间。")

    def enum(self, value: Any, path: str, values: set[str]) -> None:
        if type(value) is not str or value not in values:
            self.add(path, "枚举值不在合同范围内。")


def instance_revision_fingerprint(instance: dict[str, Any]) -> str:
    """Pin an entire revision, including its states/metadata, excluding itself."""
    if type(instance) is not dict:
        raise AIEmployeeContractError("validation_failed", "实例修订必须为对象。")
    return hashlib.sha256(canonical_json({key: value for key, value in instance.items() if key != "revision_fingerprint"})).hexdigest()


def _limits(check: _Check, limits: Any, path: str, package_limits: dict[str, Any] | None = None) -> None:
    if not check.obj(limits, path, allowed=set(LOOP_LIMITS) | {"max_cost_cny"}):
        return
    for key, value in limits.items():
        here = f"{path}.{key}"
        if key in LOOP_LIMITS:
            low, high = LOOP_LIMITS[key]
            if type(value) is not int or not low <= value <= high:
                check.add(here, "上限必须为范围内整数，不能是布尔值。", "instance_limit_invalid")
                continue
        elif key == "max_cost_cny":
            if type(value) is not str or not _MONEY.fullmatch(value):
                check.add(here, "金额上限必须为非负小数字符串。", "instance_limit_invalid")
                continue
        else:
            continue
        if package_limits is not None and key in package_limits:
            base = package_limits[key]
            valid_base = type(base) is int if key in LOOP_LIMITS else type(base) is str and bool(_MONEY.fullmatch(base))
            if valid_base and (Decimal(value) if key == "max_cost_cny" else value) > (Decimal(base) if key == "max_cost_cny" else base):
                check.add(here, "实例只能收紧包上限，不能放宽。", "instance_limit_widened")


def effective_limits(*layers: dict[str, Any]) -> dict[str, Any]:
    """Minimum platform/package/instance/order declarations; not enforcement.

    max_cost_cny remains a declaration until a real money guard is connected.
    Missing optional keys do not authorize more work or invent a platform limit.
    """
    result: dict[str, Any] = {}
    for index, layer in enumerate(layers):
        check = _Check()
        _limits(check, layer, f"limits[{index}]")
        if check.problems:
            raise AIEmployeeContractError("validation_failed", "有效上限输入无效。", details={"problems": check.problems})
        for key, value in layer.items():
            if key == "max_cost_cny":
                if key not in result or Decimal(value) < Decimal(result[key]):
                    result[key] = value
            elif key not in result or value < result[key]:
                result[key] = value
    return result


def _params_problems(params: Any, schema: Any, path: str, *, binding: bool = False) -> list[dict[str, str]]:
    code = "employee_binding_param_not_allowed" if binding else "params_invalid"
    if type(params) is not dict or type(schema) is not dict:
        return [_problem(code, path, "参数及参数 Schema 必须为对象。")]
    schema_problems = validate_package_manifest({"input_schema": schema}, require_complete=False)
    if any(p["severity"] == "error" and p["path"].startswith("input_schema") for p in schema_problems):
        return [_problem(code, path, "包的参数 Schema 尚未安全校验。")]
    try:
        properties = schema.get("properties", {})
        patterns = schema.get("patternProperties", {})
        if type(properties) is not dict or type(patterns) is not dict:
            return [_problem(code, path, "包的参数 Schema 结构无效。")]
        for key in params:
            explicit = key in properties or any(re.search(pattern, key) for pattern in patterns)
            if not explicit and schema.get("additionalProperties") is not True and type(schema.get("additionalProperties")) is not dict:
                return [_problem(code, path, "参数包含 Schema 未显式开放的字段。")]
        from jsonschema import Draft202012Validator
        from referencing import Registry
        from referencing.exceptions import NoSuchResource

        def no_remote(uri: str) -> Any:
            raise NoSuchResource(ref=uri)

        partial = copy.deepcopy(schema)
        partial.pop("required", None)  # Runtime input fields can arrive via mappings.
        validator = Draft202012Validator(partial, registry=Registry(retrieve=no_remote))
        if next(validator.iter_errors(params), None) is not None:
            return [_problem(code, path, "参数不符合包 Schema。")]
    except (ImportError, ValueError, TypeError, RecursionError, LookupError):
        return [_problem(code, path, "参数 Schema 未能完成有界本地验证。")]
    except Exception:
        # Reference resolution errors are not permission to accept the values.
        return [_problem(code, path, "参数 Schema 引用未能完成本地验证。")]
    return []


def validate_instance(instance: Any, package_manifest: dict[str, Any] | None = None, *, expected_project_id: str | None = None, verify_fingerprint: bool = True) -> list[dict[str, str]]:
    """Structural validation only. A valid record is not a ready instance."""
    check = _Check()
    try:
        canonical_json(instance)
        if package_manifest is not None:
            canonical_json(package_manifest)
    except AIEmployeeContractError as exc:
        return [_problem(exc.code, exc.path, "实例不是有界标准 JSON。")]
    if not check.obj(instance, "", _INSTANCE_REQUIRED, _INSTANCE_REQUIRED | _INSTANCE_OPTIONAL):
        return check.problems
    if instance.get("schema_version") != 1 or type(instance.get("schema_version")) is not int:
        check.add("schema_version", "只支持实例 schema_version=1。", "instance_schema_version")
    patterns = {"instance_id": _INSTANCE, "project_id": _PROJECT, "package_id": _PACKAGE, "package_fingerprint": _SHA, "revision_fingerprint": _SHA}
    for key in ("instance_id", "project_id", "package_id", "package_version", "package_fingerprint", "revision_fingerprint", "display_name", "state_changed_by", "created_by", "updated_by"):
        if key in instance:
            check.text(instance[key], key, pattern=patterns.get(key))
    if expected_project_id is not None and instance.get("project_id") != expected_project_id:
        check.add("project_id", "实例不在当前项目范围。", "project_scope_denied")
    if "enterprise_id" in instance and instance["enterprise_id"] != "":
        check.add("enterprise_id", "首期企业归属字段必须为空。")
    if type(instance.get("instance_revision")) is not int or not 1 <= instance["instance_revision"] <= 2**63 - 1:
        check.add("instance_revision", "实例修订必须为从 1 起的整数。")
    for key in ("state_changed_at", "created_at", "updated_at"):
        if key in instance:
            check.timestamp(instance[key], key)
    check.enum(instance.get("experience_scope"), "experience_scope", {"project", "instance"})
    check.enum(instance.get("intake_state"), "intake_state", {"open", "paused"})
    check.enum(instance.get("execution_state"), "execution_state", {"active", "paused", "disabled"})
    if instance.get("intake_state") == "paused" or instance.get("execution_state") in ("paused", "disabled"):
        check.text(instance.get("state_reason"), "state_reason", limit=4096)
    elif "state_reason" in instance:
        check.text(instance["state_reason"], "state_reason", empty=True, limit=4096)
    if "change_note" in instance:
        check.text(instance["change_note"], "change_note", empty=True, limit=4096)
    if "context_overlay" in instance:
        overlay = instance["context_overlay"]
        if type(overlay) is not dict:
            check.add("context_overlay", "项目上下文覆盖必须是对象。", "overlay_invalid")
        elif overlay.get("layer") != "project":
            check.add("context_overlay.layer", "实例只接受项目层覆盖。", "overlay_layer_invalid")
        else:
            try:
                from .employee_composition import validate_overlay
                validate_overlay(overlay, project_id=str(instance.get("project_id") or ""),
                                 workflow_id="")
            except AIEmployeeContractError as exc:
                check.add(exc.path or "context_overlay", exc.message, exc.code)
    adoption = instance.get("adoption")
    adoption_keys = {"adopted_by", "adopted_at", "note", "decision_id", "previous"}
    if check.obj(adoption, "adoption", adoption_keys, adoption_keys):
        if "adopted_by" in adoption:
            check.text(adoption["adopted_by"], "adoption.adopted_by")
        if "adopted_at" in adoption:
            check.timestamp(adoption["adopted_at"], "adoption.adopted_at")
        for key in ("note", "decision_id"):
            if key in adoption:
                if key == "decision_id" and adoption[key] is None:
                    continue  # Empty before the M3 decision-book migration.
                check.text(adoption[key], "adoption." + key, empty=True, limit=4096 if key == "note" else 256)
        previous = adoption.get("previous")
        if previous not in (None, {}):
            if check.obj(previous, "adoption.previous", {"version", "fingerprint"}, {"version", "fingerprint"}):
                check.text(previous.get("version"), "adoption.previous.version")
                check.text(previous.get("fingerprint"), "adoption.previous.fingerprint", pattern=_SHA)
    for group, keys, required in (
        ("model_config", {"provider", "model", "tier"}, frozenset()),
        ("params", None, frozenset()),
        ("legacy_source", {"role_id", "employee_id", "converted_at"}, frozenset()),
    ):
        if group in instance and check.obj(instance[group], group, required, keys):
            if group == "model_config":
                for key, value in instance[group].items():
                    check.text(value, f"{group}.{key}")
            elif group == "legacy_source":
                for key, value in instance[group].items():
                    if key == "converted_at":
                        check.timestamp(value, f"{group}.{key}")
                    else:
                        check.text(value, f"{group}.{key}", pattern=_REF)
    for group, keys in (
        ("knowledge_refs", {"kind", "ref", "version", "access_scope"}),
        ("connector_refs", {"connector_id", "service_instance", "environment_id", "credential_ref"}),
    ):
        if group not in instance:
            continue
        if type(instance[group]) is not list or len(instance[group]) > 500:
            check.add(group, "引用必须为最多 500 项的数组。")
            continue
        for index, row in enumerate(instance[group]):
            here = f"{group}[{index}]"
            if check.obj(row, here, keys, keys):
                for key, value in row.items():
                    check.text(value, f"{here}.{key}", pattern=_REF if key != "version" else None)
                if group == "knowledge_refs":
                    check.enum(row.get("kind"), f"{here}.kind", {"standard", "enterprise_shared", "instance_private", "order_input"})
    assignments = instance.get("human_assignments", {})
    if check.obj(assignments, "human_assignments", allowed=_SLOTS):
        for slot, row in assignments.items():
            here = f"human_assignments.{slot}"
            if check.obj(row, here, {"employee_id", "channel"}, {"employee_id", "channel", "deadline_hours"}):
                check.text(row.get("employee_id"), here + ".employee_id", pattern=_REF)
                check.enum(row.get("channel"), here + ".channel", _CHANNELS)
                if "deadline_hours" in row and (type(row["deadline_hours"]) is not int or not 1 <= row["deadline_hours"] <= 87600):
                    check.add(here + ".deadline_hours", "期限必须为正整数小时。")
    impls = instance.get("capability_impls", {})
    if check.obj(impls, "capability_impls"):
        for tool, row in impls.items():
            check.text(tool, "capability_impls", pattern=_REF)
            if check.obj(row, f"capability_impls.{tool}", {"impl", "version_or_fingerprint"}, {"impl", "version_or_fingerprint"}):
                check.enum(row.get("impl"), f"capability_impls.{tool}.impl", {"builtin", "connector", "mcp"})
                check.text(row.get("version_or_fingerprint"), f"capability_impls.{tool}.version_or_fingerprint")
    package_limits = package_manifest.get("loop_policy") if type(package_manifest) is dict and type(package_manifest.get("loop_policy")) is dict else None
    if "limits" in instance:
        _limits(check, instance["limits"], "limits", package_limits)
    if package_manifest is not None:
        if type(package_manifest) is not dict:
            check.add("package_id", "没有合法的固定包记录。", "package_version_missing")
        else:
            if (package_manifest.get("package_id"), package_manifest.get("version")) != (instance.get("package_id"), instance.get("package_version")):
                check.add("package_version", "包引用未匹配固定版本。", "package_version_missing")
            if package_manifest.get("package_fingerprint") != instance.get("package_fingerprint"):
                check.add("package_fingerprint", "采用指纹与固定包不一致。", "fingerprint_mismatch")
            if instance.get("params"):
                check.problems.extend(_params_problems(instance["params"], package_manifest.get("input_schema"), "params"))
            declared_slots = _declared_slots(package_manifest)
            if type(assignments) is dict and assignments.keys() - declared_slots:
                check.add("human_assignments", "人员槽位未由固定包声明。")
    # Instance private values are allowed; literal credentials are not. Reuse
    # the existing scanner but ignore its standard-package privacy findings.
    for code, path in _privacy_findings(instance):
        if code == "employee_package_secret":
            # This one typed reference carries a content digest, not a
            # credential. Do not exempt generic version strings or the rest
            # of a reference from the unchanged secret scanner.
            digest_path = re.fullmatch(r"knowledge_refs\[([0-9]+)\]\.version", path)
            refs = instance.get("knowledge_refs")
            if digest_path and type(refs) is list and int(digest_path[1]) < len(refs):
                row = refs[int(digest_path[1])]
                if (type(row) is dict and set(row) == {"kind", "ref", "version", "access_scope"}
                        and row.get("kind") == "enterprise_shared" and row.get("ref") == "project-memory"
                        and type(instance.get("project_id")) is str
                        and row.get("access_scope") == instance["project_id"]
                        and type(row.get("version")) is str
                        and re.fullmatch(r"sha256:[a-f0-9]{64}", row["version"])):
                    continue
            check.add(path, "实例只能保存批准的引用，不能保存凭据正文。", "instance_secret")
    if verify_fingerprint and type(instance.get("revision_fingerprint")) is str and _SHA.fullmatch(instance["revision_fingerprint"]):
        if instance_revision_fingerprint(instance) != instance["revision_fingerprint"]:
            check.add("revision_fingerprint", "实例修订指纹不一致。", "revision_fingerprint_mismatch")
    return check.problems


def _human_on_duty(member: Any, project_id: str) -> bool:
    return type(member) is dict and member.get("project_id") == project_id and member.get("kind") == "human" and member.get("status") == "active"


def derive_readiness(instance: Any, context: dict[str, Any] | None = None, *, target_intake_state: str | None = None, target_execution_state: str | None = None) -> dict[str, Any]:
    """Derive current or one-axis resume readiness from fresh trusted facts.

    Internal *_unverified gaps represent missing observations, not proof of a
    failed operation. Adapters can map them to user-facing explanations. The
    model_verified fact must already bind AE00's six evidence conditions to
    this exact revision/configuration; this function does not create evidence.
    """
    gaps: list[str] = []
    problems: list[dict[str, str]] = []
    def gap(code: str) -> None:
        if code not in gaps:
            gaps.append(code)
    if type(instance) is not dict:
        return {"ready": False, "gaps": ["instance_invalid"], "problems": validate_instance(instance), "verified_scope": {}, "effective_limits": {}}
    if target_intake_state is not None and target_intake_state != "open" or target_execution_state is not None and target_execution_state != "active" or target_intake_state is not None and target_execution_state is not None:
        raise AIEmployeeContractError("request_invalid", "恢复预检一次只能解除一个状态轴。")
    intake = "open" if target_intake_state is not None else instance.get("intake_state")
    execution = "active" if target_execution_state is not None else instance.get("execution_state")
    if intake == "paused":
        gap("intake_paused")
    if execution == "paused":
        gap("execution_paused")
    elif execution == "disabled":
        gap("instance_disabled")
    if context is None:
        problems = validate_instance(instance)
        if problems:
            gap("instance_invalid")
        gap("readiness_context_unavailable")
        return {"ready": False, "gaps": gaps, "problems": problems, "verified_scope": {}, "effective_limits": {}}
    try:
        canonical_json(context)
    except AIEmployeeContractError:
        return {"ready": False, "gaps": gaps + ["readiness_context_invalid"], "problems": [], "verified_scope": {}, "effective_limits": {}}
    if type(context) is not dict:
        return {"ready": False, "gaps": gaps + ["readiness_context_invalid"], "problems": [], "verified_scope": {}, "effective_limits": {}}
    inventory, grants = context.get("tool_inventory"), context.get("tool_grants", {})
    available_capabilities = None
    if inventory is not None:
        inventory_valid = type(inventory) is dict and len(inventory) <= 500 and all(
            type(tool) is str and bool(_REF.fullmatch(tool))
            and type(actions) is list and len(actions) <= 500
            and all(type(action) is str and bool(_REF.fullmatch(action)) for action in actions)
            for tool, actions in inventory.items()
        )
        if inventory_valid:
            # The trusted context is JSON; the package validator expects sets.
            # Inventory proves existence only, never the caller's grant.
            available_capabilities = {tool: set(actions) for tool, actions in inventory.items()}
        else:
            gap("tool_inventory_invalid")
    package = context.get("package_manifest")
    problems = validate_instance(instance, package)
    if problems:
        gap("instance_invalid")
        for problem in problems:
            if problem["code"] in {"fingerprint_mismatch", "package_version_missing", "params_invalid"}:
                gap(problem["code"])
    if type(package) is not dict:
        gap("package_version_missing")
        package = {}
    elif any(p["severity"] == "error" for p in validate_package_manifest(
        package, require_complete=True, available_capabilities=available_capabilities
    )):
        gap("package_incomplete")
    if package.get("status") == "retired":
        gap("package_retired")
    elif package.get("status") != "published":
        gap("package_not_published")
    project_id = instance.get("project_id")
    members = context.get("human_members", {})
    assignments = instance.get("human_assignments", {})
    for slot in package.get("human_slots", []) if type(package.get("human_slots")) is list else []:
        if type(slot) is not dict:
            continue
        slot_name = slot.get("slot")
        row = assignments.get(slot_name) if type(assignments) is dict and type(slot_name) is str else None
        if row is None:
            if slot.get("required") is True:
                gap("human_slot_unfilled")
            continue
        employee_id = row.get("employee_id") if type(row) is dict else None
        if type(employee_id) is not str or type(members) is not dict or not _human_on_duty(members.get(employee_id), project_id):
            gap("human_not_on_duty")
        options = slot.get("channel_options")
        if type(row) is dict and options is not None and (type(options) is not list or row.get("channel") not in options):
            gap("human_slot_unfilled")
    for required in package.get("capability_requirements", []) if type(package.get("capability_requirements")) is list else []:
        if type(required) is not dict:
            continue
        tool, actions = required.get("tool"), required.get("actions", [])
        if type(tool) is not str or type(inventory) is not dict or type(grants) is not dict or type(actions) is not list or type(inventory.get(tool)) is not list or type(grants.get(tool)) is not list or any(type(action) is not str or action not in inventory[tool] or action not in grants[tool] for action in actions):
            gap("tool_not_granted")
    states, credentials = context.get("connector_states", {}), context.get("credential_refs", [])
    for ref in instance.get("connector_refs", []) if type(instance.get("connector_refs")) is list else []:
        if type(ref) is not dict:
            continue
        connector_id = ref.get("connector_id")
        state = states.get(connector_id) if type(states) is dict and type(connector_id) is str else None
        if type(state) is not dict or state.get("enabled") is not True or state.get("project_id") != project_id:
            gap("connector_off")
        elif any(key in state and state[key] != ref.get(key) for key in ("service_instance", "environment_id")):
            gap("connector_off")
        if type(credentials) is not list or ref.get("credential_ref") not in credentials:
            gap("credential_ref_missing")
    if instance.get('knowledge_refs'):
        if type(context.get('knowledge_accessible')) is not bool:
            gap('knowledge_access_unverified')
        elif context['knowledge_accessible'] is False:
            gap('knowledge_unavailable')
    for name, failed, unknown in (
        ("model_available", "model_unavailable", "model_availability_unverified"),
        ("model_verified", "untested_for_model", "model_verification_unverified"),
        ("authorization_valid", "tool_not_granted", "authorization_unverified"),
        ("budget_available", "budget_exhausted", "budget_unverified"),
        ("adoption_active", "adoption_inactive", "adoption_unverified"),
        ("project_active", "project_inactive", "project_state_unverified"),
        ("channel_enabled", "channel_off", "channel_state_unverified"),
    ):
        if type(context.get(name)) is not bool:
            gap(unknown)
        elif context[name] is False:
            gap(failed)
    derived_limits: dict[str, Any] = {}
    try:
        derived_limits = effective_limits(context.get("platform_limits", {}), package.get("loop_policy", {}), instance.get("limits", {}), context.get("order_limits", {}))
    except AIEmployeeContractError as exc:
        gap("limits_invalid")
        problems.extend((exc.details or {}).get("problems", []))
    scope = context.get("verified_scope", {})
    if type(scope) is not dict:
        gap("verification_scope_invalid")
        scope = {}
    return {"ready": not gaps and intake == "open" and execution == "active", "gaps": gaps, "problems": problems, "verified_scope": copy.deepcopy(scope), "effective_limits": derived_limits}


def resolve_reviewer(pin: dict[str, Any], human_members: dict[str, Any], *, project_id: str, actor_employee_id: str | None = None) -> dict[str, str]:
    """Use the frozen slot/assignment plus live human status, never gate_to.

    Owner overrides belong to a separately audited server authorization path.
    This function does not grant one or infer approval from a channel message.
    """
    canonical_json(pin)
    canonical_json(human_members)
    if type(project_id) is not str or not _PROJECT.fullmatch(project_id) or type(pin) is not dict or pin.get("project_id", project_id) != project_id:
        raise AIEmployeeContractError("project_scope_denied", "审核冻结引用不在当前项目范围。")
    slot = pin.get("reviewer_slot") if type(pin) is dict else None
    assignments = pin.get("human_assignments") if type(pin) is dict else None
    valid_slot = type(slot) is str and slot in _SLOTS
    employee_id = assignments.get(slot) if type(assignments) is dict and valid_slot else None
    if type(employee_id) is not str or not _REF.fullmatch(employee_id):
        raise AIEmployeeContractError("human_not_on_duty", "没有固定的人工审核资格。", details={"slot": slot if valid_slot else ""})
    member = human_members.get(employee_id) if type(human_members) is dict else None
    if not _human_on_duty(member, project_id) or actor_employee_id is not None and actor_employee_id != employee_id:
        raise AIEmployeeContractError("human_not_on_duty", "当前调用者没有这个固定槽位的在岗资格。", details={"slot": slot})
    return {"reviewer_slot": slot, "employee_id": employee_id, "actor_basis": "frozen_human_slot"}


def validate_binding(binding: Any, package_manifest: dict[str, Any] | None = None, *, nodes: list[dict[str, Any]] | None = None, node_id: str | None = None, requires_review: bool = False) -> list[dict[str, str]]:
    """Validate portable node data; project/instance IDs cannot be embedded."""
    check = _Check()
    try:
        canonical_json(binding)
        if package_manifest is not None:
            canonical_json(package_manifest)
    except AIEmployeeContractError as exc:
        return [_problem(exc.code, exc.path, "绑定不是有界标准 JSON。")]
    if not check.obj(binding, "employee_binding", _BINDING_REQUIRED, _BINDING_REQUIRED | _BINDING_OPTIONAL):
        return check.problems
    check.text(binding.get("package_id"), "employee_binding.package_id", pattern=_PACKAGE)
    check.enum(binding.get("primary_slot"), "employee_binding.primary_slot", {"executor"})
    constraint = binding.get("version_constraint")
    if check.obj(constraint, "employee_binding.version_constraint", {"mode"}, {"mode", "version"}):
        check.enum(constraint.get("mode"), "employee_binding.version_constraint.mode", {"adopted", "exact", "at_least"})
        if constraint.get("mode") in ("exact", "at_least") or "version" in constraint:
            check.text(constraint.get("version"), "employee_binding.version_constraint.version")
        if constraint.get("mode") == "at_least" and _semver(constraint.get("version")) is None:
            check.add("employee_binding.version_constraint.version", "at_least 需要可比较的 SemVer，不能猜测版本顺序。", "employee_binding_version_unavailable")
    if "collaborators" in binding:
        if type(binding["collaborators"]) is not list:
            check.add("employee_binding.collaborators", "协作者必须为数组。", "employee_binding_collaborator_unsupported")
        elif binding["collaborators"]:
            check.add("employee_binding.collaborators", "首期协作者保存与执行尚未接入。", "employee_binding_collaborator_unsupported")
    for group in ("input_mapping", "output_mapping"):
        if group in binding and check.obj(binding[group], "employee_binding." + group):
            for field, reference in binding[group].items():
                check.text(field, "employee_binding." + group, pattern=_REF)
                check.text(reference, f"employee_binding.{group}.{field}", pattern=_SOURCE_PATH)
    if "param_overrides" in binding:
        check.obj(binding["param_overrides"], "employee_binding.param_overrides")
    if "binding_note" in binding:
        check.text(binding["binding_note"], "employee_binding.binding_note", empty=True, limit=4096)
    if "reviewer_slot" in binding:
        check.enum(binding["reviewer_slot"], "employee_binding.reviewer_slot", _SLOTS)
    elif requires_review:
        check.add("employee_binding.reviewer_slot", "审核节点必须声明固定人工槽位。", "employee_binding_reviewer_missing")
    if package_manifest is not None:
        if type(package_manifest) is not dict or package_manifest.get("package_id") != binding.get("package_id"):
            check.add("employee_binding.package_id", "绑定没有匹配的固定岗位包。", "employee_binding_package_missing")
        else:
            slots = _declared_slots(package_manifest)
            if "reviewer_slot" in binding and (type(binding["reviewer_slot"]) is not str or binding["reviewer_slot"] not in slots):
                check.add("employee_binding.reviewer_slot", "人工槽位未由包声明。", "employee_binding_reviewer_missing")
            if binding.get("param_overrides"):
                check.problems.extend(_params_problems(binding["param_overrides"], package_manifest.get("input_schema"), "employee_binding.param_overrides", binding=True))
            for group, schema_key, code in (
                ("input_mapping", "input_schema", "employee_binding_input_unmapped"),
                ("output_mapping", "output_schema", "employee_binding_output_unmapped"),
            ):
                schema = package_manifest.get(schema_key)
                mapping = binding.get(group, {})
                if type(schema) is dict and type(mapping) is dict:
                    properties = schema.get("properties", {})
                    if type(properties) is not dict or mapping.keys() - properties.keys():
                        check.add("employee_binding." + group, "映射包含未声明的输入输出字段。", code)
    if nodes is not None:
        try:
            canonical_json(nodes)
        except AIEmployeeContractError as exc:
            check.add("employee_binding", "角色节点表不是有界标准 JSON。", exc.code)
            return check.problems
        if type(nodes) is not list or node_id is None:
            check.add("employee_binding", "角色隔离校验需要本流程节点表及节点编号。", "employee_binding_role_shared")
        else:
            matching = [node for node in nodes if type(node) is dict and node.get("id") == node_id]
            if len(matching) != 1 or type(matching[0].get("role")) is not str or not matching[0]["role"]:
                check.add("employee_binding", "绑定节点没有唯一角色。", "employee_binding_role_shared")
            elif sum(type(node) is dict and node.get("role") == matching[0]["role"] for node in nodes) != 1:
                check.add("employee_binding", "首期绑定节点须使用独立角色，请新建独立步骤或角色。", "employee_binding_role_shared")
    for code, path in _privacy_findings(binding):
        if code == "employee_package_secret":
            check.add(path, "节点绑定不能保存凭据正文。", "instance_secret")
    return check.problems


def _semver(value: Any) -> tuple[tuple[int, int, int], tuple[str, ...] | None] | None:
    if type(value) is not str or len(value) > 256:
        return None
    matched = _SEMVER.fullmatch(value)
    if matched is None:
        return None
    pre = tuple(matched.group(4).split(".")) if matched.group(4) else None
    if pre is not None and any(not item or item.isdigit() and len(item) > 1 and item.startswith("0") for item in pre):
        return None
    return (tuple(int(matched.group(i)) for i in range(1, 4)), pre)


def _at_least(actual: str, minimum: str) -> bool:
    left, right = _semver(actual), _semver(minimum)
    if left is None or right is None:
        return False
    if left[0] != right[0]:
        return left[0] > right[0]
    if left[1] is None or right[1] is None:
        return left[1] is None
    for a, b in zip(left[1], right[1]):
        if a == b:
            continue
        if a.isdigit() and b.isdigit():
            return int(a) > int(b)
        if a.isdigit() != b.isdigit():
            return not a.isdigit()
        return a > b
    return len(left[1]) >= len(right[1])


def resolve_binding(binding: dict[str, Any], *, project_id: str, workflow_id: str, node_id: str, instances: list[dict[str, Any]], mappings: list[dict[str, Any]] | tuple = (), selected_instance_id: str | None = None, binding_slot: str = "executor") -> dict[str, Any]:
    """Resolve only project-local immutable inputs, without writing selection.

    A matching but stale mapping fails closed; it does not fall back to a new
    instance. Distinct same-package candidates require explicit human choice,
    even when some are paused. Readiness is a separate fresh server check.
    """
    if any(type(value) is not str or not pattern.fullmatch(value) for value, pattern in ((project_id, _PROJECT), (workflow_id, _REF), (node_id, _REF))):
        raise AIEmployeeContractError("request_invalid", "项目、流程与节点标识无效。")
    if binding_slot != "executor":
        raise AIEmployeeContractError("employee_binding_collaborator_unsupported", "首期只支持主责席位。")
    problems = validate_binding(binding)
    if problems:
        raise AIEmployeeContractError("validation_failed", "节点绑定无效。", details={"problems": problems})
    canonical_json(instances)
    if type(instances) is not list or len(instances) > 500:
        raise AIEmployeeContractError("validation_failed", "实例候选必须为最多 500 项的数组。")
    rows = list(mappings) if type(mappings) in (list, tuple) else None
    canonical_json(rows)
    if rows is None or len(rows) > 5000:
        raise AIEmployeeContractError("validation_failed", "绑定映射超出范围。")
    local: dict[str, dict[str, Any]] = {}
    constraint = binding["version_constraint"]
    for instance in instances:
        # Scope comes before validation/error details: foreign objects are not
        # exposed as candidates, and their existence must not affect the result.
        if type(instance) is not dict or instance.get("project_id") != project_id:
            continue
        if instance.get("package_id") != binding["package_id"]:
            continue
        problems = validate_instance(instance, expected_project_id=project_id)
        if problems:
            raise AIEmployeeContractError("validation_failed", "本项目候选记录无效。", details={"problems": problems})
        version = instance["package_version"]
        satisfies = constraint["mode"] == "adopted" or constraint["mode"] == "exact" and version == constraint["version"] or constraint["mode"] == "at_least" and _at_least(version, constraint["version"])
        if not satisfies:
            continue
        identity = instance["instance_id"]
        if identity in local:
            raise AIEmployeeContractError("validation_failed", "实例候选编号重复。")
        local[identity] = instance
    matched = [row for row in rows if type(row) is dict and row.get("project_id", project_id) == project_id and (row.get("workflow_id"), row.get("node_id"), row.get("binding_slot")) == (workflow_id, node_id, binding_slot)]
    if len(matched) > 1:
        raise AIEmployeeContractError("binding_unresolved", "同一节点席位有重复映射。", details={"candidates": []})
    explicit = selected_instance_id
    source = "human_choice"
    if explicit is None and matched:
        row = matched[0]
        mapping_check = _Check()
        mapping_check.text(row.get("resolved_by"), "resolved_by")
        mapping_check.timestamp(row.get("resolved_at"), "resolved_at")
        if mapping_check.problems or row.get("source") not in ("human_choice", "single_candidate") or type(row.get("binding_revision")) is not int or row["binding_revision"] < 1 or type(row.get("instance_id")) is not str:
            raise AIEmployeeContractError("binding_unresolved", "项目席位映射记录无效。", details={"candidates": []})
        explicit, source = row.get("instance_id"), row["source"]
    if explicit is not None:
        if type(explicit) is not str or explicit not in local:
            raise AIEmployeeContractError("binding_unresolved", "所选实例不能用于本项目席位。", details={"candidates": []})
        selected = local[explicit]
    elif len(local) == 1:
        selected = next(iter(local.values()))
        source = "single_candidate"
    elif local:
        raise AIEmployeeContractError("multiple_instances_choice_required", "同包多实例，请明确选择。", details={"candidates": [{"instance_id": key, "display_name": local[key]["display_name"]} for key in sorted(local)]})
    else:
        raise AIEmployeeContractError("binding_unresolved", "本项目席位尚未解析。", details={"candidates": []})
    return {"instance": copy.deepcopy(selected), "source": source, "key": {"project_id": project_id, "workflow_id": workflow_id, "node_id": node_id, "binding_slot": binding_slot}}
