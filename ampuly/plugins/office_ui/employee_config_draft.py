"""C-package five-item view and conversation-draft proposals over one draft.

Form, conversation and template all read and write the same package draft
(``employee_packages/<id>/manifest.yaml`` through EmployeePackageRepository).
This module is pure: it projects a manifest onto the five items (岗位/知识/
SOP/输出/权限), merges edits back losslessly and turns a conversation into a
*proposal* that the owner accepts item by item. A proposal never grants a
capability, never publishes and never executes; accepted items become an
ordinary ``save`` change set with the draft's ``expected_revision``.
"""
from __future__ import annotations

import copy
from typing import Any, Iterable, Mapping

from .ai_employee_contract import AIEmployeeContractError, _OPTIONAL, _REQUIRED, canonical_json, sha256_json

RULES_VERSION = "c-config-draft-v1"
SECTIONS: dict[str, tuple[str, ...]] = {
    "role": ("name", "responsibility", "scope_in", "scope_out", "human_slots"),
    "knowledge": ("knowledge_protocol", "prompt_refs", "template_refs"),
    "sop": ("checklist", "skills", "loop_policy"),
    "output": ("output_schema", "artifact_contract"),
    "permissions": ("capability_requirements",),
}
SECTION_LABELS = {"role": "岗位", "knowledge": "知识", "sop": "SOP", "output": "输出", "permissions": "权限"}
FIELD_SECTION = {field: section for section, fields in SECTIONS.items() for field in fields}
REQUIRED_FIVE = ("name", "responsibility", "scope_in", "scope_out", "human_slots", "loop_policy",
                 "output_schema", "artifact_contract", "capability_requirements")
SOURCES = frozenset({"user", "template", "knowledge", "inferred", "saved_workflow"})
_SERVER = frozenset({"package_fingerprint", "draft_revision", "status", "created_at", "created_by", "published_at",
                     "published_by", "retired_at", "retired_by", "retired_reason", "release_approval",
                     "package_id", "schema_version", "evaluation_profile"})
_PUBLISH_WORDS = ("publish", "发布", "上线", "release", "grant", "授权", "approve", "批准")


def _err(code: str, message: str, location: str = "", **details: Any) -> AIEmployeeContractError:
    return AIEmployeeContractError(code, message, location, details=details or None)


def project_sections(manifest: dict[str, Any]) -> dict[str, Any]:
    """Five-item view plus everything else, kept verbatim for a lossless merge."""
    if type(manifest) is not dict:
        raise _err("request_invalid", "草稿必须是对象。")
    sections = {name: {f: copy.deepcopy(manifest[f]) for f in fields if f in manifest} for name, fields in SECTIONS.items()}
    other = {k: copy.deepcopy(v) for k, v in manifest.items() if k not in FIELD_SECTION}
    unknown = sorted(set(manifest) - _REQUIRED - _OPTIONAL)
    editor = "legacy" if unknown or manifest.get("legacy_source") else "five_section"
    return {"rules_version": RULES_VERSION, "sections": sections, "other": other, "unknown_fields": unknown,
            "editor": editor, "missing": [f for f in REQUIRED_FIVE if f not in manifest],
            "labels": dict(SECTION_LABELS)}


def merge_sections(manifest: dict[str, Any], sections: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Apply five-item edits; untouched and unknown fields stay byte-identical."""
    out = copy.deepcopy(manifest)
    for name, fields in sections.items():
        if name not in SECTIONS:
            raise _err("config_section_unknown", "只能编辑岗位、知识、SOP、输出、权限五项。", name)
        for field, value in fields.items():
            if FIELD_SECTION.get(field) != name:
                raise _err("config_field_not_editable", "该字段不属于这一项。", f"{name}.{field}")
            out[field] = copy.deepcopy(value)
    return out


def assert_lossless(manifest: dict[str, Any]) -> None:
    view = project_sections(manifest)
    again = merge_sections(manifest, view["sections"])
    if canonical_json(again) != canonical_json(manifest):
        raise _err("config_roundtrip_lossy", "五项视图无法无损承载这份配置，请用原编辑器。")


def _caps_of(value: Any) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for req in value or []:
        if isinstance(req, dict) and isinstance(req.get("tool"), str):
            out.setdefault(req["tool"], set()).update(a for a in req.get("actions") or [] if isinstance(a, str))
    return out


def propose_revision(manifest: dict[str, Any], *, base_revision: str, items: Iterable[dict[str, Any]],
                     host_capabilities: Mapping[str, Iterable[str]] | None) -> dict[str, Any]:
    """Turn conversation output into a reviewable proposal. Saves and runs nothing."""
    if type(base_revision) is not str or len(base_revision) != 64:
        raise _err("request_invalid", "提案必须基于明确的草稿修订。", "base_revision")
    host = {k: set(v) for k, v in (host_capabilities or {}).items()}
    status = manifest.get("status")
    rows: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    capability_requests: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if type(item) is not dict or set(item) - {"field", "value", "source", "source_ref", "inferred", "intent"}:
            raise _err("request_invalid", "提案条目结构无效。", f"items[{index}]")
        field = item.get("field")
        source = item.get("source")
        if source not in SOURCES:
            raise _err("config_source_missing", "每个提案条目必须标明来源。", f"items[{index}].source")
        intent = str(item.get("intent") or "")
        if field is None and any(w in intent.lower() for w in _PUBLISH_WORDS):
            refused.append({"index": index, "reason": "publish_or_grant_not_from_conversation",
                            "note": "对话只生成草稿；发布、授权、审批需另走正式流程。"})
            continue
        if field in _SERVER or field not in FIELD_SECTION:
            refused.append({"index": index, "field": field, "reason": "field_not_editable_here"})
            continue
        before = copy.deepcopy(manifest.get(field))
        after = copy.deepcopy(item.get("value"))
        flags: list[str] = []
        acceptable = True
        if field == "capability_requirements":
            old, new = _caps_of(before), _caps_of(after)
            for tool, actions in sorted(new.items()):
                added = sorted(actions - old.get(tool, set()))
                if not added:
                    continue
                missing = sorted(set(added) - host.get(tool, set()))
                capability_requests.append({"tool": tool, "actions": added,
                                            "status": "host_gap" if missing else "declared_needs_grant",
                                            "missing_on_host": missing})
                flags.append("permission_request")
                if missing:
                    flags.append("capability_gap")
                    acceptable = False  # cannot even be declared; only listed as a gap
        row = {"item_id": sha256_json({"i": index, "f": field, "v": after})[:16], "section": FIELD_SECTION[field],
               "label": SECTION_LABELS[FIELD_SECTION[field]], "field": field, "before": before, "after": after,
               "changed": canonical_json(before) != canonical_json(after), "source": source,
               "source_ref": item.get("source_ref"), "inferred": bool(item.get("inferred")) or source == "inferred",
               "flags": flags, "acceptable": acceptable}
        rows.append(row)
    touched = {r["field"] for r in rows}
    missing = [f for f in REQUIRED_FIVE if f not in manifest and f not in touched]
    proposal = {"rules_version": RULES_VERSION, "package_id": manifest.get("package_id"), "base_revision": base_revision,
                "items": rows, "refused": refused, "missing": missing, "capability_requests": capability_requests,
                "grants_changed": False, "publish_performed": False, "executes": False,
                "requires_new_draft": status not in {"draft", "testing"}}
    proposal["proposal_id"] = sha256_json({k: v for k, v in proposal.items()})
    return proposal


def accepted_changes(manifest: dict[str, Any], proposal: dict[str, Any], accepted: Iterable[str]) -> dict[str, Any]:
    """Change set for ``EmployeePackageRepository.save``; never grants or publishes."""
    if proposal.get("requires_new_draft") or manifest.get("status") not in {"draft", "testing"}:
        raise _err("published_requires_new_draft", "已发布版本不能原地修改，请先创建新草稿。")
    wanted = set(accepted)
    by_id = {r["item_id"]: r for r in proposal["items"]}
    unknown = wanted - set(by_id)
    if unknown:
        raise _err("request_invalid", "接受了不存在的提案条目。", details_ids=sorted(unknown))
    changes: dict[str, Any] = {}
    for item_id in sorted(wanted):
        row = by_id[item_id]
        if not row["acceptable"]:
            raise _err("capability_gap_not_acceptable", "宿主没有该能力，只能记为缺口，不能写进草稿。", row["field"])
        if row["field"] in changes and canonical_json(changes[row["field"]]) != canonical_json(row["after"]):
            raise _err("config_proposal_conflict", "同一字段接受了两个不同取值。", row["field"])
        changes[row["field"]] = copy.deepcopy(row["after"])
    return changes
