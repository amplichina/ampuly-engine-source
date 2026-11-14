"""Pure publication-path preflight; internal write-plan leaf, not authorization.

Ordinary draft validation is unchanged. The old VersionCatalog.publication_plan
remains a uniqueness decision. A future publication writer must call this
wrapper or validate_publication_layout before reading/copying/writing candidate
files. No production caller, filesystem writer, public RPC/schema, readiness or
publication evidence is implemented here.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .ai_employee_contract import AIEmployeeContractError, canonical_json, _declarations, _portable_path
from .employee_package_versions import VersionCatalog

_PACKAGE = re.compile(r"[a-z0-9][a-z0-9_-]{2,63}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_SKILL = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
# Exact first segments at the candidate package root, not basenames anywhere.
_CONTROL_FILES = frozenset({"manifest.yaml", "versions.jsonl"})
_CONTROL_TREES = frozenset({".versions", "evaluations", ".publication"})
_SKILL_FIELDS = frozenset({"skill_name", "version", "tree_fingerprint", "source"})


def _fail(message: str, path: str = "declared_files") -> None:
    # Never echo a rejected input path or candidate contents into this message.
    raise AIEmployeeContractError("publication_namespace_conflict", message, path)


def _identifier(value: Any, pattern: re.Pattern, path: str) -> None:
    if type(value) is not str or not pattern.fullmatch(value):
        raise AIEmployeeContractError("publication_record_invalid", "发布路径身份格式无效。", path)


def _version(value: Any) -> None:
    if (type(value) is not str or not value.strip() or len(value) > 256
            or any(ord(character) < 32 for character in value)):
        raise AIEmployeeContractError("publication_record_invalid", "明确的发布版本无效。", "version")


def validate_publication_layout(manifest: dict[str, Any], *, package_id: str,
                                fingerprint: str) -> dict[str, Any]:
    """Return detached exact package-relative targets; grant nothing and do no I/O.

    This is namespace preflight, not full package validation or a fingerprint
    recomputation. The host must separately verify actual files, safe handles,
    skill trees, evaluation/owner approval, independent anchors and write CAS.
    """
    _identifier(package_id, _PACKAGE, "package_id")
    _identifier(fingerprint, _SHA, "fingerprint")
    _portable_path("employee_packages/" + package_id)
    if type(manifest) is not dict:
        raise AIEmployeeContractError("publication_record_invalid", "候选清单必须为有界 JSON 对象。")
    candidate = json.loads(canonical_json(manifest))
    if candidate.get("package_id") != package_id or candidate.get("package_fingerprint") != fingerprint:
        raise AIEmployeeContractError("fingerprint_conflict", "候选身份与显式发布目标不一致。")
    declared = _declarations(candidate)  # Existing portability/case/parent rules.
    skills = candidate.get("skills", [])
    if type(skills) is not list or len(skills) > 500:
        raise AIEmployeeContractError("publication_record_invalid", "技能复制声明必须为有界数组。", "skills")
    trees: list[dict[str, str]] = []
    names: set[str] = set()
    for skill in skills:
        if type(skill) is not dict or set(skill) != _SKILL_FIELDS:
            raise AIEmployeeContractError("publication_record_invalid", "技能复制声明结构无效。", "skills")
        name = skill["skill_name"]
        _identifier(name, _SKILL, "skills.skill_name")
        _portable_path("skills/" + name)
        _identifier(skill["tree_fingerprint"], _SHA, "skills.tree_fingerprint")
        _version(skill["version"])
        if name.casefold() in names:
            _fail("技能树复制目标重复。", "skills")
        names.add(name.casefold())
        if type(skill["source"]) is not str or skill["source"] not in {"library", "package"}:
            raise AIEmployeeContractError("publication_record_invalid", "技能来源必须由既有合同明确声明。", "skills.source")
        trees.append({"skill_name": name, "source": skill["source"],
                      "version": skill["version"], "tree_fingerprint": skill["tree_fingerprint"],
                      "source_ref": "skills/" + name if skill["source"] == "package" else name})
    for path in declared:
        parts = path.casefold().split("/")
        if parts[0] in _CONTROL_FILES | _CONTROL_TREES:
            _fail("候选声明与包根的发布控制文件或控制子树相撞。")
        if names and parts[0] == "skills" and (len(parts) == 1 or parts[1] in names):
            # One independent full-tree copy owns skills/<name>. Even a
            # declaration referring to its apparent source would schedule a
            # second writer into the same target; equal bytes are not assumed.
            _fail("候选声明与计划复制的技能树根或子树相撞。")
    prefix = "employee_packages/" + package_id
    target = prefix + "/.versions/" + fingerprint
    return {
        "immutable_directory": target,
        "manifest_target": target + "/manifest.yaml",
        "declared_files": [{"source_ref": path, "target": target + "/" + path}
                           for path in sorted(declared)],
        "skill_trees": [{**tree, "target": target + "/skills/" + tree["skill_name"]}
                        for tree in sorted(trees, key=lambda row: row["skill_name"])],
        "index_target": prefix + "/versions.jsonl",
        "evaluation_journal": prefix + "/evaluations/" + fingerprint + ".jsonl",
        "publication_control_root": prefix + "/.publication",
    }


def publication_write_plan(catalog: VersionCatalog, *, manifest: dict[str, Any],
                           version: str, fingerprint: str, actor: str, at: str) -> dict[str, Any]:
    """Namespace-aware internal wrapper around the unchanged uniqueness API.

    Validates before calling publication_plan, including its already-published
    branch. Returned targets do not prove they exist or are safely writable.
    No caller may replace publication readiness/authorization with this result.
    """
    if type(catalog) is not VersionCatalog:
        raise AIEmployeeContractError("publication_record_invalid", "需要既有纯版本目录。")
    _version(version)
    if type(manifest) is not dict:
        raise AIEmployeeContractError("publication_record_invalid", "候选清单必须为 JSON 对象。")
    candidate = json.loads(canonical_json(manifest))
    if candidate.get("version") != version:
        raise AIEmployeeContractError("version_fingerprint_conflict", "候选版本与显式发布版本不一致。")
    layout = validate_publication_layout(candidate, package_id=catalog.package_id, fingerprint=fingerprint)
    plan = catalog.publication_plan(version=version, fingerprint=fingerprint, actor=actor, at=at)
    if plan.get("immutable_directory") != layout["immutable_directory"]:
        _fail("目录计划和复制目标不一致。", "immutable_directory")
    return {"catalog_plan": plan, "layout": layout}
