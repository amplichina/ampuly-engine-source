"""AE00 synchronous action leaf: trusted dependencies in, plain ack out.

No socket, engine, database, filesystem, repository construction, capabilities
registry or automatic retry lives here. Authentication and project membership
are checked on every invocation, including replayed request IDs. The host must
inject read-only published_catalog.list()/detail(id, version=None), existing
draft/instance/binding repository objects, and a server-only binding loader.

Request fields are AE00's flat transport fields, not B's private view DTOs.
source='draft' is an explicit owner-editor extension for package list/detail;
omitting it always reads the published catalog, never the draft repository.
For refused/unverified results revision is None. Unknown write effects are None,
not False; the host must preserve result='outcome_unknown' without retrying.
"""
from __future__ import annotations

import inspect
import hashlib
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, ContextManager

from .ai_employee_contract import AIEmployeeContractError, canonical_json
from .ai_employee_instance_contract import effective_limits
from .employee_saved_workflow import SavedBindingSource
from .employee_instances import _model_configuration
from .employee_lifecycle_actions import LIFECYCLE_FIELDS, LIFECYCLE_WRITE, dispatch_lifecycle_action
from .employee_instance_adoption_actions import INSTANCE_ADOPTION_FIELDS, INSTANCE_ADOPTION_WRITE, dispatch_instance_adoption_action
from .employee_knowledge_actions import KNOWLEDGE_FIELDS, KNOWLEDGE_READ, KNOWLEDGE_WRITE, dispatch_knowledge_action

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_PACKAGE = re.compile(r"[a-z0-9][a-z0-9_-]{2,63}\Z")
_INSTANCE = re.compile(r"ei-[a-f0-9]{16}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_ACTION = re.compile(r"(?:employee_package|employee_instance|employee_binding)_[a-z_]{1,80}\Z")
_DIAGNOSTICS = frozenset({"employee_package_" + suffix for suffix in (
    "approval_stale", "capability_unknown", "capability_unverified", "field_unknown",
    "file_conflict", "file_duplicate", "file_encoding", "file_kind", "file_missing",
    "file_path", "file_undeclared", "invalid", "json_invalid", "limit", "model_unverified",
    "private_content", "required", "reviewer_required", "schema_invalid", "schema_remote",
    "schema_scope_unsupported", "schema_unavailable", "schema_version", "scope_out_empty", "secret",
)}) | frozenset({"employee_binding_" + suffix for suffix in (
    "collaborator_unsupported", "input_unmapped", "output_unmapped", "package_missing",
    "param_not_allowed", "reviewer_missing", "role_shared", "version_unavailable",
)})
_PACKAGE_WRITE = frozenset({"employee_package_draft_create", "employee_package_draft_save"})
_INSTANCE_WRITE = frozenset({"employee_instance_revise"})
_BINDING_WRITE = frozenset({"employee_binding_resolve_set"})
_BINDING_SELECTION = frozenset({"employee_binding_candidates", *_BINDING_WRITE})
_OWNER_READ = frozenset({"employee_instance_adoption_preview", "employee_binding_candidates", "employee_instance_model_candidates"}) | KNOWLEDGE_READ
_WRITE = _PACKAGE_WRITE | _INSTANCE_WRITE | _BINDING_WRITE | LIFECYCLE_WRITE | INSTANCE_ADOPTION_WRITE | KNOWLEDGE_WRITE
_PUBLIC_GAP = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")
_CANDIDATE_KEYS = frozenset({"instance_id", "display_name", "package_id", "package_version", "package_fingerprint",
    "instance_revision", "revision_fingerprint", "intake_state", "execution_state", "ready", "gaps"})
SUPPORTED_ACTIONS = frozenset({
    "employee_package_list", "employee_package_detail", *_WRITE, *_OWNER_READ,
    "employee_instance_list", "employee_instance_detail", "employee_binding_preview",
    *LIFECYCLE_FIELDS,
    *INSTANCE_ADOPTION_FIELDS,
})
_COMMON = frozenset({"type", "project_id", "request_id"})
_FIELDS = {
    "employee_package_list": {"source"},
    "employee_package_detail": {"source", "package_id", "version"},
    "employee_package_draft_create": {"manifest", "template_id", "expected_template_revision", "workflow_source"},
    "employee_package_draft_save": {"package_id", "changes", "expected_revision"},
    "employee_instance_list": set(),
    "employee_instance_detail": {"instance_id"},
    "employee_instance_revise": {"instance_id", "changes", "expected_revision", "note"},
    "employee_instance_model_candidates": {"instance_id", "expected_revision", "expected_fingerprint"},
    "employee_instance_adoption_preview": {"instance_id", "expected_revision",
        "package_id", "package_version", "expected_fingerprint"},
    "employee_binding_preview": {"workflow_id", "node_id", "binding_slot", "expected_workflow_revision"},
    "employee_binding_candidates": {"workflow_id", "node_id", "binding_slot", "expected_workflow_revision"},
    "employee_binding_resolve_set": {"workflow_id", "node_id", "binding_slot", "expected_workflow_revision",
        "instance_id", "expected_binding_revision", "expected_instance_revision", "expected_instance_fingerprint", "check_only"},
}
_MODEL_HEADERS = frozenset({"expected_instance_fingerprint", "expected_model_catalogue_revision", "check_only"})
_FIELDS.update(LIFECYCLE_FIELDS)
_FIELDS.update(INSTANCE_ADOPTION_FIELDS)
_FIELDS.update(KNOWLEDGE_FIELDS)
_FIELDS["employee_instance_revise"] = _FIELDS["employee_instance_revise"] | _MODEL_HEADERS
_MODEL_PROJECTION_VERSION = "employee_instance_model_selection.v1"
_MODEL_GAPS = frozenset({"model_unknown", "model_retired", "model_overseas_off", "model_not_routable", "model_needs_params", "model_needs_trial"})
_MODEL_VIEW_FIELDS = frozenset({"selection_scope", "catalogue_source", "projection_version", "model_catalogue_revision",
    "instance_id", "instance_revision", "revision_fingerprint", "package_id", "package_version", "package_fingerprint",
    "candidates", "revision", "fingerprint", "effects_produced", "problems", "allowed_actions"})


def _model_exact_keys(value: Any, expected: frozenset | set) -> bool:
    if type(value) is not dict or len(value) != len(expected):
        return False
    # Reject arbitrary key hooks before set/equality or canonical serialization.
    return all(type(key) is str for key in value) and set(value) == expected

_PACKAGE_METHODS = {"employee_package_list": "list", "employee_package_detail": "detail",
    "employee_package_draft_create": "create", "employee_package_draft_save": "save"}
_MESSAGES = {
    "owner_required": "此操作仅限项目所有者。",
    "request_invalid": "员工请求字段无效。",
    "package_not_found": "目标岗位包不可用。",
    "version_not_available": "目标岗位版本不可用。",
    "instance_not_found": "目标项目岗位不可用。",
    "binding_unresolved": "节点岗位尚未解析。",
    "multiple_instances_choice_required": "存在多个岗位实例，请明确选择。",
    "revision_conflict": "对象修订已变化，请保留草稿后重新读取。",
    "binding_revision_conflict": "岗位选择已有新修订，请重新读取。",
    "workflow_revision_conflict": "流程保存修订已变化，请重新读取保存版本；不使用未保存画布作为依据。",
    "fingerprint_mismatch": "岗位内容指纹与请求不一致。",
    "request_id_reused": "同一请求标识不能更换操作人或内容。",
    "not_allowed_in_status": "对象当前状态不允许此操作。",
    "validation_failed": "岗位合同需要修正。",
    "storage_record_invalid": "岗位记录尚不能核实。",
    "candidate_projection_limit": "岗位候选公开内容超过本次读取范围。",
    "candidate_read_limit": "岗位候选读取超过本次范围，请缩小范围或稍后再试。",
    "storage_path_invalid": "岗位资产路径不符合安全边界。",
    "storage_busy": "岗位资产正被另一保存操作占用。",
    "publication_index_invalid": "岗位发布索引尚不能核实。",
    "publication_index_unverified": "岗位当前发布依据尚不能核实。",
    "immutable_version_invalid": "岗位不可变版本尚不能核实。",
    "immutable_skill_tree_unverified": "岗位技能整树依据尚不能核实。",
    "outcome_unknown": "保存结果未知，请只读核实原请求，不要换请求标识重做。",
    "unsupported": "此员工动作尚未接入。",
    "model_catalogue_unavailable": "模型目录暂不可用。",
    "model_catalogue_changed": "模型目录已变化，请保留修改后重新读取候选。",
    "model_unknown": "模型不在当前候选目录中。",
    "model_retired": "该模型已退役，不能新选择。",
    "model_overseas_off": "该模型当前尚未开放新选择。",
    "model_not_routable": "该模型的调用路径尚未接通。",
    "model_needs_params": "该模型需要当前编辑器尚未支持的运行参数。",
    "model_needs_trial": "该模型需要独立的工具试验依据。",
}
# These are explicit domain precondition refusals, not arbitrary I/O failures.
_PRE_EFFECT = frozenset({"owner_required", "request_invalid", "revision_conflict", "binding_revision_conflict", "workflow_revision_conflict",
    "fingerprint_mismatch", "request_id_reused", "not_allowed_in_status",
    "validation_failed", "package_not_found", "version_not_available", "instance_not_found", "unsupported"})
_MODEL_PRE_EFFECT = _PRE_EFFECT | frozenset({"model_catalogue_unavailable", "model_catalogue_changed",
    "candidate_projection_limit", "candidate_read_limit", *_MODEL_GAPS})
_DATA_FIELDS = frozenset({"package", "packages", "instance", "instances", "history",
    "versions", "test_passed", "pending_review", "ready", "gaps", "verified_scope",
    "effective_limits", "publication", "retired", "existing_pins_usable",
    "new_adoption_allowed", "is_current_verified", "source", "binding_revision",
    "instance_id", "instance_revision", "revision_fingerprint"})
_LOGICAL_FIELDS = frozenset({"schema_version", "package_id", "version", "name",
    "responsibility", "scope_in", "scope_out", "human_slots", "input_schema",
    "output_schema", "artifact_contract", "checklist", "skills", "prompt_refs",
    "template_refs", "capability_requirements", "model_profile", "knowledge_protocol",
    "loop_policy", "evaluation_profile", "provenance", "package_org_id", "enterprise_id",
    "instance_id", "project_id", "display_name", "params", "knowledge_refs", "model_config",
    "connector_refs", "human_assignments", "limits", "binding_slot", "reviewer_slot"})


@dataclass(frozen=True)
class AuthenticatedActor:
    """Created only by the server's authenticate(session) callback."""
    actor: str


@dataclass(frozen=True)
class AuthorizedProject:
    """Membership-resolved scope, never constructed from browser role fields."""
    project_id: str
    is_owner: bool
    # Server lifecycle observation only. Missing facts permit existing reads,
    # never a new instance write; archived projects cannot opt in from the UI.
    allow_instance_revise: bool = False
    allow_binding_selection: bool = False


def _valid(value: Any, pattern: re.Pattern) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


def _revision(value: Any) -> bool:
    # Implemented scopes have byte/content SHA revisions or integer revisions,
    # not arbitrary strings taken from an exception's diagnostic details.
    return (_valid(value, _SHA)
            or (type(value) is int and 0 <= value <= 2**53 - 1))


def _invalid() -> AIEmployeeContractError:
    return AIEmployeeContractError("request_invalid", _MESSAGES["request_invalid"])


def _bound_selection_projection(value: dict) -> None:
    """Only the new public selection ACK; old action limits are unchanged."""
    pending, nodes = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > 50_000 or depth > 64:
            raise AIEmployeeContractError("candidate_projection_limit", _MESSAGES["candidate_projection_limit"])
        if type(item) is dict:
            pending.extend((child, depth + 1) for child in item.values())
        elif type(item) is list:
            pending.extend((child, depth + 1) for child in item)
    try:
        encoded = canonical_json(value)
    except AIEmployeeContractError:
        # Public fields have already been type/whitelist checked. Preserve the
        # new fixed limit code if the shared encoder's own bound fires first.
        raise AIEmployeeContractError("candidate_projection_limit", _MESSAGES["candidate_projection_limit"]) from None
    if len(encoded) > 2 * 1024 * 1024:
        raise AIEmployeeContractError("candidate_projection_limit", _MESSAGES["candidate_projection_limit"])


def _allowed(value: Any, *, owner: bool, source: str) -> list[str]:
    if type(value) is not list or any(type(item) is not str for item in value):
        raise AIEmployeeContractError("storage_record_invalid", "Invalid server action list")
    return list(dict.fromkeys(
        item for item in value if item in SUPPORTED_ACTIONS and (owner or item not in _OWNER_READ) and (
            item not in _WRITE
            or (owner and source == "draft" and item in _PACKAGE_WRITE)
            or (owner and source == "project" and item in (_INSTANCE_WRITE | INSTANCE_ADOPTION_WRITE))
        )
    ))


def _problems(value: Any) -> list[dict[str, str]]:
    """Machine codes and root logical fields only, not exception prose/paths."""
    if type(value) is not list or len(value) > 1000:
        raise AIEmployeeContractError("storage_record_invalid", "Invalid server problems")
    result = []
    for item in value:
        if type(item) is not dict or item.get("severity") not in {"error", "warning"}:
            raise AIEmployeeContractError("storage_record_invalid", "Invalid server problem")
        code = item.get("code")
        code = code if type(code) is str and (code in _MESSAGES or code in _DIAGNOSTICS) else "validation_failed"
        path = item.get("path")
        field = re.split(r"[.\[/]", path.lstrip("/"), maxsplit=1)[0] if type(path) is str else ""
        result.append({"code": code, "severity": item["severity"],
            "path": field if field in _LOGICAL_FIELDS else "",
            "message": _MESSAGES.get(code, "岗位合同检查发现待修正项。"),
            "hint": "按错误码与字段核对服务端当前记录。"})
    return result


class EmployeeActionDispatcher:
    """Callbacks are synchronous, server-owned, and must not create runtime state.

    Published provider envelopes require real revision, effects_produced=False,
    package/packages snapshots with status published/retired, and allowed_actions.
    Draft and instance repository method signatures are the existing leaf APIs.
    binding_loader(scope, workflow_id, node_id, expected_workflow_revision) is a
    context manager yielding SavedBindingSource. Its exit rechecks saved bytes
    before any receipt is returned. Browser bindings/pins are never accepted.
    """
    def __init__(self, *, authenticate: Callable[[Any], AuthenticatedActor | None],
                 resolve_project: Callable[[AuthenticatedActor, str], AuthorizedProject | None],
                 draft_repository: Any = None, published_catalog: Any = None,
                 instance_repository: Any = None, binding_repository: Any = None,
                 binding_loader: Callable[[AuthorizedProject, str, str, str], ContextManager[SavedBindingSource]] | None = None,
                 lifecycle_service: Any = None, knowledge_service: Any = None) -> None:
        if not callable(authenticate) or not callable(resolve_project):
            raise ValueError("Explicit server authentication and project resolution are required")
        self.authenticate = authenticate
        self.resolve_project = resolve_project
        self.drafts = draft_repository
        self.catalog = published_catalog
        self.instances = instance_repository
        self.bindings = binding_repository
        self.binding_loader = binding_loader
        self.lifecycle = lifecycle_service
        self.knowledge = knowledge_service

    @staticmethod
    def _unavailable(action: str) -> str:
        return ("instance_not_found" if action.startswith("employee_instance_") else
                "binding_unresolved" if action.startswith("employee_binding_") else "package_not_found")

    @staticmethod
    def _error(action: str, request_id: str | None, project_id: str | None, code: str,
               *, write_invoked: bool = False, details: Any = None) -> dict[str, Any]:
        code = code if type(code) is str and code in _MESSAGES else ("outcome_unknown" if write_invoked else "storage_record_invalid")
        facts = details if type(details) is dict else {}
        unknown = code == "outcome_unknown" or (write_invoked and (code not in _PRE_EFFECT or facts.get("effects_produced") is True))
        effects: bool | None = None if unknown and write_invoked else False
        if unknown and write_invoked and type(facts.get("effects_produced")) is bool:
            effects = facts["effects_produced"]
        revision = facts.get("current_revision", facts.get("revision"))
        payload = {"ok": False, "action": action, "project_id": project_id,
            "request_id": request_id, "revision": revision if _revision(revision) else None,
            "result": "outcome_unknown" if unknown else "unsupported" if code == "unsupported" else "failed",
            "effects_produced": effects, "error": code, "message": _MESSAGES[code],
            "problems": [{"code": code, "severity": "error", "path": "", "message": _MESSAGES[code],
                          "hint": "保留当前内容并只读核对；不要自动重发写动作。"}], "allowed_actions": []}
        if _revision(revision):
            payload["current_revision"] = revision
        if _valid(facts.get("current_fingerprint"), _SHA):
            payload["current_fingerprint"] = facts["current_fingerprint"]
        if type(facts.get("status")) is str and facts["status"] in {"draft", "testing", "published", "retired"}:
            payload["status"] = facts["status"]
        return {"type": "ack", "payload": payload}

    @staticmethod
    def _package(view: Any, *, source: str, owner: bool, package_id: str | None = None,
                 version: str | None = None) -> dict:
        if type(view) is not dict or type(view.get("package")) is not dict:
            raise AIEmployeeContractError("storage_record_invalid", "Invalid package view")
        package = view["package"]
        if (not _valid(package.get("package_id"), _PACKAGE)
                or (package_id is not None and package["package_id"] != package_id)
                or (version is not None and package.get("version") != version)
                or package.get("status") not in ({"draft", "testing"} if source == "draft" else {"published", "retired"})):
            raise AIEmployeeContractError("package_not_found", "Invalid scoped package view")
        selected_status = view.get("publication", {}).get("status") if type(view.get("publication")) is dict else package["status"]
        if source == "published" and selected_status not in {"published", "retired"}:
            raise AIEmployeeContractError("package_not_found", "Invalid catalog status")
        if not _revision(view.get("revision")):
            raise AIEmployeeContractError("storage_record_invalid", "Missing package revision")
        if view.get("effects_produced") is not False:
            raise AIEmployeeContractError("storage_record_invalid", "Invalid package read effects")
        if source == "published" and (not _valid(view.get("fingerprint"), _SHA)
                or package.get("package_fingerprint") != view["fingerprint"]):
            raise AIEmployeeContractError("storage_record_invalid", "Missing immutable package identity")
        result = {key: value for key, value in view.items() if key in _DATA_FIELDS or key in {"revision", "fingerprint"}}
        result["problems"] = _problems(view.get("problems", []))
        result["allowed_actions"] = _allowed(view.get("allowed_actions", []), owner=owner, source=source)
        result["effects_produced"] = False
        # Retirement belongs to the status index, not an in-place edit of the
        # immutable manifest whose published status participates in its bytes.
        result["status"] = selected_status if source == "published" else package["status"]
        # No independent current index anchor means read-only, even for owner.
        if source == "published":
            result["new_adoption_allowed"] = (view.get("new_adoption_allowed") is True
                and view.get("is_current_verified") is True and selected_status == "published"
                and package["status"] != "retired")
        return result

    @staticmethod
    def _instance(view: Any, scope: AuthorizedProject, *, instance_id: str | None = None,
                   can_revise: bool = False, can_preview: bool = False,
                   can_model_configuration: bool = False, can_knowledge_configuration: bool = False) -> dict:
        if type(view) is not dict or type(view.get("instance")) is not dict:
            raise AIEmployeeContractError("storage_record_invalid", "Invalid instance view")
        instance = view["instance"]
        if (instance.get("project_id") != scope.project_id or not _valid(instance.get("instance_id"), _INSTANCE)
                or (instance_id is not None and instance["instance_id"] != instance_id)):
            raise AIEmployeeContractError("instance_not_found", "Invalid scoped instance view")
        if (type(view.get("revision")) is not int or not _revision(view["revision"]) or view["revision"] < 1
                or type(instance.get("instance_revision")) is not int
                or instance.get("instance_revision") != view["revision"]
                or not _valid(view.get("fingerprint"), _SHA)
                or instance.get("revision_fingerprint") != view["fingerprint"]
                or view.get("effects_produced") is not False):
            raise AIEmployeeContractError("storage_record_invalid", "Missing instance revision")
        result = {key: value for key, value in view.items() if key in _DATA_FIELDS or key in {"revision", "fingerprint"}}
        result["problems"] = _problems(view.get("problems", []))
        result["allowed_actions"] = _allowed(view.get("allowed_actions", []), owner=scope.is_owner, source="project")
        gaps = view.get("gaps")
        if (not can_revise or not scope.allow_instance_revise or type(gaps) is not list
                or any(gap in {"package_version_missing", "fingerprint_mismatch"} for gap in gaps if type(gap) is str)):
            result["allowed_actions"] = [item for item in result["allowed_actions"] if item not in (_INSTANCE_WRITE | INSTANCE_ADOPTION_WRITE)]
        if not can_preview:
            result["allowed_actions"] = [item for item in result["allowed_actions"] if item not in _OWNER_READ]
        if (can_model_configuration and scope.is_owner and scope.allow_instance_revise and type(gaps) is list
                and not any(gap in {"package_version_missing", "fingerprint_mismatch"} for gap in gaps if type(gap) is str)):
            result["allowed_actions"] = list(dict.fromkeys([*result["allowed_actions"], "employee_instance_model_candidates"]))
        if can_knowledge_configuration and scope.is_owner and scope.allow_instance_revise and can_revise:
            result["allowed_actions"] = list(dict.fromkeys([*result["allowed_actions"], "employee_instance_knowledge_candidates"]))
        result["effects_produced"] = False
        return result

    @staticmethod
    def _adoption_view(raw: Any, scope: AuthorizedProject, message: dict) -> dict:
        # Shared public projection contract; importing creates no repository.
        from .employee_instances import ADOPTION_PREVIEW_GAPS, ADOPTION_PREVIEW_KEYS
        def invalid() -> None:
            raise AIEmployeeContractError("storage_record_invalid", "Invalid adoption comparison")
        if type(raw) is not dict:
            invalid()
        if (raw.get("project_id") != scope.project_id or raw.get("instance_id") != message["instance_id"]
                or raw.get("package_id") != message["package_id"]
                or type(raw.get("revision")) is not int or raw["revision"] != message["expected_revision"]
                or not _valid(raw.get("fingerprint"), _SHA)
                or not _valid(raw.get("from_fingerprint"), _SHA)
                or raw.get("to_version") != message["package_version"]
                or raw.get("to_fingerprint") != message["expected_fingerprint"]
                or type(raw.get("from_version")) is not str or not 0 < len(raw["from_version"]) <= 128
                or raw["from_version"].strip() != raw["from_version"]
                or any(ord(char) < 32 or ord(char) == 127 for char in raw["from_version"])
                or raw.get("preview_scope") != "adoption_comparison"
                or raw.get("verification_scope_kind") != "declared_package_profile_not_trial_evidence"
                or raw.get("private_configuration_policy") != "preserved_not_projected"
                or raw.get("target_status") not in {"published", "retired"}
                or type(raw.get("is_current_verified")) is not bool
                or type(raw.get("new_adoption_allowed")) is not bool
                or (raw["new_adoption_allowed"] and (raw["target_status"] != "published" or raw["is_current_verified"] is not True))
                or raw.get("effects_produced") is not False):
            invalid()
        gaps = raw.get("gaps")
        if (type(gaps) is not list or len(gaps) > len(ADOPTION_PREVIEW_GAPS)
                or any(type(gap) is not str or gap not in ADOPTION_PREVIEW_GAPS for gap in gaps)
                or len(set(gaps)) != len(gaps)
                or "evaluation_source_unverified" not in gaps
                or ("publication_index_unverified" in gaps) != (raw["is_current_verified"] is False)
                or ("package_retired" in gaps) != (raw["target_status"] == "retired")):
            invalid()
        differences = raw.get("differences")
        if type(differences) is not list or len(differences) != len(ADOPTION_PREVIEW_KEYS):
            invalid()
        strings = {"scope_in", "scope_out", "prompt_refs", "declared_verified_models"}
        records = {"checklist", "skills", "template_refs", "human_slots", "capability_requirements"}
        for section, row in zip(ADOPTION_PREVIEW_KEYS, differences):
            if (type(row) is not dict or set(row) != {"section", "changed", "before", "after"}
                    or row.get("section") != section or type(row.get("changed")) is not bool):
                invalid()
            for side in ("before", "after"):
                value = row[side]
                if type(value) is not dict or set(value) - ADOPTION_PREVIEW_KEYS[section]:
                    invalid()
                for key, item in value.items():
                    if key in strings:
                        if type(item) is not list or any(type(text) is not str for text in item):
                            invalid()
                    elif key in records:
                        if type(item) is not list or any(type(record) is not dict for record in item):
                            invalid()
                    elif key == "responsibility":
                        if type(item) is not str:
                            invalid()
                    elif type(item) is not dict:
                        invalid()
            if row["changed"] != (canonical_json(row["before"]) != canonical_json(row["after"])):
                invalid()
        allowed = raw.get("allowed_actions")
        if (type(allowed) is not list or any(type(item) is not str
                or item not in {"employee_instance_detail", "employee_instance_adoption_preview"} for item in allowed)):
            invalid()
        fields = {"preview_scope", "from_version", "from_fingerprint", "to_version", "to_fingerprint",
                  "target_status", "is_current_verified", "new_adoption_allowed", "gaps", "differences",
                  "verification_scope_kind", "private_configuration_policy"}
        return {**{key: deepcopy(raw[key]) for key in fields},
                "allowed_actions": _allowed(allowed, owner=scope.is_owner, source="project")}

    @staticmethod
    def _selection_rows(value: Any) -> list[dict]:
        if type(value) is not list or len(value) > 500:
            raise AIEmployeeContractError("storage_record_invalid", "Invalid binding candidates")
        result, seen = [], set()
        for row in value:
            if (type(row) is not dict or set(row) != _CANDIDATE_KEYS
                    or not _valid(row.get("instance_id"), _INSTANCE) or row["instance_id"] in seen
                    or not _valid(row.get("package_id"), _PACKAGE)
                    or type(row.get("display_name")) is not str
                    or len(row["display_name"]) > 256
                    or any(ord(char) < 32 or ord(char) == 127 for char in row["display_name"])
                    or type(row.get("package_version")) is not str or not 0 < len(row["package_version"]) <= 128
                    or row["package_version"].strip() != row["package_version"]
                    or any(ord(char) < 32 or ord(char) == 127 for char in row["package_version"])
                    or not _valid(row.get("package_fingerprint"), _SHA)
                    or type(row.get("instance_revision")) is not int or not 1 <= row["instance_revision"] <= 2**53 - 1
                    or not _valid(row.get("revision_fingerprint"), _SHA)
                    or row.get("intake_state") not in {"open", "paused"}
                    or row.get("execution_state") not in {"active", "paused", "disabled"}
                    or type(row.get("ready")) is not bool or type(row.get("gaps")) is not list
                    or len(row["gaps"]) > 500
                    or any(not _valid(gap, _PUBLIC_GAP) for gap in row["gaps"])
                    or len(set(row["gaps"])) != len(row["gaps"])
                    or (row["ready"] and (row["gaps"] or row["intake_state"] != "open" or row["execution_state"] != "active"))):
                raise AIEmployeeContractError("storage_record_invalid", "Invalid binding candidate")
            seen.add(row["instance_id"])
            result.append(deepcopy(row))
        return result

    def _selection(self, message: dict, *, session: Any, actor: AuthenticatedActor,
                   scope: AuthorizedProject) -> dict:
        action, request_id = message["type"], message["request_id"]
        writing = action in _BINDING_WRITE
        check_only = message.get("check_only", False)
        invoked = False
        returned = False
        facts = None
        try:
            workflow_id, node_id = message.get("workflow_id"), message.get("node_id")
            workflow_revision = message.get("expected_workflow_revision")
            if (not _valid(workflow_id, _ID) or not _valid(node_id, _ID)
                    or message.get("binding_slot") != "executor" or not _valid(workflow_revision, _SHA)
                    or type(check_only) is not bool):
                raise _invalid()
            if writing and (not _valid(message.get("instance_id"), _INSTANCE)
                    or type(message.get("expected_binding_revision")) is not int
                    or not 0 <= message["expected_binding_revision"] < 2**53 - 1
                    or type(message.get("expected_instance_revision")) is not int
                    or not 1 <= message["expected_instance_revision"] <= 2**53 - 1
                    or not _valid(message.get("expected_instance_fingerprint"), _SHA)):
                raise _invalid()
            operation = getattr(self.bindings, "resolve_set" if writing else "candidates", None)
            if not callable(operation) or not callable(self.binding_loader):
                return self._error(action, request_id, scope.project_id, "unsupported")
            with self.binding_loader(scope, workflow_id, node_id, workflow_revision) as saved:
                if (type(saved) is not SavedBindingSource or saved.project_id != scope.project_id
                        or saved.workflow_id != workflow_id or saved.node_id != node_id
                        or saved.binding_slot != "executor" or saved.workflow_revision != workflow_revision
                        or saved.workflow_source not in {"distribution", "runtime"}
                        or (saved.workflow_version is not None and (type(saved.workflow_version) is not str
                            or not 0 < len(saved.workflow_version) <= 128
                            or saved.workflow_version.strip() != saved.workflow_version
                            or any(ord(char) < 32 or ord(char) == 127 for char in saved.workflow_version)))
                        or type(saved.binding) is not dict or not callable(saved.verify)):
                    raise AIEmployeeContractError("storage_record_invalid", "Invalid saved selection source")
                binding = deepcopy(saved.binding)
                binding_bytes = canonical_json(binding)

                def server_verify() -> None:
                    current = self.authenticate(session)
                    if type(current) is not AuthenticatedActor or current.actor != actor.actor:
                        raise AIEmployeeContractError("owner_required", "Current owner is required")
                    current_scope = self.resolve_project(current, scope.project_id)
                    if (type(current_scope) is not AuthorizedProject or current_scope.project_id != scope.project_id
                            or current_scope.is_owner is not True or current_scope.allow_binding_selection is not True):
                        raise AIEmployeeContractError("not_allowed_in_status", "Active owner scope is required")
                    callback = saved.verify
                    target = callback if inspect.isfunction(callback) else getattr(callback, "__call__", callback)
                    if inspect.iscoroutinefunction(target) or inspect.isasyncgenfunction(target):
                        raise AIEmployeeContractError("storage_record_invalid", "Invalid source verifier")
                    checked = callback()
                    if checked is not None:
                        if inspect.iscoroutine(checked):
                            checked.close()
                        raise AIEmployeeContractError("storage_record_invalid", "Source verification incomplete")
                    if canonical_json(saved.binding) != binding_bytes or canonical_json(binding) != binding_bytes:
                        raise AIEmployeeContractError("storage_record_invalid", "Saved selection binding changed")

                source_scope = {"workflow_id": workflow_id, "node_id": node_id, "binding_slot": "executor",
                    "workflow_revision": workflow_revision, "workflow_source": saved.workflow_source,
                    "workflow_version": saved.workflow_version, "selection_scope": "saved_node_selection"}
                server_verify()
                if writing:
                    invoked = True
                    raw = operation(scope.project_id, binding, workflow_id=workflow_id, node_id=node_id,
                        binding_slot="executor", instance_id=message["instance_id"],
                        expected_binding_revision=message["expected_binding_revision"],
                        expected_workflow_revision=workflow_revision,
                        expected_instance_revision=message["expected_instance_revision"],
                        expected_instance_fingerprint=message["expected_instance_fingerprint"],
                        actor=actor.actor, request_id=request_id, before_commit=server_verify, check_only=check_only)
                    if type(raw) is dict:
                        facts = {"effects_produced": True if raw.get("effects_produced") is True else None,
                                 "current_revision": raw.get("revision")}
                else:
                    raw = operation(scope.project_id, binding, workflow_id=workflow_id, node_id=node_id, binding_slot="executor")
                returned = True
                server_verify()
                if type(raw) is not dict:
                    raise AIEmployeeContractError("outcome_unknown" if writing else "storage_record_invalid", "Invalid selection receipt")
                if writing and raw.get("ok") is not True:
                    # Prepared/missing/invalid original receipts never become a
                    # second write, nor does their current map prove completion.
                    raise AIEmployeeContractError("outcome_unknown", "Original selection receipt is unverified", details=facts)
                revision = raw.get("binding_revision")
                if (type(revision) is not int or not 0 <= revision <= 2**53 - 1
                        or type(raw.get("revision")) is not int or raw["revision"] != revision):
                    raise AIEmployeeContractError("outcome_unknown" if writing else "storage_record_invalid", "Invalid binding revision")
                allowed = raw.get("allowed_actions")
                if (type(allowed) is not list or any(type(item) is not str for item in allowed)
                        or set(allowed) != _BINDING_SELECTION or len(allowed) != 2):
                    raise AIEmployeeContractError("outcome_unknown" if writing else "storage_record_invalid", "Invalid selection actions")
                if writing:
                    if (raw.get("action") != action or raw.get("project_id") != scope.project_id
                            or raw.get("request_id") != request_id or raw.get("result") != "success"
                            or raw.get("effects_produced") is not True or raw.get("workflow_id") != workflow_id
                            or raw.get("node_id") != node_id or raw.get("binding_slot") != "executor"
                            or raw.get("workflow_revision") != workflow_revision or raw.get("source") != "human_choice"
                            or raw.get("selection_scope") != "saved_node_selection"
                            or raw.get("instance_id") != message["instance_id"]
                            or type(raw.get("instance_revision")) is not int
                            or raw["instance_revision"] != message["expected_instance_revision"]
                            or raw.get("revision_fingerprint") != message["expected_instance_fingerprint"]
                            or revision != message["expected_binding_revision"] + 1):
                        raise AIEmployeeContractError("outcome_unknown", "Mismatched selection receipt", details=facts)
                    view = {key: raw[key] for key in ("instance_id", "instance_revision", "revision_fingerprint", "source")}
                else:
                    rows = self._selection_rows(raw.get("candidates"))
                    selected = raw.get("selected_instance_id")
                    if ("selected_instance_id" not in raw or raw.get("effects_produced") is not False or (selected is not None and (
                            not _valid(selected, _INSTANCE) or selected not in {row["instance_id"] for row in rows}))):
                        raise AIEmployeeContractError("storage_record_invalid", "Invalid selection read")
                    view = {"candidates": rows, "selected_instance_id": selected}
                payload = {**source_scope, **view, "ok": True, "action": action, "project_id": scope.project_id,
                    "request_id": request_id, "binding_revision": revision, "revision": revision,
                    "result": "success", "effects_produced": writing, "problems": [],
                    "allowed_actions": ["employee_binding_candidates", "employee_binding_resolve_set"]}
                if writing and raw.get("idempotent") is True:
                    payload["idempotent"] = True
                _bound_selection_projection({"type": "ack", "payload": payload})
            # Loader exit/root checks also precede the public success receipt.
            return {"type": "ack", "payload": payload}
        except AIEmployeeContractError as exc:
            post_commit = writing and (returned or invoked and exc.code not in _PRE_EFFECT or check_only)
            return self._error(action, request_id, scope.project_id,
                "outcome_unknown" if post_commit else exc.code, write_invoked=invoked or check_only,
                details=exc.details if exc.details is not None else facts)
        except Exception:
            return self._error(action, request_id, scope.project_id,
                "outcome_unknown" if writing and (invoked or check_only) else "storage_record_invalid",
                write_invoked=invoked or check_only, details=facts)

    @staticmethod
    def _model_candidates_view(raw: Any, message: dict) -> dict:
        """Strict finite public view, not a directory or execution authority."""
        def invalid() -> None:
            raise AIEmployeeContractError("storage_record_invalid", "Invalid model candidate projection")
        if not _model_exact_keys(raw, _MODEL_VIEW_FIELDS):
            invalid()
        if any(type(raw[field]) is not str for field in (
                "selection_scope", "catalogue_source", "projection_version", "model_catalogue_revision",
                "instance_id", "revision_fingerprint", "package_id", "package_version", "package_fingerprint", "fingerprint")):
            invalid()
        expected, fingerprint = message["expected_revision"], message["expected_fingerprint"]
        if (raw.get("selection_scope") != "instance_model_configuration"
                or raw.get("catalogue_source") != "static_model_catalogue"
                or raw.get("projection_version") != _MODEL_PROJECTION_VERSION
                or not _valid(raw.get("model_catalogue_revision"), _SHA)
                or raw.get("instance_id") != message["instance_id"]
                or type(raw.get("instance_revision")) is not int or raw["instance_revision"] != expected
                or type(raw.get("revision")) is not int or raw["revision"] != expected
                or raw.get("revision_fingerprint") != fingerprint or raw.get("fingerprint") != fingerprint
                or not _valid(raw.get("package_id"), _PACKAGE) or not _valid(raw.get("package_fingerprint"), _SHA)
                or type(raw.get("package_version")) is not str or not 0 < len(raw["package_version"]) <= 128
                or raw["package_version"].strip() != raw["package_version"]
                or any(ord(char) < 32 or 127 <= ord(char) <= 159 or 0xD800 <= ord(char) <= 0xDFFF for char in raw["package_version"])
                or raw.get("effects_produced") is not False or type(raw.get("problems")) is not list or raw["problems"]
                or type(raw.get("allowed_actions")) is not list or len(raw["allowed_actions"]) > 2
                or any(type(item) is not str or item not in {"employee_instance_model_candidates", "employee_instance_revise"}
                       for item in raw["allowed_actions"]) or len(set(raw["allowed_actions"])) != len(raw["allowed_actions"])):
            invalid()
        candidates = raw.get("candidates")
        if type(candidates) is not list or len(candidates) > 500:
            raise AIEmployeeContractError("candidate_projection_limit", _MESSAGES["candidate_projection_limit"])
        seen, pairs = set(), []
        for row in candidates:
            if not _model_exact_keys(row, {"provider", "model", "status", "availability", "availability_reason", "capabilities", "selectable", "gaps"}):
                invalid()
            config = _model_configuration({"provider": row["provider"], "model": row["model"]})
            if (type(row["status"]) is not str or row["status"] not in {"active", "vendor_routed_alias", "retired"}
                    or type(row["availability"]) is not str or row["availability"] not in {"available", "off_by_default", "not_routable", "needs_params"}
                    or type(row["availability_reason"]) is not str or row["availability_reason"] not in {
                        "", "thinking_default_tool_loop", "thinking_default", "window_cap", "no_litellm_route", "base_not_applied", "overseas_vendor"}
                    or not _model_exact_keys(row["capabilities"], {"tools", "vision", "thinking_default", "reasoning_passback_required"})
                    or any(type(value) is not bool for value in row["capabilities"].values())
                    or type(row["selectable"]) is not bool or type(row["gaps"]) is not list or len(row["gaps"]) > 1
                    or any(type(gap) is not str or gap not in _MODEL_GAPS for gap in row["gaps"])
                    or row["selectable"] != (not row["gaps"]) or config["model"] in seen):
                invalid()
            seen.add(config["model"])
            pairs.append((config["provider"], config["model"]))
        if pairs != sorted(pairs):
            invalid()
        _bound_selection_projection(raw)
        projection = {"projection_version": raw["projection_version"], "candidates": candidates}
        if hashlib.sha256(canonical_json(projection)).hexdigest() != raw["model_catalogue_revision"]:
            invalid()
        return deepcopy(raw)

    def _model_selection(self, message: dict, *, session: Any, actor: AuthenticatedActor,
                         scope: AuthorizedProject) -> dict:
        """Original instance read/revise lane with current owner guards and durable ACKs."""
        action, request_id = message["type"], message["request_id"]
        writing = action == "employee_instance_revise"
        check_only = message.get("check_only", False) if writing else False
        invoked = returned = False
        facts = None
        try:
            expected, instance_id = message.get("expected_revision"), message.get("instance_id")
            fingerprint_key = "expected_instance_fingerprint" if writing else "expected_fingerprint"
            fingerprint = message.get(fingerprint_key)
            if (not _valid(request_id, _ID) or not _valid(instance_id, _INSTANCE)
                    or type(expected) is not int or not 1 <= expected <= 2**53 - 1
                    or writing and expected == 2**53 - 1 or not _valid(fingerprint, _SHA)
                    or type(check_only) is not bool):
                raise _invalid()
            model = None
            if writing:
                changes, note = message.get("changes"), message.get("note", "")
                if (not _model_exact_keys(changes, {"model_config"})
                        or not _valid(message.get("expected_model_catalogue_revision"), _SHA)
                        or type(note) is not str or len(note) > 4000):
                    raise _invalid()
                model = _model_configuration(changes["model_config"])
            operation = getattr(self.instances, "revise_model_config" if writing else "model_candidates", None) if self.instances is not None else None
            target = operation if inspect.isfunction(operation) else getattr(operation, "__call__", operation)
            if (not callable(operation) or inspect.iscoroutinefunction(operation) or inspect.isasyncgenfunction(operation)
                    or inspect.iscoroutinefunction(target) or inspect.isasyncgenfunction(target)):
                raise AIEmployeeContractError("unsupported", _MESSAGES["unsupported"])
            message_bytes = canonical_json(message)
            model_bytes = canonical_json(model) if writing else None
            bound_actor, bound_project = actor.actor, scope.project_id

            def server_verify() -> None:
                if canonical_json(message) != message_bytes or writing and canonical_json(model) != model_bytes:
                    raise _invalid()
                current = self.authenticate(session)
                if type(current) is not AuthenticatedActor or type(current.actor) is not str or current.actor != bound_actor:
                    raise AIEmployeeContractError("owner_required", _MESSAGES["owner_required"])
                current_scope = self.resolve_project(current, bound_project)
                if (type(current_scope) is not AuthorizedProject or not _valid(current_scope.project_id, _ID)
                        or current_scope.project_id != bound_project
                        or current_scope.is_owner is not True or current_scope.allow_instance_revise is not True
                        or type(current_scope.allow_binding_selection) is not bool):
                    raise AIEmployeeContractError("not_allowed_in_status", _MESSAGES["not_allowed_in_status"])
                if (current.actor != bound_actor or canonical_json(message) != message_bytes
                        or writing and canonical_json(model) != model_bytes):
                    raise _invalid()

            server_verify()
            invoked = True
            if writing:
                raw = operation(bound_project, instance_id, model, expected_revision=expected,
                    expected_instance_fingerprint=fingerprint,
                    expected_model_catalogue_revision=message["expected_model_catalogue_revision"],
                    actor=bound_actor, request_id=request_id, note=note, before_commit=server_verify, check_only=check_only)
                returned = True
                facts = {"effects_produced": True if type(raw) is dict and raw.get("effects_produced") is True else None}
                server_verify()
                if type(raw) is not dict or raw.get("ok") is not True or raw.get("result") != "success":
                    return self._error(action, request_id, bound_project, "outcome_unknown", write_invoked=True, details=facts)
                echoed = _model_configuration(raw.get("model_config"))
                retest = raw.get("retest_scope")
                if (any(type(raw.get(field)) is not str for field in ("action", "project_id", "request_id", "instance_id", "fingerprint", "model_catalogue_revision"))
                        or raw.get("action") != action or raw.get("project_id") != bound_project or raw.get("request_id") != request_id
                        or raw.get("instance_id") != instance_id or type(raw.get("revision")) is not int or raw["revision"] != expected + 1
                        or not _valid(raw.get("fingerprint"), _SHA) or raw.get("effects_produced") is not True
                        or raw.get("model_catalogue_revision") != message["expected_model_catalogue_revision"]
                        or canonical_json(echoed) != model_bytes or type(retest) is not list
                        or not (not retest or len(retest) == 1 and type(retest[0]) is str and retest[0] == "model_config")
                        or type(raw.get("problems")) is not list or raw["problems"]):
                    raise AIEmployeeContractError("outcome_unknown", _MESSAGES["outcome_unknown"])
                view = {"model_config": echoed, "model_catalogue_revision": raw["model_catalogue_revision"], "retest_scope": list(retest)}
                allowed = _allowed(raw.get("allowed_actions", []), owner=True, source="project")
            else:
                raw = operation(bound_project, instance_id, expected_revision=expected,
                    expected_fingerprint=fingerprint, before_read=server_verify)
                server_verify()
                view = self._model_candidates_view(raw, message)
                allowed = list(view["allowed_actions"])
            payload = {**view, "ok": True, "action": action, "project_id": bound_project, "request_id": request_id,
                "instance_id": instance_id, "revision": raw["revision"], "fingerprint": raw["fingerprint"], "result": "success",
                "effects_produced": True if writing else False, "problems": [], "allowed_actions": allowed}
            if writing and raw.get("idempotent") is True:
                payload["idempotent"] = True
            _bound_selection_projection({"type": "ack", "payload": payload})
            server_verify()
            return {"type": "ack", "payload": payload}
        except AIEmployeeContractError as exc:
            # Only the original repository's pre-effect domain refusals are safe
            # failures. No provider's false flag can turn an unknown into failure.
            pre_effect = not returned and not check_only and exc.code in _MODEL_PRE_EFFECT
            return self._error(action, request_id, scope.project_id,
                exc.code if not writing or pre_effect else "outcome_unknown",
                write_invoked=writing and (check_only or invoked and not pre_effect), details=facts)
        except Exception:
            return self._error(action, request_id, scope.project_id,
                "outcome_unknown" if writing and (invoked or check_only) else "storage_record_invalid",
                write_invoked=writing and (invoked or check_only), details=facts)

    def dispatch(self, message: Any, *, session: Any) -> dict[str, Any]:
        action = message.get("type", "") if type(message) is dict else ""
        action = action if _valid(action, _ACTION) else ""
        request_id = message.get("request_id") if type(message) is dict else None
        request_id = request_id if _valid(request_id, _ID) else None
        scope: AuthorizedProject | None = None
        write_invoked = False
        known_write_facts: dict | None = None
        binding_scope: dict | None = None
        limits_write = False
        model_write = (action == "employee_instance_revise" and type(message) is dict
            and _model_exact_keys(message.get("changes"), {"model_config"}))
        recovering_selection = (action in _BINDING_WRITE or model_write) and type(message) is dict and message.get("check_only") is True

        def scope_error(code: str, project_id: str | None) -> dict:
            return self._error(action, request_id, project_id, "outcome_unknown" if recovering_selection else code,
                               write_invoked=recovering_selection)

        try:
            if type(message) is not dict:
                raise _invalid()
            try:
                message_bytes = canonical_json(message)  # Bounded strict JSON, no aliases/cycles/NaN.
            except AIEmployeeContractError as exc:
                raise _invalid() from exc
            if not action or not _valid(message.get("project_id"), _ID):
                raise _invalid()
            if "request_id" in message and request_id is None:
                raise _invalid()
            try:
                actor = self.authenticate(session)
                if (type(actor) is not AuthenticatedActor or type(actor.actor) is not str
                        or not 0 < len(actor.actor) <= 128 or actor.actor.strip() != actor.actor
                        or any(ord(char) < 32 or ord(char) == 127 for char in actor.actor)):
                    return scope_error(self._unavailable(action), None)
                candidate = self.resolve_project(actor, message["project_id"])
                if (type(candidate) is not AuthorizedProject or not _valid(candidate.project_id, _ID)
                        or type(candidate.is_owner) is not bool or type(candidate.allow_instance_revise) is not bool
                        or type(candidate.allow_binding_selection) is not bool
                        or candidate.project_id != message["project_id"]):
                    return scope_error(self._unavailable(action), None)
                scope = candidate
            except Exception:
                return scope_error(self._unavailable(action), None)
            if action not in SUPPORTED_ACTIONS:
                return self._error(action, request_id, scope.project_id, "unsupported")
            source = message.get("source", "published")
            if action in _WRITE or action in _OWNER_READ or source == "draft":
                if not scope.is_owner:
                    return scope_error("owner_required", scope.project_id)
            if set(message) - (_COMMON | _FIELDS[action]):
                raise _invalid()
            if action in KNOWLEDGE_FIELDS:
                if request_id is None:
                    raise _invalid()
                def verify_knowledge() -> None:
                    current = self.authenticate(session)
                    if (type(current) is not AuthenticatedActor or current.actor != actor.actor
                            or canonical_json(message) != message_bytes):
                        raise AIEmployeeContractError("owner_required", "Current owner is required")
                    current_scope = self.resolve_project(current, scope.project_id)
                    if (type(current_scope) is not AuthorizedProject or current_scope.project_id != scope.project_id
                            or current_scope.is_owner is not True):
                        raise AIEmployeeContractError("owner_required", "Current owner is required")
                    if (action in KNOWLEDGE_WRITE and message.get("check_only") is not True
                            and current_scope.allow_instance_revise is not True):
                        raise AIEmployeeContractError("not_allowed_in_status", "Active owner scope is required")
                return dispatch_knowledge_action(message, service=self.knowledge, actor=actor.actor,
                    project_id=scope.project_id, verify=verify_knowledge)
            if action in INSTANCE_ADOPTION_FIELDS:
                if not scope.is_owner or request_id is None:
                    return scope_error("owner_required" if not scope.is_owner else "request_invalid", scope.project_id)
                def verify_adoption() -> None:
                    current = self.authenticate(session)
                    if (type(current) is not AuthenticatedActor or current.actor != actor.actor
                            or canonical_json(message) != message_bytes):
                        raise AIEmployeeContractError("owner_required", _MESSAGES["owner_required"])
                    current_scope = self.resolve_project(current, scope.project_id)
                    if (type(current_scope) is not AuthorizedProject or current_scope.project_id != scope.project_id
                            or current_scope.is_owner is not True
                            or message.get("check_only") is not True and current_scope.allow_instance_revise is not True):
                        raise AIEmployeeContractError("not_allowed_in_status", _MESSAGES["not_allowed_in_status"])
                return dispatch_instance_adoption_action(message, repository=self.instances, actor=actor.actor,
                    project_id=scope.project_id, verify=verify_adoption)
            if action in LIFECYCLE_FIELDS:
                if not scope.is_owner or request_id is None:
                    return scope_error("owner_required" if not scope.is_owner else "request_invalid", scope.project_id)
                def verify_lifecycle() -> None:
                    current = self.authenticate(session)
                    if (type(current) is not AuthenticatedActor or current.actor != actor.actor
                            or canonical_json(message) != message_bytes):
                        raise AIEmployeeContractError("owner_required", _MESSAGES["owner_required"])
                    current_scope = self.resolve_project(current, scope.project_id)
                    if (type(current_scope) is not AuthorizedProject or current_scope.project_id != scope.project_id
                            or current_scope.is_owner is not True
                            or action in LIFECYCLE_WRITE and current_scope.allow_instance_revise is not True):
                        raise AIEmployeeContractError("not_allowed_in_status", _MESSAGES["not_allowed_in_status"])
                return dispatch_lifecycle_action(message, service=self.lifecycle, actor=actor.actor,
                    project_id=scope.project_id, verify=verify_lifecycle)
            if action == "employee_instance_revise" and not model_write and set(message) & _MODEL_HEADERS:
                raise _invalid()  # Old name/limits message and fingerprint rules stay exact.
            if action in _INSTANCE_WRITE and not scope.allow_instance_revise:
                if model_write:
                    return scope_error("not_allowed_in_status", scope.project_id)
                return self._error(action, request_id, scope.project_id, "not_allowed_in_status")
            if action == "employee_instance_model_candidates" or model_write:
                if request_id is None:
                    raise _invalid()
                return self._model_selection(message, session=session, actor=actor, scope=scope)
            if action in _BINDING_SELECTION and not scope.allow_binding_selection:
                return scope_error("not_allowed_in_status", scope.project_id)
            if action in _BINDING_SELECTION:
                if request_id is None:
                    raise _invalid()
                return self._selection(message, session=session, actor=actor, scope=scope)
            if action.startswith("employee_package_") and action not in _WRITE and (type(source) is not str or source not in {"draft", "published"}):
                raise _invalid()
            if action in _WRITE and request_id is None:
                raise _invalid()
            package_id = message.get("package_id")
            instance_id = message.get("instance_id")
            version = message.get("version")
            if action in {"employee_package_detail", "employee_package_draft_save"} and not _valid(package_id, _PACKAGE):
                raise _invalid()
            if version is not None and (type(version) is not str or not 0 < len(version) <= 128
                                       or version.strip() != version or any(ord(char) < 32 for char in version)):
                raise _invalid()
            if action in {"employee_instance_detail", "employee_instance_revise", "employee_instance_adoption_preview"} and not _valid(instance_id, _INSTANCE):
                raise _invalid()
            provider = self.drafts if source == "draft" or action in _PACKAGE_WRITE else self.catalog
            operation = None
            if action.startswith("employee_package_"):
                operation = getattr(provider, _PACKAGE_METHODS[action], None) if provider is not None else None
                if not callable(operation):
                    return self._error(action, request_id, scope.project_id, "unsupported")
            if action == "employee_package_list":
                raw = operation(owner=True) if source == "draft" else operation()
                if type(raw) is not dict or type(raw.get("packages")) is not list or len(raw["packages"]) > 500:
                    raise AIEmployeeContractError("storage_record_invalid", "Invalid package list")
                view = {"packages": [self._package(item, source=source, owner=scope.is_owner) for item in raw["packages"]]}
                if source == "draft" and "templates" in raw:
                    templates = raw["templates"]
                    if type(templates) is not list or len(templates) > 16 or any(type(item) is not dict
                            or item.get("persisted") is not False or not _valid(item.get("template_id"), _PACKAGE)
                            or not _valid(item.get("source_revision"), _SHA) for item in templates):
                        raise AIEmployeeContractError("storage_record_invalid", "Invalid template catalog")
                    view["templates"] = deepcopy(templates)
            elif action == "employee_package_detail":
                raw = operation(package_id, version=version, owner=True) if source == "draft" else operation(package_id, version=version)
                view = self._package(raw, source=source, owner=scope.is_owner, package_id=package_id, version=version)
                if source == "published" and version is None and view["status"] != "published":
                    raise AIEmployeeContractError("version_not_available", "Default detail requires the latest published version")
            elif action == "employee_package_draft_create":
                manifest = message.get("manifest")
                if type(manifest) is not dict or not _valid(manifest.get("package_id"), _PACKAGE):
                    raise _invalid()
                package_id = manifest["package_id"]
                template = message.get("template_id")
                if "workflow_source" in message and (template != "saved-workflow" or type(message["workflow_source"]) is not dict):
                    raise _invalid()
                if "template_id" in message or "expected_template_revision" in message:
                    if not _valid(template, _PACKAGE) or not _valid(message.get("expected_template_revision"), _SHA):
                        raise _invalid()
                    operation = getattr(provider, "create_from_template", None)
                    if not callable(operation):
                        return self._error(action, request_id, scope.project_id, "unsupported")
                write_invoked = True
                raw = operation(manifest, project_id=scope.project_id, actor=actor.actor, request_id=request_id,
                    **({"template_id": template, "expected_template_revision": message["expected_template_revision"]} if template is not None else {}),
                    **({"workflow_source": deepcopy(message["workflow_source"])} if "workflow_source" in message else {}))
                view = {}
            elif action == "employee_package_draft_save":
                if type(message.get("changes")) is not dict or not _valid(message.get("expected_revision"), _SHA):
                    raise _invalid()
                write_invoked = True
                raw = operation(package_id, message["changes"], expected_revision=message["expected_revision"],
                                    project_id=scope.project_id, actor=actor.actor, request_id=request_id)
                view = {}
            elif action in {"employee_instance_list", "employee_instance_detail"}:
                operation = getattr(self.instances, "list" if action == "employee_instance_list" else "detail", None) if self.instances is not None else None
                if not callable(operation):
                    return self._error(action, request_id, scope.project_id, "unsupported")
                can_revise = callable(getattr(self.instances, "revise", None))
                can_preview = callable(getattr(self.instances, "adoption_preview", None))
                if action == "employee_instance_list":
                    raw = operation(scope.project_id, owner=scope.is_owner)
                    if type(raw) is not dict or type(raw.get("instances")) is not list or len(raw["instances"]) > 500:
                        raise AIEmployeeContractError("storage_record_invalid", "Invalid instance list")
                    can_model_configuration = (getattr(self.instances, "model_configuration_available", False) is True
                        and callable(getattr(self.instances, "model_candidates", None))
                        and callable(getattr(self.instances, "revise_model_config", None)))
                    view = {"instances": [self._instance(item, scope, can_revise=can_revise, can_preview=can_preview,
                        can_model_configuration=can_model_configuration,
                        can_knowledge_configuration=self.knowledge is not None) for item in raw["instances"]]}
                else:
                    raw = operation(scope.project_id, instance_id, owner=scope.is_owner)
                    can_model_configuration = (getattr(self.instances, "model_configuration_available", False) is True
                        and callable(getattr(self.instances, "model_candidates", None))
                        and callable(getattr(self.instances, "revise_model_config", None)))
                    view = self._instance(raw, scope, instance_id=instance_id, can_revise=can_revise, can_preview=can_preview,
                        can_model_configuration=can_model_configuration, can_knowledge_configuration=self.knowledge is not None)
            elif action == "employee_instance_adoption_preview":
                expected = message.get("expected_revision")
                target_version = message.get("package_version")
                target_fingerprint = message.get("expected_fingerprint")
                if (request_id is None or not _valid(package_id, _PACKAGE)
                        or type(expected) is not int or not 1 <= expected <= 2**53 - 1
                        or type(target_version) is not str or not 0 < len(target_version) <= 128
                        or target_version.strip() != target_version
                        or any(ord(char) < 32 or ord(char) == 127 for char in target_version)
                        or not _valid(target_fingerprint, _SHA)):
                    raise _invalid()
                operation = getattr(self.instances, "adoption_preview", None) if self.instances is not None else None
                if not callable(operation):
                    return self._error(action, request_id, scope.project_id, "unsupported")
                raw = operation(scope.project_id, instance_id, expected_revision=expected, package_id=package_id,
                                package_version=target_version, expected_fingerprint=target_fingerprint)
                view = self._adoption_view(raw, scope, message)
            elif action == "employee_instance_revise":
                changes = message.get("changes")
                expected = message.get("expected_revision")
                note = message.get("note", "")
                limits_write = type(changes) is dict and set(changes) == {"limits"}
                if limits_write:
                    operation = getattr(self.instances, "revise_limits", None) if self.instances is not None else None
                    target = operation if inspect.isfunction(operation) else getattr(operation, "__call__", operation)
                    if (not callable(operation) or inspect.iscoroutinefunction(operation)
                            or inspect.isasyncgenfunction(operation) or inspect.iscoroutinefunction(target)
                            or inspect.isasyncgenfunction(target)):
                        return self._error(action, request_id, scope.project_id, "unsupported")
                    if (type(changes["limits"]) is not dict or type(expected) is not int or not _revision(expected)
                            or not 1 <= expected < 2**53 - 1 or type(note) is not str or len(note) > 4000):
                        raise _invalid()
                    limits = deepcopy(changes["limits"])
                    limits_bytes = canonical_json(limits)
                    effective_limits(limits)  # Strict codec preflight, never runtime authorization.
                    bound_actor, bound_project = actor.actor, scope.project_id

                    def server_verify() -> None:
                        if canonical_json(message) != message_bytes or canonical_json(limits) != limits_bytes:
                            raise _invalid()
                        current = self.authenticate(session)
                        if (type(current) is not AuthenticatedActor or type(current.actor) is not str
                                or current.actor != bound_actor):
                            raise AIEmployeeContractError("owner_required", "Current owner is required")
                        current_scope = self.resolve_project(current, bound_project)
                        if (type(current_scope) is not AuthorizedProject or not _valid(current_scope.project_id, _ID)
                                or current_scope.project_id != bound_project or current_scope.is_owner is not True
                                or current_scope.allow_instance_revise is not True
                                or type(current_scope.allow_binding_selection) is not bool):
                            raise AIEmployeeContractError("not_allowed_in_status", "Active owner scope is required")
                        if (type(current.actor) is not str or current.actor != bound_actor
                                or canonical_json(message) != message_bytes or canonical_json(limits) != limits_bytes):
                            raise _invalid()

                    server_verify()
                    write_invoked = True
                    raw = operation(bound_project, instance_id, limits, expected_revision=expected,
                                    actor=bound_actor, request_id=request_id, note=note, before_commit=server_verify)
                    if type(raw) is dict:
                        known_write_facts = {"effects_produced": True if raw.get("effects_produced") is True else None,
                                             "current_revision": raw.get("revision")}
                    try:
                        server_verify()
                    except Exception:
                        raise AIEmployeeContractError("outcome_unknown", "Current revision receipt is unverified") from None
                else:
                    operation = getattr(self.instances, "revise", None) if self.instances is not None else None
                    if not callable(operation):
                        return self._error(action, request_id, scope.project_id, "unsupported")
                    if (type(changes) is not dict or set(changes) != {"display_name"}
                            or type(changes["display_name"]) is not str or not changes["display_name"].strip()
                            or len(changes["display_name"]) > 256
                            or any(ord(char) < 32 or ord(char) == 127 for char in changes["display_name"])
                            or type(expected) is not int or not _revision(expected) or not 1 <= expected < 2**53 - 1
                            or type(note) is not str or len(note) > 4000):
                        raise _invalid()
                    # Do not accept config, package/version, states or grants. The
                    # repository loads this exact existing private adopted instance.
                    write_invoked = True
                    raw = operation(scope.project_id, instance_id, changes, expected_revision=expected,
                                    actor=actor.actor, request_id=request_id, note=note)
                view = {}
            else:
                operation = getattr(self.bindings, "preview", None) if self.bindings is not None else None
                if not callable(operation) or not callable(self.binding_loader):
                    return self._error(action, request_id, scope.project_id, "unsupported")
                workflow_id, node_id = message.get("workflow_id"), message.get("node_id")
                expected_workflow_revision = message.get("expected_workflow_revision")
                if (not _valid(workflow_id, _ID) or not _valid(node_id, _ID)
                        or message.get("binding_slot", "executor") != "executor"
                        or not _valid(expected_workflow_revision, _SHA) or request_id is None):
                    raise _invalid()
                preview_error = None
                with self.binding_loader(scope, workflow_id, node_id, expected_workflow_revision) as saved:
                    if (type(saved) is not SavedBindingSource or saved.project_id != scope.project_id
                            or saved.workflow_id != workflow_id or saved.node_id != node_id
                            or saved.binding_slot != "executor" or saved.workflow_revision != expected_workflow_revision
                            or saved.workflow_source not in {"distribution", "runtime"}
                            or (saved.workflow_version is not None and (type(saved.workflow_version) is not str
                                or not 0 < len(saved.workflow_version) <= 128
                                or saved.workflow_version.strip() != saved.workflow_version
                                or any(ord(char) < 32 or ord(char) == 127 for char in saved.workflow_version)))
                            or (saved.binding is not None and type(saved.binding) is not dict)):
                        raise AIEmployeeContractError("storage_record_invalid", "Invalid saved binding source")
                    source_scope = {"workflow_id": workflow_id, "node_id": node_id, "binding_slot": "executor",
                        "workflow_revision": saved.workflow_revision, "workflow_source": saved.workflow_source,
                        "workflow_version": saved.workflow_version, "preview_scope": "binding_resolution"}
                    try:
                        if saved.binding is None:
                            raise AIEmployeeContractError("binding_unresolved", "Saved node has no binding")
                        raw = operation(scope.project_id, deepcopy(saved.binding), workflow_id=workflow_id,
                                        node_id=node_id, binding_slot="executor")
                        if (type(raw) is not dict or type(raw.get("binding_revision")) is not int or not _revision(raw["binding_revision"])
                                or not _valid(raw.get("instance_id"), _INSTANCE)
                                or type(raw.get("instance_revision")) is not int or not _revision(raw["instance_revision"]) or raw["instance_revision"] < 1
                                or not _valid(raw.get("revision_fingerprint"), _SHA)
                                or raw.get("effects_produced") is not False or type(raw.get("ready")) is not bool
                                or type(raw.get("gaps")) is not list or len(raw["gaps"]) > 1000
                                or any(type(gap) is not str or not 0 < len(gap) <= 256 for gap in raw["gaps"])
                                or raw.get("source") not in {"human_choice", "single_candidate"}
                                or raw.get("resolution_basis") not in {"stored_mapping", "single_candidate"}):
                            raise AIEmployeeContractError("storage_record_invalid", "Invalid binding preview revision")
                        canonical_json(raw)
                    except AIEmployeeContractError as exc:
                        preview_error = exc
                # Source provenance becomes usable ONLY after context exit has
                # rechecked both roots and every candidate/override manifest.
                binding_scope = source_scope
                if preview_error is not None:
                    raise preview_error
                view = {**{key: value for key, value in raw.items() if key in _DATA_FIELDS},
                        **binding_scope, "resolution_basis": raw["resolution_basis"]}
                raw = {**raw, "revision": raw["binding_revision"]}
            if type(raw) is not dict:
                raise AIEmployeeContractError("storage_record_invalid", "Invalid server response")
            if action in _WRITE:
                if (raw.get("action") != action or raw.get("request_id") != request_id
                        or (action in _PACKAGE_WRITE and raw.get("package_id") != package_id)
                        or (action in _INSTANCE_WRITE and (raw.get("project_id") != scope.project_id
                            or raw.get("instance_id") != instance_id))):
                    raise AIEmployeeContractError("outcome_unknown", "Mismatched write receipt")
                known_write_facts = {"effects_produced": (True if raw.get("effects_produced") is True else None)
                                        if limits_write else raw.get("effects_produced"),
                                     "current_revision": raw.get("revision")}
                if raw.get("ok") is not True:
                    return self._error(action, request_id, scope.project_id, raw.get("error", "outcome_unknown"),
                                       write_invoked=True, details=known_write_facts if limits_write else raw)
                if raw.get("result") != "success" or type(raw.get("effects_produced")) is not bool:
                    raise AIEmployeeContractError("outcome_unknown", "Unverified write outcome")
                if limits_write and raw.get("effects_produced") is not True:
                    raise AIEmployeeContractError("outcome_unknown", "Unverified limits write outcome")
                if action in _INSTANCE_WRITE and (type(raw.get("revision")) is not int
                        or not _revision(raw["revision"]) or raw["revision"] != message["expected_revision"] + 1
                        or not _valid(raw.get("fingerprint"), _SHA)):
                    raise AIEmployeeContractError("outcome_unknown", "Missing instance revision identity")
                if action in _INSTANCE_WRITE:
                    retest = raw.get("retest_scope")
                    if (type(retest) is not list or (limits_write and not (
                            not retest or len(retest) == 1 and type(retest[0]) is str and retest[0] == "limits"))
                            or (not limits_write and retest)):
                        raise AIEmployeeContractError("outcome_unknown", "Unverified instance revision receipt")
                    view = {"retest_scope": raw["retest_scope"]}
            elif raw.get("effects_produced") is not False:
                raise AIEmployeeContractError("storage_record_invalid", "Read provider violated its contract")
            if not _revision(raw.get("revision")):
                raise AIEmployeeContractError("outcome_unknown" if write_invoked else "storage_record_invalid", "Missing real revision")
            payload = {**view, "ok": True, "action": action, "project_id": scope.project_id,
                "request_id": request_id, "revision": raw["revision"], "result": "success",
                "effects_produced": raw["effects_produced"] if write_invoked else False,
                "problems": _problems(raw.get("problems", [])),
                "allowed_actions": _allowed(view.get("allowed_actions", raw.get("allowed_actions", [])), owner=scope.is_owner,
                                            source="draft" if action in _PACKAGE_WRITE else "project" if action.startswith("employee_instance_") else source)}
            for field, value in (("package_id", package_id), ("instance_id", instance_id),
                                 ("workflow_id", message.get("workflow_id")), ("node_id", message.get("node_id"))):
                if value is not None:
                    payload[field] = value
            if action.startswith("employee_instance_"):
                # Read grants must describe currently wired owner gestures,
                # not just a repository method or an old compatibility label.
                usable = {
                    "employee_instance_create": callable(getattr(self.instances, "create", None)),
                    "employee_instance_adopt": callable(getattr(self.instances, "adopt", None)),
                }
                def current_grants(actions):
                    return [item for item in actions if item not in INSTANCE_ADOPTION_WRITE
                            or (scope.allow_instance_revise and usable.get(item) is True)]
                payload["allowed_actions"] = current_grants(payload["allowed_actions"])
                for item in payload.get("instances", []):
                    item["allowed_actions"] = current_grants(item["allowed_actions"])
            if raw.get("fingerprint") is not None:
                if not _valid(raw["fingerprint"], _SHA):
                    raise AIEmployeeContractError("storage_record_invalid", "Invalid server fingerprint")
                payload["fingerprint"] = raw["fingerprint"]
            if raw.get("idempotent") is True:
                payload["idempotent"] = True
            # This implementation's preview is resolution/readiness only; no
            # frozen runtime execution view or new single-candidate mapping.
            if action == "employee_binding_preview":
                payload["preview_scope"] = "binding_resolution"
            canonical_json(payload)  # Bound provider output before returning it.
            return {"type": "ack", "payload": payload}
        except AIEmployeeContractError as exc:
            if model_write and recovering_selection:
                return self._error(action, request_id, scope.project_id if scope else None,
                    "outcome_unknown", write_invoked=True)
            code = self._unavailable(action) if exc.code == "project_scope_denied" else exc.code
            details = exc.details if exc.details is not None else known_write_facts
            if limits_write and write_invoked and type(details) is dict:
                details = {**details, "effects_produced": True if details.get("effects_produced") is True else None}
            reply = self._error(action, request_id, scope.project_id if scope else None, code,
                                write_invoked=write_invoked, details=details)
            if binding_scope is not None:
                reply["payload"].update(binding_scope)
            return reply
        except Exception:
            if model_write and recovering_selection:
                return self._error(action, request_id, scope.project_id if scope else None,
                    "outcome_unknown", write_invoked=True)
            return self._error(action, request_id, scope.project_id if scope else None,
                               "outcome_unknown" if write_invoked else "storage_record_invalid", write_invoked=write_invoked,
                               details=known_write_facts)


