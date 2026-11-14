"""AE01 project-instance persistence, independent of staff accounts and engines.

All providers are server-owned, read-only observations. This repository neither
authenticates a socket nor grants tools. Adapters must authorize every request,
including receipt replay. No runtime pause/disable action is exposed here: its
effect must include cancellation and dispatch guards before it can be mounted.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import re
import secrets
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .ai_employee_contract import AIEmployeeContractError, canonical_json
from .ai_employee_read_budget import AIEmployeeReadBudget
from .ai_employee_instance_contract import (
    derive_readiness, effective_limits, instance_revision_fingerprint, resolve_binding,
    validate_binding, validate_instance,
)
from .ai_employee_storage import AIEmployeeFileStore, MAX_RECORD_BYTES
from .model_catalogue import (
    EmployeeModelCatalogueError as _ModelCatalogueError,
    EmployeeModelCatalogueObservation as _ModelCatalogueObservation,
    recheck_employee_model_choices as _recheck_model_choices,
)
from .model_tiers import TIERS as _MODEL_TIERS

_PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_PACKAGE = re.compile(r"[a-z0-9][a-z0-9_-]{2,63}\Z")
_INSTANCE = re.compile(r"ei-[a-f0-9]{16}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_CONFIG = frozenset({"display_name", "params", "knowledge_refs", "experience_scope",
                     "model_config", "capability_impls", "connector_refs",
                     "human_assignments", "limits", "context_overlay"})
# AE00 revise does not silently change capability implementations or isolation.
_REVISABLE = _CONFIG - {"capability_impls", "experience_scope"}
_RETEST = frozenset({"params", "knowledge_refs", "model_config", "connector_refs",
                     "human_assignments", "limits", "context_overlay"})
# Public package declarations only. No instance configuration enters a preview.
ADOPTION_PREVIEW_KEYS = {
    "capability_scope": frozenset({"scope_in", "scope_out"}),
    "methods": frozenset({"responsibility", "input_schema", "output_schema",
        "artifact_contract", "checklist", "skills", "prompt_refs", "template_refs", "human_slots"}),
    "tools": frozenset({"capability_requirements"}),
    "models": frozenset({"model_profile"}),
    "knowledge_protocol": frozenset({"knowledge_protocol"}),
    "budget": frozenset({"loop_policy", "evaluation_budget"}),
    "verification_scope": frozenset({"evaluation_profile", "declared_verified_models"}),
}
ADOPTION_PREVIEW_GAPS = frozenset({"adoption_write_unavailable", "evaluation_source_unverified",
    "publication_index_unverified", "package_retired", "private_configuration_incompatible"})
BINDING_CANDIDATE_KEYS = frozenset({"instance_id", "display_name", "package_id", "package_version",
    "package_fingerprint", "instance_revision", "revision_fingerprint", "intake_state",
    "execution_state", "ready", "gaps"})
_PUBLIC_GAP = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")
_MAX_SAFE_INTEGER = 2**53 - 1


def _adoption_projection(manifest: dict) -> dict[str, dict]:
    result = {section: {key: deepcopy(manifest[key]) for key in keys if key in manifest}
              for section, keys in ADOPTION_PREVIEW_KEYS.items()}
    evaluation = manifest.get("evaluation_profile")
    if type(evaluation) is dict:
        if "budget" in evaluation:
            result["budget"]["evaluation_budget"] = deepcopy(evaluation["budget"])
        result["verification_scope"]["evaluation_profile"] = {
            key: deepcopy(value) for key, value in evaluation.items() if key != "budget"}
    model = manifest.get("model_profile")
    if type(model) is dict and "verified_models" in model:
        result["verification_scope"]["declared_verified_models"] = deepcopy(model["verified_models"])
    return result


def _valid(value: Any, pattern: re.Pattern, field: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise AIEmployeeContractError("request_invalid", "对象标识无效。", field)
    if field == "project_id" and value.split(".")[0].upper() in {
        "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)),
        *(f"LPT{i}" for i in range(10)),
    }:
        raise AIEmployeeContractError("request_invalid", "项目标识不可用。", field)
    return value


def _hash(raw: bytes | None) -> str | None:
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def _unique(pairs: list[tuple[str, Any]]) -> dict:
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate key")
        obj[key] = value
    return obj


def _json(raw: bytes, *, read_budget: AIEmployeeReadBudget | None = None) -> dict[str, Any]:
    try:
        obj = json.loads(raw, object_pairs_hook=_unique)
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise AIEmployeeContractError("storage_record_invalid", "岗位记录无法核实，请检查保存版本。") from exc
    # Keep the fixed budget refusal outside legacy decode/canonical wrapping,
    # including when the contract error type derives from ValueError.
    if read_budget is not None:
        read_budget.consume_json(obj)
    try:
        canonical_json(obj)
        if not isinstance(obj, dict):
            raise ValueError("object required")
        return obj
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise AIEmployeeContractError("storage_record_invalid", "岗位记录无法核实，请检查保存版本。") from exc


def _text(obj: Any) -> str:
    result = canonical_json(obj).decode("utf-8") + "\n"
    if len(result.encode("utf-8")) > MAX_RECORD_BYTES:
        raise AIEmployeeContractError("storage_record_invalid", "岗位修订超过本次保存范围。")
    return result


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _revision(value: Any, *, zero: bool = False, binding: bool = False) -> int:
    if type(value) is not int or value < (0 if zero else 1):
        raise AIEmployeeContractError("request_invalid", "必须提供明确的整数修订号。",
                                     "expected_binding_revision" if binding else "expected_revision")
    return value


# Instance model selection is configuration only: no account, route or trial grant.
_MODEL_PROJECTION_VERSION = "employee_instance_model_selection.v1"
_MODEL_PROJECTION_BYTES = 2 * 1024 * 1024
_MODEL_REFUSALS = frozenset({"model_unknown", "model_retired", "model_overseas_off",
                           "model_not_routable", "model_needs_params", "model_needs_trial"})
_MODEL_SOURCE_ERRORS = frozenset({"model_catalogue_unavailable", "model_catalogue_changed",
                                "candidate_projection_limit"})
_MODEL_SAFE_ERRORS = frozenset({
    "unsupported", "request_invalid", "owner_required", "not_allowed_in_status",
    "revision_conflict", "fingerprint_mismatch", "request_id_reused", "validation_failed",
    "version_not_available", "instance_not_found", "storage_record_invalid",
    "storage_path_invalid", "storage_busy", "outcome_unknown", "candidate_read_limit",
}) | _MODEL_REFUSALS | _MODEL_SOURCE_ERRORS


def _model_configuration(value: Any) -> dict[str, str]:
    """Strict new whole-object choice; leave legacy instance validation unchanged."""
    if type(value) is not dict or len(value) not in {2, 3}:
        raise AIEmployeeContractError("request_invalid", "模型设置必须明确供应商和模型。")
    if any(type(key) is not str for key in value) or set(value) not in (
            {"provider", "model"}, {"provider", "model", "tier"}):
        raise AIEmployeeContractError("request_invalid", "模型设置包含不支持的字段。")
    result = {}
    for key in ("provider", "model"):
        text = value[key]
        if (type(text) is not str or not 1 <= len(text) <= 256 or text != text.strip()
                or any(ord(char) < 32 or 127 <= ord(char) <= 159
                       or 0xD800 <= ord(char) <= 0xDFFF for char in text)
                or len(text.encode("utf-8")) > 256):
            raise AIEmployeeContractError("request_invalid", "模型设置的标识无效。")
        result[key] = text
    if "tier" in value:
        tier = value["tier"]
        if type(tier) is not str or tier not in _MODEL_TIERS:
            raise AIEmployeeContractError("request_invalid", "模型档位无效。")
        result["tier"] = tier
    return result


def _model_sha(value: Any) -> str:
    if type(value) is not str or not _SHA.fullmatch(value):
        raise AIEmployeeContractError("request_invalid", "模型选择必须提供固定指纹。")
    return value


def _model_revision(value: Any, *, saving: bool = False) -> int:
    maximum = _MAX_SAFE_INTEGER - 1 if saving else _MAX_SAFE_INTEGER
    if type(value) is not int or not 1 <= value <= maximum:
        raise AIEmployeeContractError("request_invalid", "模型选择修订必须是安全正整数。")
    return value


def _model_synchronous_callable(source: Any) -> bool:
    if not callable(source):
        return False
    call = getattr(source, "__call__", None)
    return not (inspect.iscoroutinefunction(source) or inspect.isasyncgenfunction(source)
                or inspect.iscoroutinefunction(call) or inspect.isasyncgenfunction(call))


def _model_source(source: Any) -> Callable:
    if source is None:
        raise AIEmployeeContractError("unsupported", "实例模型目录尚未接入。")
    if not _model_synchronous_callable(source):
        raise AIEmployeeContractError("model_catalogue_unavailable", "实例模型目录无法核实。")
    return source


def _model_projection(observation: Any) -> dict:
    # Recheck the actual catalogue producer, not a caller-provided truth value.
    if (type(observation) is not _ModelCatalogueObservation
            or type(observation.projection_version) is not str
            or observation.projection_version != _MODEL_PROJECTION_VERSION
            or type(observation.revision) is not str or not _SHA.fullmatch(observation.revision)
            or type(observation.projection_bytes) is not bytes):
        raise AIEmployeeContractError("model_catalogue_unavailable", "实例模型目录无法核实。")
    version, revision, raw = observation.projection_version, observation.revision, observation.projection_bytes
    if len(raw) > _MODEL_PROJECTION_BYTES:
        raise AIEmployeeContractError("candidate_projection_limit", "实例模型候选超过本次范围。")
    if _hash(raw) != revision:
        raise AIEmployeeContractError("model_catalogue_unavailable", "实例模型目录无法核实。")
    _model_recheck_snapshot(observation, projection_version=version, revision=revision, projection_bytes=raw)
    projection = json.loads(raw, object_pairs_hook=_unique)
    if (type(projection) is not dict or set(projection) != {"projection_version", "candidates"}
            or projection["projection_version"] != _MODEL_PROJECTION_VERSION
            or type(projection["candidates"]) is not list or len(projection["candidates"]) > 500
            or canonical_json(projection) != raw):
        raise AIEmployeeContractError("model_catalogue_unavailable", "实例模型目录无法核实。")
    # The real producer/recheck supplies the row whitelist, shared budget and
    # fixed rejection policy. This finite byte decode does not duplicate it.
    return projection


def _model_recheck_snapshot(observation: Any, *, projection_version: str,
                            revision: str, projection_bytes: bytes) -> None:
    def unchanged() -> None:
        if (type(observation) is not _ModelCatalogueObservation
                or type(observation.projection_version) is not str
                or observation.projection_version != projection_version
                or type(observation.revision) is not str or observation.revision != revision
                or type(observation.projection_bytes) is not bytes
                or observation.projection_bytes != projection_bytes):
            raise AIEmployeeContractError("model_catalogue_changed", "模型目录的冻结观察发生变化。")
    unchanged()
    _recheck_model_choices(observation)
    unchanged()


def _model_selected(projection: dict, configuration: dict) -> None:
    matches = [row for row in projection["candidates"]
               if row["provider"] == configuration["provider"] and row["model"] == configuration["model"]]
    if len(matches) != 1:
        raise AIEmployeeContractError("model_unknown", "原模型目录没有这个选择。")
    row = matches[0]
    if row["selectable"] is not True:
        gaps = row["gaps"]
        code = gaps[0] if type(gaps) is list and len(gaps) == 1 else None
        if type(code) is not str or code not in _MODEL_REFUSALS:
            code = "model_catalogue_unavailable"
        raise AIEmployeeContractError(code, "原模型目录不允许这个新选择。")


def _model_receipt_fields(value: Any) -> dict:
    if (type(value) is not dict or len(value) != 2
            or any(type(key) is not str for key in value)
            or set(value) != {"model_catalogue_revision", "model_config"}):
        raise AIEmployeeContractError("request_invalid", "模型保存回执字段无效。")
    return {"model_catalogue_revision": _model_sha(value["model_catalogue_revision"]),
            "model_config": _model_configuration(value["model_config"])}


def _model_unknown(request_id: str) -> dict:
    return {"ok": False, "result": "outcome_unknown", "error": "outcome_unknown",
            "effects_produced": None, "request_id": request_id}


def _model_saved_receipt(value: Any, *, project_id: str, instance_id: str,
                         request_id: str, expected_revision: int, catalogue_revision: str,
                         configuration: dict, fingerprint: str | None = None,
                         retest: list[str] | None = None) -> dict:
    keys = {"ok", "action", "project_id", "request_id", "instance_id", "revision", "fingerprint",
            "result", "effects_produced", "retest_scope", "problems", "allowed_actions",
            "model_catalogue_revision", "model_config"}
    if (type(value) is not dict or len(value) not in {len(keys), len(keys) + 1}
            or any(type(key) is not str for key in value)
            or set(value) not in (keys, keys | {"idempotent"})
            or ("idempotent" in value and value["idempotent"] is not True)
            or value["ok"] is not True or value["effects_produced"] is not True
            or type(value["revision"]) is not int or value["revision"] != expected_revision + 1
            or any(type(value[key]) is not str for key in (
                "action", "project_id", "request_id", "instance_id", "result", "fingerprint",
                "model_catalogue_revision"))
            or value["action"] != "employee_instance_revise" or value["project_id"] != project_id
            or value["request_id"] != request_id or value["instance_id"] != instance_id
            or value["result"] != "success" or not _SHA.fullmatch(value["fingerprint"])
            or (fingerprint is not None and value["fingerprint"] != fingerprint)
            or value["model_catalogue_revision"] != catalogue_revision
            or type(value["retest_scope"]) is not list or len(value["retest_scope"]) > 1
            or any(type(item) is not str for item in value["retest_scope"])
            or value["retest_scope"] not in ([], ["model_config"])
            or (retest is not None and value["retest_scope"] != retest)
            or type(value["problems"]) is not list or value["problems"]
            or type(value["allowed_actions"]) is not list or len(value["allowed_actions"]) != 1
            or type(value["allowed_actions"][0]) is not str
            or value["allowed_actions"] != ["employee_instance_detail"]):
        raise AIEmployeeContractError("outcome_unknown", "模型设置原保存回执待核实。")
    if canonical_json(_model_configuration(value["model_config"])) != canonical_json(configuration):
        raise AIEmployeeContractError("outcome_unknown", "模型设置原保存回执待核实。")
    # These exact finite fields have been checked. Never pass source references.
    return json.loads(canonical_json(value))


class EmployeeInstanceRepository:
    """Small bounded CAS transactions: instance + append-only revision ledger.

    package_loader(id, version, fingerprint) must read an immutable, verified
    server catalog and return {manifest, fingerprint, status}. It must not run an
    engine, write adoption defaults, or read arbitrary client file paths.
    context_provider(instance, manifest) supplies current server readiness facts;
    its absence is an explicit unknown, never a ready state.
    """

    def __init__(self, ampuly_home: Path, *, package_loader: Callable | None = None,
                 context_provider: Callable | None = None,
                 model_catalogue_source: Callable | None = None) -> None:
        self.store = AIEmployeeFileStore(ampuly_home)
        self.package_loader = package_loader
        self.context_provider = context_provider
        self.model_catalogue_source = model_catalogue_source

    @property
    def model_configuration_available(self) -> bool:
        """Source presence only, not readiness or authorization; never call it."""
        return _model_synchronous_callable(getattr(self, "model_catalogue_source", None))

    @staticmethod
    def _relative(project_id: str, instance_id: str, *, history: bool = False) -> str:
        _valid(project_id, _PROJECT, "project_id")
        _valid(instance_id, _INSTANCE, "instance_id")
        suffix = ".revisions.jsonl" if history else ".json"
        return f"projects/{project_id}/employee_instances/{instance_id}{suffix}"

    def _package(self, package_id: str, version: str, fingerprint: str,
                 *, for_adoption: bool = False) -> dict[str, Any]:
        return self._package_record(package_id, version, fingerprint,
                                    for_adoption=for_adoption)["manifest"]

    def _package_record(self, package_id: str, version: str, fingerprint: str,
                        *, for_adoption: bool = False) -> dict[str, Any]:
        _valid(package_id, _PACKAGE, "package_id")
        _valid(fingerprint, _SHA, "expected_fingerprint")
        if not isinstance(version, str) or not 0 < len(version) <= 128:
            raise AIEmployeeContractError("request_invalid", "岗位版本无效。", "package_version")
        if self.package_loader is None:
            raise AIEmployeeContractError("version_not_available", "固定岗位版本的数据源尚未接入。")
        record = self.package_loader(package_id, version, fingerprint)
        if not isinstance(record, Mapping) or not isinstance(record.get("manifest"), dict):
            raise AIEmployeeContractError("version_not_available", "固定岗位版本不可用。")
        manifest = deepcopy(record["manifest"])
        if (record.get("fingerprint") != fingerprint or manifest.get("package_id") != package_id
                or manifest.get("version") != version or manifest.get("package_fingerprint") != fingerprint):
            raise AIEmployeeContractError("fingerprint_mismatch", "岗位版本和固定指纹不一致。")
        status = record.get("status")
        if (status not in {"published", "retired"}
                or (for_adoption and (status != "published" or record.get("new_adoption_allowed") is not True))):
            # Testing adoption needs an authorized-trial path (M3), not a flag
            # supplied by the browser. No implicit bypass before that exists.
            raise AIEmployeeContractError("not_allowed_in_status", "该版本不能被新采用。")
        manifest["status"] = status
        return {"manifest": manifest, "fingerprint": fingerprint, "status": status,
                # These are read observations, never an adoption grant.
                "is_current_verified": record.get("is_current_verified") is True,
                "new_adoption_allowed": record.get("new_adoption_allowed") is True,
                "revision": record.get("revision")}

    @staticmethod
    def _validate(instance: dict, manifest: dict | None = None) -> None:
        problems = validate_instance(instance, manifest, expected_project_id=instance["project_id"])
        fatal = [row for row in problems if row.get("severity", "error") == "error"]
        if fatal:
            raise AIEmployeeContractError("validation_failed", "岗位配置需要修正。", details={"problems": fatal})

    def _stable_project(self, project_id: str) -> str:
        from ampuly.plugins.office_ui.project_alias import resolve_project_alias
        return resolve_project_alias(self.store.root, project_id)

    def _load(self, project_id: str, instance_id: str, *,
              read_budget: AIEmployeeReadBudget | None = None) -> tuple[dict, bytes, bytes, list[dict]]:
        project_id = self._stable_project(project_id)
        relative = self._relative(project_id, instance_id)
        raw = (self.store.read(relative) if read_budget is None
               else self.store.read(relative, read_budget=read_budget))
        if raw is None:
            raise AIEmployeeContractError("instance_not_found", "没有找到本项目的岗位。")
        obj = _json(raw, read_budget=read_budget)
        # Uniform not-found outside this project's trusted scope, without
        # exposing the other project/name in the exception.
        if obj.get("project_id") != project_id or obj.get("instance_id") != instance_id:
            raise AIEmployeeContractError("instance_not_found", "没有找到本项目的岗位。")
        self._validate(obj)
        history_relative = self._relative(project_id, instance_id, history=True)
        history_raw = (self.store.read(history_relative) if read_budget is None
                       else self.store.read(history_relative, read_budget=read_budget))
        if history_raw is None:
            raise AIEmployeeContractError("storage_record_invalid", "岗位修订历史缺失，不能继续修改。")
        if not history_raw.endswith(b"\n") or b"\n\n" in history_raw:
            raise AIEmployeeContractError("storage_record_invalid", "岗位修订历史未完整写入。")
        history_lines = [line for line in history_raw.splitlines() if line]
        if not history_lines or len(history_lines) > 1000:
            raise AIEmployeeContractError("storage_record_invalid", "岗位修订历史无效。")
        history = []
        for line in history_lines:
            if read_budget is not None:
                read_budget.consume_history_record()
            history.append(_json(line, read_budget=read_budget))
        previous_fp = None
        for revision, row in enumerate(history, 1):
            snapshot = row.get("snapshot")
            if (set(row) != {"action", "actor", "at", "request_id", "previous_revision_fingerprint", "revision_fingerprint", "snapshot"}
                    or not all(isinstance(row.get(key), str) and 0 < len(row[key]) <= 128
                               for key in ("action", "actor", "at", "request_id"))
                    or not isinstance(snapshot, dict) or snapshot.get("instance_id") != instance_id
                    or snapshot.get("project_id") != project_id
                    or snapshot.get("instance_revision") != revision
                    or row.get("previous_revision_fingerprint") != previous_fp
                    or row.get("revision_fingerprint") != instance_revision_fingerprint(snapshot)
                    or snapshot.get("revision_fingerprint") != row.get("revision_fingerprint")):
                raise AIEmployeeContractError("storage_record_invalid", "岗位修订链不完整。")
            self._validate(snapshot)
            previous_fp = row["revision_fingerprint"]
        if canonical_json(history[-1]["snapshot"]) != canonical_json(obj):
            raise AIEmployeeContractError("outcome_unknown", "岗位保存尚未完整核实，请先回查原请求。")
        return obj, raw, history_raw, history

    def read_revision(self, project_id: str, instance_id: str, *, revision: int,
                      revision_fingerprint: str, package_fingerprint: str | None = None,
                      read_budget: AIEmployeeReadBudget | None = None) -> dict:
        """Read one exact private snapshot, not an execution/adoption permission.

        The caller must freshly authorize project/private-scope access. Validate
        the complete stored chain and head first; an unknown save is not repaired
        here. Never fill old fields from current configuration, a package catalog
        or a readiness provider. The returned copy must not be treated as a live
        capability grant, and this method is intentionally not exposed over WS.
        One default budget spans the whole read; host callers may explicitly
        share the exact budget across reads. Exhaustion never resets it here.
        """
        _valid(project_id, _PROJECT, "project_id")
        _valid(instance_id, _INSTANCE, "instance_id")
        if type(revision) is not int or revision < 1:
            raise AIEmployeeContractError("request_invalid", "必须提供明确的历史整数修订号。", "revision")
        _valid(revision_fingerprint, _SHA, "revision_fingerprint")
        if package_fingerprint is not None:
            _valid(package_fingerprint, _SHA, "package_fingerprint")
        if read_budget is not None and type(read_budget) is not AIEmployeeReadBudget:
            raise AIEmployeeContractError("request_invalid", "历史读取预算必须由服务端提供。", "read_budget")
        budget = read_budget if read_budget is not None else AIEmployeeReadBudget()
        _, _, _, history = self._load(project_id, instance_id, read_budget=budget)
        if revision > len(history):
            raise AIEmployeeContractError("revision_not_found", "没有找到该岗位的固定历史修订。")
        snapshot = history[revision - 1]["snapshot"]
        if snapshot["revision_fingerprint"] != revision_fingerprint:
            raise AIEmployeeContractError("revision_fingerprint_mismatch", "历史修订与固定指纹不一致。")
        if package_fingerprint is not None and snapshot["package_fingerprint"] != package_fingerprint:
            raise AIEmployeeContractError("fingerprint_mismatch", "历史修订与固定岗位包指纹不一致。")
        return deepcopy(snapshot)

    def _readiness(self, instance: dict) -> dict:
        manifest = None
        try:
            manifest = self._package(instance["package_id"], instance["package_version"], instance["package_fingerprint"])
        except AIEmployeeContractError as exc:
            return {"ready": False, "gaps": ["fingerprint_mismatch" if exc.code == "fingerprint_mismatch" else "package_version_missing"],
                    "problems": [], "verified_scope": {}, "effective_limits": {}}
        context = self.context_provider(deepcopy(instance), deepcopy(manifest)) if self.context_provider else None
        if context is not None:
            if not isinstance(context, dict):
                raise AIEmployeeContractError("storage_record_invalid", "岗位就绪观测格式无效。")
            context = {**context, "package_manifest": manifest}
        return derive_readiness(instance, context)

    def detail(self, project_id: str, instance_id: str, *, owner: bool = False,
               read_budget: AIEmployeeReadBudget | None = None) -> dict:
        obj, _, _, history = (self._load(project_id, instance_id) if read_budget is None
                              else self._load(project_id, instance_id, read_budget=read_budget))
        return {"instance": deepcopy(obj), "revision": obj["instance_revision"],
                "fingerprint": obj["revision_fingerprint"], **self._readiness(obj),
                "history": [{"instance_revision": row["snapshot"]["instance_revision"],
                             "revision_fingerprint": row["revision_fingerprint"],
                             "at": row["at"], "actor": row["actor"], "action": row["action"],
                             "change_note": row["snapshot"].get("change_note", "")}
                            for row in history],
                "effects_produced": False,
                "allowed_actions": ["employee_instance_detail"] + (["employee_instance_revise", "employee_instance_adoption_preview"]
                    + (["employee_instance_adopt"] if self.package_loader else []) if owner else [])}

    def list(self, project_id: str, *, owner: bool = False,
             read_budget: AIEmployeeReadBudget | None = None) -> dict:
        project_id = self._stable_project(project_id)
        _valid(project_id, _PROJECT, "project_id")
        # SafeFiles returns only direct entries and never follows reparse points.
        relative = f"projects/{project_id}/employee_instances"
        rows = (self.store.list_entries(relative, limit=2001) if read_budget is None
                else self.store.list_entries(relative, limit=2001, read_budget=read_budget))
        names = []
        for row in rows:
            name = row["name"]
            if row["kind"] == "file" and re.fullmatch(r"\.ampuly-write-[a-f0-9]{24}\.tmp", name):
                continue  # Never interpret this store's private temporary file.
            if row["kind"] != "file" or not re.fullmatch(r"ei-[a-f0-9]{16}(?:\.revisions\.jsonl|\.json)", name):
                raise AIEmployeeContractError("storage_record_invalid", "岗位目录包含无法识别的对象。")
            if name.endswith(".json"):
                names.append(name[:-5])
        if len(names) > 500:
            raise AIEmployeeContractError("storage_record_invalid", "本次岗位列表超过范围，请缩小项目。")
        items = [(self.detail(project_id, item, owner=owner) if read_budget is None
                  else self.detail(project_id, item, owner=owner, read_budget=read_budget))
                 for item in sorted(names)]
        return {"instances": items, "effects_produced": False,
                "revision": hashlib.sha256(canonical_json([[item["instance"]["instance_id"], item["fingerprint"]] for item in items])).hexdigest(),
                "allowed_actions": ["employee_instance_create"] if owner and self.package_loader else []}

    def _receipt(self, action: str, project_id: str, actor: str, request_id: str, payload: dict,
                 *, require_committed: bool = False) -> dict | None:
        _valid(project_id, _PROJECT, "project_id")
        if not isinstance(actor, str) or not 0 < len(actor.strip()) <= 128:
            raise AIEmployeeContractError("request_invalid", "缺少已认证的操作人。")
        return self.store.request_status(action=action, project_id=project_id, actor=actor,
                                         request_id=request_id, payload=payload,
                                         **({"require_committed": True} if require_committed else {}))

    def _save(self, instance: dict, *, action: str, actor: str, request_id: str, payload: dict,
              old: tuple[dict, bytes, bytes, list[dict]] | None = None, retest: list[str] | None = None,
              before_commit: Callable[[], None] | None = None,
              model_selection_receipt: dict | None = None) -> dict:
        self._validate(instance)
        text = _text(instance)
        history = b"" if old is None else old[2]
        event = {"action": action, "actor": actor, "at": instance["updated_at"], "request_id": request_id,
                 "previous_revision_fingerprint": old[0]["revision_fingerprint"] if old else None,
                 "revision_fingerprint": instance["revision_fingerprint"], "snapshot": instance}
        history_text = history.decode("utf-8") + _text(event)
        if len(history_text.encode("utf-8")) > MAX_RECORD_BYTES:
            raise AIEmployeeContractError("storage_record_invalid", "岗位修订历史已达到本次保存上限。")
        project_id, instance_id = instance["project_id"], instance["instance_id"]
        response = {"ok": True, "action": action, "project_id": project_id, "request_id": request_id,
                    "instance_id": instance_id, "revision": instance["instance_revision"],
                    "fingerprint": instance["revision_fingerprint"], "result": "success", "effects_produced": True,
                    "retest_scope": retest or [], "problems": [], "allowed_actions": ["employee_instance_detail"]}
        if model_selection_receipt is not None:
            response.update(_model_receipt_fields(model_selection_receipt))
        return self.store.commit(action=action, project_id=project_id, actor=actor, request_id=request_id, payload=payload,
                                 changes=[{"path": self._relative(project_id, instance_id),
                                           "before_sha256": _hash(old[1]) if old else None, "after_text": text},
                                          {"path": self._relative(project_id, instance_id, history=True),
                                           "before_sha256": _hash(old[2]) if old else None, "after_text": history_text}], response=response,
                                 **({"before_commit": before_commit} if before_commit is not None else {}))

    def create(self, project_id: str, *, package_id: str, package_version: str,
               expected_fingerprint: str, config: dict, actor: str, request_id: str, note: str = "",
               before_commit: Callable[[], None] | None = None, check_only: bool = False) -> dict:
        project_id = self._stable_project(project_id)
        if not isinstance(config, dict) or set(config) - _CONFIG:
            raise AIEmployeeContractError("request_invalid", "不能指定岗位身份、状态或未知配置字段。")
        if not isinstance(note, str) or len(note) > 4000:
            raise AIEmployeeContractError("request_invalid", "采用说明无效。")
        payload = {"package_id": package_id, "package_version": package_version,
                   "expected_fingerprint": expected_fingerprint, "config": config, "note": note}
        action = "employee_instance_create"
        if type(check_only) is not bool or check_only and before_commit is None:
            raise AIEmployeeContractError("request_invalid", "采用核实必须有当前授权围栏。")
        frozen_payload = canonical_json(payload)
        def guard() -> None:
            AIEmployeeFileStore._before_commit(before_commit)
            if canonical_json(payload) != frozen_payload:
                raise AIEmployeeContractError("request_invalid", "采用请求内容发生变化。")
        guard()
        receipt = self._receipt(action, project_id, actor, request_id, payload,
                                require_committed=before_commit is not None)
        if receipt is not None:
            guard()
            return receipt  # Even unknown: do not generate a second identity.
        if check_only:
            raise AIEmployeeContractError("outcome_unknown", "未取得原采用的完整回执；未继续写入。")
        record = self._package_record(package_id, package_version, expected_fingerprint, for_adoption=True)
        if before_commit is not None and (record["is_current_verified"] is not True
                or type(record.get("revision")) is not str or not _SHA.fullmatch(record["revision"])):
            raise AIEmployeeContractError("publication_index_unverified", "采用需要当前不可变发布锚。")
        manifest = record["manifest"]
        record_bytes = canonical_json(record)
        stamp = _now()
        instance = {"schema_version": 1, "instance_id": "ei-" + secrets.token_hex(8), "project_id": project_id,
                    "package_id": package_id, "package_version": package_version, "package_fingerprint": expected_fingerprint,
                    "instance_revision": 1, "display_name": manifest.get("name", package_id),
                    "adoption": {"adopted_by": actor, "adopted_at": stamp, "note": note, "decision_id": None, "previous": None},
                    "experience_scope": "project", **deepcopy(config), "intake_state": "open", "execution_state": "active",
                    "state_changed_at": stamp, "state_changed_by": actor, "created_at": stamp, "created_by": actor,
                    "updated_at": stamp, "updated_by": actor, "change_note": note}
        instance["revision_fingerprint"] = instance_revision_fingerprint(instance)
        self._validate(instance, manifest)
        instance_bytes = canonical_json(instance)
        def guarded_commit() -> None:
            guard()
            current = self._package_record(package_id, package_version, expected_fingerprint, for_adoption=True)
            if canonical_json(current) != record_bytes or canonical_json(instance) != instance_bytes:
                raise AIEmployeeContractError("fingerprint_mismatch", "采用前固定包或候选内容发生变化。")
            self._validate(instance, current["manifest"])
            guard()
        result = self._save(instance, action=action, actor=actor, request_id=request_id, payload=payload,
                            before_commit=guarded_commit if before_commit is not None else None)
        guard()
        return result

    def revise(self, project_id: str, instance_id: str, changes: dict, *, expected_revision: int,
               actor: str, request_id: str, note: str = "") -> dict:
        _revision(expected_revision)
        if not isinstance(changes, dict) or not changes or set(changes) - _REVISABLE:
            raise AIEmployeeContractError("request_invalid", "仅能修改岗位开放的配置项。")
        if not isinstance(note, str) or len(note) > 4000:
            raise AIEmployeeContractError("request_invalid", "修订说明无效。")
        action = "employee_instance_revise"
        payload = {"instance_id": instance_id, "changes": changes, "expected_revision": expected_revision, "note": note}
        receipt = self._receipt(action, project_id, actor, request_id, payload)
        if receipt is not None:
            return receipt
        old = self._load(project_id, instance_id)
        self._assert_revision(old[0], expected_revision)
        manifest = self._package(old[0]["package_id"], old[0]["package_version"], old[0]["package_fingerprint"])
        instance = {**deepcopy(old[0]), **deepcopy(changes), "instance_revision": expected_revision + 1,
                    "updated_at": _now(), "updated_by": actor, "change_note": note}
        instance["revision_fingerprint"] = instance_revision_fingerprint(instance)
        self._validate(instance, manifest)
        return self._save(instance, action=action, actor=actor, request_id=request_id, payload=payload, old=old,
                          retest=sorted(key for key in changes if key in _RETEST and changes[key] != old[0].get(key)))

    def revise_limits(self, project_id: str, instance_id: str, limits: dict, *, expected_revision: int,
                      actor: str, request_id: str, note: str = "", before_commit: Callable[[], None]) -> dict:
        """Replace limits on the original fixed package; never authorize execution.

        The required callback is server-owned and synchronous. The original
        request remains unresolved unless its durable receipt is committed;
        no prepared intent is resumed, even when its after hashes are present.
        """
        possible_write = False
        checking_receipt = False
        retained_receipt = False
        failure_code = "storage_record_invalid"
        try:
            _valid(project_id, _PROJECT, "project_id")
            _valid(instance_id, _INSTANCE, "instance_id")
            _revision(expected_revision)
            if (type(limits) is not dict or type(note) is not str or len(note) > 4000
                    or not callable(before_commit)):
                raise AIEmployeeContractError("request_invalid", "岗位上限修订请求无效。")
            limits_bytes = canonical_json(limits)
            frozen_limits = json.loads(limits_bytes)
            effective_limits(frozen_limits)  # Structure only; no cap or grant is enforced here.
            action = "employee_instance_revise"
            payload = {"instance_id": instance_id, "changes": {"limits": frozen_limits},
                       "expected_revision": expected_revision, "note": note}
            payload_bytes = canonical_json(payload)

            def guard() -> None:
                AIEmployeeFileStore._before_commit(before_commit)
                if (canonical_json(limits) != limits_bytes or canonical_json(frozen_limits) != limits_bytes
                        or canonical_json(payload) != payload_bytes):
                    raise AIEmployeeContractError("request_invalid", "岗位上限修订请求发生变化。")

            guard()  # Before the first private receipt or instance/history read.
            checking_receipt = True
            receipt = self._receipt(action, project_id, actor, request_id, payload, require_committed=True)
            checking_receipt = False
            if receipt is not None:
                retained_receipt = True
                guard()
                return receipt  # Committed replay or fixed unknown; never roll forward.
            guard()
            old = self._load(project_id, instance_id)
            self._assert_revision(old[0], expected_revision)
            package_identity = (old[0]["package_id"], old[0]["package_version"], old[0]["package_fingerprint"])
            manifest = self._package(*package_identity)
            manifest_bytes = canonical_json(manifest)
            instance = {**deepcopy(old[0]), "limits": deepcopy(frozen_limits),
                        "instance_revision": expected_revision + 1,
                        "updated_at": _now(), "updated_by": actor, "change_note": note}
            instance["revision_fingerprint"] = instance_revision_fingerprint(instance)
            self._validate(instance, manifest)
            instance_bytes = canonical_json(instance)
            retest = sorted(key for key in payload["changes"]
                            if key in _RETEST and payload["changes"][key] != old[0].get(key))

            def guarded_commit() -> None:
                nonlocal possible_write
                guard()
                # Storage invokes this under its original lock after both CAS
                # checks/preflight and before its first durable intent write.
                fixed_manifest = self._package(*package_identity)
                if canonical_json(fixed_manifest) != manifest_bytes:
                    raise AIEmployeeContractError("fingerprint_mismatch", "固定岗位依据发生变化。")
                self._validate(instance, fixed_manifest)
                guard()
                if canonical_json(instance) != instance_bytes:
                    raise AIEmployeeContractError("request_invalid", "岗位上限修订候选发生变化。")
                possible_write = True

            result = self._save(instance, action=action, actor=actor, request_id=request_id,
                                payload=payload, old=old, retest=retest, before_commit=guarded_commit)
            guard()
            return result
        except AIEmployeeContractError as exc:
            if possible_write or retained_receipt or (checking_receipt and exc.code != "request_id_reused"):
                failure_code = "outcome_unknown"
            elif type(exc.code) is str and exc.code in {
                "request_invalid", "owner_required", "not_allowed_in_status", "revision_conflict",
                "fingerprint_mismatch", "request_id_reused", "validation_failed", "version_not_available",
                "instance_not_found", "storage_record_invalid", "storage_path_invalid", "storage_busy",
                "outcome_unknown",
            }:
                failure_code = exc.code
        except Exception:
            if possible_write or retained_receipt or checking_receipt:
                failure_code = "outcome_unknown"
        # Outside either handler: never retain a private source error context.
        message = ("岗位上限保存结果待核实；不要更换原请求重做。" if failure_code == "outcome_unknown"
                   else "岗位上限修订未能完成核验。")
        raise AIEmployeeContractError(failure_code, message) from None

    def model_candidates(self, project_id: str, instance_id: str, *, expected_revision: int,
                         expected_fingerprint: str, before_read: Callable[[], None]) -> dict:
        """Read current finite catalogue choices, not private settings or readiness."""
        failure_code = "storage_record_invalid"
        try:
            if type(project_id) is not str or type(instance_id) is not str:
                raise AIEmployeeContractError("request_invalid", "模型候选的对象标识无效。")
            _valid(project_id, _PROJECT, "project_id")
            _valid(instance_id, _INSTANCE, "instance_id")
            _model_revision(expected_revision)
            _model_sha(expected_fingerprint)
            if not callable(before_read):
                raise AIEmployeeContractError("request_invalid", "模型候选缺少服务端读取围栏。")
            source = _model_source(getattr(self, "model_catalogue_source", None))
            package_loader = self.package_loader

            def guard() -> None:
                if self.model_catalogue_source is not source or self.package_loader is not package_loader:
                    raise AIEmployeeContractError("model_catalogue_changed", "模型候选的固定来源发生变化。")
                AIEmployeeFileStore._before_commit(before_read)
                if self.model_catalogue_source is not source or self.package_loader is not package_loader:
                    raise AIEmployeeContractError("model_catalogue_changed", "模型候选的固定来源发生变化。")

            guard()  # Before the first private head/history read.
            budget = AIEmployeeReadBudget()
            old = self._load(project_id, instance_id, read_budget=budget)
            self._assert_revision(old[0], expected_revision)
            if old[0]["revision_fingerprint"] != expected_fingerprint:
                raise AIEmployeeContractError("fingerprint_mismatch", "岗位的固定修订指纹发生变化。")
            package_identity = (old[0]["package_id"], old[0]["package_version"], old[0]["package_fingerprint"])
            manifest = self._package(*package_identity)
            manifest_bytes = canonical_json(manifest)
            guard()
            observation = source()
            projection = _model_projection(observation)
            observed_version, observed_revision, observed_bytes = (
                observation.projection_version, observation.revision, observation.projection_bytes)
            final = self._load(project_id, instance_id, read_budget=budget)
            if final[1] != old[1] or final[2] != old[2]:
                self._assert_revision(final[0], expected_revision)
                raise AIEmployeeContractError("fingerprint_mismatch", "读取期间岗位的原修订发生变化。")
            if canonical_json(self._package(*package_identity)) != manifest_bytes:
                raise AIEmployeeContractError("fingerprint_mismatch", "读取期间固定岗位依据发生变化。")
            _model_recheck_snapshot(observation, projection_version=observed_version,
                                    revision=observed_revision, projection_bytes=observed_bytes)
            guard()  # Original owner/request and fixed callables, after source checks.
            return {
                "selection_scope": "instance_model_configuration", "catalogue_source": "static_model_catalogue",
                "projection_version": observed_version,
                "model_catalogue_revision": observed_revision,
                "instance_id": instance_id, "instance_revision": expected_revision,
                "revision_fingerprint": expected_fingerprint,
                "package_id": package_identity[0], "package_version": package_identity[1],
                "package_fingerprint": package_identity[2], "candidates": projection["candidates"],
                "revision": expected_revision, "fingerprint": expected_fingerprint,
                "effects_produced": False, "problems": [],
                "allowed_actions": ["employee_instance_model_candidates", "employee_instance_revise"],
            }
        except _ModelCatalogueError as exc:
            if type(exc) is _ModelCatalogueError and type(exc.code) is str and exc.code in _MODEL_SOURCE_ERRORS:
                failure_code = exc.code
            else:
                failure_code = "model_catalogue_unavailable"
        except AIEmployeeContractError as exc:
            if type(exc.code) is str and exc.code in _MODEL_SAFE_ERRORS:
                failure_code = exc.code
        except Exception:
            pass
        # Do not carry a private loader/catalogue exception through the RPC.
        raise AIEmployeeContractError(failure_code, "模型候选未能完成核验。") from None

    def revise_model_config(self, project_id: str, instance_id: str, model_config: dict, *,
                            expected_revision: int, expected_instance_fingerprint: str,
                            expected_model_catalogue_revision: str, actor: str, request_id: str,
                            note: str = "", before_commit: Callable[[], None], check_only: bool = False) -> dict:
        """Save a new explicit configuration on the original CAS/history chain.

        The source and authorization callbacks are host-owned. A committed
        receipt proves historical configuration persistence only; neither a
        current catalogue nor this method grants model or employee execution.
        """
        possible_write = False
        checking_receipt = False
        retained_receipt = False
        failure_code = "storage_record_invalid"
        try:
            if (type(project_id) is not str or type(instance_id) is not str
                    or type(actor) is not str or type(request_id) is not str):
                raise AIEmployeeContractError("request_invalid", "模型设置的请求标识无效。")
            _valid(project_id, _PROJECT, "project_id")
            _valid(instance_id, _INSTANCE, "instance_id")
            _model_revision(expected_revision, saving=True)
            _model_sha(expected_instance_fingerprint)
            _model_sha(expected_model_catalogue_revision)
            if (type(check_only) is not bool or type(note) is not str or len(note) > 4000
                    or not callable(before_commit)):
                raise AIEmployeeContractError("request_invalid", "模型设置修订请求无效。")
            source = _model_source(getattr(self, "model_catalogue_source", None))
            package_loader = self.package_loader
            configuration_bytes = canonical_json(_model_configuration(model_config))
            frozen_configuration = json.loads(configuration_bytes)
            action = "employee_instance_revise"
            payload = {"instance_id": instance_id, "changes": {"model_config": frozen_configuration},
                       "expected_revision": expected_revision,
                       "expected_instance_fingerprint": expected_instance_fingerprint,
                       "expected_model_catalogue_revision": expected_model_catalogue_revision, "note": note}
            payload_bytes = canonical_json(payload)

            def guard() -> None:
                if self.model_catalogue_source is not source or self.package_loader is not package_loader:
                    raise AIEmployeeContractError("model_catalogue_changed", "模型选择的固定来源发生变化。")
                AIEmployeeFileStore._before_commit(before_commit)
                if self.model_catalogue_source is not source or self.package_loader is not package_loader:
                    raise AIEmployeeContractError("model_catalogue_changed", "模型选择的固定来源发生变化。")
                if (canonical_json(_model_configuration(model_config)) != configuration_bytes
                        or canonical_json(frozen_configuration) != configuration_bytes
                        or canonical_json(payload) != payload_bytes):
                    raise AIEmployeeContractError("request_invalid", "模型设置的原请求发生变化。")

            guard()  # Before any private receipt, instance or history observation.
            checking_receipt = True
            receipt = self._receipt(action, project_id, actor, request_id, payload, require_committed=True)
            checking_receipt = False
            if receipt is not None:
                retained_receipt = True
                guard()
                if (type(receipt) is dict and type(receipt.get("result")) is str
                        and receipt["result"] == "outcome_unknown"):
                    return _model_unknown(request_id)
                # Do not read today's catalogue or head to rewrite a past save.
                return _model_saved_receipt(
                    receipt, project_id=project_id, instance_id=instance_id, request_id=request_id,
                    expected_revision=expected_revision, catalogue_revision=expected_model_catalogue_revision,
                    configuration=frozen_configuration)
            guard()
            if check_only:
                return _model_unknown(request_id)  # No lock, intent or first write.
            budget = AIEmployeeReadBudget()
            old = self._load(project_id, instance_id, read_budget=budget)
            self._assert_revision(old[0], expected_revision)
            if old[0]["revision_fingerprint"] != expected_instance_fingerprint:
                raise AIEmployeeContractError("fingerprint_mismatch", "岗位的固定修订指纹发生变化。")
            package_identity = (old[0]["package_id"], old[0]["package_version"], old[0]["package_fingerprint"])
            manifest = self._package(*package_identity)
            manifest_bytes = canonical_json(manifest)
            guard()
            observation = source()
            projection = _model_projection(observation)
            observed_version, observed_revision, observed_bytes = (
                observation.projection_version, observation.revision, observation.projection_bytes)
            if observed_revision != expected_model_catalogue_revision:
                raise AIEmployeeContractError("model_catalogue_changed", "原模型目录修订发生变化，请重新读取候选。")
            _model_selected(projection, frozen_configuration)
            instance = {**deepcopy(old[0]), "model_config": deepcopy(frozen_configuration),
                        "instance_revision": expected_revision + 1,
                        "updated_at": _now(), "updated_by": actor, "change_note": note}
            instance["revision_fingerprint"] = instance_revision_fingerprint(instance)
            self._validate(instance, manifest)
            instance_bytes = canonical_json(instance)
            retest = ["model_config"] if frozen_configuration != old[0].get("model_config") else []
            selection_receipt = {"model_catalogue_revision": observed_revision,
                                 "model_config": deepcopy(frozen_configuration)}
            selection_receipt_bytes = canonical_json(selection_receipt)

            def guarded_commit() -> None:
                nonlocal possible_write
                guard()
                fixed_manifest = self._package(*package_identity)
                if canonical_json(fixed_manifest) != manifest_bytes:
                    raise AIEmployeeContractError("fingerprint_mismatch", "固定岗位依据发生变化。")
                _model_recheck_snapshot(observation, projection_version=observed_version,
                                        revision=observed_revision, projection_bytes=observed_bytes)
                _model_selected(projection, frozen_configuration)
                self._validate(instance, fixed_manifest)
                guard()
                if (canonical_json(instance) != instance_bytes
                        or canonical_json(selection_receipt) != selection_receipt_bytes):
                    raise AIEmployeeContractError("request_invalid", "模型设置的冻结候选发生变化。")
                # Original FileStore invokes this after both CAS checks under
                # its lock, before the first durable intent/business write.
                possible_write = True

            result = self._save(instance, action=action, actor=actor, request_id=request_id,
                                payload=payload, old=old, retest=retest, before_commit=guarded_commit,
                                model_selection_receipt=selection_receipt)
            retained_receipt = True  # Includes a concurrent committed/prepared observation.
            guard()
            if canonical_json(self._package(*package_identity)) != manifest_bytes:
                raise AIEmployeeContractError("outcome_unknown", "模型设置提交后的固定岗位依据待核实。")
            _model_recheck_snapshot(observation, projection_version=observed_version,
                                    revision=observed_revision, projection_bytes=observed_bytes)
            if (canonical_json(instance) != instance_bytes
                    or canonical_json(selection_receipt) != selection_receipt_bytes):
                raise AIEmployeeContractError("outcome_unknown", "模型设置提交后的冻结依据待核实。")
            guard()
            if (type(result) is dict and type(result.get("result")) is str
                    and result["result"] == "outcome_unknown"):
                return _model_unknown(request_id)
            return _model_saved_receipt(
                result, project_id=project_id, instance_id=instance_id, request_id=request_id,
                expected_revision=expected_revision, catalogue_revision=expected_model_catalogue_revision,
                configuration=frozen_configuration, fingerprint=instance["revision_fingerprint"], retest=retest)
        except _ModelCatalogueError as exc:
            if possible_write or retained_receipt or checking_receipt:
                failure_code = "outcome_unknown"
            elif type(exc) is _ModelCatalogueError and type(exc.code) is str and exc.code in _MODEL_SOURCE_ERRORS:
                failure_code = exc.code
            else:
                failure_code = "model_catalogue_unavailable"
        except AIEmployeeContractError as exc:
            if possible_write or retained_receipt or (checking_receipt and exc.code != "request_id_reused"):
                failure_code = "outcome_unknown"
            elif type(exc.code) is str and exc.code in _MODEL_SAFE_ERRORS:
                failure_code = exc.code
        except Exception:
            if possible_write or retained_receipt or checking_receipt:
                failure_code = "outcome_unknown"
        # BaseException, including cancellation, is never caught or replaced.
        # Ordinary private exceptions leave no chained source details here.
        message = ("模型设置保存结果待核实；不要更换原请求重做。" if failure_code == "outcome_unknown"
                   else "模型设置修订未能完成核验。")
        raise AIEmployeeContractError(failure_code, message) from None

    @staticmethod
    def _assert_revision(instance: dict, expected: int) -> None:
        if instance["instance_revision"] != expected:
            raise AIEmployeeContractError("revision_conflict", "岗位已有新修订，请保留修改后重新读取。",
                                         details={"current_revision": instance["instance_revision"]})

    def adoption_preview(self, project_id: str, instance_id: str, *, package_id: str,
                         package_version: str, expected_fingerprint: str, expected_revision: int) -> dict:
        """Owner adapter read seam: compare fixed versions, never authorize adoption.

        Request identity/revision are checked before any package observation.
        Only already-verified immutable package loaders may be supplied by host.
        Readback detects observed changes; it is not a cross-store atomic lease.
        No context/readiness provider, receipt recovery or write API is invoked.
        """
        _valid(project_id, _PROJECT, "project_id")
        _valid(instance_id, _INSTANCE, "instance_id")
        _valid(package_id, _PACKAGE, "package_id")
        _valid(expected_fingerprint, _SHA, "expected_fingerprint")
        if (type(expected_revision) is not int or not 1 <= expected_revision <= 2**53 - 1
                or type(package_version) is not str or not 0 < len(package_version) <= 128
                or package_version.strip() != package_version
                or any(ord(char) < 32 or ord(char) == 127 for char in package_version)):
            raise AIEmployeeContractError("request_invalid", "采用预览必须明确版本和当前整数修订。")
        loaded = self._load(project_id, instance_id)
        instance = loaded[0]
        self._assert_revision(instance, expected_revision)
        if package_id != instance["package_id"]:
            raise AIEmployeeContractError("request_invalid", "采用预览只能比较当前实例的同一岗位包。")
        current_record = self._package_record(package_id, instance["package_version"], instance["package_fingerprint"])
        candidate_record = self._package_record(package_id, package_version, expected_fingerprint)
        current, candidate = current_record["manifest"], candidate_record["manifest"]
        before, after = _adoption_projection(current), _adoption_projection(candidate)
        differences = [{"section": section, "changed": canonical_json(before[section]) != canonical_json(after[section]),
                        "before": before[section], "after": after[section]}
                       for section in ADOPTION_PREVIEW_KEYS]
        updated = {**deepcopy(instance), "package_version": package_version, "package_fingerprint": expected_fingerprint}
        updated["revision_fingerprint"] = instance_revision_fingerprint(updated)
        problems = validate_instance(updated, candidate, expected_project_id=project_id)
        # This comparison is not runtime/test evidence. The write route exists,
        # but availability remains the current immutable package source's fact.
        gaps = ["evaluation_source_unverified"]
        if not candidate_record["is_current_verified"]:
            gaps.append("publication_index_unverified")
        if candidate_record["status"] == "retired":
            gaps.append("package_retired")
        if problems:
            gaps.append("private_configuration_incompatible")
        # Recheck fixed records and original instance/head-history bytes. Never
        # substitute current/latest records or silently advance the read baseline.
        if (canonical_json(self._package_record(package_id, instance["package_version"], instance["package_fingerprint"])) != canonical_json(current_record)
                or canonical_json(self._package_record(package_id, package_version, expected_fingerprint)) != canonical_json(candidate_record)):
            raise AIEmployeeContractError("storage_record_invalid", "读取期间岗位发布依据发生变化。")
        final = self._load(project_id, instance_id)
        if final[1] != loaded[1] or final[2] != loaded[2]:
            self._assert_revision(final[0], expected_revision)
            raise AIEmployeeContractError("storage_record_invalid", "读取期间岗位原修订发生变化。")
        return {"project_id": project_id, "instance_id": instance_id, "package_id": package_id,
                "revision": instance["instance_revision"], "fingerprint": instance["revision_fingerprint"],
                "from_version": instance["package_version"], "to_version": package_version,
                "from_fingerprint": instance["package_fingerprint"], "to_fingerprint": expected_fingerprint,
                "preview_scope": "adoption_comparison", "differences": differences,
                "target_status": candidate_record["status"], "is_current_verified": candidate_record["is_current_verified"],
                "new_adoption_allowed": candidate_record["new_adoption_allowed"], "gaps": gaps,
                "verification_scope_kind": "declared_package_profile_not_trial_evidence",
                "private_configuration_policy": "preserved_not_projected",
                "problems": problems, "effects_produced": False,
                "allowed_actions": ["employee_instance_detail", "employee_instance_adoption_preview"]}

    def adopt(self, project_id: str, instance_id: str, *, package_version: str, expected_fingerprint: str,
              expected_revision: int, actor: str, request_id: str, note: str = "", rollback: bool = False,
              before_commit: Callable[[], None] | None = None, check_only: bool = False) -> dict:
        _revision(expected_revision)
        if type(rollback) is not bool or not isinstance(note, str) or len(note) > 4000:
            raise AIEmployeeContractError("request_invalid", "采用参数无效。")
        action = "employee_instance_rollback" if rollback else "employee_instance_adopt"
        payload = {"instance_id": instance_id, "package_version": package_version, "expected_fingerprint": expected_fingerprint,
                   "expected_revision": expected_revision, "note": note}
        if type(check_only) is not bool or check_only and before_commit is None:
            raise AIEmployeeContractError("request_invalid", "采用核实必须有当前授权围栏。")
        frozen_payload = canonical_json(payload)
        def guard() -> None:
            AIEmployeeFileStore._before_commit(before_commit)
            if canonical_json(payload) != frozen_payload:
                raise AIEmployeeContractError("request_invalid", "采用请求内容发生变化。")
        guard()
        receipt = self._receipt(action, project_id, actor, request_id, payload,
                                require_committed=before_commit is not None)
        if receipt is not None:
            guard()
            return receipt
        if check_only:
            raise AIEmployeeContractError("outcome_unknown", "未取得原采用的完整回执；未继续写入。")
        old = self._load(project_id, instance_id)
        self._assert_revision(old[0], expected_revision)
        record = self._package_record(old[0]["package_id"], package_version, expected_fingerprint, for_adoption=True)
        if before_commit is not None and (record["is_current_verified"] is not True
                or type(record.get("revision")) is not str or not _SHA.fullmatch(record["revision"])):
            raise AIEmployeeContractError("publication_index_unverified", "采用需要当前不可变发布锚。")
        manifest, record_bytes = record["manifest"], canonical_json(record)
        if rollback and not any(row["snapshot"]["package_version"] == package_version and
                                row["snapshot"]["package_fingerprint"] == expected_fingerprint for row in old[3]):
            raise AIEmployeeContractError("version_not_available", "回退版本不在本岗位的采用历史中。")
        stamp = _now()
        instance = {**deepcopy(old[0]), "package_version": package_version, "package_fingerprint": expected_fingerprint,
                    "instance_revision": expected_revision + 1, "updated_at": stamp, "updated_by": actor, "change_note": note,
                    "adoption": {"adopted_by": actor, "adopted_at": stamp, "note": note, "decision_id": None,
                                 "previous": {"version": old[0]["package_version"], "fingerprint": old[0]["package_fingerprint"]}}}
        instance["revision_fingerprint"] = instance_revision_fingerprint(instance)
        self._validate(instance, manifest)
        instance_bytes = canonical_json(instance)
        def guarded_commit() -> None:
            guard()
            current = self._package_record(old[0]["package_id"], package_version, expected_fingerprint, for_adoption=True)
            if canonical_json(current) != record_bytes or canonical_json(instance) != instance_bytes:
                raise AIEmployeeContractError("fingerprint_mismatch", "采用前固定包或候选内容发生变化。")
            self._validate(instance, current["manifest"])
            guard()
        result = self._save(instance, action=action, actor=actor, request_id=request_id, payload=payload, old=old,
                          retest=["package_version", "model_config", "capability_impls"],
                          before_commit=guarded_commit if before_commit is not None else None)
        guard()
        return result


class EmployeeBindingRepository:
    """The project-side map only. Portable workflow drafts keep package refs.

    The caller supplies a trusted workflow draft binding from server storage,
    never a browser-provided replacement for that binding. Updating a workflow
    draft itself belongs to update_package_nodes and is deliberately separate.
    """

    def __init__(self, instances: EmployeeInstanceRepository) -> None:
        self.instances = instances
        self.store = instances.store

    @staticmethod
    def _relative(project_id: str) -> str:
        return f"projects/{_valid(project_id, _PROJECT, 'project_id')}/employee_bindings.json"

    def _load(self, project_id: str, *,
              read_budget: AIEmployeeReadBudget | None = None) -> tuple[dict, bytes | None]:
        project_id = self.instances._stable_project(project_id)
        relative = self._relative(project_id)
        raw = (self.store.read(relative) if read_budget is None
               else self.store.read(relative, read_budget=read_budget))
        if raw is None:
            return {"schema_version": 1, "project_id": project_id, "binding_revision": 0, "bindings": []}, None
        table = _json(raw, read_budget=read_budget)
        if (set(table) != {"schema_version", "project_id", "binding_revision", "bindings"}
                or type(table.get("schema_version")) is not int or table["schema_version"] != 1
                or table.get("project_id") != project_id
                or type(table.get("binding_revision")) is not int or table["binding_revision"] < 1
                or not isinstance(table.get("bindings"), list) or len(table["bindings"]) > 2000):
            raise AIEmployeeContractError("storage_record_invalid", "本项目的岗位解析表无效。")
        keys = set()
        for row in table["bindings"]:
            if (not isinstance(row, dict) or set(row) != {"workflow_id", "node_id", "binding_slot", "instance_id",
                    "resolved_by", "resolved_at", "source", "binding_revision"}
                    or row.get("binding_slot") != "executor"
                    or row.get("source") not in {"human_choice", "single_candidate"}
                    or type(row.get("binding_revision")) is not int
                    or not 1 <= row["binding_revision"] <= table["binding_revision"]
                    or not all(isinstance(row.get(key), str) and 0 < len(row[key]) <= 128
                               for key in ("workflow_id", "node_id", "resolved_by", "resolved_at"))
                    or not isinstance(row.get("instance_id"), str) or not _INSTANCE.fullmatch(row["instance_id"])):
                raise AIEmployeeContractError("storage_record_invalid", "岗位解析项无效。")
            key = (row["workflow_id"], row["node_id"], row["binding_slot"])
            if key in keys:
                raise AIEmployeeContractError("storage_record_invalid", "岗位解析表存在重复席位。")
            keys.add(key)
        return table, raw

    @staticmethod
    def _key(workflow_id: str, node_id: str, binding_slot: str) -> tuple[str, str, str]:
        if not all(isinstance(value, str) and 0 < len(value) <= 128 and not any(ord(c) < 32 for c in value)
                   for value in (workflow_id, node_id)):
            raise AIEmployeeContractError("request_invalid", "流程和节点必须明确。")
        if binding_slot != "executor":
            raise AIEmployeeContractError("employee_binding_collaborator_unsupported", "协作者席位尚未接入。")
        return workflow_id, node_id, binding_slot

    def _resolve(self, project_id: str, binding: dict, *, workflow_id: str, node_id: str,
                 binding_slot: str, selected_instance_id: str | None = None) -> tuple[dict, dict, bytes | None]:
        self._key(workflow_id, node_id, binding_slot)
        problems = validate_binding(binding)
        if problems:
            raise AIEmployeeContractError("validation_failed", "节点员工绑定无效。", details={"problems": problems})
        table, raw = self._load(project_id)
        views = self.instances.list(project_id)["instances"]
        resolution = resolve_binding(binding, project_id=project_id, workflow_id=workflow_id, node_id=node_id,
                                     instances=[view["instance"] for view in views], mappings=table["bindings"],
                                     selected_instance_id=selected_instance_id, binding_slot=binding_slot)
        return resolution, table, raw

    def preview(self, project_id: str, binding: dict, *, workflow_id: str, node_id: str,
                binding_slot: str = "executor") -> dict:
        result, table, _ = self._resolve(project_id, binding, workflow_id=workflow_id, node_id=node_id,
                                         binding_slot=binding_slot)
        instance = result["instance"]
        return {"instance_id": instance["instance_id"], "instance_revision": instance["instance_revision"],
                "revision_fingerprint": instance["revision_fingerprint"], "source": result["source"],
                "binding_revision": table["binding_revision"], "effects_produced": False,
                **self.instances._readiness(instance)}

    @staticmethod
    def _safe_revision(value: Any, *, minimum: int = 0, maximum: int = _MAX_SAFE_INTEGER) -> int:
        if type(value) is not int or not minimum <= value <= maximum:
            raise AIEmployeeContractError("request_invalid", "岗位选择修订必须是安全整数。")
        return value

    def _candidate_rows(self, project_id: str, binding: dict, *, workflow_id: str, node_id: str,
                        read_budget: AIEmployeeReadBudget | None = None) -> list[dict]:
        raw = (self.instances.list(project_id) if read_budget is None
               else self.instances.list(project_id, read_budget=read_budget))
        if type(raw) is not dict or type(raw.get("instances")) is not list or len(raw["instances"]) > 500:
            raise AIEmployeeContractError("storage_record_invalid", "岗位候选来源尚不能核实。")
        rows, seen = [], set()
        for view in raw["instances"]:
            instance = view.get("instance") if type(view) is dict else None
            # Filter foreign scope/package before examining private configuration.
            if type(instance) is not dict or instance.get("project_id") != project_id or instance.get("package_id") != binding["package_id"]:
                continue
            identity = instance.get("instance_id")
            try:
                resolve_binding(binding, project_id=project_id, workflow_id=workflow_id, node_id=node_id,
                                instances=[instance], mappings=[], selected_instance_id=identity, binding_slot="executor")
            except AIEmployeeContractError as exc:
                if exc.code == "binding_unresolved":
                    continue  # A valid same-package instance outside the version constraint.
                raise
            self._safe_revision(instance.get("instance_revision"), minimum=1)
            if (type(identity) is not str or not _INSTANCE.fullmatch(identity) or identity in seen
                    or type(instance.get("display_name")) is not str or len(instance["display_name"]) > 256
                    or any(ord(c) < 32 or ord(c) == 127 for c in instance["display_name"])
                    or type(instance.get("revision_fingerprint")) is not str or not _SHA.fullmatch(instance["revision_fingerprint"])
                    or type(instance.get("package_fingerprint")) is not str or not _SHA.fullmatch(instance["package_fingerprint"])
                    or type(view.get("ready")) is not bool or type(view.get("gaps")) is not list
                    or len(view["gaps"]) > 500
                    or instance.get("intake_state") not in {"open", "paused"}
                    or instance.get("execution_state") not in {"active", "paused", "disabled"}
                    or any(type(gap) is not str or not _PUBLIC_GAP.fullmatch(gap) for gap in view["gaps"])
                    or len(set(view["gaps"])) != len(view["gaps"])
                    or (view["ready"] and (view["gaps"] or instance["intake_state"] != "open" or instance["execution_state"] != "active"))):
                raise AIEmployeeContractError("storage_record_invalid", "岗位候选来源尚不能核实。")
            # The fixed package must really be readable. Missing pricing/runtime
            # readiness is separate and never made ready by a map selection.
            manifest = self.instances._package(instance["package_id"], instance["package_version"], instance["package_fingerprint"])
            self.instances._validate(instance, manifest)
            seen.add(identity)
            rows.append({**{key: deepcopy(instance[key]) for key in BINDING_CANDIDATE_KEYS - {"ready", "gaps"}},
                         "ready": view["ready"], "gaps": list(view["gaps"])})
        return sorted(rows, key=lambda row: row["instance_id"])

    def candidates(self, project_id: str, binding: dict, *, workflow_id: str, node_id: str,
                   binding_slot: str = "executor") -> dict:
        self._key(workflow_id, node_id, binding_slot)
        if validate_binding(binding):
            raise AIEmployeeContractError("validation_failed", "节点员工绑定无效。")
        read_budget = AIEmployeeReadBudget()
        table, before = self._load(project_id, read_budget=read_budget)
        self._safe_revision(table["binding_revision"])
        rows = self._candidate_rows(project_id, binding, workflow_id=workflow_id, node_id=node_id,
                                    read_budget=read_budget)
        # Re-read revisions/readiness and table; this is observation, not a lease.
        final = self._candidate_rows(project_id, binding, workflow_id=workflow_id, node_id=node_id,
                                     read_budget=read_budget)
        if rows != final or self._load(project_id, read_budget=read_budget)[1] != before:
            raise AIEmployeeContractError("storage_record_invalid", "岗位选择读取期间发生变化。")
        matched = [row for row in table["bindings"] if
                   (row["workflow_id"], row["node_id"], row["binding_slot"]) == (workflow_id, node_id, binding_slot)]
        identities = {row["instance_id"] for row in rows}
        selected = matched[0]["instance_id"] if len(matched) == 1 and matched[0]["instance_id"] in identities else None
        return {"candidates": rows, "selected_instance_id": selected, "binding_revision": table["binding_revision"],
                "revision": table["binding_revision"], "effects_produced": False,
                "allowed_actions": ["employee_binding_candidates", "employee_binding_resolve_set"]}

    def _assert_selection(self, project_id: str, binding: dict, *, workflow_id: str, node_id: str,
                          instance_id: str, expected_instance_revision: int, expected_instance_fingerprint: str) -> None:
        current = self.instances._load(project_id, instance_id)[0]
        if type(current.get("instance_revision")) is not int or current["instance_revision"] != expected_instance_revision:
            raise AIEmployeeContractError("revision_conflict", "所选岗位已有新修订。")
        if current.get("revision_fingerprint") != expected_instance_fingerprint:
            raise AIEmployeeContractError("fingerprint_mismatch", "所选岗位原指纹已变化。")
        resolve_binding(binding, project_id=project_id, workflow_id=workflow_id, node_id=node_id,
                        instances=[current], mappings=[], selected_instance_id=instance_id, binding_slot="executor")
        manifest = self.instances._package(current["package_id"], current["package_version"], current["package_fingerprint"])
        self.instances._validate(current, manifest)

    def resolve_set(self, project_id: str, binding: dict, *, workflow_id: str, node_id: str,
                    instance_id: str, expected_binding_revision: int, actor: str, request_id: str,
                    binding_slot: str = "executor", expected_workflow_revision: str | None = None,
                    expected_instance_revision: int | None = None, expected_instance_fingerprint: str | None = None,
                    before_commit: Callable[[], None] | None = None, check_only: bool = False) -> dict:
        if type(check_only) is not bool:
            raise AIEmployeeContractError("request_invalid", "原请求核实模式无效。")
        guarded = any(value is not None for value in (expected_workflow_revision, expected_instance_revision,
                                                      expected_instance_fingerprint, before_commit)) or check_only
        if guarded:
            self._safe_revision(expected_binding_revision, maximum=_MAX_SAFE_INTEGER - 1)
            self._safe_revision(expected_instance_revision, minimum=1)
            _valid(expected_workflow_revision, _SHA, "expected_workflow_revision")
            _valid(expected_instance_fingerprint, _SHA, "expected_instance_fingerprint")
            if not callable(before_commit):
                raise AIEmployeeContractError("request_invalid", "岗位选择缺少服务端保存围栏。")
        _revision(expected_binding_revision, zero=True, binding=True)
        _valid(instance_id, _INSTANCE, "instance_id")
        key = self._key(workflow_id, node_id, binding_slot)
        action = "employee_binding_resolve_set"
        payload = {"workflow_id": workflow_id, "node_id": node_id, "binding_slot": binding_slot,
                   "instance_id": instance_id, "expected_binding_revision": expected_binding_revision,
                   "binding": binding}
        if guarded:
            payload.update(expected_workflow_revision=expected_workflow_revision,
                           expected_instance_revision=expected_instance_revision,
                           expected_instance_fingerprint=expected_instance_fingerprint)

        def guard() -> None:
            AIEmployeeFileStore._before_commit(before_commit)
            self._assert_selection(project_id, binding, workflow_id=workflow_id, node_id=node_id, instance_id=instance_id,
                                   expected_instance_revision=expected_instance_revision,
                                   expected_instance_fingerprint=expected_instance_fingerprint)

        try:
            receipt = self.instances._receipt(action, project_id, actor, request_id, payload, require_committed=guarded)
        except Exception as exc:
            if guarded and not (isinstance(exc, AIEmployeeContractError) and exc.code == "request_id_reused"):
                raise AIEmployeeContractError("outcome_unknown", "原保存回执尚不能核实。") from None
            raise
        if receipt is not None:
            if guarded:
                try:
                    guard()
                except Exception:
                    raise AIEmployeeContractError("outcome_unknown", "原保存回执之后的依据待核实。") from None
            return receipt
        if guarded:
            guard()  # First write/check_only still checks the original baseline.
        if check_only:
            return {"ok": False, "result": "outcome_unknown", "error": "outcome_unknown",
                    "effects_produced": None, "request_id": request_id}
        # An explicit human choice may replace a prior map. Resolve against the
        # other keys only; do not let stale mapping silently win over the choice.
        problems = validate_binding(binding)
        if problems:
            raise AIEmployeeContractError("validation_failed", "节点员工绑定无效。", details={"problems": problems})
        table, raw = self._load(project_id)
        if guarded:
            self._safe_revision(table["binding_revision"], maximum=_MAX_SAFE_INTEGER - 1)
        if expected_binding_revision != table["binding_revision"]:
            raise AIEmployeeContractError("binding_revision_conflict", "岗位选择已有新修订，请重新读取。",
                                         details={"current_binding_revision": table["binding_revision"]})
        views = self.instances.list(project_id)["instances"]
        remaining = [row for row in table["bindings"]
                     if (row["workflow_id"], row["node_id"], row["binding_slot"]) != key]
        result = resolve_binding(binding, project_id=project_id, workflow_id=workflow_id, node_id=node_id,
                                 instances=[view["instance"] for view in views], mappings=remaining,
                                 selected_instance_id=instance_id, binding_slot=binding_slot)
        next_revision = table["binding_revision"] + 1
        record = {"workflow_id": workflow_id, "node_id": node_id, "binding_slot": binding_slot,
                  "instance_id": result["instance"]["instance_id"], "resolved_by": actor, "resolved_at": _now(),
                  "source": "human_choice", "binding_revision": next_revision}
        updated = {**table, "binding_revision": next_revision,
                   "bindings": sorted([*remaining, record], key=lambda row: (row["workflow_id"], row["node_id"], row["binding_slot"]))}
        if len(updated["bindings"]) > 2000:
            raise AIEmployeeContractError("storage_record_invalid", "项目的岗位绑定数量超过本次保存范围。")
        response = {"ok": True, "action": action, "project_id": project_id, "request_id": request_id,
                    "instance_id": instance_id, "binding_revision": next_revision, "revision": next_revision,
                    "result": "success", "effects_produced": True, "problems": [],
                    "allowed_actions": ["employee_binding_preview"]}
        if guarded:
            response.update(workflow_id=workflow_id, node_id=node_id, binding_slot="executor",
                            workflow_revision=expected_workflow_revision, source="human_choice",
                            selection_scope="saved_node_selection", instance_revision=expected_instance_revision,
                            revision_fingerprint=expected_instance_fingerprint,
                            allowed_actions=["employee_binding_candidates", "employee_binding_resolve_set"])
        possible_write = False

        def guarded_commit() -> None:
            nonlocal possible_write
            guard()
            possible_write = True

        try:
            result = self.store.commit(action=action, project_id=project_id, actor=actor, request_id=request_id, payload=payload,
                                       changes=[{"path": self._relative(project_id), "before_sha256": _hash(raw),
                                                 "after_text": _text(updated)}], response=response,
                                       **({"before_commit": guarded_commit} if guarded else {}))
            if guarded:
                guard()
            return result
        except Exception:
            if guarded and possible_write:
                raise AIEmployeeContractError("outcome_unknown", "岗位选择保存后的依据或回执待核实。") from None
            raise
