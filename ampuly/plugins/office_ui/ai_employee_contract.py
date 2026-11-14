"""AE00 package contracts and content identities, with no runtime side effects.

All inputs are already loaded values or bytes. This module never opens paths,
imports the engine, grants capabilities, resolves remote schemas, or generates
timestamps, approval records or evaluation evidence. It deliberately returns
missing-field findings for incomplete design drafts.
"""
from __future__ import annotations

import codecs
import hashlib
import json
import math
import re
import unicodedata
from datetime import datetime
from typing import Any
from urllib.parse import unquote

MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_FILES = 500
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 50000
# Local validation ceilings, not runtime authorization or effective limits.
LOOP_LIMITS = {
    "max_steps": (1, 10000), "max_model_calls": (1, 10000),
    "max_wall_clock_s": (1, 86400), "max_rework": (0, 100),
    "max_retries": (0, 20), "max_sub_agents": (0, 64),
}
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_PACKAGE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{2,63}\Z")
_SKILL_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_DECIMAL = re.compile(r"(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,6})?\Z")
_WINDOWS_DEVICE = re.compile(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?\Z", re.I)
_SCHEMA_URI = "https://json-schema.org/draft/2020-12/schema"
_BINARY_SUFFIXES = (".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".zip", ".7z", ".gz", ".woff", ".woff2", ".ttf", ".mp3", ".mp4", ".wav")
_BINARY_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"%PDF-", b"\x89PNG\r\n\x1a\n", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", b"GIF87a", b"GIF89a")
_OPERATION_FIELDS = {
    "package_fingerprint", "status", "release_approval", "draft_revision",
    "latest_test", "evaluation_results", "evaluation_records", "evaluations",
    "test_results", "test_passed", "pending_review",
}
_REQUIRED = {
    "schema_version", "package_id", "version", "package_fingerprint", "status",
    "name", "responsibility", "scope_in", "scope_out", "human_slots",
    "input_schema", "output_schema", "artifact_contract", "capability_requirements",
    "model_profile", "loop_policy", "evaluation_profile", "provenance",
    "created_at", "created_by",
}
_OPTIONAL = {
    "checklist", "skills", "prompt_refs", "template_refs", "knowledge_protocol",
    "package_org_id", "enterprise_id", "published_at", "published_by",
    "retired_at", "retired_by", "retired_reason", "release_approval",
    "draft_revision", "legacy_source", "latest_test", "evaluation_results",
    "evaluation_records", "evaluations", "test_results",
}
_CUSTOMER_FIELDS = {
    "customer_id", "customer_name", "brand_name", "contact", "acceptance_records",
    "orders", "credentials", "env_values", "instance_private_values",
    "connector_refs", "human_assignments",
}
# Pure patterns adapted from ampuly.cli.distribution and distribution_candidate.
# They are kept local: importing those modules also imports runtime components.
_SECRET_KEY = re.compile(r"(api[_-]?key|app[_-]?secret|client[_-]?secret|secret[_-]?key|access[_-]?token|auth[_-]?token|refresh[_-]?token|password|passwd|private[_-]?key|credentials?)$", re.I)
_HASH_KEY = re.compile(r"(sha256|sha1|sha512|md5|fingerprint|digest|_hash|^hash)$", re.I)
_ENV_REF = re.compile(r"\$?\{?[A-Z][A-Z0-9_]{2,}\}?\Z")
_SECRET_VALUE = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abp]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|authorization)\b(?!_env)\s*[:=]\s*[\"']?[A-Za-z0-9_\-./+=]{16,}"),
    re.compile(r"(?i)\b(?:https?|postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^\s/:]+:[^\s/@]+@"),
    re.compile(r"[0-9a-f]{40,}\Z"),
)
_PRIVATE_NAMES = "|".join(sorted(_CUSTOMER_FIELDS))
_PRIVATE_TEXT_KEY = re.compile(r"(?im)(?:[\"'](?:" + _PRIVATE_NAMES + r")[\"']\s*:\s*\S+|^\s*(?:" + _PRIVATE_NAMES + r")\s*[:=]\s*\S+)")


class AIEmployeeContractError(ValueError):
    def __init__(self, code: str, message: str, path: str = "", details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path
        self.details = details


def _json_shape(value: Any) -> None:
    active: set[int] = set()
    remaining = MAX_JSON_NODES
    characters = MAX_MANIFEST_BYTES

    def walk(node: Any, depth: int) -> None:
        nonlocal remaining, characters
        remaining -= 1
        if depth > MAX_JSON_DEPTH or remaining < 0:
            raise AIEmployeeContractError("employee_package_limit", "JSON 结构超过安全范围。")
        if node is None or type(node) in (str, bool, int):
            if type(node) is str:
                characters -= len(node)
                if characters < 0:
                    raise AIEmployeeContractError("employee_package_limit", "JSON 文本超过安全范围。")
            if type(node) is int and node.bit_length() > 256:
                raise AIEmployeeContractError("employee_package_json_invalid", "JSON 整数过大。")
            return
        if type(node) is float:
            if not math.isfinite(node):
                raise AIEmployeeContractError("employee_package_json_invalid", "JSON 不允许 NaN 或无穷值。")
            return
        if type(node) not in (dict, list):
            raise AIEmployeeContractError("employee_package_json_invalid", "JSON 只允许标准对象、数组和标量。")
        identity = id(node)
        if identity in active:
            raise AIEmployeeContractError("employee_package_json_invalid", "JSON 不允许循环引用。")
        active.add(identity)
        try:
            if type(node) is dict:
                for key, child in node.items():
                    if type(key) is not str:
                        raise AIEmployeeContractError("employee_package_json_invalid", "JSON 对象键必须为字符串。")
                    characters -= len(key)
                    if characters < 0:
                        raise AIEmployeeContractError("employee_package_limit", "JSON 文本超过安全范围。")
                    walk(child, depth + 1)
            else:
                for child in node:
                    walk(child, depth + 1)
        finally:
            active.remove(identity)

    walk(value, 0)


def canonical_json(value: Any) -> bytes:
    """Unicode-codepoint key order, UTF-8, bounded strict JSON, no whitespace."""
    _json_shape(value)
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise AIEmployeeContractError("employee_package_json_invalid", "无法规范化 JSON 内容。") from exc
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise AIEmployeeContractError("employee_package_limit", "JSON 内容超过 2 MiB。")
    return encoded


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _portable_path(value: Any) -> str:
    if type(value) is not str or not value or len(value) > 512:
        raise AIEmployeeContractError("employee_package_file_path", "文件路径必须为稳定的包内相对路径。")
    if value != unicodedata.normalize("NFC", value) or value.startswith("/") or any(c in value for c in "\\:< >\"|?*".replace(" ", "")) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise AIEmployeeContractError("employee_package_file_path", "文件路径不是可移植相对路径。")
    parts = value.split("/")
    if any(not p or p in {".", ".."} or p.endswith((".", " ")) or len(p) > 255 or _WINDOWS_DEVICE.fullmatch(p) for p in parts):
        raise AIEmployeeContractError("employee_package_file_path", "文件路径包含越界、空段或系统保留名称。")
    if parts[-1].lower().endswith((".lnk", ".url", ".symlink")):
        raise AIEmployeeContractError("employee_package_file_path", "标准包不能声明链接文件。")
    return value


def _secret_shaped(key: str, value: Any, *, under_hash: bool = False) -> bool:
    if type(value) is not str or not value.strip():
        return False
    text = value.strip()
    if (under_hash or _HASH_KEY.search(key) or key == "source_revision") and re.fullmatch(r"[0-9a-fA-F]{32,128}", text):
        return False
    if _SECRET_KEY.search(key):
        return not _ENV_REF.fullmatch(text) and text.lower() not in {"none", "null", "~", "changeme", "<set-in-env>"}
    return any(pattern.search(text) for pattern in _SECRET_VALUE)


def _privacy_findings(value: Any, path: str = "", *, schema: bool = False, schema_names: bool = False, under_hash: bool = False) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    if type(value) is dict:
        for key, child in value.items():
            here = f"{path}.{key}" if path else key
            # Schema property names describe runtime inputs; they are not values.
            child_schema = schema or key in {"input_schema", "output_schema", "params_schema"}
            child_names = schema and key in {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
            if schema and key in {"default", "examples", "const", "enum"}:
                child_schema = False
                child_names = False
            if not schema_names and key in _CUSTOMER_FIELDS:
                findings.append(("employee_package_private_content", here))
            if type(child) in (dict, list):
                findings.extend(_privacy_findings(child, here, schema=child_schema, schema_names=child_names, under_hash=under_hash or bool(_HASH_KEY.search(key))))
            elif _secret_shaped(key, child, under_hash=under_hash):
                findings.append(("employee_package_secret", here))
    elif type(value) is list:
        for index, child in enumerate(value):
            here = f"{path}[{index}]"
            if type(child) in (dict, list):
                findings.extend(_privacy_findings(child, here, schema=schema, schema_names=schema_names, under_hash=under_hash))
            elif _secret_shaped("", child, under_hash=under_hash):
                findings.append(("employee_package_secret", here))
    return findings


def _declarations(manifest: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Deduplicate cross-list references, but reject duplicates within a list."""
    declared: dict[str, dict[str, str]] = {}
    aliases: dict[str, str] = {}
    groups: list[tuple[str, Any]] = [("prompt_refs", manifest.get("prompt_refs", [])), ("template_refs", manifest.get("template_refs", []))]
    provenance = manifest.get("provenance", {})
    if type(provenance) is not dict:
        raise AIEmployeeContractError("employee_package_invalid", "provenance 必须为对象。", "provenance")
    groups.append(("provenance.files", provenance.get("files", [])))
    for group, entries in groups:
        if type(entries) is not list:
            raise AIEmployeeContractError("employee_package_invalid", "文件声明必须为数组。", group)
        seen: set[str] = set()
        for index, entry in enumerate(entries):
            location = f"{group}[{index}]"
            if group == "prompt_refs":
                path = _portable_path(entry)
                metadata: dict[str, str] = {}
            elif type(entry) is dict:
                path = _portable_path(entry.get("path"))
                metadata = {k: entry[k] for k in ("kind", "encoding") if k in entry} if group == "provenance.files" else {}
                if group == "provenance.files":
                    if type(metadata.get("kind")) is not str or metadata["kind"] not in {"text", "binary"}:
                        raise AIEmployeeContractError("employee_package_file_kind", "文件对象必须显式声明 text 或 binary。", location)
                    if metadata["kind"] == "text":
                        if path.lower().endswith(_BINARY_SUFFIXES):
                            raise AIEmployeeContractError("employee_package_file_kind", "文档、媒体或归档格式必须按二进制原字节声明。", location)
                        if type(metadata.get("encoding")) is not str or not metadata["encoding"].strip():
                            raise AIEmployeeContractError("employee_package_file_encoding", "文本文件必须显式声明编码。", location)
                        try:
                            codecs.lookup(metadata["encoding"])
                        except LookupError as exc:
                            raise AIEmployeeContractError("employee_package_file_encoding", "文本编码不受支持。", location) from exc
                    elif "encoding" in metadata:
                        raise AIEmployeeContractError("employee_package_file_encoding", "二进制声明不能附带文本编码。", location)
            elif group == "provenance.files" and type(entry) is str:
                path = _portable_path(entry)
                # Compatibility for AE00 design drafts; no implicit text folding.
                metadata = {}
            else:
                raise AIEmployeeContractError("employee_package_invalid", "文件声明结构无效。", location)
            if path in seen:
                raise AIEmployeeContractError("employee_package_file_duplicate", "同一声明数组不能重复引用文件。", location)
            seen.add(path)
            alias = path.casefold()
            if alias in aliases and aliases[alias] != path:
                raise AIEmployeeContractError("employee_package_file_conflict", "文件路径在 Windows 上冲突。", location)
            aliases[alias] = path
            if path in declared and metadata and declared[path] and metadata != declared[path]:
                raise AIEmployeeContractError("employee_package_file_conflict", "同一文件的类型或编码声明冲突。", location)
            if path not in declared or metadata:
                declared[path] = metadata
    if len(declared) > MAX_FILES:
        raise AIEmployeeContractError("employee_package_limit", "包内文件不能超过 500 个。")
    for path in declared:
        parts = path.casefold().split("/")
        if any("/".join(parts[:i]) in aliases for i in range(1, len(parts))):
            raise AIEmployeeContractError("employee_package_file_conflict", "同一路径不能同时是文件和目录。")
    return declared


class _Validation:
    def __init__(self, complete: bool) -> None:
        self.complete = complete
        self.problems: list[dict[str, str]] = []

    def add(self, path: str, message: str, code: str = "employee_package_invalid", *, missing: bool = False, hint: str = "") -> None:
        self.problems.append({"code": code, "severity": "warning" if missing and not self.complete else "error", "path": path, "message": message, "hint": hint or ("补齐真实值后再校验；不要伪造服务端或评测证据。" if missing else "按 AE00 合同修改该字段。")})

    def required(self, obj: dict[str, Any], keys: set[str], path: str = "") -> None:
        for key in sorted(keys - obj.keys()):
            self.add(f"{path}.{key}" if path else key, "缺少必填字段。", "employee_package_required", missing=True)

    def obj(self, value: Any, path: str, required: set[str] = frozenset(), allowed: set[str] | None = None) -> bool:
        if type(value) is not dict:
            self.add(path, "必须为对象。")
            return False
        self.required(value, required, path)
        if allowed is not None:
            for key in sorted(value.keys() - allowed):
                self.add(f"{path}.{key}" if path else key, "存在合同未声明字段。", "employee_package_field_unknown")
        return True

    def string(self, value: Any, path: str, *, empty: bool = False, limit: int = 16384, pattern: re.Pattern[str] | None = None) -> None:
        if type(value) is not str or len(value) > limit or (not empty and not value.strip()) or (pattern is not None and not pattern.fullmatch(value)):
            self.add(path, "文本为空、过长或格式无效。")

    def enum(self, value: Any, path: str, options: set[str]) -> None:
        if type(value) is not str or value not in options:
            self.add(path, "枚举值不在合同范围内。")

    def boolean(self, value: Any, path: str) -> None:
        if type(value) is not bool:
            self.add(path, "必须为布尔值。")

    def integer(self, value: Any, path: str, low: int, high: int) -> None:
        if type(value) is not int or not low <= value <= high:
            self.add(path, f"必须为 {low} 到 {high} 的整数；布尔值不是整数。")

    def array(self, value: Any, path: str, *, minimum: int = 0, maximum: int = 500) -> bool:
        if type(value) is not list or not minimum <= len(value) <= maximum:
            self.add(path, f"必须为长度 {minimum} 到 {maximum} 的数组。")
            return False
        return True

    def strings(self, value: Any, path: str, *, minimum: int = 0, options: set[str] | None = None) -> None:
        if not self.array(value, path, minimum=minimum):
            return
        seen: set[str] = set()
        for index, item in enumerate(value):
            here = f"{path}[{index}]"
            self.string(item, here)
            if options is not None:
                self.enum(item, here, options)
            if type(item) is str:
                if item in seen:
                    self.add(here, "数组不能包含重复项。")
                seen.add(item)

    def timestamp(self, value: Any, path: str) -> None:
        self.string(value, path, limit=64)
        try:
            if type(value) is not str or "T" not in value:
                raise ValueError
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            self.add(path, "必须为完整 ISO 8601 时间。")

    def schema(self, value: Any, path: str) -> None:
        if not self.obj(value, path):
            return
        anchors: set[str] = set()
        references: list[tuple[str, str]] = []
        visited: set[int] = set()
        targets: list[dict[str, Any] | bool] = []

        def walk(node: Any, here: str) -> None:
            if type(node) is dict:
                if id(node) in visited:
                    return
                visited.add(id(node))
                if "$schema" in node and (type(node["$schema"]) is not str or node["$schema"] not in {_SCHEMA_URI, _SCHEMA_URI + "#"}):
                    self.add(here + ".$schema", "只支持 JSON Schema 2020-12。", "employee_package_schema_invalid")
                if here != path and "$id" in node:
                    self.add(here + ".$id", "首期不支持会改变本地引用作用域的嵌套 $id。", "employee_package_schema_scope_unsupported")
                for key in ("$anchor", "$dynamicAnchor"):
                    if type(node.get(key)) is str:
                        anchors.add(node[key])
                for key in ("$ref", "$dynamicRef", "$recursiveRef"):
                    if key in node:
                        ref = node[key]
                        if type(ref) is not str or not ref.startswith("#"):
                            self.add(here + "." + key, "Schema 只允许本地引用，不解析远端。", "employee_package_schema_remote")
                        else:
                            references.append((ref, here + "." + key))
                for key, child in node.items():
                    if key in {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"} and type(child) is dict:
                        for name, subschema in child.items():
                            walk(subschema, here + "." + key + "." + name)
                    elif key in {"allOf", "anyOf", "oneOf", "prefixItems"} and type(child) is list:
                        for index, subschema in enumerate(child):
                            walk(subschema, f"{here}.{key}[{index}]")
                    elif key in {"additionalProperties", "unevaluatedProperties", "propertyNames", "items", "contains", "unevaluatedItems", "not", "if", "then", "else", "contentSchema"}:
                        walk(child, here + "." + key)

        walk(value, path)
        for ref, here in references:
            fragment = unquote(ref[1:])
            target: Any = value
            try:
                if fragment.startswith("/"):
                    for part in fragment[1:].split("/"):
                        if re.search(r"~(?![01])", part):
                            raise ValueError
                        part = part.replace("~1", "/").replace("~0", "~")
                        target = target[int(part)] if type(target) is list and re.fullmatch(r"0|[1-9][0-9]*", part) else target[part]
                    if type(target) not in (dict, bool):
                        raise ValueError
                    targets.append(target)
                    # A local pointer can target a schema held in an annotation;
                    # its active references must receive the same safety check.
                    walk(target, here + ".target")
                elif fragment and fragment not in anchors:
                    raise ValueError
            except (KeyError, IndexError, TypeError, ValueError):
                self.add(here, "本地 Schema 引用不存在或目标不是 Schema。", "employee_package_schema_invalid")
        try:
            from jsonschema import Draft202012Validator
            from jsonschema.exceptions import SchemaError
        except ImportError:
            self.add(path, "现有环境缺少 jsonschema；未验证 Schema。", "employee_package_schema_unavailable")
            return
        try:
            # check_schema only checks against the bundled metaschema. It does
            # not validate an instance or retrieve any declared $ref target.
            Draft202012Validator.check_schema(value)
            checked = {id(value)}
            for target in targets:
                if id(target) not in checked:
                    checked.add(id(target))
                    Draft202012Validator.check_schema(target)
        except (SchemaError, TypeError, ValueError):
            self.add(path, "JSON Schema 2020-12 结构无效。", "employee_package_schema_invalid")


def validate_package_manifest(manifest: Any, *, available_capabilities: dict[str, set[str]] | None = None, require_complete: bool = True) -> list[dict[str, str]]:
    """Return contract findings; incomplete drafts get real missing-field gaps.

    Without an inventory, non-empty capabilities are explicitly unverified.
    This is not a release gate: tests and approval require separate evidence.
    """
    v = _Validation(require_complete)
    if type(require_complete) is not bool:
        v.add("require_complete", "必须为布尔值。")
        return v.problems
    try:
        canonical_json(manifest)
    except AIEmployeeContractError as exc:
        v.add(exc.path, exc.message, exc.code)
        return v.problems
    if not v.obj(manifest, "", _REQUIRED, _REQUIRED | _OPTIONAL):
        return v.problems
    if "schema_version" in manifest and (type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1):
        v.add("schema_version", "只支持岗位包 schema_version=1。", "employee_package_schema_version")
    for key in ("package_id", "version", "name", "responsibility", "package_org_id", "created_by", "published_by", "retired_by", "retired_reason", "draft_revision"):
        if key in manifest:
            v.string(manifest[key], key, pattern=_PACKAGE_ID if key == "package_id" else None, limit=16384 if key in {"responsibility", "retired_reason"} else 256)
    if "package_fingerprint" in manifest:
        v.string(manifest["package_fingerprint"], "package_fingerprint", pattern=_SHA256, limit=64)
    if "status" in manifest:
        v.enum(manifest["status"], "status", {"draft", "testing", "published", "retired"})
    if "enterprise_id" in manifest and manifest["enterprise_id"] != "":
        v.add("enterprise_id", "首期标准包 enterprise_id 必须为空。", "employee_package_private_content")
    for prefix in ("created", "published", "retired"):
        if prefix != "created" and manifest.get("status") == {"published": "published", "retired": "retired"}.get(prefix):
            keys = {prefix + "_at", prefix + "_by"} | ({"retired_reason"} if prefix == "retired" else set())
            v.required(manifest, keys)
        if prefix + "_at" in manifest:
            v.timestamp(manifest[prefix + "_at"], prefix + "_at")
    for key in ("scope_in", "scope_out", "prompt_refs"):
        if key in manifest:
            v.strings(manifest[key], key, minimum=1 if key == "scope_in" else 0)
    if manifest.get("scope_out") == []:
        v.add("scope_out", "scope_out 为空，需要补充不适用范围说明。", "employee_package_scope_out_empty", missing=True)
    slots = manifest.get("human_slots")
    if "human_slots" in manifest and v.array(slots, "human_slots", minimum=1, maximum=7):
        seen: set[str] = set()
        reviewer = False
        for index, slot in enumerate(slots):
            here = f"human_slots[{index}]"
            keys = {"slot", "required", "decides"}
            if not v.obj(slot, here, keys, keys | {"channel_options"}):
                continue
            if "slot" in slot:
                v.enum(slot["slot"], here + ".slot", {"submitter", "rule_confirmer", "reviewer", "approver", "escalation", "backup", "delivery_authorizer"})
                if type(slot["slot"]) is str:
                    if slot["slot"] in seen:
                        v.add(here + ".slot", "人工槽位不能重复。")
                    seen.add(slot["slot"])
            if "required" in slot:
                v.boolean(slot["required"], here + ".required")
            reviewer |= slot.get("slot") == "reviewer" and slot.get("required") is True
            if "decides" in slot:
                v.strings(slot["decides"], here + ".decides", minimum=1, options={"release", "reject", "confirm_fact", "authorize_send"})
            if "channel_options" in slot:
                v.strings(slot["channel_options"], here + ".channel_options", options={"console", "feishu", "dingtalk", "wecom", "email"})
        if not reviewer:
            v.add("human_slots", "必须声明 required=true 的 reviewer 槽位。", "employee_package_reviewer_required")
    for key in ("input_schema", "output_schema"):
        if key in manifest:
            v.schema(manifest[key], key)
    contract = manifest.get("artifact_contract")
    if "artifact_contract" in manifest and v.obj(contract, "artifact_contract", {"deliverables", "acceptance_criteria"}, {"deliverables", "acceptance_criteria"}):
        if "deliverables" in contract and v.array(contract["deliverables"], "artifact_contract.deliverables", minimum=1):
            for index, delivery in enumerate(contract["deliverables"]):
                here = f"artifact_contract.deliverables[{index}]"
                if v.obj(delivery, here, {"name", "format", "required"}, {"name", "format", "required"}):
                    for key in ("name", "format"):
                        if key in delivery:
                            v.string(delivery[key], here + "." + key)
                    if "required" in delivery:
                        v.boolean(delivery["required"], here + ".required")
        if "acceptance_criteria" in contract:
            v.strings(contract["acceptance_criteria"], "artifact_contract.acceptance_criteria", minimum=1)
    record_groups = {
        "checklist": ({"id", "title", "checker", "severity"}, set()),
        "skills": ({"skill_name", "version", "tree_fingerprint", "source"}, set()),
        "template_refs": ({"path", "version", "protected_regions"}, set()),
        "capability_requirements": ({"tool", "actions", "params_schema", "effect_kind"}, set()),
    }
    inventory_valid = available_capabilities is None or type(available_capabilities) is dict and len(available_capabilities) <= 500 and all(type(k) is str and bool(_IDENTIFIER.fullmatch(k)) and type(actions) is set and len(actions) <= 500 and all(type(a) is str and bool(_IDENTIFIER.fullmatch(a)) for a in actions) for k, actions in available_capabilities.items())
    if not inventory_valid:
        v.add("available_capabilities", "能力清单必须为工具名到动作字符串集合的映射。")
    for group, (required, optional) in record_groups.items():
        if group not in manifest or not v.array(manifest[group], group):
            continue
        seen: set[str] = set()
        for index, item in enumerate(manifest[group]):
            here = f"{group}[{index}]"
            if not v.obj(item, here, required, required | optional):
                continue
            unique = {"checklist": "id", "skills": "skill_name", "template_refs": "path", "capability_requirements": "tool"}[group]
            for key in required & item.keys() - {"actions", "params_schema", "protected_regions"}:
                v.string(item[key], here + "." + key, pattern=_SHA256 if key == "tree_fingerprint" else None)
            if type(item.get(unique)) is str:
                if item[unique] in seen:
                    v.add(here + "." + unique, "标识符不能重复。")
                seen.add(item[unique])
            if group == "checklist":
                if "checker" in item:
                    v.enum(item["checker"], here + ".checker", {"deterministic", "human_label"})
                if "severity" in item:
                    v.enum(item["severity"], here + ".severity", {"info", "low", "medium", "high", "warning", "error", "blocker"})
            elif group == "skills":
                if "skill_name" in item:
                    v.string(item["skill_name"], here + ".skill_name", pattern=_SKILL_ID)
                if "source" in item:
                    v.enum(item["source"], here + ".source", {"library", "package"})
            elif group == "template_refs":
                if "protected_regions" in item:
                    v.strings(item["protected_regions"], here + ".protected_regions")
            elif group == "capability_requirements":
                if "actions" in item:
                    v.strings(item["actions"], here + ".actions", minimum=1)
                if "params_schema" in item:
                    v.schema(item["params_schema"], here + ".params_schema")
                if "effect_kind" in item:
                    v.enum(item["effect_kind"], here + ".effect_kind", {"read", "write", "external_send", "paid"})
                if available_capabilities is None:
                    v.add(here, "未提供宿主能力清单，工具与动作尚未核实。", "employee_package_capability_unverified", missing=True)
                elif inventory_valid and type(item.get("tool")) is str:
                    actions = available_capabilities.get(item["tool"])
                    if actions is None:
                        v.add(here + ".tool", "宿主没有该工具。", "employee_package_capability_unknown")
                    elif type(item.get("actions")) is list and any(type(a) is str and a not in actions for a in item["actions"]):
                        v.add(here + ".actions", "宿主工具没有请求的动作。", "employee_package_capability_unknown")
    profile = manifest.get("model_profile")
    flags = {"generation", "classification", "tool_calling", "structured_output", "vision"}
    if "model_profile" in manifest and v.obj(profile, "model_profile", flags | {"verified_models"}, flags | {"verified_models"}):
        for key in flags & profile.keys():
            v.boolean(profile[key], "model_profile." + key)
        if "verified_models" in profile:
            v.strings(profile["verified_models"], "model_profile.verified_models")
            if profile["verified_models"] == []:
                # A package cannot create its own model approval. Requiring a
                # declared model here made first testing circular: adding it
                # changed the fingerprint and invalidated that very test.
                # Admission still requires independent six-match trial facts.
                v.problems.append({"code": "employee_package_model_unverified", "severity": "warning",
                    "path": "model_profile.verified_models", "message": "模型验证以服务端本次测试记录为准。",
                    "hint": "清单中的模型名不是准入证明；必须核对实际模型、参数和工具组合。"})
    knowledge = manifest.get("knowledge_protocol")
    knowledge_keys = {"required_kinds", "citation_rule", "update_rule", "license_note"}
    if "knowledge_protocol" in manifest and v.obj(knowledge, "knowledge_protocol", knowledge_keys, knowledge_keys):
        for key in knowledge_keys - {"required_kinds"}:
            if key in knowledge:
                v.string(knowledge[key], "knowledge_protocol." + key)
        if "required_kinds" in knowledge:
            v.strings(knowledge["required_kinds"], "knowledge_protocol.required_kinds", options={"standard", "enterprise_shared", "instance_private", "order_input"})
    loop = manifest.get("loop_policy")
    if "loop_policy" in manifest and v.obj(loop, "loop_policy", set(LOOP_LIMITS) - {"max_sub_agents"}, set(LOOP_LIMITS) | {"max_cost_cny"}):
        for key, (low, high) in LOOP_LIMITS.items():
            if key in loop:
                v.integer(loop[key], "loop_policy." + key, low, high)
        if "max_cost_cny" in loop:
            v.string(loop["max_cost_cny"], "loop_policy.max_cost_cny", pattern=_DECIMAL)
    evaluation = manifest.get("evaluation_profile")
    evaluation_keys = {"taskset_id", "taskset_fingerprint", "min_samples", "aggregation", "hard_fail_veto", "non_inferior_rule", "absolute_threshold", "budget"}
    if "evaluation_profile" in manifest and v.obj(evaluation, "evaluation_profile", evaluation_keys, evaluation_keys):
        if "taskset_id" in evaluation:
            v.string(evaluation["taskset_id"], "evaluation_profile.taskset_id", pattern=_IDENTIFIER)
        if "taskset_fingerprint" in evaluation:
            v.string(evaluation["taskset_fingerprint"], "evaluation_profile.taskset_fingerprint", pattern=_SHA256)
        if "min_samples" in evaluation:
            v.integer(evaluation["min_samples"], "evaluation_profile.min_samples", 1, 10000)
        if "aggregation" in evaluation:
            v.enum(evaluation["aggregation"], "evaluation_profile.aggregation", {"median"})
        if "hard_fail_veto" in evaluation and evaluation["hard_fail_veto"] is not True:
            v.add("evaluation_profile.hard_fail_veto", "hard_fail_veto 必须恒为 true。")
        for key in ("non_inferior_rule", "absolute_threshold", "budget"):
            if key in evaluation and v.obj(evaluation[key], "evaluation_profile." + key) and not evaluation[key]:
                v.add("evaluation_profile." + key, "评测规则和预算不能为空。", "employee_package_required", missing=True)
        threshold = evaluation.get("absolute_threshold")
        if type(threshold) is dict:
            for key, value in threshold.items():
                here = "evaluation_profile.absolute_threshold." + key
                if key == "hard_fail_count":
                    v.integer(value, here, 0, 0)
                elif type(value) in (int, float):
                    if not 0 <= value <= 1:
                        v.add(here, "比例阈值必须在 0 到 1 之间。")
                elif type(value) is not bool:
                    v.add(here, "阈值必须为有限比例或布尔条件。")
        budget = evaluation.get("budget")
        if type(budget) is dict:
            for key, value in budget.items():
                here = "evaluation_profile.budget." + key
                if key.startswith("require_"):
                    v.boolean(value, here)
                elif key in {"max_cost_cny", "max_cost"}:
                    v.string(value, here, pattern=_DECIMAL)
                elif key.startswith("max_"):
                    v.integer(value, here, 0, 10000000)
                elif key in {"currency", "budget_ref", "pricing_basis"}:
                    v.string(value, here)
                else:
                    v.add(here, "预算包含未声明字段。", "employee_package_field_unknown")
        rule = evaluation.get("non_inferior_rule")
        if type(rule) is dict:
            for key, value in rule.items():
                here = "evaluation_profile.non_inferior_rule." + key
                if key.startswith("require_"):
                    v.boolean(value, here)
                elif type(value) is not str or not value.strip():
                    v.add(here, "评测基线规则必须为非空文本或显式布尔条件。")
    provenance = manifest.get("provenance")
    provenance_keys = {"source_repo", "source_revision", "files", "license", "notices", "model_or_data_licenses"}
    if "provenance" in manifest and v.obj(provenance, "provenance", provenance_keys, provenance_keys):
        for key in ("source_repo", "license"):
            if key in provenance:
                v.string(provenance[key], "provenance." + key)
        if "source_revision" in provenance:
            v.string(provenance["source_revision"], "provenance.source_revision", empty=True)
            if provenance["source_revision"] == "":
                v.add("provenance.source_revision", "来源修订尚未锚定。", "employee_package_required", missing=True)
        if "notices" in provenance:
            v.strings(provenance["notices"], "provenance.notices")
        if "model_or_data_licenses" in provenance:
            v.strings(provenance["model_or_data_licenses"], "provenance.model_or_data_licenses")
        if "files" in provenance and v.array(provenance["files"], "provenance.files"):
            for index, item in enumerate(provenance["files"]):
                here = f"provenance.files[{index}]"
                if type(item) is dict:
                    v.obj(item, here, {"path", "kind"}, {"path", "kind", "encoding"})
                elif type(item) is not str:
                    v.add(here, "文件声明必须为路径文本或类型编码对象。")
    approval = manifest.get("release_approval")
    approval_required = {"approval_id", "status", "requested_by", "requested_at", "candidate_fingerprint"}
    approval_optional = {"note", "decided_by", "decided_at", "decision_note"}
    if "release_approval" in manifest and v.obj(approval, "release_approval", approval_required, approval_required | approval_optional):
        for key in (approval_required | approval_optional) & approval.keys() - {"status", "requested_at", "decided_at"}:
            v.string(approval[key], "release_approval." + key, empty=key in {"note", "decision_note"}, pattern=_SHA256 if key == "candidate_fingerprint" else None)
        if "status" in approval:
            v.enum(approval["status"], "release_approval.status", {"pending", "approved", "rejected"})
        if type(approval.get("status")) is str and approval["status"] in {"approved", "rejected"}:
            v.required(approval, {"decided_by", "decided_at"}, "release_approval")
        for key in ("requested_at", "decided_at"):
            if key in approval:
                v.timestamp(approval[key], "release_approval." + key)
        if "candidate_fingerprint" in approval and "package_fingerprint" in manifest and approval["candidate_fingerprint"] != manifest["package_fingerprint"]:
            v.add("release_approval.candidate_fingerprint", "审批必须绑定当前候选指纹。", "employee_package_approval_stale")
    if "legacy_source" in manifest:
        v.obj(manifest["legacy_source"], "legacy_source")
    for key in ("latest_test", "evaluation_results", "evaluation_records", "evaluations", "test_results"):
        if key in manifest and type(manifest[key]) not in (dict, list):
            v.add(key, "评测记录必须为对象或数组；记录本身不推导通过。")
    try:
        _declarations(manifest)
    except AIEmployeeContractError as exc:
        v.add(exc.path, exc.message, exc.code)
    for code, path in _privacy_findings(manifest):
        v.add(path, "标准包不能含私有值或凭据形态；未回显内容。", code, hint="移除私有内容；凭据只在宿主批准的引用中保存。")
    return v.problems


def _normalized_text(raw: bytes, encoding: str, path: str) -> tuple[bytes, str]:
    try:
        codec = codecs.lookup(encoding).name
        text = raw.decode(codec, errors="strict")
        if not raw:
            return raw, text
        # Preserve the byte order and original BOM for generic UTF-16/32.
        prefix = b""
        if codec == "utf-8-sig":
            prefix = codecs.BOM_UTF8 if raw.startswith(codecs.BOM_UTF8) else b""
            codec = "utf-8"
        if codec in {"utf-16", "utf-32"}:
            variants = ((codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be")) if codec == "utf-16" else ((codecs.BOM_UTF32_LE, "utf-32-le"), (codecs.BOM_UTF32_BE, "utf-32-be"))
            prefix, codec = next((mark, name) for mark, name in variants if raw.startswith(mark))
        if prefix + text.encode(codec) != raw:
            raise ValueError("encoding does not round-trip")
        return prefix + text.replace("\r\n", "\n").encode(codec), text
    except (LookupError, UnicodeError, TypeError, ValueError, StopIteration) as exc:
        raise AIEmployeeContractError("employee_package_file_encoding", "文本字节与声明编码不匹配或无法稳定回编码。", path) from exc


def _scan_file_text(text: str, path: str) -> None:
    """Scan only supplied text, never files or external dependencies.

    JSON values also receive the private-field scanner. Other text receives a
    narrow assignment-pattern scan. This cannot certify arbitrary prose,
    archive members, images or encrypted/binary content as non-private.
    """
    if _secret_shaped("", text):
        raise AIEmployeeContractError("employee_package_secret", "文件包含凭据形态；未回显内容。", path)
    if len(text) > MAX_MANIFEST_BYTES:
        if text.lstrip().startswith(("{", "[", '"')):
            raise AIEmployeeContractError("employee_package_limit", "结构化文本超过 2 MiB，未尝试解析。", path)
        if _PRIVATE_TEXT_KEY.search(text):
            raise AIEmployeeContractError("employee_package_private_content", "文本包含私有字段赋值；未回显内容。", path)
        return
    try:
        parsed = json.loads(text)
    except RecursionError as exc:
        raise AIEmployeeContractError("employee_package_limit", "结构化文本嵌套超过安全范围。", path) from exc
    except (ValueError, UnicodeError):
        if _PRIVATE_TEXT_KEY.search(text):
            raise AIEmployeeContractError("employee_package_private_content", "文本包含私有字段赋值；未回显内容。", path)
        return
    try:
        _json_shape(parsed)
    except AIEmployeeContractError as exc:
        raise AIEmployeeContractError(exc.code, exc.message, path) from exc
    is_schema = type(parsed) is dict and parsed.get("$schema") in (_SCHEMA_URI, _SCHEMA_URI + "#")
    findings = _privacy_findings(parsed, schema=is_schema)
    if findings:
        raise AIEmployeeContractError(findings[0][0], "文件包含私有字段或凭据形态；未回显内容。", path)


def package_content_projection(manifest: dict[str, Any], files: dict[str, bytes]) -> dict[str, Any]:
    """Build AE00 §5.1 content projection from a manifest and supplied bytes.

    Missing operational evidence is allowed for computing a draft candidate;
    structural errors, private values and unbounded inputs are always refused.
    The caller must establish that bytes came from regular non-linked files.
    """
    problems = validate_package_manifest(manifest, require_complete=False)
    errors = [problem for problem in problems if problem["severity"] == "error"]
    if errors:
        first = errors[0]
        raise AIEmployeeContractError(first["code"], first["message"], first["path"], {"problems": errors})
    if type(files) is not dict or len(files) > MAX_FILES:
        raise AIEmployeeContractError("employee_package_limit", "files 必须为最多 500 个文件的字节映射。")
    declared = _declarations(manifest)
    supplied: dict[str, bytes] = {}
    aliases: set[str] = set()
    total = 0
    for key, raw in files.items():
        path = _portable_path(key)
        if type(raw) is not bytes:
            raise AIEmployeeContractError("employee_package_file_kind", "文件输入必须为原始 bytes，不能为链接或文件对象。", path)
        if path.casefold() in aliases:
            raise AIEmployeeContractError("employee_package_file_conflict", "提供的文件路径在 Windows 上冲突。")
        aliases.add(path.casefold())
        total += len(raw)
        if total > MAX_FILE_BYTES:
            raise AIEmployeeContractError("employee_package_limit", "包内文件总大小不能超过 20 MiB。")
        supplied[path] = raw
    if set(supplied) - set(declared):
        raise AIEmployeeContractError("employee_package_file_undeclared", "不能纳入清单未声明的文件。")
    if set(declared) - set(supplied):
        raise AIEmployeeContractError("employee_package_file_missing", "清单所指文件缺失。")
    projection_files: list[dict[str, str]] = []
    for path in sorted(declared):
        raw = supplied[path]
        meta = declared[path]
        kind = meta.get("kind", "binary")
        if kind == "text":
            if raw.startswith(_BINARY_MAGIC):
                raise AIEmployeeContractError("employee_package_file_kind", "识别到二进制文件头，不能作文本换行折叠。", path)
            raw, text = _normalized_text(raw, meta["encoding"], path)
            _scan_file_text(text, path)
        else:
            # UTF-8 binary declarations remain byte-exact, but cannot bypass
            # the pure literal-secret scanner by changing their declaration.
            try:
                text = raw.decode("utf-8")
            except UnicodeError:
                text = ""
            if text:
                _scan_file_text(text, path)
        projection_files.append({"path": path, "kind": kind, "sha256": hashlib.sha256(raw).hexdigest()})
    body = {key: value for key, value in manifest.items() if key not in _OPERATION_FIELDS and not key.startswith(("created_", "published_", "retired_"))}
    # Return a detached JSON value: a caller cannot mutate the supplied manifest
    # by editing the projection after a validation call.
    body = json.loads(canonical_json(body))
    trees = []
    for index, skill in enumerate(manifest.get("skills", [])):
        if "skill_name" not in skill or "tree_fingerprint" not in skill:
            raise AIEmployeeContractError("employee_package_required", "技能树指纹必须来自真实完整声明。", f"skills[{index}]")
        trees.append({"skill_name": skill["skill_name"], "tree_fingerprint": skill["tree_fingerprint"]})
    return {"manifest": body, "skill_trees": sorted(trees, key=lambda item: item["skill_name"]), "files": projection_files}


def package_fingerprint(manifest: dict[str, Any], files: dict[str, bytes]) -> str:
    return sha256_json(package_content_projection(manifest, files))
