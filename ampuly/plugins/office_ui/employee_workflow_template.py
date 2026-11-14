"""Owner-saved workflow -> native employee package draft, and its published pin.

This is a second *source* for the existing ``employee_package_draft_create``
action, not another lifecycle. The owner names one saved workflow and the
exact tree revision they looked at, plus their own trial cases. The server
reads the workflow bytes itself, freezes them into ``workflow-snapshot.json``
and writes an ordinary native draft (prompt, taskset, skill) through the
same journaled store as the built-in template. Test, review and publication
stay the existing employee package lifecycle.

``pinned_workflow_package`` is the read side: a formal flow trial takes the
workflow only from an enterprise-published pin (``enterprise_freeze``),
re-hashes the published bytes, and runs a byte-identical copy of the frozen
snapshot. It never reads the mutable workflow draft.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any


from .ai_employee_contract import AIEmployeeContractError, canonical_json, package_fingerprint
from .ai_employee_storage import AIEmployeeFileStore
from ampuly.core.config import replace_with_retry

TEMPLATE_ID = "saved-workflow"
SNAPSHOT = "workflow-snapshot.json"
SNAPSHOT_SCHEMA = "ampuly.workflow-snapshot/1"
TASKSET_SCHEMA = "ampuly.workflow-cases/1"
SKILL = "skills/saved-workflow/SKILL.md"
PROMPT = "workflow-system.md"
TASKSET = "taskset.json"
SOURCE_REPO = "ampuly-saved-workflow"
_RESOURCES = frozenset({PROMPT, SNAPSHOT, TASKSET, SKILL})
_PACKAGE = re.compile(r"[a-z0-9][a-z0-9_-]{2,63}\Z")
_WORKFLOW = re.compile(r"[a-z0-9][a-z0-9_-]{1,63}\Z")
_FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_CASE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_MONEY = re.compile(r"(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,6})?\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
MAX_FILES = 48
MAX_BYTES = 512 * 1024
MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024
MAX_PUBLISHED_CANDIDATES = 16
MAX_CASE_TEXT = 8000


def _fail(code: str, message: str = "业务流快照不满足岗位包创建合同。") -> None:
    raise AIEmployeeContractError(code, message) from None


def _workflow_root(ampuly_home: Path, workflow_id: str) -> Path:
    if type(workflow_id) is not str or not _WORKFLOW.fullmatch(workflow_id):
        _fail("request_invalid", "请选择一条已保存的业务流。")
    # Do not resolve a link before handing the root to native safe storage.
    return Path(os.path.abspath(ampuly_home)) / "workflows" / workflow_id


def _tree_files(root: Path) -> dict[str, bytes]:
    """The saved working tree, bounded; dot entries (release/runtime state) excluded."""
    from .workflow_snapshot_io import read_workflow_tree
    return read_workflow_tree(root, max_files=MAX_FILES, max_bytes=MAX_BYTES)


def workflow_tree_revision(files: dict[str, bytes]) -> str:
    rows = [[path, hashlib.sha256(raw).hexdigest()] for path, raw in sorted(files.items())]
    return hashlib.sha256(canonical_json(rows)).hexdigest()


def saved_workflow_revision(ampuly_home: Path | str, workflow_id: str) -> str:
    return workflow_tree_revision(_tree_files(_workflow_root(Path(ampuly_home), workflow_id)))


def _snapshot_bytes(workflow_id: str, manifest: dict, files: dict[str, bytes]) -> bytes:
    body = {"schema_version": SNAPSHOT_SCHEMA, "workflow_id": workflow_id,
            "workflow_version": str(manifest.get("version") or ""),
            "tree_sha256": workflow_tree_revision(files),
            "files": [{"path": path, "sha256": hashlib.sha256(raw).hexdigest(), "text": raw.decode("utf-8")}
                      for path, raw in sorted(files.items())]}
    return canonical_json(body) + b"\n"


def _input_schema(fields: list[dict]) -> dict:
    properties, required = {}, []
    for field in fields:
        properties[field["id"]] = {"type": "string", "minLength": 1, "maxLength": MAX_CASE_TEXT}
        if field.get("required"):
            required.append(field["id"])
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def _output_schema(deliverables: list[str]) -> dict:
    return {"type": "object", "required": ["deliverables"], "additionalProperties": False, "properties": {
        "deliverables": {"type": "object", "required": list(deliverables), "additionalProperties": False,
                         "properties": {name: {"type": "string", "minLength": 1, "maxLength": 20000} for name in deliverables}},
        "notes": {"type": "string", "maxLength": 4000}}}


def _plan(package) -> tuple[list[dict], list[dict], list[str]]:
    from .workflow_flow_trial import flow_plan
    workflow = package.manifest
    plan = flow_plan(package)
    if plan["blocked"]:
        _fail("workflow_not_flow_trial_ready", "这条业务流还不能整流程试跑：" + str(plan["blocked"]))
    by_id = {str(node.get("id")): node for node in workflow.get("nodes") or [] if isinstance(node, dict)}
    steps = [by_id[node_id] for node_id in plan["order"]]
    deliverables: list[str] = []
    for node in steps:
        for item in node.get("deliverables") or []:
            if type(item) is str and item.strip() and item not in deliverables:
                deliverables.append(item)
    fields = [field for field in ((workflow.get("intake") or {}).get("fields") or [])
              if isinstance(field, dict) and type(field.get("id")) is str and _FIELD.fullmatch(field["id"])]
    if not deliverables or not fields or len(deliverables) > 16:
        _fail("workflow_not_flow_trial_ready", "业务流缺少可评测的交付物或录入字段。")
    return steps, fields, deliverables


def _prompt(workflow: dict, steps: list[dict], deliverables: list[str]) -> str:
    lines = [f"# 岗位：{workflow.get('name') or workflow.get('id')}", "",
             str(workflow.get("summary") or "").strip(), "",
             "你按下面已保存业务流的步骤顺序独立完成整件工作，只用任务输入里的材料，不编造材料里没有的事实；"
             "无法确定或无法映射的内容要明确列出原因。", "", "## 步骤"]
    for index, node in enumerate(steps, 1):
        lines.append(f"{index}. {node.get('name') or node.get('id')}（{node.get('id')}）：{str(node.get('responsibility') or '').strip()}")
        if node.get("deliverables"):
            lines.append("   交付物：" + "、".join(str(item) for item in node["deliverables"]))
    lines += ["", "## 输出", "只输出一个 JSON 对象：deliverables 里以每个交付物路径为键，值为该文件的完整正文；可选 notes 写给审核人的说明。",
              "交付物路径：" + "、".join(deliverables)]
    return "\n".join(lines).strip() + "\n"


def _cases(cases: Any, schema: dict) -> list[dict]:
    from jsonschema import Draft202012Validator
    validator = Draft202012Validator(schema)
    if type(cases) is not list or not 1 <= len(cases) <= 64:
        _fail("request_invalid", "请提供 1 到 64 条试跑样例。")
    result, seen = [], set()
    for case in cases:
        if (type(case) is not dict or set(case) - {"id", "title", "input"} or type(case.get("id")) is not str
                or not _CASE.fullmatch(case["id"]) or case["id"] in seen or type(case.get("input")) is not dict
                or not validator.is_valid(case["input"])
                or type(case.get("title", "")) is not str or len(case.get("title", "")) > 128):
            _fail("request_invalid", "试跑样例需要唯一编号，且输入与业务流录入字段一致。")
        seen.add(case["id"])
        result.append({"id": case["id"], "title": case.get("title") or case["id"], "input": deepcopy(case["input"])})
    return result


def build_workflow_package(*, ampuly_home: Path | str, workflow_id: str, package_id: str, name: str,
                           max_cost_cny: str, cases: Any, actor: str, created_at: str) -> tuple[dict, dict[str, bytes], str]:
    """Pure-ish build from the saved tree: (manifest, files, workflow tree revision)."""
    import yaml
    from .workflow_packages import WorkflowPackage
    root = _workflow_root(Path(ampuly_home), workflow_id)
    tree = _tree_files(root)
    revision = workflow_tree_revision(tree)
    try:
        workflow = yaml.safe_load(tree["workflow.yaml"])
    except yaml.YAMLError:
        _fail("workflow_snapshot_invalid", "业务流定义无法读取。")
    if not isinstance(workflow, dict):
        _fail("workflow_snapshot_invalid", "业务流定义必须是对象。")
    package = WorkflowPackage(root=root, manifest=workflow)
    if package.id != workflow_id:
        _fail("workflow_snapshot_invalid", "业务流定义与目录不一致。")
    workflow = package.manifest
    steps, fields, deliverables = _plan(package)
    input_schema, output_schema = _input_schema(fields), _output_schema(deliverables)
    taskset = canonical_json({"schema_version": TASKSET_SCHEMA, "mode": "owner-supplied",
        "workflow_id": workflow_id, "workflow_tree_sha256": revision,
        "notice": "样例由负责人提供；通过只说明本岗位包按冻结业务流产出合同交付物，质量仍须人工逐条审核。",
        "cases": _cases(cases, input_schema)}) + b"\n"
    skill = ("---\nname: saved-workflow\ndescription: 按冻结的已保存业务流逐步完成工作\n---\n\n"
             "# 按冻结业务流工作\n\n- 只依据本次任务输入与冻结的业务流步骤。\n- 每个交付物都写完整正文，不留占位。\n"
             "- 不能映射或无法确认的内容单独列出并说明原因。\n- 没有外发、写入外部系统或修改业务流的权限。\n").encode("utf-8")
    files = {PROMPT: _prompt(workflow, steps, deliverables).encode("utf-8"),
             SNAPSHOT: _snapshot_bytes(workflow_id, workflow, tree), TASKSET: taskset, SKILL: skill}
    skill_tree = hashlib.sha256(canonical_json([["SKILL.md", hashlib.sha256(skill).hexdigest()]])).hexdigest()
    count = len(json.loads(taskset)["cases"])
    manifest = {
        "schema_version": 1, "package_id": package_id, "version": "0.1.0", "status": "draft",
        "name": name.strip(),
        "responsibility": f"按已保存业务流「{workflow.get('name') or workflow_id}」的冻结版本完成整件工作，交付各步骤的交付物供人工审核；不外发、不写外部系统。",
        "scope_in": ["只处理本次任务输入中的材料。", "按冻结业务流的步骤顺序产出全部交付物。", "无法映射或无法确认的内容单独列出原因。"],
        "scope_out": ["不外发、不写入外部系统或业务数据。", "不修改业务流定义，不使用冻结快照之外的步骤。", "不把样例通过当作真实客户交付的验收。"],
        "human_slots": [{"slot": "reviewer", "required": True, "decides": ["release", "reject"], "channel_options": ["console"]}],
        "input_schema": input_schema, "output_schema": output_schema,
        "artifact_contract": {"deliverables": [{"name": item, "format": "text", "required": True} for item in deliverables],
                              "acceptance_criteria": ["每个交付物都有完整正文", "无法映射的记录列出原因", "人工逐条审核真实输出"]},
        "checklist": [{"id": "WF01-deliverables", "title": "每个业务流交付物都产出非空正文", "checker": "deterministic", "severity": "blocker"}],
        "skills": [{"skill_name": "saved-workflow", "version": "0.1.0", "tree_fingerprint": skill_tree, "source": "package"}],
        "prompt_refs": [PROMPT], "template_refs": [], "capability_requirements": [],
        "model_profile": {"generation": True, "classification": False, "tool_calling": False,
                          "structured_output": True, "vision": False, "verified_models": []},
        "knowledge_protocol": {"required_kinds": ["standard"],
            "citation_rule": "只引用本次任务输入与冻结业务流快照；未提供的事实标记为未知。",
            "update_rule": "业务流变更须重新创建草稿、重新试跑与审核后发布新版本，不改在途依据。",
            "license_note": "冻结快照来自负责人已保存的业务流，仅用于本企业评测与发布。"},
        "loop_policy": {"max_steps": 4, "max_model_calls": 2, "max_wall_clock_s": 120, "max_rework": 0,
                        "max_retries": 0, "max_sub_agents": 0, "max_cost_cny": max_cost_cny},
        "evaluation_profile": {"taskset_id": "saved-workflow-" + workflow_id.replace("_", "-"),
            "taskset_fingerprint": hashlib.sha256(taskset).hexdigest(), "min_samples": count,
            "aggregation": "median", "hard_fail_veto": True,
            "non_inferior_rule": {"baseline": "同一冻结任务集与当前模型配置的人工审核结论",
                                  "require_current_candidate_match": True, "require_independent_labels": True},
            "absolute_threshold": {"hard_fail_count": 0},
            "budget": {"currency": "CNY", "max_cost_cny": max_cost_cny, "require_host_budget_before_test_start": True}},
        "provenance": {"source_repo": SOURCE_REPO, "source_revision": revision,
            "files": [{"path": path, "kind": "text", "encoding": "utf-8"} for path in sorted((PROMPT, SNAPSHOT, TASKSET))],
            "license": "安普里企业内部业务流快照；未授权再分发",
            "notices": [f"冻结自已保存业务流 {workflow_id}（版本 {workflow.get('version') or ''}，树 {revision[:12]}）。",
                        "model evaluation not_run；verified_models 为空。"],
            "model_or_data_licenses": []},
        "enterprise_id": "", "created_at": created_at, "created_by": actor,
    }
    return manifest, files, revision


class _WorkflowTemplateStore(AIEmployeeFileStore):
    max_changes = 16

    def __init__(self, root: Path, package_id: str):
        self.resource_paths = {f"employee_packages/{package_id}/{name}".casefold(): f"employee_packages/{package_id}/{name}" for name in _RESOURCES}
        super().__init__(root)

    def object_path(self, relative: str) -> Path:
        if type(relative) is str and relative.casefold() in self.resource_paths:
            path = self.root.joinpath(*self.resource_paths[relative.casefold()].split("/"))
            self._check(path)
            return path
        return super().object_path(relative)


def create_from_saved_workflow(repository, manifest: dict, *, template_id: str, expected_template_revision: str,
                               workflow_source: Any, project_id: str, actor: str, request_id: str) -> dict:
    if (template_id != TEMPLATE_ID or type(manifest) is not dict
            or set(manifest) != {"package_id", "name", "max_cost_cny"}
            or type(manifest["package_id"]) is not str or not _PACKAGE.fullmatch(manifest["package_id"])
            or type(manifest["name"]) is not str or not 0 < len(manifest["name"].strip()) <= 128
            or type(manifest["max_cost_cny"]) is not str or not _MONEY.fullmatch(manifest["max_cost_cny"])
            or type(expected_template_revision) is not str or not _SHA.fullmatch(expected_template_revision)
            or type(workflow_source) is not dict or set(workflow_source) != {"workflow_id", "cases"}):
        _fail("request_invalid", "请选择已保存业务流，填写岗位编号、名称、金额上限与试跑样例。")
    package_id = manifest["package_id"]
    store = _WorkflowTemplateStore(repository.store.root, package_id)
    action = "employee_package_draft_create"
    payload = {"manifest": manifest, "template_id": template_id,
               "expected_template_revision": expected_template_revision, "workflow_source": workflow_source}
    previous = store.request_status(action=action, project_id=project_id, actor=actor, request_id=request_id, payload=payload)
    if previous is not None and previous.get("result") != "outcome_unknown":
        return previous
    if previous is not None:
        return store.commit(action=action, project_id=project_id, actor=actor, request_id=request_id,
                            payload=payload, changes=[], response={})
    if store.read(repository._relative(package_id)) is not None:
        _fail("not_allowed_in_status", "这个岗位编号已有草稿，请另选编号。")
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    draft, files, revision = build_workflow_package(ampuly_home=repository.store.root,
        workflow_id=workflow_source["workflow_id"], package_id=package_id, name=manifest["name"],
        max_cost_cny=manifest["max_cost_cny"], cases=workflow_source["cases"], actor=actor, created_at=now)
    if revision != expected_template_revision:
        _fail("revision_conflict", "业务流已有新的保存版本，请重新查看后创建。")
    repository._validate_draft(draft)
    declared = {entry["path"] for entry in draft["provenance"]["files"]}
    draft["package_fingerprint"] = package_fingerprint(draft, {name: raw for name, raw in files.items() if name in declared})
    text = canonical_json(draft).decode("utf-8") + "\n"
    changes = [{"path": f"employee_packages/{package_id}/{name}", "before_sha256": None,
                "after_text": raw.decode("utf-8")} for name, raw in sorted(files.items())]
    changes.append({"path": repository._relative(package_id), "before_sha256": None, "after_text": text})
    response = {"ok": True, "action": action, "package_id": package_id, "request_id": request_id,
        "revision": hashlib.sha256(text.encode("utf-8")).hexdigest(), "fingerprint": draft["package_fingerprint"],
        "workflow_id": workflow_source["workflow_id"], "workflow_tree_sha256": revision,
        "result": "success", "effects_produced": True, "problems": [],
        "allowed_actions": ["employee_package_detail", "employee_package_draft_save"]}
    return store.commit(action=action, project_id=project_id, actor=actor, request_id=request_id,
                        payload=payload, changes=changes, response=response)


# ---------------------------------------------------------------- pinned read

def _published_snapshot(ampuly_home: Path, package_id: str, fingerprint: str, *, require_new_adoption: bool = False) -> tuple[dict, bytes, dict]:
    from .enterprise_freeze import _published_catalog
    # Use the same contained, bounded bytes that the catalogue hashed. Never
    # re-open published paths with Path.read_bytes after verifying a different read.
    view, files = _published_catalog(ampuly_home).read_with_files(package_id, fingerprint=fingerprint,
        purpose="new_adoption" if require_new_adoption else "pinned")
    manifest = view["package"]
    if (manifest.get("provenance") or {}).get("source_repo") != SOURCE_REPO:
        _fail("pinned_workflow_unavailable", "这个已发布岗位包不是由已保存业务流冻结的。")
    if SNAPSHOT not in files:
        _fail("pinned_workflow_tampered", "企业发布冻结内容与发布指纹不一致。")
    freeze = {"freeze_source": "employee_packages_published", "package_id": package_id,
        "version": manifest["version"], "package_fingerprint": fingerprint,
        "index_relative": f"employee_packages/{package_id}/versions.jsonl",
        "version_relative": f"employee_packages/{package_id}/.versions/{fingerprint}",
        "draft_digest": None}
    return manifest, files[SNAPSHOT], freeze


def _snapshot_files(raw: bytes) -> tuple[dict, dict[str, bytes]]:
    if len(raw) > MAX_SNAPSHOT_BYTES:
        _fail("workflow_snapshot_too_large", "发布快照超过读取范围。")
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeError):
        _fail("pinned_workflow_tampered", "冻结的业务流快照无法读取。")
    if type(body) is not dict or body.get("schema_version") != SNAPSHOT_SCHEMA or type(body.get("files")) is not list:
        _fail("pinned_workflow_tampered", "冻结的业务流快照格式无效。")
    if len(body["files"]) > MAX_FILES:
        _fail("workflow_snapshot_too_large", "发布快照超过文件数量范围。")
    files: dict[str, bytes] = {}
    total = 0
    for row in body["files"]:
        path = row.get("path") if type(row) is dict else None
        if (type(path) is not str or "\\" in path or ":" in path or path.startswith("/")
                or any(part in {"", ".", ".."} for part in path.split("/")) or path in files or type(row.get("text")) is not str):
            _fail("pinned_workflow_tampered", "冻结的业务流快照路径无效。")
        data = row["text"].encode("utf-8")
        total += len(data)
        if total > MAX_BYTES:
            _fail("workflow_snapshot_too_large", "发布快照超过文件大小范围。")
        from .ai_employee_safe_files import validate_relative
        validate_relative("employee_packages/snapshot/" + path)
        if hashlib.sha256(data).hexdigest() != row.get("sha256"):
            _fail("pinned_workflow_tampered", "冻结的业务流快照内容与哈希不一致。")
        files[path] = data
    if workflow_tree_revision(files) != body.get("tree_sha256") or "workflow.yaml" not in files:
        _fail("pinned_workflow_tampered", "冻结的业务流快照树哈希不一致。")
    return body, files


def published_workflow_trial_candidates(ampuly_home: Path | str, project_id: str, workflow_id: str) -> list[dict]:
    """Read current, independently anchored published pins; never expand a tree.

    Project authority is enforced by the owning WS session before this helper;
    this enterprise catalogue is shared, while the selected run stays project-bound.
    """
    from .enterprise_freeze import _published_catalog
    from ampuly.project_id import is_valid_project_id
    if (not is_valid_project_id(project_id)
            or type(workflow_id) is not str or not _WORKFLOW.fullmatch(workflow_id)):
        _fail("request_invalid", "请指定当前项目与业务流。")
    home = Path(ampuly_home)
    packages = _published_catalog(home).list()["packages"]
    candidates = [item for item in packages
                  if (item["package"].get("provenance") or {}).get("source_repo") == SOURCE_REPO
                  and item.get("new_adoption_allowed") is True]
    if len(candidates) > MAX_PUBLISHED_CANDIDATES:
        _fail("publication_source_unavailable", "业务流发布目录超过本次候选范围，请缩小发布目录后重读。")
    result = []
    for item in candidates:
        package = item["package"]
        manifest, snapshot, _ = _published_snapshot(home, package["package_id"], item["fingerprint"], require_new_adoption=True)
        body, frozen_files = _snapshot_files(snapshot)
        if body.get("workflow_id") == workflow_id:
            import yaml
            from .workflow_packages import WorkflowPackage, usable_by_project
            try:
                workflow = yaml.safe_load(frozen_files["workflow.yaml"])
            except yaml.YAMLError:
                _fail("pinned_workflow_tampered", "冻结的业务流定义无法读取。")
            if (not isinstance(workflow, dict) or workflow.get("id") != workflow_id
                    or str(workflow.get("version") or "") != str(body.get("workflow_version") or "")):
                _fail("pinned_workflow_tampered", "冻结的业务流身份与快照不一致。")
            if not usable_by_project(WorkflowPackage(root=home, manifest=workflow), project_id):
                continue
            result.append({"package_id": manifest["package_id"], "package_version": manifest["version"],
                "package_fingerprint": item["fingerprint"], "workflow_id": workflow_id,
                "workflow_version": str(body.get("workflow_version") or ""), "name": manifest["name"]})
    return sorted(result, key=lambda item: (item["name"], item["package_id"]))


def pinned_workflow_package(ampuly_home: Path | str, package_id: str, fingerprint: str, *, require_new_adoption: bool = False):
    """(WorkflowPackage, freeze) from an enterprise-published employee pin only."""
    from .enterprise_freeze import assert_not_draft_digest
    import yaml
    from .workflow_packages import WorkflowPackage
    if type(package_id) is not str or not _PACKAGE.fullmatch(package_id) or type(fingerprint) is not str or not _SHA.fullmatch(fingerprint):
        _fail("request_invalid", "请给出已发布岗位包编号与发布指纹。")
    home = Path(ampuly_home)
    _, snapshot, freeze = _published_snapshot(home, package_id, fingerprint, require_new_adoption=require_new_adoption)
    assert_not_draft_digest(freeze)
    body, files = _snapshot_files(snapshot)
    workflow_id = str(body.get("workflow_id") or "")
    if not _WORKFLOW.fullmatch(workflow_id):
        _fail("pinned_workflow_tampered", "冻结的业务流编号无效。")
    base = home / "employee_workflow_pins" / fingerprint
    target = base / workflow_id
    if target.exists():
        if _tree_files(target) != files:
            _fail("pinned_workflow_tampered", "已展开的冻结业务流与发布内容不一致。")
    else:
        base.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".stage-", dir=base))
        for path, data in files.items():
            destination = staging.joinpath(*path.split("/"))
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        replace_with_retry(staging, target)
    # The manifest must come from the very bytes the catalogue verified. A
    # second path-based load would reopen mutable files and sidecar history.
    try:
        workflow = yaml.safe_load(files["workflow.yaml"])
    except yaml.YAMLError:
        _fail("pinned_workflow_tampered", "冻结的业务流定义无法读取。")
    if not isinstance(workflow, dict):
        _fail("pinned_workflow_tampered", "冻结的业务流定义必须是对象。")
    package = WorkflowPackage(root=target, manifest=workflow)
    if package.id != workflow_id or str(package.manifest.get("version") or "") != str(body.get("workflow_version") or ""):
        _fail("pinned_workflow_tampered", "冻结的业务流无法按原版本载入。")
    record = {"freeze_source": freeze["freeze_source"], "package_id": package_id,
              "package_version": freeze["version"], "package_fingerprint": fingerprint,
              "index_relative": freeze["index_relative"], "version_relative": freeze["version_relative"],
              "workflow_id": workflow_id, "workflow_version": str(body.get("workflow_version") or ""),
              "workflow_tree_sha256": body["tree_sha256"], "draft_digest": None}
    return package, record
