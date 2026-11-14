"""Build an AE00 customer-service draft from bounded, host-loaded source bytes.

Pure asset authoring: no paths, clocks, registry, storage or execution. Actor,
time, budgets and capability inventory come from the trusted host. A candidate
hash describes content; it does not authenticate those host facts or publish it.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from typing import Any

PACKAGE_ID = "customer-service"
PACKAGE_VERSION = "0.3.0"
UNDERSTANDING_PACKAGE_VERSION = "0.4.0"
SKILL_NAME = "customer-service"
MAX_ASSET_BYTES = 128 * 1024
MAX_TOTAL_BYTES = 1024 * 1024
ASSET_NAMES = frozenset({"package.draft.json", "conversation-system.md",
    "conversation-playbook.md", "conversation-response.schema.json",
    "knowledge-contract.json", "taskset.json"})
MANAGED_FIELDS = frozenset({"package_fingerprint", "draft_revision", "status",
    "created_at", "created_by", "published_at", "published_by", "retired_at",
    "retired_by", "retired_reason", "release_approval", "updated_at", "updated_by",
    "latest_test", "evaluation_results", "evaluation_records", "evaluations",
    "test_results", "test_passed", "pending_review"})
_MONEY = re.compile(r"(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,6})?\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_CHANNELS = ["console", "wecom_cs", "wechat_official", "dingtalk", "email", "qianniu"]


class PackageBuildError(ValueError):
    """Bounded authoring failure; never include source/customer contents."""

    def __init__(self, code: str, *, findings: list[dict[str, Any]] | None = None):
        super().__init__(code)
        self.code = code
        self.findings = deepcopy(findings or [])


@dataclass(frozen=True)
class DraftPackageBundle:
    """In-memory candidate, not a saved/published package or ready instance."""

    manifest: dict[str, Any]
    files: dict[str, bytes]
    create_payload: dict[str, Any]
    skill_tree_files: dict[str, bytes]
    draft_findings: tuple[dict[str, Any], ...]
    admission_findings: tuple[dict[str, Any], ...]
    evaluation_state: str = "not_run"
    publication_state: str = "not_requested"
    effects_produced: bool = False


def _contract(contract: Any) -> Any:
    if contract is None:
        from ampuly.plugins.office_ui import ai_employee_contract
        contract = ai_employee_contract
    if any(not callable(getattr(contract, name, None)) for name in
           ("canonical_json", "validate_package_manifest", "package_fingerprint")):
        raise PackageBuildError("engine_contract_unavailable")
    return contract


def _duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise PackageBuildError("source_json_duplicate")
        value[key] = item
    return value


def _decode(raw: bytes, contract: Any) -> dict[str, Any]:
    try:
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_duplicates,
                         parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        contract.canonical_json(obj)  # Real AE00 bounded shape/depth validation.
    except PackageBuildError:
        raise
    except (ValueError, UnicodeError, RecursionError, TypeError):
        raise PackageBuildError("source_json_invalid") from None
    if type(obj) is not dict:
        raise PackageBuildError("source_json_invalid")
    return obj


def _object(properties: dict[str, Any], *, required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required,
            "additionalProperties": False}


def _text(maximum: int = 128) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": maximum}


def _array(items: Any, maximum: int, minimum: int = 0) -> dict[str, Any]:
    return {"type": "array", "items": items, "minItems": minimum, "maxItems": maximum}


def conversation_input_schema() -> dict[str, Any]:
    """Trusted host input; public clients must never supply context or signals."""
    context = {key: _text() for key in ("tenant_id", "shop_id", "seller_id",
                                      "buyer_id", "conversation_id", "account_id")}
    context["channel"] = {"enum": list(_CHANNELS)}
    schema = _object({
        "context": _object(context),
        "turns": _array(_object({"id": _text(), "role": {"enum": ["customer", "assistant"]},
                                  "text": _text(2000)}), 16, 1),
        "verified_customer": {"type": "boolean"},
        "signals": _object({"failed_attempts": {"type": "integer", "minimum": 0, "maximum": 10},
                            "human_requested": {"type": "boolean"},
                            "safety_risk": {"type": "boolean"},
                            "complaint_open": {"type": "boolean"}}),
        "active_order_id": {"type": ["string", "null"],
                            "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{1,63}$"},
    })
    schema.update({"$schema": "https://json-schema.org/draft/2020-12/schema",
                   "title": "CustomerServiceConversationInputV1",
                   "$comment": "Host authenticates all context fields, history and signals. "
                   "Executor also checks unique turn IDs, last customer turn and total 24000 characters."})
    return schema


def conversation_output_schema(response: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    """Whole ConversationService result, not just the model's response schema."""
    text = _text(400)
    claim = deepcopy(response["properties"]["needs"]["items"])
    citations = deepcopy(response["properties"]["citations"])
    conversation = deepcopy(conversation_input_schema()["properties"]["turns"])
    source_array = _array({"$ref": "#/$defs/Source"}, 14)
    handoff = _object({
        "required": {"type": "boolean"}, "status": {"enum": ["pending_assignment", "not_requested"]},
        "owner": {"type": "null"}, "due_at": {"type": "null"},
        "priority": {"enum": ["routine", "high", "urgent"]},
        "reasons": _array(_text(200), 32),
        "customer_statements": _array(claim, 24), "hypotheses": _array(deepcopy(claim), 24),
        "evidence_backed_facts": citations, "sources": deepcopy(source_array),
        "unknowns": deepcopy(response["properties"]["missing_information"]),
        "problem_summary": deepcopy(response["properties"]["after_sales"]),
        "suggested_team": _text(100), "next_steps": _array(text, 5),
        "conversation": conversation, "executed_actions": {"type": "array", "maxItems": 0},
        "pending_actions": _array(_text(400), 8),
    })
    schema = _object({
        "kind": {"const": "ampuly.cs.conversation-draft/1"},
        "context_key": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "history_hash": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "result_status": {"enum": ["draft", "fallback", "denied"]},
        "mode": {"enum": ["model-candidate", "safe-fallback"]},
        "draft": deepcopy(response), "requires_human": {"type": "boolean"},
        "review_required": {"const": True}, "sources": source_array, "handoff": handoff,
        "effects_produced": {"type": "array", "maxItems": 0}, "case_status": {"const": "open"},
        "model_calls": {"type": "integer", "minimum": 0, "maximum": 1},
        "validation_errors": _array(_text(128), 32), "customer_claims_verified": {"const": False},
        "semantic_quality_verified": {"const": False},
    })
    schema.update({"$schema": "https://json-schema.org/draft/2020-12/schema",
                   "title": "CustomerServiceConversationDraftV1", "$defs": {"Source": deepcopy(source)}})
    schema["allOf"] = [{"if": {"properties": {"result_status": {"const": "denied"}},
                               "required": ["result_status"]},
                        "then": {"properties": {"requires_human": {"const": True},
                                                "sources": {"maxItems": 0}}}}]
    return schema


def _source_assets(assets: Any, skill_bytes: Any, contract: Any) -> tuple[dict[str, bytes], dict[str, Any]]:
    if type(assets) is not dict or set(assets) != ASSET_NAMES:
        raise PackageBuildError("source_assets_incomplete")
    if type(skill_bytes) is not bytes or not skill_bytes.strip() or len(skill_bytes) > MAX_ASSET_BYTES:
        raise PackageBuildError("skill_resource_invalid")
    source = dict(assets)
    source["skills/customer-service/SKILL.md"] = skill_bytes
    if any(type(raw) is not bytes or not raw.strip() or len(raw) > MAX_ASSET_BYTES for raw in source.values()):
        raise PackageBuildError("source_asset_limit")
    if sum(len(raw) for raw in source.values()) > MAX_TOTAL_BYTES:
        raise PackageBuildError("source_asset_limit")
    decoded = {name: _decode(raw, contract) for name, raw in source.items() if name.endswith(".json")}
    for name, raw in source.items():
        if name.endswith(".md"):
            try:
                raw.decode("utf-8")
            except UnicodeError:
                raise PackageBuildError("source_text_invalid") from None
    return source, decoded


def build_customer_service_package(*, assets: dict[str, bytes], skill_bytes: bytes,
                                   actor: str, created_at: str, max_cost_cny: str,
                                   capability_requirements: list[dict[str, Any]] | None = None,
                                   available_capabilities: dict[str, set[str]] | None = None,
                                   contract: Any = None,
                                   structured_understanding: bool = False) -> DraftPackageBundle:
    """Construct only a draft; all arguments belong to an internal trusted host.

    The returned create_payload excludes AE01 managed fields. Repository.create
    supplies its own actor/time/status/hash again; it must stage the exact file
    bytes first through its safe file transaction. This helper does not save.
    """
    contract = _contract(contract)
    if type(structured_understanding) is not bool:
        raise PackageBuildError("understanding_version_invalid")
    version = UNDERSTANDING_PACKAGE_VERSION if structured_understanding else PACKAGE_VERSION
    if (type(actor) is not str or not actor.strip() or actor != actor.strip() or
            len(actor) > 256 or any(ord(c) < 32 for c in actor)):
        raise PackageBuildError("host_actor_required")
    try:
        stamp = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError, TypeError):
        raise PackageBuildError("host_timestamp_required") from None
    if type(created_at) is not str or "T" not in created_at or stamp.tzinfo is None:
        raise PackageBuildError("host_timestamp_required")
    if type(max_cost_cny) is not str or not _MONEY.fullmatch(max_cost_cny):
        raise PackageBuildError("host_budget_required")
    source, decoded = _source_assets(assets, skill_bytes, contract)
    if structured_understanding:
        # Preserve the v0.3 resource bytes on disk for existing frozen packages.
        # The v0.4 contract/prompt are real generated package bytes, covered by
        # source_revision, package_fingerprint and existing admission checks.
        from .core.understanding import UNDERSTANDING_SCHEMA, UNDERSTANDING_PROMPT
        response = deepcopy(decoded["conversation-response.schema.json"])
        try:
            if type(response["properties"]) is not dict or type(response["required"]) is not list:
                raise TypeError()
            response["properties"]["understanding"] = deepcopy(UNDERSTANDING_SCHEMA)
            response["required"].append("understanding")
        except (KeyError, TypeError):
            raise PackageBuildError("source_schema_incompatible") from None
        decoded["conversation-response.schema.json"] = response
        source["conversation-response.schema.json"] = contract.canonical_json(response)
        source["conversation-system.md"] += ("\n\n" + UNDERSTANDING_PROMPT).encode("utf-8")
    base = decoded["package.draft.json"]
    if base.get("package_id") != PACKAGE_ID or base.get("status") != "draft":
        raise PackageBuildError("source_package_identity")
    response = decoded["conversation-response.schema.json"]
    try:
        output = conversation_output_schema(response, base["output_schema"]["$defs"]["Source"])
    except (KeyError, TypeError):
        raise PackageBuildError("source_schema_incompatible") from None
    if structured_understanding:
        # Diagnostic only. Absent on an accepted draft; v0.3 schemas stay unchanged.
        output["properties"]["rejected_model_output"] = {
            "type": "string", "minLength": 1, "maxLength": 4000}
    taskset = decoded["taskset.json"]
    cases = taskset.get("cases")
    if (taskset.get("schema_version") != "ampuly.cs.conversation-cases/1" or
            type(cases) is not list or not 20 <= len(cases) <= 500 or
            any(type(case) is not dict or type(case.get("id")) is not str or
                not case["id"].strip() or len(case["id"]) > 128 for case in cases) or
            len({case["id"] for case in cases}) != len(cases)):
        raise PackageBuildError("taskset_invalid")
    requirements = [] if capability_requirements is None else deepcopy(capability_requirements)
    if type(requirements) is not list or len(requirements) > 32:
        raise PackageBuildError("capability_declarations_invalid")
    if any(type(row) is not dict or row.get("effect_kind") not in {"read", "paid"}
           for row in requirements):
        raise PackageBuildError("draft_effect_out_of_scope")
    if requirements and available_capabilities is None:
        raise PackageBuildError("host_capability_inventory_required")
    # Single-file Skill tree exactly matches the existing Engine native reader:
    # files before subdirectories; raw bytes; compact UTF-8 JSON row list.
    skill_hash = hashlib.sha256(skill_bytes).hexdigest()
    tree_hash = hashlib.sha256(json.dumps([["SKILL.md", skill_hash]], ensure_ascii=False,
                              sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    source_revision = hashlib.sha256(contract.canonical_json(
        {path: hashlib.sha256(raw).hexdigest() for path, raw in source.items()})).hexdigest()
    files = {path: raw for path, raw in source.items() if path != "package.draft.json"}
    files["conversation-input.schema.json"] = contract.canonical_json(conversation_input_schema())
    files["conversation-output.schema.json"] = contract.canonical_json(output)
    # Package skills have their own tree namespace/fingerprint. Keep the
    # bytes in the bundle, but never declare them again as ordinary resources.
    resources = {path: raw for path, raw in files.items() if not path.startswith("skills/")}
    if (any(len(raw) > MAX_ASSET_BYTES for raw in files.values()) or
            sum(len(raw) for raw in files.values()) > MAX_TOTAL_BYTES):
        raise PackageBuildError("generated_asset_limit")
    keep = ("name", "responsibility", "scope_in", "scope_out", "human_slots", "checklist",
            "knowledge_protocol")
    manifest = {key: deepcopy(base[key]) for key in keep if key in base}
    manifest.update({
        "responsibility": "依据当前项目实例获准的店铺知识与本人订单，完成多轮售前需求匹配、"
            "售后解释和人工交接，交付带引用的待审核回复草稿；不执行订单写入或外发。",
        "scope_in": ["中文多轮售前、商品问答、物流说明与售后诉求整理。",
            "只读取当前店铺和渠道获准知识；版本、覆盖或事实未知则明确保留未知。",
            "订单查询须由可信渠道核验买家，并匹配租户、店铺、买家三重归属。",
            "客户诉求、已观察事实、情绪线索与推测分开，最多两个澄清问题。",
            "每轮最多一次模型请求，回退草稿和人工交接同样保留完整输出合同。"],
        "scope_out": ["不退款、支付、取消订单、改地址、改库存或关闭业务问题。",
            "不凭客户文本、订单号、历史助手回复或来源文档自授身份与权限。",
            "不包含真实个人、企业知识值、订单正文、平台凭据或连接器实例。",
            "不自动外发、重试、启动子代理或学习客户内容；采用与能力更新另行审批。",
            "草稿完成与模型/岗位评测、人工批准、平台送达、EmployeeRun完成分别验证。"],
        "schema_version": 1, "package_id": PACKAGE_ID, "version": version,
        "status": "draft", "package_fingerprint": "0" * 64,
        "created_at": created_at, "created_by": actor, "enterprise_id": "",
        "input_schema": conversation_input_schema(), "output_schema": output,
        "artifact_contract": {"deliverables": [
            {"name": "客服多轮回复草稿", "format": "ampuly.cs.conversation-draft/1 JSON", "required": True},
            {"name": "售后与转人工交接材料", "format": "同一交付物的 handoff 对象", "required": True}],
            "acceptance_criteria": ["引用须来自本轮获准知识和本人订单，未知版本与覆盖范围显式保留。",
                "模型草稿始终 review_required=true；已审核、已送达与问题解决分别由宿主登记。",
                "退款、取消、改址仅建议；effects_produced为空，case_status保持open。",
                "输出必须通过整个多轮输出schema及引用/权限检查，不能只检验模型内部JSON。"]},
        "skills": [{"skill_name": SKILL_NAME, "version": version,
                    "tree_fingerprint": tree_hash, "source": "package"}],
        "prompt_refs": ["conversation-system.md"], "template_refs": [],
        "capability_requirements": requirements,
        "model_profile": {"generation": True, "classification": True, "tool_calling": False,
                          "structured_output": True, "vision": False, "verified_models": []},
        "loop_policy": {"max_steps": 6, "max_model_calls": 1, "max_wall_clock_s": 120,
                        "max_rework": 1, "max_retries": 0, "max_sub_agents": 0,
                        "max_cost_cny": max_cost_cny},
        "evaluation_profile": {"taskset_id": "ampuly-ai-customer-service-conversation-v0_3",
            "taskset_fingerprint": hashlib.sha256(source["taskset.json"]).hexdigest(),
            "min_samples": 20, "aggregation": "median", "hard_fail_veto": True,
            "non_inferior_rule": {"baseline": "同一冻结任务集、checker与当前模型配置的人工获准基线",
                                  "require_current_candidate_match": True, "require_independent_labels": True},
            "absolute_threshold": {"hard_fail_count": 0, "scope_isolation": 1,
                                   "identity_gate": 1, "citation_scope_consistency": 1},
            "budget": {"currency": "CNY", "max_cost_cny": max_cost_cny,
                       "require_host_budget_before_test_start": True}},
        "provenance": {"source_repo": "ampuly-customer-service-lab", "source_revision": source_revision,
            "files": [{"path": path, "kind": "text", "encoding": "utf-8"} for path in sorted(resources)],
            "license": base.get("provenance", {}).get("license", "私有岗位候选；再分发许可未确认"),
            "notices": [f"由真实来源字节构建的{version}多轮岗位草稿，未发布、未采用、未启动员工。",
                "model evaluation not_run；verified_models为空，技术mock不能成为模型语义通过证据。",
                "source_revision采用SHA-256裸64位十六进制内容地址，不声称Git提交或宿主授权。",
                "skills哈希覆盖实际单文件整树，独立宿主仍需核对安全落盘后的原字节。",
                "空capability_requirements表示本草稿未声明工具；宿主服务权限、模型预算仍需另行核验。"],
            "model_or_data_licenses": []},
    })
    if structured_understanding:
        manifest["scope_in"].append("同一次引擎模型调用中识别多意图与逐字参数，保留否定、纠正和缺失槽位；理解结果不授予执行权限。")
        manifest["artifact_contract"]["acceptance_criteria"].append(
            "draft.understanding 按意图分别保留参数与客户引文，不混合不同订单；字面校验不等于模型语义实测通过。")
    # Use the real validator and real projection, never a copied substitute.
    draft_findings = contract.validate_package_manifest(manifest,
        available_capabilities=available_capabilities, require_complete=False)
    fatal = [row for row in draft_findings if row.get("severity") == "error"]
    if fatal:
        raise PackageBuildError("draft_contract_invalid", findings=fatal)
    manifest["package_fingerprint"] = contract.package_fingerprint(manifest, resources)
    if type(manifest["package_fingerprint"]) is not str or not _SHA.fullmatch(manifest["package_fingerprint"]):
        raise PackageBuildError("engine_fingerprint_invalid")
    complete_findings = contract.validate_package_manifest(manifest,
        available_capabilities=available_capabilities, require_complete=True)
    # Empty verified_models is an intentional admission gap, not fabricated data.
    structural_errors = [row for row in complete_findings
                         if row.get("severity") == "error" and row.get("code") != "employee_package_model_unverified"]
    if structural_errors:
        raise PackageBuildError("complete_draft_contract_invalid", findings=structural_errors)
    return DraftPackageBundle(deepcopy(manifest), dict(files),
        {key: deepcopy(value) for key, value in manifest.items() if key not in MANAGED_FIELDS},
        {"SKILL.md": skill_bytes}, tuple(deepcopy(draft_findings)), tuple(deepcopy(complete_findings)))
