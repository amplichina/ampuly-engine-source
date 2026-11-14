"""AE01 immutable version policy and trusted publication-evidence checks.

There is deliberately no filesystem writer here. A catalogue exposes only the
versions.jsonl visibility index; an unindexed staging/immutable directory is not
published. The caller supplies a safe, bounded reader. Publication anchors and
the expected evaluation context must originate in authenticated server state,
not from an RPC payload or from the same untrusted JSON being checked.

The separate writer must use: invisible bounded staging -> verify exact bytes
and skill trees -> durable atomic directory promotion -> CAS-append visibility
index last. Its request intent retains staging/target/index hashes. A receipt
loss is outcome_unknown and is reconciled only using that original request;
it never deletes, overwrites, or republishes an uncertain immutable directory.
This module cannot establish that a model was run from a client's status flag.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from . import ai_employee_contract as _contract

AIEmployeeContractError = _contract.AIEmployeeContractError
canonical_json = _contract.canonical_json
package_content_projection = _contract.package_content_projection
package_fingerprint = _contract.package_fingerprint
validate_package_manifest = _contract.validate_package_manifest

MAX_LEDGER_BYTES = 2 * 1024 * 1024
MAX_INDEX_RECORDS = 2000
MAX_EVALUATION_RECORDS = 500
MAX_REPEATS = 10000
MAX_DIFF_CHANGES = 1000
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_PACKAGE = re.compile(r"[a-z0-9][a-z0-9_-]{2,63}\Z")
_ZERO = "0" * 64
_EVALUATION_FIELDS = {
    "schema_version", "record_kind", "record_id", "trial_id", "package_id",
    "package_fingerprint", "taskset_fingerprint", "model_config",
    "capability_impls", "checker_versions", "repeats", "observed_at",
    "previous_sha256", "record_sha256",
}
_REPEAT_FIELDS = {
    "repeat_id", "completed", "hard_fail_count", "non_inferior_met",
    "absolute_threshold_met",
}
_INDEX_FIELDS = {"version", "package_fingerprint", "status", "at", "actor", "previous_sha256", "record_sha256"}


def _error(code: str, message: str, path: str = "") -> None:
    raise AIEmployeeContractError(code, message, path)


def _text(value: Any, path: str, limit: int = 256, *, empty: bool = False, multiline: bool = False) -> str:
    if type(value) is not str or len(value) > limit or (not empty and not value.strip()) or any(ord(c) < 32 and (not multiline or c not in "\r\n\t") for c in value):
        _error("publication_record_invalid", "事实记录文本为空、过长或格式无效。", path)
    return value


def _sha(value: Any, path: str) -> str:
    if type(value) is not str or not _SHA.fullmatch(value):
        _error("publication_record_invalid", "事实依据必须是规范 SHA-256。", path)
    return value


def _time(value: Any, path: str) -> datetime:
    _text(value, path, 64)
    try:
        if "T" not in value:
            raise ValueError
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _error("publication_record_invalid", "事实记录必须使用完整 ISO 8601 时间。", path)


def _package_id(value: Any) -> str:
    if type(value) is not str or not _PACKAGE.fullmatch(value):
        _error("publication_record_invalid", "岗位包编号无效。", "package_id")
    return value


def _object(value: Any, path: str, required: set[str], optional: set[str] = frozenset()) -> dict[str, Any]:
    if type(value) is not dict or required - value.keys() or value.keys() - (required | optional):
        _error("publication_record_invalid", "事实记录字段不符合固定合同。", path)
    return value


def _unique_mapping(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _error("publication_record_invalid", "事实 JSON 不能包含重复键。")
        result[key] = value
    return result


def _decode(raw: bytes, path: str) -> Any:
    if type(raw) is not bytes or len(raw) > MAX_LEDGER_BYTES:
        _error("publication_record_invalid", "事实记录字节超过有界读取范围。", path)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_mapping)
        canonical_json(value)
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise AIEmployeeContractError("publication_record_invalid", "事实 JSON 损坏或结构越界；未回显内容。", path) from exc
    return value


def _lines(raw: bytes, maximum: int, path: str) -> list[Any]:
    if type(raw) is not bytes or len(raw) > MAX_LEDGER_BYTES:
        _error("publication_record_invalid", "事实台账超过 2 MiB。", path)
    lines = raw.splitlines()
    if len(lines) > maximum or any(not line.strip() for line in lines):
        _error("publication_record_invalid", "事实台账记录数量越界或含空记录。", path)
    return [_decode(line, f"{path}[{index}]") for index, line in enumerate(lines)]


def _context(value: Any) -> dict[str, Any]:
    obj = _object(value, "expected_context", {"model_config", "capability_impls", "checker_versions"}, {"skill_tree_fingerprints"})
    # Snapshot caller-owned mappings; subsequent mutation cannot change a check.
    obj = json.loads(canonical_json(obj))
    if type(obj["model_config"]) is not dict or not obj["model_config"] or type(obj["capability_impls"]) is not dict:
        _error("publication_context_invalid", "模型和工具必须来自服务端冻结组合。")
    _checker_versions(obj["checker_versions"], "expected_context.checker_versions")
    trees = obj.get("skill_tree_fingerprints", {})
    if type(trees) is not dict or len(trees) > 500:
        _error("publication_context_invalid", "技能树证明映射无效。")
    for name, fingerprint in trees.items():
        _text(name, "expected_context.skill_tree_fingerprints", 64)
        _sha(fingerprint, "expected_context.skill_tree_fingerprints")
    return obj


def _checker_versions(value: Any, path: str) -> None:
    if type(value) is not dict or not value or len(value) > 500:
        _error("publication_record_invalid", "必须提供非空、固定的检查器版本映射。", path)
    for key, version in value.items():
        _text(key, path, 128)
        _text(version, path + ".version", 128)


@dataclass(frozen=True)
class PublicationAnchors:
    """Independent evidence anchors supplied by the trusted server adapter.

    Neither this object nor expected_context is accepted from a client's JSON.
    Reviewer identities must have been authenticated as authorized humans;
    trial identities must point to independently authorized trial records.
    Constructing an anchor does not run or authorize an evaluation.
    """
    evaluation_tail_sha256: str | None = None
    approval_sha256: str | None = None
    authorized_trial_ids: frozenset[str] = frozenset()
    authorized_reviewer_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        for digest in (self.evaluation_tail_sha256, self.approval_sha256):
            if digest is not None:
                _sha(digest, "publication_anchor")
        for identities in (self.authorized_trial_ids, self.authorized_reviewer_ids):
            if type(identities) is not frozenset or len(identities) > 2000:
                _error("publication_evidence_untrusted", "授权身份必须由服务端提供有限不可变集合。")
            for identity in identities:
                _text(identity, "publication_anchor.identity", 128)


def parse_evaluation_records(raw: bytes, *, expected_tail_sha256: str) -> list[dict[str, Any]]:
    """Check strictly shaped, chained evaluator facts against an external tail.

    These are evidence records, not an evaluator API. No `passed`, `green`,
    `outcome` or client-supplied aggregate can stand in for repeat facts.
    """
    expected_tail_sha256 = _sha(expected_tail_sha256, "evaluation_tail_sha256")
    records = _lines(raw, MAX_EVALUATION_RECORDS, "evaluations")
    previous = _ZERO
    record_ids: set[str] = set()
    total_repeats = 0
    for index, record in enumerate(records):
        here = f"evaluations[{index}]"
        _object(record, here, _EVALUATION_FIELDS)
        if type(record["schema_version"]) is not int or record["schema_version"] != 1 or record["record_kind"] != "employee_package.evaluation":
            _error("publication_record_invalid", "不支持该评测事实合同版本。", here)
        _package_id(record["package_id"])
        for key in ("record_id", "trial_id"):
            _text(record[key], here + "." + key, 128)
        if record["record_id"] in record_ids:
            _error("publication_record_invalid", "评测事实编号不能重复。", here)
        record_ids.add(record["record_id"])
        for key in ("package_fingerprint", "taskset_fingerprint", "previous_sha256", "record_sha256"):
            _sha(record[key], here + "." + key)
        if record["previous_sha256"] != previous:
            _error("publication_evidence_tampered", "评测事实前后哈希不连续。", here)
        body = {key: value for key, value in record.items() if key != "record_sha256"}
        if hashlib.sha256(canonical_json(body)).hexdigest() != record["record_sha256"]:
            _error("publication_evidence_tampered", "评测事实内容与哈希不符。", here)
        _time(record["observed_at"], here + ".observed_at")
        if type(record["model_config"]) is not dict or not record["model_config"] or type(record["capability_impls"]) is not dict:
            _error("publication_record_invalid", "评测事实缺少模型或工具组合。", here)
        _checker_versions(record["checker_versions"], here + ".checker_versions")
        repeats = record["repeats"]
        if type(repeats) is not list or not repeats or len(repeats) > MAX_REPEATS:
            _error("publication_record_invalid", "评测事实必须包含有限逐轮结果。", here)
        total_repeats += len(repeats)
        if total_repeats > MAX_REPEATS:
            _error("publication_record_invalid", "逐轮评测结果总量越界。", here)
        repeat_ids: set[str] = set()
        for number, repeat in enumerate(repeats):
            there = f"{here}.repeats[{number}]"
            _object(repeat, there, _REPEAT_FIELDS)
            _text(repeat["repeat_id"], there + ".repeat_id", 128)
            if repeat["repeat_id"] in repeat_ids:
                _error("publication_record_invalid", "同一评测里的重复轮编号不能复用。", there)
            repeat_ids.add(repeat["repeat_id"])
            for key in ("completed", "non_inferior_met", "absolute_threshold_met"):
                if type(repeat[key]) is not bool:
                    _error("publication_record_invalid", "逐轮结果必须使用明确布尔条件。", there + "." + key)
            if type(repeat["hard_fail_count"]) is not int or not 0 <= repeat["hard_fail_count"] <= 1000000:
                _error("publication_record_invalid", "硬失败数量必须为非负整数。", there)
        previous = record["record_sha256"]
    if not records or previous != expected_tail_sha256:
        _error("publication_evidence_tampered", "评测事实尾哈希与独立服务端锚不符。")
    return records


def parse_approval_record(raw: bytes, *, expected_sha256: str) -> dict[str, Any]:
    """Check a human decision against the server writer's independent byte hash."""
    _sha(expected_sha256, "approval_sha256")
    if type(raw) is not bytes or hashlib.sha256(raw).hexdigest() != expected_sha256:
        _error("publication_evidence_tampered", "发布审批字节与服务端锚不符。")
    record = _decode(raw, "approval")
    _object(record, "approval", {"schema_version", "record_kind", "package_id", "approval"})
    if type(record["schema_version"]) is not int or record["schema_version"] != 1 or record["record_kind"] != "employee_package.release_approval":
        _error("publication_record_invalid", "不支持该发布审批事实合同版本。")
    _package_id(record["package_id"])
    approval = _object(record["approval"], "approval.approval", {"approval_id", "status", "requested_by", "requested_at", "candidate_fingerprint"}, {"note", "decided_by", "decided_at", "decision_note"})
    if type(approval["status"]) is not str or approval["status"] not in {"pending", "approved", "rejected"}:
        _error("publication_record_invalid", "发布审批状态无效。")
    for key in ("approval_id", "requested_by"):
        _text(approval[key], "approval." + key, 128)
    requested = _time(approval["requested_at"], "approval.requested_at")
    _sha(approval["candidate_fingerprint"], "approval.candidate_fingerprint")
    for key in ("note", "decision_note"):
        if key in approval:
            _text(approval[key], "approval." + key, 4096, empty=True, multiline=True)
    if approval["status"] in {"approved", "rejected"}:
        if not {"decided_by", "decided_at"} <= approval.keys():
            _error("publication_record_invalid", "人审决定缺少经认证作者或决定时间。")
        _text(approval["decided_by"], "approval.decided_by", 128)
        decided = _time(approval["decided_at"], "approval.decided_at")
        try:
            if decided < requested:
                raise ValueError
        except (TypeError, ValueError):
            _error("publication_record_invalid", "人审时间顺序或时区不一致。")
    elif "decided_by" in approval or "decided_at" in approval:
        _error("publication_record_invalid", "待审记录不能携带已决定作者或时间。")
    return record


def publication_readiness(
    manifest: dict[str, Any], files: dict[str, bytes], *,
    expected_fingerprint: str, expected_context: dict[str, Any] | None = None,
    anchors: PublicationAnchors | None = None, evaluations_raw: bytes | None = None,
    approval_raw: bytes | None = None, available_capabilities: dict[str, set[str]] | None = None,
) -> dict[str, Any]:
    """Derive six matches and human approval; perform no publication or adoption."""
    result: dict[str, Any] = {"ready": False, "test_passed": False, "approval_passed": False, "candidate_fingerprint": None, "effects_produced": False, "six_matches": {key: False for key in ("candidate_fingerprint", "taskset_fingerprint", "model_tool_combination", "checker_versions", "hard_fail_veto", "min_samples")}, "problems": []}

    def problem(code: str, message: str, path: str = "") -> None:
        result["problems"].append({"code": code, "severity": "error", "path": path, "message": message, "hint": "从可信服务端取得固定依据；不要用状态或声明代替证据。"})

    problems = validate_package_manifest(manifest, available_capabilities=available_capabilities, require_complete=True)
    result["problems"].extend(problems)
    if any(p["severity"] == "error" for p in problems):
        return result
    try:
        actual = package_fingerprint(manifest, files)
        _sha(expected_fingerprint, "expected_fingerprint")
    except AIEmployeeContractError as exc:
        problem(exc.code, exc.message, exc.path)
        return result
    result["candidate_fingerprint"] = actual
    if manifest["package_fingerprint"] != actual or expected_fingerprint != actual:
        problem("fingerprint_conflict", "当前清单和文件与预期候选指纹不符。")
    if manifest["status"] not in {"draft", "testing"}:
        problem("not_allowed_in_status", "只有当前草稿或测试候选可以申请新发布。")
    if type(anchors) is not PublicationAnchors:
        problem("publication_evidence_untrusted", "缺少独立服务端评测和人审锚。")
        return result
    context = None
    try:
        if expected_context is None:
            _error("publication_context_unavailable", "没有服务端冻结的模型、工具和检查器组合。")
        context = _context(expected_context)
    except AIEmployeeContractError as exc:
        problem(exc.code, exc.message, exc.path)
    records: list[dict[str, Any]] = []
    try:
        if evaluations_raw is None or anchors.evaluation_tail_sha256 is None:
            _error("publication_evaluation_unavailable", "缺少真实授权试单的固定评测事实。")
        records = parse_evaluation_records(evaluations_raw, expected_tail_sha256=anchors.evaluation_tail_sha256)
        if any(record["trial_id"] not in anchors.authorized_trial_ids for record in records):
            _error("publication_trial_unauthorized", "评测事实没有独立授权试单依据。")
    except AIEmployeeContractError as exc:
        records = []
        problem(exc.code, exc.message, exc.path)
    matches = result["six_matches"]
    candidates = [r for r in records if r["package_id"] == manifest["package_id"] and r["package_fingerprint"] == actual]
    matches["candidate_fingerprint"] = bool(candidates)
    taskset = [r for r in candidates if r["taskset_fingerprint"] == manifest["evaluation_profile"]["taskset_fingerprint"]]
    matches["taskset_fingerprint"] = bool(taskset)
    combination: list[dict[str, Any]] = []
    eligible: list[dict[str, Any]] = []
    if context is not None:
        combination = [r for r in taskset if canonical_json(r["model_config"]) == canonical_json(context["model_config"]) and canonical_json(r["capability_impls"]) == canonical_json(context["capability_impls"])]
        eligible = [r for r in combination if r["checker_versions"] == context["checker_versions"]]
    matches["model_tool_combination"] = bool(combination)
    matches["checker_versions"] = bool(eligible)
    completed: list[dict[str, Any]] = []
    seen_repeats: set[tuple[str, str]] = set()
    duplicate = False
    for record in eligible:
        for repeat in record["repeats"]:
            identity = (record["trial_id"], repeat["repeat_id"])
            if identity in seen_repeats:
                duplicate = True
            seen_repeats.add(identity)
            if repeat["completed"]:
                completed.append(repeat)
    if duplicate:
        problem("publication_repeat_duplicate", "同一授权试单的重复轮被重复计数。")
    matches["hard_fail_veto"] = bool(eligible) and manifest["evaluation_profile"]["hard_fail_veto"] is True and all(repeat["hard_fail_count"] == 0 for record in eligible for repeat in record["repeats"])
    matches["min_samples"] = not duplicate and len(completed) >= manifest["evaluation_profile"]["min_samples"]
    quality = bool(completed) and all(repeat["completed"] and repeat["non_inferior_met"] and repeat["absolute_threshold_met"] for record in eligible for repeat in record["repeats"])
    skills_ok = True
    for skill in manifest.get("skills", []):
        trees = context.get("skill_tree_fingerprints", {}) if context is not None else {}
        if trees.get(skill["skill_name"]) != skill["tree_fingerprint"]:
            skills_ok = False
            problem("skill_tree_evidence_unavailable", "技能树副本没有独立真实指纹证明。", "skills")
    result["test_passed"] = all(matches.values()) and quality and skills_ok
    if not result["test_passed"]:
        problem("publication_test_not_passed", "当前候选缺少六匹配及阈值通过事实。")
    try:
        if approval_raw is None or anchors.approval_sha256 is None:
            _error("publication_approval_unavailable", "缺少锚定当前候选的人类发布决定。")
        envelope = parse_approval_record(approval_raw, expected_sha256=anchors.approval_sha256)
        approval = envelope["approval"]
        result["approval_passed"] = envelope["package_id"] == manifest["package_id"] and approval["candidate_fingerprint"] == actual and approval["status"] == "approved" and approval.get("decided_by") in anchors.authorized_reviewer_ids
        if not result["approval_passed"]:
            problem("publication_approval_not_passed", "人审未通过、指纹过期或作者没有当前发布资格。")
    except AIEmployeeContractError as exc:
        problem(exc.code, exc.message, exc.path)
    result["ready"] = result["test_passed"] and result["approval_passed"] and not any(p["severity"] == "error" for p in result["problems"])
    return result


class VersionCatalog:
    """An append-only visibility index, detached from its supplied values."""
    def __init__(self, package_id: str, records: list[dict[str, Any]], *, expected_tail_sha256: str | None = None) -> None:
        self.package_id = _package_id(package_id)
        if type(records) is not list or len(records) > MAX_INDEX_RECORDS:
            _error("publication_record_invalid", "版本索引数量越界。")
        self.records = json.loads(canonical_json(records))
        self._versions: dict[str, dict[str, Any]] = {}
        self._fingerprints: dict[str, str] = {}
        self.tail_sha256 = _ZERO
        self.is_current_verified = False
        for position, record in enumerate(self.records):
            _object(record, f"versions[{position}]", _INDEX_FIELDS, {"reason"})
            _sha(record["previous_sha256"], "previous_sha256")
            _sha(record["record_sha256"], "record_sha256")
            body = {key: value for key, value in record.items() if key != "record_sha256"}
            if record["previous_sha256"] != self.tail_sha256 or hashlib.sha256(canonical_json(body)).hexdigest() != record["record_sha256"]:
                _error("publication_evidence_tampered", "版本可见索引的哈希链损坏。")
            version = _text(record["version"], "version")
            fingerprint = _sha(record["package_fingerprint"], "package_fingerprint")
            _text(record["actor"], "actor", 128)
            _time(record["at"], "at")
            if type(record["status"]) is not str or record["status"] not in {"published", "retired"}:
                _error("publication_record_invalid", "版本索引只能记录已发布或已退役。")
            previous = self._versions.get(version)
            if previous is not None and previous["package_fingerprint"] != fingerprint:
                _error("version_fingerprint_conflict", "同一版本不能对应不同内容指纹。")
            if fingerprint in self._fingerprints and self._fingerprints[fingerprint] != version:
                _error("version_fingerprint_conflict", "同一内容指纹不能被另一个版本名复用。")
            if record["status"] == "retired":
                if previous is None:
                    _error("publication_record_invalid", "未发布版本不能先退役。")
                _text(record.get("reason"), "reason", 4096, multiline=True)
            elif "reason" in record:
                _error("publication_record_invalid", "发布索引不能携带退役理由。")
            if previous is not None and previous["status"] == "retired" and record["status"] == "published":
                _error("package_retired", "已退役版本不能原地重新发布。")
            published_order = previous["published_order"] if previous is not None else position
            self._versions[version] = {**record, "published_order": published_order}
            self._fingerprints[fingerprint] = version
            self.tail_sha256 = record["record_sha256"]
        if expected_tail_sha256 is not None:
            if _sha(expected_tail_sha256, "expected_tail_sha256") != self.tail_sha256:
                _error("publication_evidence_tampered", "版本可见索引尾哈希与服务端锚不符。")
            # The adapter must supply this independently from authoritative
            # server state. A digest of these same records is not a fresh anchor.
            self.is_current_verified = True

    @classmethod
    def from_index(cls, package_id: str, raw: bytes | None, *, expected_tail_sha256: str | None = None) -> "VersionCatalog":
        return cls(package_id, [] if raw is None else _lines(raw, MAX_INDEX_RECORDS, "versions"), expected_tail_sha256=expected_tail_sha256)

    def list(self) -> list[dict[str, Any]]:
        return [{key: value for key, value in record.items() if key != "published_order"} for record in sorted(self._versions.values(), key=lambda row: row["published_order"], reverse=True)]

    def select(self, *, version: str | None = None, fingerprint: str | None = None, purpose: str = "detail") -> dict[str, Any]:
        if type(purpose) is not str or purpose not in {"detail", "new_adoption", "pinned"}:
            _error("request_invalid", "版本读取用途无效。")
        if purpose == "new_adoption" and not self.is_current_verified:
            _error("publication_index_unverified", "新采用必须核对独立可信的当前索引尾锚。")
        if purpose == "pinned" and fingerprint is None:
            _error("request_invalid", "在途依据必须按已冻结指纹读取。")
        if version is not None:
            _text(version, "version")
        if fingerprint is not None:
            _sha(fingerprint, "fingerprint")
            by_fingerprint = self._fingerprints.get(fingerprint)
            if by_fingerprint is None:
                _error("version_not_available", "指定指纹不在发布可见索引中。")
            if version is not None and version != by_fingerprint:
                _error("fingerprint_conflict", "版本名和指纹不指向同一依据。")
            version = by_fingerprint
        if version is None:
            published = [record for record in self._versions.values() if record["status"] == "published"]
            if not published:
                _error("version_not_available", "没有可见的最新发布版。")
            selected = max(published, key=lambda record: record["published_order"])
        else:
            selected = self._versions.get(version)
            if selected is None:
                _error("version_not_available", "指定版本未发布。")
        if selected["status"] == "retired" and purpose == "new_adoption":
            _error("package_retired", "退役版本不能成为新的采用依据。")
        return {key: value for key, value in selected.items() if key != "published_order"}

    def publication_plan(self, *, version: str, fingerprint: str, actor: str, at: str) -> dict[str, Any]:
        """Pure uniqueness decision; a readiness proof is still separately needed."""
        _text(version, "version")
        _sha(fingerprint, "fingerprint")
        _text(actor, "actor", 128)
        _time(at, "at")
        existing = self._versions.get(version)
        if existing is not None:
            if existing["package_fingerprint"] != fingerprint:
                _error("version_fingerprint_conflict", "同一版本已冻结为另一个指纹。")
            if existing["status"] == "retired":
                _error("package_retired", "退役版本不能原地重新发布。")
            return {"effects_produced": False, "already_published": True, "append_record": None, "immutable_directory": f"employee_packages/{self.package_id}/.versions/{fingerprint}"}
        if not self.is_current_verified:
            _error("publication_index_unverified", "发布追加计划必须核对独立可信的当前索引尾锚。")
        if fingerprint in self._fingerprints:
            _error("version_fingerprint_conflict", "这个指纹已经属于另一个版本。")
        record = {"version": version, "package_fingerprint": fingerprint, "status": "published", "at": at, "actor": actor}
        return {"effects_produced": False, "already_published": False, "append_record": chained_index_record(record, previous_sha256=self.tail_sha256), "immutable_directory": f"employee_packages/{self.package_id}/.versions/{fingerprint}", "write_backend_required": True}

    def retirement_plan(self, *, fingerprint: str, reason: str, actor: str, at: str) -> dict[str, Any]:
        _text(reason, "reason", 4096, multiline=True)
        _text(actor, "actor", 128)
        _time(at, "at")
        selected = self.select(fingerprint=fingerprint)
        if selected["status"] != "retired" and not self.is_current_verified:
            _error("publication_index_unverified", "退役追加计划必须核对独立可信的当前索引尾锚。")
        append = None if selected["status"] == "retired" else chained_index_record({"version": selected["version"], "package_fingerprint": fingerprint, "status": "retired", "at": at, "actor": actor, "reason": reason}, previous_sha256=self.tail_sha256)
        return {"effects_produced": False, "append_record": append, "already_retired": selected["status"] == "retired", "preserve_immutable_directory": True, "new_adoption_allowed": False, "existing_pins_usable": True, "existing_instance_gap": "package_retired", "write_backend_required": append is not None}


def chained_index_record(record: dict[str, Any], *, previous_sha256: str) -> dict[str, Any]:
    """Create a pure append candidate, never persist it or grant publication."""
    _object(record, "index_record", _INDEX_FIELDS - {"previous_sha256", "record_sha256"}, {"reason"})
    body = {**json.loads(canonical_json(record)), "previous_sha256": _sha(previous_sha256, "previous_sha256")}
    return {**body, "record_sha256": hashlib.sha256(canonical_json(body)).hexdigest()}


def content_diff(left_manifest: dict[str, Any], left_files: dict[str, bytes], right_manifest: dict[str, Any], right_files: dict[str, bytes]) -> dict[str, Any]:
    """Content differences; arbitrary legacy metadata values stay undisplayed.

    Legacy content still contributes fully to fingerprints. Withholding it from
    this display does not certify the package or its prose as desensitized.
    """
    left = package_content_projection(left_manifest, left_files)
    right = package_content_projection(right_manifest, right_files)

    def view(projection: dict[str, Any]) -> dict[str, Any]:
        return {"manifest": projection["manifest"], "skill_trees": {row["skill_name"]: row["tree_fingerprint"] for row in projection["skill_trees"]}, "files": {row["path"]: {"kind": row["kind"], "sha256": row["sha256"]} for row in projection["files"]}}

    changes: list[dict[str, Any]] = []
    missing = object()

    def append(path: str, before: Any, after: Any) -> None:
        if len(changes) >= MAX_DIFF_CHANGES:
            _error("version_diff_limit", "版本差异超过单次有界展示范围。")
        row: dict[str, Any] = {"path": path, "change": "added" if before is missing else "removed" if after is missing else "changed"}
        if path == "/manifest/legacy_source" or path.startswith("/manifest/legacy_source/"):
            # Preserve fingerprint sensitivity while declining to expose any
            # arbitrary legacy metadata values. This is display withholding,
            # not a claim that the underlying asset has been desensitized.
            row["path"] = "/manifest/legacy_source"
            row["redacted"] = True
        else:
            if before is not missing:
                row["before"] = before
            if after is not missing:
                row["after"] = after
        changes.append(row)

    def walk(before: Any, after: Any, path: str) -> None:
        if path == "/manifest/legacy_source":
            # Keys can themselves contain private names, so do not expose
            # arbitrary nested paths either. Compare strict JSON, including
            # bool-vs-int changes that Python dictionary equality would hide.
            if canonical_json(before) != canonical_json(after):
                append(path, before, after)
        elif type(before) is dict and type(after) is dict:
            for key in sorted(before.keys() | after.keys()):
                child_path = path + "/" + key.replace("~", "~0").replace("/", "~1")
                if key not in before or key not in after:
                    append(child_path, before.get(key, missing), after.get(key, missing))
                else:
                    walk(before[key], after[key], child_path)
        elif type(before) is list and type(after) is list:
            for index in range(max(len(before), len(after))):
                child_path = path + "/" + str(index)
                if index >= len(before) or index >= len(after):
                    append(child_path, before[index] if index < len(before) else missing, after[index] if index < len(after) else missing)
                else:
                    walk(before[index], after[index], child_path)
        elif type(before) is not type(after) or before != after:
            append(path, before, after)

    walk(view(left), view(right), "")
    return {"left_fingerprint": hashlib.sha256(canonical_json(left)).hexdigest(), "right_fingerprint": hashlib.sha256(canonical_json(right)).hexdigest(), "changes": changes, "effects_produced": False}


class ImmutableVersionReader:
    """Read through a backend callback that enforces safe-handle containment.

    read_bytes(relative, limit) must never create or repair anything. Published
    manifest.yaml contains canonical UTF-8 JSON (valid YAML), avoiding arbitrary
    YAML constructors/aliases. Only index-visible fingerprints are readable.
    A skill-tree digest callback is required only for versions containing skills;
    it must use the existing tree algorithm over safe regular files. No skill is
    installed. Missing verification cannot be treated as a usable pinned tree.
    """
    def __init__(self, read_bytes: Callable[[str, int], bytes | None], *, read_skill_tree_fingerprint: Callable[[str], str] | None = None, current_index_tail: Callable[[str], str | None] | None = None) -> None:
        self.read_bytes = read_bytes
        self.read_skill_tree_fingerprint = read_skill_tree_fingerprint
        # This callback must consult an independent authoritative server anchor,
        # never rehash or take the last row of the same index read above.
        self.current_index_tail = current_index_tail

    def catalog(self, package_id: str) -> VersionCatalog:
        package_id = _package_id(package_id)
        raw = self.read_bytes(f"employee_packages/{package_id}/versions.jsonl", MAX_LEDGER_BYTES)
        anchor = self.current_index_tail(package_id) if self.current_index_tail is not None else None
        return VersionCatalog.from_index(package_id, raw, expected_tail_sha256=anchor)

    def list_versions(self, package_id: str) -> dict[str, Any]:
        catalog = self.catalog(package_id)
        return {"package_id": package_id, "versions": catalog.list(), "is_current_verified": catalog.is_current_verified, "effects_produced": False}

    def _snapshot(self, package_id: str, *, version: str | None = None, fingerprint: str | None = None, purpose: str = "detail") -> tuple[dict[str, Any], dict[str, bytes]]:
        catalog = self.catalog(package_id)
        selected = catalog.select(version=version, fingerprint=fingerprint, purpose=purpose)
        prefix = f"employee_packages/{package_id}/.versions/{selected['package_fingerprint']}"
        raw = self.read_bytes(prefix + "/manifest.yaml", MAX_LEDGER_BYTES)
        if raw is None:
            _error("immutable_version_invalid", "可见版本缺少不可变清单。")
        manifest = _decode(raw, "manifest")
        problems = validate_package_manifest(manifest, require_complete=True)
        errors = [p for p in problems if p["severity"] == "error" and p["code"] != "employee_package_capability_unverified"]
        if errors or type(manifest) is not dict or manifest.get("package_id") != package_id or manifest.get("version") != selected["version"] or manifest.get("package_fingerprint") != selected["package_fingerprint"] or manifest.get("status") != "published":
            _error("immutable_version_invalid", "不可变清单身份或合同与发布索引不符。")
        names = _contract._declarations(manifest)
        files: dict[str, bytes] = {}
        total = 0
        for path in sorted(names):
            content = self.read_bytes(prefix + "/" + path, _contract.MAX_FILE_BYTES)
            if type(content) is not bytes:
                _error("immutable_version_invalid", "不可变版本缺少声明的原始文件。")
            total += len(content)
            if total > _contract.MAX_FILE_BYTES:
                _error("immutable_version_invalid", "不可变版本总字节超过范围。")
            files[path] = content
        if package_fingerprint(manifest, files) != selected["package_fingerprint"]:
            _error("immutable_version_invalid", "不可变清单与原字节内容指纹不符。")
        for skill in manifest.get("skills", []):
            if self.read_skill_tree_fingerprint is None:
                _error("immutable_skill_tree_unverified", "包含技能的不可变版本缺少安全整树指纹读取器。")
            actual_tree = self.read_skill_tree_fingerprint(prefix + "/skills/" + skill["skill_name"])
            if actual_tree != skill["tree_fingerprint"]:
                _error("immutable_version_invalid", "不可变技能树与固定指纹不符。")
        result = {"package": manifest, "publication": selected, "fingerprint": selected["package_fingerprint"], "retired": selected["status"] == "retired", "existing_pins_usable": True, "new_adoption_allowed": selected["status"] == "published" and catalog.is_current_verified, "is_current_verified": catalog.is_current_verified, "problems": ([{"code": "package_retired", "severity": "warning", "path": "status", "message": "该版本已退役；已冻结在途依据继续有效。", "hint": "新采用请选择当前发布版。"}] if selected["status"] == "retired" else []), "effects_produced": False}
        return result, files

    def read(self, package_id: str, *, version: str | None = None, fingerprint: str | None = None, purpose: str = "detail") -> dict[str, Any]:
        return self._snapshot(package_id, version=version, fingerprint=fingerprint, purpose=purpose)[0]

    def diff(self, package_id: str, left_fingerprint: str, right_fingerprint: str) -> dict[str, Any]:
        left, left_files = self._snapshot(package_id, fingerprint=left_fingerprint, purpose="pinned")
        right, right_files = self._snapshot(package_id, fingerprint=right_fingerprint, purpose="pinned")
        return content_diff(left["package"], left_files, right["package"], right_files)
