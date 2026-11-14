"""AE01 draft repository. No implicit publication, evaluation or adoption.

The mutable draft is one manifest.yaml, revisions are byte hashes, and requests
have durable CAS receipts. Published versions stay outside this draft writer;
until the real evaluation/review/publish chain is implemented its actions are
never advertised as available. Reads are inert even on a fresh installation.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .ai_employee_contract import (
    AIEmployeeContractError, canonical_json, package_fingerprint,
    validate_package_manifest,
)
from .ai_employee_storage import AIEmployeeFileStore, MAX_RECORD_BYTES

_PACKAGE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{2,63}\Z")
_MANAGED = frozenset({"package_fingerprint", "draft_revision", "status", "created_at", "created_by", "published_at", "published_by", "retired_at", "retired_by", "retired_reason", "release_approval", "updated_at", "updated_by", "latest_test", "evaluation_results", "evaluation_records", "evaluations", "test_results", "test_passed", "pending_review"})


class _UniqueSafeLoader(yaml.SafeLoader):
    """Reject duplicate YAML fields instead of accepting the last one."""

    def compose_node(self, parent: Any, index: Any) -> yaml.Node:
        # Asset manifests never need YAML aliases. Reject them before object
        # construction and bound parser recursion, rather than expanding a bomb.
        if self.check_event(yaml.AliasEvent):
            raise ValueError("manifest aliases are not supported")
        depth = getattr(self, "_asset_depth", 0) + 1
        if depth > 64:
            raise ValueError("manifest nesting exceeds limit")
        self._asset_depth = depth
        try:
            return super().compose_node(parent, index)
        finally:
            self._asset_depth -= 1


def _mapping(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    result: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ValueError("duplicate or non-string manifest key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueSafeLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _package_id(value: str) -> str:
    if not isinstance(value, str) or not _PACKAGE_ID.fullmatch(value):
        raise AIEmployeeContractError("request_invalid", "岗位包编号无效。", "package_id")
    return value


class EmployeePackageRepository:
    def __init__(self, ampuly_home: Path, *, available_capabilities: dict[str, set[str]] | None = None) -> None:
        self.store = AIEmployeeFileStore(ampuly_home)
        # Missing runtime registry does not mean any tool exists.
        self.capabilities = available_capabilities if available_capabilities is not None else {}

    @staticmethod
    def _relative(package_id: str) -> str:
        return f"employee_packages/{_package_id(package_id)}/manifest.yaml"

    def _load(self, package_id: str) -> tuple[dict[str, Any], bytes]:
        raw = self.store.read(self._relative(package_id))
        if raw is None:
            raise AIEmployeeContractError("package_not_found", "没有找到这个岗位包。")
        try:
            # JSON is valid YAML, and our writer uses it to avoid ambiguous
            # scalars. The bounded loader also reads existing ordinary YAML.
            manifest = yaml.load(raw.decode("utf-8"), Loader=_UniqueSafeLoader)
            canonical_json(manifest)
            if not isinstance(manifest, dict) or manifest.get("package_id") != package_id or type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
                raise ValueError
        except (ValueError, TypeError, UnicodeError, RecursionError, yaml.YAMLError) as exc:
            raise AIEmployeeContractError("storage_record_invalid", "岗位包记录无效，请核实保存版本。") from exc
        return manifest, raw

    def _files(self, manifest: dict[str, Any]) -> dict[str, bytes]:
        declared = list(manifest.get("prompt_refs", []) or [])
        declared += [row.get("path") for row in manifest.get("template_refs", []) or [] if isinstance(row, dict)]
        declared += [row if isinstance(row, str) else row.get("path") for row in manifest.get("provenance", {}).get("files", []) or [] if isinstance(row, (str, dict))]
        if len(declared) > 500:
            raise AIEmployeeContractError("validation_failed", "岗位包文件数量过多。")
        files: dict[str, bytes] = {}
        root = self.store.root / "employee_packages" / _package_id(manifest["package_id"])
        total = 0
        for relative in declared:
            if not isinstance(relative, str) or "\\" in relative or ":" in relative or relative.startswith("/") or any(p in {"", ".", ".."} for p in relative.split("/")):
                raise AIEmployeeContractError("validation_failed", "岗位包文件引用必须是稳定相对路径。")
            if relative in files:
                continue  # Same file can be both a template and provenance.
            path = root.joinpath(*relative.split("/"))
            self.store._check(path)
            raw = self.store._read(path, 20 * 1024 * 1024)
            if raw is None:
                raise AIEmployeeContractError("validation_failed", "岗位包引用的文件尚未入库。", relative)
            total += len(raw)
            if total > 20 * 1024 * 1024:
                raise AIEmployeeContractError("validation_failed", "岗位包文件总量超过范围。")
            files[relative] = raw
        return files

    def _validate_draft(self, manifest: dict[str, Any]) -> list[dict[str, Any]]:
        problems = validate_package_manifest(manifest, available_capabilities=self.capabilities, require_complete=False)
        # Incomplete drafts can be saved, but invalid types, secrets, unknown
        # capability declarations and unsupported schema cannot be persisted.
        fatal = [p for p in problems if p["severity"] == "error"]
        if fatal:
            raise AIEmployeeContractError("validation_failed", "岗位包草稿有需要先修正的问题。", details={"problems": fatal})
        return problems

    def _view(self, manifest: dict[str, Any], raw: bytes, *, owner: bool) -> dict[str, Any]:
        revision = hashlib.sha256(raw).hexdigest()
        problems = validate_package_manifest(manifest, available_capabilities=self.capabilities, require_complete=True)
        return {
            "package": {**manifest, "draft_revision": revision},
            "revision": revision, "fingerprint": manifest.get("package_fingerprint"),
            "versions": [], "test_passed": False, "pending_review": False,
            "problems": problems,
            "allowed_actions": ["employee_package_detail", "employee_package_validate"] + (["employee_package_draft_save"] if owner and manifest.get("status") in {"draft", "testing"} else []),
            "effects_produced": False,
        }

    def detail(self, package_id: str, *, version: str | None = None, owner: bool = False) -> dict[str, Any]:
        manifest, raw = self._load(package_id)
        if version is not None and manifest.get("version") != version:
            raise AIEmployeeContractError("version_not_available", "这个岗位包版本尚未入库。")
        return self._view(manifest, raw, owner=owner)

    def list(self, *, owner: bool = False) -> dict[str, Any]:
        entries = []
        for row in self.store.list_entries("employee_packages", limit=1001):
            if row["name"].startswith("."):
                continue
            if len(entries) >= 500:
                raise AIEmployeeContractError("storage_record_invalid", "岗位包目录超过单次读取范围。")
            if row["kind"] != "directory" or not _PACKAGE_ID.fullmatch(row["name"]):
                raise AIEmployeeContractError("storage_record_invalid", "岗位包目录包含无效对象。")
            entries.append(row["name"])
        packages = [self.detail(name, owner=owner) for name in sorted(entries)]
        from .employee_builtin_templates import builtin_template_list
        templates = builtin_template_list() if owner else []
        revision = hashlib.sha256(canonical_json({"packages": [[p["package"]["package_id"], p["revision"]] for p in packages], "templates": templates})).hexdigest()
        return {"packages": packages, "templates": templates, "revision": revision, "problems": [], "effects_produced": False, "allowed_actions": ["employee_package_draft_create"] if owner else []}

    def create_from_template(self, manifest: dict[str, Any], *, template_id: str,
                             expected_template_revision: str, project_id: str,
                             actor: str, request_id: str, workflow_source: Any = None) -> dict[str, Any]:
        from .employee_workflow_template import TEMPLATE_ID as WORKFLOW_TEMPLATE, create_from_saved_workflow
        if template_id == WORKFLOW_TEMPLATE:
            return create_from_saved_workflow(self, manifest, template_id=template_id,
                expected_template_revision=expected_template_revision, workflow_source=workflow_source,
                project_id=project_id, actor=actor, request_id=request_id)
        if workflow_source is not None:
            raise AIEmployeeContractError("request_invalid", "只有“已保存业务流”来源可以携带业务流。")
        from .employee_builtin_templates import create_builtin_template
        return create_builtin_template(self, manifest, template_id=template_id,
            expected_template_revision=expected_template_revision, project_id=project_id,
            actor=actor, request_id=request_id)

    def create(self, manifest: dict[str, Any], *, project_id: str, actor: str, request_id: str) -> dict[str, Any]:
        if not isinstance(manifest, dict) or set(manifest) & _MANAGED:
            raise AIEmployeeContractError("request_invalid", "创建草稿不能指定服务端状态、身份或指纹。")
        package_id = _package_id(manifest.get("package_id"))
        action = "employee_package_draft_create"
        previous = self.store.request_status(action=action, project_id=project_id, actor=actor, request_id=request_id, payload=manifest)
        if previous is not None:
            return previous
        if self.store.read(self._relative(package_id)) is not None and previous is None:
            raise AIEmployeeContractError("not_allowed_in_status", "这个岗位包已有草稿，不会覆盖。")
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        draft = {**manifest, "schema_version": manifest.get("schema_version", 1), "status": "draft", "created_at": now, "created_by": actor}
        self._validate_draft(draft)
        draft["package_fingerprint"] = package_fingerprint(draft, self._files(draft))
        text = canonical_json(draft).decode("utf-8") + "\n"
        revision = hashlib.sha256(text.encode("utf-8")).hexdigest()
        response = {"ok": True, "action": action, "package_id": package_id, "request_id": request_id, "revision": revision, "fingerprint": draft["package_fingerprint"], "result": "success", "effects_produced": True, "problems": [], "allowed_actions": ["employee_package_detail", "employee_package_validate", "employee_package_draft_save"]}
        return self.store.commit(action=action, project_id=project_id, actor=actor, request_id=request_id, payload=manifest, changes=[{"path": self._relative(package_id), "before_sha256": None, "after_text": text}], response=response)

    def save(self, package_id: str, changes: dict[str, Any], *, expected_revision: str, project_id: str, actor: str, request_id: str) -> dict[str, Any]:
        _package_id(package_id)
        if not isinstance(changes, dict) or set(changes) & (_MANAGED | {"package_id", "schema_version"}):
            raise AIEmployeeContractError("request_invalid", "草稿不能改服务端字段或对象身份。")
        payload = {"package_id": package_id, "changes": changes, "expected_revision": expected_revision}
        action = "employee_package_draft_save"
        previous = self.store.request_status(action=action, project_id=project_id, actor=actor, request_id=request_id, payload=payload)
        if previous is not None:
            return previous
        manifest, raw = self._load(package_id)
        current = hashlib.sha256(raw).hexdigest()
        # Whole-package rewrite optimistic concurrency (Top5 item 4). Prefer the
        # shared etag gate so stale tokens refuse with package_rewrite_stale;
        # keep revision_conflict as a compatibility alias for older clients.
        if previous is None:
            from ampuly.plugins.office_ui.employee_task_center import assert_package_rewrite_fresh
            try:
                assert_package_rewrite_fresh(expected_etag=str(expected_revision or ""), current_etag=current)
            except AIEmployeeContractError as exc:
                if getattr(exc, "code", "") in {"package_rewrite_stale", "package_rewrite_token_missing"}:
                    raise AIEmployeeContractError(
                        "revision_conflict" if exc.code == "package_rewrite_stale" else "request_invalid",
                        "草稿已有新版本，保留本地修改后重新读取。" if exc.code == "package_rewrite_stale" else "整包重写需要 expected_revision。",
                        details={"current_revision": current, "rewrite_gate": exc.code, **(getattr(exc, "details", None) or {})},
                    ) from exc
                raise
        if manifest.get("status") not in {"draft", "testing"}:
            raise AIEmployeeContractError("not_allowed_in_status", "这个版本不能原地修改。")
        draft = {**manifest, **changes}
        self._validate_draft(draft)
        draft["package_fingerprint"] = package_fingerprint(draft, self._files(draft))
        text = canonical_json(draft).decode("utf-8") + "\n"
        if len(text.encode("utf-8")) > MAX_RECORD_BYTES:
            raise AIEmployeeContractError("validation_failed", "岗位包草稿过大。")
        revision = hashlib.sha256(text.encode("utf-8")).hexdigest()
        response = {"ok": True, "action": action, "package_id": package_id, "request_id": request_id, "revision": revision, "fingerprint": draft["package_fingerprint"], "result": "success", "effects_produced": current != revision, "problems": [], "allowed_actions": ["employee_package_detail", "employee_package_validate", "employee_package_draft_save"]}
        return self.store.commit(action=action, project_id=project_id, actor=actor, request_id=request_id, payload=payload, changes=[{"path": self._relative(package_id), "before_sha256": current, "after_text": text}], response=response)
